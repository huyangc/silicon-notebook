# CI Portability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the complete repository gate install and pass from declared dependencies on a clean Ubuntu runner without developer-machine paths, while preserving the Apple Silicon Homebrew warm-gate target below 60 seconds.

**Architecture:** CI remains a read-only wrapper around `scripts/check.sh`. Harness tests locate committed fixtures from their own file location through shared pytest fixtures, a semantic AST contract rejects absolute path literals in the CI-executed harness tests, and Matplotlib becomes a direct declared dependency because the pytest controller imports it directly.

**Tech Stack:** Python 3.13, pytest/pytest-xdist, Python AST and `pathlib`, pip requirements, Node.js 22/npm, GitHub Actions, Bash.

## Global Constraints

- Work only in `/Users/hzf/workspace/silicon_notebook/.worktrees/ci-portability` on branch `codex/ci-portability`; do not touch the main checkout or other worktrees.
- Base is `origin/master` commit `e824489e`; fetch and re-check the base before publishing.
- Use `/opt/homebrew/Caskroom/miniconda/base/bin/python` for local development verification.
- `.github/workflows/ci.yml` must keep delegating test selection solely to `bash scripts/check.sh`; do not duplicate pytest roots, npm commands, or skip lists.
- Do not introduce Docker, devcontainers, uv/Poetry lock files, dependency-layer refactors, or caches for `node_modules`, virtualenvs, databases, `.local`, or Next.js build output.
- CI-executed tests must not depend on checkout absolute paths, `HOME`, current working directory, repository-external source documents, or undeclared third-party packages.
- The local Apple Silicon Homebrew warm gate must finish in less than 60 seconds. GitHub-hosted cold timing is observational only and must not become a failure threshold.
- Required branch protection remains disabled unless the user separately approves it.
- Preserve unrelated changes. Use `apply_patch` for edits and TDD RED→GREEN for behavior or contract changes.
- Update `README.md`, `README_zh.md`, and `AGENTS.md` together for the new development constraint.
- Do not push until task-level review, focused verification, clean-environment verification, and the local full gate are complete.

---

### Task 1: Make the committed harness fixtures checkout-relative

**Files:**
- Create: `backend/tests/test_ci_portability_contract.py`
- Create: `fangan/testcases/harness/tests/conftest.py`
- Modify: `fangan/testcases/harness/tests/test_cli.py:1-23`
- Modify: `fangan/testcases/harness/tests/test_demo_feedback.py:1-27`
- Modify: `fangan/testcases/harness/tests/test_perturbation.py:1-62`
- Modify: `fangan/testcases/harness/tests/test_run_all.py:1-17`
- Modify: `fangan/testcases/harness/tests/test_scorer.py:1-32`

**Interfaces:**
- Consumes: committed fixture tree rooted at `fangan/testcases`.
- Produces: session fixtures `testcases_root: pathlib.Path` and `gold_paths: tuple[pathlib.Path, ...]`; semantic contract `_absolute_path_literals(path: Path) -> tuple[tuple[int, str], ...]`.

- [ ] **Step 1: Write the failing semantic portability contract**

Create `backend/tests/test_ci_portability_contract.py`:

```python
"""CI-executed tests must be independent of a developer checkout path."""
from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath, PureWindowsPath


ROOT = Path(__file__).resolve().parents[2]
HARNESS_TESTS = ROOT / "fangan" / "testcases" / "harness" / "tests"


def _absolute_path_literals(path: Path) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value
        if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
            matches.append((node.lineno, value))
    return tuple(matches)


def test_ci_harness_tests_do_not_embed_absolute_paths() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): _absolute_path_literals(path)
        for path in sorted(HARNESS_TESTS.glob("test_*.py"))
        if _absolute_path_literals(path)
    }
    assert offenders == {}
```

- [ ] **Step 2: Run the contract to prove RED**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider -n0 \
  backend/tests/test_ci_portability_contract.py::test_ci_harness_tests_do_not_embed_absolute_paths -q
```

Expected: FAIL. The assertion reports exactly these five files with
`/Users/hzf/workspace/silicon_notebook` literals:

```text
test_cli.py
test_demo_feedback.py
test_perturbation.py
test_run_all.py
test_scorer.py
```

- [ ] **Step 3: Add shared checkout-relative fixtures**

Create `fangan/testcases/harness/tests/conftest.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def testcases_root() -> Path:
    return Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def gold_paths(testcases_root: Path) -> tuple[Path, ...]:
    return tuple(sorted(testcases_root.glob("*/ch*/gold.yaml")))
```

- [ ] **Step 4: Replace all five hard-coded test roots**

Replace `fangan/testcases/harness/tests/test_cli.py` with:

```python
import json
from pathlib import Path
import subprocess
import sys


def test_cli_gold_vs_itself(tmp_path: Path, testcases_root: Path) -> None:
    gold_dir = testcases_root / "engram" / "ch00_abstract"
    out_json = tmp_path / "r.json"
    out_md = tmp_path / "r.md"
    cmd = [
        sys.executable,
        "-m",
        "harness.score",
        "--gold",
        str(gold_dir),
        "--pred",
        str(gold_dir / "gold.yaml"),
        "--out",
        str(out_json),
        "--md",
        str(out_md),
    ]
    proc = subprocess.run(
        cmd,
        cwd=testcases_root,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["weighted_score"] == 100.0
    assert out_md.read_text(encoding="utf-8").strip() != ""
    assert "100.0" in proc.stdout
```

Replace `fangan/testcases/harness/tests/test_demo_feedback.py` with:

```python
import copy
from pathlib import Path

import yaml

from harness import report, scorer


def test_imperfect_candidate_produces_actionable_markdown(
    testcases_root: Path,
) -> None:
    gold_path = testcases_root / "engram" / "ch00_abstract" / "gold.yaml"
    gold = yaml.safe_load(gold_path.read_text(encoding="utf-8"))
    pred = copy.deepcopy(gold)
    pred["objects"] = pred["objects"][:-1]
    pred["evidence_atoms"] = pred["evidence_atoms"][:-1]
    if pred.get("relations"):
        pred["relations"][0]["relation_type"] = "WRONG_REL_TYPE"
    result = scorer.score_fixture(gold, pred)
    md = report.to_markdown(result, title="degraded-demo")

    assert result["weighted_score"] < 100.0
    assert ("false negatives" in md) or ("Type mismatches" in md)
    assert report.to_json(result).startswith("{")
```

Replace `fangan/testcases/harness/tests/test_perturbation.py` with:

```python
import copy
from pathlib import Path

import pytest
import yaml

from harness import scorer


@pytest.fixture
def architecture_gold_path(testcases_root: Path) -> Path:
    return testcases_root / "engram" / "ch02_architecture" / "gold.yaml"


def load(gold_path: Path) -> tuple[dict, dict]:
    gold = yaml.safe_load(gold_path.read_text(encoding="utf-8"))
    return gold, copy.deepcopy(gold)


def test_shifting_a_span_lowers_atom_iou_and_recall(
    architecture_gold_path: Path,
) -> None:
    gold, pred = load(architecture_gold_path)
    atom = pred["evidence_atoms"][0]["source_span"]
    atom["char_start"] += 100000
    atom["char_end"] += 100000
    result = scorer.score_fixture(gold, pred)
    assert result["stage_scores"]["evidence_atoms"] < 1.0


def test_flipping_an_atom_type_lowers_type_accuracy(
    architecture_gold_path: Path,
) -> None:
    gold, pred = load(architecture_gold_path)
    pred["evidence_atoms"][0]["atom_type"] = "DEFINITELY_WRONG"
    result = scorer.score_fixture(gold, pred)
    assert result["stages"]["evidence_atoms"]["type_accuracy"] < 1.0


def test_injecting_spurious_object_lowers_object_precision(
    architecture_gold_path: Path,
) -> None:
    gold, pred = load(architecture_gold_path)
    pred["objects"].append(
        {
            "id": "JUNK",
            "type": "ArticleClaim",
            "home_package": "PKG-NONE",
            "local_evidence_atom_ids": [],
            "supporting_context_atom_ids": [],
            "payload": {"statement": "totally unrelated fabricated claim xyz"},
        }
    )
    result = scorer.score_fixture(gold, pred)
    assert result["stages"]["objects"]["prf"]["precision"] < 1.0


def test_dropping_a_relation_lowers_relation_recall(
    architecture_gold_path: Path,
) -> None:
    gold, pred = load(architecture_gold_path)
    pred["relations"] = pred["relations"][:-1]
    result = scorer.score_fixture(gold, pred)
    assert result["stages"]["relations"]["prf"]["recall"] < 1.0


def test_extracting_forbidden_text_triggers_violation(
    architecture_gold_path: Path,
) -> None:
    gold, pred = load(architecture_gold_path)
    forbidden = None
    for entry in gold.get("do_not_extract") or []:
        forbidden = entry.get("text") or (entry.get("examples") or [None])[0]
        if forbidden:
            break
    if forbidden:
        pred.setdefault("mentions", []).append(
            {
                "id": "BAD",
                "text": forbidden,
                "type": "Concept",
                "atom_id": pred["evidence_atoms"][0]["id"],
            }
        )
        result = scorer.score_fixture(gold, pred)
        assert result["stages"]["do_not_extract"]["violations"] >= 1
```

Replace `fangan/testcases/harness/tests/test_run_all.py` with:

```python
from pathlib import Path

from harness import run_all


def test_run_all_gold_as_candidate_scores_100(
    tmp_path: Path,
    testcases_root: Path,
) -> None:
    aggregate = run_all.run(
        gold_root=str(testcases_root),
        pred_root=str(testcases_root),
        out_dir=str(tmp_path),
    )
    assert aggregate["chapters_scored"] == 14
    assert abs(aggregate["mean_weighted_score"] - 100.0) < 1e-9
    assert (tmp_path / "aggregate.json").exists()
    leaderboard = tmp_path / "leaderboard.md"
    assert leaderboard.exists()
    assert "ch00_abstract" in leaderboard.read_text(encoding="utf-8")
```

Replace `fangan/testcases/harness/tests/test_scorer.py` with:

```python
from pathlib import Path

import yaml

from harness import scorer


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_gold_files_found(gold_paths: tuple[Path, ...]) -> None:
    assert len(gold_paths) == 14


def test_gold_vs_gold_is_perfect(gold_paths: tuple[Path, ...]) -> None:
    for gold_path in gold_paths:
        gold = _read_yaml(gold_path)
        result = scorer.score_fixture(gold, gold)
        assert result["weighted_score"] == 100.0, (
            f"{gold_path} -> {result['weighted_score']}"
        )
        for bucket, score in result["stage_scores"].items():
            assert abs(score - 1.0) < 1e-9, (
                f"{gold_path} bucket {bucket} = {score}"
            )


def test_dropping_an_object_lowers_score(
    gold_paths: tuple[Path, ...],
) -> None:
    gold = _read_yaml(gold_paths[0])
    pred = _read_yaml(gold_paths[0])
    if pred.get("objects"):
        pred["objects"] = pred["objects"][:-1]
    result = scorer.score_fixture(gold, pred)
    assert result["weighted_score"] < 100.0
```

- [ ] **Step 5: Run focused GREEN tests from two working directories**

Run from the worktree:

```bash
PYTHONPATH=fangan/testcases /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider -n0 fangan/testcases/harness/tests -q
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider -n0 backend/tests/test_ci_portability_contract.py -q
```

Expected: harness `54 passed`; portability contract `1 passed`.

Then run with `/tmp` as the current directory:

```bash
cd /tmp
PYTHONPATH=/Users/hzf/workspace/silicon_notebook/.worktrees/ci-portability/fangan/testcases \
  /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  -p no:cacheprovider -n0 \
  /Users/hzf/workspace/silicon_notebook/.worktrees/ci-portability/fangan/testcases/harness/tests -q
```

Expected: `54 passed`; no lookup under `/Users/hzf/workspace/silicon_notebook/fangan/testcases`.

- [ ] **Step 6: Review and commit Task 1**

Run:

```bash
git diff --check
git status --short
```

Inspect that only the seven Task 1 files changed, then commit:

```bash
git add backend/tests/test_ci_portability_contract.py \
  fangan/testcases/harness/tests/conftest.py \
  fangan/testcases/harness/tests/test_cli.py \
  fangan/testcases/harness/tests/test_demo_feedback.py \
  fangan/testcases/harness/tests/test_perturbation.py \
  fangan/testcases/harness/tests/test_run_all.py \
  fangan/testcases/harness/tests/test_scorer.py
git commit -m "test: make CI harness fixtures portable"
```

---

### Task 2: Declare the pytest controller's Matplotlib dependency

**Files:**
- Modify: `backend/tests/test_ci_portability_contract.py`
- Modify: `backend/requirements.txt:15-20`

**Interfaces:**
- Consumes: `ROOT` from Task 1's portability contract.
- Produces: `_declared_requirement_names() -> frozenset[str]`; direct requirement `matplotlib>=3.10`.

- [ ] **Step 1: Add a failing dependency-closure contract**

Replace `backend/tests/test_ci_portability_contract.py` with:

```python
"""CI-executed tests must be independent of developer-local state."""
from __future__ import annotations

import ast
from pathlib import Path, PurePosixPath, PureWindowsPath
import re


ROOT = Path(__file__).resolve().parents[2]
HARNESS_TESTS = ROOT / "fangan" / "testcases" / "harness" / "tests"
REQUIREMENTS = ROOT / "backend" / "requirements.txt"
_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


def _absolute_path_literals(path: Path) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    matches: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value
        if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
            matches.append((node.lineno, value))
    return tuple(matches)


def _declared_requirement_names() -> frozenset[str]:
    names: set[str] = set()
    for raw_line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw_line.partition("#")[0].strip()
        if not line:
            continue
        match = _REQUIREMENT_NAME.match(line)
        assert match is not None, f"unsupported requirement line: {raw_line!r}"
        names.add(re.sub(r"[-_.]+", "-", match.group(0)).lower())
    return frozenset(names)


def test_ci_harness_tests_do_not_embed_absolute_paths() -> None:
    offenders = {
        path.relative_to(ROOT).as_posix(): _absolute_path_literals(path)
        for path in sorted(HARNESS_TESTS.glob("test_*.py"))
        if _absolute_path_literals(path)
    }
    assert offenders == {}


def test_pytest_controller_matplotlib_import_is_declared() -> None:
    assert "matplotlib" in _declared_requirement_names()
```

- [ ] **Step 2: Run the new contract to prove RED**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider -n0 \
  backend/tests/test_ci_portability_contract.py::test_pytest_controller_matplotlib_import_is_declared -q
```

Expected: FAIL with `assert 'matplotlib' in ...`.

- [ ] **Step 3: Add the direct dependency**

Insert immediately after `python-igraph>=0.11` in `backend/requirements.txt`:

```text
matplotlib>=3.10       # pytest controller 直接预热字体缓存；igraph drawing adapter 也会加载，CI 不得依赖开发机碰巧预装
```

Do not add an optional import fallback to `backend/tests/conftest.py`.

- [ ] **Step 4: Run dependency GREEN checks**

Run:

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider -n0 \
  backend/tests/test_ci_portability_contract.py \
  backend/tests/test_ci_workflow_contract.py -q
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pip check
```

Expected: both contract modules pass; `pip check` reports no broken requirements.

- [ ] **Step 5: Review and commit Task 2**

Run:

```bash
git diff --check
git diff -- backend/requirements.txt backend/tests/test_ci_portability_contract.py
```

Commit:

```bash
git add backend/requirements.txt backend/tests/test_ci_portability_contract.py
git commit -m "test: declare clean CI Python dependency"
```

---

### Task 3: Document the portable CI contract in all development guides

**Files:**
- Modify: `README.md:1083-1100`
- Modify: `README_zh.md:998-1015`
- Modify: `AGENTS.md:352-368`

**Interfaces:**
- Consumes: Task 1 checkout-relative fixture rule and Task 2 direct dependency rule.
- Produces: identical English/Chinese/agent development constraints; no runtime interface.

- [ ] **Step 1: Update the English README**

Append this paragraph to `README.md`'s `### GitHub Actions CI` section:

```markdown
CI portability is part of the gate contract: every CI-executed test locates
committed fixtures relative to its own repository files, never through a
developer checkout path or `HOME`; every third-party package imported during
test startup is declared in `backend/requirements.txt`. A clean hosted runner
must install and pass from those declarations alone. Lane timings remain
visible for observation, while the under-60-second target applies only to the
verified Apple Silicon Homebrew warm gate.

Developer-only gold-generation/build/validation scripts that consume external
PDF parse output remain outside `scripts/check.sh`; that exception never
applies to committed tests.
```

- [ ] **Step 2: Update the Chinese README**

Append the corresponding paragraph to `README_zh.md`'s
`### GitHub Actions CI` section:

```markdown
CI 可移植性属于门禁契约：所有由 CI 执行的测试都必须从当前仓库文件位置
定位已提交 fixture，禁止依赖开发机 checkout 绝对路径或 `HOME`；测试启动时
直接导入的第三方包必须声明在 `backend/requirements.txt`。干净 hosted runner
必须只凭这些声明即可安装并全绿。各 lane 时长继续输出供观察，60 秒内目标
只约束已验证的 Apple Silicon Homebrew warm gate。

依赖仓库外 PDF 解析产物的 gold 生成、构建与校验脚本仍属于 developer-only
工具并保持在 `scripts/check.sh` 之外；该例外绝不适用于已提交测试。
```

- [ ] **Step 3: Update AGENTS.md**

Add these bullets under `AGENTS.md`'s `### GitHub Actions CI` section:

```markdown
- CI-executed tests locate committed fixtures from `Path(__file__)`-anchored
  repository paths. They must not embed developer checkout paths, depend on
  `HOME`, or read repository-external source documents.
- Any third-party package imported during test startup is a direct declared
  dependency in `backend/requirements.txt`; a developer's preinstalled package
  is never evidence that CI can install the gate.
- Hosted-runner lane timings are observational. The under-60-second acceptance
  target applies to the verified Apple Silicon Homebrew warm gate, not a cold
  GitHub runner.
- Developer-only gold-generation/build/validation scripts that consume
  repository-external PDF parse output remain outside `scripts/check.sh`; this
  exception never applies to committed tests.
```

- [ ] **Step 4: Verify documentation consistency**

Run:

```bash
rg -n "CI portability|CI 可移植性|Hosted-runner lane timings" \
  README.md README_zh.md AGENTS.md
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider -n0 \
  backend/tests/test_architecture_documentation.py \
  backend/tests/test_ci_portability_contract.py \
  backend/tests/test_ci_workflow_contract.py -q
git diff --check
```

Expected: all three documents contain the new constraint; focused tests pass;
no whitespace errors.

- [ ] **Step 5: Commit Task 3**

```bash
git add README.md README_zh.md AGENTS.md
git commit -m "docs: define the portable CI contract"
```

---

### Task 4: Prove clean installation, bounded local verification, and exact-head review

**Files:**
- No tracked file changes expected.
- Ignored verification environment: `.local/ci-portability-venv/`

**Interfaces:**
- Consumes: all Task 1–3 commits and unchanged `.github/workflows/ci.yml`.
- Produces: clean-environment evidence, local warm-gate evidence, PR URL, exact pushed SHA, GitHub Actions result, and independent review result.

- [ ] **Step 1: Rebase only if the remote base advanced**

Run:

```bash
git fetch origin master
git rev-parse origin/master
git merge-base HEAD origin/master
```

If `origin/master` is still `e824489e`, continue. If it advanced, merge
`origin/master` into the branch without rewriting published history, then rerun
Tasks 1–3 focused tests before continuing.

- [ ] **Step 2: Create a clean Python environment from declarations**

Run:

```bash
/opt/homebrew/Caskroom/miniconda/base/bin/python -m venv --clear \
  .local/ci-portability-venv
.local/ci-portability-venv/bin/python -m pip install \
  -r backend/requirements.txt
.local/ci-portability-venv/bin/python -m pip check
.local/ci-portability-venv/bin/python -c \
  "import matplotlib.font_manager, igraph, pytest; print('clean imports ok')"
```

Expected: installation succeeds from `backend/requirements.txt`; `pip check`
is clean; the import command prints `clean imports ok`.

- [ ] **Step 3: Run the complete gate with the clean Python environment**

Run:

```bash
npm ci --prefix frontend
PYTHON_BIN="$PWD/.local/ci-portability-venv/bin/python" \
  BACKEND_PYTEST_WORKERS=4 bash scripts/check.sh
```

Expected: contracts, complete backend pytest, all frontend Node/component
tests, `tsc --noEmit`, and Next production build pass. Record lane timings but
do not fail the task solely because a clean/cold lane exceeds 60 seconds.

- [ ] **Step 4: Run the Homebrew warm gate under 60 seconds**

Run once to ensure caches reflect the final source, then measure the warm run:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
time PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python \
  bash scripts/check.sh
```

Expected: both runs exit 0; the measured warm wall time is less than 60
seconds. Do not rerun over a failure—preserve and diagnose the first failure.

- [ ] **Step 5: Perform final local hygiene checks**

Run:

```bash
git diff --check origin/master...HEAD
git status --short --branch
git log --oneline origin/master..HEAD
```

Expected: no tracked changes; only the design, plan, Task 1, Task 2, and Task 3
commits are ahead of `origin/master`.

- [ ] **Step 6: Task-level and whole-branch reviews**

For each implementation task, dispatch a reviewer subagent that did not write
the task. Reviewers must inspect the task's base/head diff and run focused
tests. Resolve every Critical or Important finding before continuing.

After local integration is green, dispatch a fresh whole-branch reviewer over
`origin/master...HEAD`, with special attention to:

- AST portability contract semantics rather than source line identity;
- fixture behavior from arbitrary `cwd`;
- direct dependency closure on Python 3.13;
- unchanged CI test selection and non-required branch protection;
- local warm timing versus observational CI timing.

Expected: Critical `0`, Important `0`.

- [ ] **Step 7: Push and open the PR**

Use the `github:yeet` skill. Push `codex/ci-portability`, open a ready PR, and
include:

- the two original GitHub failure roots;
- clean Python environment evidence;
- focused counts and local warm wall time;
- explicit statement that CI timings are observational and the check remains
  non-required;
- exact head SHA and independent review result.

- [ ] **Step 8: Wait for exact-head GitHub Actions**

Keep the original exact-head workflow run. Do not rerun, edit YAML, or change
branch protection while it is queued or in progress.

If it fails, use `github:gh-fix-ci` to inspect the failed step and full logs,
reproduce the root cause, then return to the relevant TDD task. If it succeeds,
verify contracts/backend/frontend steps and timings, update the PR body with
the exact run URL/result, and dispatch a final independent
`gpt-5.6-terra` high-reasoning review of the exact pushed green head.

Expected final state: PR open and mergeable, exact-head `CI / full-gate`
successful, final review Critical `0` / Important `0`, required check still
disabled, worktree preserved for feedback.
