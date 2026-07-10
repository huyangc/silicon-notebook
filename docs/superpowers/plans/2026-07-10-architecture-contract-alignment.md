# Architecture Contract Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让架构文档准确描述当前已测试的 Ask lifecycle、two-tier 排序与两列 workspace，并用自动化测试阻止这些契约再次漂移。

**Architecture:** 本阶段只修改文档和文档契约测试，不改变任何运行时代码。测试读取四份架构文档，要求当前语义存在且已知的旧语义不存在；`architecture.md` 改为稳定的组件/数据流/边界说明，避免维护容易过期的全量 endpoint 和表枚举。

**Tech Stack:** Markdown、Python 3.11+、pytest。

## Global Constraints

- 当前代码与已通过的回归测试是行为真相；文档追随代码，不反向改变 runtime。
- 同步更新 `README.md`、`README_zh.md`、`AGENTS.md`；`architecture.md` 作为架构说明同时修复。
- Ask 断连保持 detached execution；显式 interrupt 才调用 cancel endpoint。
- tier 排序保持纯相关度，只有 score 完全相同时 base 优先；不得恢复 `1.20` 乘数。
- workspace 保持两列，不恢复固定 Studio sidebar。
- 不增加依赖，不修改 endpoint、schema、Python/TypeScript runtime 文件。
- 后端命令使用已安装 `backend/requirements.txt` 的隔离解释器；通过激活环境或设置通用的 `PYTHON_BIN=/path/to/python` 运行脚本。

---

### Task 1: 添加行为契约测试并同步四份架构文档

**Files:**
- Create: `backend/tests/test_architecture_documentation.py`
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Replace: `architecture.md`

**Interfaces:**
- Consumes: repository-root `README.md`、`README_zh.md`、`AGENTS.md`、`architecture.md`。
- Produces: pytest tests that fail when detached Ask、exact-score-tie tier policy or two-column workspace wording regresses。
- Produces the exact phrases asserted by the tests while preserving all current setup commands, environment-variable names and product constraints.

- [ ] **Step 1: Write the failing tests**

```python
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_ask_disconnect_documentation_matches_detached_worker_contract():
    agents = _read("AGENTS.md")
    readme = _read("README.md")
    readme_zh = _read("README_zh.md")
    architecture = _read("architecture.md")

    assert "A transport disconnect only stops delivery to that client" in agents
    assert "continues in the background" in readme
    assert "断开连接只会停止当前客户端继续接收" in readme_zh
    assert "transport 断连只停止向该客户端继续推送" in architecture
    assert "frontend abort/client disconnect" not in agents
    assert "Client disconnect / abort must propagate" not in agents


def test_tier_documentation_matches_exact_score_tie_contract():
    agents = _read("AGENTS.md")
    readme = _read("README.md")
    readme_zh = _read("README_zh.md")
    architecture = _read("architecture.md")

    assert "only on an exact score tie" in agents
    assert "only when their relevance scores are exactly equal" in readme
    assert "仅在相关度分数完全相同时" in readme_zh
    assert "只在 score 完全相同时让 base 先排" in architecture
    for text in (agents, readme, readme_zh, architecture):
        assert "base `1.20`" not in text
        assert "base 1.20" not in text


def test_architecture_document_describes_current_workspace_boundary():
    architecture = _read("architecture.md")
    assert "来源栏 + Ask/Knowledge 主区域的两列 workspace" in architecture
    assert "前端（单文件）" not in architecture
    assert "工作区三栏" not in architecture
```

- [ ] **Step 2: Run tests and confirm RED**

Run: `cd backend && python -m pytest tests/test_architecture_documentation.py -q`
Expected: three failures because the current documents still contain the obsolete contracts.

- [ ] **Step 3: Correct Ask lifecycle wording**

Document the two distinct events:

```text
transport disconnect/navigation/refresh -> stop delivery only; detached job continues
explicit interrupt control -> POST cancel endpoint -> cancellation event -> worker stops
```

- [ ] **Step 4: Correct tier authority wording**

State that retrieval score is tier-blind and base is the secondary key only on an exact score tie. Keep the answer-prompt contradiction rule as a separate synthesis policy.

- [ ] **Step 5: Replace architecture.md with stable current boundaries**

The replacement document must contain these sections:

```markdown
# silicon-notebook 架构
## 1. 真实行为与验证
## 2. 运行时组件
## 3. 核心数据流
## 4. 关键行为契约
## 5. 当前模块边界
## 6. 已知架构债务与整改顺序
## 7. 验证命令
```

It must describe the SQLite facade + identity/sharing mixins, FastAPI router, KG/retrieval/report services, two-column frontend workspace, detached Ask job lifecycle, exact-score-tie tier ordering, explicit KG/index maintenance, and the six remediation phases from the approved design.

- [ ] **Step 6: Run documentation tests and confirm GREEN**

Run: `cd backend && python -m pytest tests/test_architecture_documentation.py tests/test_ask_stream_cancel.py tests/test_two_tier_federated.py -q`
Expected: all tests pass.

- [ ] **Step 7: Check synchronized prose**

Run: `rg -n "1\\.20|frontend abort/client disconnect|Client disconnect / abort must propagate|前端（单文件）|工作区三栏" README.md README_zh.md AGENTS.md architecture.md`
Expected: no matches.

### Task 2: 完成记录、完整验证与发布

**Files:**
- Modify: `fangan_done.md`
- Modify: `docs/superpowers/specs/2026-07-10-architecture-remediation-design.md` only if implementation exposed a contradiction.
- Modify: `docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md` checkbox status after execution is not required; git history is the execution record.

**Interfaces:**
- Produces a factual architecture-remediation entry with the exact verified test totals.

- [ ] **Step 1: Add the completed architecture-contract entry**

Record that current code/tests became authoritative, the three stale contracts were repaired, and the documentation regression test is part of the normal pytest suite. Do not claim later remediation phases are complete.

- [ ] **Step 2: Run formatting and conflict checks**

Run: `git diff --check`
Expected: exit 0.

Run: `rg -n "^(<<<<<<<|=======|>>>>>>>)" README.md README_zh.md AGENTS.md architecture.md fangan_done.md backend/tests/test_architecture_documentation.py`
Expected: no matches.

- [ ] **Step 3: Run the full gate**

Run: `PYTHON_BIN=/path/to/python bash scripts/check.sh`
Expected: backend, frontend tests, TypeScript and Next.js production build all pass.

- [ ] **Step 4: Commit**

```bash
git add README.md README_zh.md AGENTS.md architecture.md fangan_done.md \
  backend/tests/test_architecture_documentation.py \
  docs/superpowers/specs/2026-07-10-architecture-remediation-design.md \
  docs/superpowers/plans/2026-07-10-architecture-contract-alignment.md
git commit -m "Align architecture documentation with current behavior"
```

- [ ] **Step 5: Synchronize and publish**

Fetch and merge the latest `origin/master`, rerun the full gate if the merge is non-empty, push `codex/architecture-remediation-phase3`, and open a draft PR whose body lists the three repaired contracts and verification evidence.
