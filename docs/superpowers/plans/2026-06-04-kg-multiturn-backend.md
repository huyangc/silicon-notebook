# KG Multi-Turn Chat — Backend Implementation Plan (Phase 3, backend only)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`. Frontend chat UI is OUT OF SCOPE (handled separately by other agents) — this plan only makes `/ask` conversation-aware and exposes conversation read APIs.

**Goal:** Make `/ask` part of a persisted multi-turn conversation: each ask appends a turn to a conversation, prior turns are fed into the prompt so follow-ups stay coherent, and conversations are listable/readable via API.

**Architecture:** Reuse the existing `answers` table (each row already stores question + full `AskResponse` payload) by adding a `conversation_id` foreign key, plus a new `conversations` metadata table. `ask()` creates a conversation when none is supplied, loads the last N prior turns into a history block for `answer_prompt`, saves the turn, and returns the `conversation_id`. Retrieval still runs fresh per question (history shapes wording, not retrieval).

**Tech Stack:** FastAPI routes (`backend/app/api/routes.py`), SQLite repo (`backend/app/services/sqlite_repository.py`), prompts (`backend/app/services/prompts.py`), Pydantic schemas (`backend/app/models/schemas.py`). Tests: pytest, all LLM/embeddings mocked.

**Run from:** ROOT checkout `/Users/hzf/workspace/silicon_notebook` on `master`. Gate: `cd backend && python -m pytest -q`.

Reference reading: `_save_answer` (`sqlite_repository.py:2823`), `answers` table DDL (`:291`), `ask()` (`:2560`), `answer_prompt` (`prompts.py:74`, currently `answer_prompt(question, context_block)`), `AskRequest`/`AskResponse` (`schemas.py`).

---

### Task 3.1: Schema — `conversations` table + `answers.conversation_id`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — table DDL (near `:291`) + a guarded `ALTER TABLE`
- Test: `backend/tests/test_conversations.py` (new)

- [ ] **Step 1: Write the failing test** (reuse the repo fixture from `test_unified_kg_repository.py`: temp DB, `EMBED_PROVIDER=dashscope`, injected `FakeEmbedder` + a `FakeLLM` returning `{"answer":"...","grounded":false}`).

```python
# backend/tests/test_conversations.py
import json, pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest

class FakeLLM:
    configured = True
    def chat_json(self, messages, schema_hint):
        return json.dumps({"answer": "ok.", "grounded": False})

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope"); monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings()); r.embedder = FakeEmbedder(dim=16); r.llm_client = FakeLLM()
    return r

def test_schema_has_conversations_and_fk(repo):
    with repo._connect() as db:
        cols = {row[1] for row in db.execute("PRAGMA table_info(answers)").fetchall()}
        assert "conversation_id" in cols
        tbls = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "conversations" in tbls
```

- [ ] **Step 2: Run** `cd backend && python -m pytest tests/test_conversations.py::test_schema_has_conversations_and_fk -v` → FAIL.

- [ ] **Step 3: Implement** — in the table-creation method, add the new table and a guarded column add (SQLite has no `ADD COLUMN IF NOT EXISTS`):

```python
db.execute("""CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL, title TEXT DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
# guarded column add on answers
cols = {r[1] for r in db.execute("PRAGMA table_info(answers)").fetchall()}
if "conversation_id" not in cols:
    db.execute("ALTER TABLE answers ADD COLUMN conversation_id TEXT")
```
Place alongside the other `CREATE TABLE IF NOT EXISTS` statements so it runs on every init.

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_conversations.py
git commit -m "feat(chat): conversations table + answers.conversation_id column"
```

### Task 3.2: Schemas — `conversation_id` on AskRequest/AskResponse + Conversation models

**Files:**
- Modify: `backend/app/models/schemas.py`
- Test: `backend/tests/test_conversations.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_conversation_schemas_exist():
    from app.models.schemas import AskRequest, AskResponse, ConversationSummary, ConversationDetail
    req = AskRequest(question="q", conversation_id="c1")
    assert req.conversation_id == "c1"
    resp = AskResponse(conclusion="x", conversation_id="c1")
    assert resp.conversation_id == "c1"
    s = ConversationSummary(id="c1", notebook_id="n", title="t", updated_at="now", turn_count=2)
    assert s.turn_count == 2
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement** — add `conversation_id: Optional[str] = None` to `AskRequest`; `conversation_id: str = ""` to `AskResponse`; and:

```python
class ConversationSummary(BaseModel):
    id: str
    notebook_id: str
    title: str = ""
    updated_at: str = ""
    turn_count: int = 0

class ConversationTurn(BaseModel):
    answer_id: str
    question: str
    response: AskResponse
    created_at: str = ""

class ConversationDetail(ConversationSummary):
    turns: List[ConversationTurn] = Field(default_factory=list)
```

- [ ] **Step 4: Run** → PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/schemas.py backend/tests/test_conversations.py
git commit -m "feat(chat): conversation schemas + conversation_id on AskRequest/AskResponse"
```

### Task 3.3: `answer_prompt` history block

**Files:**
- Modify: `backend/app/services/prompts.py` — `answer_prompt`
- Test: `backend/tests/test_prompts.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def test_answer_prompt_includes_history_when_present():
    from app.services.prompts import answer_prompt
    p = answer_prompt("follow up?", "k1: [concept] X", history_block="User: prev q\nAssistant: prev a")
    assert "prev q" in p and "prev a" in p
    p2 = answer_prompt("q?", "k1: [concept] X")   # default no history
    assert "prev q" not in p2
```

- [ ] **Step 2: Run** `cd backend && python -m pytest tests/test_prompts.py::test_answer_prompt_includes_history_when_present -v` → FAIL.

- [ ] **Step 3: Implement** — add a third optional param `history_block: str = ""`; when non-empty, insert a "Prior conversation (for context; the current question may refer to it):\n{history_block}\n\n" section BEFORE the `Question:` line. Keep all existing rules intact.

- [ ] **Step 4: Run** → PASS. (Existing `answer_prompt` callers pass 2 args → still valid since the new param defaults.)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/prompts.py backend/tests/test_prompts.py
git commit -m "feat(chat): answer_prompt optional history block"
```

### Task 3.4: `ask()` conversation-aware (create/append + history + return id)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — `ask()`, `_save_answer`, add `_conversation_history`, `_ensure_conversation`
- Test: `backend/tests/test_conversations.py` (append)

- [ ] **Step 1: Write the failing test**

```python
def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id": "C1", "object_type": "concept",
        "payload": {"name": "Engram", "section_path": "1"}, "evidence": []}], [])
    return nb

def test_ask_creates_then_appends_conversation(repo):
    nb = _seed(repo)
    r1 = repo.ask(nb.id, AskRequest(question="what is engram"))
    assert r1.conversation_id                      # new conversation created
    r2 = repo.ask(nb.id, AskRequest(question="and its drawbacks?", conversation_id=r1.conversation_id))
    assert r2.conversation_id == r1.conversation_id  # appended, not new
    detail = repo.get_conversation(r1.conversation_id)
    assert detail.turn_count == 2 and len(detail.turns) == 2

def test_ask_feeds_prior_turns_into_prompt(repo, monkeypatch):
    nb = _seed(repo)
    captured = {}
    def cap(messages, schema_hint):
        captured["p"] = messages[0]["content"]
        return json.dumps({"answer": "ok.", "grounded": False})
    repo.llm_client.chat_json = cap
    r1 = repo.ask(nb.id, AskRequest(question="ZZTOPIC question"))
    repo.ask(nb.id, AskRequest(question="follow up", conversation_id=r1.conversation_id))
    assert "ZZTOPIC question" in captured["p"]      # prior turn present in 2nd prompt
```

- [ ] **Step 2: Run** `cd backend && python -m pytest tests/test_conversations.py -k ask -v` → FAIL (no conversation logic / `get_conversation`).

- [ ] **Step 3: Implement.**
  - `_ensure_conversation(self, db, notebook_id, conversation_id, question) -> str`: if `conversation_id` given AND a row exists for it in this notebook → update `updated_at`, return it; else create a new `conversations` row (id `conv-<hex>`, title=`question[:60]`, timestamps) and return its id.
  - `_conversation_history(self, db, conversation_id, limit=5) -> str`: load prior answers for the conversation ordered by `created_at` (oldest→newest, last `limit`); for each build `User: {question}\nAssistant: {conclusion}` (use the stored payload's `conclusion` = markers stripped). Return joined string ("" if none).
  - In `ask()`: at the start of the synthesis, resolve `conversation_id = self._ensure_conversation(...)`; build `history = self._conversation_history(db, conversation_id)`; pass `history` into `_answer_kg`→`answer_prompt(question, context_block, history)`. Set `response.conversation_id = conversation_id`. Pass `conversation_id` into `_save_answer` so the row records it.
  - Update `_save_answer(self, notebook_id, question, response, conversation_id=None)` to write the `conversation_id` column.
  - Add `get_conversation(self, conversation_id) -> ConversationDetail` and `list_conversations(self, notebook_id) -> List[ConversationSummary]` (turn_count = count of answers with that conversation_id; turns built from the stored answer payloads, parsed back into `AskResponse`).
  - `_answer_kg` / `_answer_context` already exist (Phase 1); thread `history` through `_answer_kg`'s `answer_prompt` call.

- [ ] **Step 4: Run** `cd backend && python -m pytest tests/test_conversations.py -q` then full `python -m pytest -q`. Expected PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_conversations.py
git commit -m "feat(chat): ask() creates/append conversation, feeds prior turns into prompt, returns conversation_id"
```

### Task 3.5: Conversation read endpoints

**Files:**
- Modify: `backend/app/api/routes.py` — add 2 GET routes
- Test: `backend/tests/test_conversations.py` (append, via FastAPI TestClient if the suite already uses one; else call repo methods directly — check existing route tests for the pattern)

- [ ] **Step 1: Write the failing test** (repo-level is sufficient; the routes are thin wrappers):

```python
def test_list_conversations(repo):
    nb = _seed(repo)
    r = repo.ask(nb.id, AskRequest(question="q1"))
    convs = repo.list_conversations(nb.id)
    assert len(convs) == 1 and convs[0].id == r.conversation_id and convs[0].turn_count == 1
```

- [ ] **Step 2: Run** → FAIL if `list_conversations` missing (implemented in 3.4; if already passing, keep as regression guard and proceed to routes).

- [ ] **Step 3: Implement routes** in `routes.py` (mirror existing route style + 404 handling):

```python
@router.get("/notebooks/{notebook_id}/conversations", response_model=List[ConversationSummary])
def list_conversations(notebook_id: str):
    try:
        return repository().list_conversations(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")

@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(conversation_id: str):
    try:
        return repository().get_conversation(conversation_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")
```
Import `ConversationSummary`, `ConversationDetail` in routes.

- [ ] **Step 4: Run** `cd backend && python -m pytest -q` → all green.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes.py backend/tests/test_conversations.py
git commit -m "feat(chat): GET conversations list + conversation detail endpoints"
```

---

## Self-Review (against spec §3 Phase 3)
- Conversation persistence (tables + turns) → 3.1/3.4. `conversation_id` on AskRequest/AskResponse → 3.2. History into prompt → 3.3/3.4. Re-retrieve per question (history shapes wording not retrieval) → 3.4 (retrieval unchanged; history only added to the answer prompt). Read APIs → 3.5. Frontend chat UI → **out of scope (other agents)**.
- No placeholder steps; every code step shows code. Type names consistent: `ConversationSummary`/`ConversationDetail`/`ConversationTurn`, `get_conversation`/`list_conversations`, `_ensure_conversation`/`_conversation_history`.
- Back-compat: old `answers` rows have `conversation_id = NULL` (ignored by conversation listing); `AskRequest.conversation_id` optional; `answer_prompt` history param defaults to "".

## Non-goals
Frontend chat thread UI; conversation rename/delete (can add later); summarizing long histories (simple last-N for now); cross-conversation memory.
