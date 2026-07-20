# GitHub Actions CI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a secure, cost-bounded GitHub Actions workflow that installs the declared Python and Node dependencies and runs the repository's complete `scripts/check.sh` gate for pull requests, `master` pushes, and manual dispatches.

**Architecture:** `.github/workflows/ci.yml` contains one `CI / full-gate` job and delegates all test selection and lane management to `scripts/check.sh`. A PyYAML-based semantic contract test guards workflow events, permissions, runtimes, caches, dependency installation, and the full-gate invocation without asserting YAML lines or source layout.

**Tech Stack:** GitHub Actions, YAML, Python 3.13, PyYAML, Node.js 22, npm, pytest, existing `scripts/check.sh`.

## Global Constraints

- Work only in `/Users/hzf/workspace/silicon_notebook/.worktrees/github-actions-ci` on `codex/github-actions-ci`.
- The workflow must use `pull_request`, never `pull_request_target`.
- Workflow permissions must be exactly `contents: read`; checkout credentials must not persist.
- Pin GitHub-maintained actions to the reviewed full-length commit:
  checkout `de0fac2e4500dabe0009e67214ff5f5447ce83dd` (`v6.0.2`),
  setup-python `a309ff8b426b58ec0e2a45f0f869d46889d02405`
  (`v6.2.0`), and setup-node
  `48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e` (`v6.4.0`).
- Use one `ubuntu-24.04` job with Python `3.13`, Node.js `22`, and a 20-minute timeout.
- Install from `backend/requirements.txt` and `frontend/package-lock.json`; cache package-manager downloads only.
- The workflow must call `bash scripts/check.sh`; it must not copy or enumerate the gate's test roots.
- Set `PYTHON_BIN=python` and `BACKEND_PYTEST_WORKERS=4` for the hosted runner.
- Keep the local Apple Silicon warm-gate target under 60 seconds, but do not impose a 60-second GitHub timeout.
- Update `README.md`, `README_zh.md`, and `AGENTS.md` together.
- Do not configure branch protection or make the check required in this change.
- Finish with a pull request, wait for the real GitHub Actions result, then use an independent review subagent on the exact pushed head.

---

### Task 1: Add the semantic CI contract and workflow

**Files:**
- Create: `backend/tests/test_ci_workflow_contract.py`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `scripts/check.sh`, `backend/requirements.txt`, `frontend/package-lock.json`
- Produces: GitHub check `CI / full-gate`; semantic test helper `_load_workflow() -> dict[str, object]`

- [ ] **Step 1: Write the failing semantic workflow contract**

Create `backend/tests/test_ci_workflow_contract.py` with:

```python
"""GitHub Actions must remain a read-only wrapper around the complete gate."""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"


def _load_workflow() -> dict[str, object]:
    workflow = yaml.load(
        WORKFLOW_PATH.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(workflow, dict)
    return workflow


def _uses_step(job: dict[str, object], action: str) -> dict[str, object]:
    steps = job["steps"]
    assert isinstance(steps, list)
    matches = [
        step
        for step in steps
        if isinstance(step, dict) and step.get("uses") == action
    ]
    assert len(matches) == 1
    return matches[0]


def test_ci_events_permissions_and_concurrency_are_bounded() -> None:
    workflow = _load_workflow()

    assert workflow["name"] == "CI"
    events = workflow["on"]
    assert isinstance(events, dict)
    assert set(events) == {"pull_request", "push", "workflow_dispatch"}
    assert events["pull_request"] == {"branches": ["master"]}
    assert events["push"] == {"branches": ["master"]}
    assert events["workflow_dispatch"] == {}
    assert "pull_request_target" not in events

    assert workflow["permissions"] == {"contents": "read"}
    concurrency = workflow["concurrency"]
    assert isinstance(concurrency, dict)
    assert concurrency["cancel-in-progress"] == "true"
    assert "github.workflow" in concurrency["group"]
    assert "github.event.pull_request.head.ref" in concurrency["group"]
    assert "github.ref" in concurrency["group"]
    assert "secrets." not in repr(workflow)


def test_ci_job_installs_declared_dependencies_and_runs_only_the_complete_gate() -> None:
    workflow = _load_workflow()
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"full-gate"}
    job = jobs["full-gate"]
    assert isinstance(job, dict)

    assert job["name"] == "full-gate"
    assert job["runs-on"] == "ubuntu-24.04"
    assert job["timeout-minutes"] == "20"

    checkout = _uses_step(
        job,
        "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    )
    assert checkout["with"] == {"persist-credentials": "false"}

    python = _uses_step(
        job,
        "actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405",
    )
    assert python["with"] == {
        "python-version": "3.13",
        "cache": "pip",
        "cache-dependency-path": "backend/requirements.txt",
    }

    node = _uses_step(
        job,
        "actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e",
    )
    assert node["with"] == {
        "node-version": "22",
        "cache": "npm",
        "cache-dependency-path": "frontend/package-lock.json",
    }

    steps = job["steps"]
    assert isinstance(steps, list)
    run_steps = [
        step
        for step in steps
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]
    commands = [step["run"] for step in run_steps]
    assert "python -m pip install -r backend/requirements.txt" in commands
    assert "npm ci --prefix frontend" in commands
    assert commands[-1] == "bash scripts/check.sh"
    assert [
        command for command in commands if "scripts/check" in command
    ] == ["bash scripts/check.sh"]

    gate = run_steps[-1]
    assert gate["env"] == {
        "PYTHON_BIN": "python",
        "BACKEND_PYTEST_WORKERS": "4",
    }
```

- [ ] **Step 2: Run the contract and verify the red state**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider backend/tests/test_ci_workflow_contract.py -q
```

Expected: FAIL with `FileNotFoundError` for
`.github/workflows/ci.yml`.

- [ ] **Step 3: Add the minimal GitHub Actions workflow**

Create `.github/workflows/ci.yml` with:

```yaml
name: CI

on:
  pull_request:
    branches: [master]
  push:
    branches: [master]
  workflow_dispatch: {}

permissions:
  contents: read

concurrency:
  group: ${{ github.workflow }}-${{ github.event.pull_request.head.ref || github.ref }}
  cancel-in-progress: true

jobs:
  full-gate:
    name: full-gate
    runs-on: ubuntu-24.04
    timeout-minutes: 20
    steps:
      - name: Check out repository
        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd # v6.0.2
        with:
          persist-credentials: false

      - name: Set up Python
        uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0
        with:
          python-version: "3.13"
          cache: pip
          cache-dependency-path: backend/requirements.txt

      - name: Set up Node.js
        uses: actions/setup-node@48b55a011bda9f5d6aeb4c2d9c7362e8dae4041e # v6.4.0
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: frontend/package-lock.json

      - name: Install backend dependencies
        run: python -m pip install -r backend/requirements.txt

      - name: Install frontend dependencies
        run: npm ci --prefix frontend

      - name: Run complete offline gate
        env:
          PYTHON_BIN: python
          BACKEND_PYTEST_WORKERS: "4"
        run: bash scripts/check.sh
```

- [ ] **Step 4: Run the focused contracts and verify green**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider \
  backend/tests/test_ci_workflow_contract.py \
  backend/tests/test_check_script_contract.py -q
```

Expected: all CI and aggregate-gate contract tests pass.

- [ ] **Step 5: Inspect the semantic diff and commit**

Run:

```bash
git diff --check
git diff -- .github/workflows/ci.yml backend/tests/test_ci_workflow_contract.py
git add .github/workflows/ci.yml backend/tests/test_ci_workflow_contract.py
git commit -m "ci: run the complete gate in GitHub Actions"
```

Expected: one commit containing only the workflow and its semantic contract.

---

### Task 2: Synchronize development documentation

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: `CI / full-gate` from Task 1
- Produces: synchronized English, Chinese, and agent-facing CI operating contract

- [ ] **Step 1: Add the English CI contract**

In `README.md`, immediately after the paragraph explaining the local
60-second warm-gate target, add:

```markdown
### GitHub Actions CI

`.github/workflows/ci.yml` exposes the same complete gate as the single
`CI / full-gate` check. It runs for pull requests targeting `master`, pushes
to `master`, and manual dispatches on `ubuntu-24.04` with Python 3.13 and
Node.js 22. The workflow installs from `backend/requirements.txt` and
`frontend/package-lock.json`, then delegates test selection entirely to
`scripts/check.sh`.

The workflow is read-only, does not receive model or deployment secrets, and
uses four backend pytest workers to avoid oversubscribing the hosted runner.
Its 20-minute timeout includes dependency installation and is intentionally
separate from the under-60-second local Apple Silicon warm-gate target.
`CI / full-gate` is initially observational; make it a required `master` check
only after stable green pull-request and post-merge runs have been observed.
```

- [ ] **Step 2: Add the equivalent Chinese CI contract**

In `README_zh.md`, immediately after the paragraph explaining the local
60-second warm-gate target, add:

```markdown
### GitHub Actions CI

`.github/workflows/ci.yml` 把同一套完整门禁暴露为唯一的
`CI / full-gate` 检查。它在目标为 `master` 的 PR、`master` push 与手动触发时
运行，环境固定为 `ubuntu-24.04`、Python 3.13、Node.js 22。workflow 从
`backend/requirements.txt` 与 `frontend/package-lock.json` 安装依赖，然后把
测试选择完整委托给 `scripts/check.sh`。

该 workflow 只有读权限，不接收模型或部署 secrets，并把后端 pytest worker
限制为 4，避免 GitHub 托管 runner 过度抢占。20 分钟 timeout 包含依赖安装，
与 Apple Silicon 本地 warm gate 的 60 秒内目标刻意分开。初次接入时
`CI / full-gate` 仅用于观察；只有在 PR 与合并后的 `master` 都稳定绿跑后，
才把它设为 `master` 的 required check。
```

- [ ] **Step 3: Record the agent-facing maintenance constraint**

In `AGENTS.md`, immediately after its local 60-second verification paragraph,
add:

```markdown
### GitHub Actions CI

- `.github/workflows/ci.yml` is a read-only wrapper around
  `scripts/check.sh`; never duplicate test roots or frontend commands in the
  workflow.
- `CI / full-gate` runs on pull requests to `master`, pushes to `master`, and
  manual dispatches with Python 3.13, Node.js 22, and four backend pytest
  workers.
- Keep model/deployment secrets out of this workflow. Package-manager caches
  may contain downloads only; do not cache `node_modules`, virtualenvs,
  databases, or `.local` application state.
- The 20-minute hosted-runner timeout is not the local 60-second warm-gate
  target. Do not make the check required until stable green PR and post-merge
  runs have been observed and the user explicitly approves branch-protection
  changes.
```

- [ ] **Step 4: Verify documentation synchronization and focused contracts**

Run:

```bash
rg -n "CI / full-gate|GitHub Actions CI|20-minute|20 分钟" \
  README.md README_zh.md AGENTS.md
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider \
  backend/tests/test_ci_workflow_contract.py \
  backend/tests/test_architecture_documentation.py -q
git diff --check
```

Expected: each document describes the same workflow contract, all focused
tests pass, and Git reports no whitespace errors.

- [ ] **Step 5: Commit the synchronized documentation**

Run:

```bash
git add README.md README_zh.md AGENTS.md
git commit -m "docs: document the GitHub Actions gate"
```

Expected: a documentation-only commit covering all three required files.

---

### Task 3: Verify, publish, observe, and independently review

**Files:**
- Verify: all files changed since `origin/master`
- No new implementation files

**Interfaces:**
- Consumes: the workflow, semantic contract, and documentation from Tasks 1–2
- Produces: a draft pull request with a real `CI / full-gate` result and an independent review verdict

- [ ] **Step 1: Run the complete local acceptance gate**

Run:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Expected: all contracts, backend tests, frontend Node tests, frontend
component tests, TypeScript checks, and the production build pass; the local
wall-clock measurement remains under 60 seconds.

- [ ] **Step 2: Reconcile the latest base before publication**

Run:

```bash
git fetch origin master
git merge --no-edit origin/master
git status -sb
git diff --check origin/master...HEAD
```

Expected: the feature branch contains the latest `master`, has no unresolved
conflicts, and its diff contains only the CI feature.

If the merge adds a commit or resolves a conflict, rerun:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Expected: the merged branch remains fully green and below the local 60-second
warm target.

- [ ] **Step 3: Push the branch**

Run:

```bash
git push -u origin codex/github-actions-ci
```

Expected: `origin/codex/github-actions-ci` points to the verified local head.

- [ ] **Step 4: Open the draft pull request**

Create a draft PR in `huyangc/silicon-notebook` with:

```text
base: master
head: codex/github-actions-ci
title: ci: 接入完整 GitHub Actions 门禁
```

The body must cover:

```markdown
## 背景

#307 已建立可信、60 秒内的本地完整测试基线，但仓库尚未配置 GitHub
Actions，PR 与 master 没有自动门禁。

## 实现

- 新增单一 `CI / full-gate`，复用 `scripts/check.sh`
- 使用只读权限、无 secrets 的 `pull_request` / push / 手动触发
- 固定 Ubuntu 24.04、Python 3.13、Node 22，并缓存 pip/npm 下载
- 将 CI pytest 并发限制为 4；20 分钟 timeout 不冒充本地 60 秒目标
- 新增不依赖源码行数的语义 workflow 契约，并同步中英文与 Agent 文档

## 验证

- 本地完整 `scripts/check.sh`：通过，记录实际秒数
- GitHub `CI / full-gate`：等待本 PR 真实运行

## 治理边界

本 PR 不修改 branch protection。待 PR 与合并后的 master 稳定绿跑后，再单独
确认是否设为 required check。
```

Expected: GitHub creates one draft PR for the pushed branch.

- [ ] **Step 5: Wait for the real GitHub Actions verdict**

Inspect the exact pushed head until `CI / full-gate` reaches a terminal state.
Expected: success within the 20-minute job timeout.

If it fails, do not guess. Invoke `github:gh-fix-ci`, inspect the failing step
and logs, reproduce the root cause locally where possible, add a regression
contract when appropriate, push the verified fix, and wait for the replacement
run.

- [ ] **Step 6: Request independent review on the exact green head**

Invoke `superpowers:requesting-code-review` and dispatch one independent
review subagent using `gpt-5.6-terra` with `reasoning_effort: high`. Give it
the exact base SHA, head SHA, design specification, implementation plan, PR
URL, local verification result, and GitHub run result. Require review of:

```text
- workflow trigger and concurrency correctness
- least-privilege and fork-PR security
- cache safety and dependency reproducibility
- semantic contract quality (no source-line/layout coupling)
- consistency with scripts/check.sh and all three documentation files
- CI resource bounds and timeout semantics
```

Expected final line:

```text
Ready to merge: Yes
```

If the reviewer identifies a blocker, fix it with a focused regression test,
rerun the complete local gate, push, wait for GitHub green, and re-review the
new exact head.

- [ ] **Step 7: Final handoff**

Confirm:

```bash
git status -sb
git rev-parse HEAD
git rev-parse origin/codex/github-actions-ci
```

Expected: clean worktree and identical local/remote SHAs. Report the draft PR
URL, local timing, GitHub `CI / full-gate` result and duration, reviewer
verdict, and the fact that required-check enforcement remains intentionally
disabled.
