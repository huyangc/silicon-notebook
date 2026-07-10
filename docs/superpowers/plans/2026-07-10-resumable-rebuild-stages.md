# 可中断续跑的 KG rebuild 阶段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `rebuild_unified_kg` 的三个小时级阶段(merge 审查 / 概念描述 / 节点向量)分批落库、可中断续跑,重跑只补未完成部分。

**Architecture:** 统一版本键 checkpoint 表 `kg_rebuild_checkpoint` 做两个 LLM 阶段的崩溃恢复日志(每块/每 16 个完成即落库,按 canonical id 键、按 `input_version` GC);节点向量以 `knowledge_embeddings` 为天然 checkpoint 改增量提交。`--rebuild-only` 保持 `force=True`,新增 `--fresh` 清 checkpoint 强制重裁。

**Tech Stack:** Python 3.13、SQLite(WAL)、pytest、FastAPI 后端;`backend/app/services/sqlite_repository.py`(God 对象)、`concept_merge_review.py`、`batch_ingest.py`、`config.py`。

## Global Constraints

- **Schema 迁移约定**:新增表必须**追加 `_migration_10` + `SCHEMA_VERSION` 9→10**,并**同时写入 `_migration_1` baseline**(双写,与 `_migration_8/9` 同款);已部署库靠版本闸执行 `_migration_10`,不塞进已封版的 `_migration_1..9`。见 `sqlite_repository.py:250` 注释。
- **新配置项**用 pydantic `validation_alias=`(非 `env=`),否则环境变量映射失效。
- **fail-open 不变**:merge 审查/描述单块失败或 checkpoint 落库失败,绝不能抛出打断 rebuild;既有外层 `try/except` 保留。
- **聚类产出零变化**:既有 `test_rebuild_streaming`/`test_cross_doc_merge`/`test_kg_merge`/`test_unified_kg_repository` 必须全绿。
- **checkpoint 落库只在主线程**(`as_completed`/backfill 循环均主线程),单写者。
- **注释用中文**,与文件既有风格一致。
- **提交信息**结尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`;分支 `feat/resumable-rebuild-stages`(worktree `.claude/worktrees/resumable-rebuild`)保持线性(rebase 到 master,勿 merge)。
- **测试命令**从 `backend/` 目录跑:`cd backend && python -m pytest ...`。

## File Structure

- `backend/app/services/sqlite_repository.py`(改):`SCHEMA_VERSION`、`_migration_1` baseline 加表、新 `_migration_10`、4 个 `_rebuild_ckpt_*` helper、`_embed_objects_batch` 增量提交 + `_flush_object_vectors`、`_backfill_knowledge_embeddings` 进度透传、`rebuild_unified_kg` 加 `fresh` 参数 + 入口 GC/clear + merge 审查块 + 描述块。
- `backend/app/services/concept_merge_review.py`(改):`review_merge_candidates` 加 `on_chunk` 回调。
- `backend/app/services/batch_ingest.py`(改):`backfill_node_embeddings` 传进度打印器、`run_kg` 接 `fresh`、CLI `--fresh`。
- `backend/app/core/config.py`(改):`embed_commit_batches`。
- `backend/tests/test_rebuild_checkpoint.py`(新):迁移 + helper + 两个 LLM 阶段续跑。
- `backend/tests/test_concept_merge_review.py`(改):`on_chunk` 单测。
- `backend/tests/test_batch_ingest.py` 或 `test_kg_object_embed_concurrency.py`(改):节点向量增量提交/续跑。

---

### Task 1: `kg_rebuild_checkpoint` 表 + 迁移 + 4 个 helper

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`SCHEMA_VERSION` 行 252;`_migration_1` baseline 约 1119 后;新 `_migration_10` 紧接 `_migration_9` 之后约 1420;helper 紧接 `_migration_10` 之后)
- Test: `backend/tests/test_rebuild_checkpoint.py`(新建)

**Interfaces:**
- Produces:
  - `_rebuild_ckpt_gc(self, notebook_id: str, input_version: str) -> None`
  - `_rebuild_ckpt_clear(self, notebook_id: str) -> None`
  - `_rebuild_ckpt_load(self, notebook_id: str, input_version: str, stage: str) -> Dict[str, dict]`
  - `_rebuild_ckpt_put(self, notebook_id: str, input_version: str, stage: str, rows: List[Tuple[str, dict]]) -> None`
  - 表 `kg_rebuild_checkpoint(notebook_id, input_version, stage, item_key, payload, created_at)`,PK `(notebook_id, input_version, stage, item_key)`

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_rebuild_checkpoint.py`:

```python
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_migration_creates_checkpoint_table(repo):
    """迁移后表存在,且 user_version 已达 SCHEMA_VERSION。"""
    from app.services.sqlite_repository import SCHEMA_VERSION
    with repo._connect() as db:
        row = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kg_rebuild_checkpoint'"
        ).fetchone()
        uv = int(db.execute("PRAGMA user_version").fetchone()[0])
    assert row is not None
    assert uv == SCHEMA_VERSION


def test_ckpt_put_load_roundtrip(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._rebuild_ckpt_put(nb.id, "v1", "merge_review",
                           [("K-a\x1fK-b", {"decision": "merge", "confidence": 0.9})])
    loaded = repo._rebuild_ckpt_load(nb.id, "v1", "merge_review")
    assert loaded == {"K-a\x1fK-b": {"decision": "merge", "confidence": 0.9}}
    # 不同 stage / 版本互不干扰
    assert repo._rebuild_ckpt_load(nb.id, "v1", "concept_desc") == {}
    assert repo._rebuild_ckpt_load(nb.id, "v2", "merge_review") == {}


def test_ckpt_gc_drops_other_versions_only(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._rebuild_ckpt_put(nb.id, "old", "merge_review", [("k1", {"d": 1})])
    repo._rebuild_ckpt_put(nb.id, "cur", "merge_review", [("k2", {"d": 2})])
    repo._rebuild_ckpt_gc(nb.id, "cur")
    assert repo._rebuild_ckpt_load(nb.id, "old", "merge_review") == {}
    assert repo._rebuild_ckpt_load(nb.id, "cur", "merge_review") == {"k2": {"d": 2}}


def test_ckpt_clear_drops_all(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._rebuild_ckpt_put(nb.id, "cur", "merge_review", [("k1", {"d": 1})])
    repo._rebuild_ckpt_put(nb.id, "cur", "concept_desc", [("k2", {"d": 2})])
    repo._rebuild_ckpt_clear(nb.id)
    assert repo._rebuild_ckpt_load(nb.id, "cur", "merge_review") == {}
    assert repo._rebuild_ckpt_load(nb.id, "cur", "concept_desc") == {}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_rebuild_checkpoint.py -q`
Expected: FAIL(`no such table: kg_rebuild_checkpoint` / `AttributeError: _rebuild_ckpt_put`)

- [ ] **Step 3: bump SCHEMA_VERSION**

`sqlite_repository.py:252` 改:

```python
SCHEMA_VERSION = 10
```

- [ ] **Step 4: baseline 双写(_migration_1)**

在 `_migration_1` 的 baseline 里、`concept_comentions` CREATE 之后(约 1119 行、`db.execute("""CREATE TABLE IF NOT EXISTS concept_comentions ...""")` 那个 `)` 与紧随的 `# Lightweight column migrations` 注释之间)插入:

```python
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_rebuild_checkpoint (
                    notebook_id   TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    input_version TEXT NOT NULL,
                    stage         TEXT NOT NULL,
                    item_key      TEXT NOT NULL,
                    payload       TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    PRIMARY KEY (notebook_id, input_version, stage, item_key)
                )
                """
            )
```

- [ ] **Step 5: 新增 `_migration_10`**

紧接 `_migration_9` 方法之后插入:

```python
    def _migration_10(self) -> None:
        """rebuild 断点续跑:kg_rebuild_checkpoint 版本键崩溃恢复日志,让 merge 审查 /
        概念描述两个 LLM 阶段可中断续跑。

        已部署库(user_version>=1 时 _migration_1 短路)靠本迁移补建——与
        _migration_8/_migration_9 同款两层写法(baseline 双写 + 独立迁移)。"""
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS kg_rebuild_checkpoint (
                    notebook_id   TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    input_version TEXT NOT NULL,
                    stage         TEXT NOT NULL,
                    item_key      TEXT NOT NULL,
                    payload       TEXT NOT NULL,
                    created_at    TEXT NOT NULL,
                    PRIMARY KEY (notebook_id, input_version, stage, item_key)
                )
                """
            )
```

- [ ] **Step 6: 新增 4 个 helper**

紧接 `_migration_10` 之后插入(注意 `import json` 与 `_now` 已在文件顶部;`Dict`/`List`/`Tuple` 已从 typing 导入):

```python
    def _rebuild_ckpt_gc(self, notebook_id: str, input_version: str) -> None:
        """删掉本 notebook 里 input_version 不等于当前值的所有 checkpoint 行(表有界)。
        rebuild 开头调一次:数据/算法版本一变,旧决策自动失效。"""
        with self._write() as db:
            db.execute(
                "DELETE FROM kg_rebuild_checkpoint WHERE notebook_id=? AND input_version!=?",
                (notebook_id, input_version))

    def _rebuild_ckpt_clear(self, notebook_id: str) -> None:
        """删掉本 notebook 的全部 checkpoint(所有版本/阶段)。--fresh 用,强制两个 LLM 阶段重跑。"""
        with self._write() as db:
            db.execute("DELETE FROM kg_rebuild_checkpoint WHERE notebook_id=?", (notebook_id,))

    def _rebuild_ckpt_load(self, notebook_id: str, input_version: str, stage: str) -> Dict[str, dict]:
        """载入某阶段在当前 input_version 下已完成的 item:{item_key: payload_dict}。"""
        with self._connect() as db:
            return {
                r["item_key"]: json.loads(r["payload"])
                for r in db.execute(
                    "SELECT item_key, payload FROM kg_rebuild_checkpoint "
                    "WHERE notebook_id=? AND input_version=? AND stage=?",
                    (notebook_id, input_version, stage)).fetchall()
            }

    def _rebuild_ckpt_put(self, notebook_id: str, input_version: str, stage: str,
                          rows: List[Tuple[str, dict]]) -> None:
        """把一批已完成 item 落库(一个写事务,幂等 REPLACE)。rows=[(item_key, payload_dict)]。"""
        if not rows:
            return
        now = _now()
        with self._write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO kg_rebuild_checkpoint "
                "(notebook_id, input_version, stage, item_key, payload, created_at) "
                "VALUES (?,?,?,?,?,?)",
                [(notebook_id, input_version, stage, k, json.dumps(v), now) for k, v in rows])
```

- [ ] **Step 7: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_rebuild_checkpoint.py -q`
Expected: PASS(4 passed)

- [ ] **Step 8: 提交**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/resumable-rebuild
git add backend/app/services/sqlite_repository.py backend/tests/test_rebuild_checkpoint.py
git commit -m "feat(kg-rebuild): kg_rebuild_checkpoint 表+迁移10+4个helper

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `review_merge_candidates` 加 `on_chunk` 回调

**Files:**
- Modify: `backend/app/services/concept_merge_review.py:99-138`
- Test: `backend/tests/test_concept_merge_review.py`(追加)

**Interfaces:**
- Consumes: 无(独立)
- Produces: `review_merge_candidates(llm_client, candidates, batch_size=30, max_workers=1, on_chunk=None)` —— `on_chunk: Optional[Callable[[List[dict]], None]]`,每块决策就绪即在主线程调用一次;`on_chunk` 抛异常被吞+warning,不影响返回。

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_concept_merge_review.py`:

```python
def test_on_chunk_fires_per_chunk_and_covers_all_decisions():
    """on_chunk 每块调用一次,累计决策 == 返回决策;回调抛异常不影响返回。"""
    seen_chunks = []

    def on_chunk(decs):
        seen_chunks.append(list(decs))
        raise RuntimeError("持久化失败也不能打断 review")  # 必须被吞

    cands = _cands(5)                       # 5 候选
    decisions = review_merge_candidates(
        _ReviewLLM(), cands, batch_size=2, on_chunk=on_chunk)  # → 3 块(2,2,1)

    assert len(seen_chunks) == 3            # 每块一次
    flat = [d for chunk in seen_chunks for d in chunk]
    # on_chunk 收到的决策并集 == 函数返回的决策
    assert sorted(d["candidate_id"] for d in flat) == \
           sorted(d["candidate_id"] for d in decisions)
```

(`_ReviewLLM` 每次返回 `mc-1`/`mc-2` 两条决策;上面只校验"每块触发一次 + 覆盖全部返回",不依赖具体 id 数量。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_concept_merge_review.py::test_on_chunk_fires_per_chunk_and_covers_all_decisions -q`
Expected: FAIL(`TypeError: review_merge_candidates() got an unexpected keyword argument 'on_chunk'`)

- [ ] **Step 3: 实现 `on_chunk`**

改 `concept_merge_review.py` 的 `review_merge_candidates`。签名(99-104)改为:

```python
def review_merge_candidates(
    llm_client: Any,
    candidates: List[dict],
    batch_size: int = 30,
    max_workers: int = 1,
    on_chunk=None,
) -> List[dict]:
```

在函数体内加一个本地帮助(紧接 `chunks = [...]` 之后、`out: List[dict] = []` 之前):

```python
    def _emit(decs: List[dict]) -> None:
        if on_chunk is None:
            return
        try:
            on_chunk(decs)
        except Exception:  # noqa: BLE001 — 持久化失败绝不能打断 fail-open 的 review
            logger.warning("merge-review: on_chunk 回调失败,已忽略")
```

并发分支(as_completed 循环)改为:

```python
            for fut in concurrent.futures.as_completed(futures):
                try:
                    decs = fut.result()
                except Exception:  # noqa: BLE001 — belt-and-suspenders
                    logger.warning("merge-review: chunk failed in worker; skipping")
                    continue
                _emit(decs)
                out.extend(decs)
```

串行分支改为:

```python
    else:
        for chunk in chunks:
            decs = _review_chunk(llm_client, chunk)
            _emit(decs)
            out.extend(decs)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_concept_merge_review.py -q`
Expected: PASS(既有 + 新用例全绿)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/concept_merge_review.py backend/tests/test_concept_merge_review.py
git commit -m "feat(merge-review): review_merge_candidates 加 on_chunk 逐块回调

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `rebuild_unified_kg` —— `fresh` 参数 + 入口 GC/clear + merge 审查 checkpoint

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`rebuild_unified_kg` 签名 6776-6778;入口 `_ver` 后约 6805;merge 审查块 6886-6925)
- Test: `backend/tests/test_rebuild_checkpoint.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `_rebuild_ckpt_gc/clear/load/put`;Task 2 的 `on_chunk`
- Produces: `rebuild_unified_kg(self, notebook_id, progress=None, force=False, fresh=False) -> int`;merge 审查决策落 `kg_rebuild_checkpoint`(stage='merge_review',item_key=`_pair_key(a,b)`);模块级 `_pair_key(a, b) -> str`

- [ ] **Step 1: 写失败测试**

追加到 `test_rebuild_checkpoint.py`。用计数假 KG-LLM 注入(`kg_llm_client` 是无 setter 的 property → monkeypatch 类属性):

```python
class _CountingReviewLLM:
    """把每个候选都判成 merge;记录 chat_json 调用次数。"""
    configured = True

    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, schema):
        self.calls += 1
        import re
        ids = re.findall(r"id=(ac\d+)", messages[0]["content"])
        decisions = [{"candidate_id": i, "decision": "merge",
                      "canonical_name": "x", "confidence": 0.99, "rationale": "r"}
                     for i in ids]
        return __import__("json").dumps({"decisions": decisions})


def _seed_mergeable(repo, nb_id):
    """造若干近义 concept(名字接近 → 进 auto_candidates → 触发 merge 审查)。"""
    objs = []
    for i in range(6):
        objs.append({"local_id": f"c{i}", "object_type": "concept",
                     "payload": {"name": f"low noise amplifier {i}", "section_path": ""},
                     "evidence": [{"quoted_span": f"lna variant {i}"}]})
        objs.append({"local_id": f"d{i}", "object_type": "concept",
                     "payload": {"name": f"lna {i}", "section_path": ""},
                     "evidence": [{"quoted_span": f"lna variant {i}"}]})
    repo.store_kg(nb_id, None, objs, [])


def test_merge_review_checkpoint_skips_relled_llm_on_second_run(repo, monkeypatch):
    """同输入连跑两次 rebuild:第二次 merge 审查 LLM 调用数=0(全部命中 checkpoint)。"""
    fake = _CountingReviewLLM()
    monkeypatch.setattr(type(repo), "kg_llm_client", property(lambda self: fake))
    monkeypatch.setattr(type(repo), "kg_concept_desc_enabled", False, raising=False)  # 隔离描述阶段
    monkeypatch.setattr(repo.settings, "kg_concept_desc_enabled", False, raising=False)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_mergeable(repo, nb.id)

    repo.rebuild_unified_kg(nb.id, force=True)
    first = fake.calls
    assert first > 0                       # 首跑确有 merge 审查 LLM

    repo.rebuild_unified_kg(nb.id, force=True)
    assert fake.calls == first             # 二跑零新增(input_version 未变 → 全命中)


def test_fresh_clears_checkpoint_and_readjudicates(repo, monkeypatch):
    """--fresh(fresh=True)清 checkpoint → 再跑重新裁决(LLM 又被调用)。"""
    fake = _CountingReviewLLM()
    monkeypatch.setattr(type(repo), "kg_llm_client", property(lambda self: fake))
    monkeypatch.setattr(repo.settings, "kg_concept_desc_enabled", False, raising=False)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _seed_mergeable(repo, nb.id)

    repo.rebuild_unified_kg(nb.id, force=True)
    first = fake.calls
    repo.rebuild_unified_kg(nb.id, force=True, fresh=True)
    assert fake.calls > first               # fresh 清了 checkpoint → 又裁决一轮
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_rebuild_checkpoint.py -k "merge_review or fresh" -q`
Expected: FAIL(`fresh` 参数不存在 / 二跑 `fake.calls` 翻倍不等)

- [ ] **Step 3: 加 `_pair_key` 与签名/入口**

模块顶部(靠近其它模块级 helper,如 `_concept_desc_sig` 附近)加:

```python
def _pair_key(a: str, b: str) -> str:
    """canonical id 对的稳定归一键:排序后用 \x1f 连接,(a,b)/(b,a) 同键。"""
    x, y = sorted((a, b))
    return f"{x}\x1f{y}"
```

`rebuild_unified_kg` 签名(6776-6778)改为:

```python
    def rebuild_unified_kg(self, notebook_id: str,
                           progress: Optional[Callable[[str, int, int], None]] = None,
                           force: bool = False, fresh: bool = False) -> int:
```

在 `_ver = self._cluster_input_version(notebook_id)`(约 6805)之后、`if not force:` 之前插入:

```python
        # 断点续跑:fresh 清全部 checkpoint(强制两个 LLM 阶段重跑);否则 GC 掉非当前
        # input_version 的残留(数据/算法版本一变 → 旧决策自动失效)。fail-open。
        try:
            if fresh:
                self._rebuild_ckpt_clear(notebook_id)
            else:
                self._rebuild_ckpt_gc(notebook_id, _ver)
        except Exception:  # noqa: BLE001 — checkpoint 维护失败不能打断 rebuild
            self.event_log.logger.warning("rebuild checkpoint GC/clear 失败 for %s", notebook_id, exc_info=True)
```

- [ ] **Step 4: 改 merge 审查块**

把 6886-6925 的 merge 审查块(`from app.services.concept_merge_review import review_merge_candidates` 起,到 `_stage(f"concept: merge-review ...")` 止)整体替换为:

```python
        from app.services.concept_merge_review import review_merge_candidates
        autoc = sd.get("auto_candidates", [])
        _t_mr = _time.perf_counter()
        if autoc and getattr(self.kg_llm_client, "configured", False):
            try:
                cand_dicts = [{"id": f"ac{i}", "canonical_a": a, "canonical_b": b, "score": s}
                              for i, (a, b, s) in enumerate(autoc)]
                # ac{i} → canonical id 对的稳定键(续跑复用的锚)。
                id_to_key = {f"ac{i}": _pair_key(a, b) for i, (a, b, s) in enumerate(autoc)}
                # 已决(同 input_version)命中即跳过 LLM;只把未决候选发出去。
                cached = self._rebuild_ckpt_load(notebook_id, _ver, "merge_review")
                todo = [c for c in cand_dicts if id_to_key[c["id"]] not in cached]

                def _persist(chunk_decisions):
                    rows = [(id_to_key[d["candidate_id"]],
                             {"decision": d["decision"], "confidence": d["confidence"],
                              "canonical_name": d.get("canonical_name", "")})
                            for d in chunk_decisions if d.get("candidate_id") in id_to_key]
                    if rows:
                        self._rebuild_ckpt_put(notebook_id, _ver, "merge_review", rows)

                new = review_merge_candidates(
                    self.kg_llm_client, todo,
                    batch_size=self.settings.kg_merge_review_batch_size,
                    max_workers=self.settings.kg_job_concurrency,
                    on_chunk=_persist,
                )
                # 合并 缓存 ∪ 新决策,按 pair_key 索引。
                decided = dict(cached)
                for d in new:
                    k = id_to_key.get(d.get("candidate_id"))
                    if k:
                        decided[k] = {"decision": d["decision"], "confidence": d["confidence"]}
                extra = set()
                for i, (a, b, s) in enumerate(autoc):
                    dec = decided.get(_pair_key(a, b))
                    if dec and dec.get("decision") == "merge" and \
                            float(dec.get("confidence", 0)) >= self.settings.kg_merge_confirm_threshold:
                        extra.add(frozenset((a[2:] if a.startswith("K-") else a,
                                            b[2:] if b.startswith("K-") else b)))
            except Exception:
                self.event_log.logger.exception(
                    "unified-KG merge-review adjudication failed for %s; proceeding without it",
                    notebook_id,
                )
                extra = set()
            if extra:
                confirmed = set(confirmed) | extra
                sd = cluster_seeds(seeds, reps, members_count, seed_first_name, confirmed, rejected,
                                   conflict_fn=_discriminative_conflict, id_prefix="K-",
                                   rep_ann_max=self.settings.kg_cluster_rep_ann_max,
                                   ann_threads=self.settings.kg_cluster_ann_threads)
            _stage(f"concept: merge-review {len(autoc)} candidates → "
                   f"{len(extra)} merged ({_time.perf_counter() - _t_mr:.1f}s)")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_rebuild_checkpoint.py -q`
Expected: PASS(含 Task 1 四个 + merge_review + fresh)

- [ ] **Step 6: 回归**

Run: `cd backend && python -m pytest tests/test_rebuild_streaming.py tests/test_cross_doc_merge.py tests/test_kg_merge.py -q`
Expected: PASS(聚类语义不变)

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_rebuild_checkpoint.py
git commit -m "feat(kg-rebuild): merge 审查 checkpoint 续跑 + fresh 参数

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `rebuild_unified_kg` —— 概念描述 checkpoint

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(描述 PHASE1 约 6941-6992;PHASE2 约 7012-7026)
- Test: `backend/tests/test_rebuild_checkpoint.py`(追加)

**Interfaces:**
- Consumes: Task 1 helper;Task 3 的入口 GC;模块常量 `_DESC_CKPT_FLUSH = 16`
- Produces: 描述结果落 `kg_rebuild_checkpoint`(stage='concept_desc',item_key=canonical_id,payload=`{"description","sig"}`)

- [ ] **Step 1: 写失败测试**

追加到 `test_rebuild_checkpoint.py`:

```python
class _CountingDescLLM:
    """merge 审查一律 keep_separate(不并簇,保持描述阶段候选稳定);描述返回定值,计数。"""
    configured = True

    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, schema):
        content = messages[0]["content"]
        if "candidate concept merges" in content:      # merge 审查 prompt
            import re, json as _j
            ids = re.findall(r"id=(ac\d+)", content)
            return _j.dumps({"decisions": [
                {"candidate_id": i, "decision": "keep_separate",
                 "canonical_name": "", "confidence": 0.9, "rationale": "r"} for i in ids]})
        self.calls += 1                                # 概念描述 prompt
        import json as _j
        return _j.dumps({"description": "一句定值描述。"})


def test_concept_desc_checkpoint_skips_relled_llm_on_second_run(repo, monkeypatch):
    fake = _CountingDescLLM()
    monkeypatch.setattr(type(repo), "kg_llm_client", property(lambda self: fake))
    monkeypatch.setattr(repo.settings, "kg_concept_desc_enabled", True, raising=False)

    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # 造跨"源"同名 concept → 多成员 canonical(total>=2)→ 触发描述生成
    repo.store_kg(nb.id, "s1", [{"local_id": "a", "object_type": "concept",
        "payload": {"name": "bandgap reference", "section_path": ""},
        "evidence": [{"quoted_span": "bandgap ref circuit"}]}], [])
    repo.store_kg(nb.id, "s2", [{"local_id": "b", "object_type": "concept",
        "payload": {"name": "bandgap reference", "section_path": ""},
        "evidence": [{"quoted_span": "bandgap ref circuit"}]}], [])

    repo.rebuild_unified_kg(nb.id, force=True)
    first = fake.calls
    assert first > 0                        # 首跑确有描述 LLM

    repo.rebuild_unified_kg(nb.id, force=True)
    assert fake.calls == first              # 二跑零新增(命中 concept_desc checkpoint)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_rebuild_checkpoint.py::test_concept_desc_checkpoint_skips_relled_llm_on_second_run -q`
Expected: FAIL(`fake.calls` 二跑翻倍)

> 说明:二跑本可能靠既有 `old_desc`(concept_clusters 里的 canonical_desc_sig)命中而跳过。但 `old_desc` 命中要求 canonical id 一致且已写簇——本任务的 checkpoint 是"写簇前被杀"也能续。若此测试首版意外已过,说明 old_desc 掩盖了 checkpoint 缺口;改造测试为"写簇前中断"(见 Step 5 备选)以真正驱动 checkpoint。

- [ ] **Step 3: 加模块常量**

模块顶部(靠近 `_pair_key`)加:

```python
_DESC_CKPT_FLUSH = 16   # 概念描述每完成多少个 flush 一次 checkpoint(被杀最多丢这么多)
```

- [ ] **Step 4: 改描述 PHASE1(载入 checkpoint 作第一优先复用源)**

在描述块加载 `old_desc` 的 `with self._connect() as db:` 之后(约 6947,`old_desc` 填完之后),加:

```python
            # 同 input_version 的 checkpoint(写簇前被杀留下的已完成描述)作第一优先复用源。
            desc_ckpt = self._rebuild_ckpt_load(notebook_id, _ver, "concept_desc")
```

在 PHASE1 的判定处(约 6987-6992,现有 `prev = old_desc.get(cid)` 那段)改为**先查 checkpoint、再查 old_desc**:

```python
                name = sd["canonical_names"].get(cid, "")
                sig = _concept_desc_sig(name, quotes)
                ck = desc_ckpt.get(cid)
                if ck and ck.get("sig") == sig and ck.get("description"):
                    desc_by_cid[cid] = ck["description"]     # checkpoint 命中:复用,跳过 LLM
                    desc_sig_by_cid[cid] = sig
                    continue
                prev = old_desc.get(cid)
                if prev and prev[0] and prev[1] == sig:
                    desc_by_cid[cid] = prev[0]               # 跨 rebuild 缓存命中:复用
                    desc_sig_by_cid[cid] = sig
                    continue
                work.append((cid, name, quotes, sig))
```

- [ ] **Step 5: 改描述 PHASE2(完成即缓冲 flush 到 checkpoint)**

把 PHASE2 的 `as_completed` 循环(约 7015-7026)改为带缓冲落库:

```python
                with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="kg-desc") as pool:
                    _ck_buf: List[Tuple[str, dict]] = []
                    for fut in _cf.as_completed([pool.submit(_gen, it) for it in work]):
                        cid, desc, sig = fut.result()
                        done_n += 1
                        if desc:
                            desc_by_cid[cid] = desc
                            desc_sig_by_cid[cid] = sig
                            _ck_buf.append((cid, {"description": desc, "sig": sig}))
                            if len(_ck_buf) >= _DESC_CKPT_FLUSH:
                                self._rebuild_ckpt_put(notebook_id, _ver, "concept_desc", _ck_buf)
                                _ck_buf = []
                        if progress is not None:
                            try:
                                progress("concept_desc", done_n, len(work))
                            except Exception:
                                pass
                    if _ck_buf:
                        self._rebuild_ckpt_put(notebook_id, _ver, "concept_desc", _ck_buf)
```

> Step 2 若因 old_desc 掩盖未失败:把测试改为首跑在**写簇前**中断以真正驱动 checkpoint——monkeypatch `repo._write_cluster_map_streamed`,首次调用(object_type=='concept')时先 `raise RuntimeError`;捕获后二跑断言描述 LLM 调用数=首跑已完成数,不重跑已 checkpoint 的。若实现简单起见,亦可接受"两次都靠 checkpoint/old_desc 跳过"的等价断言:二跑 `fake.calls == first`。

- [ ] **Step 6: 跑测试 + 回归**

Run: `cd backend && python -m pytest tests/test_rebuild_checkpoint.py tests/test_rebuild_streaming.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_rebuild_checkpoint.py
git commit -m "feat(kg-rebuild): 概念描述 checkpoint 续跑

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 节点向量增量提交 + 进度 + 配置

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_embed_objects_batch` 3808-3856;`_backfill_knowledge_embeddings` 4059-4081;新 `_flush_object_vectors`)
- Modify: `backend/app/core/config.py`(新 `embed_commit_batches`,约 196 `embed_batch_size` 附近)
- Modify: `backend/app/services/batch_ingest.py`(`backfill_node_embeddings` 519-540)
- Test: `backend/tests/test_kg_object_embed_concurrency.py`(追加;若无则新建 `test_node_embed_incremental.py`)

**Interfaces:**
- Consumes: 无
- Produces:
  - `_embed_objects_batch(self, notebook_id, items, progress=None, commit_every=None) -> None`(每 `commit_every` 批一个写事务;`progress: Callable[[int,int],None]`)
  - `_flush_object_vectors(self, notebook_id, rows) -> None`(rows=[(oid, vec)])
  - `_backfill_knowledge_embeddings(self, db, notebook_id, objects, progress=None)`
  - `settings.embed_commit_batches: int`(默认 50,env `EMBED_COMMIT_BATCHES`)

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_node_embed_incremental.py`:

```python
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "2")     # 小批,好数 commit
    monkeypatch.setenv("EMBED_COMMIT_BATCHES", "1") # 每批 commit,便于观测增量
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _n_vectors(repo, nb_id):
    with repo._connect() as db:
        return db.execute(
            "SELECT COUNT(*) c FROM knowledge_embeddings WHERE notebook_id=?", (nb_id,)
        ).fetchone()["c"]


def test_node_embed_commits_incrementally_and_resumes(repo, monkeypatch):
    """增量提交:flush 中途抛错也已落库前几组;二次调用只补剩余(missing 续跑)。"""
    from app.services import batch_ingest
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [{"local_id": f"o{i}", "object_type": "concept",
             "payload": {"name": f"concept number {i}", "section_path": ""},
             "evidence": []} for i in range(10)]
    repo.store_kg(nb.id, None, objs, [])
    assert _n_vectors(repo, nb.id) == 0

    # 第 3 次 flush 抛错模拟中断(前 2 组已落库)。flush 在主线程、不被 _embed_only 吞、
    # 会传播出 _embed_objects_batch。EMBED_COMMIT_BATCHES=1,batch=2 → 每 2 个一 flush。
    real_flush = repo._flush_object_vectors
    calls = {"n": 0}
    def flaky_flush(nb_id, rows):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("模拟中断")
        return real_flush(nb_id, rows)
    monkeypatch.setattr(repo, "_flush_object_vectors", flaky_flush)

    with pytest.raises(RuntimeError):
        batch_ingest.backfill_node_embeddings(repo, nb.id, conc=1)
    mid = _n_vectors(repo, nb.id)
    assert 0 < mid < 10                      # 中断前已增量落库前几组

    monkeypatch.setattr(repo, "_flush_object_vectors", real_flush)  # 恢复
    batch_ingest.backfill_node_embeddings(repo, nb.id, conc=1)
    assert _n_vectors(repo, nb.id) == 10     # 续跑补齐


def test_node_embed_progress_monotonic(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [{"local_id": f"o{i}", "object_type": "concept",
             "payload": {"name": f"widget {i}", "section_path": ""}, "evidence": []}
            for i in range(6)]
    repo.store_kg(nb.id, None, objs, [])
    seen = []
    with repo._connect() as db:
        rows = [{"id": r["id"], "payload": __import__("json").loads(r["payload"] or "{}")}
                for r in db.execute(
                    "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
                    (nb.id,)).fetchall()]
        repo._backfill_knowledge_embeddings(db, nb.id, rows,
                                            progress=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1][0] == seen[-1][1]           # 末次 done==total
    assert [d for d, _ in seen] == sorted(d for d, _ in seen)  # done 单调不减
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_node_embed_incremental.py -q`
Expected: FAIL(`_backfill_knowledge_embeddings() got unexpected kwarg 'progress'`;增量断言不满足——当前是结尾一次写,中断后 mid==0)

- [ ] **Step 3: 加配置**

`config.py` `embed_batch_size`(约 196)之后加:

```python
    embed_commit_batches: int = Field(50, validation_alias="EMBED_COMMIT_BATCHES")
```

- [ ] **Step 4: 抽 `_flush_object_vectors` + 改 `_embed_objects_batch`**

把 `_embed_objects_batch`(3808-3856)整体替换为:

```python
    def _flush_object_vectors(self, notebook_id: str, rows: list) -> None:
        """把一批 (oid, vector) 落 knowledge_embeddings(一个写事务,幂等 REPLACE)。"""
        if not rows:
            return
        from app.services.vector_index import encode_vector
        now = _now()
        with self._write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
                [(oid, notebook_id, encode_vector(vec), now) for oid, vec in rows],
            )

    def _embed_objects_batch(self, notebook_id: str, items: List[dict],
                             progress=None, commit_every: Optional[int] = None) -> None:
        """并发计算 payload 向量,**每 commit_every 批 flush 一次**(增量提交:中断可续跑、
        内存不攒全量)。每批计算失败照旧 log+跳过(best-effort)。"""
        if not self.settings.embedder_configured:
            return
        pending = []
        for it in items:
            text = _payload_text(it["payload"]).strip()
            if text:
                pending.append((it["_oid"], text[:2000]))
        if not pending:
            if progress:
                progress(0, 0)
            return
        import concurrent.futures as _cf

        size = max(1, self.settings.embed_batch_size)
        batches = [pending[i:i + size] for i in range(0, len(pending), size)]
        commit_every = commit_every or max(1, self.settings.embed_commit_batches)
        ensure = getattr(self.embedder, "_ensure", None)
        if callable(ensure):
            try:
                ensure()
            except Exception:  # noqa: BLE001 — warm-up only
                pass

        def _embed_only(batch) -> list:
            texts = [t for _, t in batch]
            try:
                vectors = self.embedder.embed_texts(texts)
            except Exception as exc:  # noqa: BLE001 — best-effort; isolate per batch
                self.event_log.logger.warning(
                    "embed kg-objects batch failed (%d) for %s: %s",
                    len(batch), notebook_id, exc,
                )
                return []
            return [(oid, vec) for (oid, _), vec in zip(batch, vectors)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        total = len(pending)
        done = 0
        buf: list = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-kg") as pool:
            for bi, part in enumerate(pool.map(_embed_only, batches), 1):
                buf.extend(part)
                done += len(batches[bi - 1])
                if bi % commit_every == 0:
                    self._flush_object_vectors(notebook_id, buf)
                    buf = []
                    if progress:
                        progress(done, total)
        if buf:
            self._flush_object_vectors(notebook_id, buf)
        if progress:
            progress(total, total)
```

> 注:批内 embedder 失败被 `_embed_only` 吞成 `[]`(best-effort,跳过该批);而 `_flush_object_vectors` 的异常在主线程迭代处抛出、不被吞 → 传播出本方法。Step 1 测试正是靠 flush 抛错模拟中断、验证增量落库已生效 + 续跑补齐。

- [ ] **Step 5: 改 `_backfill_knowledge_embeddings` 透传 progress**

`_backfill_knowledge_embeddings`(4059-4081)签名与末行改为:

```python
    def _backfill_knowledge_embeddings(self, db: sqlite3.Connection,
                                       notebook_id: str, objects: List[dict],
                                       progress=None) -> None:
        # ...（have / missing 计算不变）...
        if missing:
            self._embed_objects_batch(notebook_id, missing, progress=progress)
        elif progress:
            progress(0, 0)
```

- [ ] **Step 6: `backfill_node_embeddings` 传进度打印器**

`batch_ingest.py` `backfill_node_embeddings`(519-540)里,把 `repo._backfill_knowledge_embeddings(db, notebook_id, objects)` 那行改为:

```python
            def _p(done, total):
                end = "\n" if done >= total else "\r"
                print(f"  节点向量: {done}/{total}", end=end, flush=True)
            repo._backfill_knowledge_embeddings(db, notebook_id, objects, progress=_p)
```

- [ ] **Step 7: 跑测试 + 回归**

Run: `cd backend && python -m pytest tests/test_node_embed_incremental.py tests/test_kg_object_embed_concurrency.py tests/test_embed_concurrency.py -q`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/app/core/config.py \
        backend/app/services/batch_ingest.py backend/tests/test_node_embed_incremental.py
git commit -m "feat(kg-rebuild): 节点向量增量提交+续跑+进度(EMBED_COMMIT_BATCHES)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: CLI `--fresh` 贯通

**Files:**
- Modify: `backend/app/services/batch_ingest.py`(`run_kg` 签名 297-299 + rebuild 调用 378-379 与 491(run_all);argparse 约 882;dispatch 约 1001-1003)
- Test: `backend/tests/test_batch_ingest.py`(追加)

**Interfaces:**
- Consumes: Task 3 的 `rebuild_unified_kg(..., fresh=...)`
- Produces: `run_kg(..., fresh: bool = False)`;CLI flag `--fresh`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_batch_ingest.py`(参考文件既有 `_StubLLM`/repo fixture):

```python
def test_kg_fresh_flag_clears_checkpoint(repo, monkeypatch):
    """run_kg(fresh=True) 把 fresh 透传给 rebuild_unified_kg。"""
    from app.services import batch_ingest
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    seen = {}
    def _fake_rebuild(notebook_id, progress=None, force=False, fresh=False):
        seen["force"] = force
        seen["fresh"] = fresh
        return 0
    monkeypatch.setattr(repo, "rebuild_unified_kg", _fake_rebuild)
    batch_ingest.run_kg(repo, nb.id, rebuild_only=True, fresh=True)
    assert seen == {"force": True, "fresh": True}
```

(若 `test_batch_ingest.py` 的 repo fixture 需 embedder,沿用文件既有 fixture;`run_kg` 在 embedder 未配时会跳过节点向量但仍走 rebuild 分支。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_batch_ingest.py::test_kg_fresh_flag_clears_checkpoint -q`
Expected: FAIL(`run_kg() got an unexpected keyword argument 'fresh'`)

- [ ] **Step 3: `run_kg` 接 `fresh` 并透传**

`run_kg` 签名(297-299)加 `fresh`:

```python
def run_kg(repo: SQLiteRepository, notebook_id, limit=None, conc=4, log=None,
           no_rebuild: bool = False, rebuild_only: bool = False, fresh: bool = False,
           report_interval: int = 15) -> dict:
```

两处 `repo.rebuild_unified_kg(notebook_id, progress=_rebuild_progress, force=rebuild_only)`(378-379,以及 run_all 的 491 若存在同调用)改为:

```python
            clusters = repo.rebuild_unified_kg(notebook_id, progress=_rebuild_progress,
                                               force=rebuild_only, fresh=fresh)
```

(run_all 的 rebuild 调用同样加 `fresh=fresh`;`run_all` 签名亦加 `fresh: bool = False` 参数并在 dispatch 传入。若 run_all 当前无 fresh 需求,仅改 run_kg 即可,保持最小改动——以 dispatch 实际调用为准。)

- [ ] **Step 4: argparse + dispatch**

`--rebuild-only`(882)之后加:

```python
    p.add_argument("--fresh", action="store_true",
                   help="kg 阶段:清空 rebuild checkpoint,强制 merge 审查/概念描述全量重跑"
                        "(用于只换了 KG 模型/阈值、数据没变的场景)。")
```

dispatch(约 1001-1003)`run_kg(...)` 调用加 `fresh=getattr(args, "fresh", False)`:

```python
        r = run_kg(repo, notebook_id, limit=args.limit, conc=args.embed_conc, log=log,
                   no_rebuild=no_rebuild, rebuild_only=rebuild_only,
                   fresh=getattr(args, "fresh", False),
                   report_interval=args.pool_report_interval)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_batch_ingest.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/batch_ingest.py backend/tests/test_batch_ingest.py
git commit -m "feat(cli): batch_ingest kg --fresh 清 checkpoint 强制重裁

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: 全量验证 + README + PR

**Files:**
- Modify: `README.md` / `README_zh.md`(`--fresh` 用法一行,遵通用口径,见 [[document-cli-in-readme]])
- 无新测试

- [ ] **Step 1: 全量后端测试**

Run: `cd backend && python -m pytest -q`
Expected: PASS(全绿;关注 `test_rebuild_*`/`test_kg_*`/`test_batch_ingest`/`test_concept_merge_review`/`test_node_embed_incremental`)

- [ ] **Step 2: README 补 `--fresh`**

在两个 README 的 batch_ingest kg 用法处,加一行:`--fresh`(清 rebuild checkpoint,强制 merge 审查/概念描述全量重跑;仅在换了模型/阈值而数据未变时需要)。

- [ ] **Step 3: 提交 + rebase + PR**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/resumable-rebuild
git add README.md README_zh.md
git commit -m "docs: batch_ingest kg --fresh 用法

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git fetch origin
git rebase origin/master
git push -u origin feat/resumable-rebuild-stages
gh pr create --base master --title "feat(kg-rebuild): rebuild 三阶段可中断续跑(merge 审查/概念描述/节点向量)" \
  --body "见 docs/superpowers/specs/2026-07-10-resumable-rebuild-stages-design.md。统一 kg_rebuild_checkpoint 版本键日志覆盖两个 LLM 阶段,节点向量增量提交,--fresh 强制重裁。

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

## Self-Review

**Spec coverage:**
- 统一 checkpoint 表 + `_migration_10` + SCHEMA_VERSION → Task 1 ✓
- merge 审查 checkpoint(版本键、pair_key、on_chunk、GC)→ Task 2+3 ✓
- 概念描述 checkpoint(_DESC_CKPT_FLUSH、PHASE1 优先复用、PHASE2 flush)→ Task 4 ✓
- 节点向量增量提交 + 进度 + 内存有界 + EMBED_COMMIT_BATCHES → Task 5 ✓
- `--fresh`(rebuild_unified_kg fresh 参数 + 入口 clear + CLI)→ Task 3(参数/入口)+ Task 6(CLI)✓
- 迁移双写约定 / validation_alias / fail-open / 主线程落库 → Global Constraints + 各任务 ✓
- 集成不变量(二跑 LLM=0、聚类产出一致)→ Task 3/4 测试 + Task 7 全量 ✓

**Placeholder scan:** 无 TBD/TODO;每个 code step 给了完整代码。Task 4 Step 2/5 与 Task 5 Step 4 附了"若测试被既有缓存掩盖/embedder 吞异常"的显式微调指引(非占位符,是实现者需处理的真实分叉)。

**Type consistency:**
- `_pair_key(a,b)`(Task 3 定义)在 merge 审查块两处一致使用 ✓
- `_rebuild_ckpt_load/put/gc/clear` 签名 Task 1 定义,Task 3/4 按同签名调用 ✓
- `_embed_objects_batch(..., progress, commit_every)` 与 `_backfill_knowledge_embeddings(..., progress)`(Task 5)贯通 ✓
- `rebuild_unified_kg(..., force, fresh)`(Task 3)被 `run_kg`(Task 6)按同签名调用 ✓
- stage 字面量 `'merge_review'`/`'concept_desc'` 全plan一致 ✓
