# 大库检索统一 copyable + 无索引提示建索引 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让六条检索路径的「大库」定义统一到 `copyable`(chunk 守卫补 `not copyable` 触发),并在大库完全无 scale 索引时经 `AskResponse.index_required` 提示前端渲染「构建索引」按钮。

**Architecture:** 后端:chunk 守卫加 `not copyable` 触发条件(叠加既有 chunk 计数阈值);新增 `_needs_index(nb)` 判定,在 `_save_answer`(所有 ask handler 的唯一收口)里赋 `response.index_required`;`AskResponse` 加布尔字段。前端:AskResponse 类型加 `index_required?`,问答结果区渲染建索引 banner + 复用既有 `rebuildScaleIndex(nb,"now")`。

**Tech Stack:** Python 3.13 / FastAPI / SQLite / pytest;前端 Next.js + TS。

## Global Constraints

- 后端测试从 `backend/` 跑:`python -m pytest tests/<file> -q`;本机系统 `python`(共享 conda),不建 venv。
- 「大库」全局统一定义 = `not self.notebook_copy_stats(nb)["copyable"]`;`chunk_bruteforce_max_chunks`(默认 20000)保留作叠加下限,不删。
- `index_required` 仅在「大库 **且** 磁盘完全无 scale 索引(`_scale_index(nb, allow_stale=True) is None`)」时为 True;「建过但有 delta」不弹(既有「N 源待索引」徽章覆盖)。
- 小库(copyable=True)路径行为字节不变:chunk 少 → 全量暴力;chunk 多(>阈值)→ 既有降级。
- 前端 `frontend/app/page.tsx` 中文文案弯引号“”是合法 JSX 文本;只做增量编辑,严禁全文件批量替引号;自查 `git diff frontend/app/page.tsx | grep -c '^-.*[“”]'` = 0。
- 前端 `cd frontend && npx tsc --noEmit` = 0 errors(worktree 无 node_modules 时先 `npm ci`)。
- TDD:每后端任务先写失败测试→跑失败→实现→跑通过→commit。
- Commit 中文 conventional,尾行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- 分支 `feat/scale-index-disk-identity`(= PR#185),worktree `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/scale-idx-cache`。⚠️ 只在此 worktree 跑 git;**绝不在** `/Users/hzf/workspace/silicon_notebook`(root checkout)跑 git。提交前核 `pwd` 与 `git rev-parse --abbrev-ref HEAD`。
- 收尾后端全量 `python -m pytest tests/ -q` 保持全绿(基线 1838 passed / 1 skipped)。

## 文件结构

- `backend/app/services/sqlite_repository.py`:`_retrieve_chunks` 守卫(Part 1)、新增 `_needs_index`、`_save_answer` 赋值(Part 2)。
- `backend/app/models/schemas.py`:`AskResponse.index_required` 字段。
- `backend/tests/test_large_lib_index_required.py`(新建):后端测试。
- `frontend/app/page.tsx`:AskResponse 类型 + banner + 钮接线。

---

### Task 1: chunk 守卫统一到 copyable(Part 1)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_retrieve_chunks`,当前 ~10484-10495 的守卫块)
- Test: `backend/tests/test_large_lib_index_required.py`(新建)

**Interfaces:**
- Produces: `_retrieve_chunks` 对 `not copyable` 的大库无条件走 FTS 降级(不 `_gather_chunks` 全表),与既有 `chunk_bruteforce_max_chunks` 阈值叠加(OR)。

**当前守卫代码(替换目标,10490 起):**

```python
        threshold = self.settings.chunk_bruteforce_max_chunks
        if threshold > 0:
            with self._connect() as db:
                n_chunks = db.execute(
                    "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id = ?",
                    (notebook_id,)).fetchone()["c"]
            if n_chunks > threshold:
                return self._retrieve_chunks_fts_degraded(
                    notebook_id, query, query_vector, recall, n_chunks)
```

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_large_lib_index_required.py`:

```python
"""大库检索统一 copyable + 无索引提示建索引。"""
import json
import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _add_chunk(repo, nb_id, sid, cid, text):
    now = "2026-07-03T00:00:00"
    with repo._write() as db:
        db.execute("INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb_id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                   (cid, nb_id, sid, text, "", "[]", now))
        v = repo.embedder.embed_query(text)
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (cid, nb_id, json.dumps(v), now))


def test_large_lib_few_chunks_degrades_to_fts(repo, monkeypatch):
    """大库(copyable=False)即使 chunk 数远低于阈值,也走 FTS 降级、不全表暴力。"""
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha")     # 仅 1 chunk,远低于 20000
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)  # 一切皆大
    events = []
    monkeypatch.setattr(repo.event_log, "emit", lambda e: events.append(e))

    def _boom(*a, **k):
        raise AssertionError("大库不得走 _gather_chunks 全表暴力")
    monkeypatch.setattr(repo, "_gather_chunks", _boom)

    scored, ids, mat = repo._retrieve_chunks(nb.id, "alpha")
    assert any(e.get("kind") == "chunk_bruteforce_skipped" for e in events)


def test_small_lib_few_chunks_bruteforces(repo):
    """小库 chunk 少 → 全量暴力路径不变(能拿到打分结果)。"""
    nb = repo.create_notebook(NotebookCreate(name="small"))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha beta")
    scored, ids, mat = repo._retrieve_chunks(nb.id, "alpha")
    assert ids is not None   # 走了全量矩阵路径
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_large_lib_index_required.py -q -k chunks`
Expected: `test_large_lib_few_chunks_degrades_to_fts` FAIL(当前 threshold=20000,1 chunk 不触发守卫 → 走 `_gather_chunks` → `_boom`)

- [ ] **Step 3: 实现**

把 10490 起的守卫块替换为:

```python
        # 大库暴力守卫(统一「大库」定义 = not copyable,与其余 5 条检索路径一把尺子):
        # 大库无论 chunk 多少都强制走索引/FTS 降级,绝不全表暴力。chunk 计数阈值
        # chunk_bruteforce_max_chunks 作叠加下限保留(小库 chunk 极多也降级)。
        large = not self.notebook_copy_stats(notebook_id)["copyable"]
        threshold = self.settings.chunk_bruteforce_max_chunks
        if large or threshold > 0:
            with self._connect() as db:
                n_chunks = db.execute(
                    "SELECT COUNT(*) AS c FROM chunks WHERE notebook_id = ?",
                    (notebook_id,)).fetchone()["c"]
            if large or n_chunks > threshold:
                return self._retrieve_chunks_fts_degraded(
                    notebook_id, query, query_vector, recall, n_chunks)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_large_lib_index_required.py -q -k chunks && python -m pytest tests/test_indexed_only_principle.py tests/test_p4_kg_shrink.py -q`
Expected: 全 PASS(既有 chunk 守卫/检索测试不受影响——小库路径字节不变)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_large_lib_index_required.py
git commit -m "feat(retrieval): chunk 大库守卫统一到 not copyable(六路径一把尺子)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: `index_required` 信号(后端)

**Files:**
- Modify: `backend/app/models/schemas.py`(`AskResponse`,`kg_required` 字段旁,当前 :299)
- Modify: `backend/app/services/sqlite_repository.py`(新增 `_needs_index`;`_save_answer` 头部赋值,当前 :12052)
- Test: `backend/tests/test_large_lib_index_required.py`(追加)

**Interfaces:**
- Produces: `AskResponse.index_required: bool = False`;`SQLiteRepository._needs_index(notebook_id) -> bool`;`_save_answer` 在持久化前置 `response.index_required = self._needs_index(notebook_id)`(覆盖全部 ask handler 与全部 return 路径)。

- [ ] **Step 1: 写失败测试(追加)**

```python
from app.models.schemas import AskResponse


def _index_nb(repo, name="big"):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name=name))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha")
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)   # 磁盘有 manifest → 有索引
    return nb


def test_needs_index_truth_table(repo, monkeypatch):
    # 大库无索引 → True
    big = repo.create_notebook(__import__("app.models.schemas", fromlist=["NotebookCreate"]).NotebookCreate(name="bignoidx"))
    _add_chunk(repo, big.id, "s1", "c1", "alpha")
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    assert repo._needs_index(big.id) is True
    # 小库无索引 → False(小库允许暴力,不要求索引)
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 5000)
    assert repo._needs_index(big.id) is False


def test_needs_index_false_when_indexed(repo, monkeypatch):
    nb = _index_nb(repo)
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)  # 大库
    assert repo._needs_index(nb.id) is False   # 有磁盘索引 → 不提示


def test_save_answer_sets_index_required(repo, monkeypatch):
    """_save_answer 是所有 handler 的收口:大库无索引时给 response 打 index_required。"""
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="bignoidx2"))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha")
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    resp = AskResponse(conclusion="x")
    repo._save_answer(nb.id, "q", resp)
    assert resp.index_required is True


def test_save_answer_index_required_false_small(repo):
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="small2"))
    _add_chunk(repo, nb.id, "s1", "c1", "alpha")
    resp = AskResponse(conclusion="x")
    repo._save_answer(nb.id, "q", resp)
    assert resp.index_required is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_large_lib_index_required.py -q -k "needs_index or save_answer"`
Expected: FAIL(`_needs_index` 不存在 / `AskResponse` 无 `index_required`)

- [ ] **Step 3: 实现**

(a) `schemas.py:299` 的 `kg_required: bool = False` 之后加:

```python
    # 大库(not copyable)且完全无 scale 索引(从未建过)时 True:检索能力受限,
    # 驱动前端渲染「构建索引」提示。「建过但有 delta」不置此位(既有「N 源待索引」
    # 徽章覆盖那种最终一致态)。
    index_required: bool = False
```

(b) `sqlite_repository.py` 新增 `_needs_index`(放在 `_save_answer` 定义之前):

```python
    def _needs_index(self, notebook_id: str) -> bool:
        """大库且磁盘完全无 scale 索引(从未建过)→ True。用于 AskResponse.index_required:
        大库检索强制走索引,无索引时检索降级(FTS/skip/refuse),需提示用户手动建索引。
        小库(copyable=True)允许暴力、不要求索引 → False。已建索引(含 stale/有 delta)→
        False(那是恒定成本·最终一致态,由「N 源待索引」徽章覆盖,不重复提示)。
        两处判定都廉价:copystats 版本 memo;_scale_index(allow_stale) 经磁盘身份缓存 O(1)。"""
        try:
            if self.notebook_copy_stats(notebook_id)["copyable"]:
                return False
            return self._scale_index(notebook_id, allow_stale=True) is None
        except Exception:  # noqa: BLE001 — 判定失败不拖垮 ask,退化为不提示
            return False
```

(c) `_save_answer` 头部(`answer_id = f"ans-..."` 之前)加:

```python
        # 所有 ask handler 的唯一收口:在持久化/返回前给 response 打大库无索引提示位。
        # 覆盖 chunk/reasoning/graph 三 handler 的全部 return 路径(含早退),避免逐 handler
        # 多 return 点漏赋值。小库/已索引 → False(默认),无副作用。
        response.index_required = self._needs_index(notebook_id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_large_lib_index_required.py -q && python -m pytest tests/test_ask_modes_contract.py -q`
Expected: 全 PASS(若无 test_ask_modes_contract.py 则跳过该文件)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/schemas.py backend/app/services/sqlite_repository.py backend/tests/test_large_lib_index_required.py
git commit -m "feat(api): AskResponse.index_required——大库无索引时提示前端建索引

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 前端建索引 banner + 钮

**Files:**
- Modify: `frontend/app/page.tsx`(AskResponse 类型 ~:173-189;答案渲染区,镜像 `model_errors` banner ~:5305)

**Interfaces:**
- Consumes: 后端 `AskResponse.index_required`;既有 `rebuildScaleIndex(nb, when)`(:631)、`fetchScaleIndexStatus`(:632)、`scaleIndexStatus` 状态(:967)。

- [ ] **Step 1: 读上下文定位**

先读 `frontend/app/page.tsx` 的:AskResponse 类型块(173-189)、`model_errors` banner 渲染块(5305 起,看它在答案区的确切 JSX 位置与 className 约定)、`rebuildScaleIndex`(631)、答案对象变量名(答案渲染处的 `answer`)。banner 放在答案区顶部、与 `model_errors` banner 同级相邻。

- [ ] **Step 2: 类型加字段**

在 AskResponse 类型(:173)里加(紧邻 `kg_required` 或 `model_errors`):

```typescript
  index_required?: boolean;
```

- [ ] **Step 3: 渲染 banner + 钮**

在答案渲染区、`model_errors` banner 相邻处加(变量名 `answer` 以该文件实际为准;`nb` = 当前 notebook id 变量,以文件实际为准,如 `activeNotebookId`/`selectedNotebook`):

```tsx
{answer.index_required && (
  <div className="answer-model-error" title="大库检索强制走索引;未建索引时仅有降级结果">
    <span>此知识库较大且尚未建立检索索引,当前检索能力受限。</span>
    <button
      type="button"
      onClick={() => { void rebuildScaleIndex(nb, "now"); }}
    >
      构建索引
    </button>
  </div>
)}
```

(复用既有 `answer-model-error` 或同区既有 banner 的 className 保持视觉一致;按钮样式沿用文件里既有按钮类。点钮后 `scaleIndexStatus` 轮询会反映 building——若该轮询未在问答视图挂载,则在 onClick 里额外 `void fetchScaleIndexStatus(nb).then(setScaleIndexStatus)` 触发一次,mirror KB 视图用法。)

- [ ] **Step 4: 类型检查 + 弯引号自查**

Run: `cd frontend && npm ci >/dev/null 2>&1; npx tsc --noEmit`
Expected: 0 errors

Run: `git diff frontend/app/page.tsx | grep -c '^-.*[“”]'`
Expected: `0`

- [ ] **Step 5: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(ui): 大库无索引时问答面板提示「构建索引」(消费 index_required)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 全量验证 + 更新 PR#185

- [ ] **Step 1: 后端全量**

Run: `cd backend && python -m pytest tests/ -q`
Expected: 全绿(1838+ passed,含本特性新增;1 skipped 保持)

- [ ] **Step 2: check.sh**

Run: `bash scripts/check.sh`
Expected: 绿(前端有 node_modules 时含 tsc)

- [ ] **Step 3: push(同分支自动更新 PR#185)**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/scale-idx-cache
git fetch origin && git rebase origin/master   # 若 origin/master 前移;冲突按分支版本解
git push
```

（同分支 push 自动更新 PR#185;若需在 PR 描述补 Branch B 段,用 `gh pr edit 185 --body ...` 追加。）

---

## Self-Review 结论

- **Spec 覆盖**:Part 1(chunk 守卫统一)=Task 1;Part 2(index_required 后端)=Task 2;前端=Task 3;验证=Task 4。测试映射:_needs_index 真值表 + _save_answer 收口 + chunk 大库降级 + 小库不变——与 spec §测试 四条对应 ✓
- **无占位符**:每步含真实代码/命令;前端变量名(`answer`/`nb`/className)标注「以文件实际为准」并给了定位步骤(Task 3 Step 1)✓
- **类型一致**:`_needs_index(nb)->bool`(T2)与 `_save_answer` 调用一致;`AskResponse.index_required`(T2 后端)与前端 `index_required?`(T3)字段名一致;chunk 守卫 `large` 变量与 `_retrieve_chunks_fts_degraded` 既有签名一致 ✓
- **已知风险**:T3 前端变量名需按文件实际(已在 Step 1 要求先读定位);banner 视觉需真机验证(index_required 态本地难复现,靠 tsc + 代码审 + 弯引号自查把关)。
