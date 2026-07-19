# Historical Test Baseline Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore one hermetic, complete, zero-failure local quality gate that covers every committed Python and frontend test suite before hosted CI is added.

**Architecture:** Keep `scripts/check.sh` as the single aggregate entry point, but make its coverage explicit and guarded: backend product tests, the committed extraction-scoring harness, MCP/contract smokes, recursively discovered frontend tests, TypeScript, and the production build. Repair shipped behavior when current guards expose a real defect, delete only demonstrably non-hermetic or redundant tests, and derive live schema documentation checks from executable `SCHEMA_VERSION`.

**Tech Stack:** Bash; Python 3.13 from `/opt/homebrew/Caskroom/miniconda/base/bin/python`; FastAPI; pytest/pytest-xdist; Node.js 22; `node --test`; TypeScript; Next.js 15.

## Global Constraints

- Work only in `/Users/hzf/workspace/silicon_notebook/.worktrees/historical-debt-baseline` on `codex/historical-debt-baseline`.
- Use `/opt/homebrew/Caskroom/miniconda/base/bin/python` for every backend command. For `scripts/check.sh`, pass it as `PYTHON_BIN`.
- Keep all checks offline: clear model, embedding, rerank, and MinerU configuration through `scripts/check.sh`; do not install dependencies or call external services.
- Do not add GitHub Actions in this pull request. Hosted CI is the next project and must call the local gate restored here.
- Never use `skip`, `xfail`, a narrowed collector, or weaker security/protocol assertions to manufacture a green result.
- Delete a test only when the plan identifies its replacement coverage or proves that it is machine-local/dead infrastructure.
- User-facing wording follows `AGENTS.md`「界面词汇表」: `Memory` prose → `记忆`, `抽取` → `分析`/`整理`, and `notebook` prose → `笔记本`.
- The exact MCP surface remains eleven tools: seven Memory tools plus four knowhow tools. The expected set in the smoke remains independent from the runtime registry.
- Documentation changes affecting setup, behavior, architecture, or constraints update `README.md`, `README_zh.md`, and `AGENTS.md` together.
- Do not claim a new product-spec feature in `fangan_done.md`.
- Use TDD for every behavior/configuration repair: demonstrate RED, make the minimum change, then demonstrate GREEN.
- Do not spawn implementation subagents. The one requested subagent is reserved for independent review after the draft PR exists.

---

## File Structure

### Create

| File | Responsibility |
| --- | --- |
| `backend/tests/test_check_script_contract.py` | Pins the aggregate gate's committed Python and frontend test roots so future edits cannot silently drop a suite. |
| `frontend/app/test-runner-config.test.mjs` | Pins ESM package mode used when Node tests import TypeScript modules, preventing the current module-type warning flood. |

### Modify

| File | Responsibility |
| --- | --- |
| `scripts/check.sh` | Add the deterministic extraction-scoring harness to the aggregate gate. |
| `scripts/smoke_memory_mcp.py` | Pin and report the exact eleven-tool MCP contract. |
| `frontend/app/memory-panel.tsx` | Replace nine transfer-flow `Memory` prose leaks with `记忆`. |
| `frontend/app/transfer-picker.tsx` | Replace the rendered `抽取` action with the established `整理` wording. |
| `backend/app/api/routes.py` | Mark the same-notebook transfer error as trusted user copy and use Chinese interface wording. |
| `backend/tests/test_architecture_documentation.py` | Derive current schema documentation assertions from executable `SCHEMA_VERSION`; keep historical v10 statements separate. |
| `frontend/package.json` | Declare ESM package mode for the Node test runner. |
| `README.md` | Document schema 20, eleven MCP tools, and the complete Python test roots. |
| `README_zh.md` | Chinese counterpart of the same live contracts. |
| `AGENTS.md` | Update the working contract for schema 20, eleven-tool smoke, and complete test collection. |
| `architecture.md` | Replace its stale live schema-13 statement with schema 20 while retaining explicitly historical v10 text. |
| `fangan/testcases/harness/README.md` | State that the deterministic harness self-tests are part of `scripts/check.sh`. |

### Delete

| File | Reason |
| --- | --- |
| `backend/tests/test_innovus_characterization.py` | Three tests depend on a developer-local `~/Downloads` document and become skips on another machine. Their parser/window behavior is already covered by committed fixtures in `test_structural_markdown.py`, `test_parsers_markdown.py`, `test_kg_parsing_structural.py`, `test_windowing_packing.py`, and `kg/test_windowing.py`. |
| `backend/tests/kg/test_eval_selftest.py` | It only runs when an ignored, uncommitted draft gold exists and duplicates `kg/test_eval.py::test_gold_vs_gold_is_perfect`. |
| `backend/tests/kg/conftest.py` | Its only fixture, `source_text`, has no consumer and points at a developer-local `/Users/hzf/workspace/pdf_parser` tree. |

---

### Task 1: Make Test Collection Complete and Hermetic

**Files:**
- Create: `backend/tests/test_check_script_contract.py`
- Modify: `scripts/check.sh:68-69`
- Modify: `README.md:990-994`
- Modify: `README_zh.md:904-908`
- Modify: `AGENTS.md:329-343`
- Modify: `fangan/testcases/harness/README.md:15-21`
- Delete: `backend/tests/test_innovus_characterization.py`
- Delete: `backend/tests/kg/test_eval_selftest.py`
- Delete: `backend/tests/kg/conftest.py`

**Interfaces:**
- Consumes: `scripts/check.sh` aggregate gate; `frontend/package.json` test script.
- Produces: a guarded gate that runs both committed Python test roots and the recursive frontend test command without external documents.

- [ ] **Step 1: Reproduce the non-hermetic skips**

Run:

```bash
INNOVUS_SAMPLE=/definitely/missing \
SILICON_NOTEBOOK_ENV_FILE='' \
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider \
  backend/tests/test_innovus_characterization.py \
  backend/tests/kg/test_eval_selftest.py -q -ra
```

Expected: four skips — one ignored draft-gold skip and three missing-Innovus-sample skips.

- [ ] **Step 2: Add the failing aggregate-gate contract**

Create `backend/tests/test_check_script_contract.py`:

```python
"""The local aggregate gate must not silently drop a committed test root."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_check_script_runs_every_committed_test_root():
    check = (ROOT / "scripts" / "check.sh").read_text(encoding="utf-8")
    assert '"$ROOT_DIR/backend/tests"' in check
    assert '"$ROOT_DIR/fangan/testcases/harness/tests"' in check
    assert "npm run test" in check

    package = json.loads(
        (ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    assert package["scripts"]["test"] == (
        "node --test $(find app -name '*.test.mjs' -type f -print)"
    )
```

- [ ] **Step 3: Run the new test and verify RED**

Run:

```bash
SILICON_NOTEBOOK_ENV_FILE='' \
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider \
  backend/tests/test_check_script_contract.py -q
```

Expected: FAIL because `scripts/check.sh` does not contain
`"$ROOT_DIR/fangan/testcases/harness/tests"`.

- [ ] **Step 4: Remove the three stale/non-hermetic test files**

Delete:

```text
backend/tests/test_innovus_characterization.py
backend/tests/kg/test_eval_selftest.py
backend/tests/kg/conftest.py
```

Do not copy the local documents into the repository. The retained committed
tests already cover:

```text
anchor removal       -> backend/tests/test_structural_markdown.py
code-block integrity -> backend/tests/test_structural_markdown.py
Markdown parsing     -> backend/tests/test_parsers_markdown.py
structural KG input  -> backend/tests/test_kg_parsing_structural.py
window packing       -> backend/tests/test_windowing_packing.py
window overlap       -> backend/tests/kg/test_windowing.py
evaluator self-score -> backend/tests/kg/test_eval.py
```

- [ ] **Step 5: Add the scoring harness to `scripts/check.sh`**

Immediately after the backend pytest command, add:

```bash
PYTHONPATH="$ROOT_DIR/backend:$ROOT_DIR" "$PYTHON_BIN" \
  -m pytest -p no:cacheprovider "$ROOT_DIR/fangan/testcases/harness/tests"
```

Keep it as a separate pytest invocation. The backend suite intentionally uses
`backend/pytest.ini` and xdist; the standalone harness has its own lightweight
root/package assumptions.

- [ ] **Step 6: Synchronize test-coverage documentation**

In `README.md`, replace the verification sentence with wording that explicitly
includes:

```text
the complete backend pytest suite, the deterministic extraction-scoring harness
under fangan/testcases/harness/tests, every recursively discovered frontend
*.test.mjs, Next.js tsc --noEmit, and the production frontend build
```

In `README_zh.md`, use:

```text
完整 backend pytest、fangan/testcases/harness/tests 下的确定性抽取评分
harness、递归发现的全部前端 *.test.mjs、Next.js tsc --noEmit 与 production build
```

In `AGENTS.md`「Verification」, replace `Complete backend pytest suite.` with:

```text
- Complete backend `pytest` suite plus the deterministic extraction-scoring
  harness under `fangan/testcases/harness/tests`; committed tests must not
  depend on developer-local source documents.
```

In `fangan/testcases/harness/README.md`, add after the self-test command:

```text
The same deterministic self-tests run from the repository-wide
`scripts/check.sh`; they require no model, network, or external source tree.
```

- [ ] **Step 7: Verify the focused result**

Run:

```bash
SILICON_NOTEBOOK_ENV_FILE='' \
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider \
  backend/tests/test_check_script_contract.py \
  backend/tests/test_structural_markdown.py \
  backend/tests/test_parsers_markdown.py \
  backend/tests/test_kg_parsing_structural.py \
  backend/tests/test_windowing_packing.py \
  backend/tests/kg/test_windowing.py \
  backend/tests/kg/test_eval.py -q -ra
```

Expected: PASS with no skipped tests.

Run:

```bash
SILICON_NOTEBOOK_ENV_FILE='' \
PYTHONPATH=backend:. \
/opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider fangan/testcases/harness/tests -q -ra
```

Expected: 54 passed, zero failed, zero skipped.

- [ ] **Step 8: Commit**

```bash
git add scripts/check.sh \
  backend/tests/test_check_script_contract.py \
  backend/tests/test_innovus_characterization.py \
  backend/tests/kg/test_eval_selftest.py \
  backend/tests/kg/conftest.py \
  README.md README_zh.md AGENTS.md \
  fangan/testcases/harness/README.md
git commit -m "test: make local test collection hermetic and complete"
```

---

### Task 2: Restore the Exact Eleven-Tool MCP Smoke

**Files:**
- Modify: `scripts/smoke_memory_mcp.py:17-25,171`
- Modify: `README.md:985-996`
- Modify: `README_zh.md:900-910`
- Modify: `AGENTS.md:339-341`

**Interfaces:**
- Consumes: the independently registered runtime tools exposed by
  `backend/app/api/mcp_server.py`.
- Produces: an independent exact-name assertion for seven Memory tools plus
  four knowhow tools.

- [ ] **Step 1: Verify the existing smoke is RED**

Run:

```bash
SILICON_NOTEBOOK_ENV_FILE='' \
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/smoke_memory_mcp.py
```

Expected: FAIL at `assert {item.name for item in listed.tools} == TOOLS`.

- [ ] **Step 2: Extend the independently pinned expected set**

Change `TOOLS` to:

```python
TOOLS = {
    "list_notebooks",
    "select_notebook",
    "search_agent_memory",
    "search_notebook_context",
    "get_memory",
    "ask_notebook",
    "propose_memory",
    "list_knowhow_tables",
    "get_knowhow_discrimination",
    "get_knowhow_row",
    "put_knowhow_cell_code",
}
```

Do not import `PUBLIC_TOOLS` into the smoke. Sharing the runtime's manifest
would let an accidental tool addition change both sides and create a false
green result.

- [ ] **Step 3: Correct the success diagnostic**

Replace the final print with:

```python
print(
    "memory MCP smoke: OK "
    "(11 tools, session isolation, candidate plane isolation)"
)
```

- [ ] **Step 4: Synchronize the verification docs**

Add to the `scripts/check.sh` verification description in `README.md`:

```text
The official-client MCP smoke pins exactly eleven tools: seven Memory tools
plus four knowhow tools.
```

Add the Chinese counterpart to `README_zh.md`:

```text
官方 client MCP smoke 精确锁定十一个工具：七个 Memory 工具加四个 knowhow 工具。
```

Replace the stale `AGENTS.md` verification bullet with:

```text
- Official MCP client smoke for exactly eleven tools (seven Memory plus four
  knowhow), session notebook selection, candidate exclusion from formal
  context, and same-user/same-notebook cross-Agent candidate recall.
```

- [ ] **Step 5: Verify GREEN**

Run:

```bash
SILICON_NOTEBOOK_ENV_FILE='' \
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/smoke_memory_mcp.py
```

Expected: exit 0 and the final line contains `OK (11 tools`.

- [ ] **Step 6: Commit**

```bash
git add scripts/smoke_memory_mcp.py README.md README_zh.md AGENTS.md
git commit -m "fix(mcp): restore the eleven-tool smoke contract"
```

---

### Task 3: Repair User-Facing Transfer Copy and Error Provenance

**Files:**
- Modify: `frontend/app/memory-panel.tsx:872,878,896,1024,1030,1036,1298-1299`
- Modify: `frontend/app/transfer-picker.tsx:151`
- Modify: `backend/app/api/routes.py:2175-2176`
- Test: `backend/tests/test_ui_vocabulary_guard.py`
- Test: `backend/tests/test_user_error.py`

**Interfaces:**
- Consumes: `AGENTS.md` user-facing vocabulary and
  `app.api.deps.user_error(status, message)`.
- Produces: vocabulary-compliant transfer UI and a trusted, displayable 400
  response for same-notebook knowhow transfer.

- [ ] **Step 1: Reproduce both guard failures**

Run:

```bash
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/check_ui_vocabulary.py
```

Expected: FAIL with nine `Memory` findings and one `抽取` finding.

Run:

```bash
SILICON_NOTEBOOK_ENV_FILE='' \
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider backend/tests/test_user_error.py -q
```

Expected: one failure naming
`app/api/routes.py:2176 '源与目标不能是同一个 notebook'`.

- [ ] **Step 2: Replace the nine rendered `Memory` prose occurrences**

Apply these literal replacements in `frontend/app/memory-panel.tsx`:

```text
源 Memory 标记为「弃用」 -> 源记忆标记为「弃用」
源 Memory 有更新的修改 -> 源记忆有更新的修改
条 Memory 到 -> 条记忆到
选中的 Memory 不在本页 -> 选中的记忆不在本页
所选 Memory 均非已确认状态 -> 所选记忆均非已确认状态
已确认的 Memory 才能复制/移动 -> 已确认的记忆才能复制/移动
所选 Memory 属于同一个笔记本 -> 所选记忆属于同一个笔记本
条 Memory（已排除 -> 条记忆（已排除
条 Memory` -> 条记忆`
```

Change only rendered strings. Internal type names, comments, API fields, and
the `MemoryPanel` component name remain unchanged.

- [ ] **Step 3: Use the established knowledge-graph action wording**

In `frontend/app/transfer-picker.tsx`, change:

```tsx
同时抽取到知识图谱
```

to:

```tsx
同时整理进知识图谱
```

This matches the two existing `memory-panel.tsx` controls.

- [ ] **Step 4: Mark the backend 400 as trusted user copy**

Replace:

```python
raise HTTPException(status_code=400, detail="源与目标不能是同一个 notebook")
```

with:

```python
raise user_error(400, "源与目标不能是同一个笔记本")
```

`user_error` is already imported at the top of `routes.py`; add no second
import.

- [ ] **Step 5: Verify focused GREEN**

Run:

```bash
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python scripts/check_ui_vocabulary.py
```

Expected: `UI vocabulary contract OK`.

Run:

```bash
SILICON_NOTEBOOK_ENV_FILE='' \
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider \
  backend/tests/test_ui_vocabulary_guard.py \
  backend/tests/test_user_error.py -q
```

Expected: all tests pass.

Run:

```bash
cd frontend && npm run test
```

Expected: all frontend tests pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/app/memory-panel.tsx \
  frontend/app/transfer-picker.tsx \
  backend/app/api/routes.py
git commit -m "fix(ui): align transfer copy with user-facing contracts"
```

---

### Task 4: Remove Node Test-Runner Module-Type Warning Noise

**Files:**
- Create: `frontend/app/test-runner-config.test.mjs`
- Modify: `frontend/package.json:2-5`

**Interfaces:**
- Consumes: Node 22 ESM semantics used by the existing `.mjs` tests when they
  import TypeScript modules.
- Produces: explicit ESM package mode and warning-free frontend test startup.

- [ ] **Step 1: Add a failing configuration test**

Create `frontend/app/test-runner-config.test.mjs`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const packageJson = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
);

test("frontend Node tests import TypeScript modules under explicit ESM mode", () => {
  assert.equal(packageJson.type, "module");
});
```

- [ ] **Step 2: Verify RED**

Run:

```bash
cd frontend && node --test app/test-runner-config.test.mjs
```

Expected: FAIL because `packageJson.type` is `undefined`.

- [ ] **Step 3: Declare frontend ESM package mode**

Add after `"private": true` in `frontend/package.json`:

```json
"type": "module",
```

No lockfile change is required; package type is runtime metadata, not a
dependency.

- [ ] **Step 4: Verify GREEN and no module-type warning**

Run:

```bash
cd frontend && node --test app/test-runner-config.test.mjs
```

Expected: one passing test.

Run:

```bash
cd frontend && npm run test
```

Expected: all frontend tests pass and no
`[MODULE_TYPELESS_PACKAGE_JSON]` warning appears.

- [ ] **Step 5: Commit**

```bash
git add frontend/package.json frontend/app/test-runner-config.test.mjs
git commit -m "test(frontend): declare ESM mode for the Node test runner"
```

---

### Task 5: Make Live Schema Documentation Follow `SCHEMA_VERSION`

**Files:**
- Modify: `backend/tests/test_architecture_documentation.py:1-10,610-640`
- Modify: `README.md:44-47`
- Modify: `README_zh.md:44`
- Modify: `AGENTS.md:152-155`
- Modify: `architecture.md:47`

**Interfaces:**
- Consumes: `app.repositories.sqlite.migrations.SCHEMA_VERSION`.
- Produces: live schema-20 documentation in four live reference documents and
  a guard that fails automatically on the next version bump.

- [ ] **Step 1: Replace the false-green guard before editing docs**

Add this import near the other top-level imports in
`backend/tests/test_architecture_documentation.py`:

```python
from app.repositories.sqlite.migrations import SCHEMA_VERSION
```

Replace `test_repository_schema_baseline_wording_is_exact_and_not_stale` with:

```python
def test_live_schema_docs_follow_executable_version():
    english_version = f"The current schema version is {SCHEMA_VERSION}."
    chinese_version = f"当前 schema 版本为 {SCHEMA_VERSION}。"
    migration_span = f"v10–v{SCHEMA_VERSION}"

    for name in ("README.md", "AGENTS.md"):
        text = _read(name)
        assert english_version in text
        assert re.findall(
            r"The current schema version is (\d+)\.", text
        ) == [str(SCHEMA_VERSION)]
        assert migration_span in text

    for name in ("README_zh.md", "architecture.md"):
        text = _read(name)
        assert chinese_version in text
        assert re.findall(
            r"当前 schema 版本为 (\d+)[。]", text
        ) == [str(SCHEMA_VERSION)]
        assert migration_span in text


def test_repository_composition_history_keeps_v10_baseline():
    historical_chinese = (
        "本次重构不改变其 master 基线已有的 schema 版本（`SCHEMA_VERSION = 10`）。"
        "已提交的 v9 兼容 fixture 会经由既有 v10 migration 升级，并保持可读。"
    )
    for name in ("architecture.md", "fangan_done.md") + COMPOSITION_HISTORY_DOCS:
        assert historical_chinese in _read(name), (
            f"{name} is missing the historical schema statement"
        )

    for name in COMPOSITION_HISTORY_DOCS:
        text = _read(name)
        for stale in (
            "SCHEMA_VERSION=9",
            "SCHEMA_VERSION = 9",
            "SCHEMA_VERSION 保持 9",
            "SCHEMA_VERSION remains 9",
            "schema v9 and frozen-master",
        ):
            assert stale not in text, (
                f"{name} retains stale schema wording: {stale}"
            )
```

The two tests intentionally distinguish live references from historical design
records. Do not rewrite old documents that correctly describe their v10
baseline.

- [ ] **Step 2: Verify the new guard is RED**

Run:

```bash
SILICON_NOTEBOOK_ENV_FILE='' \
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider \
  backend/tests/test_architecture_documentation.py \
  -k 'schema or composition_history' -q
```

Expected: `test_live_schema_docs_follow_executable_version` fails because the
live documents declare 15 or 13 while `SCHEMA_VERSION == 20`.

- [ ] **Step 3: Update the English live schema statement**

Use this text in both `README.md` and `AGENTS.md`:

```text
The current schema version is 20. The committed v9 compatibility fixture
upgrades through migrations v10–v20 and remains readable. Those migrations
cover compatibility and SQLite hot-path indexes (v10–v12), Memory/Agent and
Memory-derived source links/indexes (v13–v15), knowhow tables and cell code
(v16/v18), paper metadata (v17), source-linked assets (v19), and multi-domain
reference-library mounts plus promotion targets (v20).
```

- [ ] **Step 4: Update the Chinese live schema statement**

Use this text in `README_zh.md` and for the live schema sentence in
`architecture.md`:

```text
当前 schema 版本为 20。已提交的 v9 兼容 fixture 会经由 v10–v20 migration
升级并保持可读：v10–v12 覆盖兼容与 SQLite 热路径索引，v13–v15 覆盖
Memory/Agent 与 Memory 派生源 link/index，v16/v18 覆盖 knowhow 表与格子代码，
v17 覆盖论文元数据，v19 覆盖来源内嵌图片资产，v20 覆盖多领域参考库挂载与晋升目标。
```

Keep the explicitly historical v10 paragraph in `architecture.md` unchanged.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
SILICON_NOTEBOOK_ENV_FILE='' \
PYTHONPATH=backend \
/opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider \
  backend/tests/test_architecture_documentation.py \
  -k 'schema or composition_history' -q
```

Expected: both live and historical schema tests pass.

Run:

```bash
rg -n "current schema version is 15|当前 schema 版本为 (13|15)" \
  README.md README_zh.md AGENTS.md architecture.md
```

Expected: no matches.

- [ ] **Step 6: Commit**

```bash
git add backend/tests/test_architecture_documentation.py \
  README.md README_zh.md AGENTS.md architecture.md
git commit -m "docs: derive live schema contract from version 20"
```

---

### Task 6: Run the Complete Trusted Baseline

**Files:**
- Verify only. A newly observed failure makes this task RED and sends execution
  back to the responsible earlier task; do not patch code inside the aggregate
  verification step.

**Interfaces:**
- Consumes: all deliverables from Tasks 1–5.
- Produces: fresh evidence that the single aggregate entry point is complete,
  offline, warning-clean on the frontend, and green.

- [ ] **Step 1: Confirm active test roots contain no skip/xfail escape hatches**

Run:

```bash
rg -n \
  "pytest\\.(skip|xfail)|pytest\\.mark\\.(skip|skipif|xfail)|@unittest\\.skip" \
  backend/tests fangan/testcases/harness/tests
```

Expected: no matches.

- [ ] **Step 2: Confirm there are no unaccounted test roots**

Run:

```bash
find . -type f -name 'test_*.py' \
  -not -path './frontend/node_modules/*' \
  -not -path './.local/*'
```

Expected: every result is under either `backend/tests/` or
`fangan/testcases/harness/tests/`.

Run:

```bash
find . -type f \
  \( -name '*.test.mjs' -o -name '*.test.js' \
     -o -name '*.spec.mjs' -o -name '*.spec.js' \) \
  -not -path './frontend/node_modules/*' \
  -not -path './.local/*'
```

Expected: every result is under `frontend/app/`, which the package test command
recursively discovers.

- [ ] **Step 3: Run the complete aggregate gate**

Run:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python \
bash scripts/check.sh
```

Expected, in order:

```text
backend syntax/import preflight: pass
smoke_backend.py: pass offline
smoke_memory_mcp.py: OK (11 tools ...)
ask-mode/object-label/UI-vocabulary contracts: pass
backend/tests: zero failed, zero skipped
fangan/testcases/harness/tests: 54 passed
frontend node tests: zero failed, zero skipped
tsc --noEmit: pass
next build: pass
command exit: 0
```

- [ ] **Step 4: Run diff hygiene**

Run:

```bash
git diff --check
git diff --check origin/master...HEAD
git status --short --branch
```

Expected: no whitespace errors; only intentional branch commits ahead of
`origin/master`; no untracked source files.

---

### Task 7: Publish the Draft PR and Run the Requested Independent Review

**Files:**
- No planned source edits before review.
- Review fixes may modify only files already in scope or add a narrowly focused
  regression test for a confirmed finding.

**Interfaces:**
- Consumes: verified branch `codex/historical-debt-baseline`.
- Produces: one draft GitHub PR and one independent post-PR review.

- [ ] **Step 1: Review the final branch diff**

Run:

```bash
git log --oneline origin/master..HEAD
git diff --stat origin/master...HEAD
git diff --check origin/master...HEAD
```

Expected: the design/plan plus the bounded test-baseline repairs from Tasks
1–5; no GitHub Actions files and no unrelated runtime debt.

- [ ] **Step 2: Push the branch**

```bash
git push -u origin codex/historical-debt-baseline
```

- [ ] **Step 3: Open a draft PR**

Use the GitHub publishing workflow. The PR body must include this evidence
table with actual command results:

```markdown
| Baseline defect | Root cause | Resolution | Verification |
| --- | --- | --- | --- |
| MCP smoke stopped before pytest | Seven-tool expected set predated knowhow MCP tools | Pin all eleven names independently | `smoke_memory_mcp.py` |
| UI vocabulary guard failed | Transfer UI shipped internal `Memory`/`抽取` prose | Use `记忆`/`整理` | vocabulary script + guard pytest |
| Chinese 400 was hidden | Bare `HTTPException` lacked trusted-user marker | Use `user_error` with interface wording | `test_user_error.py` |
| Schema docs falsely passed at 15 | Test hard-coded the same stale sentence | Derive live assertions from `SCHEMA_VERSION` | architecture documentation tests |
| Harness was outside aggregate gate | `check.sh` only targeted `backend/tests` | Add deterministic harness root and guard it | harness pytest + aggregate gate |
| Four tests skipped off the developer machine | Local Innovus/draft-gold dependencies | Delete redundant/non-hermetic tests and dead fixture | replacement suites + zero-skip full run |
| Frontend tests emitted module-mode warnings | Package mode was implicit | Declare and test ESM mode | frontend tests/build |
```

State explicitly that GitHub Actions is deferred to the next PR.

- [ ] **Step 4: Start exactly one review subagent after the PR exists**

Read the full `superpowers:requesting-code-review` reviewer template, then
spawn one reviewer with:

```text
model: gpt-5.6-terra
reasoning_effort: high
scope: origin/master...codex/historical-debt-baseline and the created draft PR
focus: weakened guards, lost coverage, non-hermetic behavior, documentation
       drift, incorrect test deletion, and whether scripts/check.sh truly
       collects every committed test root
```

Use `gpt-5.6-sol` high only if the actual diff unexpectedly crosses database
migration logic, concurrency, or a security-sensitive runtime boundary.

- [ ] **Step 5: Independently validate every review finding**

For each finding:

1. reproduce it against the branch;
2. reject it with evidence if it conflicts with the approved contract;
3. otherwise add/run a focused RED test;
4. implement the minimum fix;
5. rerun the focused test;
6. rerun the complete `scripts/check.sh`.

Do not spawn a second reviewer.

- [ ] **Step 6: Push confirmed review fixes**

```bash
git push
```

Update the draft PR verification section with the post-review
`scripts/check.sh` result.
