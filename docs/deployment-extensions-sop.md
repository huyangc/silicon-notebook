# Deployment extensions SOP: develop, integrate and operate an out-of-tree plugin

[中文](./deployment-extensions-sop_zh.md) · [Back to README](../README.md)

This runbook takes one private feature from an empty directory to a running deployment: a plugin package that lives in **its own repository**, is installed beside this checkout, and is loaded in-process without a single patch to the public tree. It covers the backend bundle, the build-time UI package, local integration, packaging, install, startup verification, upgrade/rollback, and the complete rejection-code table.

The authoritative contracts stay where they are — [Deployment and configuration → Deployment extensions](./deployment-and-configuration.md#deployment-extensions-extensions_config), [Product and API reference → Deployment extensions](./product-and-api.md#deployment-extensions), and the frontend registry rules in [Development and repository contracts](./development.md). This document does not restate them; it tells you what to do, in what order, and what each failure looks like.

## 1. Scope and trust model

```text
public checkout (unmodified)                private plugin repository
  backend/app/extension_sdk   ◀── imports ── silicon_notebook_corp_search/
  backend/app/extensions      ── loads ────▶   bundle.py   (ExtensionManifest + register)
    via EXTENSIONS_CONFIG                      routes.py   (APIRouter factory)
  frontend/features/extension-sdk             ui/
    ◀── copied at build time ──────────────    ui-plugin.json
    via SILICON_NOTEBOOK_UI_PLUGINS            workspace-plugin.tsx
```

A manifest's `trust` field has three values. `builtin` ships with this build. `deployment` is this document's subject: **trusted, in-process, out-of-repo, named by the deployment**. `isolated` is a reserved value for a future process-isolated tier and the registry refuses it today.

A `deployment` plugin **can**: register any of the four contribution kinds (Provider / ProviderChain / Contributor / Observer) at any core extension point, mount its own HTTP routes under `/api/extensions/{plugin_id}`, declare and supply its own capabilities, contribute workspace UI, take validated settings from the deployment's TOML, and import third-party Python packages it installs itself.

A `deployment` plugin **cannot**: reach the repository, global `Settings`, a model client, the FastMCP host, or a raw bearer token; extend the application lifespan; add MCP tools; create its own database tables; serve an anonymous route; or ship browser code that is fetched at runtime. Authorization is never the plugin's: every core port it touches checks the *request's own* user itself.

Trust means "we accept the code" — it does not mean "the code decides who may read a notebook".

## 2. Prerequisites

| Item | Requirement |
| --- | --- |
| Public checkout | The exact commit the deployment will run. Read its `EXTENSION_API_VERSION` from `backend/app/extension_sdk/contracts.py` — currently `"1"`. |
| Python | ≥ 3.13, and the plugin installs into **the same `PYTHON_BIN` environment as the backend**, not a separate interpreter or venv. |
| Node.js | ≥ 20 with npm, same version the deployment builds the frontend with. |
| Frontend manifest API version | `ui-plugin.json`'s `api_version` must equal `"1"` as well (`CONTRACT_API_VERSION` in `frontend/scripts/sync-ui-plugins.mjs`). |

A workable plugin repository layout:

```text
silicon-notebook-corp-search/
├─ src/silicon_notebook_corp_search/
│  ├─ __init__.py
│  ├─ bundle.py            # the module-level object EXTENSIONS_CONFIG names
│  └─ routes.py            # the APIRouter factory
├─ ui/corp-search/         # one flat directory = one UI package
│  ├─ ui-plugin.json
│  └─ workspace-plugin.tsx
├─ tests/
├─ pyproject.toml
├─ extensions.local.toml   # local integration config, never shipped
└─ CHANGELOG.md            # every entry names the api_version it supports
```

Pin the supported `api_version` in `CHANGELOG.md` from the first release. It is the only thing that tells an operator whether a plugin build and a core build fit together, and the startup check that enforces it (`plugin_api_version_unsupported`) names the plugin but not the expected value.

## 3. Step one — the backend package

### 3.1 The minimal bundle

Discovery imports `"<module path>:<attribute>"` and structurally validates the object. Nothing is subclassed and nothing is registered globally: a module-level object that happens to have the right shape is fully conformant. `app.extension_sdk.deployment` documents that shape as Protocols for readers only.

```python
# silicon_notebook_corp_search/bundle.py
from dataclasses import dataclass

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
)
from app.extension_sdk.http import PLUGIN_HTTP_ROUTER_POINT

from .routes import build_router

_ROUTER = ContributionDeclaration(
    id="corp.search.router",
    point=PLUGIN_HTTP_ROUTER_POINT,
    kind=ContributionKind.CONTRIBUTOR,
)


@dataclass
class CorpSearchBundle:
    manifest: ExtensionManifest

    def register(self, registrar) -> None:
        registrar.add_contributor(
            ExtensionContribution(declaration=_ROUTER, implementation=build_router)
        )


BUNDLE = CorpSearchBundle(
    ExtensionManifest(
        id="corp.search",
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Corp literature search",
        trust="deployment",
        contributions=(_ROUTER,),
    )
)
```

*Core does for you:* imports the module, checks the manifest is an `ExtensionManifest`, that `manifest.id` equals the config key, that `trust == "deployment"`, that `api_version` matches this build, and — after `register()` returns — that the set of contribution ids you actually registered equals the set your manifest declared. Any mismatch stops the process.

*You cannot:* register a contribution your manifest does not declare, or declare one you do not register. Both land as "registrations do not match its manifest".

### 3.2 Settings (optional)

`settings_model` and `configure` are a **pair**: declare both, or neither. A plugin that takes no configuration declares neither, and any `[settings]` table for it in the deployment TOML is then a startup failure.

```python
from pydantic import BaseModel


class CorpSearchSettings(BaseModel):
    base_url: str
    api_key_env: str = "CORP_SEARCH_API_KEY"   # the variable *name*, not the key
    timeout_seconds: int = 20


@dataclass
class CorpSearchBundle:
    manifest: ExtensionManifest
    settings_model: type[BaseModel] = CorpSearchSettings
    settings: CorpSearchSettings | None = None

    def configure(self, settings: CorpSearchSettings) -> None:
        self.settings = settings          # store and return — that is the whole method

    def register(self, registrar) -> None:
        ...
```

*Core does for you:* computes the accepted key set from `model_fields` itself (plus plain string aliases) rather than trusting you to set `extra="forbid"`, validates the TOML table into one instance, and calls `configure` with it **before** `register`. A rejection carries the offending key *names* and an exception *class* name — never a value, because pydantic's `ValidationError` echoes the input it rejected.

*You cannot:* start a thread or background task, open a network or database connection, or perform blocking I/O inside `configure`. It runs inside startup composition, before the registry is frozen and before the service is ready. Do that work lazily, on the first request that needs it.

Reference a secret by env-var name (mirroring `model-services.toml`'s `api_key_env`) instead of embedding it. Nothing in core ever prints a settings value, but a raw key in a config file is one `cat` away from a chat log.

*An aliased field is accepted by its alias only.* If you write `api_key: str = Field("", alias="token")`, the accepted key is `token` — not `api_key`. That mirrors pydantic itself: by default it populates an aliased field from the alias and ignores the field name, so a TOML key of `api_key` would have been silently dropped and your plugin would have run on the field's default. Core rejects it as `plugin_settings_unknown_key` instead, which is the whole point of computing the key set. To accept both spellings, opt in on the model — `model_config = ConfigDict(populate_by_name=True)` (pydantic 2.11+ also spells it `validate_by_name`; setting either turns both on) — and core follows.

*Registered limitation:* pydantic's `AliasChoices`/`AliasPath` alias forms are **not** collected into the accepted key set. A settings key that only matches such an alias is reported as `plugin_settings_unknown_key` rather than accepted — fail-closed, and deliberate.

### 3.3 Capabilities (optional)

If a manifest declares `provides`, the bundle must expose a `capability_decisions` mapping whose keys equal `provides` **exactly**. That is what lets your own `requires` / `ui_contributions` freeze at all.

```python
from app.extension_sdk import Availability, AvailabilityStatus
from app.extension_sdk.ui import UiContributionDeclaration


def _corp_search_available(_context: object | None) -> Availability:
    if not BUNDLE.settings:
        return Availability(AvailabilityStatus.DISABLED, "not_configured")
    return Availability.available()


BUNDLE = CorpSearchBundle(
    ExtensionManifest(
        ...,
        provides=("corp.search.available",),
        ui_contributions=(
            UiContributionDeclaration(
                id="corp.search.panel",
                slot="workspace.side_panel",
                capability="corp.search.available",
            ),
        ),
    )
)
BUNDLE.capability_decisions = {"corp.search.available": _corp_search_available}
```

*Core does for you:* validates every name's shape, rejects a probe for an undeclared name and a declared name without a probe, and refuses a collision with a core capability or with another plugin's — silently letting one probe shadow another would make availability depend on registration order.

*You cannot:* use `:` in a capability name. Core's own capabilities read `point:name` and that spelling is reserved, so a plugin can never mint a name shaped like a core one. Legal names are lowercase, dot/underscore/hyphen separated: `corp.search.available`. Availability is evaluated live on every request, so keep the probe **I/O-free** — it must not call your upstream to decide whether it is available.

### 3.4 HTTP routes (optional, at most one per plugin)

The factory receives one `PluginRouteContext` and returns an `APIRouter`. Core mounts it under `/api/extensions/{plugin_id}` behind a router-level session dependency.

```python
# silicon_notebook_corp_search/routes.py
from fastapi import APIRouter, Depends

from app.extension_sdk.http import PluginRouteContext


def build_router(context: PluginRouteContext) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health(actor=Depends(context.current_actor)):
        return {"plugin_id": context.plugin_id, "actor_id": actor.id}

    @router.get(
        "/notebooks/{notebook_id}/candidates",
        dependencies=[Depends(context.require_notebook_read)],
    )
    def candidates(notebook_id: str, q: str = ""):
        return {"items": _upstream_search(context.settings, q)}

    @router.post(
        "/notebooks/{notebook_id}/import",
        dependencies=[Depends(context.require_notebook_capability("sources:write"))],
    )
    def import_selected(notebook_id: str, payload: dict):
        urls = [u for u in (payload.get("urls") or []) if isinstance(u, str)]
        if not urls:
            raise context.user_error(400, "请先选择要导入的文献")
        result = context.url_sources.import_urls(notebook_id, urls)
        context.emit_event({"event": "corp_urls_imported", "count": len(result.created)})
        return {
            "created": [
                {"source_id": row.source_id, "title": row.title, "url": row.url}
                for row in result.created
            ],
            "rejected": [
                {"url": row.url, "reason": row.reason} for row in result.rejected
            ],
        }

    # An `async def` handler runs on the event loop thread, so it must await
    # the offloaded variant instead. Same authorization, same result.
    @router.post(
        "/notebooks/{notebook_id}/import-async",
        dependencies=[Depends(context.require_notebook_capability("sources:write"))],
    )
    async def import_selected_async(notebook_id: str, payload: dict):
        urls = [u for u in (payload.get("urls") or []) if isinstance(u, str)]
        result = await context.url_sources.import_urls_async(notebook_id, urls)
        return {"created": [row.source_id for row in result.created]}

    return router
```

The eight seams, and nothing else: `plugin_id`, `settings`, `require_notebook_capability`, `require_notebook_read`, `current_actor`, `user_error`, `url_sources`, `emit_event`.

*Core does for you:*

- **Authorization, per port.** `url_sources.import_urls` checks `sources:write` for the request's own user — resolved from core's request context, never from anything you pass — and refuses with the same 404 core's own endpoints use (existence is not disclosed). Past that check it is literally core's own URL-import function: the same capacity accounting, admin exemption, unconfigured-parser mapping and background parse scheduler.
- **Refusing to block the event loop.** `url_sources.import_urls` blocks — database writes plus one serial remote probe per URL. A `def` handler is already in FastAPI's threadpool, so it calls it directly; an `async def` handler is on the event loop thread, so it must `await url_sources.import_urls_async(...)`, which does the same work in the threadpool. Getting it backwards is refused at runtime, not merely discouraged: `import_urls` raises `RuntimeError` when it finds a running event loop on its own thread, before doing any work, and the message names the method to await. Nothing is half-imported, and the failure is a plain `500` with a traceback rather than user-facing copy — it is a bug in the plugin, not something the end user did.
- **A structural gate on `{notebook_id}` routes.** A route whose path contains that literal substring must run one of core's own gates (any capability guard, or the read gate). This is defence in depth, not the boundary: name the parameter `{nb}` or take the id from a body and the check does not see it — and the port still refuses. Removing the port's check would open a hole; removing this one would not.
- **401 translation.** A 401 your handler either *raises* — as either `fastapi.HTTPException` or `starlette.exceptions.HTTPException`; the former is a subclass of the latter, and both are caught identically — or *returns* (proxying an upstream service's own 401 response onto your own `Response`/`JSONResponse` is a normal return, not a raise, and is caught the same way) becomes `424` with core's own copy and a logged `plugin_upstream_unauthorized` event. In core, 401 has exactly one meaning to the browser — clear the token and reload — so an expired upstream credential inside one plugin route would otherwise sign the user out of the whole product. **The cover is the handler and your own `Depends(...)` callables alike**, at any nesting depth: checking an upstream inside a dependency is at least as ordinary as checking it inside the handler, and FastAPI solves dependencies before it calls the endpoint, so a dependency-raised 401 would otherwise escape untranslated. Dependencies get the raised half only — a dependency's return value is injected as a parameter and never becomes the response. **Core's own dependencies are excluded** — by object identity *and* by defining module — so a genuine 401 from core's session gate still surfaces as 401 and still signs the user out, which is what it is for. Generator (`yield`) dependencies and security schemes are left alone.
- **Event sanitization.** `emit_event` accepts exactly four fields — `event`, `outcome`, `count`, `elapsed_ms` — and drops the *whole* record on anything else. Core adds `kind` and `plugin_id`. It can never raise back into your handler.

*You cannot:* mount startup/shutdown hooks on the router, add a non-`APIRoute` route (a sub-application, a raw websocket, a bare Starlette route), declare a second router, or return something that is not an `APIRouter`. Each is a startup failure with its own code (§9).

### 3.5 Other contribution kinds

The six remaining production extension points are Protocols in the SDK; implement the Protocol, declare the matching `ContributionKind`, and register through the typed `add_*` helper.

| Point constant | Kind | Protocol | Module |
| --- | --- | --- | --- |
| `RETRIEVAL_CONTRIBUTOR_POINT` | `CONTRIBUTOR` | `RetrievalContributor` | `app/extension_sdk/retrieval.py` |
| `PARSER_PROVIDER_CHAIN_POINT` | `PROVIDER_CHAIN` | `ParserChainLink` | `app/extension_sdk/parser.py` |
| `ASK_COMPLETED_OBSERVER_POINT` | `OBSERVER` | `AskCompletedObserver` | `app/extension_sdk/ask.py` |
| `REPORT_COMPLETED_OBSERVER_POINT` | `OBSERVER` | `ReportCompletedObserver` | `app/extension_sdk/report.py` |
| `REPORT_EXPORTER_POINT` | `PROVIDER` | `ReportExporterProvider` | `app/extension_sdk/report_export.py` |
| `ASK_GAP_CONSULT_POINT` (`ask.gap_consult`) | `CONTRIBUTOR` | `GapConsultContributor` | `app/extension_sdk/gap_consult.py` |

Each point hands a narrow, point-specific context — never a universal service locator — and each declares the capabilities a contribution must `require` to receive its access port. Read the Protocol and its module docstring before writing against it; they carry the fail-open and cancellation rules for that point.

### 3.6 Backend red lines

- Import only `app.extension_sdk` and `app.domain`. Never a concrete repository, facade, runtime, service, or `app.api` module.
- A settings value must never reach a log, an event, an exception message, or a response body.
- `configure()` starts no thread and opens no connection.
- Capability names are dot/underscore/hyphen separated; `:` is core's.
- A plugin route must not raise 401 for anything other than a genuine session invalidation — translate an upstream 401 to `502`/`424` yourself if you want your own wording.
- Never `raise ExtensionRegistryError` from `register()`. It is not on the SDK surface, but it is importable, and core deliberately leaves it *unsanitized* — a message you write there reaches the operator's log verbatim. Every other exception from `register()` is converted to `plugin_registration_failed` with only the class name.
- A `GapConsultContributor` runs its availability probe and its `consult` call together on one private worker thread under a hard deadline (see [Gap consultation](./product-and-api.md#gap-consultation-askgap_consult)): do not rely on `contextvars`, thread-local state, or any core ContextVar surviving into that thread — none does, by design. The host waits up to `ASK_GAP_CONSULT_TIMEOUT_SECONDS` and accepts whatever you return within that window; exceed it and the contribution is abandoned outright — its eventual return value is read by no one and discarded, never applied late. Return only `http`/`https` URLs, and only ones that point directly at a PDF — the import endpoint probes the exact URL you give it and does not go looking for one on a landing or abstract page.

## 4. Step two — the frontend package

A UI package is a **flat directory** that the build copies into `frontend/features/ext-<name>/`. It ships no dependencies of its own and no CSS.

### 4.1 `ui-plugin.json`

```json
{
  "api_version": "1",
  "contributions": [
    {
      "id": "corp.search.panel",
      "plugin_id": "corp.search",
      "version": "0.1.0",
      "capability": "corp.search.available",
      "slot": "workspace.side_panel",
      "permission": "source:write",
      "mode": "all",
      "component": "CorpSearchEntry"
    }
  ]
}
```

`id`, `plugin_id` and `capability` must match the backend manifest exactly — the browser renders a contribution only when the local tuple `(plugin_id, version, contribution_id)` matches a live server row. `slot` is `workspace.side_panel` or `source.detail_section`; `permission` is one of `notebook:read` / `notebook:write` / `notebook:configure` / `source:read` / `source:write` / `system:admin`; `mode` is `all` or `advanced`; `component` is the exported React component's name.

### 4.2 `workspace-plugin.tsx`

```tsx
import { useState } from "react";
import { Search } from "lucide-react";

import type { WorkspaceExtensionProps } from "../extension-sdk/contracts.ts";
import { ExtensionModal } from "../extension-sdk/ui.tsx";

type Candidate = { id: string; title: string; url: string };

export function CorpSearchEntry({ context, actions }: WorkspaceExtensionProps) {
  // No `open` state: whether the dialog is showing is `context.dialog`'s answer (see below).
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  async function importSelected(urls: readonly string[]) {
    setBusy(true);
    setNotice("");
    try {
      const result = await actions.api.requestJson<{ created: Candidate[] }>(
        `/notebooks/${context.notebook.id}/import`,
        { method: "POST", body: JSON.stringify({ urls }) },
      );
      setNotice(`已导入 ${result.created.length} 篇资料`);
      await actions.refreshSources().catch((error: unknown) => {
        setNotice(actions.api.userMessage(error, "资料已导入，但列表未能刷新，请手动刷新页面"));
      });
    } catch (error) {
      setNotice(actions.api.userMessage(error, "导入失败，请稍后再试"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        className="button secondary workspace-extension-entry"
        onClick={() => actions.openDialog()}
      >
        <Search size={16} aria-hidden="true" />
        <span>文献检索</span>
      </button>
      <ExtensionModal
        context={context}
        actions={actions}
        storageKey="search"
        title="文献检索"
        description="搜索并导入到当前笔记本"
      >
        {/* your panel; `busy` disables the submit control, `notice` is shown inline */}
      </ExtensionModal>
    </>
  );
}
```

*Core does for you:* injects `actions.api` bound to **this contribution's** `pluginId`, so every request is confined under `/api/extensions/<plugin id>/`, `authorization`/`cookie` headers you set are stripped, and `tag`/`auth`/`unauthorized` are fixed. `ExtensionModal` gives you the system dialog shell, the drag handle, and a per-plugin window-position key.

*The dialog is core-owned.* You do not hold an `open` boolean. `actions.openDialog()` **asks** for the one generic `extension` slot in core's root-dialog coordinator; `context.dialog` is the answer, and `ExtensionModal` renders nothing until `context.dialog.open` is true. Core closes it for you when another primary dialog opens, when the user switches notebooks, and on sign-out; `actions.closeDialog()` is the only way *you* close it. Two consequences worth knowing: one plugin dialog is visible at a time (the slot is claimed per **contribution**, so two contributions of the same plugin do not both open), and a dialog covered by a higher layer becomes `inert`/`aria-hidden` instead of quietly staying focusable behind it. **A different contribution calling `openDialog()` closes the current holder's dialog first, then re-registers the slot to itself** — the focus-return target moves to the new holder's own trigger, not the previous holder's (codex #578 R7 P2). A local `useState` for the dialog would only diverge from all of that — it cannot close a dialog the coordinator already took away.

*You cannot:*

| Rule | Why |
| --- | --- |
| Import anything outside `../extension-sdk/contracts.ts`, `../extension-sdk/ui.tsx`, bare `react`, bare `lucide-react`, and same-package siblings | A wider surface is unreviewable from outside this repository. |
| Import `../extension-sdk/api.ts` | `createWorkspaceExtensionApi(pluginId)` in plugin hands means plugin A can build plugin B's port and the path confinement stops meaning anything. |
| `setTimeout` / `setInterval` (bare, `window.` or `globalThis.`), dynamic `import(`, `new WebSocket` / `EventSource` / `XMLHttpRequest`, `navigator.sendBeacon` / bare `sendBeacon` | Background channels a build-time review cannot see. The AST guard sweeps **every** `.ts`/`.tsx` in the package, not just the entry. |
| `fetch(` | The repository-wide `api-boundary` guard already sweeps every module; `actions.api` is the only sanctioned I/O surface. |
| Read `error.message` / `.error` / `.error_message`, or `throw new Error("中文…")` | `errors-guard` is an exact-count sweep whose `APPROVED_*` allowlists live in the public repository — a package outside it cannot register. Use `api.userMessage(error, fallback)`. |
| Ship a `.css` file, a `package.json`, a `node_modules` directory, or any subdirectory | CSS cannot join the base stylesheet; a dependency tree would be swept whole into `next build`'s type-check (`exclude: ["node_modules"]` only covers `frontend/node_modules`) and would introduce a second React instance. New dependencies go through a base-repository PR. |
| Write a colour literal | Reuse existing classes and `:root` tokens. `extension-ui-layout-guard` pins this on both sides. |
| Put `actions` or `context` in a `useEffect`/`useMemo` dependency array | Both are fresh objects every render (the owner gate is re-frozen each pass). `actions.api` is memoized per `pluginId` and *is* safe there. |
| Let `refreshSources()` reject unhandled | It resolves silently once the owner gate has closed (that is not an error, just a refresh that no longer matters) and rejects on a genuine load failure. Call it at most once, after your own action completes, and `catch` it. |
| Keep your own `open` state for the dialog | `context.dialog` is the single source of truth. A local boolean cannot close a dialog the coordinator already took away (a conflicting primary, a notebook switch), so the two diverge and the plugin renders a dialog core believes is closed. |
| Open a dialog from `source.detail_section` | Two reasons. `ExtensionModal`'s `position: fixed` resolves against the host's own floating card there, so the dialog follows the source-detail window; and that host holds the `source-detail` primary lease, which the `extension` lease conflicts with — opening one closes the source-detail window, unmounting your own contribution with it. **The host refuses this structurally, not by your discipline**: in that slot `actions.openDialog()`/`actions.closeDialog()` are no-ops that never reach core (a dev-mode `console.warn` fires), `context.dialog.open` is always `false`, and `ExtensionModal` throws a `TypeError` outright in that slot — it never even attempts to render. |

The package files: exactly one `ui-plugin.json`, exactly one `workspace-plugin.ts` **or** `.tsx`, any number of flat sibling `.ts`/`.tsx` modules. `.d.ts` and `*.test.*` files are rejected. Dotfiles at the package root (`.DS_Store`, `.gitignore`, editor leftovers) are skipped with a note on stderr; **subdirectories are always rejected, `.git` included** — keep the UI package in its own directory, not at the repository root.

## 5. Step three — local integration

Use the public checkout as the runtime. Nothing in it changes.

### 5.1 Backend

```bash
# from the public checkout; PYTHON_BIN is the interpreter the backend runs on
"$PYTHON_BIN" -m pip install -e /path/to/silicon-notebook-corp-search
```

Or, without installing, put the plugin's `src/` on `PYTHONPATH` for the process you start.

Write a local config the plugin repository keeps to itself:

```toml
# extensions.local.toml
[extensions."corp.search"]
bundle = "silicon_notebook_corp_search.bundle:BUNDLE"
enabled = true

[extensions."corp.search".settings]
base_url = "https://search.corp.internal/api"
api_key_env = "CORP_SEARCH_API_KEY"
timeout_seconds = 20
```

Point the backend at it with `EXTENSIONS_CONFIG` (a relative path is anchored to the repo root):

```bash
EXTENSIONS_CONFIG=/path/to/extensions.local.toml npm run dev
```

### 5.2 Frontend

```bash
cd frontend
SILICON_NOTEBOOK_UI_PLUGINS=/path/to/silicon-notebook-corp-search/ui/corp-search \
  npm run sync:ui-plugins
```

The variable is a `:`-separated list of package directories; relative paths resolve against the current directory. You rarely need to run the sync by hand — `postinstall` and the five `pre*` hooks (`predev`, `prebuild`, `prestart`, `pretest`, `prelint`) run it for you, so exporting the variable in the shell that runs `npm run dev` / `build` / `test` / `lint` is enough.

It produces three artifacts, all gitignored:

| Artifact | What it is |
| --- | --- |
| `frontend/features/ext-<name>/` | The validated package copy, plus a `.ui-plugin-origin` marker that authorizes a later sync to remove it. |
| `frontend/features/extension-sdk/registry.local.ts` | The local contribution list (an empty array when nothing is configured). |
| `frontend/.local/ui-extension-contract.json` | The deployment-time reconciliation input: the built-in fixture's rows concatenated with each package's manifest rows. |

The sync is two-phase on purpose: everything that can fail (reading the built-in contract, validating every input package, surveying existing `ext-*` directories, rendering both generated texts) happens **before** the file tree is touched.

### 5.3 Checks to run inside the plugin repository

```bash
# 1. the plugin's own tests
"$PYTHON_BIN" -m pytest tests -q

# 2. the same Chinese-UI-copy guarantee core enforces on itself, over your tree
python3 /path/to/public-checkout/scripts/check_ui_vocabulary.py \
  --extra-root /path/to/silicon-notebook-corp-search/src

# 3. after syncing, the base repository's five extension guards over the copied
#    package (module graph, plugin package boundary, UI boundary, layout, parity)
cd /path/to/public-checkout/frontend
node --test tests/guards/extension-*.test.mjs

# 4. the type-check covering features/ext-*/ — `next build`'s pass silently
#    skips diagnostics in `*.test.*`/`*.spec.*`-named files, `npm run lint` does not
npm run build
npm run lint
```

**A tree with plugins configured does not pass the base repository's `npm run test`.** `extension-ui-host.component.test.tsx` pins "the merged registry equals the built-in catalog, length 1, with zero plugins configured" — that property is the entire reason the registry is split into two modules, and it must never be loosened to `>= 1` to accommodate a local plugin. Your acceptance gate is items 3 and 4 above plus the parity check in §7, not `npm run test`.

`--extra-root` only widens the vocabulary guard's scan face: it scans your tree's `**/*.py` for `user_error(...)` messages with the same blacklist and the same `SANCTIONED_UI` allowance, and leaves every other check and every line of output unchanged.

### 5.4 End-to-end by hand

1. Start backend and frontend, sign in.
2. The entry row appears in the sources panel's fixed area, above the scrolling source list. If it does not, work down the four visibility gates in §7.
3. Open it, run your action, import one document; the source list refreshes.
4. Open `/admin/extensions` as a system administrator — your plugin is listed with its version, trust `部署装入`, its server contributions and its UI contributions.
5. Check the event log: your records carry only `kind`, `plugin_id`, and the whitelisted counters.

## 6. Step four — package and hand over

Ship four things, and only four:

1. **A Python wheel** on the internal index (or a path the target can `pip install`). It must declare no dependency on this repository — the SDK is imported from the backend environment, not vendored.
2. **The UI package directory**, either inside the wheel's data files, as a separate tarball, or as an internal npm package the target unpacks. It must arrive as a flat directory named exactly what the deployment will point at.
3. **A TOML fragment** to paste into the deployment's `extensions.toml`, plus the names of the environment variables the settings reference (`CORP_SEARCH_API_KEY` above) — never their values.
4. **A `CHANGELOG.md` entry** naming the supported `api_version`, the plugin `version` (it must match `ui-plugin.json`'s `version`, or the browser tuple check hides the contribution), and any settings-key change.

## 7. Step five — install on a deployment

```bash
# 1. the public checkout, unmodified
cd /srv/silicon-notebook && git pull

# 2. the plugin, into the backend's own interpreter
"$PYTHON_BIN" -m pip install /tmp/silicon_notebook_corp_search-0.1.0-py3-none-any.whl

# 3. the deployment config
sudo install -m 0640 -o silicon -g silicon \
  /tmp/extensions.toml /etc/silicon-notebook/extensions.toml
# in the root .env (or the service's environment):
#   EXTENSIONS_CONFIG=/etc/silicon-notebook/extensions.toml
#   SILICON_NOTEBOOK_UI_PLUGINS=/srv/silicon-notebook-plugins/corp-search

# 4. build and start; both variables must be in this process's environment
export EXTENSIONS_CONFIG=/etc/silicon-notebook/extensions.toml
export SILICON_NOTEBOOK_UI_PLUGINS=/srv/silicon-notebook-plugins/corp-search
npm run start

# 5. readiness
curl -s http://127.0.0.1:8000/api/ready
```

`npm run start` runs `npm ci` (whose `postinstall` syncs the UI packages) and then `npm run build` (whose `prebuild` syncs them again) in the foreground before it backgrounds the two services. Both inherit this shell's environment, so exporting `SILICON_NOTEBOOK_UI_PLUGINS` before step 4 is what makes the plugin part of the build. With `SKIP_INSTALL=1` or `SKIP_BUILD=1` you are responsible for having run the sync yourself.

### Startup validation, stage by stage

Every rejection is a **startup failure**: the process refuses to start rather than come up half-wired. All of them are logged once, through the logger `silicon_notebook.extensions`, carrying a plugin id, a stable reason code, and an exception *class* name — never a settings value, a module path, a file path, or upstream exception text.

| Stage | What runs | Failure looks like |
| --- | --- | --- |
| discover | Read the TOML, validate its shape, then each entry in plugin-id order | `extension configuration rejected: config_*` (no plugin id yet) or `extension 'corp.search' rejected: plugin_*` |
| import | `importlib.import_module` on the bundle spec, then `getattr` | `plugin_module_import_failed (ModuleNotFoundError)` — the class name is the whole diagnosis |
| identity | manifest type, id match, `trust`, `api_version` | `plugin_api_version_unsupported` — a core upgrade moved `EXTENSION_API_VERSION`; install the matching plugin build |
| settings | key set, pydantic validation, `configure()` | `plugin_settings_unknown_key keys=['timout_seconds']` |
| register / freeze | `register()`, declaration match, capability merge, dependency order | `plugin_registration_failed (ConnectionError)` — usually `configure`-scale work that leaked into `register` |
| routes | Collect the router contributions, build and mount them | `PluginRouteMountError: corp.search: plugin_route_missing_notebook_gate` |
| ready | Migrations, warm-up, index preload | unrelated to plugins |

The `extension discovery FAILED — service will not start` line names the plugin, the reason and the exception class. That is the whole log surface by design; §9 turns each code into an action.

### Reconcile the frontend build against this deployment's topology

The committed `backend/tests/fixtures/ui_extension_contract.json` is generated with `EXTENSIONS_CONFIG` cleared, so CI never sees a site's plugins. To ask "does the frontend I just built still match the plugins that will actually load here", run — with the site's own environment:

```bash
EXTENSIONS_CONFIG=/etc/silicon-notebook/extensions.toml PYTHONPATH=backend \
  python3 scripts/check_deployment_extension_parity.py \
  --frontend-contract frontend/.local/ui-extension-contract.json
```

| Exit | Meaning |
| --- | --- |
| `0` | Parity — the live topology matches the frontend contract. |
| `1` | Drift — a per-row diff (five wire fields only) on stderr. Usually a version bump on one side only, or a UI package that was not in `SILICON_NOTEBOOK_UI_PLUGINS` at build time. |
| `2` | Usage/environment error — missing or malformed `--frontend-contract`, an `api_version` other than `"1"`, or this deployment's own discovery/registry composition failed (only the plugin id and its stable reason are printed). |

The script is read-only end to end: no file writes, no `Repository`, no network. `check_contracts.sh` deliberately does not run it — CI has no deployment to check against.

### Acceptance checklist

- `/api/ready` reports ready.
- `GET /api/admin/extensions` (or `/admin/extensions` in the browser, system administrator only) lists the plugin with the expected version and contributions.
- The workspace entry renders. It needs **four** gates true at once: the local tuple `(plugin_id, version, contribution_id)` matches a server row; that row's `available` is `true`; the core permission snapshot grants the manifest's `permission`; and `mode` is `all` or the user's UI mode is `advanced`.
- One real action works end to end (e.g. import one document, and the source list refreshes).
- The event log shows your records with `plugin_id` and counters only — no titles, ids, questions, or exception text.
- The parity check exits `0`.

## 8. Upgrade, rollback, disable

| Situation | Action |
| --- | --- |
| Core upgrade, `EXTENSION_API_VERSION` unchanged | `git pull`, rebuild the frontend with `SILICON_NOTEBOOK_UI_PLUGINS` set, restart. Nothing about the plugin changes. |
| Core upgrade, `EXTENSION_API_VERSION` changed | Startup refuses with `plugin_api_version_unsupported` and names the plugin. Install the plugin build that supports the new version **first**, then upgrade core. There is no compatibility window. |
| Plugin upgrade | Install the new wheel, replace the UI package directory, rebuild the frontend, restart. Bump `version` in the manifest *and* `ui-plugin.json` together — a mismatch makes the browser tuple check hide the contribution while the backend still lists it. |
| Rollback | Install the previous wheel and UI package, restart. Or set `enabled = false` on the entry and restart — a disabled entry is never even imported. |
| Disable temporarily | `enabled = false` in the TOML, restart. Do **not** clear `EXTENSIONS_CONFIG` to disable one plugin: that swaps in a different discovery/registry composition for every plugin at once. |

**Every one of these is a process restart.** There is no hot reload, deliberately: the registry is frozen at startup so the loaded topology is a fact for the lifetime of the process rather than a moving target every request has to re-derive.

Offline CLI tools (`scripts/batch_ingest.py` and friends) build the same runtime and therefore load the same plugin topology. If a batch job fails on a plugin, the fix is the config file — never clearing the variable for that one run, which silently gives that job a different composition from the service.

## 9. Rejection codes

Every code below is stable, appears verbatim in the startup log, and carries at most a plugin id, offending key *names*, and an exception class name.

### Discovery — file and entry shape (`app/extensions/discovery.py`)

| Code | Meaning | Fix |
| --- | --- | --- |
| `config_unreadable` | `EXTENSIONS_CONFIG` points at a file that cannot be opened | Check the path, ownership and mode. The path itself is never echoed — it may be a deployment detail. |
| `config_invalid_toml` | The file is not valid TOML | Parse it locally; the parser's message is deliberately not forwarded. |
| `config_unknown_top_level_key` | A top-level key other than `extensions` | The named keys are listed. The only legal top-level table is `[extensions]`. |
| `config_extensions_not_a_table` | `extensions` is not a table | Use `[extensions."<id>"]`, not a list. |
| `plugin_id_invalid` | The table key is not a stable id | Lowercase, dot/underscore/hyphen separated: `corp.search`. |
| `plugin_entry_not_a_table` | `extensions.<id>` is not a table | You probably wrote `corp.search = "..."`. |
| `plugin_unknown_key` | A key other than `bundle` / `enabled` / `settings` | The named keys are listed; typos land here. |
| `plugin_enabled_not_bool` | `enabled` is not a TOML boolean | `true` / `false`, unquoted. |

### Discovery — bundle load

| Code | Meaning | Fix |
| --- | --- | --- |
| `plugin_bundle_missing` | No `bundle` key | Add `bundle = "module.path:ATTRIBUTE"`. |
| `plugin_bundle_spec_invalid` | Not exactly one `:`, empty module, or a non-identifier attribute | The form is `module.path:ATTRIBUTE`. |
| `plugin_module_import_failed` | The import raised | The exception class is the diagnosis: `ModuleNotFoundError` = not installed in `PYTHON_BIN`'s environment; anything else = an error in the plugin's import-time code. |
| `plugin_attribute_missing` | The module has no such attribute (or its `__getattr__` raised) | Check the attribute name and that it is module-level. |
| `plugin_not_a_bundle` | `manifest` is not an `ExtensionManifest`, `register` is not callable, or reading either attribute raised | Usually a plugin built against a different SDK, a manifest built from a copy of the dataclass, or `manifest`/`register` implemented as a property that raises. |
| `plugin_id_mismatch` | `manifest.id` ≠ the config key | Make them identical; the config key is authoritative. |
| `plugin_trust_not_deployment` | `trust` is not `"deployment"` | Only `deployment` may be loaded this way. `isolated` is reserved and refused everywhere today. |
| `plugin_api_version_unsupported` | `manifest.api_version` ≠ this build's `EXTENSION_API_VERSION` | Install the plugin build that matches this core build. |
| `plugin_module_import_interrupted` | `KeyboardInterrupt`/`SystemExit` during the plugin's import | Not a rejection — the signal propagates. The line exists only to name which plugin's import the interpreter was inside. |

### Discovery — settings

| Code | Meaning | Fix |
| --- | --- | --- |
| `plugin_settings_not_a_table` | `settings` is not a table | Use `[extensions."<id>".settings]`. |
| `plugin_settings_not_accepted` | A `[settings]` table for a plugin that declares neither `settings_model` nor `configure` | Remove the table, or add both halves to the bundle. |
| `plugin_settings_binding_missing` | Exactly one of `settings_model` / `configure` is declared, or reading either raised | They are a pair. Add the other, or remove both; a raising property is treated the same as a missing attribute. |
| `plugin_settings_model_invalid` | `settings_model` is not a pydantic `BaseModel` subclass | Use `pydantic.BaseModel`. |
| `plugin_settings_unknown_key` | A key the model does not accept | The names are listed. Also lands here for a key that only matches an `AliasChoices`/`AliasPath` alias (registered limitation, fail-closed). |
| `plugin_settings_invalid` | Pydantic rejected the table | Only the exception class is shown, because `ValidationError` echoes the rejected value. Validate locally against the same model. |
| `plugin_settings_binding_failed` | `configure()` raised | Almost always work that does not belong there — a connection, a thread, an upstream probe. Move it to first use. |

### Discovery — capabilities

| Code | Meaning | Fix |
| --- | --- | --- |
| `plugin_attribute_access_failed` | Re-reading `manifest` while merging capability decisions raised (this manifest was already read once, successfully, during bundle load — this is a non-deterministic accessor) | Only the exception class is shown. Make `manifest` a plain, non-raising attribute. |
| `plugin_capability_declaration_invalid` | `capability_decisions` is absent while `provides` is non-empty, is not a Mapping, cannot be iterated, reading it raised, or a probe is not callable | Supply a plain `dict[str, AvailabilityProbe]`. |
| `plugin_capability_name_invalid` | A name is not a stable id | Lowercase, dot/underscore/hyphen. `:` is reserved for core. |
| `plugin_capability_not_declared` | A probe for a name not in `provides` | The names are listed. Add them to `provides` or drop the probes. |
| `plugin_capability_missing_decision` | A `provides` name with no probe | The names are listed. |
| `plugin_capability_conflicts_core` | The name collides with a core capability | Rename. Core's own names use `point:name`, so this normally means a literal duplicate. |
| `plugin_capability_conflicts_plugin` | The name collides with another plugin's | Namespace it with your own prefix. |

### Registration

| Code | Meaning | Fix |
| --- | --- | --- |
| `plugin_registration_failed` | `register()` raised anything other than `ExtensionRegistryError` | Only the exception class is shown — a bundle holding an API key must not be able to leak it through a traceback. Reproduce locally. |
| *(unsanitized)* `extension '<id>' registrations do not match its manifest` | The contribution ids you registered ≠ the ids your manifest declares | Core's own diagnostic, kept verbatim because it is built only from validated ids. Make the two sets identical. |

### Router collection (`app/extensions/http_router.py`)

| Code | Meaning | Fix |
| --- | --- | --- |
| `plugin_router_kind_invalid` | The router declaration's kind is not `CONTRIBUTOR` | Declare `ContributionKind.CONTRIBUTOR` and register with `add_contributor`. |
| `plugin_router_trust_denied` | A `builtin` bundle contributed a router | Core endpoints belong in `app/api/*_routes.py`, under the frozen `api_contract` fixture. |
| `plugin_router_multiple` | Two router contributions from one plugin | Both would mount under the same prefix and shadow each other by registration order. One prefix, one router. |
| `plugin_router_factory_invalid` | The registered implementation is not callable | Register the factory function itself, not its result. |

### Router mounting (`app/api/extension_routes.py`)

| Code | Meaning | Fix |
| --- | --- | --- |
| `plugin_router_not_a_router` | The factory returned something that is not an `APIRouter` | Return `APIRouter()`; do not return the app or a list of routes. |
| `plugin_route_lifecycle_denied` | The router carries `on_startup`/`on_shutdown` | Those would run inside the application lifespan, next to migrations and warm-up, with no budget and no failure containment. Do the work lazily. |
| `plugin_route_unsupported_kind` | A route that is not an `APIRoute` | A mounted sub-application, a raw websocket, or a bare Starlette route escapes the dependency inspection, so its notebook gate cannot be proven. |
| `plugin_route_missing_notebook_gate` | A path containing `{notebook_id}` runs none of core's gates | Add `Depends(context.require_notebook_read)` or `Depends(context.require_notebook_capability("<capability>"))`. Wrapping a core gate inside your own dependency counts — the scan is transitive. |
| `plugin_router_factory_failed` | `spec.factory(context)` raised anything other than `PluginRouteMountError`/`KeyboardInterrupt`/`SystemExit` | Only the exception class is shown — the factory runs with the plugin's own validated settings in hand, so an unsanitized message could leak a secret into a startup traceback. Reproduce locally. |
| `plugin_router_validation_failed` | The structural checks above raised something other than the `PluginRouteMountError` they themselves throw — e.g. an `APIRouter` subclass whose `on_startup`/`on_shutdown`/`routes` raises on read | Only the exception class is shown, same reasoning as `plugin_router_factory_failed`. Do not override those attributes with anything that can fail. |

### Runtime

| Signal | Meaning |
| --- | --- |
| `plugin_upstream_unauthorized` event, client sees `424` | Your handler **or one of your own dependencies** raised (either `fastapi.HTTPException` or `starlette.exceptions.HTTPException`), or your handler returned, a 401. Core translated it so the browser does not sign the user out. Translate upstream 401s yourself if you want your own wording. |
| `404` from `url_sources.import_urls` / `import_urls_async` | The calling user does not hold `sources:write` on that notebook — or the notebook does not exist. The two are deliberately indistinguishable. |
| `RuntimeError: url_sources.import_urls must not be called from an async handler…`, client sees `500` | An `async def` handler called the blocking variant, which would have stalled the event loop for every other in-flight request. `await url_sources.import_urls_async(...)` instead; a sync `def` handler keeps calling `import_urls`. Nothing was imported. |
| Your event silently absent from the log | The payload carried a field outside `event`/`outcome`/`count`/`elapsed_ms`, a code longer than 64 characters or not matching `^[a-z][a-z0-9_]{0,63}$`, or a `count`/`elapsed_ms` that is not an integer in `0..1e9` (`True` is not `1`). The whole record is dropped rather than partially written. |
| The entry does not render, but `/admin/extensions` lists the plugin | One of the four visibility gates in §7 is false. Check `GET /api/system/extensions` first: that row's `available` and `unavailable_reason` (`disabled` = your probe returned `DISABLED`, `unavailable` = it returned `UNAVAILABLE`). If the row is absent altogether, the browser's local tuple does not match — the manifest `version` on the two sides has drifted. |

## 10. Deliberately unsupported

| Not supported | Why, and what to do instead |
| --- | --- |
| Hot reload | The registry is frozen at startup so the topology is a fact rather than a per-request question. Enabling, disabling and upgrading are restarts. |
| Process-isolated plugins | `trust="isolated"` is a reserved value the registry refuses today. A deployment plugin is trusted, in-process code. |
| Plugin-owned database tables | The schema is versioned, checksummed, and forward-replicated as one closed set. Persist through the seams you are given, or keep state in your own upstream. See §10 of the modular plugin architecture design. |
| Anonymous plugin routes | The mount always carries a router-level session dependency. Public, sign-in-free pages are a core product decision, not a plugin one. |
| Extending the MCP tool catalog | `PUBLIC_TOOLS` is one frozen, core-owned list with static documentation and smoke guards derived from it. Not open to plugins. |
| Plugin CSS, plugin dependencies, remote browser code | Visuals reuse existing classes and `:root` tokens; new dependencies go through a base-repository PR; the browser never fetches plugin JavaScript at runtime. |
| A dedicated root-dialog slot per plugin | There is one generic `extension` slot, claimed per contribution, so one plugin dialog is visible at a time. Naming plugins in core's slot union would mean patching the public tree for every install — the exact thing this whole procedure exists to avoid. |

## 11. Checklists

### Plugin developer

- [ ] `manifest.id` equals the TOML key, `trust="deployment"`, `api_version` equals the target build's.
- [ ] `settings_model` and `configure` are both present or both absent; `configure` only stores.
- [ ] Secrets are referenced by env-var name, never embedded.
- [ ] `provides` and `capability_decisions` have identical key sets; probes are I/O-free.
- [ ] Every `{notebook_id}` route carries a core gate; nothing raises a bare 401.
- [ ] Backend imports are limited to `app.extension_sdk` and `app.domain`.
- [ ] The UI package is flat: one `ui-plugin.json`, one entry file, `.ts`/`.tsx` siblings only, no CSS, no `package.json`.
- [ ] `plugin_id` / `id` / `capability` / `version` match the backend manifest exactly.
- [ ] The UI imports nothing outside the allowlist — and never `api.ts`.
- [ ] No timers, no dynamic `import(`, no sockets, no `fetch(`, no `error.message`, no colour literals.
- [ ] `ExtensionModal` receives `context={context}`, `actions={actions}` and a `storageKey`; the dialog opens through `actions.openDialog()` and there is no local `open` state.
- [ ] `refreshSources()` is called at most once, after the action, and its rejection is caught.
- [ ] `check_ui_vocabulary.py --extra-root <src>` is clean.
- [ ] The base repository's `extension-*` guards, `npm run build`, and `npm run lint` are clean with the package synced.
- [ ] `CHANGELOG.md` names the supported `api_version`; `version` was bumped in both manifests together.

### Operator

- [ ] The public checkout is unmodified and at the intended commit.
- [ ] The plugin is installed into the backend's own `PYTHON_BIN` environment.
- [ ] `extensions.toml` is owner-readable only and holds no raw secrets.
- [ ] `EXTENSIONS_CONFIG` and `SILICON_NOTEBOOK_UI_PLUGINS` are both in the environment of the process that builds and starts the service.
- [ ] Startup produced no `silicon_notebook.extensions` error line.
- [ ] `/api/ready` is ready.
- [ ] `check_deployment_extension_parity.py` exits `0`.
- [ ] `/admin/extensions` lists the expected plugin, version and contributions.
- [ ] One real user action works end to end.
- [ ] The rollback path is written down: previous wheel + previous UI package, or `enabled = false`, plus a restart.
