# Application Boundary Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the FastAPI router and Pydantic monoliths into domain-owned modules, centralize frontend API transport, preserve the complete public contract, and keep two consecutive local warm gates at or below 60 seconds.

**Architecture:** Use a contract-first strangler migration. Temporary normalized OpenAPI and route-collision fixtures protect the move; `app.api.routes` and `app.models.schemas` remain compatibility composition/facade modules, while new production code imports domain modules directly. The frontend gets a neutral auth-session/config layer plus one transport client; domain files retain endpoint and product-policy ownership.

**Tech Stack:** Python 3.13/Homebrew, FastAPI, Pydantic v2, pytest + pytest-xdist, TypeScript, React 19, Next.js 15, Node test runner, Vitest.

## Global Constraints

- Work only in `/Users/hzf/workspace/silicon_notebook/.worktrees/application-boundary-foundation` on `codex/application-boundary-foundation`.
- Use `/opt/homebrew/Caskroom/miniconda/base/bin/python` through `PYTHON_BIN`; do not create or switch to another Python environment.
- Use TDD for every behavioural or architecture change: test/characterize first, observe the expected failure where the target behaviour does not yet exist, implement minimally, then rerun.
- Do not change public paths, methods, dependency order, response schemas, status codes, trusted error policy, permissions, streaming semantics, polling cadence, or cancellation semantics.
- Do not delete or retire an endpoint in this PR.
- Do not split `page.tsx` state into hooks and do not change FastAPI lifespan/application lifecycle.
- Do not add generated API clients, a frontend state library, source-line tests, source-position tests, file-size tests, or total route/model-count tests.
- The local Apple Silicon warm gate is a hard acceptance condition: two consecutive uncontended `scripts/check.sh` runs must each complete in at most 60 seconds. CI duration remains observational only.
- Prove functional/API equivalence before removing temporary migration tests or consolidating duplicate tests.
- Every removed migration-time test needs an explicit retained-coverage mapping in the PR body; never delete meaningful coverage to reach 60 seconds.
- Keep full-stack parity and all current UI entry points; this is an internal architecture change, not a product redesign.
- Synchronize `README.md`, `README_zh.md`, `AGENTS.md`, `architecture.md`, and the historical-debt ledger in the same PR.
- Deliver one PR. After it is created and green, run an independent exact-head review using `gpt-5.6-terra` with `reasoning_effort=high`; fix every Critical/Important finding and reverify the new exact head.

---

## File and ownership map

### Backend model modules

- Create `backend/app/models/common.py`: `Evidence`.
- Create `backend/app/models/identity.py`: `UserProfile`, auth request/result, Agent profile/principal/token contracts.
- Modify `backend/app/models/memory.py`: retain `MemoryWrite`/`MemoryRevision`; add Memory aliases and API/retrieval contracts.
- Create `backend/app/models/sources.py`: paper metadata, source element/summary/import/detail, URL import, and document-type detection contracts.
- Create `backend/app/models/notebooks.py`: notebook create/update/summary, sharing, mounted-base, tier, template, and analytics contracts.
- Create `backend/app/models/reports.py`: report create/outline/generate/summary/export/detail contracts.
- Create `backend/app/models/ask.py`: `RuleCard`, citations, trace, Ask, conversations, search, KG-search projection, and feedback contracts.
- Create `backend/app/models/knowledge.py`: knowledge browse/update/schema/graph/edge-review/dedupe/merge contracts.
- Create `backend/app/models/kg.py`: KG-build job, unified-KG/index status, merge-review, and concept-whitelist contracts.
- Create `backend/app/models/knowhow.py`: every session and Agent Knowhow wire contract.
- Create `backend/app/models/content_overview.py`: Memory/Knowhow overview and notebook-content overview contracts.
- Create `backend/app/models/admin.py`: KG/Memory promotion and admin-user projection contracts.
- Create `backend/app/models/model_services.py`: model settings/test contracts.
- Modify `backend/app/models/schemas.py`: compatibility imports/re-exports only; no class or validator definitions.
- Create `backend/app/core/memory_inputs.py`; delete `backend/app/services/memory_inputs.py` after all production/tests import the neutral module.

### Backend route modules

- Create `backend/app/api/system_routes.py`: health, `/me`, model settings/test, doc types/templates, and `/me/pending-actions` REST/stream.
- Create `backend/app/api/notebook_routes.py`: notebook CRUD/analytics, tier, bases, sharing, membership.
- Create `backend/app/api/source_routes.py`: source import/upload/detail/parse/elements/delete, notebook assets, paper-metadata backfill.
- Create `backend/app/api/knowhow_routes.py`: all session Knowhow table/import/edit/template/optimize/reformat/transfer endpoints.
- Create `backend/app/api/knowledge_routes.py`: knowledge browse/update/schema/dedupe/merge/graph and edge-review endpoints.
- Create `backend/app/api/ask_routes.py`: notebook search, Ask/modes/stream/jobs/conversations, answer feedback.
- Create `backend/app/api/report_routes.py`: report lifecycle and export.
- Create `backend/app/api/kg_routes.py`: KG build/rebuild/relink, KG search, unified graph, scale index, conflicts, whitelist, merge review.
- Create `backend/app/api/admin_routes.py`: promotions and admin user/online endpoints.
- Modify `backend/app/api/routes.py`: include existing `memory_router` and the domain routers in collision-safe order; keep only proven compatibility exports.

### Frontend transport and domain modules

- Create `frontend/app/api-config.ts`: `API_BASE` only.
- Create `frontend/app/auth-session.ts`: token storage and `authHeaders()` only.
- Create `frontend/app/api-client.ts`: URL resolution, auth, diagnostics, trusted errors, JSON/void/Blob handling, AbortSignal, and the reviewed raw-response seam.
- Create `frontend/app/api-client.test.mjs`: transport behaviours.
- Create `frontend/app/api-boundary.test.mjs`: semantic guard against new local transport implementations.
- Create `frontend/app/system-api.ts`, `notebook-api.ts`, `source-api.ts`, `ask-api.ts`, `knowledge-api.ts`, `report-api.ts`, and `kg-api.ts` for endpoint ownership now embedded in `page.tsx`.
- Modify existing domain clients (`notebook-bases.ts`, `notebook-share.ts`, `notebook-tier.ts`, `knowhow-model.ts`, `knowhow-transfer.ts`, `memory-transfer.ts`, `edge-review-queue.ts`, `promotion-queue.ts`, `model-settings.ts`, admin/dev-log clients) to consume `api-client.ts` rather than define transport.
- Modify `auth.ts` to retain auth product policy and re-export compatibility names from the neutral config/session modules without creating a cycle.
- Modify `page.tsx`, `memory-panel.tsx`, `pending-center.tsx`, `transfer-picker.tsx`, `knowhow-cell-editor.tsx`, and `knowhow-panel.tsx` to call domain clients.

---

### Task 1: Restore the verified warm gate before migration

**Files:**
- Modify: `backend/tests/test_test_architecture_policy.py`
- Modify: `scripts/check_backend.sh`

**Interfaces:**
- Consumes: `BACKEND_PYTEST_WORKERS`, currently overridable by environment.
- Produces: a default full-gate backend worker count of `9`; explicit overrides such as CI's `BACKEND_PYTEST_WORKERS=4` remain valid.

- [ ] **Step 1: Change the architecture test to require the measured gate default**

Keep direct `pytest` bounded at 12 workers, but require the complete gate to reserve CPU for the concurrently running contract and Next.js lanes:

```python
def test_backend_parallelism_is_bounded_and_explicit():
    config = (ROOT / "backend" / "pytest.ini").read_text(encoding="utf-8")
    assert "addopts = -n 12 --dist loadgroup" in config
    assert "architecture_contract:" in config
    assert "graph_index_contract:" in config
    assert "-n auto" not in config
    assert "worksteal" not in config
    backend_lane = (ROOT / "scripts" / "check_backend.sh").read_text(encoding="utf-8")
    assert 'BACKEND_PYTEST_WORKERS="${BACKEND_PYTEST_WORKERS:-9}"' in backend_lane
```

- [ ] **Step 2: Run the focused test and observe the expected failure**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n0 backend/tests/test_test_architecture_policy.py::test_backend_parallelism_is_bounded_and_explicit
```

Expected: FAIL because `check_backend.sh` still defaults to 12.

- [ ] **Step 3: Change only the complete-gate default**

In `scripts/check_backend.sh`:

```bash
BACKEND_PYTEST_WORKERS="${BACKEND_PYTEST_WORKERS:-9}"
```

Do not change the environment override or `backend/pytest.ini` in this task.

- [ ] **Step 4: Re-run the focused test**

Expected: PASS.

- [ ] **Step 5: Run two complete warm gates without overriding worker count**

Run twice, separately:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Expected for each run: all lanes PASS; `contracts`, `backend`, and `frontend` are each at most 60 seconds. Record the three lane values from both runs for the PR body. The planning measurement with 9 workers was `contracts=14`, `backend=57`, `frontend=20`.

- [ ] **Step 6: Commit the bounded gate**

```bash
git add backend/tests/test_test_architecture_policy.py scripts/check_backend.sh
git commit -m "test: keep the warm gate within sixty seconds"
```

### Task 2: Capture temporary public-contract and route-collision evidence

**Files:**
- Create: `backend/tests/application_boundary_snapshot.py`
- Create: `backend/tests/fixtures/application_boundary_contract.json`
- Create: `backend/tests/test_application_boundary_contract.py`

**Interfaces:**
- Consumes: `app.main.app`, FastAPI `APIRoute`, current OpenAPI document.
- Produces: `snapshot(app) -> dict[str, object]` containing normalized OpenAPI and only the relative order of route pairs that can match the same method/path shape.

- [ ] **Step 1: Add the snapshot helper**

Implement the helper with stable semantic keys, never source lines or total-count assertions:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute


def _segments(path: str) -> tuple[str, ...]:
    return tuple(part for part in path.strip("/").split("/") if part)


def _dynamic(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def _overlap(left: str, right: str) -> bool:
    left_parts, right_parts = _segments(left), _segments(right)
    if len(left_parts) != len(right_parts):
        return False
    return all(
        a == b or _dynamic(a) or _dynamic(b)
        for a, b in zip(left_parts, right_parts, strict=True)
    )


def _api_routes(app: FastAPI) -> list[APIRoute]:
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _collision_order(app: FastAPI) -> list[dict[str, str]]:
    routes = _api_routes(app)
    collisions: list[dict[str, str]] = []
    for index, left in enumerate(routes):
        for right in routes[index + 1:]:
            methods = sorted((left.methods or set()) & (right.methods or set()) - {"HEAD", "OPTIONS"})
            if not methods or not _overlap(left.path, right.path):
                continue
            for method in methods:
                collisions.append({
                    "method": method,
                    "first_path": left.path,
                    "first_name": left.name,
                    "second_path": right.path,
                    "second_name": right.name,
                })
    return collisions


def snapshot(app: FastAPI) -> dict[str, Any]:
    return {
        "openapi": app.openapi(),
        "collision_order": _collision_order(app),
    }


def write_snapshot(app: FastAPI, target: Path) -> None:
    target.write_text(
        json.dumps(snapshot(app), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
```

- [ ] **Step 2: Generate the temporary baseline fixture from the untouched application**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -c 'from pathlib import Path; from app.main import app; from tests.application_boundary_snapshot import write_snapshot; write_snapshot(app, Path("backend/tests/fixtures/application_boundary_contract.json"))'
```

Expected: the JSON file contains `openapi` and `collision_order` and is deterministic across two generations (`git diff --exit-code` after the second generation).

- [ ] **Step 3: Add the temporary characterization test**

```python
import json
from pathlib import Path

from app.main import app
from tests.application_boundary_snapshot import snapshot


FIXTURE = Path(__file__).parent / "fixtures" / "application_boundary_contract.json"


def test_application_contract_matches_pre_split_baseline():
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert snapshot(app) == expected
```

- [ ] **Step 4: Run the characterization test**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n0 backend/tests/test_application_boundary_contract.py
```

Expected: PASS on the pre-split application.

- [ ] **Step 5: Commit the explicitly temporary migration evidence**

```bash
git add backend/tests/application_boundary_snapshot.py backend/tests/fixtures/application_boundary_contract.json backend/tests/test_application_boundary_contract.py
git commit -m "test: characterize the application boundary contract"
```

### Task 3: Establish neutral validation and the first domain model modules

**Files:**
- Create: `backend/app/core/memory_inputs.py`
- Delete: `backend/app/services/memory_inputs.py`
- Create: `backend/app/models/common.py`
- Create: `backend/app/models/identity.py`
- Modify: `backend/app/models/memory.py`
- Create: `backend/app/models/sources.py`
- Create: `backend/app/models/notebooks.py`
- Create: `backend/app/models/kg.py`
- Create: `backend/app/models/reports.py`
- Modify: `backend/app/models/schemas.py`
- Create: `backend/tests/fixtures/legacy_schema_exports.json`
- Create: `backend/tests/test_model_domain_boundaries.py`
- Modify: `backend/app/api/auth_routes.py`
- Modify: `backend/app/api/deps.py`
- Modify: `backend/app/api/mcp_server.py`
- Modify: `backend/app/api/memory_routes.py`
- Modify: `backend/app/core/request_context.py`
- Modify: relevant repository/service imports named in Step 6

**Interfaces:**
- Consumes: all current model classes and validator functions without changing fields/config/validators.
- Produces: domain model modules; the old `app.models.schemas.<Name>` objects are identical (`is`, not merely schema-equal) to the new definitions.

- [ ] **Step 1: Freeze the legacy facade names without freezing future model growth**

Generate the one-time list of current public model/type names before moving definitions:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -c 'import json; from pathlib import Path; import app.models.schemas as s; from pydantic import BaseModel; names = sorted(name for name, value in vars(s).items() if (isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == "app.models.schemas") or name in {"MemoryOrigin", "MemoryStatus", "MemoryPromotionState"}); Path("backend/tests/fixtures/legacy_schema_exports.json").write_text(json.dumps(names, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")'
```

This fixture is a legacy compatibility surface, not a model-count gate: new domain models do not get added automatically and therefore do not force unrelated future test edits.

- [ ] **Step 2: Add failing ownership and identity tests**

Create `backend/tests/test_model_domain_boundaries.py` with semantic import checks:

```python
import ast
import importlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "backend" / "app" / "models"
LEGACY = json.loads(
    (Path(__file__).parent / "fixtures" / "legacy_schema_exports.json").read_text(encoding="utf-8")
)
DOMAIN_MODULES = (
    "common",
    "identity",
    "memory",
    "sources",
    "notebooks",
    "kg",
    "reports",
)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_first_domain_model_modules_exist_without_reverse_dependencies():
    for module_name in DOMAIN_MODULES:
        path = MODELS / f"{module_name}.py"
        assert path.exists(), module_name
        imports = _imports(path)
        assert not any(name.startswith("app.api") for name in imports)
        assert not any(name.startswith("app.services") for name in imports)
        assert not any(name.startswith("app.repositories") for name in imports)


def test_legacy_schema_exports_resolve_to_domain_objects():
    facade = importlib.import_module("app.models.schemas")
    assert sorted(facade.__all__) == LEGACY
    for name in LEGACY:
        assert hasattr(facade, name), name
```

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n0 backend/tests/test_model_domain_boundaries.py
```

Expected: FAIL because the domain files and `schemas.__all__` do not yet exist.

- [ ] **Step 3: Move Memory validation into neutral core code**

Move the implementation byte-for-byte from `app.services.memory_inputs` to `app.core.memory_inputs`. Update imports in:

```text
backend/app/models/schemas.py
backend/app/services/memory_service.py
backend/app/api/mcp_server.py
backend/app/api/memory_routes.py
backend/tests/test_memory_service.py
backend/tests/test_memory_repository_boundaries.py
backend/tests/test_memory_api.py
backend/tests/test_memory_mcp.py
```

The resulting import must be:

```python
from app.core.memory_inputs import (
    MemoryInputError,
    normalize_client_request_id,
    normalize_content,
    normalize_evidence_refs,
    normalize_reason,
    normalize_tags,
    normalize_task_context,
    normalize_title,
)
```

Import only the names each consumer uses. Delete the service-layer file after `rg -n 'app.services.memory_inputs' backend` returns no matches.

- [ ] **Step 4: Move the first model groups without rewriting definitions**

Move these exact definitions and aliases:

```text
common.py:
  Evidence

identity.py:
  UserProfile, AgentProfile, AgentProfileCreate, AgentProfileUpdate,
  AgentTokenCreate, AgentTokenSummary, AgentTokenIssued, AgentPrincipal,
  AuthRequest, AuthResult

memory.py:
  MemoryOrigin, MemoryStatus, MemoryPromotionState, MemoryRecord, MemoryHit,
  MemoryNotebookOption, PaginatedMemories, MemoryPreview,
  MemoryCreateFromAnswer, AnswerMemoryLinksRequest,
  AnswerMemoryLinksResponse, MemoryBulkDeleteRequest, MemoryUpdate,
  MemoryReviewRequest, MemoryTransferRequest

sources.py:
  PaperAuthor, PaperMeta, SourceElement, SourceSummary, PaginatedSources,
  SourceImportFile, SourceImportRequest, AddUrlSourcesRequest, RejectedUrl,
  AddUrlSourcesResult, SourceDetail, DetectDocTypeItem,
  DetectDocTypesRequest, DetectedDocType

notebooks.py:
  NotebookCreate, NotebookUpdate, NotebookRef, MountedBase, NotebookSummary,
  ShareResponse, SharedPreview, SharedByMeItem, NotebookTemplate,
  SetTierRequest, SetBasesRequest, MountedByCount, NotebookAnalytics

kg.py:
  KgBuildJobStatus

reports.py:
  ReportCreate, ReportOutlineUpdate, ReportGenerateRequest, ReportSummary,
  ReportExportRequest, ReportDetail
```

`NotebookSummary` imports `KgBuildJobStatus` directly from `app.models.kg`. No domain module imports the compatibility facade.

- [ ] **Step 5: Re-export the moved objects from `schemas.py`**

Use explicit imports, for example:

```python
from app.models.common import Evidence
from app.models.identity import AuthRequest, AuthResult, UserProfile
from app.models.memory import MemoryHit, MemoryRecord, MemoryStatus, MemoryUpdate
from app.models.notebooks import NotebookCreate, NotebookSummary, NotebookUpdate
from app.models.reports import ReportCreate, ReportDetail, ReportSummary
from app.models.sources import SourceDetail, SourceElement, SourceSummary
```

Complete the explicit import lists for every name moved in Step 4 and set `__all__` to the frozen legacy list. Do not duplicate/subclass a Pydantic model.

- [ ] **Step 6: Point first-party production consumers at domain modules**

Update imports in these exact production files when they consume a moved name:

```text
backend/app/api/auth_routes.py
backend/app/api/deps.py
backend/app/api/mcp_server.py
backend/app/api/memory_routes.py
backend/app/core/request_context.py
backend/app/eval/memory_retrieval.py
backend/app/repositories/ports.py
backend/app/repositories/sqlite/identity_store.py
backend/app/repositories/sqlite/memory_store.py
backend/app/repositories/sqlite/notebook_store.py
backend/app/repositories/sqlite/query_store.py
backend/app/repositories/sqlite/source_store.py
backend/app/services/batch_ingest.py
backend/app/services/extraction_profiles.py
backend/app/services/memory_retrieval.py
backend/app/services/memory_service.py
backend/app/services/notebook_catalog.py
backend/app/services/notebook_sharing.py
backend/app/services/parsers.py
backend/app/services/source_ingestion.py
```

Production code imports the owning domain module; tests/scripts may continue using `schemas.py` to prove compatibility.

- [ ] **Step 7: Run focused model, Memory, source, notebook, auth, and report tests**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n9 \
  backend/tests/test_model_domain_boundaries.py \
  backend/tests/test_memory_api.py \
  backend/tests/test_memory_service.py \
  backend/tests/test_auth.py \
  backend/tests/test_url_sources_schemas.py \
  backend/tests/test_notebook_store_component.py \
  backend/tests/test_report_api.py
```

Expected: PASS; existing `app.models.schemas` imports still work.

- [ ] **Step 8: Commit the first model boundaries**

```bash
git add backend/app/core/memory_inputs.py backend/app/models backend/app/api backend/app/core/request_context.py backend/app/eval backend/app/repositories backend/app/services backend/tests
git commit -m "refactor: establish core domain model boundaries"
```

### Task 4: Complete domain models and reduce `schemas.py` to a facade

**Files:**
- Create: `backend/app/models/ask.py`
- Create: `backend/app/models/knowledge.py`
- Modify: `backend/app/models/kg.py`
- Create: `backend/app/models/knowhow.py`
- Create: `backend/app/models/content_overview.py`
- Create: `backend/app/models/admin.py`
- Create: `backend/app/models/model_services.py`
- Modify: `backend/app/models/notebooks.py`
- Modify: `backend/app/models/schemas.py`
- Modify: all first-party production consumers listed in Step 5
- Modify: `backend/tests/test_model_domain_boundaries.py`
- Modify: `scripts/check_contracts.sh`

**Interfaces:**
- Consumes: the partial facade from Task 3.
- Produces: all 147 legacy names from `schemas.py` as object-identical re-exports; no first-party production module outside `models/schemas.py` imports the facade.

- [ ] **Step 1: Extend the architecture test to the complete target**

Replace `DOMAIN_MODULES` with:

```python
DOMAIN_MODULES = (
    "common",
    "identity",
    "memory",
    "sources",
    "notebooks",
    "reports",
    "ask",
    "knowledge",
    "kg",
    "knowhow",
    "content_overview",
    "admin",
    "model_services",
)
```

Add:

```python
def test_schema_facade_contains_no_model_definitions():
    tree = ast.parse((MODELS / "schemas.py").read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.ClassDef) for node in tree.body)


def test_first_party_production_uses_domain_model_modules():
    offenders = []
    app_root = ROOT / "backend" / "app"
    for path in app_root.rglob("*.py"):
        if path == MODELS / "schemas.py":
            continue
        if "app.models.schemas" in _imports(path):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
```

Run the file and expect FAIL for missing modules, remaining class definitions, and first-party facade imports.

- [ ] **Step 2: Move the remaining exact model groups**

```text
ask.py:
  RuleCard, CitationKnowhowRef, Citation, TraceStep, AskRequest,
  AnswerAnchor, ModelError, AskResponse, ConversationRenameRequest,
  ConversationSummary, ConversationTurn, ActiveAskJob, ConversationDetail,
  SearchHit, NotebookSearchResponse, KgSearchHit, KgSearchResponse,
  FeedbackRequest, FeedbackResponse

knowledge.py:
  KnowledgeUpdate, KnowledgeRef, KnowledgeFieldValue, KnowledgeRecord,
  KnowledgeTypeCount, PaginatedKnowledge, ObjectSchemaModel,
  ObjectSchemaCreate, ObjectSchemaUpdate, KnowledgeNode, KnowledgeEdge,
  KnowledgeGraph, EdgeReviewItem, EdgeReviewRequest, DuplicateGroup,
  MergeRequest

kg.py:
  UnifiedKgStatus, MergeReviewJob, ScaleIndexStatus,
  RebuildScaleIndexRequest, MergeReviewRequest, MergeReviewSummary,
  ConceptWhitelistEntry, ConceptWhitelistAdd

content_overview.py:
  MemoryOverviewItem, MemoryOverviewSummary, KnowhowOverviewTable,
  KnowhowOverviewSummary, NotebookContentOverview

admin.py:
  PromotionCandidate, PromotionApproveResult, PromotionRejectRequest,
  PromoteRequest, AdminUserUsage, AdminUserNotebook

model_services.py:
  ModelServiceView, ModelServiceUpdate, ModelSettingsUpdate,
  ModelTestRequest, ModelTestResult

knowhow.py:
  KnowhowColumn, KnowhowRow, KnowhowTableSummary, KnowhowTableDetail,
  KnowhowPreviewColumn, KnowhowImportPreview, KnowhowTablePatch,
  KnowhowColumnCreate, KnowhowColumnPatch, KnowhowRowCreate,
  KnowhowCellPatch, KnowhowCellPatchResult, KnowhowCellsBatchPatch,
  KnowhowNewColumnInput, KnowhowTableCreate,
  KnowhowAppendDuplicateTitle, KnowhowAppendPreview, KnowhowAppendResult,
  KnowhowCellOptimizeResult, KnowhowCellReformatResult,
  KnowhowAgentColumn, KnowhowAgentTable, KnowhowDiscriminationMethod,
  KnowhowDiscriminationRow, KnowhowDiscriminationSet, KnowhowRowCell,
  KnowhowRowCode, KnowhowRowDetail, KnowhowCellCodePut,
  KnowhowCellCodeResult, KnowhowTransferRequest
```

Preserve class bodies, Pydantic configuration, validators, comments that explain wire semantics, and inheritance. Keep the direct `notebooks -> kg.KgBuildJobStatus` import established in Task 3.

- [ ] **Step 3: Finish the explicit compatibility facade**

`schemas.py` contains only domain imports, aliases, `__all__`, and a short compatibility docstring. Verify every frozen name resolves and is defined outside `app.models.schemas`:

```python
def test_legacy_schema_exports_are_object_identical_domain_definitions():
    facade = importlib.import_module("app.models.schemas")
    assert sorted(facade.__all__) == LEGACY
    for name in LEGACY:
        value = getattr(facade, name)
        if isinstance(value, type):
            assert value.__module__ != "app.models.schemas", name
```

- [ ] **Step 4: Update the contract lane to compile every boundary module**

Add these paths to the existing `py_compile` invocation in `scripts/check_contracts.sh`:

```text
backend/app/models/common.py
backend/app/models/identity.py
backend/app/models/memory.py
backend/app/models/sources.py
backend/app/models/notebooks.py
backend/app/models/reports.py
backend/app/models/ask.py
backend/app/models/knowledge.py
backend/app/models/kg.py
backend/app/models/knowhow.py
backend/app/models/content_overview.py
backend/app/models/admin.py
backend/app/models/model_services.py
```

Keep `backend/app/models/schemas.py` in the compile list.

- [ ] **Step 5: Replace all remaining first-party facade imports**

Run `rg -l 'app\.models\.schemas' backend/app` and update every result except `backend/app/models/schemas.py`. At the planning baseline the complete set is:

```text
backend/app/api/auth_routes.py
backend/app/api/content_overview_routes.py
backend/app/api/debug_logs.py
backend/app/api/deps.py
backend/app/api/knowhow_agent_routes.py
backend/app/api/mcp_server.py
backend/app/api/memory_routes.py
backend/app/api/routes.py
backend/app/core/request_context.py
backend/app/eval/inference.py
backend/app/eval/memory_retrieval.py
backend/app/eval/speed.py
backend/app/repositories/ports.py
backend/app/repositories/sqlite/ask_state_store.py
backend/app/repositories/sqlite/identity_store.py
backend/app/repositories/sqlite/knowledge_store.py
backend/app/repositories/sqlite/memory_store.py
backend/app/repositories/sqlite/notebook_store.py
backend/app/repositories/sqlite/query_store.py
backend/app/repositories/sqlite/source_store.py
backend/app/services/ask_execution.py
backend/app/services/ask_service.py
backend/app/services/batch_ingest.py
backend/app/services/content_overview.py
backend/app/services/evidence_context.py
backend/app/services/extraction_profiles.py
backend/app/services/graph_retrieval.py
backend/app/services/knowhow/projection.py
backend/app/services/knowledge_governance.py
backend/app/services/knowledge_query.py
backend/app/services/memory_retrieval.py
backend/app/services/memory_service.py
backend/app/services/notebook_catalog.py
backend/app/services/notebook_sharing.py
backend/app/services/parsers.py
backend/app/services/reasoning_retrieval.py
backend/app/services/retrieval.py
backend/app/services/retrieval_candidates.py
backend/app/services/schema_registry.py
backend/app/services/source_ingestion.py
backend/app/services/sqlite_repository.py
```

The final `rg` output must contain only the facade itself plus compatibility-focused tests/scripts.

- [ ] **Step 6: Run model and full backend verification**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n9 \
  backend/tests/test_model_domain_boundaries.py \
  backend/tests/test_memory_api.py \
  backend/tests/test_ask_modes_api.py \
  backend/tests/test_knowledge_governance_boundaries.py \
  backend/tests/test_knowhow_api.py \
  backend/tests/test_content_overview_api.py \
  backend/tests/test_model_settings_api.py
```

Then run `scripts/check_backend.sh` with `PYTHON_BIN` and expect all backend tests to pass within 60 seconds.

- [ ] **Step 7: Commit the complete model boundary**

```bash
git add backend/app/models backend/app/api backend/app/core backend/app/eval backend/app/repositories backend/app/services backend/tests scripts/check_contracts.sh
git commit -m "refactor: split pydantic contracts by domain"
```

### Task 5: Split system, model-settings, and pending-center routes

**Files:**
- Create: `backend/app/api/system_routes.py`
- Create: `backend/tests/test_route_domain_boundaries.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/tests/test_model_settings_api.py`
- Modify: `backend/tests/test_model_provider_runtime.py`
- Modify: `backend/tests/test_reasoning_llm_config.py`
- Modify: `backend/tests/test_pending_actions_api.py`
- Modify: `backend/app/main.py` (comment/import references only; no lifespan changes)

**Interfaces:**
- Consumes: dependencies from `app.api.deps`, model-service and identity/source domain models.
- Produces: `system_routes.router`; endpoint names, paths, dependencies, sync/async form, and response models remain unchanged.

- [ ] **Step 1: Add the failing semantic route-ownership test**

```python
import ast
from pathlib import Path

from fastapi.routing import APIRoute

from app.main import app


ROOT = Path(__file__).resolve().parents[2]


def _endpoint_modules() -> dict[str, str]:
    return {
        route.name: route.endpoint.__module__
        for route in app.routes
        if isinstance(route, APIRoute)
    }


def test_system_endpoints_are_owned_by_the_system_router():
    modules = _endpoint_modules()
    for endpoint in (
        "health",
        "me",
        "get_model_settings",
        "put_model_settings",
        "test_model_service",
        "list_doc_types",
        "detect_doc_types",
        "list_notebook_templates",
        "me_pending_actions",
        "me_pending_stream",
    ):
        assert modules[endpoint] == "app.api.system_routes", endpoint


def test_domain_router_does_not_import_the_schema_facade():
    path = ROOT / "backend" / "app" / "api" / "system_routes.py"
    assert path.exists()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "app.models.schemas" not in modules
```

Run the file with `-n0`; expect FAIL because the module does not exist and endpoints still belong to `app.api.routes`.

- [ ] **Step 2: Move the exact system route set**

Move these functions and decorators unchanged to `system_routes.py`:

```text
health, me, _mask_key, get_model_settings, put_model_settings,
test_model_service, list_doc_types, detect_doc_types,
list_notebook_templates, me_pending_actions, me_pending_stream
```

The module starts with:

```python
from fastapi import APIRouter, Depends

from app.api.deps import (
    admin_query_repository,
    get_current_user,
    identity_repository,
    repository,
)
from app.models.identity import UserProfile
from app.models.model_services import ModelSettingsUpdate, ModelTestRequest, ModelTestResult
from app.models.notebooks import NotebookTemplate
from app.models.sources import DetectDocTypesRequest, DetectedDocType


router = APIRouter()
```

Import the existing service/config/pending symbols used by the moved bodies. Do not change synchronous functions to async or vice versa.

- [ ] **Step 3: Compose the router at the former first-system-route position**

In `routes.py` import `router as system_router` and include it immediately after `memory_router`. Remove the moved definitions and now-unused imports.

- [ ] **Step 4: Move tests to the stable domain seam**

Replace private monolith imports/patch targets as follows:

```text
test_model_settings_api.py       app.api.system_routes._mask_key / identity_repository
test_model_provider_runtime.py   app.api.system_routes
test_reasoning_llm_config.py     app.api.system_routes.health
test_pending_actions_api.py      app.api.system_routes.me_pending_stream
```

Use `TestClient` assertions when the test is checking HTTP behaviour; patch only model clients/repositories needed to isolate the scenario.

- [ ] **Step 5: Run focused and temporary-contract tests**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n0 \
  backend/tests/test_route_domain_boundaries.py \
  backend/tests/test_model_settings_api.py \
  backend/tests/test_pending_actions_api.py \
  backend/tests/test_application_boundary_contract.py
```

Expected: PASS; the temporary OpenAPI/collision fixture has no diff.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api backend/app/main.py backend/tests
git commit -m "refactor: split system routes"
```

### Task 6: Split notebook and source routes

**Files:**
- Create: `backend/app/api/notebook_routes.py`
- Create: `backend/app/api/source_routes.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/tests/test_route_domain_boundaries.py`
- Modify: `backend/tests/test_url_sources_api.py`
- Modify: source/notebook API tests selected in Step 5

**Interfaces:**
- Consumes: notebook/source models and existing repository/service dependencies.
- Produces: `notebook_routes.router` and `source_routes.router` with unchanged endpoint contracts.

- [ ] **Step 1: Extend route ownership tests and observe failure**

Add representative assertions:

```python
def test_notebook_and_source_endpoints_have_domain_owners():
    modules = _endpoint_modules()
    expected = {
        "list_notebooks": "app.api.notebook_routes",
        "create_notebook": "app.api.notebook_routes",
        "get_notebook": "app.api.notebook_routes",
        "set_notebook_tier": "app.api.notebook_routes",
        "share_notebook_route": "app.api.notebook_routes",
        "list_sources": "app.api.source_routes",
        "upload_sources": "app.api.source_routes",
        "get_source": "app.api.source_routes",
        "backfill_paper_metadata": "app.api.source_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint
```

Generalize the facade-import test over every existing `*_routes.py` target created by this plan. Run and expect FAIL before moving handlers.

- [ ] **Step 2: Move the exact notebook handler set**

```text
list_notebooks, shared_by_me_route, create_notebook, get_notebook,
notebook_analytics, update_notebook, delete_notebook, set_notebook_tier,
list_notebook_bases_route, set_notebook_bases_route,
mountable_notebooks_route, mounted_by_count_route, share_notebook_route,
unshare_notebook_route, shared_preview_route, copy_shared_route,
join_shared_route, leave_notebook_route
```

Keep each existing `Depends(require_notebook_read/access/write)` list and each direct `get_current_user` dependency in its current position.

- [ ] **Step 3: Move the exact source handler/helper set**

```text
SUPPORTED_SOURCE_SUFFIXES, MAX_SOURCE_UPLOAD_BYTES, _asset_service,
_validate_source_file, list_sources, import_sources, add_url_sources,
upload_sources, get_source, parse_source, source_elements, delete_source,
upload_notebook_asset, get_notebook_asset_file, backfill_paper_metadata
```

Preserve multipart/FormData handling, size validation, `UploadFile` async reads, scheduler submission, `FileResponse`, and source-access 404 behaviour.

- [ ] **Step 4: Compose both routers and remove obsolete imports**

Include `notebook_router` and `source_router` after `system_router`. Run the collision characterization immediately; if a static/dynamic pair changes relative order, use ordered subrouters at the aggregate boundary instead of weakening the test.

- [ ] **Step 5: Migrate patches and run focused suites**

In `test_url_sources_api.py`, patch `app.api.source_routes.repository` and `app.api.source_routes.kg_scheduler`; keep `app.api.deps.repository` patches that explicitly exercise dependency resolution.

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n9 \
  backend/tests/test_route_domain_boundaries.py \
  backend/tests/test_notebook_owner_scope.py \
  backend/tests/test_notebook_share_copy.py \
  backend/tests/test_notebook_share_readonly.py \
  backend/tests/test_sources_pagination.py \
  backend/tests/test_url_sources_api.py \
  backend/tests/test_notebook_assets.py \
  backend/tests/test_paper_meta_api.py \
  backend/tests/test_application_boundary_contract.py
```

Expected: PASS with identical temporary API/collision snapshot.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api backend/tests
git commit -m "refactor: split notebook and source routes"
```

### Task 7: Split Knowhow and knowledge-governance routes

**Files:**
- Create: `backend/app/api/knowhow_routes.py`
- Create: `backend/app/api/knowledge_routes.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/tests/test_route_domain_boundaries.py`
- Modify: Knowhow/knowledge tests with private route patches

**Interfaces:**
- Consumes: `app.services.knowhow.api`, `AssetService`, knowledge services/repositories, background scheduling.
- Produces: session Knowhow and knowledge-governance routers; Agent Knowhow remains in `knowhow_agent_routes.py`.

- [ ] **Step 1: Add failing representative ownership assertions**

```python
def test_knowhow_and_knowledge_endpoints_have_domain_owners():
    modules = _endpoint_modules()
    expected = {
        "preview_knowhow_import": "app.api.knowhow_routes",
        "patch_knowhow_cell": "app.api.knowhow_routes",
        "reformat_knowhow_cell": "app.api.knowhow_routes",
        "transfer_knowhow_table": "app.api.knowhow_routes",
        "knowledge_types": "app.api.knowledge_routes",
        "list_knowledge": "app.api.knowledge_routes",
        "merge_knowledge": "app.api.knowledge_routes",
        "edge_review_queue": "app.api.knowledge_routes",
        "review_relation": "app.api.knowledge_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint
```

- [ ] **Step 2: Move the exact Knowhow set**

Move all handler bodies from `preview_knowhow_import` through `reformat_knowhow_cell`, plus `transfer_knowhow_table`, and their private helpers:

```text
preview_knowhow_import, import_knowhow_table, list_knowhow_tables,
get_knowhow_table, delete_knowhow_table, reproject_knowhow_table,
_require_table, _require_column_in_table, _require_row_in_table,
create_knowhow_table, patch_knowhow_table, add_knowhow_column,
patch_knowhow_column, delete_knowhow_column, add_knowhow_row,
delete_knowhow_row, patch_knowhow_cell, patch_knowhow_cells_batch,
_template_content_disposition, download_knowhow_template,
append_knowhow_rows, optimize_knowhow_cell, reformat_knowhow_cell,
transfer_knowhow_table
```

Do not change projection scheduling, guarded batch transactions, upload/save mutual exclusion, or error translation.

- [ ] **Step 3: Move the exact knowledge set**

```text
knowledge_types, list_knowledge, list_object_schemas,
create_object_schema, update_object_schema, delete_object_schema,
propose_schemas, update_knowledge, find_duplicates, knowledge_graph,
merge_knowledge, edge_review_queue, review_relation
```

- [ ] **Step 4: Migrate private patches to the owning module**

Update these tests when they patch route-local `repository` or helpers:

```text
test_knowhow_asset_gc_trigger.py  -> app.api.knowhow_routes
test_knowhow_retrieval.py         -> app.api.knowhow_routes
test_knowhow_code_isolation.py    -> app.api.knowhow_routes
test_knowhow_pr23_integration.py  -> app.api.knowhow_routes
test_edge_review_queue.py         -> app.api.knowledge_routes
```

Prefer public `TestClient` assertions and service seams; do not add re-exports to `routes.py` solely to keep a private patch alive.

- [ ] **Step 5: Run focused and contract tests**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n9 \
  backend/tests/test_route_domain_boundaries.py \
  backend/tests/test_knowhow_api.py \
  backend/tests/test_knowhow_editing_api.py \
  backend/tests/test_knowhow_reformat.py \
  backend/tests/test_knowhow_transfer_routes.py \
  backend/tests/test_knowledge_governance_boundaries.py \
  backend/tests/test_edge_review_queue.py \
  backend/tests/test_application_boundary_contract.py
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/api backend/tests
git commit -m "refactor: split knowhow and knowledge routes"
```

### Task 8: Split Ask and report routes

**Files:**
- Create: `backend/app/api/ask_routes.py`
- Create: `backend/app/api/report_routes.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/tests/test_route_domain_boundaries.py`
- Modify: Ask/report tests with private route imports

**Interfaces:**
- Consumes: Ask execution coordinator/stream port/cancellation and report background-job services.
- Produces: unchanged JSON/NDJSON/report contracts and cancellation behaviour.

- [ ] **Step 1: Add failing representative ownership assertions**

```python
def test_ask_and_report_endpoints_have_domain_owners():
    modules = _endpoint_modules()
    expected = {
        "search_notebook": "app.api.ask_routes",
        "ask": "app.api.ask_routes",
        "ask_stream": "app.api.ask_routes",
        "cancel_ask_job": "app.api.ask_routes",
        "list_conversations": "app.api.ask_routes",
        "submit_feedback": "app.api.ask_routes",
        "create_report": "app.api.report_routes",
        "export_reports_endpoint": "app.api.report_routes",
        "generate_report": "app.api.report_routes",
        "cancel_report_endpoint": "app.api.report_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint
```

- [ ] **Step 2: Move the exact Ask set**

```text
search_notebook, ask, ask_modes, _ndjson_line, _stream_ask_events,
ask_stream, cancel_ask_job, get_ask_job, list_conversations,
get_conversation, rename_conversation, delete_conversation,
bulk_delete_conversations, submit_feedback
```

Keep `_stream_ask_events` async, preserve detached-worker semantics on disconnect, and keep explicit cancellation bound only to the cancel endpoint.

- [ ] **Step 3: Move the exact report set**

```text
_report_llm_ready, _launch_plan_job, _launch_generate_job,
create_report, list_reports, export_reports_endpoint, get_report,
update_report_outline, generate_report, cancel_report_endpoint,
delete_report
```

- [ ] **Step 4: Migrate private imports/patches**

```text
test_report_api.py              -> app.api.report_routes
test_report_execution.py        -> app.api.report_routes
test_ask_service_boundary.py    -> app.api.ask_routes._stream_ask_events
test_ask_stream_cancel.py       -> app.api.ask_routes._stream_ask_events
test_reasoning_stream.py        -> app.api.ask_routes
test_ask_modes_api.py           -> app.api.ask_routes
test_ask_jobs.py                -> app.api.ask_routes
test_ask_reconnect.py           -> app.api.ask_routes
test_conversations.py           -> app.api.ask_routes
```

- [ ] **Step 5: Run focused behavioural and temporary-contract tests**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n9 \
  backend/tests/test_route_domain_boundaries.py \
  backend/tests/test_ask_modes_api.py \
  backend/tests/test_ask_stream_cancel.py \
  backend/tests/test_ask_reconnect.py \
  backend/tests/test_conversations.py \
  backend/tests/test_report_api.py \
  backend/tests/test_report_execution.py \
  backend/tests/test_application_boundary_contract.py
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/api backend/tests
git commit -m "refactor: split ask and report routes"
```

### Task 9: Split KG/admin routes and finish the aggregate router

**Files:**
- Create: `backend/app/api/kg_routes.py`
- Create: `backend/app/api/admin_routes.py`
- Modify: `backend/app/api/routes.py`
- Modify: `backend/app/main.py`
- Modify: `backend/tests/test_route_domain_boundaries.py`
- Modify: tests/scripts with remaining private monolith imports
- Modify: `scripts/check_contracts.sh`

**Interfaces:**
- Consumes: all domain routers from Tasks 5-8 plus the existing `memory_router`.
- Produces: a composition-only `app.api.routes.router`; no decorated endpoint functions remain in the aggregate.

- [ ] **Step 1: Add final failing architecture assertions**

```python
def test_kg_and_admin_endpoints_have_domain_owners():
    modules = _endpoint_modules()
    expected = {
        "kg_search": "app.api.kg_routes",
        "build_kg": "app.api.kg_routes",
        "rebuild_unified_kg": "app.api.kg_routes",
        "get_unified_kg": "app.api.kg_routes",
        "resolve_conflicts": "app.api.kg_routes",
        "review_unified_kg_merges": "app.api.kg_routes",
        "propose_promotion": "app.api.admin_routes",
        "approve_promotion": "app.api.admin_routes",
        "list_admin_users": "app.api.admin_routes",
        "list_online_users": "app.api.admin_routes",
    }
    for endpoint, module in expected.items():
        assert modules[endpoint] == module, endpoint


def test_aggregate_routes_module_is_composition_only():
    path = ROOT / "backend" / "app" / "api" / "routes.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    decorated_functions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.decorator_list
    ]
    assert decorated_functions == []
```

Run and expect FAIL before the last handlers move.

- [ ] **Step 2: Move the exact KG set**

```text
kg_search, build_kg, rebuild_kg, relink_kg, rebuild_unified_kg,
unified_kg_status, rebuild_scale_index, cancel_scale_index,
scale_index_status, index_status, get_unified_kg, get_pending_merges,
get_concept_detail, object_context, object_neighbors, confirm_merge,
reject_merge, resolve_conflicts, get_pending_conflicts,
confirm_conflict, reject_conflict, list_concept_whitelist,
add_concept_whitelist, delete_concept_whitelist,
review_unified_kg_merges, review_all_unified_kg_merges,
merge_review_job
```

Keep `KnowledgeGraphTooLargeError`, scheduler/job state, repository cancellation, and read/write dependency boundaries unchanged.

- [ ] **Step 3: Move the exact admin set**

```text
propose_promotion, list_promotion_queue, approve_promotion,
reject_promotion, list_admin_users, list_admin_user_notebooks,
list_online_users
```

Preserve admin checks, reviewer identity, complete `base_object_ids`, and async offloading in admin list endpoints.

- [ ] **Step 4: Reduce `routes.py` to explicit composition**

The aggregate has this responsibility and order, adjusted only if the committed collision test requires ordered subrouters:

```python
from fastapi import APIRouter

from app.api.admin_routes import router as admin_router
from app.api.ask_routes import router as ask_router
from app.api.kg_routes import router as kg_router
from app.api.knowhow_routes import router as knowhow_router
from app.api.knowledge_routes import router as knowledge_router
from app.api.memory_routes import memory_router
from app.api.notebook_routes import router as notebook_router
from app.api.report_routes import router as report_router
from app.api.source_routes import router as source_router
from app.api.system_routes import router as system_router


router = APIRouter()
for domain_router in (
    memory_router,
    system_router,
    notebook_router,
    source_router,
    knowhow_router,
    knowledge_router,
    ask_router,
    report_router,
    kg_router,
    admin_router,
):
    router.include_router(domain_router)
```

Do not re-export endpoint helpers for tests. `main.py` continues to include this aggregate once with the blanket authenticated dependency.

- [ ] **Step 5: Migrate every remaining monolith-specific patch/import**

Use these owners:

```text
test_conflict_endpoints.py               app.api.kg_routes
test_legacy_graph_guard.py               app.api.kg_routes
test_trackF_governance_promotion.py      app.api.admin_routes
test_repository_api_contract.py          app.api.ask_routes
scripts/smoke_backend.py                 app.api.deps.repository
```

Then run:

```bash
rg -n 'app\.api\.routes|from app\.api import routes' backend/tests scripts
```

Every remaining match must either import only the public aggregate `router` for composition testing or be migrated to a domain/public HTTP seam. Update stale comments in `main.py`, `knowhow_agent_routes.py`, and tests that still describe `routes.py` as the implementation owner.

- [ ] **Step 6: Compile all new router modules in the contract lane**

Add the nine new `backend/app/api/*_routes.py` paths to `scripts/check_contracts.sh` while retaining existing auth/Memory/Agent/debug modules.

- [ ] **Step 7: Run route, API, and full backend tests**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n9 \
  backend/tests/test_route_domain_boundaries.py \
  backend/tests/test_conflict_endpoints.py \
  backend/tests/test_kg_rebuild_relink_api.py \
  backend/tests/test_trackF_governance_promotion.py \
  backend/tests/test_admin_users.py \
  backend/tests/test_application_boundary_contract.py
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check_backend.sh
```

Expected: PASS, backend lane at most 60 seconds, no temporary contract difference.

- [ ] **Step 8: Commit**

```bash
git add backend/app/api backend/app/main.py backend/tests scripts/check_contracts.sh scripts/smoke_backend.py
git commit -m "refactor: complete fastapi domain routers"
```

### Task 10: Build the shared frontend transport core

**Files:**
- Create: `frontend/app/api-config.ts`
- Create: `frontend/app/auth-session.ts`
- Create: `frontend/app/api-client.ts`
- Create: `frontend/app/api-client.test.mjs`
- Modify: `frontend/app/auth.ts`
- Modify: `frontend/app/auth.test.mjs`
- Modify: `frontend/app/errors.test.mjs`

**Interfaces:**
- Produces: `performApiRequest`, `requestJson<T>`, `requestVoid`, `requestBlob`, `API_BASE`, and token/session helpers.
- Consumes: `throwHumanizedHttpError` and the current trusted-error policy from `errors.ts`.

- [ ] **Step 1: Write failing transport tests**

Cover auth, FormData, 204, Blob, AbortSignal, trusted errors, custom/raw policy, and 401 cleanup. The central test harness installs a fake `window.localStorage`, fake `window.location.reload`, and a `globalThis.fetch` spy. Representative assertions:

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import { clearToken, setToken } from "./auth-session.ts";
import { performApiRequest, requestBlob, requestJson, requestVoid } from "./api-client.ts";

const storage = new Map();
let reloads = 0;
globalThis.window = {
  localStorage: {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  },
  location: { reload: () => { reloads += 1; } },
};

test("requestJson applies bearer and JSON headers and parses the body", async () => {
  setToken("tok-1");
  let captured;
  globalThis.fetch = async (url, init) => {
    captured = { url, init };
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  assert.deepEqual(await requestJson("/health", { tag: "test" }), { ok: true });
  assert.equal(captured.init.headers.get("Authorization"), "Bearer tok-1");
});

test("requestVoid accepts 204 without parsing JSON", async () => {
  globalThis.fetch = async () => new Response(null, { status: 204 });
  await requestVoid("/notebooks/nb/share", { method: "DELETE", tag: "test" });
});

test("performApiRequest exposes a reviewed raw response for special policy", async () => {
  globalThis.fetch = async () => new Response("forbidden", { status: 403 });
  const response = await performApiRequest("/admin/users", { tag: "admin" });
  assert.equal(response.status, 403);
});
```

Also assert that `requestJson` calls the shared error layer for a marked 4xx, `requestBlob` returns the exact Blob, caller signals reach fetch, unauthenticated requests omit bearer, the default 401 policy preserves the session, and `unauthorized: "clear-and-reload"` clears a stored token before reload.

Run:

```bash
cd frontend && node --test app/api-client.test.mjs
```

Expected: FAIL because the modules do not exist.

- [ ] **Step 2: Split config and session storage out of `auth.ts`**

`api-config.ts`:

```typescript
export const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";
```

`auth-session.ts` owns `TOKEN_KEY`, `getToken`, `setToken`, `clearToken`, and `authHeaders` with the existing SSR/localStorage behaviour.

`auth.ts` re-exports those names and `API_BASE` for compatibility, but imports transport from `api-client.ts`; `api-client.ts` imports only `api-config.ts` and `auth-session.ts`, so there is no cycle.

- [ ] **Step 3: Implement the transport core**

Use these exact public option types and function responsibilities:

```typescript
import { API_BASE } from "./api-config.ts";
import { authHeaders, clearToken, getToken } from "./auth-session.ts";
import { throwHumanizedHttpError } from "./errors.ts";

export type ApiAuth = "required" | "none";

export type ApiRequestOptions = RequestInit & {
  auth?: ApiAuth;
  tag: string;
  unauthorized?: "preserve" | "clear-and-reload";
};

function resolveApiUrl(pathOrUrl: string): string {
  if (pathOrUrl.startsWith("/")) return `${API_BASE}${pathOrUrl}`;
  if (pathOrUrl === API_BASE || pathOrUrl.startsWith(`${API_BASE}/`)) return pathOrUrl;
  throw new TypeError("authenticated API requests must stay under API_BASE");
}

export async function performApiRequest(
  pathOrUrl: string,
  options: ApiRequestOptions,
): Promise<Response> {
  const {
    auth = "required",
    tag,
    unauthorized = "preserve",
    headers: inputHeaders,
    ...init
  } = options;
  const headers = new Headers(inputHeaders);
  if (auth === "required") {
    for (const [name, value] of Object.entries(authHeaders())) headers.set(name, value);
  }
  const isFormData = typeof FormData !== "undefined" && init.body instanceof FormData;
  if (init.body !== undefined && !isFormData && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const started = performance.now();
  const response = await fetch(resolveApiUrl(pathOrUrl), { ...init, headers });
  const elapsed = Math.round(performance.now() - started);
  const requestId = response.headers.get("X-Request-Id") || "";
  console.debug(`[api] ${(init.method || "GET").toUpperCase()} ${pathOrUrl} -> ${response.status} ${elapsed}ms${requestId ? ` (${requestId})` : ""}`);
  if (
    auth === "required"
    && unauthorized === "clear-and-reload"
    && response.status === 401
    && getToken()
  ) {
    clearToken();
    if (typeof window !== "undefined") window.location.reload();
  }
  void tag;
  return response;
}

async function checked(path: string, options: ApiRequestOptions): Promise<Response> {
  const response = await performApiRequest(path, options);
  if (!response.ok) await throwHumanizedHttpError(response, options.tag);
  return response;
}

export async function requestJson<T>(path: string, options: ApiRequestOptions): Promise<T> {
  const response = await checked(path, options);
  return response.json() as Promise<T>;
}

export async function requestVoid(path: string, options: ApiRequestOptions): Promise<void> {
  await checked(path, options);
}

export async function requestBlob(path: string, options: ApiRequestOptions): Promise<Blob> {
  return (await checked(path, options)).blob();
}
```

Use `globalThis.performance.now()` when accessing the timer and keep the explicit `typeof FormData` guard shown above. Do not swallow fetch rejection.

- [ ] **Step 4: Migrate auth policy onto the core**

- Register/login call `performApiRequest` with `auth: "none"`, retain the special login-401 wording via `readHttpError`/`humanizeHttpError`.
- Logout uses `performApiRequest` but remains fail-open, then always clears the token.
- `fetchMe` uses `requestJson<AuthUser>`.

Keep all exported signatures unchanged.

- [ ] **Step 5: Run transport, auth, and error tests plus type checking**

```bash
cd frontend && node --test app/api-client.test.mjs app/auth.test.mjs app/errors.test.mjs
cd frontend && npm run lint
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/api-config.ts frontend/app/auth-session.ts frontend/app/api-client.ts frontend/app/api-client.test.mjs frontend/app/auth.ts frontend/app/auth.test.mjs frontend/app/errors.test.mjs
git commit -m "refactor: add the shared frontend api client"
```

### Task 11: Migrate existing frontend domain clients and special transports

**Files:**
- Modify: `frontend/app/notebook-bases.ts`
- Modify: `frontend/app/notebook-share.ts`
- Modify: `frontend/app/notebook-tier.ts`
- Modify: `frontend/app/knowhow-model.ts`
- Modify: `frontend/app/knowhow-transfer.ts`
- Modify: `frontend/app/memory-transfer.ts`
- Modify: `frontend/app/edge-review-queue.ts`
- Modify: `frontend/app/promotion-queue.ts`
- Modify: `frontend/app/model-settings.ts`
- Modify: `frontend/app/memory-panel.tsx`
- Modify: `frontend/app/transfer-picker.tsx`
- Modify: `frontend/app/pending-center.tsx`
- Modify: `frontend/app/knowhow-cell-editor.tsx`
- Modify: `frontend/app/knowhow-panel.tsx`
- Modify: `frontend/app/admin/usage/api.ts`
- Modify: `frontend/app/admin/usage/notebooks.ts`
- Modify: `frontend/app/dev/logs/api.ts`

**Interfaces:**
- Consumes: `requestJson`, `requestVoid`, `requestBlob`, and the reviewed `performApiRequest` seam from Task 10.
- Produces: the same exported domain function signatures with no module-local fetch wrapper.

- [ ] **Step 1: Characterize special policies before replacing transport**

Extend existing tests so they fail if migration flattens these behaviours:

```text
knowhow-transfer.test.mjs:
  409 source_cleanup_failed/source_changed_kept still becomes
  KnowhowSourceCleanupError after inspecting a cloned body.

errors.test.mjs:
  admin 403 still records diagnostics and throws FORBIDDEN_SENTINEL;
  trusted user errors still use the shared humanized layer.

ask/pending tests:
  stream AbortSignal is forwarded and disconnect remains retry/fail-open policy.

source-image.test.mjs / knowhow image tests:
  internal assets use bearer-authenticated Blob requests and external images
  never receive a bearer token.
```

Run the affected existing tests and verify they pass before migration; these are characterization tests for already-working behaviour.

- [ ] **Step 2: Replace ordinary local wrappers with checked client calls**

For ordinary JSON clients, replace each private `apiFetch` body with direct domain functions such as:

```typescript
export const listBases = (notebookId: string): Promise<MountedBase[]> =>
  requestJson(`/notebooks/${notebookId}/bases`, { tag: "bases" });

export const setBases = (
  notebookId: string,
  baseNotebookIds: string[],
): Promise<MountedBase[]> =>
  requestJson(`/notebooks/${notebookId}/bases`, {
    method: "PUT",
    body: JSON.stringify({ base_notebook_ids: baseNotebookIds }),
    tag: "bases",
  });

export const unshareNotebook = (notebookId: string): Promise<void> =>
  requestVoid(`/notebooks/${notebookId}/share`, {
    method: "DELETE",
    tag: "share",
  });
```

Apply the same pattern to tier, edge review, promotion, Memory transfer, model settings, Knowhow, debug logs, and normal admin responses. Preserve each existing error tag.

- [ ] **Step 3: Preserve Knowhow transfer's structured 409 policy**

Use `performApiRequest` once, inspect `response.clone()` for the two cleanup shapes, then call `throwHumanizedHttpError` for every other failure. Do not make the shared client Knowhow-aware:

```typescript
const response = await performApiRequest(path, {
  ...init,
  tag: "knowhow-transfer",
});
if (!response.ok) {
  let parsed: unknown;
  try {
    parsed = JSON.parse(await response.clone().text());
  } catch {
    parsed = undefined;
  }
  const cleanup = parseCleanupFailure(response.status, parsed);
  if (cleanup) throw new KnowhowSourceCleanupError(cleanup.newTableId, cleanup.message);
  await throwHumanizedHttpError(response, "knowhow-transfer");
}
return response.json() as Promise<T>;
```

- [ ] **Step 4: Preserve admin sentinel and stream/fail-open policies**

- Admin modules use `performApiRequest`; a 403 still runs `readHttpError` and throws `FORBIDDEN_SENTINEL`; other failures use `throwHumanizedHttpError`.
- `pending-center.tsx` uses `performApiRequest` for snapshot/SSE so it can retain retry/backoff and body streaming without forcing a thrown HTTP policy.
- `memory-panel.tsx` may keep a small `memoryApi<T>` endpoint adapter, but it must delegate to `requestJson`/`requestVoid` with `unauthorized: "clear-and-reload"` and retain its existing policy.
- `transfer-picker.tsx` delegates its abortable list call to `requestJson` and keeps `AbortError` silent.

- [ ] **Step 5: Route authenticated internal images through `requestBlob`**

`requestBlob` accepts only a relative API path or a URL under `API_BASE`. Use it for source/Knowhow internal assets with the current cancellation/alive guards. External image URLs continue through `<img src>` and never enter the authenticated client.

- [ ] **Step 6: Run existing domain and component tests**

```bash
cd frontend && node --test \
  app/errors.test.mjs \
  app/notebook-bases.test.mjs \
  app/notebook-share.test.mjs \
  app/notebook-tier.test.mjs \
  app/edge-review-queue.test.mjs \
  app/promotion-queue.test.mjs \
  app/knowhow-transfer.test.mjs \
  app/source-image.test.mjs
cd frontend && npm run test:component
cd frontend && npm run lint
```

Expected: PASS, with no exported domain API signature changes.

- [ ] **Step 7: Confirm only the page-level migration remains**

Run:

```bash
rg -n '\bfetch\s*\(' frontend/app --glob '*.{ts,tsx}' --glob '!*.test.*' --glob '!api-client.ts'
rg -n 'function apiFetch|const apiFetch' frontend/app --glob '*.{ts,tsx}' --glob '!*.test.*'
```

Expected: no local `apiFetch`; any remaining direct fetches are only the page-level/system/Ask/asset calls assigned to Task 12. Do not add a permanent allowlist for those transitional matches.

- [ ] **Step 8: Commit**

```bash
git add frontend/app
git commit -m "refactor: migrate domain clients to shared transport"
```

### Task 12: Extract page endpoint ownership and eliminate direct production fetches

**Files:**
- Create: `frontend/app/system-api.ts`
- Create: `frontend/app/notebook-api.ts`
- Create: `frontend/app/source-api.ts`
- Create: `frontend/app/ask-api.ts`
- Create: `frontend/app/knowledge-api.ts`
- Create: `frontend/app/report-api.ts`
- Create: `frontend/app/kg-api.ts`
- Create: `frontend/app/api-boundary.test.mjs`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/app/errors-guard.test.mjs`
- Modify: domain tests related to Ask/report/KG/source behaviour

**Interfaces:**
- Consumes: workspace view types and transport core.
- Produces: endpoint functions by domain; `page.tsx` remains state/orchestration/UI only.

- [ ] **Step 1: Add a failing semantic transport-boundary test**

Use the existing TypeScript semantic scanner rather than line/snippet matching:

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import { appSourceModules, callsIn } from "./test/semantic-source.mjs";

test("production HTTP calls are owned by api-client", async () => {
  const offenders = [];
  for (const { path, module } of await appSourceModules()) {
    if (path === "api-client.ts") continue;
    const direct = callsIn(module).filter((target) => target === "fetch" || target === "globalThis.fetch");
    if (direct.length > 0) offenders.push({ path, direct });
  }
  assert.deepEqual(offenders, []);
});
```

Run it and expect FAIL while `page.tsx` still owns direct fetches. The test is position-independent and does not assert a source line, file length, function position, or endpoint count.

- [ ] **Step 2: Extract system and notebook APIs**

Move these endpoint responsibilities out of `page.tsx`:

```text
system-api.ts:
  ReadySnapshot, probeReady (raw/fail-open, auth none), fetchHealth

notebook-api.ts:
  listNotebooks, createNotebook, getNotebook, updateNotebook,
  deleteNotebook, fetchNotebookAnalytics, fetchNotebookContentOverview
```

Every function formerly reached through page's `api<T>` passes `unauthorized: "clear-and-reload"` so page behaviour stays unchanged. `probeReady` remains unauthenticated, no-store, parse-failure-safe, and never throws into the UI.

- [ ] **Step 3: Extract source and authenticated-asset APIs**

```text
source-api.ts:
  listSources, uploadSources, getSource, getSourceElements,
  parseSource, deleteSource, fetchInternalAssetBlob
```

Keep multipart header inference, pagination query parameters, and Blob URL lifecycle in the React component. The domain API returns the Blob; the component owns `URL.createObjectURL`/`revokeObjectURL`.

- [ ] **Step 4: Extract Ask and conversation APIs with NDJSON ownership**

Move `readAskStream` into `ask-api.ts` alongside:

```text
searchNotebook, runAskStream, cancelAskJob, getAskJob,
listConversations, getConversation, renameConversation,
deleteConversation, bulkDeleteConversations, submitFeedback
```

Reuse `AskStreamEvent` and `takeNdjsonLines` from `ask-stream.ts`. `runAskStream` uses `performApiRequest` for the streaming response, preserves progress paint yielding, branded safe errors, detached-worker disconnect semantics, and explicit-cancel-only behaviour.

- [ ] **Step 5: Extract knowledge, report, and KG APIs**

```text
knowledge-api.ts:
  listKnowledge, listKnowledgeTypes, updateKnowledge, findDuplicates,
  mergeKnowledge, listObjectSchemas, createObjectSchema,
  updateObjectSchema, deleteObjectSchema, proposeObjectSchemas,
  getKnowledgeGraph

report-api.ts:
  createReport, listReports, getReport, cancelReport, deleteReport,
  updateReportOutline, generateReport, downloadReportsZip

kg-api.ts:
  rebuildUnifiedKg, buildKg, rebuildKg, relinkKg,
  rebuildScaleIndex, cancelScaleIndex, fetchScaleIndexStatus,
  fetchIndexStatus, fetchUnifiedGraph, fetchKgSearch,
  fetchKgNeighbors, fetchConceptDetail, fetchNodeContext,
  fetchPendingMerges, fetchUnifiedKgStatus, confirmMerge,
  rejectMerge, reviewMerges, reviewAllMerges, fetchMergeReviewJob
```

Move the wire response types currently local to the page into the owning API module or reuse `workspace-model.ts`/`kg-merge-model.ts`; do not duplicate a type in both locations. `downloadReportsZip` retains the exact `reports.zip` name and DOM download behaviour.

- [ ] **Step 6: Replace page helpers with imports and preserve state orchestration**

Delete the generic `api<T>`, page-local endpoint functions, and direct fetches. Keep all React state, effects, epochs, stale-result checks, polling intervals, and UI callbacks in `page.tsx`; only replace their I/O calls with typed domain imports.

Update `errors-guard.test.mjs` semantic scope entries whose functions moved from `page.tsx` to `ask-api.ts`/`system-api.ts`; remove obsolete entries rather than duplicating both old and new paths.

- [ ] **Step 7: Run focused frontend tests and semantic guard**

```bash
cd frontend && node --test \
  app/api-client.test.mjs \
  app/api-boundary.test.mjs \
  app/ask-stream.test.mjs \
  app/ask-reconnect.test.mjs \
  app/errors-guard.test.mjs \
  app/workspace-transitions.test.mjs \
  app/source-image.test.mjs
cd frontend && npm run test:component
cd frontend && npm run lint
cd frontend && npm run build
```

Expected: PASS; semantic guard finds no direct production fetch outside `api-client.ts`.

- [ ] **Step 8: Commit**

```bash
git add frontend/app
git commit -m "refactor: split frontend domain api clients"
```

### Task 13: Prove equivalence, then optimize migration-time tests

**Files:**
- Modify: backend tests that still import/patch `app.api.routes`
- Modify: `backend/tests/test_route_domain_boundaries.py`
- Modify: `backend/tests/test_model_domain_boundaries.py`
- Modify: `frontend/app/api-client.test.mjs`
- Modify: `frontend/app/api-boundary.test.mjs`
- Delete: `backend/tests/application_boundary_snapshot.py`
- Delete: `backend/tests/fixtures/application_boundary_contract.json`
- Delete: `backend/tests/test_application_boundary_contract.py`
- Create temporarily outside Git: `/tmp/application-boundary-test-audit.md`

**Interfaces:**
- Consumes: migrated application plus the temporary baseline fixture.
- Produces: documented API equivalence, durable behavioural/architecture tests, no temporary golden, and a test audit visible in the PR body.

- [ ] **Step 1: Prove the temporary complete contract before deleting anything**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n0 \
  backend/tests/test_application_boundary_contract.py \
  backend/tests/test_route_domain_boundaries.py \
  backend/tests/test_model_domain_boundaries.py
```

Expected: PASS with byte-equivalent normalized OpenAPI and collision ordering. Save the command, exit code, and commit SHA for the PR body.

- [ ] **Step 2: Remove all remaining private-monolith test coupling**

Run:

```bash
rg -n 'app\.api\.routes|from app\.api import routes' backend/tests scripts
```

For each match:

- HTTP behaviour tests use `TestClient` and public status/response assertions.
- Isolation patches target the owning domain router's imported service/repository seam.
- Composition tests may import `app.api.routes.router`, but no test imports an endpoint/helper/repository from the aggregate.

Do not add compatibility re-exports for private test convenience.

- [ ] **Step 3: Build the post-equivalence test audit**

Write `/tmp/application-boundary-test-audit.md` with this table and fill every migration-time file with a concrete retained mapping:

```markdown
| Test or fixture | Classification | Final action | Retained behaviour/architecture coverage |
| --- | --- | --- | --- |
| backend/tests/test_application_boundary_contract.py | temporary migration evidence | delete after equivalence | PR evidence + focused public API tests |
| backend/tests/application_boundary_snapshot.py | temporary migration helper | delete after equivalence | no runtime responsibility |
| backend/tests/fixtures/application_boundary_contract.json | temporary golden | delete after equivalence | no permanent golden; route/model semantic tests remain |
| backend/tests/test_route_domain_boundaries.py | durable architecture | keep | domain endpoint ownership, composition-only aggregate, no facade import |
| backend/tests/test_model_domain_boundaries.py | durable compatibility/architecture | keep | frozen legacy imports and dependency direction |
| frontend/app/api-client.test.mjs | durable transport behaviour | keep | auth, errors, body types, AbortSignal, raw seam |
| frontend/app/api-boundary.test.mjs | durable architecture | keep | semantic single transport boundary |
```

Add a row for every existing test modified only to change a patch/import seam. State whether it was retained unchanged in meaning, consolidated, or deleted. No row may say only “covered elsewhere”; name the retained test and behaviour.

- [ ] **Step 4: Delete the temporary OpenAPI/collision harness**

Delete the three temporary files only after Step 1 is green. Remove their references from test discovery/commands. Keep the PR evidence and audit mapping.

- [ ] **Step 5: Consolidate only proven duplicate or implementation-coupled migration tests**

Apply these rules:

- Keep representative domain ownership tests; do not enumerate all 126 routes or assert counts.
- Keep the frozen legacy facade list; new domain models must not require fixture edits.
- Combine repeated AST file reads behind a module-level cached helper, while preserving every assertion.
- Remove old-module patch assertions after the same test exercises the public route or owning domain seam.
- Do not remove permission, 401/403/404, streaming, cancellation, trusted-error, Blob, or build coverage.

Update the audit table for every consolidation.

- [ ] **Step 6: Measure the resulting gate and profile only the slow lane if needed**

Run once:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

If every lane is at most 60 seconds, continue. If one exceeds 60:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -p no:cacheprovider -n 9 backend/tests --durations=50 --durations-min=0.2
```

For a backend overrun, first cache repeated AST/import-graph construction inside the new architecture tests and remove repeated app construction introduced by this PR. For a frontend overrun, inspect Node/Vitest timing and confirm the production build is invoked only once by `check_frontend.sh`. If host contention remains the cause, benchmark the three bounded commands `BACKEND_PYTEST_WORKERS=8 scripts/check.sh`, `BACKEND_PYTEST_WORKERS=9 scripts/check.sh`, and `BACKEND_PYTEST_WORKERS=10 scripts/check.sh`; update the default and its contract test only when two consecutive runs show the chosen value below 60 with the best margin. Never add ignores, skips, xfails, or remove assertions.

- [ ] **Step 7: Run two consecutive final warm gates**

Run twice without overrides:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Expected: both runs PASS and each reported lane is at most 60 seconds. Record both triplets in the audit/PR evidence.

- [ ] **Step 8: Commit equivalence-driven test cleanup**

```bash
git add backend/tests frontend/app scripts
git commit -m "test: retain durable application boundary coverage"
```

### Task 14: Synchronize architecture and historical-debt documentation

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `architecture.md`
- Modify: `fangan_done.md`
- Modify: `docs/superpowers/specs/2026-07-21-application-boundary-foundation-design.md`

**Interfaces:**
- Consumes: the verified final module/file names and timings.
- Produces: matching English/Chinese setup and architecture guidance plus a factual debt-ledger update.

- [ ] **Step 1: Add/adjust documentation contract tests before prose**

Extend `backend/tests/test_architecture_documentation.py` with stable phrases/paths rather than line positions:

```python
def test_application_boundary_documentation_names_the_canonical_facades_and_clients():
    architecture = (ROOT / "architecture.md").read_text(encoding="utf-8")
    assert "app/api/routes.py" in architecture
    assert "app/models/schemas.py" in architecture
    assert "frontend/app/api-client.ts" in architecture
    assert "composition" in architecture or "聚合" in architecture
    assert "compatibility" in architecture or "兼容" in architecture
```

Add README/AGENTS checks only for load-bearing commands/path names; do not snapshot paragraphs. Run and expect FAIL while docs still describe the monolith as current.

- [ ] **Step 2: Update both READMEs together**

Document:

- domain FastAPI routers composed by `app/api/routes.py`;
- domain Pydantic modules with `app/models/schemas.py` as legacy facade;
- `api-client.ts` transport plus domain API modules;
- `check.sh` running three lanes, backend gate default 9 workers while retaining the environment override;
- Apple Silicon warm gate hard target of 60 seconds and CI observation-only timing.

Keep English and Chinese sections semantically aligned.

- [ ] **Step 3: Update AGENTS and architecture**

Add canonical constraints:

```text
- New backend endpoints go to the owning domain router, never routes.py.
- New Pydantic models go to the owning domain model module; schemas.py is legacy compatibility only.
- Frontend HTTP mechanics go through api-client.ts; product policy remains in domain API modules.
- Tests patch public/domain seams, never private aggregate helpers.
- No test may bind architecture to line counts, source positions, or total file/route/model counts.
```

In `architecture.md`, replace the “planned” router/client and Pydantic split entries with the actual modules and leave workspace-state and lifespan work explicitly outstanding.

- [ ] **Step 4: Update the historical-debt ledger factually**

In `fangan_done.md`:

- add a dated architecture-debt entry for domain routers, Pydantic facade, shared frontend client, equivalence evidence, and measured warm timings;
- remove FastAPI router/frontend API client and Pydantic split from the outstanding architecture list;
- leave workspace-state hooks and FastAPI lifespan/application lifecycle outstanding;
- do not label file movement as a new product-spec feature.

Set the design document status to `Implemented` only after the complete gate is green.

- [ ] **Step 5: Run documentation and complete gates**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n0 backend/tests/test_architecture_documentation.py
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Expected: PASS and all lane times at most 60 seconds.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md README_zh.md AGENTS.md architecture.md fangan_done.md docs/superpowers/specs/2026-07-21-application-boundary-foundation-design.md backend/tests/test_architecture_documentation.py
git commit -m "docs: record the application boundary baseline"
```

### Task 15: Verify, publish one PR, and independently review the exact head

**Files:**
- Modify only if verification or review finds a concrete defect.
- Update the GitHub PR body with verification and test-audit evidence.

**Interfaces:**
- Produces: one draft-then-ready PR and an independently reviewed exact green SHA.

- [ ] **Step 1: Invoke the completion verification skill**

Read and use `superpowers:verification-before-completion`. Confirm:

```bash
git status --short --branch
git diff --check origin/master...HEAD
rg -n 'app\.api\.routes|from app\.api import routes' backend/tests scripts
rg -n '\bfetch\s*\(' frontend/app --glob '*.{ts,tsx}' --glob '!*.test.*' --glob '!api-client.ts'
```

Expected: clean worktree, no diff whitespace errors, no private aggregate coupling, no direct production fetch outside the transport core.

- [ ] **Step 2: Run fresh focused and production verification**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -n9 \
  backend/tests/test_model_domain_boundaries.py \
  backend/tests/test_route_domain_boundaries.py \
  backend/tests/test_architecture_documentation.py
cd frontend && node --test app/api-client.test.mjs app/api-boundary.test.mjs app/errors.test.mjs app/ask-stream.test.mjs
cd frontend && npm run build
```

Expected: PASS.

- [ ] **Step 3: Run the two authoritative final warm gates**

From the worktree root, run separately and record both outputs:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Expected: two consecutive PASS runs, every lane at most 60 seconds. These are the authoritative final timings; earlier task timings are diagnostic only.

- [ ] **Step 4: Prepare the PR evidence**

The PR body must include:

```markdown
## Contract equivalence
- normalized OpenAPI: identical
- competing route order: identical
- public paths/methods/status/response/security: no unexplained differences

## Verification
- Homebrew Python: /opt/homebrew/Caskroom/miniconda/base/bin/python
- warm run 1: record the exact contracts/backend/frontend values from the first authoritative run
- warm run 2: record the exact contracts/backend/frontend values from the second authoritative run
- frontend production build: passed

## Test optimization after equivalence
[paste the completed audit table from /tmp/application-boundary-test-audit.md]

## Deferred debt
- frontend workspace state/hooks
- FastAPI lifespan/application lifecycle
- endpoint retirement decisions
```

Replace the two timing instructions with the observed numeric values before creating the PR.

- [ ] **Step 5: Publish through the GitHub PR workflow**

Read and use `github:yeet` to confirm scope, push `codex/application-boundary-foundation`, and open one draft PR against `master`. Do not mix in the unrelated local `master` model-status commits.

- [ ] **Step 6: Review the exact PR head with an independent subagent**

After the PR exists and checks for the exact head are green, read and use `superpowers:requesting-code-review`. Record:

```bash
git rev-parse HEAD
gh pr view --json number,url,headRefOid
```

Spawn one independent reviewer with model `gpt-5.6-terra`, `reasoning_effort=high`, and the exact head SHA. Ask it to inspect:

- API/schema/permission drift;
- FastAPI route collision/order drift;
- async event-loop blocking;
- Pydantic import cycles or duplicate class definitions;
- frontend auth/trusted-error/Blob/NDJSON/cancellation policy drift;
- brittle or redundant migration tests;
- whether both ≤60-second runs preserve all coverage.

- [ ] **Step 7: Process review findings rigorously**

Read and use `superpowers:receiving-code-review`. Reproduce every Critical/Important finding, add or adjust a failing test, implement the minimal correction, and rerun the affected focused tests plus two complete warm gates. Push the new head and request a follow-up exact-head review. Do not mark the PR ready with an open Critical/Important finding.

- [ ] **Step 8: Final handoff**

Update the PR body with final SHA, final timings, review disposition, retained facades, and deferred debt. Mark the PR ready only when the exact pushed head is clean, green, independently reviewed, and still meets two consecutive ≤60-second warm gates.
