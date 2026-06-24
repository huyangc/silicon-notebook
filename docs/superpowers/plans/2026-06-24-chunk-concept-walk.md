# 概念漫游(PPR)接入通用问答(chunk)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让默认的通用问答(chunk)模式获得跨文档检索:把 PPR 跨文档 chunk 作为 `_mix_retrieve` 的第 3 路候选并入,复用现成 rerank 免费控噪;并把用户可见的「PPR」文案统一改名为「概念漫游」。

**Architecture:** `_mix_retrieve`(只被 `ask_chunk` 调用)在「向量 chunk + KG-overlay chunk」两路之外,加第 3 路 `_ppr_retrieve` 跨文档 chunk(gated `GRAPH_PPR_ENABLED`),三路 round-robin 去重;返回扩为 5-tuple 多带 `ppr_count`(落 events.jsonl 诊断)。ask_chunk 其余(rerank/truncate/`_answer_mix`)零改。reasoning 轨迹摘要的「PPR 跨文档」改成「概念漫游」(机器键 `step_type="ppr"` 不动)。

**Tech Stack:** Python / FastAPI / SQLite;`_ppr_retrieve`(已有);pytest。

**Spec:** [docs/superpowers/specs/2026-06-24-chunk-concept-walk-design.md](../specs/2026-06-24-chunk-concept-walk-design.md)

**约束(项目记忆):** 中文交互;不新增对外开关(复用 `GRAPH_PPR_ENABLED`);守 `[0,1]`/tau;收尾 rebase→push→`gh pr create --base master`;commit 末尾署名 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。当前分支 `claude/chunk-concept-walk`(off origin/master,已含 #70)。

---

## 文件结构

**修改:**
- `backend/app/services/sqlite_repository.py` — `_mix_retrieve`(加第 3 路 PPR + 5-tuple 返回);`ask_chunk` 调用点解包 5 值 + `ask_stage` 加 `concept_walk`。
- `backend/app/services/reasoning_retrieval.py` — 3 处用户可见 `summary` 文案改「概念漫游」。
- `backend/tests/test_mix_answer.py` — 2 处 `_mix_retrieve` 解包改 5 值(现有测试,随签名同步)。

**新建:**
- `backend/tests/test_chunk_concept_walk.py` — 本特性测试(fixture + 复制的 `_seed_two_doc_moe` + rerank/answer stub)。

---

## Task 1: `_mix_retrieve` 第 3 路 PPR + 5-tuple + 同步解包

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_mix_retrieve` [:5661](backend/app/services/sqlite_repository.py:5661);`ask_chunk` 调用点 [:5301](backend/app/services/sqlite_repository.py:5301);`ask_stage("mix_rerank")` [:5314](backend/app/services/sqlite_repository.py:5314))
- Modify: `backend/tests/test_mix_answer.py`([:44](backend/tests/test_mix_answer.py:44)、[:54](backend/tests/test_mix_answer.py:54))
- Test: `backend/tests/test_chunk_concept_walk.py`(新建)

- [ ] **Step 1: 写失败测试(新建文件)**

创建 `backend/tests/test_chunk_concept_walk.py`:

```python
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate, AskRequest


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_two_doc_moe(repo):
    """两源各一 MoE 概念,经 concept_clusters(K-moe)桥接;evidence 指向本源 chunk。
    复刻 test_ppr_retrieve.py 同名助手。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    with repo._write() as db:
        now = "2026-06-22T00:00:00"
        for sid, title in [("src-A", "DeepSeek paper"), ("src-B", "GLM paper")]:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (sid, nb.id, title, "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cA", nb.id, "src-A", "DeepSeek-V3 uses a Mixture-of-Experts (MoE) architecture.",
                    "Arch", json.dumps(["elA"]), now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cB", nb.id, "src-B", "GLM-4.5 is a Mixture-of-Experts (MoE) model.",
                    "Arch", json.dumps(["elB"]), now))
        for oid, sid, el in [("e1", "src-A", "elA"), ("e2", "src-B", "elB")]:
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": el,
                              "element_type": "paragraph", "location_label": "p1",
                              "quoted_span": "MoE", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "",
                        json.dumps({"name": "Mixture-of-Experts (MoE)"}), ev, sid, now, now))
        for oid in ("e1", "e2"):
            db.execute("INSERT INTO concept_clusters "
                       "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"cl-{oid}", nb.id, "K-moe", oid, "Mixture-of-Experts (MoE)", "concept", now))
    return nb


class _AnswerLLM:
    configured = True
    def __init__(self, text): self.text = text
    def chat_json(self, messages, schema_hint, **kw):
        return json.dumps({"answer": self.text, "grounded": True})


class _FakeRerank:
    def __init__(self, configured=True): self._c = configured
    @property
    def configured(self): return self._c
    def rerank(self, query, documents, on_error=None): return list(range(len(documents)))


def test_mix_retrieve_adds_concept_walk_stream_when_flag_on(repo):
    nb = _seed_two_doc_moe(repo)
    cand, _block, _idmap, _hits, ppr_n = repo._mix_retrieve(
        nb.id, "DeepSeek-V3 Mixture-of-Experts", "", ["DeepSeek-V3 Mixture-of-Experts"])
    assert ppr_n > 0                                       # 概念漫游(PPR)贡献了 chunk
    ids = [c.chunk_id for c in cand]
    assert "cA" in ids and "cB" in ids                     # 跨文档 chunk 都进候选池
    assert len(ids) == len(set(ids))                       # 三路去重:无重复 chunk_id


def test_mix_retrieve_no_concept_walk_when_flag_off(repo, monkeypatch):
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    cand, _block, _idmap, _hits, ppr_n = repo._mix_retrieve(
        nb.id, "DeepSeek-V3 Mixture-of-Experts", "", ["DeepSeek-V3 Mixture-of-Experts"])
    assert ppr_n == 0                                      # flag 关 → 不跑 PPR
    assert len(set(c.chunk_id for c in cand)) == len(cand) # 仍去重
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_chunk_concept_walk.py -q`
Expected: FAIL — `_mix_retrieve` 当前返回 4-tuple,解包 5 值报 `ValueError: not enough values to unpack`。

- [ ] **Step 3: 改 `_mix_retrieve`(加第 3 路 + 5-tuple)**

把 `_mix_retrieve`([:5661-5679](backend/app/services/sqlite_repository.py:5661))整体替换为:

```python
    def _mix_retrieve(self, notebook_id: str, query: str, hl: str, sub_queries: list) -> tuple:
        """三路 mix:向量 chunk + KG-overlay 源 chunk + 概念漫游(PPR)跨文档 chunk,
        round-robin 并池去重。返回 (candidates, kg_block, kg_id_map, kg_hits, ppr_count)。
        PPR 跨文档扩散的噪声由 ask_chunk 侧现成 rerank 免费压低。"""
        vector_chunks = self._gather_vector_chunks(notebook_id, sub_queries)
        kg_block, kg_id_map, kg_hits, kg_chunks = "", {}, [], []
        overlay_on = self.settings.chunk_kg_overlay_enabled and (
            self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg())
        if overlay_on:
            kg_block, kg_id_map, kg_hits = self._chunk_kg_overlay(
                notebook_id, query, hl, id_offset=self._MIX_KG_KEY_BASE)
            kg_chunks = self._kg_source_chunks(
                notebook_id, [v["object_id"] for v in kg_id_map.values()])
        # 概念漫游(PPR)第 3 路:gated GRAPH_PPR_ENABLED;无 KG/无 reset → []。
        ppr_chunks = self._ppr_retrieve(notebook_id, query) if self.settings.graph_ppr_enabled else []
        merged, seen = [], set()
        for i in range(max(len(vector_chunks), len(kg_chunks), len(ppr_chunks))):
            for src in (vector_chunks, kg_chunks, ppr_chunks):
                if i < len(src) and src[i].chunk_id not in seen:
                    seen.add(src[i].chunk_id)
                    merged.append(src[i])
        return merged, kg_block, kg_id_map, kg_hits, len(ppr_chunks)
```

- [ ] **Step 4: 同步 `ask_chunk` 调用点 + `ask_stage`**

`ask_chunk` 调用点([:5301-5302](backend/app/services/sqlite_repository.py:5301)):
```python
                candidates, kg_block, kg_id_map, kg_hits, concept_walk_n = self._mix_retrieve(
                    notebook_id, retrieval_query, hl, sub_queries)
```
紧随其后的 `ask_stage("mix_rerank", ...)`([:5314-5315](backend/app/services/sqlite_repository.py:5314))加 `concept_walk`:
```python
                ask_stage("mix_rerank", _t, recall=len(candidates),
                          selected=len(selected), kg_nodes=len(kg_id_map),
                          concept_walk=concept_walk_n)
```

- [ ] **Step 5: 同步现有 `test_mix_answer.py` 两处解包(随签名)**

[:44](backend/tests/test_mix_answer.py:44):
```python
    cand, block, id_map, kg_hits, _ppr_n = repo._mix_retrieve(nb.id, "cascode", "", ["cascode"])
```
[:54-55](backend/tests/test_mix_answer.py:54):
```python
    cand, _block, _id_map, _kg_hits, _ppr_n = repo._mix_retrieve(
        nb.id, "cascode", "", ["cascode", "output resistance"])
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_chunk_concept_walk.py tests/test_mix_answer.py -q`
Expected: PASS（新文件 2 测试 + test_mix_answer 全绿）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_mix_answer.py backend/tests/test_chunk_concept_walk.py
git commit -m "$(cat <<'EOF'
feat(chunk): Concept Walk (PPR) as 3rd fused stream in _mix_retrieve

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: reasoning 轨迹文案改名「概念漫游」

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`([:247](backend/app/services/reasoning_retrieval.py:247)、[:350](backend/app/services/reasoning_retrieval.py:350)、[:365](backend/app/services/reasoning_retrieval.py:365))
- Test: `backend/tests/test_chunk_concept_walk.py`

- [ ] **Step 1: 写失败测试(追加)**

追加到 `backend/tests/test_chunk_concept_walk.py`:

```python
class _AnswerOnlyReasoningLLM:
    """plan 单子查询;reflect 永远 answer → reasoning 只靠 seed pass 跑出 ppr 轨迹。"""
    configured = True
    def chat_json(self, messages, schema_hint, **kw):
        if "sub_queries" in schema_hint:
            return json.dumps({"sub_queries": [{"query": "DeepSeek MoE"}]})
        if "next_action" in schema_hint:
            return json.dumps({"next_action": "answer", "sufficient": True})
        return json.dumps({"answer": "都用 MoE [k1].", "grounded": True})


def test_reasoning_trace_uses_concept_walk_name(repo):
    """reasoning 的 ppr 轨迹 summary 改叫「概念漫游」,机器键 step_type 仍 'ppr'。"""
    from app.services.reasoning_retrieval import ReasoningRetriever
    nb = _seed_two_doc_moe(repo)
    repo._reasoning_llm_client = _AnswerOnlyReasoningLLM()
    result = ReasoningRetriever(repo, repo.settings).run(nb.id, "DeepSeek-V3 MoE 对比")
    ppr_steps = [s for s in result.trace if s.step_type == "ppr"]
    assert ppr_steps                                            # 机器键不变
    assert any("概念漫游" in s.summary for s in ppr_steps)      # 文案已改名
    assert not any("PPR 跨文档" in s.summary for s in result.trace)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_chunk_concept_walk.py -q -k "concept_walk_name"`
Expected: FAIL — 当前 summary 仍是「PPR 跨文档兜底检索…」,断言 `"概念漫游" in summary` 失败。

- [ ] **Step 3: 改三处用户可见 summary**

[:247](backend/app/services/reasoning_retrieval.py:247):
```python
                             summary=f"概念漫游:跨文档检索,得到 {len(seeded)} 段原文",
```
[:350](backend/app/services/reasoning_retrieval.py:350):
```python
                                     summary="跳过概念漫游(未启用)",
```
[:365](backend/app/services/reasoning_retrieval.py:365):
```python
                                     summary=f"概念漫游:{pq},新增 {len(new)} 段",
```
(只改人读 `summary` 文案;`step_type="ppr"`、`detail` 的键全部不动。)

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_chunk_concept_walk.py -q -k "concept_walk_name"`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_chunk_concept_walk.py
git commit -m "$(cat <<'EOF'
feat(reasoning): rename user-facing PPR trace text to 概念漫游 (Concept Walk)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: ask_chunk 端到端 + 隔离 + 全量

**Files:**
- Test: `backend/tests/test_chunk_concept_walk.py`

- [ ] **Step 1: 写端到端测试(追加)**

追加到 `backend/tests/test_chunk_concept_walk.py`:

```python
def test_ask_chunk_concept_walk_end_to_end(repo):
    """overlay 路 + flag 开:概念漫游把跨文档 chunk 并入候选 → rerank → _answer_mix,
    答案出 chunk 引用。"""
    repo.settings.query_rewrite_enabled = False
    repo.llm_client = _AnswerLLM("DeepSeek 与 GLM 都用 MoE [k1].")
    repo.rerank_client = _FakeRerank(configured=True)
    nb = _seed_two_doc_moe(repo)
    resp = repo.ask_chunk(nb.id, AskRequest(question="DeepSeek-V3 MoE 相比其他模型", mode="chunk"))
    assert resp.mode == "chunk"
    assert resp.answer
    assert any(a.object_type == "chunk" for a in resp.anchors)   # 跨文档 chunk 成了可引用证据


def test_ask_chunk_concept_walk_off_unchanged(repo, monkeypatch):
    """flag 关 → 不并入 PPR 路,overlay 仍按今天行为出 chunk 答案。"""
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    repo.settings.query_rewrite_enabled = False
    repo.llm_client = _AnswerLLM("答案 [k1].")
    repo.rerank_client = _FakeRerank(configured=True)
    nb = _seed_two_doc_moe(repo)
    resp = repo.ask_chunk(nb.id, AskRequest(question="MoE", mode="chunk"))
    assert resp.mode == "chunk" and resp.answer
```

- [ ] **Step 2: 跑新文件确认通过**

Run: `cd backend && python -m pytest tests/test_chunk_concept_walk.py -q`
Expected: PASS（全文件)。若 e2e 的 chunk-anchor 断言失败:打印 `resp.anchors` 排查——预期路径 overlay_on(rerank stub configured + 有 KG)→ `_mix_retrieve` 三路 → rerank → `_answer_mix` → `[k1]` 落 chunk 段。

- [ ] **Step 3: 隔离验证(reasoning/graph/chunk 朴素路径不受影响)**

Run: `cd backend && python -m pytest tests/test_reasoning_ppr.py tests/test_ppr_retrieve.py tests/test_mix_overlay.py tests/test_ask_modes.py tests/test_ask_redesign.py -q`
Expected: PASS（本特性未碰 reasoning/graph 检索逻辑、未碰 chunk 朴素路径)。

- [ ] **Step 4: 全量回归**

Run: `cd backend && python -m pytest -q`
Expected: 0 failed（除环境性 innovus `~/Downloads` 沙箱偶发错外）。记录 passed/skipped 数。

- [ ] **Step 5: 提交**

```bash
git add backend/tests/test_chunk_concept_walk.py
git commit -m "$(cat <<'EOF'
test(chunk): Concept Walk e2e + flag-off + isolation

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## 收尾:提 PR

```bash
cd backend && python -m pytest -q                          # 最终确认
git -C .. fetch origin && git -C .. rebase origin/master   # 线性
git -C .. push -u origin claude/chunk-concept-walk
gh pr create --base master --head claude/chunk-concept-walk \
  --title "feat(chunk): Concept Walk (PPR) cross-doc retrieval in general Q&A" \
  --body "见 spec/plan:通用问答把 PPR 跨文档 chunk 作 _mix_retrieve 第 3 路融合(复用现成 rerank 控噪),gated GRAPH_PPR_ENABLED;用户可见『PPR』文案统一改名『概念漫游』。reasoning/graph 检索零改。"
```

待真机:overlay 路问对比题看是否跨多篇引用;看 events.jsonl 的 `concept_walk=N`。

---

## 自审清单(写计划后已核)

- **Spec 覆盖:** B(第 3 路+5-tuple)→T1;C(concept_walk 诊断)→T1 Step4;D(改名)→T2;E/F(隔离/flag-off/去重)→T1+T3;G(测试)→T1/T2/T3。✓
- **类型一致:** `_mix_retrieve` 5-tuple `(merged, kg_block, kg_id_map, kg_hits, ppr_count)` 在 src 调用点 + 两处现有测试 + 新测试解包一致;`ppr_count=len(ppr_chunks)`。✓
- **无占位:** 每步真实代码 + 确切命令 + 预期。✓
- **解包全覆盖:** grep 确认 `_mix_retrieve(` 仅 3 处解包(ask_chunk + test_mix_answer×2),T1 全部更新。✓
