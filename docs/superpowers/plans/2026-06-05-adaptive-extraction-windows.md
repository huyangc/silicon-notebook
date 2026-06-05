# 自适应抽取窗口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 抽取窗口大小按 `level=clamp(内容字符/并发, 4000, 8000)` 自适应、并按 `ceil(C/N)` 等长切分，消除大窗口超时与小文档长尾；并发提到 100。

**Architecture:** 新增纯函数 `kg_ingest.plan_window_size()` 计算等长窗口字符数；`_run_extraction` 用它（基于 `len(raw_text)` 与 `kg_extract_workers`）算出 `n` 再传给已有的 `extract_graph(..., n=...)`。`make_windows` 不改（已按 `n` 打包）。三个 config 旋钮 + `.env` 把并发设 100。

**Tech Stack:** Python / pydantic-settings / 既有 KG 抽取管线。

**Spec:** `docs/superpowers/specs/2026-06-05-adaptive-extraction-windows-design.md`

**约定**：`PY=/opt/homebrew/Caskroom/miniconda/base/bin/python`；测试 `cd backend && PYTHONPATH=. $PY -m pytest tests/<f> -q`。本期不做效果回归；判据=单测绿 + check.sh 绿 + 既有不回归。

---

## Task 1: config 旋钮

**Files:** Modify `backend/app/core/config.py`（`kg_window_target_chars` 一带，约 47 行）；Test `backend/tests/test_adaptive_windows.py`

- [ ] **Step 1: 写失败测试**（新建 `tests/test_adaptive_windows.py`）
```python
def test_settings_window_knobs(monkeypatch):
    from app.core.config import Settings
    s = Settings()
    assert s.kg_window_min_chars == 4000
    assert s.kg_window_max_chars == 8000
    assert s.kg_window_target_chars == 0          # 0 = 自适应（默认）
    monkeypatch.setenv("KG_WINDOW_MAX_CHARS", "6000")
    assert Settings().kg_window_max_chars == 6000
```

- [ ] **Step 2: 跑测试确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_adaptive_windows.py::test_settings_window_knobs -q`
Expected: FAIL（`kg_window_min_chars` 不存在 / `kg_window_target_chars` 仍是 9000）。

- [ ] **Step 3: 改 config** — 把 `kg_window_target_chars` 一行改为下面三行（`kg_window_overlap_chars` 保持不变，紧随其后）：
```python
    # 抽取窗口：0=按文档大小+并发自适应（见 plan_window_size）；>0=固定字符数（覆盖/调试）。
    kg_window_target_chars: int = Field(0, env="KG_WINDOW_TARGET_CHARS")
    # 自适应窗口的下/上限：level = clamp(内容字符/并发, min, max)。
    kg_window_min_chars: int = Field(4000, env="KG_WINDOW_MIN_CHARS")
    kg_window_max_chars: int = Field(8000, env="KG_WINDOW_MAX_CHARS")
```

- [ ] **Step 4: 跑测试确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_adaptive_windows.py::test_settings_window_knobs -q`
Expected: PASS。

- [ ] **Step 5: Commit**
```bash
git add backend/app/core/config.py backend/tests/test_adaptive_windows.py
git commit -m "feat(config): 自适应窗口 min/max + target 改为可选覆盖"
```

---

## Task 2: `plan_window_size` 纯函数

**Files:** Modify `backend/app/services/kg_ingest.py`（顶部确保 `import math`；新增函数）；Test `backend/tests/test_adaptive_windows.py`

- [ ] **Step 1: 写失败测试**（追加）
```python
def test_plan_window_size():
    from app.services.kg_ingest import plan_window_size
    import math
    # 极小文档 -> 单窗口
    assert plan_window_size(3000, 100, 4000, 8000) == 3000
    # 9000 @100 -> level 4000 -> N=3 -> 等长 3000（用户的例子）
    assert plan_window_size(9000, 100, 4000, 8000) == 3000
    # 大文档 -> 封顶 8000
    assert plan_window_size(1_000_000, 100, 4000, 8000) == 8000
    # 中等文档 -> 等长、全覆盖
    n = plan_window_size(129206, 100, 4000, 8000)
    N = math.ceil(129206 / n)
    assert N == 33 and n <= 8000 and N * n >= 129206
    # override>0 -> 固定
    assert plan_window_size(129206, 100, 4000, 8000, override=9000) == 9000
```

- [ ] **Step 2: 跑测试确认 FAIL**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_adaptive_windows.py::test_plan_window_size -q`
Expected: FAIL（ImportError plan_window_size）。

- [ ] **Step 3: 实现** — 确保 `kg_ingest.py` 顶部有 `import math`（没有就加）；新增函数（放在 `extract_graph` 之前）：
```python
def plan_window_size(content_chars: int, workers: int, w_min: int, w_max: int,
                     override: int = 0) -> int:
    """Balanced extraction window size (chars).

    override>0 forces a fixed size (back-compat / manual). Otherwise pick
    level = clamp(content_chars / workers, w_min, w_max), split into
    N = ceil(content_chars / level) windows, and return the BALANCED size
    ceil(content_chars / N) so windows are near-equal (no long-tail runt).
    """
    if override and override > 0:
        return override
    if content_chars <= w_min:
        return max(1, content_chars)
    level = min(w_max, max(w_min, content_chars // max(1, workers)))
    n_windows = max(1, math.ceil(content_chars / level))
    return math.ceil(content_chars / n_windows)
```

- [ ] **Step 4: 跑测试确认 PASS**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_adaptive_windows.py -q`
Expected: PASS（2 passed）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/kg_ingest.py backend/tests/test_adaptive_windows.py
git commit -m "feat(kg): plan_window_size 自适应等长窗口计算"
```

---

## Task 3: 接入 `_run_extraction`

**Files:** Modify `backend/app/services/sqlite_repository.py:1109-1115`

- [ ] **Step 1: 实现** — 把：
```python
            raw_text = self._source_raw_text(source, elements)
            graph = kg_ingest.extract_graph(
                self.llm_client, raw_text, source.file_name or "source.md", kg_doc_type,
                n=self.settings.kg_window_target_chars,
                m=self.settings.kg_window_overlap_chars,
                workers=self.settings.kg_extract_workers,
            )
```
替换为：
```python
            raw_text = self._source_raw_text(source, elements)
            n_chars = kg_ingest.plan_window_size(
                len(raw_text), self.settings.kg_extract_workers,
                self.settings.kg_window_min_chars, self.settings.kg_window_max_chars,
                override=self.settings.kg_window_target_chars,
            )
            graph = kg_ingest.extract_graph(
                self.llm_client, raw_text, source.file_name or "source.md", kg_doc_type,
                n=n_chars,
                m=self.settings.kg_window_overlap_chars,
                workers=self.settings.kg_extract_workers,
            )
```

- [ ] **Step 2: 编译 + 冒烟（覆盖 _run_extraction 路径）**
Run: `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`
Expected: EXIT 0（py_compile + smoke「KG extraction boundary」会跑通自适应 n 路径 + 前端 lint）。

> 说明：`_run_extraction` 需要 configured LLM + 已解析 source 才会走到此处，单测搭建成本高；其正确性由 Task 2 的纯函数单测 + check.sh 的 KG 抽取冒烟共同覆盖（冒烟会调用抽取，从而经过新的 `plan_window_size`）。

- [ ] **Step 3: Commit**
```bash
git add backend/app/services/sqlite_repository.py
git commit -m "feat(kg): _run_extraction 用 plan_window_size 自适应窗口"
```

---

## Task 4: 全量校验 + 并发 rollout

**Files:** 校验 + 根 `.env`（部署侧，gitignored，不进提交）

- [ ] **Step 1: 后端 kg + 新单测**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/kg tests/test_adaptive_windows.py -q`
Expected: 全绿。

- [ ] **Step 2: 后端全量（无回归）**
Run: `cd backend && PYTHONPATH=. /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests -q`
Expected: 全绿。

- [ ] **Step 3: check.sh**
Run: `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`
Expected: EXIT 0。

- [ ] **Step 4: 并发 rollout（部署侧，手动）**
在根 `/Users/hzf/workspace/silicon_notebook/.env` 设 `KG_EXTRACT_WORKERS=100`（代码默认仍 16，不影响其它部署）。按 [[service-restart-prefs]] 基于 root master 重启服务。
**先验证高并发吞吐**：重启后对 innovus 重抽一次 `PYTHONPATH=backend python scripts/reextract_notebook.py nb-59ce4f4923`，看 `llm.jsonl` 里单调用 latency 是否仍 ~ 同量级（确认 100 并发不掉速）、超时是否归零。

---

## 自检：spec 覆盖
- level=clamp(C/workers,4000,8000) → Task 2。✓
- 等长切分 ceil(C/N) → Task 2（返回 balanced n）。✓
- 接入抽取 → Task 3。✓
- 三 config 旋钮 + 可选 override → Task 1。✓
- workers=100 + 吞吐验证 → Task 4 Step 4。✓
- 非目标（不改 make_windows 语义/效果回归）→ 不在计划。✓

## 风险
- 100 并发吞吐未验 → Task 4 Step 4 显式验证。
- 等长 n 可能略低于 w_min（如 9000→3000）：这是设计（w_min 是 level 下限，不是末窗口下限），不是 bug；测试已覆盖。
- `kg_window_target_chars` 默认 0 改变了默认行为（由固定 9000 → 自适应）：这是本期目的；仅 `_run_extraction` 读它，无其它依赖。
