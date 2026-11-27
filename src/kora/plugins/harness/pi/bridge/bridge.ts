import { cwd, exit, stdin, stdout } from "node:process";
import { InMemoryCredentialStore } from "@earendil-works/pi-ai";
import {
  type AgentSession,
  ModelRuntime,
  type ResourceLoader,
  SessionManager,
  SettingsManager,
  createAgentSession,
  createExtensionRuntime,
} from "@earendil-works/pi-coding-agent";

const frameLimit = 64 * 1024;
const promptLimit = 32 * 1024;
const responseLimit = 256 * 1024;
const provider = "anthropic";
const messagesPath = "/v1/messages?beta=true";
const utf8 = new TextDecoder("utf-8", { fatal: true, ignoreBOM: true });
const exchangeId = /^[0-9a-f]{32}$/;

type Phase = "init" | "starting" | "credential" | "installing" | "prompt" | "running" | "closed";
type Failure = "protocol" | "startup" | "credential" | "prompt" | "stopped";

function frameFields(source: Uint8Array): Record<string, string> {
  const text = utf8.decode(source);
  const parsed: unknown = JSON.parse(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
  const values: unknown[] = Object.values(parsed);
  if (values.length > 4 || values.some((value) => typeof value !== "string" || !value.isWellFormed())
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

function messagesFetch(destination: string, signal: AbortSignal): typeof fetch {
  let used = false;
  return async (input, init) => {
    signal.throwIfAborted();
    const request = new Request(input, init);
    if (used || request.url !== destination || request.method !== "POST") throw new Error();
    used = true;
    const response = await fetch(request, {
      redirect: "error",
      signal: AbortSignal.any([request.signal, signal]),
    });
    if (!response.body) return response;
    let remaining = responseLimit;
    const body = response.body.pipeThrough(new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, output) {
        remaining -= chunk.byteLength;
        if (remaining < 0) throw new Error();
        output.enqueue(chunk);
      },
    }));
    return new Response(body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  };
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
    const { kind, nonce, challenge, origin, model, key, text } = fields;
    const count = Object.keys(fields).length;
    if (!nonce || !exchangeId.test(nonce) || (this.nonce !== undefined && nonce !== this.nonce)) throw new Error();
    if (kind === "activate") {
      const activate = this.staged;
      if (!activate || count !== 3 || !challenge || !exchangeId.test(challenge)
        || this.challenges.includes(challenge)) throw new Error();
      this.staged = undefined;
      this.challenges.push(challenge);
      activate();
      return;
    }

    if (this.staged) throw new Error();
    if (this.phase === "init" && kind === "init" && count === 4
      && origin && model && /^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(origin)
      && new URL(origin).origin === origin && /^[a-z0-9][a-z0-9-]{0,127}$/.test(model)) {
      this.nonce = nonce;
      this.stage("starting", () => this.start(origin, model), "startup");
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

  private async start(origin: string, modelId: string): Promise<void> {
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
    const { session } = await createAgentSession({
      cwd: workspace,
      agentDir: workspace,
      modelRuntime: models,
      model: { ...model, baseUrl: origin },
      thinkingLevel: "off",
      noTools: "all",
      tools: [],
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
    if (session.getAllTools().length || session.getActiveToolNames().length || session.getCallableToolNames().length) {
      throw new Error();
    }
    const stream = session.agent.streamFunction;
    const guardedFetch = messagesFetch(origin + messagesPath, this.stop.signal);
    session.agent.streamFunction = (selected, context, options) => {
      if (this.phase !== "running" || selected.id !== modelId || selected.provider !== provider
        || selected.api !== "anthropic-messages" || selected.baseUrl !== origin) throw new Error();
      return stream(selected, context, {
        ...options,
        fetch: guardedFetch,
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

  private async prompt(text: string): Promise<void> {
    await this.pendingWrite;
    this.stop.signal.throwIfAborted();
    const session = this.session;
    if (!session || !text.length || Buffer.byteLength(text) > promptLimit
      || !this.key || text.includes(this.key)) throw new Error();

    await session.prompt(text, { expandPromptTemplates: false });
    if (this.phase !== "running") return;
    const message = session.messages.at(-1);
    if (message?.role !== "assistant" || message.stopReason !== "stop"
      || message.rawStopReason !== "end_turn" || message.provider !== provider
      || message.model !== this.model
      || (Object.hasOwn(message, "responseModel") && message.responseModel !== this.model)) throw new Error();
    const answer = message.content.flatMap((part) => part.type === "text" ? [part.text] : []).join("");
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
