# Agent Memory System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add notebook-bound, creator-private Memory with Ask capture, Agent-shared candidates, formal notebook retrieval after user confirmation, MCP access, and governed KG promotion.

**Architecture:** Add a native Memory store/service beside Source and Knowledge, then project eligible Memory into retrieval without pretending it is a source. REST and MCP adapters consume the same narrow ports; MCP has a user+notebook Agent candidate plane while Ask/Search/Report use confirmed-only notebook retrieval.

**Tech Stack:** Python 3.11+, FastAPI, SQLite/sqlite3, Pydantic v2, NumPy embeddings, official `mcp` Python SDK/FastMCP, Next.js/React/TypeScript, Node test runner.

## Global Constraints

- Product/project name remains `silicon-notebook`; do not add a Chinese product name.
- On the merged branch, bump `SCHEMA_VERSION` from master v12 to v13 with `_migration_13`; master v11/v12 retain their SQLite hot-path indexes, and v9 fixture replay must still upgrade and load. (The feature branch originally used v11 before those master migrations landed.)
- Every Memory has exactly one `notebook_id` and one `created_by`; no global orphan Memory and no cross-notebook Memory retrieval.
- Memory is creator-private even in shared notebooks; Agent candidates are shared only among the same user’s authorized Agents in the same notebook.
- Candidate Memory never enters Ask, Notebook Search, Deep Report, or `search_notebook_context`; only user-confirmed Memory does.
- Backend and frontend ship together. The workspace tabs become `Ask | Knowledge | Memory | Deep Report`.
- Do not put Memory into `sources`, `source_elements`, `chunks`, or `knowledge_objects`; promotion creates separate governed KG objects.
- Keep repository dependencies facade → runtime → services → stores → `SqliteDatabase`; add consumer-specific Protocols in `backend/app/repositories/ports.py`.
- MCP uses Streamable HTTP at `/mcp`; local binds stay on localhost and remote usage requires HTTPS plus scoped, revocable Agent tokens.
- Add `mcp` to `backend/requirements.txt`; ask before installing it into the canonical environment if installation is required.
- Keep `.env.example` defaults offline and do not add Docker/PostgreSQL requirements.
- Final verification is `bash scripts/check.sh` and `cd frontend && npm run build`; update `README.md`, `README_zh.md`, `AGENTS.md`, `silicon_notebook_fangan.md`, and `fangan_done.md` together.

---

## File Structure

- `backend/app/repositories/sqlite/memory_store.py`: Memory rows, revisions, provenance, lexical index, embeddings, Agent profiles/tokens.
- `backend/app/services/memory_service.py`: lifecycle, privacy, idempotency, preview/save, candidate review, token policy.
- `backend/app/services/memory_retrieval.py`: two retrieval planes and authority-aware Memory hits.
- `backend/app/api/memory_routes.py`: REST models-to-service adapter.
- `backend/app/api/mcp_server.py`: FastMCP tools, token request context, Streamable HTTP ASGI app.
- `frontend/app/memory-model.ts`: Memory API/view types and labels.
- `frontend/app/memory-panel.tsx`: global/notebook Memory lists, editor/review, Agent access manager.
- `frontend/app/memory-panel.css`: Memory-only layout and state styling.
- Existing `page.tsx` remains the orchestrator; it owns navigation/opening but not Memory rendering internals.

---

### Task 1: Schema v13 and Memory Models

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py`
- Modify: `backend/app/models/schemas.py`
- Test: `backend/tests/test_memory_migration.py`
- Test: `backend/tests/test_legacy_db_compat.py`

**Interfaces:**
- Produces Pydantic `MemoryRecord`, `PaginatedMemories`, `MemoryPreview`, `MemoryCreateFromAnswer`, `MemoryUpdate`, `MemoryReviewRequest`, `AgentProfile`, `AgentTokenCreate`, and `AgentTokenIssued`.
- Produces schema tables `memory_items`, `memory_revisions`, `memory_provenance`, `memory_embeddings`, `agent_profiles`, `agent_access_tokens`, and `agent_token_notebooks`.

- [ ] **Step 1: Write failing migration and schema tests**

```python
def test_v11_memory_schema_has_privacy_and_agent_indexes(repo):
    with repo._connect() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"memory_items", "memory_revisions", "memory_provenance", "memory_embeddings",
                "agent_profiles", "agent_access_tokens", "agent_token_notebooks"} <= tables
        indexes = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_memory_owner_notebook_status" in indexes
        assert "idx_memory_agent_candidate" in indexes
```

- [ ] **Step 2: Run the focused tests and observe failure**

Run: `cd backend && python -m pytest tests/test_memory_migration.py tests/test_legacy_db_compat.py -q`

Expected: FAIL because schema version 13 and Memory tables/models do not exist.

- [ ] **Step 3: Add `_migration_13`, constraints, indexes, and models**

```python
SCHEMA_VERSION = 13

def _migration_13(self) -> None:
    with self._connect() as db:
        db.executescript("""
        CREATE TABLE memory_items (
          id TEXT PRIMARY KEY,
          notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
          created_by TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          agent_profile_id TEXT REFERENCES agent_profiles(id) ON DELETE SET NULL,
          source_answer_id TEXT,
          origin TEXT NOT NULL CHECK(origin IN ('ask_answer','external_agent')),
          status TEXT NOT NULL CHECK(status IN ('candidate','confirmed','rejected','deprecated')),
          promotion_state TEXT NOT NULL DEFAULT 'none' CHECK(promotion_state IN ('none','proposed','approved','rejected')),
          title TEXT NOT NULL,
          content_md TEXT NOT NULL,
          tags_json TEXT NOT NULL DEFAULT '[]',
          confirmed_by TEXT REFERENCES users(id), confirmed_at TEXT,
          embedding_status TEXT NOT NULL DEFAULT 'pending', embedding_error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_memory_answer_once
          ON memory_items(created_by, source_answer_id) WHERE source_answer_id IS NOT NULL;
        CREATE INDEX idx_memory_owner_notebook_status
          ON memory_items(created_by, notebook_id, status, updated_at DESC);
        CREATE INDEX idx_memory_agent_candidate
          ON memory_items(created_by, notebook_id, status, agent_profile_id);
        """)
```

Create dependent tables before `memory_items` or split the script so `agent_profiles` exists before the FK. Add an FTS5 `memory_items_fts` table and triggers/rebuild helper rather than scanning Memory text.

- [ ] **Step 4: Run tests and compatibility replay**

Run: `cd backend && python -m pytest tests/test_memory_migration.py tests/test_legacy_db_compat.py tests/test_repository_v9_fixture.py -q`

Expected: PASS; upgraded DB reports user_version 13.

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/sqlite/migrations.py backend/app/models/schemas.py backend/tests/test_memory_migration.py backend/tests/test_legacy_db_compat.py
git commit -m "feat(memory): add v13 memory schema"
```

### Task 2: Memory Store, Ports, and Lifecycle Service

**Files:**
- Create: `backend/app/repositories/sqlite/memory_store.py`
- Create: `backend/app/services/memory_service.py`
- Modify: `backend/app/repositories/ports.py`
- Modify: `backend/app/services/repository_runtime.py`
- Modify: `backend/app/services/sqlite_repository.py`
- Modify: `backend/app/repositories/ownership_manifest.py`
- Modify: `backend/app/repositories/sqlite/query_store.py`
- Test: `backend/tests/test_memory_service.py`
- Test: `backend/tests/test_memory_repository_boundaries.py`

**Interfaces:**
- Produces `MemoryStorePort.list_memories(user_id, notebook_id, status, origin, query, offset, limit) -> PaginatedMemories`.
- Produces `MemoryService.create_candidate(notebook_id, user_id, agent_profile_id, client_request_id, title, content_md, tags, reason, task_context, evidence_refs) -> MemoryRecord`, `create_from_answer(notebook_id, user_id, answer_id, title, content_md, tags) -> MemoryRecord`, `update(memory_id, user_id, patch) -> MemoryRecord`, `confirm(memory_id, user_id, patch) -> MemoryRecord`, `reject(memory_id, user_id) -> MemoryRecord`, `deprecate(memory_id, user_id) -> MemoryRecord`, and `get(memory_id, user_id) -> MemoryRecord`.

- [ ] **Step 1: Write lifecycle, privacy, revision, and idempotency tests**

```python
def test_agent_candidate_is_private_to_owner_but_shared_across_owner_profiles(memory_service, users, notebook):
    item = memory_service.create_candidate(notebook.id, users.alice.id, "agent-a", "req-1", "Title", "Body", [], "task")
    assert memory_service.get(item.id, users.alice.id).id == item.id
    with pytest.raises(KeyError):
        memory_service.get(item.id, users.bob.id)

def test_confirm_writes_revision_and_duplicate_answer_is_idempotent(memory_service, saved_answer, alice):
    first = memory_service.create_from_answer(saved_answer.notebook_id, alice.id, saved_answer.id, "T", "B", [])
    second = memory_service.create_from_answer(saved_answer.notebook_id, alice.id, saved_answer.id, "T2", "B2", [])
    assert second.id == first.id
    assert memory_service.revisions(first.id)[0].status == "confirmed"
```

- [ ] **Step 2: Run focused tests and observe missing store/service failures**

Run: `cd backend && python -m pytest tests/test_memory_service.py tests/test_memory_repository_boundaries.py -q`

Expected: FAIL on missing `MemoryService`/ports.

- [ ] **Step 3: Implement store methods with all SQL confined to `MemoryStore`**

Implement these exact store methods: `insert_memory(write: MemoryWrite) -> MemoryRecord`, `memory_for_user(memory_id: str, user_id: str) -> MemoryRecord`, `append_revision(memory_id: str, snapshot: dict, changed_by: str, reason: str) -> None`, `transition(memory_id: str, user_id: str, expected: set[str], target: str) -> MemoryRecord`, and `list_memories(user_id: str, *, notebook_id: str | None, status: str | None, origin: str | None, query: str, offset: int, limit: int) -> PaginatedMemories`.

- [ ] **Step 4: Compose `MemoryService` in `RepositoryRuntime` and add explicit facade delegates**

The service must take `MemoryStorePort`, `AskStateStorePort`, `NotebookAccessRepository`, `Embedder`, `EventLogger`, `new_id`, and `now`. Emit lifecycle/embedding events without raw private content. Extend `QueryStore` notebook summaries with one grouped `(created_by, notebook_id)` Memory count query, not per-card queries. Add ownership-manifest entries and static boundary tests so services never import `SQLiteRepository`.

- [ ] **Step 5: Run service and repository contract suites**

Run: `cd backend && python -m pytest tests/test_memory_service.py tests/test_memory_repository_boundaries.py tests/test_repository_ports.py tests/test_repository_facade_contract.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/repositories/sqlite/memory_store.py backend/app/services/memory_service.py backend/app/repositories/ports.py backend/app/services/repository_runtime.py backend/app/services/sqlite_repository.py backend/app/repositories/ownership_manifest.py backend/app/repositories/sqlite/query_store.py backend/tests/test_memory_service.py backend/tests/test_memory_repository_boundaries.py
git commit -m "feat(memory): add lifecycle service and store"
```

### Task 3: REST API and Ask-to-Memory Capture

**Files:**
- Create: `backend/app/api/memory_routes.py`
- Modify: `backend/app/api/deps.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/services/prompts.py`
- Test: `backend/tests/test_memory_api.py`
- Test: `backend/tests/test_memory_preview.py`

**Interfaces:**
- Produces the REST endpoints from spec §8.
- Consumes `MemoryService`; preview never persists and save rebuilds provenance server-side.

- [ ] **Step 1: Write API tests for owner/reader creation, foreign-user 404, fallback preview, and answer idempotency**

```python
def test_reader_can_save_private_memory_from_own_answer(client, reader_headers, shared_notebook, answer):
    preview = client.post(f"/api/answers/{answer.id}/memory-preview", headers=reader_headers)
    assert preview.status_code == 200
    saved = client.post(f"/api/notebooks/{shared_notebook.id}/memories/from-answer",
        headers=reader_headers, json={"answer_id": answer.id, "title": "Edited", "content_md": "Body", "tags": []})
    assert saved.status_code == 201
    assert saved.json()["status"] == "confirmed"
```

- [ ] **Step 2: Run tests and observe 404/missing route failures**

Run: `cd backend && python -m pytest tests/test_memory_api.py tests/test_memory_preview.py -q`

Expected: FAIL because `memory_router` is not mounted.

- [ ] **Step 3: Implement `memory_router` and async-safe dependencies**

Use `run_in_threadpool` for synchronous SQLite auth/service calls. `from-answer` uses notebook read access, not owner-only write access, because the row is creator-private. Preview returns deterministic `{title: question[:80], content_md: cleaned_answer, tags: []}` on missing/failed LLM.

- [ ] **Step 4: Run API tests and full auth/sharing regressions**

Run: `cd backend && python -m pytest tests/test_memory_api.py tests/test_memory_preview.py tests/test_auth.py tests/test_notebook_share_readonly.py tests/test_notebook_share_copy.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/memory_routes.py backend/app/api/deps.py backend/app/main.py backend/app/services/prompts.py backend/tests/test_memory_api.py backend/tests/test_memory_preview.py
git commit -m "feat(memory): add REST and answer capture"
```

### Task 4: Memory Pages and Answer Action

**Files:**
- Create: `frontend/app/memory-model.ts`
- Create: `frontend/app/memory-panel.tsx`
- Create: `frontend/app/memory-panel.css`
- Modify: `frontend/app/answer-panel.tsx`
- Modify: `frontend/app/page.tsx`
- Modify: `frontend/app/workspace-model.ts`
- Modify: `frontend/app/globals.css`
- Test: `frontend/app/memory-model.test.mjs`
- Test: `frontend/app/memory-navigation.test.mjs`
- Test: `frontend/app/answer-memory.test.mjs`

**Interfaces:**
- Produces `<MemoryPanel scope="global" | "notebook" notebookId={string | null} />`.
- Extends `AnswerView` with `onSaveMemory(answerId)` and `memorySaved`.

- [ ] **Step 1: Write failing pure-model/navigation tests**

```javascript
test("candidate labels remain distinct from confirmed", () => {
  assert.equal(memoryStatusMeta("candidate").label, "待确认")
  assert.equal(memoryStatusMeta("confirmed").label, "已确认")
})
test("memory count deep-link targets the notebook memory tab", () => {
  assert.equal(memoryHash("nb-1"), "#notebook=nb-1&tab=memory")
})
```

- [ ] **Step 2: Run tests and observe missing exports**

Run: `cd frontend && node --test app/memory-model.test.mjs app/memory-navigation.test.mjs app/answer-memory.test.mjs`

Expected: FAIL.

- [ ] **Step 3: Implement focused Memory components and wire `page.tsx` only as orchestrator**

Add outer navigation `Notebooks | Memory`, fourth workspace tab, card count button, paginated filters, candidate editor/review, provenance detail, and save-answer preview modal. Preserve input lock/cancel behavior and epoch guards. Update notebook-delete confirmation to warn that all members' bound private Memory is lifecycle-deleted without exposing identities/counts. Agent access management is added in Task 6 after its API exists.

- [ ] **Step 4: Run all frontend tests and build**

Run: `cd frontend && node --test $(find app -name '*.test.mjs' -print) && npm run build`

Expected: all tests PASS and Next.js build exits 0.

- [ ] **Step 5: Commit**

```bash
git add frontend/app/memory-model.ts frontend/app/memory-panel.tsx frontend/app/memory-panel.css frontend/app/answer-panel.tsx frontend/app/page.tsx frontend/app/workspace-model.ts frontend/app/globals.css frontend/app/*memory*.test.mjs
git commit -m "feat(memory): add global and notebook memory UI"
```

### Task 5: Two-Plane Memory Retrieval and Authority

**Files:**
- Create: `backend/app/services/memory_retrieval.py`
- Modify: `backend/app/services/retrieval_candidates.py`
- Modify: `backend/app/services/ask_service.py`
- Modify: `backend/app/services/knowledge_query.py`
- Modify: `backend/app/models/schemas.py`
- Create: `backend/app/eval/memory_retrieval.py`
- Create: `backend/app/eval/memory_gold.yaml`
- Create: `backend/app/eval/memory_agent_ab.py`
- Test: `backend/tests/test_memory_retrieval.py`
- Test: `backend/tests/test_memory_authority.py`

**Interfaces:**
- Produces `MemoryHit(memory_id, title, text, status, authority, score, provenance)`.
- Produces `notebook_memory_hits(user_id, notebook_id, query, limit)` confirmed-only.
- Produces `agent_memory_hits(user_id, notebook_id, query, include_candidates, limit)` same-user candidate + confirmed.

- [ ] **Step 1: Write failing eligibility, isolation, relevance, and conflict tests**

```python
def test_notebook_plane_never_returns_candidate(memory_retriever, data):
    hits = memory_retriever.notebook_memory_hits(data.alice, data.nb, "timing", 10)
    assert all(hit.status == "confirmed" for hit in hits)

def test_agent_plane_shares_candidate_across_same_user_profiles_only(memory_retriever, data):
    assert data.candidate.id in {h.memory_id for h in memory_retriever.agent_memory_hits(data.alice, data.nb, "timing", True, 10)}
    assert data.candidate.id not in {h.memory_id for h in memory_retriever.agent_memory_hits(data.bob, data.nb, "timing", True, 10)}
```

- [ ] **Step 2: Run tests and observe missing retriever failures**

Run: `cd backend && python -m pytest tests/test_memory_retrieval.py tests/test_memory_authority.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement lexical+vector fusion and confirmed-only Ask projection**

Apply relevance eligibility before authority. For contradiction synthesis use `candidate < personal source < confirmed Memory < base`; candidate is only present in Agent-plane results. Add Memory anchors with provenance snapshot and no fake source ids.

- [ ] **Step 4: Add fixed gold evaluator and regression thresholds**

`memory_retrieval.py` must report Recall@5/MRR/nDCG plus zero-tolerance counts for candidate-to-notebook, cross-user, and cross-notebook leaks. `memory_agent_ab.py` runs the same task fixtures in no-Memory, KB-only, and KB+confirmed-Memory modes and reports success, tool calls, repeated steps, token counts, and citation validity. Tests assert every zero-tolerance count is zero.

- [ ] **Step 5: Run retrieval/Ask/Report regressions**

Run: `cd backend && python -m pytest tests/test_memory_retrieval.py tests/test_memory_authority.py tests/test_retrieval.py tests/test_mix_answer.py tests/test_report_api.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/memory_retrieval.py backend/app/services/retrieval_candidates.py backend/app/services/ask_service.py backend/app/services/knowledge_query.py backend/app/models/schemas.py backend/app/eval/memory_retrieval.py backend/app/eval/memory_gold.yaml backend/app/eval/memory_agent_ab.py backend/tests/test_memory_retrieval.py backend/tests/test_memory_authority.py
git commit -m "feat(memory): add two-plane retrieval"
```

### Task 6: Agent Profiles, Tokens, and Management UI

**Files:**
- Modify: `backend/app/repositories/sqlite/memory_store.py`
- Modify: `backend/app/services/memory_service.py`
- Modify: `backend/app/api/memory_routes.py`
- Modify: `frontend/app/memory-panel.tsx`
- Test: `backend/tests/test_agent_tokens.py`
- Test: `frontend/app/agent-token-model.test.mjs`

**Interfaces:**
- Produces `GET|POST /api/agent-profiles`, `PATCH /api/agent-profiles/{id}`, `POST /api/agent-profiles/{id}/tokens`, `GET /api/agent-tokens`, and `DELETE /api/agent-tokens/{id}`.
- Produces `resolve_agent_token(raw_token) -> AgentPrincipal | None` with profile, owner, scopes, default notebook, and allowlist.

- [ ] **Step 1: Write failing hash, scope, expiry, rotation, and revocation tests**

```python
def test_rotated_profile_token_can_read_shared_candidate(service, profile, old_token, new_token, candidate):
    service.revoke_token(old_token.id)
    principal = service.resolve_agent_token(new_token.raw)
    assert principal.profile_id == profile.id
    assert service.get_for_agent(candidate.id, principal).id == candidate.id
```

- [ ] **Step 2: Run tests and observe failures**

Run: `cd backend && python -m pytest tests/test_agent_tokens.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement SHA-256 token hashing, one-time display, scope/allowlist checks, throttled last-used touch, and UI**

Use constant-time hash comparison; never return stored hashes. UI requires profile, default notebook, allowlist, scopes, and expiry before issuing, and exposes list/revoke/disable actions from the total Memory page.

- [ ] **Step 4: Run backend and frontend focused tests**

Run: `cd backend && python -m pytest tests/test_agent_tokens.py -q && cd ../frontend && node --test app/agent-token-model.test.mjs`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/repositories/sqlite/memory_store.py backend/app/services/memory_service.py backend/app/api/memory_routes.py frontend/app/memory-panel.tsx backend/tests/test_agent_tokens.py frontend/app/agent-token-model.test.mjs
git commit -m "feat(memory): add agent profiles and tokens"
```

### Task 7: Streamable HTTP MCP Server and Tools

**Files:**
- Modify: `backend/requirements.txt`
- Create: `backend/app/api/mcp_server.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_memory_mcp.py`
- Create: `scripts/smoke_memory_mcp.py`

**Interfaces:**
- Exposes `/mcp` with `list_notebooks`, `select_notebook`, `search_agent_memory`, `search_notebook_context`, `get_memory`, `ask_notebook`, and `propose_memory`.

- [ ] **Step 1: Add `mcp>=1.26.0` and write failing official-client contract tests**

```python
async def test_candidate_is_agent_recallable_but_not_notebook_context(mcp_client):
    await mcp_client.call_tool("select_notebook", {"notebook_id": "nb-1"})
    created = await mcp_client.call_tool("propose_memory", {"title": "T", "content_md": "B", "reason": "R", "task_context": "C", "evidence_refs": [], "client_request_id": "r1"})
    assert created["memory_id"] in ids(await mcp_client.call_tool("search_agent_memory", {"query": "B"}))
    assert created["memory_id"] not in ids(await mcp_client.call_tool("search_notebook_context", {"query": "B"}))
```

- [ ] **Step 2: Install dependency only after approval if missing, then run the failing test**

Run: `cd backend && python -m pytest tests/test_memory_mcp.py -q`

Expected before implementation: FAIL because `/mcp` and tools do not exist.

- [ ] **Step 3: Implement FastMCP ASGI app with explicit lifespan composition**

Use `FastMCP("silicon-notebook Memory", stateless_http=False, json_response=True, streamable_http_path="/")`, mount at `/mcp`, and compose its session-manager lifespan with FastAPI’s lifespan. Resolve Bearer Agent tokens into a request ContextVar; every tool rechecks selected notebook, allowlist, and scope. Validate Origin and reject non-local plain HTTP deployment configuration.

- [ ] **Step 4: Run MCP tests and smoke client**

Run: `cd backend && python -m pytest tests/test_memory_mcp.py -q && PYTHONPATH=. python ../scripts/smoke_memory_mcp.py`

Expected: PASS; tool list contains exactly the seven public tools and isolation assertions pass.

- [ ] **Step 5: Commit**

```bash
git add backend/requirements.txt backend/app/api/mcp_server.py backend/app/main.py backend/tests/test_memory_mcp.py scripts/smoke_memory_mcp.py
git commit -m "feat(memory): expose scoped MCP tools"
```

### Task 8: Memory-to-KG Promotion

**Files:**
- Modify: `backend/app/services/memory_service.py`
- Modify: `backend/app/services/knowledge_governance.py`
- Modify: `backend/app/repositories/sqlite/governance_store.py`
- Modify: `backend/app/api/memory_routes.py`
- Modify: `frontend/app/memory-panel.tsx`
- Test: `backend/tests/test_memory_promotion.py`
- Test: `frontend/app/memory-promotion.test.mjs`

**Interfaces:**
- Produces `propose_memory_promotion(memory_id, user_id) -> PromotionCandidate`.
- Approval reuses existing dedupe/merge and records resulting base object ids without changing Memory privacy/tier.

- [ ] **Step 1: Write failing confirmed-only, admin-review, dedupe, and provenance-redaction tests**

```python
def test_approved_memory_promotion_keeps_private_memory_and_creates_base_object(service, admin, memory):
    proposal = service.propose_promotion(memory.id, memory.created_by)
    result = service.approve_promotion(proposal.id, admin.id)
    assert service.get(memory.id, memory.created_by).status == "confirmed"
    assert service.get(memory.id, memory.created_by).promotion_state == "approved"
    assert result.base_object_ids
```

- [ ] **Step 2: Run tests and observe missing adapter failures**

Run: `cd backend && python -m pytest tests/test_memory_promotion.py -q`

Expected: FAIL.

- [ ] **Step 3: Implement Memory-backed promotion source and admin UI**

Extract Concept/Claim/Formula/Procedure candidates from confirmed Memory, attach approved evidence only, reuse the promotion queue, and never expose private task context in the resulting base payload.

- [ ] **Step 4: Run promotion/governance/frontend tests**

Run: `cd backend && python -m pytest tests/test_memory_promotion.py tests/test_knowledge_governance_boundaries.py -q && cd ../frontend && node --test app/memory-promotion.test.mjs`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/memory_service.py backend/app/services/knowledge_governance.py backend/app/repositories/sqlite/governance_store.py backend/app/api/memory_routes.py frontend/app/memory-panel.tsx backend/tests/test_memory_promotion.py frontend/app/memory-promotion.test.mjs
git commit -m "feat(memory): add governed KG promotion"
```

### Task 9: Documentation, Full Verification, and Delivery

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Modify: `silicon_notebook_fangan.md`
- Modify: `fangan_done.md`
- Modify: `scripts/check.sh`

**Interfaces:**
- Documents local Claude Code/Codex Streamable HTTP configuration, token scopes, privacy, two retrieval planes, and user review workflow.

- [ ] **Step 1: Add Memory/MCP smoke coverage to `scripts/check.sh` and run it before docs claims**

Run: `bash scripts/check.sh`

Expected: backend syntax/smoke/pytest, all frontend tests, TypeScript, and production build PASS.

- [ ] **Step 2: Update all product and setup documents together**

Document exact endpoints/tools, Candidate visibility, notebook deletion cascade warning, Agent token creation/revocation, and deterministic preview fallback. In `fangan_done.md`, mark only behavior proven by Step 1 and cite the new spec section.

- [ ] **Step 3: Run final fresh verification**

Run: `bash scripts/check.sh && (cd frontend && npm run build) && git diff --check`

Expected: all commands exit 0 with no failures or whitespace errors.

- [ ] **Step 4: Review final diff for unrelated changes and commit**

```bash
git status --short
git diff --stat
git add README.md README_zh.md AGENTS.md silicon_notebook_fangan.md fangan_done.md scripts/check.sh
git commit -m "docs(memory): document agent memory workflow"
```

- [ ] **Step 5: Run completion workflow**

Invoke `superpowers:requesting-code-review`, address findings, rerun `superpowers:verification-before-completion`, then invoke `superpowers:finishing-a-development-branch` for merge/PR/cleanup choices.
