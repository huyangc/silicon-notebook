# Architecture Contract Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让架构文档准确描述当前已测试的 Ask lifecycle、mode-specific federation/two-tier 排序、三 tab 两列 workspace 与 source cleanup，并用自动化测试阻止这些契约再次漂移。

**Architecture:** 本阶段只修改文档和文档契约测试，不改变任何运行时代码。测试读取 README、双语 README、AGENTS、architecture、fangan 账本与本阶段 design/plan 七份契约文档，要求当前语义存在且已知的旧语义不存在；`architecture.md` 改为稳定的组件/数据流/边界说明，避免维护容易过期的全量 endpoint 和表枚举。

**Tech Stack:** Markdown、Python 3.11+、pytest。

## Global Constraints

- 当前代码与已通过的回归测试是行为真相；文档追随代码，不反向改变 runtime。
- 同步更新 `README.md`、`README_zh.md`、`AGENTS.md`；`architecture.md` 作为架构说明同时修复。
- Ask 断连保持 detached execution；显式 interrupt 才调用 cancel endpoint。
- `chunk` 基线只读取 active notebook 的 chunk；可选 KG overlay / PPR 才可能加入 federated KG 上下文与 base-backed chunk，`graph` / `reasoning` 使用 federated KG 路径。
- exact-score 的 `base` 次序只适用于知识对象命中；`federated_retrieve_relations()` 的关系命中仍只按 score 排序，不得恢复 `1.20` 乘数或把次序规则扩展到 relation hits。
- workspace 保持来源栏 + 主栏两列，主栏为问答 / 知识库 / 深度报告三个 tab，不恢复固定 Studio sidebar。「分析」菜单只含晋升队列（admin）、tier 切换（admin）与边审查队列；看板、Schema、全屏知识图谱是独立顶栏动作。
- 重新解析保留 source 行与原始文件，替换 source element / chunk 及 embedding，并先清理 extraction run 与 source-derived knowledge；删除再删除 source 行与本地文件。不得宣称会清理文章产物。
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
- Consumes: repository-root `README.md`、`README_zh.md`、`AGENTS.md`、`architecture.md`、`fangan_done.md` 与本阶段 design/plan。
- Produces: pytest tests that fail when detached Ask、mode-specific federation、knowledge-hit-only exact-score tie、three-tab/two-column workspace or source-cleanup wording regresses。
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
    assert "Only the explicit interrupt path" in agents


def test_retrieval_documentation_scopes_federation_and_tier_tie_break_by_path():
    assert "Baseline `chunk` retrieval reads chunks from the active notebook only" in _read("README.md")
    assert "exact-score 的 `base` 次序只适用于知识对象命中" in _read("architecture.md")
    assert "`federated_retrieve_relations()` 的关系命中只按 score 降序" in _read("architecture.md")


def test_architecture_document_describes_current_workspace_boundary():
    architecture = _read("architecture.md")
    assert "来源栏 + 主区域的两列 workspace" in architecture
    assert "问答 / 知识库 / 深度报告三个 tab" in architecture
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

按 mode 记录联合范围：chunk 基线 active-only，可选 KG overlay/PPR 可加入 federated KG/base-backed chunk，graph/reasoning 使用 federated KG。exact-score 的 base 次序仅属于 `federated_retrieve()` 知识对象命中；`federated_retrieve_relations()` 的关系命中仍只按 score 排序。回答 prompt 的 base-wins 冲突规则继续作为独立合成策略。

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

It must describe the SQLite facade + identity/sharing mixins, FastAPI router, KG/retrieval/report services, two-column frontend workspace with 问答 / 知识库 / 深度报告三个 tab, detached Ask job lifecycle, mode-specific federation, knowledge-hit-only exact-score tier ordering, precise source cleanup, explicit KG/index maintenance, and the six remediation phases from the approved design. 「分析」菜单只含晋升队列（admin）、tier 切换（admin）与边审查队列；看板、Schema、全屏知识图谱是独立顶栏动作。

- [ ] **Step 6: Run documentation tests and confirm GREEN**

Run: `cd backend && python -m pytest tests/test_architecture_documentation.py tests/test_ask_stream_cancel.py tests/test_two_tier_federated.py tests/test_relation_retrieval.py tests/test_source_reverse_index.py tests/test_report_api.py -q`
Expected: all tests pass.

- [ ] **Step 7: Check synchronized prose**

Run the documentation contract test, then scan the synchronized documents for obsolete cancellation, tier-weight, single-file-frontend, and three-column-workspace claims.
Expected: no obsolete claims.

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
