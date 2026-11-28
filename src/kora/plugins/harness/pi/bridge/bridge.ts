import { randomBytes } from "node:crypto";
import { isDeepStrictEqual } from "node:util";
import { cwd, exit, hrtime, stdin, stdout } from "node:process";
import { InMemoryCredentialStore } from "@earendil-works/pi-ai";
import { type ServerSentEvent, SseDecoder } from "@earendil-works/pi-ai/utils/sse";
import {
  type AgentSession,
  ModelRuntime,
  type ResourceLoader,
  SessionManager,
  SettingsManager,
  createAgentSession,
  createExtensionRuntime,
  defineTool,
} from "@earendil-works/pi-coding-agent";

const frameLimit = 64 * 1024;
const promptLimit = 32 * 1024;
const responseLimit = 256 * 1024;
const provider = "anthropic";
const messagesPath = "/v1/messages?beta=true";
const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
const exchangeId = /^[0-9a-f]{32}$/;
const callId = /^[a-zA-Z0-9_-]{1,64}$/;
const cutoffText = /^[1-9][0-9]{0,18}$/;
const shellResultLimit = 8 * 1024;
const commandLimit = frameLimit / 2;
const bashDescription = "Run the approved command in the prepared checkout.";
const bashParameters = {
  type: "object",
  properties: { command: { type: "string" } },
  required: ["command"],
  additionalProperties: false,
};

type BashCall = { id: string; command: string };
type PendingCall = {
  id: string;
  challenge: string;
  resolve: (text: string) => void;
  reject: () => void;
};

type Phase = "init" | "starting" | "credential" | "installing" | "prompt" | "running" | "closed";
type Failure = "protocol" | "startup" | "credential" | "prompt" | "stopped";

function frameFields(source: Uint8Array): Record<string, string> {
  const text = utf8.decode(source);
  const parsed: unknown = JSON.parse(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
  const values: unknown[] = Object.values(parsed);
  if (values.length > 6 || values.some((value) => typeof value !== "string" || !value.isWellFormed())
    || JSON.stringify(parsed) !== text) throw new Error();
  return parsed as Record<string, string>;
}

function emptyResources(): ResourceLoader {
  const extensions = { extensions: [], errors: [], runtime: createExtensionRuntime() };
  return {
    getExtensions: () => extensions,
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => undefined,
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [],
    getAppendSystemPromptSources: () => [],
    extendResources: () => { throw new Error(); },
    reload: async () => { throw new Error(); },
  };
}

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) throw new Error();
  return value as Record<string, unknown>;
}

function list(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error();
  return value;
}

function strictJson(source: string): unknown {
  const value: unknown = JSON.parse(source);
  const objects: Set<string>[] = [];
  for (let index = 0; index < source.length; index++) {
    const token = source[index];
    if (token === '"') {
      const start = index++;
      while (source[index] !== '"') {
        index += source[index] === "\\" ? 2 : 1;
      }
      let after = index + 1;
      while (/[ \n\r\t]/.test(source[after] ?? "")) after++;
      if (source[after] !== ":") continue;
      const key: unknown = JSON.parse(source.slice(start, index + 1));
      const keys = objects.at(-1);
      if (typeof key !== "string" || !keys || keys.has(key)) throw new Error();
      keys.add(key);
    } else if (token === "{") {
      objects.push(new Set());
    } else if (token === "}") {
      objects.pop();
    }
  }
  return value;
}

function commandOf(value: unknown): string {
  const fields = object(value);
  const command = fields.command;
  if (Object.keys(fields).length !== 1 || typeof command !== "string"
    || !command.length || !command.isWellFormed() || command.includes("\0")
    || Buffer.byteLength(command) > commandLimit) throw new Error();
  return command;
}

class MessageWitness {
  private readonly decoder = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
  private readonly events = new SseDecoder();
  private state: "start" | "blocks" | "stop" | "ended" = "start";
  private nextIndex = 0;
  private open: { type: string; input?: { id: string; parts: string[]; bytes: number } } | undefined;
  private call: BashCall | undefined;

  private readonly model: string;
  private readonly bash: boolean;

  constructor(model: string, bash: boolean) {
    this.model = model;
    this.bash = bash;
  }

  push(chunk: Uint8Array): void {
    this.decoder.decode(chunk, { stream: true });
    for (const event of this.events.push(chunk)) this.accept(event);
  }

  finish(): BashCall | undefined {
    this.decoder.decode();
    if (this.events.end().length || this.state !== "ended") throw new Error();
    return this.call;
  }

  private accept(event: ServerSentEvent): void {
    const data = object(strictJson(event.data));
    if (event.event !== data.type || this.state === "ended") throw new Error();
    if (data.type === "ping") return;
    if (this.state === "start" && data.type === "message_start") {
      const message = object(data.message);
      if (message.model !== this.model || message.role !== "assistant" || message.type !== "message"
        || list(message.content).length || message.stop_reason !== null) throw new Error();
      this.state = "blocks";
    } else if (this.state === "blocks" && data.type === "content_block_start") {
      if (this.open || data.index !== this.nextIndex) throw new Error();
      const block = object(data.content_block);
      if (block.type === "tool_use") {
        if (!this.bash || this.call || block.name !== "bash" || typeof block.id !== "string"
          || !callId.test(block.id) || Object.keys(object(block.input)).length) throw new Error();
        this.open = { type: block.type, input: { id: block.id, parts: [], bytes: 0 } };
      } else {
        const text = block.type === "text" && typeof block.text === "string";
        const thinking = block.type === "thinking" && typeof block.thinking === "string"
          && (!Object.hasOwn(block, "signature") || typeof block.signature === "string");
        if ((!text && !thinking) || typeof block.type !== "string") throw new Error();
        this.open = { type: block.type };
      }
    } else if (this.state === "blocks" && data.type === "content_block_delta") {
      if (!this.open || data.index !== this.nextIndex) throw new Error();
      this.delta(object(data.delta), this.open);
    } else if (this.state === "blocks" && data.type === "content_block_stop") {
      if (!this.open || data.index !== this.nextIndex) throw new Error();
      if (this.open.input) {
        const { id, parts } = this.open.input;
        this.call = { id, command: commandOf(strictJson(parts.join(""))) };
      }
      this.open = undefined;
      this.nextIndex++;
    } else if (this.state === "blocks" && data.type === "message_delta") {
      if (this.open || !this.nextIndex || this.bash !== (this.call !== undefined)
        || object(data.delta).stop_reason !== (this.bash ? "tool_use" : "end_turn")
        || Object.hasOwn(data, "input_transformations")) throw new Error();
      this.state = "stop";
    } else if (this.state === "stop" && data.type === "message_stop") {
      this.state = "ended";
    } else {
      throw new Error();
    }
  }

  private delta(delta: Record<string, unknown>, open: NonNullable<MessageWitness["open"]>): void {
    if (open.input) {
      if (delta.type !== "input_json_delta" || typeof delta.partial_json !== "string"
        || !delta.partial_json.length) throw new Error();
      open.input.bytes += Buffer.byteLength(delta.partial_json);
      if (open.input.bytes > commandLimit) throw new Error();
      open.input.parts.push(delta.partial_json);
    } else if (!(open.type === "text" && delta.type === "text_delta" && typeof delta.text === "string")
      && !(open.type === "thinking" && delta.type === "thinking_delta" && typeof delta.thinking === "string")
      && !(open.type === "thinking" && delta.type === "signature_delta" && typeof delta.signature === "string")) {
      throw new Error();
    }
  }
}

async function boundedBody(request: Request, signal: AbortSignal): Promise<ArrayBuffer> {
  const reader = request.body?.getReader();
  if (!reader) throw new Error();
  const bytes = new Uint8Array(responseLimit);
  const cancel = () => { void reader.cancel().catch(() => undefined); };
  signal.addEventListener("abort", cancel, { once: true });
  let size = 0;
  try {
    for (;;) {
      signal.throwIfAborted();
      const next = await reader.read();
      signal.throwIfAborted();
      if (next.done) return bytes.buffer.slice(0, size);
      if (next.value.byteLength > bytes.length - size) throw new Error();
      bytes.set(next.value, size);
      size += next.value.byteLength;
    }
  } finally {
    signal.removeEventListener("abort", cancel);
    cancel();
    reader.releaseLock();
  }
}

function requireCredentialFree(root: unknown, key: string): void {
  const pending = [root];
  while (pending.length) {
    const value = pending.pop();
    if (typeof value === "string" && value.includes(key)) throw new Error();
    if (value !== null && typeof value === "object") {
      for (const [name, member] of Object.entries(value)) {
        if (name.includes(key)) throw new Error();
        pending.push(member);
      }
    }
  }
}

class ModelExchange {
  private used = 0;
  private remaining = responseLimit;
  private initial: unknown[] | undefined;
  call: BashCall | undefined;
  result: string | undefined;
  terminal = false;

  private readonly destination: string;
  private readonly model: string;
  private readonly bash: boolean;
  private readonly signal: AbortSignal;
  private readonly cutoff: bigint | undefined;
  private readonly credential: () => string | undefined;

  constructor(
    destination: string,
    model: string,
    bash: boolean,
    signal: AbortSignal,
    cutoff: bigint | undefined,
    credential: () => string | undefined,
  ) {
    this.destination = destination;
    this.model = model;
    this.bash = bash;
    this.signal = signal;
    this.cutoff = cutoff;
    this.credential = credential;
  }

  private requireCurrent(signal: AbortSignal): void {
    signal.throwIfAborted();
    if (this.cutoff !== undefined && hrtime.bigint() >= this.cutoff) throw new Error();
  }

  readonly fetch: typeof fetch = async (input, init) => {
    this.signal.throwIfAborted();
    const request = new Request(input, init);
    const first = this.used === 0;
    if (request.url !== this.destination || request.method !== "POST"
      || (!first && (this.used !== 1 || !this.bash || !this.call || this.result === undefined))) throw new Error();
    this.used++;
    const signal = AbortSignal.any([request.signal, this.signal]);
    const bytes = await boundedBody(request, signal);
    const source = utf8.decode(bytes);
    const key = this.credential();
    if (!key || source.includes(key)) throw new Error();
    const body = object(strictJson(source));
    requireCredentialFree(body, key);
    this.requireRequest(body, first);
    this.requireCurrent(signal);
    const response = await fetch(request.url, {
      method: request.method,
      headers: request.headers,
      body: bytes,
      redirect: "error",
      signal,
    });
    if (!response.ok || !response.body) throw new Error();
    const witness = new MessageWitness(this.model, this.bash && first);
    const stream = response.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
      transform: (chunk, output) => {
        this.remaining -= chunk.byteLength;
        if (this.remaining < 0) throw new Error();
        witness.push(chunk);
        output.enqueue(chunk);
      },
      flush: () => {
        const call = witness.finish();
        if (call) this.call = call;
        else this.terminal = true;
      },
    }));
    return new Response(stream, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  };

  private requireRequest(body: Record<string, unknown>, first: boolean): void {
    const tools = list(body.tools ?? []);
    if (body.model !== this.model || body.stream !== true || Object.hasOwn(body, "fallbacks")
      || tools.length !== Number(this.bash)) throw new Error();
    if (this.bash) {
      const tool = object(tools[0]);
      if (tool.name !== "bash" || tool.description !== bashDescription
        || !isDeepStrictEqual(tool.input_schema, bashParameters)
        || (Object.hasOwn(tool, "eager_input_streaming") && tool.eager_input_streaming !== true)
        || Object.keys(tool).some((key) => !["name", "description", "input_schema", "eager_input_streaming"].includes(key))) throw new Error();
    }
    const history = list(body.messages);
    if (first) {
      if (history.length !== 1 || object(history[0]).role !== "user") throw new Error();
      this.initial = history;
      return;
    }
    if (!this.initial || history.length !== 3 || !isDeepStrictEqual(history.slice(0, 1), this.initial)) throw new Error();
    const assistant = object(history[1]);
    const user = object(history[2]);
    if (assistant.role !== "assistant" || user.role !== "user") throw new Error();
    const blocks = list(assistant.content).map(object);
    const toolsUsed = blocks.filter((block) => block.type === "tool_use");
    if (toolsUsed.length !== 1 || blocks.some((block) => !["tool_use", "text", "thinking"].includes(String(block.type)))
      || !isDeepStrictEqual(toolsUsed[0], {
        type: "tool_use", id: this.call?.id, name: "bash", input: { command: this.call?.command },
      })
      || !isDeepStrictEqual(user.content, [{
        type: "tool_result", tool_use_id: this.call?.id, content: this.result, is_error: false,
      }])) throw new Error();
  }
}

class Bridge {
  private phase: Phase = "init";
  private readonly buffer = Buffer.alloc(frameLimit - 1);
  private readonly stop = new AbortController();
  private end = 0;
  private pendingWrite: Promise<void> | undefined;
  private nonce: string | undefined;
  private staged: (() => void) | undefined;
  private readonly challenges: string[] = [];
  private session: AgentSession | undefined;
  private models: ModelRuntime | undefined;
  private model: string | undefined;
  private key: string | undefined;
  private exchange: ModelExchange | undefined;
  private pendingCall: PendingCall | undefined;
  private called = false;

  run(): void {
    process.on("uncaughtException", () => exit(1));
    process.on("unhandledRejection", () => exit(1));
    stdin.on("data", (chunk: Buffer) => this.receive(chunk));
    stdin.on("end", () => this.fail("stopped"));
    stdin.on("error", () => this.fail("protocol"));
    stdout.on("error", () => exit(1));
  }

  private receive(chunk: Buffer): void {
    try {
      let offset = 0;
      while (offset < chunk.byteLength) {
        const lf = chunk.indexOf(10, offset);
        const end = lf === -1 ? chunk.byteLength : lf;
        const count = end - offset;
        if (this.end + count >= frameLimit) throw new Error();
        chunk.copy(this.buffer, this.end, offset, end);
        this.end += count;
        if (lf === -1) return;

        const frame = this.buffer.subarray(0, this.end);
        try {
          const fields = frameFields(frame);
          if (fields.kind === "activate" && lf + 1 !== chunk.byteLength) throw new Error();
          this.accept(fields);
        } finally {
          frame.fill(0);
          this.end = 0;
        }
        offset = lf + 1;
      }
    } catch {
      this.fail("protocol");
    }
  }

  private accept(fields: Record<string, string>): void {
    const { kind, nonce, challenge, origin, model, key, text, id, call, result, profile, cutoff } = fields;
    const count = Object.keys(fields).length;
    if (!nonce || !exchangeId.test(nonce) || (this.nonce !== undefined && nonce !== this.nonce)) throw new Error();
    if (!this.staged && kind === "bash_result" && this.phase === "running" && count === 6
      && this.pendingCall && call === this.pendingCall.challenge && id === this.pendingCall.id
      && challenge === this.challenges.at(-1) && result !== undefined
      && Buffer.byteLength(result) <= shellResultLimit) {
      const pending = this.pendingCall;
      this.stage("running", async () => pending.resolve(result), "prompt");
      return;
    }
    if (kind === "activate") {
      const activate = this.staged;
      if (!activate || count !== 3 || !challenge || !exchangeId.test(challenge)
        || challenge === nonce || challenge === this.pendingCall?.challenge
        || this.challenges.includes(challenge)) throw new Error();
      this.staged = undefined;
      this.challenges.push(challenge);
      activate();
      return;
    }

    if (this.staged) throw new Error();
    if (this.phase === "init" && kind === "init"
      && (count === 4 && profile === undefined && cutoff === undefined
        || count === 6 && profile === "bash" && cutoff !== undefined && cutoffText.test(cutoff))
      && origin && model && /^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(origin)
      && new URL(origin).origin === origin && /^[a-z0-9][a-z0-9-]{0,127}$/.test(model)) {
      this.nonce = nonce;
      const deadline = cutoff === undefined ? undefined : BigInt(cutoff);
      if (deadline !== undefined && deadline > (1n << 63n) - 1n) throw new Error();
      this.stage("starting", () => this.start(origin, model, deadline), "startup");
    } else if (this.phase === "credential" && kind === "credential" && count === 3 && key !== undefined) {
      this.stage("installing", () => this.installCredential(key), "credential");
    } else if (this.phase === "prompt" && kind === "prompt" && count === 3 && text !== undefined) {
      this.stage("running", () => this.prompt(text), "prompt");
    } else {
      throw new Error();
    }
  }

  private stage(phase: Phase, action: () => Promise<void>, code: Failure): void {
    this.staged = () => {
      this.phase = phase;
      void action().catch(() => this.fail(code));
    };
  }

  private async start(origin: string, modelId: string, cutoff: bigint | undefined): Promise<void> {
    const bash = cutoff !== undefined;
    const models = await ModelRuntime.create({
      credentials: new InMemoryCredentialStore(),
      modelsPath: null,
      allowModelNetwork: false,
      refreshOnCreate: false,
      signal: this.stop.signal,
    });
    const model = models.getPhysicalModel(provider, modelId);
    if (model?.api !== "anthropic-messages" || model.provider !== provider || model.id !== modelId) throw new Error();

    const workspace = cwd();
    const execute = async (id: string, args: unknown, signal?: AbortSignal) => {
      const text = await this.bash(id, commandOf(args), signal);
      return { content: [{ type: "text" as const, text }], details: undefined };
    };
    const tool = defineTool({
      name: "bash", label: "Bash", description: bashDescription, parameters: bashParameters,
      constrainedSampling: false, execute,
    });
    const { session } = await createAgentSession({
      cwd: workspace,
      agentDir: workspace,
      modelRuntime: models,
      model: { ...model, baseUrl: origin },
      thinkingLevel: "off",
      noTools: "all",
      tools: bash ? ["bash"] : [],
      customTools: bash ? [tool] : [],
      resourceLoader: emptyResources(),
      sessionManager: SessionManager.inMemory(workspace),
      settingsManager: SettingsManager.inMemory({
        compaction: { enabled: false },
        retry: { enabled: false, provider: { maxRetries: 0 } },
        cacheWarming: "off",
        enableInstallTelemetry: false,
      }, { projectTrusted: false }),
    });
    this.session = session;
    this.stop.signal.throwIfAborted();
    const names = bash ? ["bash"] : [];
    const all = session.getAllTools();
    const active = session.agent.state.tools[0];
    const registered = all[0];
    if (!isDeepStrictEqual(session.getActiveToolNames(), names)
      || !isDeepStrictEqual(session.getCallableToolNames(), names)
      || all.length !== names.length) throw new Error();
    if (bash && (session.getToolDefinition("bash") !== tool || tool.execute !== execute
      || registered?.name !== "bash" || registered.parameters !== tool.parameters
      || registered.sourceInfo?.source !== "sdk" || registered.sourceInfo.path !== "<sdk:bash>"
      || registered.exposure !== "direct" || active?.name !== "bash"
      || active.parameters !== tool.parameters || typeof active.execute !== "function"
      || active.execute === execute)) throw new Error();
    const stream = session.agent.streamFunction;
    const exchange = new ModelExchange(origin + messagesPath, modelId, bash, this.stop.signal, cutoff, () => this.key);
    this.exchange = exchange;
    session.agent.streamFunction = (selected, context, options) => {
      if (this.phase !== "running" || selected.id !== modelId || selected.provider !== provider
        || selected.api !== "anthropic-messages" || selected.baseUrl !== origin) throw new Error();
      return stream(selected, context, {
        ...options,
        fetch: exchange.fetch,
        cacheRetention: "none",
        maxRetries: 0,
        env: {},
      });
    };
    this.models = models;
    this.model = modelId;
    this.phase = "credential";
    this.send({ kind: "ready" });
  }

  private async installCredential(key: string): Promise<void> {
    await this.pendingWrite;
    this.stop.signal.throwIfAborted();
    if (!this.models || !key || !/^[!-~]{1,4096}$/.test(key) || key.includes("sk-ant-oat")) throw new Error();
    await this.models.setRuntimeApiKey(provider, key, { signal: this.stop.signal });
    this.stop.signal.throwIfAborted();
    this.key = key;
    this.phase = "prompt";
    this.send({ kind: "credential_ready" });
  }

  private async bash(id: string, command: string, signal?: AbortSignal): Promise<string> {
    signal?.throwIfAborted();
    this.stop.signal.throwIfAborted();
    const exchange = this.exchange;
    if (this.phase !== "running" || this.called || !exchange || exchange.call?.id !== id
      || exchange.call.command !== command || !this.key || command.includes(this.key)) throw new Error();
    this.called = true;
    const challenge = randomBytes(16).toString("hex");
    if (this.challenges.includes(challenge)) throw new Error();
    const reply = new Promise<string>((resolve, reject) => {
      this.pendingCall = { id, challenge, resolve, reject: () => reject(new Error()) };
    });
    const stopped = () => this.fail("stopped");
    signal?.addEventListener("abort", stopped, { once: true });
    this.send({ kind: "bash_call", id, call: challenge, command });
    try {
      const result = await reply;
      signal?.throwIfAborted();
      this.stop.signal.throwIfAborted();
      if (result.includes(this.key)) throw new Error();
      exchange.result = result;
      return result;
    } finally {
      signal?.removeEventListener("abort", stopped);
      this.pendingCall = undefined;
    }
  }

  private async prompt(text: string): Promise<void> {
    await this.pendingWrite;
    this.stop.signal.throwIfAborted();
    const session = this.session;
    if (!session || !text.length || Buffer.byteLength(text) > promptLimit
      || !this.key || text.includes(this.key)) throw new Error();

    await session.prompt(text, { expandPromptTemplates: false });
    if (this.phase !== "running") return;
    const message = session.messages.at(-1);
    if (!this.exchange?.terminal || message?.role !== "assistant" || message.stopReason !== "stop"
      || message.rawStopReason !== "end_turn" || message.provider !== provider
      || message.model !== this.model
      || (Object.hasOwn(message, "responseModel") && message.responseModel !== this.model)) throw new Error();
    const segments = message.content.flatMap((part) => part.type === "text" ? [part.text] : []);
    if (segments.some((segment) => typeof segment !== "string" || !segment.isWellFormed())) throw new Error();
    const answer = segments.join("");
    if (!answer.length || answer.includes(this.key)) throw new Error();
    session.dispose();
    this.phase = "closed";
    this.send({ kind: "terminal", text: answer }, 0);
  }

  private fail(code: Failure): void {
    if (this.phase === "closed") return;
    this.phase = "closed";
    this.stop.abort();
    this.buffer.fill(0);
    this.key = undefined;
    this.staged = undefined;
    this.pendingCall?.reject();
    try {
      this.session?.dispose();
    } catch {
      exit(1);
    }
    if (this.pendingWrite || !this.challenges.length) exit(1);
    this.send({ kind: "failure", code }, 1);
  }

  private send(value: Record<string, string>, exitCode?: number): void {
    const frame = JSON.stringify({ ...value, nonce: this.nonce, challenge: this.challenges.at(-1) }) + "\n";
    if (this.pendingWrite || Buffer.byteLength(frame) > frameLimit) exit(1);
    this.pendingWrite = new Promise((written) => {
      stdout.write(frame, "utf8", (error) => {
        this.pendingWrite = undefined;
        if (error) exit(1);
        if (exitCode !== undefined) exit(exitCode);
        written();
      });
    });
  }
}

new Bridge().run();
