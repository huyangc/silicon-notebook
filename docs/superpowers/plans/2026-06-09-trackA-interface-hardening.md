# Track A: Interface Hardening (P1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden three internal interface boundaries in the POC worktree so that (1) eval scripts never reach past the public `NotebookRepository` Protocol to raw SQLite; (2) the LLM cache backend is injectable and testable in isolation; and (3) the RRF retrieval path computes `relevance` with the same dual-index best-of logic as the non-RRF `score_knowledge` path, keeping the `[0,1]/tau` invariant unbroken.

**Architecture:** All changes are within `backend/app/`. No new infra. No Schema migrations. No new tables. Two new thin objects (`CacheBackend` Protocol + `NoCacheBackend`), two new public methods on `SQLiteRepository`, and a one-line fix to `_rrf_scored`. The eval module (`app/eval/`) is re-plumbed to use only public repo methods and the existing `EvalDB` read-only helper (which already has its own `_connect` on a separate RO connection — that is intentional and untouched). Every task is independently commitable and independently testable.

**Tech Stack:** Python 3.11, pytest, numpy, sqlite3, pydantic-settings, worktree `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/`. Python interpreter: `/opt/homebrew/Caskroom/miniconda/base/bin/python`. Tests run from `backend/`.

---

## Conventions (read before starting)

- **Run tests from `backend/`:** `/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/<file>::<test> -q`
- **Avoid real LLM/embedding:** inject stub `llm_client` (`chat_json(messages, schema_hint, **kwargs) -> str`, `.configured: bool`) and `FakeEmbedder(dim=16)` from `app/services/embedding.py`.
- **Temp DB:** `tmp_path` + `monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")`, `monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/'s'))`, `monkeypatch.setenv("LLM_LOG_ENABLED", "false")`.
- **Commit style:** `fix(eval): ...` / `feat(core): ...` / `fix(retrieval): ...` with trailing `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

---

## Files

**Read (understand before any edit):**
- `backend/app/eval/speed.py` — `_insert_source` (line 71–94) and `_cleanup` (lines 97–107) both use `repo._connect()` directly; `measure_speed` calls both.
- `backend/app/eval/db.py` — `EvalDB` is its own class with its own read-only `_connect`; NOT `repo._connect`. Its `objects`, `relation_degree`, `source_titles` methods are the read queries the repo needs to surface.
- `backend/app/core/llm.py` — `OpenAICompatibleClient._get_cache` (lines 52–63): lazy-constructs `LLMCache` when `llm_cache_enabled`; stores it in `self._cache`. `chat_json` calls `_get_cache()` (lines 122–130) for hit check and writes back (lines 203–206).
- `backend/app/core/llm_cache.py` — `LLMCache` class (lines 26–59): `__init__`, `_connect`, `get(key)->Optional[str]`, `put(key, value)`. Fully functional already. This is the **only** cache backend today.
- `backend/app/services/sqlite_repository.py` — `_rrf_scored` (lines 3582–3649): takes `knowledge_sims: Optional[Dict[str, float]]` but **not** `element_sims`. Relevance computed at lines 3631–3633: `relevance = _fuse(keyword_score(...), sims.get(oid, 0.0), has_vec)` where `has_vec = oid in sims`. Element sims are never consulted.
- `backend/app/services/sqlite_repository.py` — `_retrieve_scored` (lines 3119–3150): non-RRF path. Computes both `element_sims` and `knowledge_sims` (lines 3136–3137), passes both to `score_knowledge` (line 3145).
- `backend/app/services/sqlite_repository.py` — the `ask` non-RRF branch (lines 3259–3284): same pattern — computes `element_sims` at line 3260, passes to `score_knowledge` at line 3281.
- `backend/app/services/retrieval.py` — `score_knowledge` (lines 284–363): iterates evidence, takes `max(semantic, element_sims[eid])` for each evidence element (lines 331–342). `_fuse` (lines 271–281): normalized weighted sum.
- `backend/app/services/repository.py` — `NotebookRepository` Protocol (lines 47–138): current public surface. `delete_notebook` exists; no public `eval_objects`, `eval_relation_degree`, `eval_source_titles`, `eval_insert_source_for_test`, `eval_cleanup_notebook` methods.

**Create:**
- `backend/tests/test_trackA_eval_connect.py` — T1 tests
- `backend/tests/test_trackA_cache_backend.py` — T2 tests
- `backend/tests/test_trackA_rrf_relevance.py` — T3 tests

**Modify:**
- `backend/app/services/sqlite_repository.py` — add 3 public methods (T1) + fix `_rrf_scored` signature and body (T3)
- `backend/app/services/repository.py` — add the 3 new method stubs to the `NotebookRepository` Protocol (T1)
- `backend/app/core/llm_cache.py` — add `CacheBackend` Protocol and `NoCacheBackend` class (T2)
- `backend/app/core/llm.py` — accept injected `CacheBackend` in `__init__` (T2)
- `backend/app/eval/speed.py` — remove `repo._connect()` from `_insert_source` and `_cleanup`; replace with new public methods (T1)

---

## Task 1: Close eval `_connect` leaks

**Why:** `speed.py:_insert_source` (line 79) and `speed.py:_cleanup` (lines 98–107) reach into `repo._connect()` directly, bypassing the public Protocol and the repo's write-serialisation path (`_write`). `_cleanup` manually deletes the same tables that `delete_notebook`'s FK cascade already handles. `EvalDB._connect` (in `db.py`) opens a **separate read-only** connection for a **separate class** — that is intentional and is NOT changed.

**Design:** Add three public methods to `SQLiteRepository` (and matching stubs to the `NotebookRepository` Protocol):
1. `eval_objects(notebook_id, object_type)` — wraps the same query as `EvalDB.objects` but uses `repo._connect()`. (Needed so eval orchestrator scripts can use the repo directly without a separate `EvalDB`; distinct from `EvalDB` which targets an arbitrary `db_path`.)
2. `eval_insert_source_for_test(notebook_id, name, text, tmpdir)` — the body of `_insert_source`; uses `_write` instead of `_connect`.
3. No new `eval_cleanup_notebook` needed: `delete_notebook` already cascades all child tables (FK `ON DELETE CASCADE` is enabled at line 190 `PRAGMA foreign_keys = ON`). The `_cleanup` body in `speed.py` is fully redundant — `_cleanup(repo, nb_id)` is replaced by `repo.delete_notebook(nb_id)`.

After the change, `app/eval/speed.py` calls only `repo.create_notebook`, `repo.eval_insert_source_for_test`, `repo._run_extraction`, and `repo.delete_notebook`. The gate is: `grep -rn "_connect(" backend/app/eval` returns no matches.

### Step 1: Write failing tests

Create `backend/tests/test_trackA_eval_connect.py`:

```python
"""T1: eval _connect leaks — public method coverage + grep gate."""
import inspect
import app.eval.speed as speed_mod


def test_insert_source_does_not_use_connect():
    """_insert_source must not call repo._connect() at all."""
    src = inspect.getsource(speed_mod._insert_source)
    assert "_connect" not in src, "_insert_source still calls _connect()"


def test_cleanup_does_not_use_connect():
    """_cleanup must not call repo._connect() at all."""
    src = inspect.getsource(speed_mod._cleanup)
    assert "_connect" not in src, "_cleanup still calls _connect()"


def test_repo_has_eval_insert_source_for_test():
    from app.services.sqlite_repository import SQLiteRepository
    assert hasattr(SQLiteRepository, "eval_insert_source_for_test"), \
        "SQLiteRepository missing eval_insert_source_for_test"


def test_protocol_has_eval_insert_source_for_test():
    from app.services.repository import NotebookRepository
    assert hasattr(NotebookRepository, "eval_insert_source_for_test"), \
        "NotebookRepository Protocol missing eval_insert_source_for_test"


def test_cleanup_delegates_to_delete_notebook(tmp_path, monkeypatch):
    """_cleanup(repo, nb_id) must call repo.delete_notebook(nb_id), not raw SQL."""
    import app.eval.speed as speed_mod
    deleted = []

    class FakeRepo:
        def delete_notebook(self, nb_id):
            deleted.append(nb_id)

    speed_mod._cleanup(FakeRepo(), "nb-test")
    assert deleted == ["nb-test"]
```

### Step 2: Run — confirm failure

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/backend && \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_trackA_eval_connect.py -q
```

Expected: FAIL — `test_insert_source_does_not_use_connect` and `test_cleanup_does_not_use_connect` fail because `_connect` is still present; `test_repo_has_eval_insert_source_for_test` fails because the method does not exist yet; `test_cleanup_delegates_to_delete_notebook` fails because `_cleanup` still does raw SQL.

### Step 3: Implement

**3a.** In `backend/app/services/sqlite_repository.py`, add after `delete_notebook_kg` (around line 807):

```python
def eval_insert_source_for_test(
    self, nb_id: str, name: str, text: str, tmpdir: str
) -> str:
    """Insert a parsed source directly for eval speed tests.
    Uses the repo's write path; avoids raw _connect access in eval scripts."""
    import pathlib, uuid
    from app.services.kg.parsing import parse_elements
    f = pathlib.Path(tmpdir) / f"{name}.md"
    f.write_text(text, encoding="utf-8")
    sid = f"src-{uuid.uuid4().hex[:10]}"
    now = _now()
    els = parse_elements(text, source_file=str(f))
    with self._write() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, ?, 'markdown', 'extracted', 'parsed', ?, ?, 0, '', '', ?, ?, ?)""",
            (sid, nb_id, name, f"{name}.md", str(f), "textbook", now, now))
        for el in els:
            db.execute(
                """INSERT INTO source_elements
                   (id, source_id, element_type, location_label, text, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, '{}', ?)""",
                (f"el-{uuid.uuid4().hex[:10]}", sid, el.type,
                 f"L{el.line_start}-{el.line_end}", el.text, now))
    return sid
```

**3b.** In `backend/app/services/repository.py`, add to the `NotebookRepository` Protocol body (after `delete_notebook`):

```python
def eval_insert_source_for_test(
    self, nb_id: str, name: str, text: str, tmpdir: str
) -> str: ...
```

**3c.** In `backend/app/eval/speed.py`:
- Replace `_insert_source` body: remove `with repo._connect() as db: ...` block; call `return repo.eval_insert_source_for_test(nb_id, name, text, tmpdir)`.
- Replace `_cleanup` body: replace the entire `with repo._connect() as db: ...` block with a single call: `repo.delete_notebook(nb_id)`.

### Step 4: Run — confirm green

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/backend && \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_trackA_eval_connect.py -q
```

Expected: 5 PASSED.

### Step 5: Grep gate

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/backend && \
  grep -rn "_connect(" app/eval
```

Expected: no output (zero matches).

### Step 6: Regression — existing eval tests still pass

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/backend && \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/eval/ -q
```

Expected: all green (the existing `test_speed.py` and `test_db.py` tests do not test `_insert_source`/`_cleanup` directly so they pass unchanged).

### Step 7: Commit

```
git commit -m "$(cat <<'EOF'
refactor(eval): route speed/_cleanup off repo._connect via public methods

Add SQLiteRepository.eval_insert_source_for_test (uses _write path).
Replace _cleanup with repo.delete_notebook (FK cascade covers all tables).
NotebookRepository Protocol gains the new stub.
grep -rn "_connect(" app/eval is now empty.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

**Task 1 gate:** `grep -rn "_connect(" backend/app/eval` is empty; `pytest tests/eval/ -q` all green.

---

## Task 2: Extract `CacheBackend` Protocol

**Why:** `OpenAICompatibleClient._get_cache` (lines 52–63) hardcodes `LLMCache` construction inline. Tests that exercise the cache hit/miss path must either create a real SQLite file or monkeypatch a private attribute. A `CacheBackend` Protocol with a `NoCacheBackend` sentinel makes injection clean and lets unit tests verify cache semantics without touching `_get_cache`.

**Design:**
- Add `CacheBackend` Protocol (`get(key: str) -> Optional[str]`, `put(key: str, value: str) -> None`) and `NoCacheBackend` (returns `None` from `get`, `put` is no-op) to `llm_cache.py`.
- `OpenAICompatibleClient.__init__` gains an optional `cache: Optional[CacheBackend] = None` parameter. When supplied it is stored directly as `self._cache`; when omitted the lazy-construction path in `_get_cache` is unchanged.
- `_get_cache` becomes: if `self._cache is not None: return self._cache`; then the existing lazy-construct block (unchanged).

### Step 1: Write failing tests

Create `backend/tests/test_trackA_cache_backend.py`:

```python
"""T2: CacheBackend Protocol + NoCacheBackend + injection into OpenAICompatibleClient."""
from app.core.llm_cache import CacheBackend, LLMCache, NoCacheBackend, cache_key


def test_no_cache_backend_is_always_miss():
    nc = NoCacheBackend()
    k = cache_key("m", [{"role": "user", "content": "x"}], "{}")
    assert nc.get(k) is None
    nc.put(k, "value")     # no-op; must not raise
    assert nc.get(k) is None


def test_llm_cache_satisfies_protocol(tmp_path):
    """LLMCache must be recognised as a CacheBackend at runtime."""
    lc = LLMCache(str(tmp_path / "c.db"))
    assert isinstance(lc, CacheBackend)


def test_no_cache_satisfies_protocol():
    assert isinstance(NoCacheBackend(), CacheBackend)


def test_injected_cache_used_for_hit(monkeypatch, tmp_path):
    """When a CacheBackend is injected, a pre-seeded hit is returned without
    calling the real LLM."""
    import json
    from app.core.config import Settings
    from app.core.llm import OpenAICompatibleClient
    from app.core.llm_cache import LLMCache, cache_key

    path = str(tmp_path / "c.db")
    lc = LLMCache(path)
    # Pre-seed a hit
    client = OpenAICompatibleClient.__new__(OpenAICompatibleClient)
    # Build the exact key chat_json builds (system message is prepended)
    schema_hint = "{answer: str}"
    model = "test-model"
    user_msg = {"role": "user", "content": "what is X?"}
    system_msg = {
        "role": "system",
        "content": (
            "You are the extraction and reasoning engine for "
            "silicon-notebook. Return valid JSON only, no markdown fences. "
            f"Schema hint: {schema_hint}"
        ),
    }
    key = cache_key(model, [system_msg, user_msg], schema_hint)
    lc.put(key, '{"answer": "cached"}')

    # Construct client with injected cache; model/url/key must look configured
    settings = Settings(
        openai_compat_base_url="http://fake",
        openai_compat_api_key="fake",
        openai_compat_model=model,
        llm_cache_enabled=False,   # disable auto-construct path; we inject
    )
    injected_client = OpenAICompatibleClient(settings, cache=lc)
    assert injected_client._cache is lc

    # chat_json must return the cached value without touching the network
    result = injected_client.chat_json([user_msg], schema_hint)
    assert json.loads(result)["answer"] == "cached"


def test_no_cache_backend_injection_skips_cache(monkeypatch, tmp_path):
    """Injecting NoCacheBackend means every call is a cache miss (no SQLite file)."""
    from app.core.config import Settings
    from app.core.llm import OpenAICompatibleClient
    from app.core.llm_cache import NoCacheBackend

    settings = Settings(
        openai_compat_base_url="http://fake",
        openai_compat_api_key="fake",
        openai_compat_model="m",
        llm_cache_enabled=False,
    )
    client = OpenAICompatibleClient(settings, cache=NoCacheBackend())
    # _get_cache must return the injected NoCacheBackend
    assert isinstance(client._get_cache(), NoCacheBackend)
```

### Step 2: Run — confirm failure

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/backend && \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_trackA_cache_backend.py -q
```

Expected: FAIL — `ImportError: cannot import name 'CacheBackend'` / `ImportError: cannot import name 'NoCacheBackend'`; the `cache=` kwarg on `OpenAICompatibleClient.__init__` does not exist yet.

### Step 3: Implement

**3a.** In `backend/app/core/llm_cache.py`, add at the top (after imports, before `cache_key`):

```python
from typing import Optional, runtime_checkable
from typing import Protocol as _Protocol


@runtime_checkable
class CacheBackend(_Protocol):
    def get(self, key: str) -> Optional[str]: ...
    def put(self, key: str, value: str) -> None: ...


class NoCacheBackend:
    """Always-miss backend for use in tests or when cache is explicitly disabled."""
    def get(self, key: str) -> Optional[str]:
        return None

    def put(self, key: str, value: str) -> None:
        pass
```

**3b.** In `backend/app/core/llm.py`, update `OpenAICompatibleClient.__init__` signature to add the optional `cache` parameter (after `max_retries`):

```python
def __init__(self, settings: Settings, *, base_url: Optional[str] = None,
             api_key: Optional[str] = None, model: Optional[str] = None,
             max_retries: Optional[int] = None,
             cache: Optional["CacheBackend"] = None):
```

And in the body, after `self._cache = None`, add:

```python
if cache is not None:
    self._cache = cache
```

Update `_get_cache` to short-circuit when `self._cache` is already set (it already does `if self._cache is None: ...` inside the lazy block — but the outer guard checks `llm_cache_enabled` first, which would return `None` if disabled even when injected). Replace the method body:

```python
def _get_cache(self):
    if self._cache is not None:
        return self._cache
    if not getattr(self.settings, "llm_cache_enabled", False):
        return None
    from pathlib import Path
    from app.core.llm_cache import LLMCache
    path = self.settings.llm_cache_path
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).resolve().parents[3] / path
    self._cache = LLMCache(str(p))
    return self._cache
```

Add the import at the top of `llm.py` (or keep it local — the `CacheBackend` type hint can be a string annotation; add `from app.core.llm_cache import CacheBackend` inside `__init__` or at module top).

### Step 4: Run — confirm green

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/backend && \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_trackA_cache_backend.py -q
```

Expected: 5 PASSED.

### Step 5: Regression — existing cache tests still pass

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/backend && \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_llm_cache.py -q
```

Expected: all green.

### Step 6: Commit

```
git commit -m "$(cat <<'EOF'
feat(core): CacheBackend Protocol + NoCacheBackend + injectable cache

Add CacheBackend (@runtime_checkable Protocol) and NoCacheBackend to
llm_cache.py. OpenAICompatibleClient.__init__ accepts optional cache=
injection; _get_cache short-circuits if already set. Existing lazy-
construct path and llm_cache_enabled flag are preserved unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

**Task 2 gate:** `pytest tests/test_trackA_cache_backend.py tests/test_llm_cache.py -q` all green; `_get_cache` still initialises `LLMCache` lazily when no injection and `llm_cache_enabled=True`.

---

## Task 3: Fix `_rrf_scored` dual-index relevance gap

**Why / Bug description:** The RRF branch in `ask` (line 3267: `scored_all = self._rrf_scored(query, kg_objs, knowledge_sims)`) computes `relevance` using only `knowledge_sims` (the object-level embedding). The non-RRF branch and `_retrieve_scored` both pass `element_sims` to `score_knowledge`, which then takes `max(knowledge_sim, max(element_sims[eid] for ev in evidence))`. The RRF branch's `_rrf_scored` (lines 3631–3633) uses only `sims.get(oid, 0.0)` — it never consults element-level similarities. This violates the dual-index invariant for the `relevance` field (the `score` field is RRF-based, which is intentional; the `relevance` field must reflect the best-of for `classify_evidence`'s tau thresholds). An object that is only reachable via an evidence-element embedding (no object-level embedding) will have `has_vec=False` and `relevance` computed keyword-only, causing `classify_evidence` to downgrade it to `inferred` even though it is semantically grounded.

**Root cause (exact lines):**
- `_rrf_scored` signature (line 3582): `knowledge_sims: Optional[Dict[str, float]]` — no `element_sims` parameter.
- Relevance computation (lines 3631–3633):
  ```python
  has_vec = oid in sims
  relevance = _fuse(keyword_score(query, text_by_id.get(oid, "")),
                    sims.get(oid, 0.0), has_vec)
  ```
  `element_sims` is not consulted at all.
- Call site (line 3267): `self._rrf_scored(query, kg_objs, knowledge_sims)` — `element_sims` is computed at line 3260 but not passed.

**Fix:** Add `element_sims: Optional[Dict[str, float]]` to `_rrf_scored`'s signature. Inside the loop, after computing `has_vec`/`semantic` from `knowledge_sims`, iterate each evidence element's `element_id` and take `max(semantic, element_sims[eid])` exactly as `score_knowledge` does (lines 331–342 of `retrieval.py`). Update the call site to pass `element_sims`.

### Step 1: Write failing regression test

Create `backend/tests/test_trackA_rrf_relevance.py`:

```python
"""T3: _rrf_scored dual-index relevance — element-sim must feed relevance."""
import pytest
from unittest.mock import patch
from app.services.retrieval import Evidence, RetrievedKnowledge


def _make_repo(tmp_path, monkeypatch):
    """Minimal SQLiteRepository with tmp DB, no LLM, no embedder."""
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    from app.services.embedding import FakeEmbedder

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings(retrieval_rrf_k=60, retrieval_rrf_enabled=True)
    repo = SQLiteRepository(settings)
    repo.embedder = FakeEmbedder(dim=16)
    return repo


def test_element_sim_raises_relevance_above_keyword_only(tmp_path, monkeypatch):
    """Object with no knowledge-level embedding but strong element-level sim must
    get relevance > keyword-only score, i.e. has_vec=True and semantic reflects
    the element sim.

    Setup: one object with element_id='el-A'.
      knowledge_sims = {}          (no object-level vector hit)
      element_sims  = {'el-A': 0.9}  (strong element-level hit)

    Expected: relevance > pure keyword score (which would be ~0 for a query
    that does not overlap the payload tokens).
    """
    repo = _make_repo(tmp_path, monkeypatch)
    ev = Evidence(
        element_id="el-A",
        source_id="src-x",
        quoted_span="cascode stage Miller effect compensation",
        location_label="L1",
    )
    obj = {
        "id": "obj-1",
        "payload": {"name": "cascode"},
        "evidence": [ev],
        "status": "approved",
        "owner": "",
        "last_reviewed": "",
    }
    kg_objs = {"concept": [obj], "relation": [], "procedure": [], "parameter": []}
    knowledge_sims = {}          # NO object-level vector
    element_sims = {"el-A": 0.9}  # strong element-level match

    results = repo._rrf_scored("zzz_no_keyword_match_zzz", kg_objs,
                                knowledge_sims, element_sims=element_sims)
    assert results, "expected at least one result"
    hit = next((r for r in results if r.object_id == "obj-1"), None)
    assert hit is not None, "obj-1 not in results"

    # With element sim 0.9 injected, relevance must exceed pure keyword score.
    # A keyword-only fuse (has_vec=False) of a non-matching query gives ~0.
    assert hit.relevance > 0.05, (
        f"relevance {hit.relevance:.4f} too low — element_sims not used"
    )


def test_knowledge_sim_still_works_in_rrf_path(tmp_path, monkeypatch):
    """Regression: object with only knowledge-level sim must still score correctly."""
    repo = _make_repo(tmp_path, monkeypatch)
    obj = {
        "id": "obj-2",
        "payload": {"name": "miller"},
        "evidence": [],
        "status": "approved",
        "owner": "",
        "last_reviewed": "",
    }
    kg_objs = {"concept": [obj], "relation": [], "procedure": [], "parameter": []}
    knowledge_sims = {"obj-2": 0.85}
    element_sims = {}

    results = repo._rrf_scored("miller compensation", kg_objs,
                                knowledge_sims, element_sims=element_sims)
    hit = next((r for r in results if r.object_id == "obj-2"), None)
    assert hit is not None
    assert hit.relevance > 0.1
```

### Step 2: Run — confirm failure

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/backend && \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_trackA_rrf_relevance.py -q
```

Expected: FAIL — `TypeError: _rrf_scored() got an unexpected keyword argument 'element_sims'` (the parameter does not exist yet); or after a first attempt to add the param, `test_element_sim_raises_relevance_above_keyword_only` fails because `relevance` is still keyword-only when `knowledge_sims={}`.

### Step 3: Implement

In `backend/app/services/sqlite_repository.py`, update `_rrf_scored`:

**3a.** Add `element_sims: Optional[Dict[str, float]] = None` to the signature after `knowledge_sims`:

```python
def _rrf_scored(
    self,
    query: str,
    kg_objs: Dict[str, List[dict]],
    knowledge_sims: Optional[Dict[str, float]],
    element_sims: Optional[Dict[str, float]] = None,
) -> List[RetrievedKnowledge]:
```

**3b.** Replace the `has_vec` / `relevance` computation block (lines 3631–3633) with the best-of logic from `score_knowledge`:

```python
# Best-of: object-level sim OR max(element-level sims), same as score_knowledge.
semantic = sims.get(oid, 0.0)
has_vec = oid in sims
obj_evidence = id_to_obj.get(oid, {}).get("evidence", [])
if element_sims:
    for ev in obj_evidence:
        eid = getattr(ev, "element_id", "") or ""
        s = element_sims.get(eid)
        if s is not None:
            has_vec = True
            semantic = max(semantic, s)
relevance = _fuse(keyword_score(query, text_by_id.get(oid, "")),
                  semantic, has_vec)
```

**3c.** Update the call site (line 3267 in the `ask` method):

```python
scored_all = self._rrf_scored(query, kg_objs, knowledge_sims,
                              element_sims=element_sims)
```

(Note: `element_sims` is already computed at line 3260 — no new computation needed.)

### Step 4: Run — confirm green

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/backend && \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_trackA_rrf_relevance.py -q
```

Expected: 2 PASSED.

### Step 5: Regression — RRF and retrieval tests still pass

```
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution/backend && \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_bm25_rrf.py tests/test_ask_redesign.py tests/test_followup_retrieval_grounding.py -q
```

Expected: all green.

### Step 6: Commit

```
git commit -m "$(cat <<'EOF'
fix(retrieval): _rrf_scored relevance uses dual-index best-of (element_sims)

_rrf_scored previously computed relevance from knowledge_sims only,
ignoring element_sims — breaking the dual-index invariant for classify_evidence.
Now takes element_sims parameter and applies max(knowledge_sim,
max(element_sims[eid] for ev in evidence)), matching score_knowledge exactly.
Call site in ask() passes the element_sims already computed at line 3260.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

**Task 3 gate:** `pytest tests/test_trackA_rrf_relevance.py -q` passes; `pytest tests/test_bm25_rrf.py tests/test_ask_redesign.py -q` still green.

---

## Phase Gate (run after all three tasks)

All of the following must pass before the track is considered complete:

```bash
# 1. Zero _connect leaks in eval
grep -rn "_connect(" backend/app/eval
# Expected: no output

# 2. Full eval test suite
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/eval/ -q
# Expected: all green

# 3. All three new test files
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_trackA_eval_connect.py \
  tests/test_trackA_cache_backend.py \
  tests/test_trackA_rrf_relevance.py -q
# Expected: all green

# 4. Cache parity — existing cache tests
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_llm_cache.py -q
# Expected: all green

# 5. RRF / retrieval regression
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_bm25_rrf.py tests/test_ask_redesign.py \
  tests/test_followup_retrieval_grounding.py -q
# Expected: all green

# 6. Full suite smoke
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/ -q --tb=short
# Expected: all green, grounded/overview/inferred distribution unchanged
```

**Invariant checks (must not regress):**
- `[0,1]/tau`: `_fuse` is called with `semantic` that now includes element-sim max; `_fuse` output is still in `[0,1]` by construction (weighted average of `[0,1]` inputs). `classify_evidence` tau thresholds (0.18 grounded, 0.35 inferred) are not touched.
- **Dual-index best-of**: both RRF and non-RRF paths now compute `relevance` as `max(knowledge_sim, max(element_sims))` before fusing. The `score` field in the RRF path remains the RRF micro-score (ordering only) — untouched.
- **EvalDB**: `EvalDB._connect` (in `app/eval/db.py`) opens its own read-only connection to an arbitrary `db_path` — this is a separate class and is NOT repo access. It is intentionally untouched.
