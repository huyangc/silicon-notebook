# Managed companion-service example

[中文](./README_zh.md)

This installable sample demonstrates the companion-process contract. Its bundle
declares no UI or application contributions and starts nothing when imported.
The separate foreground module serves `GET /health` on loopback, stays alive until
SIGTERM/SIGINT, and closes its listener before exiting. It is a lifecycle example,
not a production HTTP server or an automatic discovery mechanism.

From the Silicon Notebook repository root, activate the backend's Python
environment so `python3` selects that interpreter, then install the sample:

```bash
python3 -m pip install -e examples/extensions/managed-service
export EXTENSIONS_CONFIG="$PWD/examples/extensions/managed-service/extensions.example.toml"
bash scripts/cli.sh extensions check
bash scripts/cli.sh extensions services validate
npm run dev
```

The ordinary startup waits for this service's health endpoint before starting
the application. Development exit also stops the service created for that run.
Alternatively, operate just companion services:

```bash
bash scripts/cli.sh extensions services start
bash scripts/cli.sh extensions services status
bash scripts/cli.sh extensions services logs
bash scripts/cli.sh extensions services stop
```

If activation is unsuitable, set `command[0]` in your deployment copy of the
TOML to the absolute executable path of the interpreter where you installed the
sample. `cwd = "."` resolves against the TOML directory, not the caller's directory.
Do not move this example into an existing production configuration without
checking its loopback port is available; choose a free port in both the command
and health URL. Startup never installs dependencies.

The sample writes no request logs. The supervisor discards process stdout/stderr
and records safe lifecycle metadata only. A real plugin should configure its own
sanitized diagnostic sink and report healthy only after its actual dependencies
and initialization are ready. If wrapping a service in a shell script, use
`exec` and keep it in the foreground so termination reaches the managed group.

For an independently managed instance, replace the service table with:

```toml
[extensions."examples.managed_service".services.health]
mode = "external"
healthcheck_url = "http://127.0.0.1:9100/health"
```

Start that instance yourself with
`python3 -m silicon_notebook_managed_service.server --port 9100` before the
application. Core checks readiness and leaves external processes running on stop.
The `external` table must not contain a start command, working directory, or
environment overrides. Batch commands do not implicitly start either mode.

For environment references, dependency ordering, accepted timeouts and the full
author contract, see the [extension SOP](../../../docs/deployment-extensions-sop.md#companion-service-delivery-contract).
Example tests use fake handlers and a fake server; they never bind a host socket.
