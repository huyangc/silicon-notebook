# Phase 1: KG/检索效果提升 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不引入新基建的前提下,通过 LLM 响应缓存、抽取自校验、检索上下文预算化装配、in-network 关系入上下文、LLM 重排、检索召回评测六项改动,提升 silicon-notebook 的问答效果与抽取可靠性。

**Architecture:** 全部改动落在现有 `backend/app` 内,沿用项目既有模式:手写 `sqlite3`(无 ORM)、pydantic-settings 单例、LLM 走 `OpenAICompatibleClient.chat_json(messages, schema_hint) -> JSON str`、测试用「注入替身 client/embedder + tmp sqlite」避开网络。新增一个 SQLite KV 缓存模块与一个 eval 指标模块,其余为对现有函数的就地增强。每个任务独立可提交、独立可测。

**Tech Stack:** Python 3.11、pytest、numpy、sqlite3、pydantic-settings、openai SDK(URL 端点)。

**来源依据:** 本计划所有"现状"行号引自对当前 worktree 的逐字核对(`backend/app` 为准)。参考方法来自 `/Users/hzf/workspace/ref-kg/`(GraphRAG + ToG-3),见 `docs/superpowers/plans` 同目录的分析记录与记忆 `ref-kg-borrow-roadmap`。

---

## 约定(Conventions,执行前必读)

- **跑测试目录:** 从 `backend/` 跑。单测命令:`cd backend && python -m pytest tests/<file>::<test> -q`(从 `backend/` 跑无需显式 `PYTHONPATH`,pytest 会把 rootdir 插入 sys.path)。如默认 `python` 缺依赖,用共享解释器:`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest ...`。
- **整体编译/烟雾 gate(非 pytest):** `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`(py_compile + import + smoke + 前端 lint)。
- **避开真实 LLM:** 不要 monkeypatch 全局;**注入替身**。仓库路径:替 `repo.llm_client`,接口契约 `chat_json(messages, schema_hint, **kwargs) -> JSON str`,并提供 `.configured: bool`。KG 抽取路径:把本地 `class Fake`(实现 `chat_json(self, messages, response_schema_hint)`,需要时加 `.configured`)作为首参传给 `extract_window` / `refine_nodes`。
- **避开真实 embedding:** `repo.embedder = FakeEmbedder(dim=16)`(`app/services/embedding.py:18`,确定性 hash 向量)。
- **临时 DB:** `tmp_path` + `monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")`;`monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/'s'))`;`monkeypatch.setenv("LLM_LOG_ENABLED", "false")`。准备 KG 数据用 `repo.store_kg(nb_id, None, objects, relations)` 绕过抽取。参照 `backend/tests/test_reasoning_retrieval.py:77-103`。
- **提交:** 每个任务末尾提交一次。提交信息用项目风格的中文 conventional commit(如 `feat(kg): ...`、`feat(retrieval): ...`、`feat(eval): ...`),并在结尾追加一行
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- **新增 config 字段:** 统一加在 `backend/app/core/config.py` 的 `Settings` 类体内(LLM/embed 字段段之后,`config.py:43` 附近),用 `Field(default, env="...")`。

## 基线(Task 0,非代码,先做)

- [ ] **捕获 before 基线**:在 ROOT 仓 master 上,对现有 innovus/教材 notebook 跑一次现有端到端评测,留存数字供 Phase 1 完成后对比。
  Run(从仓库根):`PYTHONPATH=backend python -m app.eval.run_all --notebook <nb-id> --only inference`
  把生成的 `inference_report.md` 另存为 `before_inference.md`。无需改任何代码。

---

## Task 1: C8 — LLM 响应 SQLite 缓存

**为何:** 抽取逐窗调用 LLM 是最贵且高度可复用的步骤;缓存后重抽近零成本(直击「抽取超时/重试昂贵」痛点),也为后续 C6/重抽实验铺路。键含完整 prompt(prompt 内嵌检索上下文),输入一变键即变,安全。

**并发与隔离(关键设计约束,勿改):** 缓存必须是**独立文件** `.local/llm_cache.db`,与 KG 主库 `.local/silicon_notebook.db` 物理分离。理由:SQLite 锁是**按文件**的,独立文件 = 独立写锁,故缓存写**不与 KG 库写争锁**、不经过 `SQLiteRepository._write` 串行写路径、不会加重 KG 库既有写竞争。**严禁把缓存表并入主库**(那会增加主库单写者竞争)。缓存写为单行小 INSERT,频率受 LLM 时延钳制(16 worker × 每次等秒级 LLM ≈ 每秒个位数次写),WAL + 自带 `threading.Lock` + `busy_timeout=30000` 足以避免 `database is locked`;每线程在本线程内开独立连接(不跨线程共享)。缓存命中路径(重抽取主收益)是**只读、无写、无 LLM 调用**,反而降低整体负载。

**Files:**
- Create: `backend/app/core/llm_cache.py`
- Create: `backend/tests/test_llm_cache.py`
- Modify: `backend/app/core/config.py`(加 2 个字段)
- Modify: `backend/app/core/llm.py:36-179`(`OpenAICompatibleClient` 接入缓存)

- [ ] **Step 1: 写失败测试(纯缓存类)**

Create `backend/tests/test_llm_cache.py`:
```python
from app.core.llm_cache import LLMCache, cache_key


def test_cache_key_is_stable_and_order_independent():
    m = [{"role": "user", "content": "hi"}]
    k1 = cache_key("model-x", m, "{}")
    k2 = cache_key("model-x", [{"content": "hi", "role": "user"}], "{}")
    assert k1 == k2                      # dict key order must not matter
    assert k1 != cache_key("model-y", m, "{}")   # model is part of the key


def test_put_get_roundtrip_and_miss(tmp_path):
    c = LLMCache(str(tmp_path / "c.db"))
    k = cache_key("m", [{"role": "user", "content": "q"}], "{}")
    assert c.get(k) is None              # miss
    c.put(k, '{"a": 1}')
    assert c.get(k) == '{"a": 1}'        # hit


def test_persists_across_instances(tmp_path):
    path = str(tmp_path / "c.db")
    k = cache_key("m", [{"role": "user", "content": "q"}], "{}")
    LLMCache(path).put(k, "cached")
    assert LLMCache(path).get(k) == "cached"   # second instance, same file
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_llm_cache.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.core.llm_cache'`

- [ ] **Step 3: 实现缓存模块**

Create `backend/app/core/llm_cache.py`:
```python
"""Content-addressed SQLite cache for OpenAICompatibleClient.chat_json responses.
Key = sha256(model + messages + schema_hint). Identical extraction/ask prompts
return the cached JSON string instead of re-calling the endpoint. Safe: the key
embeds the full prompt (which carries any retrieved context), so any input change
yields a new key. WAL + serialized writes for the concurrent extraction pool."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional


def cache_key(model: str, messages: List[Dict[str, str]], schema_hint: str) -> str:
    payload = json.dumps(
        {"model": model, "messages": messages, "schema": schema_hint},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LLMCache:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS llm_cache ("
                "key TEXT PRIMARY KEY, response TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def get(self, key: str) -> Optional[str]:
        with self._connect() as db:
            row = db.execute(
                "SELECT response FROM llm_cache WHERE key = ?", (key,)
            ).fetchone()
        return row["response"] if row else None

    def put(self, key: str, response: str) -> None:
        with self._lock:
            with self._connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO llm_cache (key, response) VALUES (?, ?)",
                    (key, response),
                )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_llm_cache.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: 加 config 字段**

In `backend/app/core/config.py`, after the embed fields (around `config.py:43`, before `embed_truncate_chars`), add:
```python
    llm_cache_enabled: bool = Field(True, env="LLM_CACHE_ENABLED")
    llm_cache_path: str = Field(".local/llm_cache.db", env="LLM_CACHE_PATH")
```

- [ ] **Step 6: 写失败测试(client 接入缓存)**

Append to `backend/tests/test_llm_cache.py`:
```python
from app.core.config import Settings
from app.core.llm import OpenAICompatibleClient


class _FakeCompletions:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls += 1
        msg = type("M", (), {"content": '{"ok": 1}'})()
        choice = type("C", (), {"message": msg})()
        return type("R", (), {"choices": [choice], "usage": None})()


class _FakeOpenAI:
    def __init__(self):
        self.calls = 0
        self.chat = type("Ch", (), {"completions": _FakeCompletions(self)})()


def _configured_client(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://llm.example.test")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "k")
    monkeypatch.setenv("OPENAI_COMPAT_MODEL", "m")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_CACHE_PATH", str(tmp_path / "cache.db"))
    client = OpenAICompatibleClient(Settings())
    fake = _FakeOpenAI()
    monkeypatch.setattr(client, "client", lambda: fake)
    return client, fake


def test_chat_json_caches_identical_calls(tmp_path, monkeypatch):
    client, fake = _configured_client(tmp_path, monkeypatch)
    msgs = [{"role": "user", "content": "same question"}]
    r1 = client.chat_json(msgs, "{}")
    r2 = client.chat_json(msgs, "{}")
    assert r1 == r2 == '{"ok": 1}'
    assert fake.calls == 1               # second call served from cache


def test_chat_json_cache_disabled(tmp_path, monkeypatch):
    client, fake = _configured_client(tmp_path, monkeypatch)
    monkeypatch.setattr(client.settings, "llm_cache_enabled", False)
    msgs = [{"role": "user", "content": "q"}]
    client.chat_json(msgs, "{}")
    client.chat_json(msgs, "{}")
    assert fake.calls == 2               # no caching -> two endpoint calls
```

- [ ] **Step 7: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_llm_cache.py -k caches_identical -q`
Expected: FAIL — `assert 2 == 1`(缓存未接入,fake 被调两次)

- [ ] **Step 8: 在 client 中接入缓存**

In `backend/app/core/llm.py`:

(a) In `__init__` (`llm.py:37-40`), after `self.interaction_logger = ...`, add:
```python
        self._cache = None
```

(b) Add a helper method on the class (place right after `__init__`, before the `configured` property):
```python
    def _get_cache(self):
        if not getattr(self.settings, "llm_cache_enabled", False):
            return None
        if self._cache is None:
            from pathlib import Path
            from app.core.llm_cache import LLMCache
            path = self.settings.llm_cache_path
            p = Path(path)
            if not p.is_absolute():
                p = Path(__file__).resolve().parents[3] / path   # anchor to repo root
            self._cache = LLMCache(str(p))
        return self._cache
```

(c) In `chat_json`, after `model = self.settings.openai_compat_model` and the `full_messages = [...]` block are both built (i.e. right before `kwargs: Dict[str, Any] = {...}` at `llm.py:113`), insert the cache-lookup:
```python
        cache = self._get_cache()
        ckey = cache_key(model, full_messages, response_schema_hint) if cache else ""
        if cache and ckey:
            cached = cache.get(ckey)
            if cached is not None:
                return cached
```

(d) On the success path, right before `record["status"] = "ok"` (`llm.py:166`), insert:
```python
            if cache and ckey:
                cache.put(ckey, content)
```

(e) Add the import at the top of `llm.py` (with the other `app.core` imports, `llm.py:10-11`):
```python
from app.core.llm_cache import cache_key
```

- [ ] **Step 9: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_llm_cache.py -q`
Expected: PASS (5 passed)

- [ ] **Step 10: 回归 + 提交**

Run: `cd backend && python -m pytest tests/test_retrieval.py tests/kg -q`
Expected: PASS(确认未破坏抽取/检索)
```bash
git add backend/app/core/llm_cache.py backend/tests/test_llm_cache.py backend/app/core/config.py backend/app/core/llm.py
git commit -m "feat(llm): chat_json 响应 SQLite 缓存(键=model+messages+schema,默认开,可关)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: C6 — 抽取自校验(接通死代码 refine_prompt)

**为何:** `prompts.py:42 refine_prompt` + `REFINE_SCHEMA_HINT` 全仓无调用点(死代码)。接成抽取后的一道核验:让 LLM 对照源元素剔除「源文不支持/太空泛/只是复述标题」的节点,降低幻觉节点。默认关闭(`kg_refine_enabled=False`),不改动既有抽取行为与现有测试;验证后由配置开启。

**Files:**
- Modify: `backend/app/services/kg/extract.py`(新增 `refine_nodes`;`extract_window` 加 `refine` 形参)
- Modify: `backend/app/services/kg_ingest.py:151-186`(`extract_graph` 加 `refine` 形参并下传)
- Modify: `backend/app/services/sqlite_repository.py:1228` 附近(抽取调用处传 `refine=self.settings.kg_refine_enabled`)
- Modify: `backend/app/core/config.py`(加 `kg_refine_enabled`)
- Create: `backend/tests/kg/test_refine.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/kg/test_refine.py`:
```python
import json
from app.services.kg.extract import refine_nodes, extract_window
from app.services.kg.models import Node, Evidence
from app.services.kg.parsing import SourceElementQ


def _se(idx, text, cs):
    return SourceElementQ(id=f"SE-{idx}", type="paragraph", file="d.md",
                          line_start=idx + 1, line_end=idx + 1,
                          char_start=cs, char_end=cs + len(text), text=text)


def _node(name):
    return Node(id=f"n-{name}", type="Concept", name=name,
                evidence=[Evidence(file="d", char_start=0, char_end=1,
                                   line_start=1, line_end=1, quote="z")])


class _RefineLLM:
    """Returns a refine verdict: drop index 1, keep the rest."""
    configured = True

    def chat_json(self, messages, response_schema_hint):
        return json.dumps({"items": [
            {"index": 0, "keep": True}, {"index": 1, "keep": False},
        ]})


def test_refine_nodes_drops_rejected():
    nodes = [_node("Engram"), _node("vague thing")]
    elements = [_se(0, "Engram is a memory module.", 0)]
    out = refine_nodes(_RefineLLM(), elements, nodes)
    assert [n.name for n in out] == ["Engram"]


def test_refine_nodes_noop_when_client_unconfigured():
    class _Off:
        configured = False
        def chat_json(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("should not call LLM when unconfigured")
    nodes = [_node("A"), _node("B")]
    assert refine_nodes(_Off(), [], nodes) == nodes


class _ExtractThenRefineLLM:
    """First call (extraction schema) returns nodes; refine call drops 'filler'."""
    configured = True

    def chat_json(self, messages, response_schema_hint):
        if '"items"' in response_schema_hint:
            return json.dumps({"items": [{"index": 0, "keep": True},
                                         {"index": 1, "keep": False}]})
        return json.dumps({"nodes": [
            {"local_id": "a", "type": "Concept", "name": "analog signal", "ev": 0},
            {"local_id": "b", "type": "Concept", "name": "filler", "ev": 1}],
            "edges": []})


def test_extract_window_applies_refine_when_enabled():
    elements = [_se(0, "Analog signal is continuous.", 0), _se(1, "filler", 40)]
    nodes, _edges = extract_window(_ExtractThenRefineLLM(), elements, "1", "textbook",
                                   win_idx=0, refine=True)
    assert [n.name for n in nodes] == ["analog signal"]   # 'filler' refined away
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/kg/test_refine.py -q`
Expected: FAIL — `ImportError: cannot import name 'refine_nodes'`

- [ ] **Step 3: 实现 refine_nodes 并接入 extract_window**

In `backend/app/services/kg/extract.py`:

(a) Add imports near the top (with the other `app.services` imports, `extract.py:5-7`):
```python
from app.services.prompts import refine_prompt, REFINE_SCHEMA_HINT
```

(b) Add the function (place after `_parse_steps`, before `extract_window`, ~`extract.py:108`):
```python
def refine_nodes(client: Any, elements: List[SourceElementQ], nodes: List[Node],
                 source_title: str = "") -> List[Node]:
    """Self-refinement pass: ask the LLM to drop nodes not supported by the source
    elements. No-op when there are no nodes or the client is unconfigured (so the
    deterministic / test path never calls the network). On any parse/transport
    soft-failure, returns nodes unchanged (never raises except hard transport)."""
    if not nodes or not getattr(client, "configured", False):
        return nodes
    records_block = "\n".join(f"[{i}] {n.type}: {n.name}" for i, n in enumerate(nodes))
    elements_block = "\n".join(f"[{i}] {e.text}" for i, e in enumerate(elements))
    try:
        raw = client.chat_json(
            [{"role": "user",
              "content": refine_prompt(source_title, records_block, elements_block)}],
            REFINE_SCHEMA_HINT,
        )
        data = safe_json(raw)
    except (APIConnectionError, APITimeoutError):
        raise
    except Exception:
        return nodes
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return nodes
    drop = set()
    for it in items:
        if isinstance(it, dict) and isinstance(it.get("index"), int) \
                and it.get("keep") is False:
            drop.add(it["index"])
    if not drop:
        return nodes
    return [n for i, n in enumerate(nodes) if i not in drop]
```

(c) Change `extract_window` signature (`extract.py:110-111`) to add `refine`:
```python
def extract_window(client: Any, elements: List[SourceElementQ], section_path: str,
                   doc_type: str, win_idx: int = 0, refine: bool = False) -> Tuple[List[Node], List[Edge]]:
```

(d) In `extract_window`, between the node loop and the edge loop (right after the `for it in (data.get("nodes") or []):` loop ends and before `edges: List[Edge] = []`, ~`extract.py:138`), insert:
```python
    if refine and nodes:
        kept = refine_nodes(client, elements, nodes, section_path)
        kept_ids = {n.id for n in kept}
        nodes = kept
        by_local = {lid: nid for lid, nid in by_local.items() if nid in kept_ids}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/kg/test_refine.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: 下传 refine 到编排层 + config**

In `backend/app/core/config.py` (with the other kg fields), add:
```python
    kg_refine_enabled: bool = Field(False, env="KG_REFINE_ENABLED")
```

In `backend/app/services/kg_ingest.py`, change `extract_graph` signature (`kg_ingest.py:151-152`) to add `refine`:
```python
def extract_graph(client: Any, raw_text: str, source_file: str, doc_type: str,
                  n: int = 9000, m: int = 450, whitelist=frozenset(),
                  refine: bool = False) -> KnowledgeGraph:
```
And in the `submit_window(...)` call (`kg_ingest.py:170-172`), pass `refine`:
```python
        futs = [submit_window(extract_window, client, els, w.section_path,
                              doc_type, idx, refine=refine)
                for idx, (w, els) in enumerate(pairs)]
```

In `backend/app/services/sqlite_repository.py`, at the `extract_graph(...)` call site (`sqlite_repository.py:1228` 附近 `_run_extraction`), add the kwarg:
```python
        refine=self.settings.kg_refine_enabled,
```
(添加到该 `extract_graph(...)` 调用的实参列表里;其余实参保持不变。)

- [ ] **Step 6: 回归确认现有抽取测试不变(refine 默认关闭)**

Run: `cd backend && python -m pytest tests/kg -q`
Expected: PASS(`test_extract.py` / `test_canonicalize.py` 等全绿;默认 `refine=False`,行为不变)

- [ ] **Step 7: 提交**
```bash
git add backend/app/services/kg/extract.py backend/app/services/kg_ingest.py backend/app/services/sqlite_repository.py backend/app/core/config.py backend/tests/kg/test_refine.py
git commit -m "feat(kg): 接通 refine_prompt 抽取自校验(默认关闭,可配置开启)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: R1 — 答案上下文按字符预算装配

**为何:** `_answer_context`(`sqlite_repository.py:3179-3217`)当前对 `top_hits` 无条件逐条装配,只有固定 `definition[:200]`、`steps[:8]` 的硬切,无总预算——大命中集会让前几条长定义挤占全部上下文。改为「总字符预算 + 逐条按剩余预算裁定义 + 至少保 min_items 条」。沿用项目既有 char 级度量(全仓无 token 计数,见现状),预算常量后续可平滑替换为 token 估计。

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:3179-3217`(`_answer_context`)
- Modify: `backend/app/core/config.py`(加 2 个字段)
- Create: `backend/tests/test_answer_context_budget.py`

- [ ] **Step 1: 加 config 字段**

In `backend/app/core/config.py`, add:
```python
    answer_context_budget_chars: int = Field(6000, env="ANSWER_CONTEXT_BUDGET_CHARS")
    answer_context_min_items: int = Field(3, env="ANSWER_CONTEXT_MIN_ITEMS")
```

- [ ] **Step 2: 写失败测试**

Create `backend/tests/test_answer_context_budget.py`:
```python
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievedKnowledge
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _hit(i):
    return RetrievedKnowledge(object_id=f"o{i}", object_type="concept",
                              payload={"name": f"Concept {i}"}, evidence=[])


def test_answer_context_respects_char_budget(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    long_def = "x" * 5000
    # Each concept reports a 5000-char definition; without a budget the block
    # would be ~25k chars. Distinct cluster ids so none are collapsed.
    monkeypatch.setattr(repo, "_concept_cluster_id", lambda nbid, oid: oid)
    monkeypatch.setattr(repo, "node_context", lambda nbid, oid: {
        "occurrences": [{"element_text": long_def, "source_title": "S",
                         "section_path": "1"}],
        "definition": long_def, "steps": None})
    repo.settings.answer_context_budget_chars = 1000
    repo.settings.answer_context_min_items = 2
    block, id_map = repo._answer_context(nb.id, [_hit(i) for i in range(5)])
    assert len(block) <= 1000 + 5000      # bounded: at most one over-budget line
    assert len(id_map) >= 2               # min_items honored
    assert len(id_map) < 5                # not all 5 packed in


def test_answer_context_keeps_all_when_small(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    monkeypatch.setattr(repo, "_concept_cluster_id", lambda nbid, oid: oid)
    monkeypatch.setattr(repo, "node_context", lambda nbid, oid: {
        "occurrences": [{"element_text": "short", "source_title": "S",
                         "section_path": "1"}],
        "definition": "short", "steps": None})
    repo.settings.answer_context_budget_chars = 6000
    block, id_map = repo._answer_context(nb.id, [_hit(i) for i in range(3)])
    assert len(id_map) == 3               # all small hits fit
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_answer_context_budget.py -q`
Expected: FAIL — `test_answer_context_respects_char_budget` 失败(当前无预算,5 条全装入,`len(id_map) == 5`)

- [ ] **Step 4: 改 `_answer_context` 引入预算**

In `backend/app/services/sqlite_repository.py`, replace the body of `_answer_context` (`sqlite_repository.py:3187-3217`,自 `lines, id_map = [], {}` 起到 `return ...`)with:
```python
        budget = self.settings.answer_context_budget_chars
        min_items = self.settings.answer_context_min_items
        lines, id_map = [], {}
        seen_concept_clusters: set = set()
        used = 0
        i = 0
        for hit in top_hits:
            if hit.object_type == "concept":
                cid = self._concept_cluster_id(notebook_id, hit.object_id)
                if cid in seen_concept_clusters:
                    continue
                seen_concept_clusters.add(cid)
            try:
                ctx = self.node_context(notebook_id, hit.object_id)
            except KeyError:
                continue
            # Stop once the budget is spent, but always keep at least min_items.
            if used >= budget and len(lines) >= min_items:
                break
            i += 1
            key = f"k{i}"
            name = str(hit.payload.get("name", "")).strip()
            occ = ctx.get("occurrences") or []
            snippet = occ[0].get("element_text") if occ else ""
            definition = ctx.get("definition") or snippet
            remaining = max(0, budget - used)
            def_cap = max(0, min(300, remaining))   # per-line cap shrinks as budget fills
            extra = f" — def: {definition[:def_cap]}" if (definition and def_cap) else ""
            if ctx.get("steps"):
                extra += "; steps: " + " -> ".join(
                    s.get("name", "") for s in ctx["steps"][:8]
                )
            line = f"{key}: [{hit.object_type}] {name}{extra}"
            lines.append(line)
            used += len(line)
            id_map[key] = {
                "object_id": hit.object_id, "object_type": hit.object_type,
                "name": name, "definition": definition, "snippet": snippet,
                "source_title": (occ[0].get("source_title", "") if occ else ""),
                "location_label": (occ[0].get("section_path", "") if occ else ""),
            }
        return ("\n".join(lines) if lines else "(none)"), id_map
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_answer_context_budget.py -q`
Expected: PASS (2 passed)

- [ ] **Step 6: 回归 + 提交**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py -q`
Expected: PASS(确认 ask/装配链未坏)
```bash
git add backend/app/services/sqlite_repository.py backend/app/core/config.py backend/tests/test_answer_context_budget.py
git commit -m "feat(retrieval): 答案上下文按字符预算装配(替固定截断,保 min_items)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: R2 — in-network 关系入答案上下文

**为何:** 当前答案上下文**完全没有关系信息**——LLM 看不到「k1 依赖 k2」这类图结构(1-hop 邻居只进展示用 `related_knowledge`,不进答案)。GraphRAG 的关键启发:两端都已命中的「in-network」关系信息价值最高。把这些关系渲染成上下文行喂给 LLM,直接提升多跳/关系类问答质量。

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_answer_context` 末尾追加 in-network 关系行)
- Create: `backend/tests/test_in_network_relations.py`

- [ ] **Step 1: 写失败测试**

Create `backend/tests/test_in_network_relations.py`:
```python
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievedKnowledge
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_in_network_relation_surfaced_in_context(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "A", "object_type": "concept",
         "payload": {"name": "Cascode", "section_path": "1"}, "evidence": []},
        {"local_id": "B", "object_type": "concept",
         "payload": {"name": "Output Resistance", "section_path": "1"}, "evidence": []},
    ], [
        {"source_local_id": "A", "target_local_id": "B",
         "edge_type": "depends_on", "evidence": []},
    ])
    # Resolve the two stored objects' db ids, build hits over them.
    objs = repo._notebook_objects_for_test(nb.id) if hasattr(repo, "_notebook_objects_for_test") else None
    import sqlite3  # read ids directly
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? ORDER BY id",
            (nb.id,)).fetchall()
    import json
    ids = {json.loads(r["payload"])["name"]: r["id"] for r in rows}
    hits = [
        RetrievedKnowledge(object_id=ids["Cascode"], object_type="concept",
                           payload={"name": "Cascode"}, evidence=[]),
        RetrievedKnowledge(object_id=ids["Output Resistance"], object_type="concept",
                           payload={"name": "Output Resistance"}, evidence=[]),
    ]
    block, id_map = repo._answer_context(nb.id, hits)
    assert "relations:" in block
    assert "depends_on" in block
    # the relation references the two k-ids that the two hits received
    keys = list(id_map.keys())
    assert any(f"{keys[0]} -[depends_on]-> {keys[1]}" in block
               or f"{keys[1]} -[depends_on]-> {keys[0]}" in block
               for _ in [0])
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_in_network_relations.py -q`
Expected: FAIL — `assert "relations:" in block`(当前不渲染关系)

- [ ] **Step 3: 在 `_answer_context` 末尾追加 in-network 关系**

In `backend/app/services/sqlite_repository.py`, in `_answer_context`, change the final `return` so that before returning it appends in-network relation lines. Replace the closing `return ("\n".join(lines) if lines else "(none)"), id_map` with:
```python
        # In-network relations: edges whose BOTH endpoints are in the context.
        oid_to_key = {v["object_id"]: k for k, v in id_map.items()}
        if len(oid_to_key) >= 2:
            ids = list(oid_to_key)
            ph = ",".join("?" for _ in ids)
            with self._connect() as db:
                rels = db.execute(
                    f"SELECT source_object_id, target_object_id, edge_type "
                    f"FROM knowledge_relations WHERE notebook_id=? "
                    f"AND source_object_id IN ({ph}) AND target_object_id IN ({ph})",
                    [notebook_id, *ids, *ids],
                ).fetchall()
            rel_lines = []
            seen_rel = set()
            for r in rels:
                s = oid_to_key.get(r["source_object_id"])
                t = oid_to_key.get(r["target_object_id"])
                if s and t and (s, r["edge_type"], t) not in seen_rel:
                    seen_rel.add((s, r["edge_type"], t))
                    rel_lines.append(f"{s} -[{r['edge_type']}]-> {t}")
            if rel_lines:
                lines.append("relations: " + "; ".join(rel_lines))
        return ("\n".join(lines) if lines else "(none)"), id_map
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_in_network_relations.py -q`
Expected: PASS (1 passed)

- [ ] **Step 5: 回归 + 提交**

Run: `cd backend && python -m pytest tests/test_answer_context_budget.py tests/test_reasoning_retrieval.py -q`
Expected: PASS
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_in_network_relations.py
git commit -m "feat(retrieval): in-network 关系渲染进答案上下文(LLM 可见图结构)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: R5a — LLM-as-reranker

**为何:** 当前**无任何重排**(grep 全空),也无 rerank URL 端点。复用现有 `chat_json` 端点做轻量重排:对 top 候选池让 LLM 打相关性分并重排,再取 top_n。不需新基建,契合「模型只走 URL」。未配置/出错则保持原序(安全降级)。

**Files:**
- Modify: `backend/app/services/prompts.py`(加 `rerank_prompt` + `RERANK_SCHEMA_HINT`)
- Modify: `backend/app/services/sqlite_repository.py`(`ask` 内打分后插入重排;新增 `_rerank_hits`)
- Modify: `backend/app/core/config.py`(加 2 个字段)
- Create: `backend/tests/test_rerank.py`

- [ ] **Step 1: 加 config 字段**

In `backend/app/core/config.py`, add:
```python
    rerank_enabled: bool = Field(True, env="RERANK_ENABLED")
    rerank_candidates: int = Field(20, env="RERANK_CANDIDATES")
```

- [ ] **Step 2: 加 prompt**

In `backend/app/services/prompts.py`, add (near the other prompt helpers):
```python
RERANK_SCHEMA_HINT = '{"items":[{"index":0,"score":0.0}]}'


def rerank_prompt(query: str, candidates_block: str) -> str:
    return (
        "Score how relevant each candidate knowledge item is to the user question "
        "on a 0.0-1.0 scale (1.0 = directly answers it, 0.0 = irrelevant). "
        "Return JSON only: one entry per candidate index.\n\n"
        f"Question: {query}\n\n"
        f"Candidates:\n{candidates_block}"
    )
```

- [ ] **Step 3: 写失败测试**

Create `backend/tests/test_rerank.py`:
```python
import json
import pytest
from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.retrieval import RetrievedKnowledge
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _hit(i, score):
    return RetrievedKnowledge(object_id=f"o{i}", object_type="concept",
                              payload={"name": f"C{i}"}, evidence=[], score=score)


class _RerankLLM:
    configured = True

    def chat_json(self, messages, schema_hint, **kwargs):
        # Promote index 2 to the top, demote index 0.
        return json.dumps({"items": [{"index": 0, "score": 0.1},
                                     {"index": 1, "score": 0.5},
                                     {"index": 2, "score": 0.9}]})


def test_rerank_reorders_by_llm_score(repo):
    repo.llm_client = _RerankLLM()
    hits = [_hit(0, 0.9), _hit(1, 0.8), _hit(2, 0.7)]   # original order by score
    out = repo._rerank_hits("q", hits)
    assert [h.object_id for h in out] == ["o2", "o1", "o0"]


def test_rerank_noop_when_unconfigured(repo):
    class _Off:
        configured = False
        def chat_json(self, *a, **k):  # pragma: no cover
            raise AssertionError("must not call LLM")
    repo.llm_client = _Off()
    hits = [_hit(0, 0.9), _hit(1, 0.8)]
    assert repo._rerank_hits("q", hits) == hits


def test_rerank_noop_when_disabled(repo):
    repo.llm_client = _RerankLLM()
    repo.settings.rerank_enabled = False
    hits = [_hit(0, 0.9), _hit(1, 0.8)]
    assert repo._rerank_hits("q", hits) == hits
```

- [ ] **Step 4: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_rerank.py -q`
Expected: FAIL — `AttributeError: 'SQLiteRepository' object has no attribute '_rerank_hits'`

- [ ] **Step 5: 实现 `_rerank_hits` 并接入 ask**

In `backend/app/services/sqlite_repository.py`:

(a) Add the import for the rerank prompt (with the other `from app.services.prompts import ...`):
```python
from app.services.prompts import rerank_prompt, RERANK_SCHEMA_HINT
```
(若该文件已有 `from app.services.prompts import (...)` 聚合块,把这两个名字加进括号即可。)

(b) Add the method (place near `_answer_kg` / other private retrieval helpers):
```python
    def _rerank_hits(self, query: str, hits: List[RetrievedKnowledge]) -> List[RetrievedKnowledge]:
        """LLM relevance rerank over a candidate pool. No-op when disabled, the
        client is unconfigured, or the response is unusable (keeps input order)."""
        if not hits or not self.settings.rerank_enabled \
                or not getattr(self.llm_client, "configured", False):
            return hits
        block = "\n".join(
            f"[{i}] [{h.object_type}] {str(h.payload.get('name', ''))[:200]}"
            for i, h in enumerate(hits)
        )
        try:
            raw = self.llm_client.chat_json(
                [{"role": "user", "content": rerank_prompt(query, block)}],
                RERANK_SCHEMA_HINT, max_retries=0,
            )
            data = json.loads(raw)
        except Exception:
            return hits
        scores: Dict[int, float] = {}
        for it in (data.get("items") if isinstance(data, dict) else None) or []:
            if isinstance(it, dict) and isinstance(it.get("index"), int):
                try:
                    scores[it["index"]] = float(it.get("score", 0.0))
                except (TypeError, ValueError):
                    pass
        if not scores:
            return hits
        order = sorted(range(len(hits)), key=lambda i: scores.get(i, -1.0), reverse=True)
        return [hits[i] for i in order]
```

(c) In `ask`, after `scored_all.sort(key=rank_key, reverse=True)` and `top_n = self.settings.retrieval_top_n` (`sqlite_repository.py:3000-3001`), insert the rerank-then-trim:
```python
        pool = scored_all[: max(top_n, self.settings.rerank_candidates)]
        pool = self._rerank_hits(query, pool)
```
and change the subsequent `top_hits` selection (`sqlite_repository.py:3002-3006`) to consume `pool` instead of `scored_all`:
```python
        if process_intent:
            top_hits = ensure_procedure_quota(pool, top_n, self.settings.proc_min, rank_key)
        else:
            top_hits = pool[:top_n]
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_rerank.py -q`
Expected: PASS (3 passed)

- [ ] **Step 7: 回归 + 提交**

Run: `cd backend && python -m pytest tests/test_reasoning_retrieval.py tests/test_answer_context_budget.py -q`
Expected: PASS
```bash
git add backend/app/services/prompts.py backend/app/services/sqlite_repository.py backend/app/core/config.py backend/tests/test_rerank.py
git commit -m "feat(retrieval): ask 候选池 LLM 重排(复用 chat_json,未配置降级保序)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: 评测 — 检索召回指标 recall@k / MRR

**为何:** 现状只有端到端 LLM-judge(`inference.py`),检索这一段无独立量化(`questions.yaml` 也无 gold 命中标注)。补 recall@k/MRR 纯函数 + 一个读 `gold_object_ids` 的 probe + run_all 接线 + 题集 schema 扩展,让 Phase 1 的检索改动可独立度量。纯函数离线 TDD;probe 用 fake repo 离线 TDD。

**Files:**
- Create: `backend/app/eval/retrieval_metrics.py`
- Create: `backend/tests/eval/test_retrieval_metrics.py`
- Modify: `backend/app/eval/run_all.py`(加 `--only recall` 分段)
- Modify: `backend/app/eval/questions.yaml`(注释说明新增可选字段 `gold_object_ids`)

- [ ] **Step 1: 写失败测试(纯函数 + run_recall)**

Create `backend/tests/eval/test_retrieval_metrics.py`:
```python
from app.eval.retrieval_metrics import recall_at_k, mrr, run_recall


def test_recall_at_k():
    assert recall_at_k(["a", "b", "c"], ["b", "z"], k=3) == 0.5   # 1 of 2 gold in top-3
    assert recall_at_k(["a", "b", "c"], ["a", "b"], k=1) == 0.5   # only top-1 counts
    assert recall_at_k(["a"], [], k=3) is None                    # no gold -> undefined


def test_mrr():
    assert mrr(["a", "b", "c"], ["b"]) == 0.5        # first gold at rank 2
    assert mrr(["a", "b"], ["a"]) == 1.0             # rank 1
    assert mrr(["a", "b"], ["z"]) == 0.0             # no gold retrieved


class _FakeHit:
    def __init__(self, oid):
        self.object_id = oid


class _FakeRepo:
    def _retrieve_scored(self, nb, q, **k):
        return [_FakeHit("o2"), _FakeHit("o1"), _FakeHit("o3")]


def test_run_recall_skips_unannotated_and_scores_annotated():
    questions = [
        {"id": "q1", "question": "x", "gold_object_ids": ["o1"]},
        {"id": "q2", "question": "y"},                       # no gold -> skipped
    ]
    rows = run_recall(_FakeRepo(), "nb", questions, k=3)
    assert len(rows) == 1
    assert rows[0]["id"] == "q1"
    assert rows[0]["recall_at_k"] == 1.0                     # o1 present in top-3
    assert rows[0]["mrr"] == 0.5                             # o1 at rank 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/eval/test_retrieval_metrics.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.eval.retrieval_metrics'`

- [ ] **Step 3: 实现指标模块**

Create `backend/app/eval/retrieval_metrics.py`:
```python
"""Retrieval-quality metrics: recall@k and MRR over a notebook's KG retrieval.
Ground truth comes from an optional `gold_object_ids` field on each question;
questions without it are skipped (retrieval recall needs a labeled hit set, which
questions.yaml does not carry by default)."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def recall_at_k(retrieved_ids: Sequence[str], gold_ids: Sequence[str],
                k: int) -> Optional[float]:
    gold = set(gold_ids)
    if not gold:
        return None
    topk = set(list(retrieved_ids)[:k])
    return len(topk & gold) / len(gold)


def mrr(retrieved_ids: Sequence[str], gold_ids: Sequence[str]) -> float:
    gold = set(gold_ids)
    for i, rid in enumerate(retrieved_ids):
        if rid in gold:
            return 1.0 / (i + 1)
    return 0.0


def run_recall(repo: Any, notebook_id: str, questions: List[Dict[str, Any]],
               k: int = 12) -> List[Dict[str, Any]]:
    """For each question carrying `gold_object_ids`, run KG retrieval and score
    recall@k + MRR. Keyword-only retrieval works without an LLM."""
    rows: List[Dict[str, Any]] = []
    for q in questions:
        gold = q.get("gold_object_ids")
        if not gold:
            continue
        hits = repo._retrieve_scored(notebook_id, q["question"])
        ids = [h.object_id for h in hits]
        rows.append({
            "id": q.get("id", ""),
            "recall_at_k": recall_at_k(ids, gold, k),
            "mrr": mrr(ids, gold),
            "n_gold": len(gold),
            "n_retrieved": len(ids),
        })
    return rows
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/eval/test_retrieval_metrics.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: 接入 run_all + 题集 schema 说明**

In `backend/app/eval/run_all.py`, add a segment mirroring the `quality` one (`run_all.py:32-37`). After the `inference` segment, add:
```python
    if "recall" in only:
        from app.core.config import Settings
        from app.eval.inference import load_questions
        from app.eval.retrieval_metrics import run_recall
        from app.services.sqlite_repository import SQLiteRepository
        repo = SQLiteRepository(Settings())
        rows = run_recall(repo, a.notebook, load_questions())
        import json as _json
        (out / "recall_report.json").write_text(
            _json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[eval] recall_report.json done ({len(rows)} annotated questions)")
```

In `backend/app/eval/questions.yaml`, extend the header comment (`questions.yaml:1-2`) with one line documenting the optional field:
```yaml
# 可选字段 gold_object_ids: [<knowledge_objects.id>, ...] —— 标注该题期望命中的 KG 对象,供 recall@k/MRR(--only recall)使用;未标注的题在 recall 评测中跳过。
```

- [ ] **Step 6: 运行编译/导入 gate**

Run: `cd backend && python -c "import app.eval.run_all, app.eval.retrieval_metrics"`
Expected: 无输出、无报错(导入成功)

- [ ] **Step 7: 提交**
```bash
git add backend/app/eval/retrieval_metrics.py backend/tests/eval/test_retrieval_metrics.py backend/app/eval/run_all.py backend/app/eval/questions.yaml
git commit -m "feat(eval): 检索召回 recall@k/MRR 指标 + run_recall probe(读 gold_object_ids)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> **后续(非本计划必须):** 给 `questions.yaml` 的 L1/L2 题补 `gold_object_ids`(需对目标 notebook 查实际对象 id),`recall` 评测才有数据。可在 Phase 1 收尾用现有 `before/after_inference.md` 对比作为即时效果度量,recall 指标作为长期检索质量看板。

---

## 收尾(Phase 1 完成后)

- [ ] **全量 gate**:`cd backend && python -m pytest -q`(全绿);`PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`(全绿)。
- [ ] **after 对比**:重跑 `--only inference` 生成 `after_inference.md`,与 `before_inference.md` 对比 correctness/grounding。
- [ ] **可选开关验证**:把 `KG_REFINE_ENABLED=true` 跑一次重抽 + inference,确认 refine 不降质;`RERANK_ENABLED` 默认开,如延迟敏感可关。
- [ ] **按 [[dev-flow-finish-with-pr]] 收尾提 PR**(3-way 并 master → push → `gh pr create --base master`)。

---

## 明确移到 Phase 2 的两项(读码后的发现,非遗漏)

- **C1 概念描述 LLM 融合**:`Node` 无 `description` 字段,`canonicalize` 仅合并 `mentions`,`cluster_concepts` 仅产出 `canonical_name` 不存描述。要"融合描述"须先加 schema 字段(节点 payload 或簇级新表/新列),与 Phase 2 的 C7(跨文档合并扩到 Claim/Formula/Procedure)同批做最干净。
- **R3 原文块按关系覆盖加权**:`build_records` 里关系 evidence 只存 `{quote}`、无 `element_id`,无法按 element 覆盖度排序。须先给关系 evidence 补 `element_id`(数据模型改动),属 Phase 2。

---

## Self-Review(计划完成后自查记录)

- **Spec 覆盖**:Phase 1 表格 8 项 → C8(Task1)、C6(Task2)、R1(Task3)、R2(Task4)、R5a(Task5)、评测 recall(Task6)均有任务;C1、R3 显式移 Phase 2 并附原因 → 覆盖完整。
- **Placeholder 扫描**:无 TODO/“类似上文”/“适当处理”;每个代码步骤含完整可粘贴代码与确切命令。
- **类型一致性**:`refine_nodes`/`_rerank_hits`/`recall_at_k`/`mrr`/`run_recall`/`cache_key`/`LLMCache` 在定义任务与调用处签名一致;`chat_json(messages, schema_hint, max_retries=...)` 调用形态与 `app/core/llm.py:74-81` 现签名一致;`RetrievedKnowledge` 字段名(`object_id/object_type/payload/evidence/score`)与 `retrieval.py:33-47` 一致;`store_kg(nb, None, objects, relations)` 与 `sqlite_repository.py:1803` 一致。
