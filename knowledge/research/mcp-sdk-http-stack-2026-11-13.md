# MCP SDK HTTP stack in the stdio server (checked 2026-11-13)

This record answers the open implementation question "MCP SDK HTTP stack" of the [architecture reference](../architecture/detailed.md#open-implementation-questions) for the first kora MCP server, the stdio authoring server of [#1436](https://github.com/realkarych/kora/issues/1436).

## Facts

Checked on 2026-11-13 against the committed `uv.lock` and the installed packages on macOS, Python 3.11.17.

- `kora[mcp]` pins `mcp==2.3.0`; `kora[claude]` includes the `mcp` extra.
- `mcp` 2.3.0 depends on `anyio`, `httpx2`, `jsonschema`, `mcp-types`, `opentelemetry-api`, `pydantic`, `pyjwt`, `python-multipart`, `sse-starlette`, `starlette`, `typing-extensions`, `typing-inspection`, `uvicorn` and, on Windows only, `pywin32`.
- `httpx2` 2.16.0 depends on `httpcore2`, `httpx2-jsfetch`, `idna`, `truststore`, `anyio` and `typing-extensions`. It is a separately named HTTP client stack, not the `httpx` 0.28.1 that the shared kora client uses.
- Importing `kora.mcp.stdio` loads `httpx`, `httpcore`, `httpx2`, `starlette`, `sse_starlette` and `uvicorn` into the process. It does not load `truststore`, and the `ssl.SSLContext` class is unchanged after the import.

## Answer

The SDK brings a second HTTP client stack, and the stdio server process imports it. The process sends every API request through the shared client's `httpx` transport and its bearer and destination policy; nothing in kora calls `httpx2` or the SDK's client transports, and the import changes no global TLS state. A separate stack decision is needed only when a kora component starts to use `httpx2` or an SDK client transport, or when the HTTP-mounted servers add the SDK's server transports to the API process.

## Method

```bash
uv run --no-sync python -c "import tomllib; d = tomllib.load(open('uv.lock', 'rb')); print([p['dependencies'] for p in d['package'] if p['name'] == 'mcp'])"
uv run --no-sync python -c "import sys, ssl; before = ssl.SSLContext; import kora.mcp.stdio; print(sorted({m.split('.')[0] for m in sys.modules} & {'httpx', 'httpx2', 'httpcore', 'httpcore2', 'truststore'}), ssl.SSLContext is before)"
```
