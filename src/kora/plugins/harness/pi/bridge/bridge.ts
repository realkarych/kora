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
const fieldPattern = /[ \t]*("(?:[^"\\\u0000-\u001f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*")[ \t]*:[ \t]*("(?:[^"\\\u0000-\u001f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*")[ \t]*(,|})/y;

type Phase = "init" | "starting" | "credential" | "installing" | "prompt" | "running" | "closed";
type Failure = "protocol" | "startup" | "credential" | "prompt" | "stopped";

function decodeField(source: string | undefined): string {
  if (source === undefined) throw new Error();
  const value: unknown = JSON.parse(source);
  if (typeof value !== "string" || !value.isWellFormed()) throw new Error();
  return value;
}

function frameFields(source: Uint8Array): Map<string, string> {
  const text = utf8.decode(source);
  const prefix = /^[ \t]*\{/.exec(text);
  if (!prefix) throw new Error();

  const fields = new Map<string, string>();
  fieldPattern.lastIndex = prefix[0].length;
  while (fields.size < 4) {
    const match = fieldPattern.exec(text);
    if (!match) throw new Error();
    const name = decodeField(match[1]);
    if (fields.has(name)) throw new Error();
    fields.set(name, decodeField(match[2]));
    if (match[3] === "}") {
      if (!/^[ \t]*$/.test(text.slice(fieldPattern.lastIndex))) throw new Error();
      return fields;
    }
  }
  throw new Error();
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
  private session: AgentSession | undefined;
  private models: ModelRuntime | undefined;
  private model: string | undefined;
  private key: string | undefined;

  run(): void {
    process.on("uncaughtException", () => exit(1));
    process.on("unhandledRejection", () => exit(1));
    stdin.on("data", (chunk: Buffer) => this.receive(chunk));
    stdin.on("end", () => this.fail("protocol"));
    stdin.on("error", () => this.fail("protocol"));
    stdout.on("error", () => exit(1));
  }

  private receive(chunk: Buffer): void {
    try {
      const lf = chunk.indexOf(10);
      if (lf !== -1 && lf !== chunk.byteLength - 1) throw new Error();
      const count = lf === -1 ? chunk.byteLength : lf;
      if (this.end + count >= frameLimit) throw new Error();
      chunk.copy(this.buffer, this.end, 0, count);
      this.end += count;
      if (lf === -1) return;

      const frame = this.buffer.subarray(0, this.end);
      try {
        this.accept(frameFields(frame));
      } finally {
        frame.fill(0);
        this.end = 0;
      }
    } catch {
      this.fail("protocol");
    }
  }

  private accept(fields: Map<string, string>): void {
    const kind = fields.get("kind");
    const nonce = fields.get("nonce");
    if (nonce === undefined || !/^[0-9a-f]{32}$/.test(nonce)) throw new Error();

    if (this.phase === "init" && kind === "init" && fields.size === 4) {
      const origin = fields.get("origin");
      const model = fields.get("model");
      if (!origin || !model || !/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}$/.test(origin)
        || new URL(origin).origin !== origin || !/^[a-z0-9][a-z0-9-]{0,127}$/.test(model)) throw new Error();
      this.nonce = nonce;
      this.phase = "starting";
      void this.start(origin, model).catch(() => this.fail("startup"));
      return;
    }

    if (nonce !== this.nonce) throw new Error();
    if (kind === "stop" && fields.size === 2) {
      this.fail("stopped");
      return;
    }
    if (this.phase === "credential" && kind === "credential" && fields.size === 3) {
      const key = fields.get("key");
      this.phase = "installing";
      void this.installCredential(key).catch(() => this.fail("credential"));
      return;
    }
    if (this.phase === "prompt" && kind === "prompt" && fields.size === 3) {
      const text = fields.get("text");
      this.phase = "running";
      void this.prompt(text).catch(() => this.fail("prompt"));
      return;
    }
    throw new Error();
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

  private async installCredential(key: string | undefined): Promise<void> {
    await this.pendingWrite;
    this.stop.signal.throwIfAborted();
    if (!this.models || !key || !/^[!-~]{1,4096}$/.test(key) || key.includes("sk-ant-oat")) throw new Error();
    await this.models.setRuntimeApiKey(provider, key, { signal: this.stop.signal });
    this.stop.signal.throwIfAborted();
    this.key = key;
    this.phase = "prompt";
    this.send({ kind: "credential_ready" });
  }

  private async prompt(text: string | undefined): Promise<void> {
    await this.pendingWrite;
    this.stop.signal.throwIfAborted();
    const session = this.session;
    if (!session || text === undefined || !text.length || Buffer.byteLength(text) > promptLimit
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
    try {
      this.session?.dispose();
    } catch {
      exit(1);
    }
    if (this.pendingWrite || !this.nonce) exit(1);
    this.send({ kind: "failure", code }, 1);
  }

  private send(value: Record<string, string>, exitCode?: number): void {
    const frame = JSON.stringify({ ...value, nonce: this.nonce }) + "\n";
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
