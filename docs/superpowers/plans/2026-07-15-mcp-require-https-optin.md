# MCP `MCP_REQUIRE_HTTPS` (default off) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MCP HTTPS enforcement opt-in via `MCP_REQUIRE_HTTPS` (default off = allow remote plain HTTP + skip Host/Origin checks, with a loud startup warning), so intranet deploys work out of the box.

**Architecture:** Three enforcement points (`validate_mcp_deployment`, `AgentBearerMiddleware` request guard, `TransportSecuritySettings` DNS-rebinding) each gain a `require_https` flag whose function-signature default stays `True` (fail-closed primitive). The product-wide "default open" is a single deployment decision in the composition root `app.main.create_app`, which reads `MCP_REQUIRE_HTTPS` (env default `False`) and threads that value down.

**Tech Stack:** Python, FastAPI/Starlette, official `mcp` SDK (FastMCP + TransportSecurityMiddleware), pytest (`anyio`), httpx ASGITransport.

## Global Constraints

- Env var name: `MCP_REQUIRE_HTTPS`. Truthy = one of `{"1","true","yes","on"}`, case-insensitive, stripped. Anything else (incl. unset) = false.
- Function-signature defaults stay secure: `require_https=True` for `validate_mcp_deployment`, `create_memory_mcp`, `AgentBearerMiddleware`. Only `app.main.create_app` opts into `False` via env (env default `False`).
- No schema change (no `SCHEMA_VERSION` / `_migration_N`).
- Docs stay generic (no machine-specific paths/ports); keep `README.md` and `README_zh.md` in sync.
- Fixture golden `facade_surface.json` must NOT be regenerated — accommodate line shifts via the `LINE_NUMBER_INSENSITIVE_FILES` allowlist (per "fixture 冻结走 allowlist 不 regen").
- Commit style: end messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

### Task 1: `validate_mcp_deployment` gains `require_https` (+ module logger + surface-manifest allowlist)

**Files:**
- Modify: `backend/app/api/mcp_server.py` (imports ~10-15; `validate_mcp_deployment` at 64-72)
- Modify: `backend/tests/test_repository_surface_manifest.py` (`LINE_NUMBER_INSENSITIVE_FILES` set, ~2002-2030)
- Create: `backend/tests/test_mcp_https_policy.py`

**Interfaces:**
- Produces: `validate_mcp_deployment(bind_host: str, public_url: str, *, require_https: bool = True) -> None`. When remotely reachable + non-HTTPS: raises `RuntimeError("remote MCP deployment requires HTTPS")` if `require_https`, else logs a WARNING and returns.
- Produces: module-level `logger = logging.getLogger(__name__)` in `mcp_server.py`.

- [ ] **Step 1: Prep the surface manifest (line-shift accommodation, no-op today)**

In `backend/tests/test_repository_surface_manifest.py`, add `mcp_server.py` to `LINE_NUMBER_INSENSITIVE_FILES` (near the other API entry files like `deps.py`/`routes.py`):

```python
    # MCP HTTPS opt-in (MCP_REQUIRE_HTTPS): validate_mcp_deployment +
    # create_memory_mcp + AgentBearerMiddleware gain a require_https param and a
    # module logger above this file's facade consumers, shifting those call
    # sites' lines without changing the surface. Internal line numbers here are
    # not API surface.
    "backend/app/api/mcp_server.py",
```

- [ ] **Step 2: Verify the manifest test is still green (normalization is a no-op before any shift)**

Run: `cd backend && python -m pytest tests/test_repository_surface_manifest.py -q`
Expected: PASS (all).

- [ ] **Step 3: Write the failing tests**

Create `backend/tests/test_mcp_https_policy.py`:

```python
"""MCP_REQUIRE_HTTPS opt-in policy: default off (intranet-friendly)."""
from __future__ import annotations

import logging

import pytest

from app.api import mcp_server
from app.api.mcp_server import validate_mcp_deployment


def test_validate_default_requires_https():
    with pytest.raises(RuntimeError, match="requires HTTPS"):
        validate_mcp_deployment("0.0.0.0", "http://10.0.0.5:8000/mcp")


def test_validate_still_raises_when_required_explicitly():
    with pytest.raises(RuntimeError, match="requires HTTPS"):
        validate_mcp_deployment(
            "0.0.0.0", "http://10.0.0.5:8000/mcp", require_https=True
        )


def test_validate_allows_plain_http_when_not_required(caplog):
    with caplog.at_level(logging.WARNING, logger="app.api.mcp_server"):
        validate_mcp_deployment(
            "0.0.0.0", "http://10.0.0.5:8000/mcp", require_https=False
        )
    assert any("cleartext" in r.getMessage() for r in caplog.records)


def test_validate_loopback_never_warns_or_raises(caplog):
    with caplog.at_level(logging.WARNING, logger="app.api.mcp_server"):
        # loopback bind + loopback public url: not remotely reachable
        validate_mcp_deployment(
            "127.0.0.1", "http://127.0.0.1:8000/mcp", require_https=False
        )
    assert not caplog.records
```

- [ ] **Step 4: Run the tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_mcp_https_policy.py -q`
Expected: FAIL — `test_validate_still_raises_when_required_explicitly` / `test_validate_allows_plain_http_when_not_required` error with `TypeError: validate_mcp_deployment() got an unexpected keyword argument 'require_https'`.

- [ ] **Step 5: Implement — add logger + `require_https` param + warning branch**

In `backend/app/api/mcp_server.py`, add to the stdlib import group (alongside `import contextvars` etc.):

```python
import logging
```

Add a module logger just below the imports / above `PUBLIC_TOOLS`:

```python
logger = logging.getLogger(__name__)
```

Replace `validate_mcp_deployment` (currently lines 64-72) with:

```python
def validate_mcp_deployment(
    bind_host: str, public_url: str, *, require_https: bool = True
) -> None:
    """Guard a remotely reachable MCP endpoint that would serve plain HTTP.

    Fail closed by default. When ``require_https`` is False (the product's
    intranet default, wired in ``app.main.create_app``) keep serving but log a
    prominent warning: the Agent Bearer token then crosses the network in
    cleartext, so this is only safe on a trusted private network.
    """
    parsed = urlparse(public_url)
    public_host = parsed.hostname or ""
    remotely_reachable = not _is_loopback(bind_host) or (
        public_host and not _is_loopback(public_host)
    )
    if remotely_reachable and parsed.scheme.lower() != "https":
        if require_https:
            raise RuntimeError("remote MCP deployment requires HTTPS")
        logger.warning(
            "MCP is serving remote clients over plain HTTP (bind_host=%s "
            "public_url=%s): the Agent Bearer token crosses the network in "
            "cleartext. Only do this on a trusted private network; set "
            "MCP_REQUIRE_HTTPS=1 to enforce HTTPS.",
            bind_host,
            public_url,
        )
```

- [ ] **Step 6: Run the policy tests + manifest test to verify green**

Run: `cd backend && python -m pytest tests/test_mcp_https_policy.py tests/test_repository_surface_manifest.py -q`
Expected: PASS (all).

- [ ] **Step 7: Commit**

```bash
git add backend/app/api/mcp_server.py backend/tests/test_mcp_https_policy.py backend/tests/test_repository_surface_manifest.py
git commit -m "$(printf 'feat(mcp): validate_mcp_deployment gains opt-in require_https\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 2: request guard + DNS-rebinding toggle (`AgentBearerMiddleware`, `create_memory_mcp`)

**Files:**
- Modify: `backend/app/api/mcp_server.py` (`create_memory_mcp`, `TransportSecuritySettings`, `AgentBearerMiddleware`, instantiation)
- Modify: `backend/tests/test_mcp_https_policy.py`
- Modify: `backend/tests/test_repository_surface_manifest.py` (re-pin `TASK7_MEMORY_ALLOWED_CONSUMERS` exact lines after this task shifts them)

> **Line-number note:** Task 1 already inserted ~21 lines into `mcp_server.py` (a `logging` import + module logger). The literal line numbers written in Step 3 below ("currently lines 479-481" etc.) are the *pre-Task-1* coordinates — locate each construct by name, not by the stated line number.

**Interfaces:**
- Consumes: `logger`, `validate_mcp_deployment` (Task 1).
- Produces: `create_memory_mcp(repository_provider, *, allowed_origins=(), public_url="http://127.0.0.1:8000/mcp", require_https: bool = True) -> tuple[FastMCP, Any]`. Builds `TransportSecuritySettings(enable_dns_rebinding_protection=require_https, ...)` and `AgentBearerMiddleware(..., require_https=require_https)`.
- Produces: `AgentBearerMiddleware(app, repository_provider, *, require_https: bool = True)`; `__call__` skips the `scheme != https → 403` check when `require_https` is False.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_mcp_https_policy.py`:

```python
class _StubRepo:
    def resolve_agent_token(self, raw):  # no valid token → 401 path
        return None


async def _drive_middleware(require_https, scheme, client_host):
    """Return the HTTP status the middleware emits for a bare POST."""
    sent = {}

    async def inner(scope, receive, send):  # pragma: no cover - not reached on 403
        await send({"type": "http.response.start", "status": 599, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = mcp_server.AgentBearerMiddleware(
        inner, lambda: _StubRepo(), require_https=require_https
    )
    scope = {
        "type": "http",
        "scheme": scheme,
        "client": (client_host, 5555),
        "headers": [],
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = msg["status"]

    await mw(scope, receive, send)
    return sent["status"]


@pytest.mark.anyio
async def test_request_guard_blocks_remote_http_when_required():
    status = await _drive_middleware(True, "http", "198.51.100.9")
    assert status == 403


@pytest.mark.anyio
async def test_request_guard_allows_remote_http_when_not_required():
    # scheme check skipped → reaches token resolution → 401 (bad token), not 403
    status = await _drive_middleware(False, "http", "198.51.100.9")
    assert status == 401


def test_create_memory_mcp_toggles_dns_rebinding(monkeypatch):
    captured = []
    real = mcp_server.TransportSecuritySettings

    def spy(**kwargs):
        captured.append(kwargs.get("enable_dns_rebinding_protection"))
        return real(**kwargs)

    monkeypatch.setattr(mcp_server, "TransportSecuritySettings", spy)
    mcp_server.create_memory_mcp(lambda: _StubRepo(), require_https=False)
    mcp_server.create_memory_mcp(lambda: _StubRepo(), require_https=True)
    assert captured == [False, True]
```

Note: `test_mcp_https_policy.py` must declare the anyio backend. Add near the top (after imports):

```python
@pytest.fixture
def anyio_backend():
    return "asyncio"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && python -m pytest tests/test_mcp_https_policy.py -q`
Expected: FAIL — `AgentBearerMiddleware(...)` / `create_memory_mcp(...)` reject the `require_https` keyword (`TypeError: unexpected keyword argument`).

- [ ] **Step 3: Implement — thread `require_https`**

In `backend/app/api/mcp_server.py`:

(a) `AgentBearerMiddleware.__init__` (currently lines 479-481) → add keyword-only flag:

```python
    def __init__(
        self, app, repository_provider: Callable[[], Any], *,
        require_https: bool = True,
    ) -> None:
        self.app = app
        self.repository_provider = repository_provider
        self.require_https = require_https
```

(b) `AgentBearerMiddleware.__call__` scheme check (currently lines 489-495) → gate on the flag:

```python
        if (
            self.require_https
            and str(scope.get("scheme", "http")).lower() != "https"
            and not _is_loopback(client_host)
        ):
            await JSONResponse(
                {"detail": "remote MCP transport requires HTTPS"}, status_code=403
            )(scope, receive, send)
            return
```

(c) `create_memory_mcp` signature (currently lines 555-558) → add the param:

```python
def create_memory_mcp(
    repository_provider: Callable[[], Any], *, allowed_origins: Sequence[str] = (),
    public_url: str = "http://127.0.0.1:8000/mcp", require_https: bool = True,
) -> tuple[FastMCP, Any]:
```

(d) `TransportSecuritySettings` construction (currently line 567) → drive rebinding from the flag:

```python
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=require_https,
        allowed_hosts=list(dict.fromkeys([
```

(e) `AgentBearerMiddleware` instantiation (currently line 956) → pass it through:

```python
    app = AgentBearerMiddleware(
        server.streamable_http_app(), repository_provider,
        require_https=require_https,
    )
```

- [ ] **Step 4: Run the policy tests to verify green**

Run: `cd backend && python -m pytest tests/test_mcp_https_policy.py -q`
Expected: PASS (all).

- [ ] **Step 5: Re-pin the surface manifest (exact lines — do NOT use `:<line>`)**

This task inserts a few lines into `mcp_server.py` above the facade call sites tracked by `TASK7_MEMORY_ALLOWED_CONSUMERS` in `backend/tests/test_repository_surface_manifest.py`, so its 8 exact-line tuples go stale.

Run: `cd backend && python -m pytest tests/test_repository_surface_manifest.py -q`

If it fails (`test_static_repository_consumer_scan_matches_manifest_exactly`), the assertion diff lists the current `backend/app/api/mcp_server.py:<N>` sites for each member. Update the 8 tuples to those new line numbers (map by member: `user_can_read_notebook` ×2, `get_notebook` ×2, `unified_kg_status`, `agent_memory_hits`, `search_notebook`, `ask`), and update the trailing "shifting … by exactly N lines" note in the comment above the set to the new delta. You can also read the new lines directly, e.g. `grep -n "user_can_read_notebook\|\.get_notebook\|unified_kg_status\|agent_memory_hits\|search_notebook(\|\.ask(" backend/app/api/mcp_server.py`.

**Do NOT** convert these tuples to the `:<line>` placeholder. That was tried and rejected in review: because `mcp_server.py` has no entries in `ACTIVE_PRODUCTION_MEMBER_SITES`, a `:<line>` entry would match unlimited occurrences and silently hide a genuinely new call site to any of those 6 facade members in this externally-authenticated adapter. Keep exact line pins so new consumers stay detectable.

Re-run until green:

Run: `cd backend && python -m pytest tests/test_repository_surface_manifest.py -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/mcp_server.py backend/tests/test_mcp_https_policy.py backend/tests/test_repository_surface_manifest.py
git commit -m "$(printf 'feat(mcp): gate request-guard + DNS-rebinding on require_https\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 3: composition root wiring (`app.main.create_app`) + pin existing fixture strict

**Files:**
- Modify: `backend/app/main.py` (lines 74-83)
- Modify: `backend/tests/test_memory_mcp.py` (`mcp_env` fixture, ~136-138)
- Modify: `backend/tests/test_mcp_https_policy.py`

**Interfaces:**
- Consumes: `validate_mcp_deployment`, `create_memory_mcp` (Tasks 1-2).
- Produces: `create_app()` reads `MCP_REQUIRE_HTTPS` (env default false) and passes `require_https=` to both seams. Default env → remote plain HTTP is allowed (no raise).

- [ ] **Step 1: Pin the existing MCP fixture to strict so pre-existing tests keep their intent**

In `backend/tests/test_memory_mcp.py`, inside `mcp_env`, update the comment + add the env pin next to the existing `MCP_PUBLIC_URL` line (currently 136-138):

```python
    # Startup declares the public deployment as HTTPS and pins the strict
    # (fail-closed) policy so the runtime transport still rejects a remote
    # client that reaches ASGI over plain HTTP. The product default is now
    # open (MCP_REQUIRE_HTTPS unset); these tests exercise the strict path.
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://memory.example.test/mcp")
    monkeypatch.setenv("MCP_REQUIRE_HTTPS", "1")
```

- [ ] **Step 2: Confirm the existing MCP suite stays green under the pin (before touching main.py it must still pass because default is still strict; this guards the pin itself)**

Run: `cd backend && python -m pytest tests/test_memory_mcp.py -q`
Expected: PASS (all).

- [ ] **Step 3: Write the failing create_app wiring tests**

Append to `backend/tests/test_mcp_https_policy.py`:

```python
from app.core.config import get_settings
from app.services import repository as _repository_module


def _min_app_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("BACKEND_HOST", "0.0.0.0")
    monkeypatch.setenv("MCP_PUBLIC_URL", "http://10.0.0.5:8000/mcp")
    get_settings.cache_clear()
    _repository_module.cache_clear()


def test_create_app_defaults_to_open(monkeypatch, tmp_path):
    _min_app_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MCP_REQUIRE_HTTPS", raising=False)
    from app.main import create_app

    app = create_app()  # must NOT raise despite 0.0.0.0 + http
    assert app is not None


def test_create_app_require_https_restores_failclosed(monkeypatch, tmp_path):
    _min_app_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MCP_REQUIRE_HTTPS", "1")
    from app.main import create_app

    with pytest.raises(RuntimeError, match="requires HTTPS"):
        create_app()
```

- [ ] **Step 4: Run to verify failure**

Run: `cd backend && python -m pytest tests/test_mcp_https_policy.py -k create_app -q`
Expected: FAIL — `test_create_app_defaults_to_open` raises `RuntimeError: remote MCP deployment requires HTTPS` (main.py still forces the strict default).

- [ ] **Step 5: Implement the wiring in `create_app`**

In `backend/app/main.py`, replace lines 74-83 with:

```python
    bind_host = os.environ.get("BACKEND_HOST", os.environ.get("HOST", "127.0.0.1"))
    mcp_public_url = os.environ.get(
        "MCP_PUBLIC_URL", f"http://{bind_host}:8000/mcp"
    )
    # Product default: DO NOT require HTTPS for MCP. This is an intranet-friendly
    # default — remote plain HTTP is allowed and Host/Origin (DNS-rebinding)
    # checks are relaxed — accompanied by a loud startup warning. Set
    # MCP_REQUIRE_HTTPS=1 on any public deployment to restore the fail-closed
    # guard (HTTPS enforced + Host/Origin validated).
    require_https = os.environ.get("MCP_REQUIRE_HTTPS", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    validate_mcp_deployment(bind_host, mcp_public_url, require_https=require_https)
    mcp_server, mcp_app = create_memory_mcp(
        mcp_memory_repository,
        allowed_origins=settings.cors_origins,
        public_url=mcp_public_url,
        require_https=require_https,
    )
```

- [ ] **Step 6: Run the wiring tests + the full new policy file to verify green**

Run: `cd backend && python -m pytest tests/test_mcp_https_policy.py -q`
Expected: PASS (all).

- [ ] **Step 7: Run the existing MCP suite to verify the fixture pin holds after the default flip**

Run: `cd backend && python -m pytest tests/test_memory_mcp.py -q`
Expected: PASS (all) — `test_runtime_transport_requires_https_only_for_remote_clients` still gets 403 because the fixture pins `MCP_REQUIRE_HTTPS=1`.

- [ ] **Step 8: Commit**

```bash
git add backend/app/main.py backend/tests/test_memory_mcp.py backend/tests/test_mcp_https_policy.py
git commit -m "$(printf 'feat(mcp): default MCP HTTPS off, opt-in via MCP_REQUIRE_HTTPS\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 4: documentation

**Files:**
- Modify: `README.md` (~309-310), `README_zh.md` (~275-276)
- Modify: `architecture.md` (line 80)
- Modify: `.env.example`
- Modify: `packaging/DEPLOY.md` (env table ~51)
- Check: `backend/tests/test_architecture_documentation.py`

**Interfaces:** none (docs only).

- [ ] **Step 1: Check whether any documentation test asserts the strings being edited**

Run: `cd backend && python -m pytest tests/test_architecture_documentation.py -q`
Then: `grep -n "must expose an HTTPS\|必须使用 HTTPS\|必须是 HTTPS\|MCP_PUBLIC_URL\|MCP_REQUIRE_HTTPS" backend/tests/test_architecture_documentation.py`
Expected: baseline PASS; note any asserted substrings so the new wording keeps them satisfied.

- [ ] **Step 2: Update `architecture.md` line 80**

Replace:

```
- Agent MCP：`MCP_PUBLIC_URL`；本机可用 loopback HTTP，非 loopback 的 public URL 必须是 HTTPS。
```

with:

```
- Agent MCP：`MCP_PUBLIC_URL`；默认允许远程明文 HTTP 并放宽 Host/Origin 校验（仅可信内网），启动会打印明文告警；公网部署设 `MCP_REQUIRE_HTTPS=1` 恢复强制 HTTPS + DNS-rebinding 保护。
```

- [ ] **Step 3: Update `README.md`**

Replace the sentence at ~309-310:

```
Local use may use loopback HTTP; any remote deployment must expose an HTTPS URL and set
`MCP_PUBLIC_URL` to that public `/mcp` URL.
```

with:

```
By default MCP allows remote plain HTTP and relaxes Host/Origin (DNS-rebinding)
checks — intended for a trusted private network — and prints a startup warning
because the Agent token then travels in cleartext. On any public deployment set
`MCP_REQUIRE_HTTPS=1` to enforce HTTPS (and restore Host/Origin validation), and
set `MCP_PUBLIC_URL` to the public HTTPS `/mcp` URL.
```

- [ ] **Step 4: Update `README_zh.md`**

Replace the sentence at ~275-276:

```
loopback HTTP；远程部署必须使用 HTTPS，并把 `MCP_PUBLIC_URL` 设为公开的 `/mcp` URL。
```

with:

```
loopback HTTP；默认允许远程明文 HTTP 并放宽 Host/Origin（DNS-rebinding）校验，供可信内网使用，
启动会打印明文告警（Agent token 明文过网）。公网部署请设 `MCP_REQUIRE_HTTPS=1` 强制 HTTPS
（并恢复 Host/Origin 校验），并把 `MCP_PUBLIC_URL` 设为公开的 HTTPS `/mcp` URL。
```

Note: verify the preceding line's join reads naturally after the edit (the original wraps across two lines); adjust whitespace only, no meaning change.

- [ ] **Step 5: Add `MCP_REQUIRE_HTTPS` to `.env.example`**

Add near the backend/MCP settings (search for `MCP_PUBLIC_URL` first; if absent, place after `BACKEND_HOST`):

```bash
# MCP HTTPS 强制(默认关):不设=允许远程明文 HTTP + 放宽 Host/Origin 校验(仅可信内网,
# 启动打印明文告警)。公网部署设为 1 恢复 fail-closed(强制 HTTPS + DNS-rebinding 保护)。
# MCP_REQUIRE_HTTPS=1
```

- [ ] **Step 6: Add a row to `packaging/DEPLOY.md` env table**

After the `BACKEND_HOST` row (~51) add:

```
| `MCP_REQUIRE_HTTPS` | `0` | MCP 是否强制 HTTPS。默认关(允许内网明文+放宽 Host 校验);公网设 `1` |
```

- [ ] **Step 7: Re-run the documentation test**

Run: `cd backend && python -m pytest tests/test_architecture_documentation.py -q`
Expected: PASS. If it asserts an edited substring, reconcile the wording (keep the required phrase) and re-run.

- [ ] **Step 8: Commit**

```bash
git add README.md README_zh.md architecture.md .env.example packaging/DEPLOY.md
git commit -m "$(printf 'docs: document MCP_REQUIRE_HTTPS opt-in (default off)\n\nCo-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>')"
```

---

### Task 5: full verification + PR

**Files:** none (verification + integration).

- [ ] **Step 1: Run the focused suites**

Run: `cd backend && python -m pytest tests/test_mcp_https_policy.py tests/test_memory_mcp.py tests/test_repository_surface_manifest.py tests/test_architecture_documentation.py -q`
Expected: PASS (all).

- [ ] **Step 2: Manual runtime check (default open)**

```bash
cd backend && MCP_REQUIRE_HTTPS= BACKEND_HOST=0.0.0.0 \
  MCP_PUBLIC_URL=http://127.0.0.1:8000/mcp \
  python -c "from app.main import create_app; create_app(); print('create_app OK (open default)')"
```
Expected: prints OK, and logs the plain-HTTP WARNING (bind_host=0.0.0.0). No RuntimeError.

- [ ] **Step 3: Manual runtime check (opt-in strict)**

```bash
cd backend && MCP_REQUIRE_HTTPS=1 BACKEND_HOST=0.0.0.0 \
  MCP_PUBLIC_URL=http://127.0.0.1:8000/mcp \
  python -c "from app.main import create_app; create_app()" ; echo "exit=$?"
```
Expected: raises `RuntimeError: remote MCP deployment requires HTTPS` (non-zero exit).

- [ ] **Step 4: Rebase to master (linear) and open PR**

```bash
git fetch origin
git rebase origin/master
git push -u origin claude/mcp-server-https-deploy-4c2c35
gh pr create --base master --title "feat(mcp): MCP_REQUIRE_HTTPS opt-in (default off) for intranet deploy" --body "$(cat <<'BODY'
## What
Make MCP HTTPS enforcement opt-in. Default (no env) allows remote plain HTTP and
relaxes Host/Origin (DNS-rebinding) checks — intended for trusted intranet — with
a loud startup warning. Set `MCP_REQUIRE_HTTPS=1` to restore the fail-closed guard.

## Why
Intranet deployments hit `RuntimeError: remote MCP deployment requires HTTPS` at
startup with no way to proceed. Owner chose an intranet-friendly default.

## Design
Function-signature defaults stay secure (`require_https=True`); the product-wide
default-open is a single decision in `app.main.create_app` reading `MCP_REQUIRE_HTTPS`.
Spec: docs/superpowers/specs/2026-07-15-mcp-insecure-http-optout-design.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
BODY
)"
```

---

## Self-Review

**Spec coverage:**
- Switch `MCP_REQUIRE_HTTPS` + truthy parsing → Task 3 Step 5. ✓
- Signature defaults secure, product-open in main.py → Global Constraints + Task 3. ✓
- Startup-guard warning branch → Task 1. ✓
- Request-guard skip → Task 2. ✓
- DNS-rebinding toggle → Task 2. ✓
- Surface manifest allowlist (no regen) → Task 1 Step 1. ✓
- Existing fixture pinned strict → Task 3 Step 1. ✓
- Docs (README/README_zh/architecture/.env.example/DEPLOY) + doc-test check → Task 4. ✓
- Verification + PR → Task 5. ✓

**Placeholder scan:** No TBD/TODO; every code/test step shows full content. ✓

**Type consistency:** `require_https: bool` keyword-only everywhere; `validate_mcp_deployment(bind_host, public_url, *, require_https=True)`, `create_memory_mcp(..., require_https=True)`, `AgentBearerMiddleware(app, repository_provider, *, require_https=True)` consistent across Tasks 1-3 and their call sites. ✓
