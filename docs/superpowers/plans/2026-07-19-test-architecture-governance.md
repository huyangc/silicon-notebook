# Test Architecture Governance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace line- and source-layout-coupled tests with semantic or executable contracts and make the complete local quality gate pass in less than 60 seconds for three consecutive warm runs.

**Architecture:** Python and TypeScript source guards share semantic indexes whose comparable identities exclude source positions. Frontend behavior moves to pure models or Vitest/jsdom component tests while repository-wide semantic guards stay static. The complete gate runs three bounded, independently timed lanes, with backend hotspots fixed at their root before worker counts are tuned.

**Tech Stack:** Python 3.13 from Homebrew/Miniconda, pytest 9, pytest-xdist 3.8, Python `ast`, Node 22 type stripping, TypeScript compiler API, React 19, Vitest, jsdom, React Testing Library, Next.js 15, Bash.

## Global Constraints

- Work only in `.worktrees/test-architecture-governance` on `codex/test-architecture-governance`.
- Deliver all changes in one pull request.
- Use `/opt/homebrew/Caskroom/miniconda/base/bin/python` through `PYTHON_BIN`.
- No test, fixture, manifest, or generator may use source line number as identity, allowlist key, or expected assertion.
- A line number may appear only as non-comparable failure-diagnostic metadata.
- Do not add skips, xfails, collection narrowing, or weaker replacement assertions to meet the time budget.
- Keep all smoke, contract, backend, deterministic harness, frontend Node, frontend component, TypeScript, and production-build layers in `scripts/check.sh`.
- Keep verification offline and leave MinerU and remote model providers disabled.
- `node:test` remains the runner for pure logic and semantic static guards; only `*.component.test.tsx` uses Vitest/jsdom.
- Do not use jsdom to claim pixel geometry or computed responsive layout.
- Keep the frozen v9 repository fixture unchanged.
- Update `README.md`, `README_zh.md`, and `AGENTS.md` together when the development contract changes.
- Do not use implementation subagents. Create one `gpt-5.6-sol` high-reasoning review subagent only after the draft PR exists, then reuse that reviewer until it reports `Ready to merge: Yes`.

---

## File Structure

### Backend semantic contract

- Create `backend/tests/architecture/__init__.py`: marks shared architecture-test support as a package.
- Create `backend/tests/architecture/semantic_source.py`: parses Python sources once and exposes source-position-independent findings.
- Create `backend/tests/architecture/repository_contract.py`: holds the single current repository surface/ownership contract.
- Create `backend/tests/test_semantic_source.py`: mutation-style unit tests for semantic identity and diagnostics.
- Create `backend/tests/test_test_architecture_policy.py`: prevents line identities, historical task allowlists, skips, and xfails from returning.
- Create `backend/tests/test_repository_dependency_contract.py`: import and layer-direction assertions.
- Create `backend/tests/test_repository_surface_contract.py`: compatibility exports, facade surface, and consumer assertions.
- Create `backend/tests/test_repository_patch_contract.py`: private seams and monkeypatch target assertions.
- Modify `backend/tests/conftest.py`: provide one session semantic index per xdist worker and group architecture checks.
- Modify `backend/tests/test_repository_callers_static.py`: consume semantic findings rather than `(path, line, member)` tuples.
- Delete `backend/tests/test_repository_surface_manifest.py` after all its live contracts are represented by the focused files.
- Modify `backend/app/repositories/ownership_manifest.py`: replace `path:line` consumer strings with semantic consumer keys.
- Modify `scripts/generate_repository_contract_fixtures.py`: generate semantic consumer/patch keys.
- Modify `backend/tests/fixtures/repository_contract/*.json`: regenerate current contract fixtures without positions.

### Frontend executable and semantic tests

- Create `frontend/vitest.config.ts`: collects only `app/**/*.component.test.tsx` in jsdom.
- Create `frontend/app/test/setup.ts`: installs Testing Library cleanup and jest-dom matchers.
- Create `frontend/app/test/semantic-source.mjs`: parses TS/TSX with the TypeScript compiler API and exposes semantic queries.
- Create `frontend/app/test/static-source-contracts.mjs`: central registry of permitted repository-wide semantic scans.
- Create `frontend/app/test/static-source-policy.test.mjs`: rejects direct production-source reads and position/order assertions outside the registry.
- Create `frontend/app/ask-composer.tsx`: executable Enter/Shift+Enter/running/abort behavior extracted from `page.tsx`.
- Create `frontend/app/ask-composer.component.test.tsx`: user-event coverage for the Ask composer.
- Create `frontend/app/account-menu.tsx`: executable account popover/logout behavior extracted from `page.tsx`.
- Create `frontend/app/account-menu.component.test.tsx`: accessible menu behavior coverage.
- Create `frontend/app/workspace-transitions.ts`: pure ownership, history, reconnect-reset, and restore decisions used by the workspace.
- Create `frontend/app/workspace-transitions.test.mjs`: direct model tests replacing source slices.
- Modify `frontend/app/page.tsx`: consume the extracted components and transition helpers without changing product behavior.
- Modify the 16 existing source-reading `*.test.mjs` files: convert behavior assertions to component/model tests and semantic repository guards to the central scanner.
- Modify `frontend/package.json` and `frontend/package-lock.json`: install and run the component test stack.

### Performance and orchestration

- Modify `backend/tests/test_llm_client.py`: fake the actual `sleep_or_cancel` boundary.
- Modify `backend/tests/test_large_lib_index_required.py`: create the minimum valid index artifact instead of rebuilding the graph/index.
- Modify additional profiled backend tests only when a before/after focused profile proves avoidable setup, real waiting, or isolation-safe duplication.
- Create `scripts/check_backend.sh`: complete backend pytest lane.
- Create `scripts/check_contracts.sh`: Python preflight, smoke, contracts, and deterministic harness lane.
- Create `scripts/check_frontend.sh`: Node, Vitest, TypeScript, and Next build lane.
- Modify `scripts/check.sh`: bounded parallel coordinator with stable logs, timings, cleanup, and exit aggregation.
- Modify `backend/pytest.ini`: pin the measured worker/distribution configuration and register the architecture resource group if needed.

### Documentation

- Modify `README.md`, `README_zh.md`, and `AGENTS.md`: document the durable test policy and complete gate.
- Modify the draft PR description after verification: record migrated contracts, deleted brittle assertions, lane timings, and three complete warm-run totals.

---

### Task 1: Add the line-identity policy and shared Python semantic index

**Files:**
- Create: `backend/tests/architecture/__init__.py`
- Create: `backend/tests/architecture/semantic_source.py`
- Create: `backend/tests/test_semantic_source.py`
- Create: `backend/tests/test_test_architecture_policy.py`
- Modify: `backend/tests/conftest.py`

**Interfaces:**
- Produces: `SemanticKey(path, scope, kind, target)`, `SemanticFinding(key, count, diagnostic_lines)`, `PythonSourceIndex.from_sources()`, and `python_source_index`.
- Consumes: repository root and Python `ast`; no production code.

- [ ] **Step 1: Write failing mutation tests for semantic identity**

```python
from backend.tests.architecture.semantic_source import PythonSourceIndex


def test_line_movement_does_not_change_semantic_identity():
    before = {"app/a.py": "def run(repo):\n    return repo.save()\n"}
    after = {"app/a.py": "\n\n\ndef run(repo):\n    return repo.save()\n"}
    assert PythonSourceIndex.from_sources(before).call_keys() == (
        PythonSourceIndex.from_sources(after).call_keys()
    )


def test_scope_movement_changes_semantic_identity():
    before = {"app/a.py": "def run(repo):\n    return repo.save()\n"}
    after = {"app/a.py": "class Worker:\n    def run(self, repo):\n        return repo.save()\n"}
    assert PythonSourceIndex.from_sources(before).call_keys() != (
        PythonSourceIndex.from_sources(after).call_keys()
    )


def test_duplicate_count_is_semantic_and_lines_are_diagnostics_only():
    index = PythonSourceIndex.from_sources(
        {"app/a.py": "def run(repo):\n    repo.save()\n    repo.save()\n"}
    )
    finding = index.calls(target="repo.save")[0]
    assert finding.count == 2
    assert finding.diagnostic_lines == (2, 3)
```

- [ ] **Step 2: Run the mutation tests and verify the missing module failure**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -n0 backend/tests/test_semantic_source.py -q
```

Expected: collection fails because `backend.tests.architecture.semantic_source`
does not exist.

- [ ] **Step 3: Implement the semantic source model and scope-aware AST visitor**

```python
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


@dataclass(frozen=True, order=True)
class SemanticKey:
    path: str
    scope: str
    kind: str
    target: str


@dataclass(frozen=True)
class SemanticFinding:
    key: SemanticKey
    count: int
    diagnostic_lines: tuple[int, ...]


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scopes: list[str] = ["<module>"]
        self.sites: list[tuple[SemanticKey, int]] = []

    @property
    def scope(self) -> str:
        return ".".join(self.scopes)

    def _visit_scope(self, node: ast.AST, name: str) -> None:
        self.scopes.append(name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node, node.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            self.sites.append(
                (
                    SemanticKey(
                        self.path,
                        self.scope,
                        "import",
                        f"{module}:{alias.name}",
                    ),
                    node.lineno,
                )
            )

    def visit_Call(self, node: ast.Call) -> None:
        target = dotted_name(node.func)
        if target:
            self.sites.append(
                (SemanticKey(self.path, self.scope, "call", target), node.lineno)
            )
        self.generic_visit(node)


def dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


class PythonSourceIndex:
    def __init__(self, findings: tuple[SemanticFinding, ...]) -> None:
        self.findings = findings

    @classmethod
    def from_sources(cls, sources: Mapping[str, str]) -> "PythonSourceIndex":
        grouped: dict[SemanticKey, list[int]] = defaultdict(list)
        for path, source in sorted(sources.items()):
            visitor = _Visitor(path)
            visitor.visit(ast.parse(source, filename=path))
            for key, line in visitor.sites:
                grouped[key].append(line)
        findings = tuple(
            SemanticFinding(key, len(lines), tuple(lines))
            for key, lines in sorted(grouped.items())
        )
        return cls(findings)

    @classmethod
    def from_paths(
        cls, root: Path, paths: Iterable[Path]
    ) -> "PythonSourceIndex":
        return cls.from_sources(
            {
                path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
                for path in paths
            }
        )

    def calls(self, *, target: str | None = None) -> tuple[SemanticFinding, ...]:
        return tuple(
            finding
            for finding in self.findings
            if finding.key.kind == "call"
            and (target is None or finding.key.target == target)
        )

    def call_keys(self) -> frozenset[SemanticKey]:
        return frozenset(finding.key for finding in self.calls())
```

Extend the visitor in the same file with normalized attribute access,
assignment, decorator, patch-target, SQL-literal, and compatibility-export
findings needed by Tasks 2 and 3. Every extension returns `SemanticKey`;
`lineno` is appended only to `diagnostic_lines`.

- [ ] **Step 4: Add the policy guard before converting existing debt**

```python
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED_ROOTS = (
    ROOT / "backend" / "tests",
    ROOT / "scripts",
    ROOT / "backend" / "app" / "repositories",
)
HISTORICAL_ALLOWLIST = re.compile(r"\bTASK\d+_ALLOWED_[A-Z0-9_]+\b")
PATH_LINE_IDENTITY = re.compile(r"(?:backend|frontend|scripts)/[^:\n]+:\d+\b")


def _policy_offenders() -> list[str]:
    return policy_offenders(
        tuple(
            path
            for base in SCANNED_ROOTS
            for path in sorted(base.rglob("*.py"))
        )
    )


def policy_offenders(paths: tuple[Path, ...]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:
            rel = path.name
        if HISTORICAL_ALLOWLIST.search(source):
            offenders.append(f"{rel}: historical TASK allowlist")
        if PATH_LINE_IDENTITY.search(source):
            offenders.append(f"{rel}: path:line identity")
        tree = ast.parse(source, filename=rel)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            dotted = ast.unparse(node)
            if dotted in {
                "pytest.mark.skip",
                "pytest.mark.skipif",
                "pytest.mark.xfail",
            }:
                offenders.append(f"{rel}: pytest.{node.attr.removesuffix('if')}")
    return offenders


def test_policy_detects_line_identity_task_history_and_markers(tmp_path):
    source = tmp_path / "test_bad.py"
    source.write_text(
        "TASK7_ALLOWED_CALLS = {'backend/app/a.py:17'}\n"
        "@pytest.mark.skip\n"
        "def test_bad(): pass\n",
        encoding="utf-8",
    )
    assert policy_offenders((source,)) == [
        "test_bad.py: historical TASK allowlist",
        "test_bad.py: path:line identity",
        "test_bad.py: pytest.skip",
    ]


def test_policy_allows_line_numbers_as_diagnostic_metadata(tmp_path):
    source = tmp_path / "test_good.py"
    source.write_text(
        "finding = SemanticFinding(key, 1, diagnostic_lines=(node.lineno,))\n",
        encoding="utf-8",
    )
    assert policy_offenders((source,)) == []
```

Extend this scanner with the AST parent checks used by the final policy:
`node.lineno` is permitted only when it initializes `diagnostic_lines` or is
rendered in a failure message, and is rejected when it participates in a
tuple/set/dict identity or equality assertion. The repository-wide assertion
is added only after Tasks 2 and 3 remove the known debt, so no committed
intermediate state contains skip or xfail.

- [ ] **Step 5: Run the focused tests**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -n0 \
  backend/tests/test_semantic_source.py \
  backend/tests/test_test_architecture_policy.py -q
```

Expected: all semantic and policy-scanner mutation tests pass with zero skip or
xfail.

- [ ] **Step 6: Add one session index fixture per worker**

```python
from pathlib import Path

from backend.tests.architecture.semantic_source import PythonSourceIndex


@pytest.fixture(scope="session")
def python_source_index() -> PythonSourceIndex:
    root = Path(__file__).resolve().parents[2]
    paths = tuple((root / "backend" / "app").rglob("*.py")) + tuple(
        (root / "backend" / "tests").rglob("*.py")
    )
    return PythonSourceIndex.from_paths(root, paths)
```

Do not make this fixture autouse. Only semantic architecture tests request it,
so ordinary behavior tests do not pay its parsing cost.

- [ ] **Step 7: Commit the semantic foundation**

```bash
git add backend/tests/architecture backend/tests/conftest.py \
  backend/tests/test_semantic_source.py \
  backend/tests/test_test_architecture_policy.py
git commit -m "test: add semantic source contract foundation"
```

---

### Task 2: Replace historical repository surface layers with one current contract

**Files:**
- Create: `backend/tests/architecture/repository_contract.py`
- Create: `backend/tests/test_repository_dependency_contract.py`
- Create: `backend/tests/test_repository_surface_contract.py`
- Create: `backend/tests/test_repository_patch_contract.py`
- Modify: `backend/app/repositories/ownership_manifest.py`
- Modify: `scripts/generate_repository_contract_fixtures.py`
- Modify: `backend/tests/fixtures/repository_contract/facade_surface.json`
- Modify: `backend/tests/test_repository_surface_manifest.py`

**Interfaces:**
- Consumes: `PythonSourceIndex` and current production repository composition.
- Produces: `ConsumerSite(path, scope, kind, target)` and
  `RepositoryContract(imports, consumers, patches, members, delegates)` with
  semantic keys only.

- [ ] **Step 1: Write a failing current-contract schema test**

```python
from backend.tests.architecture.repository_contract import REPOSITORY_CONTRACT


def test_repository_contract_contains_no_task_history_or_line_identity():
    rendered = repr(REPOSITORY_CONTRACT)
    assert "TASK" not in rendered
    assert not re.search(r"(?:backend|scripts)/[^:\s]+:\d+\b", rendered)
    assert all(site.scope for site in REPOSITORY_CONTRACT.consumers)
    assert all(site.kind in {
        "import", "call", "attribute", "patch", "compatibility"
    } for site in (
        *REPOSITORY_CONTRACT.consumers,
        *REPOSITORY_CONTRACT.patches,
    ))
```

- [ ] **Step 2: Run the schema test and verify it fails**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -n0 \
  backend/tests/test_repository_surface_contract.py::test_repository_contract_contains_no_task_history_or_line_identity -q
```

Expected: import fails because `repository_contract.py` does not exist.

- [ ] **Step 3: Give production ownership metadata a semantic consumer type**

```python
from dataclasses import dataclass
from typing import Literal


ConsumerKind = Literal[
    "import",
    "call",
    "attribute",
    "patch",
    "compatibility",
]


@dataclass(frozen=True, order=True)
class ConsumerSite:
    path: str
    scope: str
    kind: ConsumerKind
    target: str


@dataclass(frozen=True)
class SurfaceMember:
    name: str
    owner: str
    kind: SurfaceKind
    consumers: tuple[ConsumerSite, ...]
    patches: tuple[ConsumerSite, ...] = ()
```

Compatibility exports use:

```python
ConsumerSite(
    path="app.services.sqlite_repository",
    scope="<module>",
    kind="compatibility",
    target="_now",
)
```

No compatibility or consumer entry contains a source position.

- [ ] **Step 4: Add an isolated semantic surface rebaseline mode**

In `scripts/generate_repository_contract_fixtures.py`, set the surface source
commit to the merged PR #305 baseline:

```python
SURFACE_SOURCE_COMMIT = "842de5d22dbdc423d6572fe0650911dafa910c35"
```

Add:

```python
def consumer_site(
    path: str,
    scope: str,
    kind: ConsumerKind,
    target: str,
    *,
    diagnostic_line: int | None = None,
) -> ConsumerSite:
    del diagnostic_line
    return ConsumerSite(path=path, scope=scope, kind=kind, target=target)


def _consumer_payload(site: ConsumerSite) -> dict[str, str]:
    return {
        "path": site.path,
        "scope": site.scope,
        "kind": site.kind,
        "target": site.target,
    }


def _render_site(site: ConsumerSite) -> str:
    return (
        "ConsumerSite("
        f"path={site.path!r}, scope={site.scope!r}, "
        f"kind={site.kind!r}, target={site.target!r})"
    )


def _render_sites(items: list[dict[str, str]]) -> str:
    return "(" + "".join(
        f"{_render_site(ConsumerSite(**item))},"
        for item in items
    ) + ")"


def write_ownership_manifest(
    path: Path,
    surface: dict[str, dict[str, object]],
) -> None:
    source = path.read_text(encoding="utf-8")
    start = source.index("SURFACE_MEMBERS = (")
    end = source.index("\ndef _unique_nonempty_owners", start)
    lines = ["SURFACE_MEMBERS = ("]
    for name, item in sorted(surface.items()):
        consumers = _render_sites(item["consumers"])
        patches = _render_sites(item["patch_targets"])
        lines.append(
            "    SurfaceMember("
            f"name={name!r}, owner={item['owner']!r}, kind={item['kind']!r}, "
            f"consumers={consumers}, patches={patches}),"
        )
    lines.append(")")
    path.write_text(
        source[:start] + "\n".join(lines) + source[end:],
        encoding="utf-8",
    )
```

The AST collector tracks class/function scope with the same visitor rules as
`semantic_source.py`. Add a `--rebaseline-surface` CLI flag whose branch writes
only:

```python
_write_json(
    contract_dir / "facade_surface.json",
    collect_facade_surface(),
)
write_ownership_manifest(
    REPO_ROOT / "backend" / "app" / "repositories" / "ownership_manifest.py",
    collect_facade_surface(),
)
```

It must not call the v9 database/storage fixture builder. Before writing,
verify all `backend/app/**/*.py` files except the generated
`ownership_manifest.py` match `SURFACE_SOURCE_COMMIT`.

- [ ] **Step 5: Rebaseline only the current semantic surface**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  scripts/generate_repository_contract_fixtures.py --rebaseline-surface
```

Expected:

- `facade_surface.json` consumers and patch targets are structured semantic
  objects;
- `ownership_manifest.py` contains `ConsumerSite` values;
- no historical task identifier or path-line identity exists in either file;
- `backend/tests/fixtures/repository_v9/**` is byte-for-byte unchanged.

- [ ] **Step 6: Define one declarative current-state contract**

```python
from dataclasses import dataclass

from app.repositories.ownership_manifest import (
    OWNER_BY_MEMBER,
    SURFACE_MEMBERS,
)
from backend.tests.architecture.semantic_source import SemanticKey


@dataclass(frozen=True)
class RepositoryContract:
    imports: frozenset[SemanticKey]
    consumers: frozenset[SemanticKey]
    patches: frozenset[SemanticKey]
    members: frozenset[str]
    delegates: tuple[tuple[str, str], ...]


def _key(site) -> SemanticKey:
    return SemanticKey(site.path, site.scope, site.kind, site.target)


all_consumers = frozenset(
    _key(site)
    for member in SURFACE_MEMBERS
    for site in member.consumers
)
all_patches = frozenset(
    _key(site)
    for member in SURFACE_MEMBERS
    for site in member.patches
)

REPOSITORY_CONTRACT = RepositoryContract(
    imports=frozenset(site for site in all_consumers if site.kind == "import"),
    consumers=all_consumers,
    patches=all_patches,
    members=frozenset(member.name for member in SURFACE_MEMBERS),
    delegates=tuple(sorted(OWNER_BY_MEMBER.items())),
)
```

The generated ownership manifest is the committed current-state snapshot.
Tests compare it with the live semantic index; the generator is never invoked
automatically by the test, so a newly introduced consumer or patch fails until
it is reviewed and explicitly rebaselined.

- [ ] **Step 7: Split the large test by responsibility**

`test_repository_dependency_contract.py` contains forbidden-import and layer
direction tests. `test_repository_surface_contract.py` contains member,
compatibility export, consumer, signature, and delegate tests.
`test_repository_patch_contract.py` contains patch-target and private-seam
tests.

All three compare `SemanticKey` sets and format failures with:

```python
def format_findings(findings):
    return [
        f"{item.key.path}::{item.key.scope} {item.key.kind} "
        f"{item.key.target} (lines {item.diagnostic_lines})"
        for item in findings
    ]
```

The formatted lines help navigation but are never compared with the manifest.

- [ ] **Step 8: Prove neutral line movement stays green and new debt fails**

Add synthetic mutation tests:

```python
def test_repository_import_contract_ignores_blank_line_insertion():
    first = PythonSourceIndex.from_sources(
        {"backend/app/services/a.py": "from app.repositories.ports import AskPort\n"}
    )
    moved = PythonSourceIndex.from_sources(
        {"backend/app/services/a.py": "\n\nfrom app.repositories.ports import AskPort\n"}
    )
    assert first.import_keys() == moved.import_keys()


def test_repository_import_contract_detects_forbidden_facade_import():
    index = PythonSourceIndex.from_sources(
        {"backend/app/services/a.py": "from app.services.sqlite_repository import SQLiteRepository\n"}
    )
    assert forbidden_repository_imports(index)
```

- [ ] **Step 9: Run the new focused architecture suite**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -n0 \
  backend/tests/test_semantic_source.py \
  backend/tests/test_repository_dependency_contract.py \
  backend/tests/test_repository_surface_contract.py \
  backend/tests/test_repository_patch_contract.py -q
```

Expected: all tests pass.

- [ ] **Step 10: Delete the superseded large surface test**

Delete `backend/tests/test_repository_surface_manifest.py` only after mapping
every test function to one of the three focused files. Record the mapping in
the commit body:

```text
dependency/import tests -> test_repository_dependency_contract.py
surface/consumer/delegate tests -> test_repository_surface_contract.py
patch/private seam tests -> test_repository_patch_contract.py
scanner mutation tests -> test_semantic_source.py
```

- [ ] **Step 11: Run all repository architecture tests and compare collection**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -n0 backend/tests \
  -k 'repository or architecture or semantic_source or test_architecture_policy' \
  --durations=30 -q
```

Expected: zero failures; all former surface responsibilities have at least one
replacement assertion; no test refers to `TASKN_ALLOWED_*`.

- [ ] **Step 12: Commit the current repository contract**

```bash
git add backend/tests/architecture/repository_contract.py \
  backend/app/repositories/ownership_manifest.py \
  scripts/generate_repository_contract_fixtures.py \
  backend/tests/fixtures/repository_contract/facade_surface.json \
  backend/tests/test_repository_dependency_contract.py \
  backend/tests/test_repository_surface_contract.py \
  backend/tests/test_repository_patch_contract.py \
  backend/tests/test_repository_surface_manifest.py
git commit -m "test: collapse repository history into semantic contracts"
```

---

### Task 3: Convert generated fixtures, ownership data, and remaining backend scans

**Files:**
- Modify: `backend/app/repositories/ownership_manifest.py`
- Modify: `scripts/generate_repository_contract_fixtures.py`
- Modify: `backend/tests/fixtures/repository_contract/api_contract.json`
- Modify: `backend/tests/fixtures/repository_contract/ask_responses.json`
- Modify: `backend/tests/fixtures/repository_contract/error_policies.json`
- Modify: `backend/tests/fixtures/repository_contract/facade_surface.json`
- Modify: `backend/tests/fixtures/repository_contract/mutation_phases.json`
- Modify: `backend/tests/fixtures/repository_contract/transaction_phases.json`
- Modify: `backend/tests/test_repository_callers_static.py`
- Modify: `backend/tests/test_repository_monkeypatch_owners.py`
- Modify: `backend/tests/test_repository_protocol_coverage.py`
- Modify: `backend/tests/test_architecture_documentation.py`
- Modify: `backend/tests/test_test_architecture_policy.py`

**Interfaces:**
- Consumes: semantic source index and current repository contract.
- Produces: `ConsumerSite(path, scope, kind, target)` values in every remaining
  generated fixture and backend static contract.

- [ ] **Step 1: Write failing generator stability tests**

```python
def test_consumer_site_ignores_diagnostic_line():
    assert consumer_site(
        "backend/app/api/routes.py",
        "<module>.create_report",
        "call",
        "repo.create_report",
        diagnostic_line=100,
    ) == consumer_site(
        "backend/app/api/routes.py",
        "<module>.create_report",
        "call",
        "repo.create_report",
        diagnostic_line=900,
    )


def test_generated_contract_contains_no_path_line_identity(generated_contract):
    assert not re.search(
        r"(?:backend|frontend|scripts)/[^:\s\"]+:\d+\b",
        json.dumps(generated_contract, ensure_ascii=False),
    )
```

- [ ] **Step 2: Verify the current generator fails the new contract**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -n0 backend/tests/test_repository_facade_contract.py \
  -k 'consumer_site or path_line' -q
```

Expected: fail before Task 2's semantic rebaseline; after Task 2 it passes and
protects the generator interface used below.

- [ ] **Step 3: Apply semantic consumer construction to every collector**

```python
def consumer_site(
    path: str,
    scope: str,
    kind: ConsumerKind,
    target: str,
    *,
    diagnostic_line: int | None = None,
) -> ConsumerSite:
    del diagnostic_line
    return ConsumerSite(
        path=path,
        scope=scope,
        kind=kind,
        target=target,
    )
```

Use the shared scope rules from `semantic_source.py` when the generator walks
AST. Replace every `f"{relative}:{node.lineno}"` and every tuple containing
`call.lineno` with this value. Keep the line only in an error formatter printed
when generation detects an unsupported construct. This completes collectors
not exercised by the facade-surface-only rebaseline in Task 2.

- [ ] **Step 4: Regenerate the repository contract fixtures**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  scripts/generate_repository_contract_fixtures.py --rebaseline-surface
```

Expected: `facade_surface.json` and the generated ownership manifest are stable
on the second run; the frozen `backend/tests/fixtures/repository_v9/**` tree
does not change. Other living fixtures change only if their existing payload
actually contains a converted consumer or patch site.

- [ ] **Step 5: Update ownership metadata to semantic consumers**

Use the same four-part value in `SurfaceMember.consumers`, for example:

```python
SurfaceMember(
    "_needs_index",
    "ScaleArtifactRuntime",
    "private_wrapper",
    (
        ConsumerSite(
            "backend/app/services/sqlite_repository.py",
            "<module>.SQLiteRepository._needs_index",
            "call",
            "runtime.ask_component._needs_index",
        ),
        ConsumerSite(
            "backend/tests/test_large_lib_index_required.py",
            "<module>.test_needs_index_truth_table",
            "call",
            "repo._needs_index",
        ),
        ConsumerSite(
            "backend/tests/test_large_lib_index_required.py",
            "<module>.test_needs_index_false_when_indexed",
            "call",
            "repo._needs_index",
        ),
    ),
)
```

Generate this file through the repository generator where it is generated
today; do not hand-edit hundreds of consumer entries independently.

- [ ] **Step 6: Convert remaining backend static callers to semantic findings**

Replace collections such as:

```python
hits.append((rel, node.lineno, attr))
```

with:

```python
hits.append(
    SemanticKey(
        path=rel,
        scope=current_scope,
        kind="attribute",
        target=attr,
    )
)
```

When a failure needs navigation, look up the matching
`SemanticFinding.diagnostic_lines`. SQL read-only and security checks may still
inspect literal values, but their expected identity is
`(path, scope, kind, target)`.

- [ ] **Step 7: Enable the repository-wide policy guard**

Make the policy test ordinary and add these assertions:

```python
def test_repository_tests_have_no_line_identity_or_historical_allowlists():
    assert _policy_offenders() == []


def test_complete_suite_has_no_skip_or_xfail_markers():
    assert marker_offenders() == []
```

The policy scanner ignores `diagnostic_lines`, `node.lineno` passed into
diagnostic constructors, and failure strings. It rejects line values in
hashable manifest data, generated JSON, allowlist comparisons, and keys.

- [ ] **Step 8: Run generator verification and all backend architecture tests**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -n0 backend/tests \
  -k 'repository or architecture or semantic_source or test_architecture_policy' \
  --durations=30 -q

git diff --exit-code backend/tests/fixtures/repository_v9
```

Expected: tests pass, zero skips/xfails, and the frozen v9 fixture has no diff.

- [ ] **Step 9: Commit the semantic fixtures and remaining backend scans**

```bash
git add backend/app/repositories/ownership_manifest.py \
  scripts/generate_repository_contract_fixtures.py \
  backend/tests/fixtures/repository_contract \
  backend/tests/test_repository_callers_static.py \
  backend/tests/test_repository_monkeypatch_owners.py \
  backend/tests/test_repository_protocol_coverage.py \
  backend/tests/test_architecture_documentation.py \
  backend/tests/test_test_architecture_policy.py
git commit -m "test: remove source positions from repository contracts"
```

---

### Task 4: Add Vitest and a semantic TypeScript source-test boundary

**Files:**
- Create: `frontend/vitest.config.ts`
- Create: `frontend/app/test/setup.ts`
- Create: `frontend/app/test/semantic-source.mjs`
- Create: `frontend/app/test/static-source-contracts.mjs`
- Create: `frontend/app/test/static-source-policy.test.mjs`
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/app/test-runner-config.test.mjs`

**Interfaces:**
- Produces: `parseModule(relativePath)`, `findFunction(module, name)`, `callsIn(node)`, `importsFrom(module, path)`, `jsxElements(module, name)`, and `STATIC_SOURCE_CONTRACTS`.
- Consumes: TypeScript compiler API already present through `typescript`.

- [ ] **Step 1: Add failing test-runner contract assertions**

```javascript
test("frontend test command runs pure and component suites", () => {
  assert.equal(pkg.scripts["test:node"], "node --test $(find app -name '*.test.mjs' -type f -print)");
  assert.equal(pkg.scripts["test:component"], "vitest run");
  assert.equal(pkg.scripts.test, "npm run test:node && npm run test:component");
});

test("component tests have an exclusive suffix", () => {
  assert.equal(vitestConfig.test.include[0], "app/**/*.component.test.tsx");
  assert.equal(vitestConfig.test.environment, "jsdom");
});
```

- [ ] **Step 2: Run and confirm the runner contract fails**

Run:

```bash
cd frontend
node --test app/test-runner-config.test.mjs
```

Expected: fail because the component script and Vitest config do not exist.

- [ ] **Step 3: Add the component test dependencies**

Modify `package.json`:

```json
{
  "scripts": {
    "test": "npm run test:node && npm run test:component",
    "test:node": "node --test $(find app -name '*.test.mjs' -type f -print)",
    "test:component": "vitest run"
  },
  "devDependencies": {
    "@testing-library/jest-dom": "^6.6.3",
    "@testing-library/react": "^16.1.0",
    "@testing-library/user-event": "^14.5.2",
    "jsdom": "^26.0.0",
    "vitest": "^3.2.0"
  }
}
```

Run `npm install` inside the worktree after replacing the temporary
`frontend/node_modules` symlink with a worktree-local install. Do not mutate
the main worktree's dependency directory.

- [ ] **Step 4: Configure Vitest**

```typescript
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["app/**/*.component.test.tsx"],
    setupFiles: ["./app/test/setup.ts"],
    clearMocks: true,
    restoreMocks: true,
  },
});
```

`frontend/app/test/setup.ts`:

```typescript
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(cleanup);
```

- [ ] **Step 5: Write failing semantic-source mutation tests**

```javascript
test("semantic TS queries ignore line movement", () => {
  const first = parseText("function save() { api('/save'); }", "a.tsx");
  const moved = parseText("\n\nfunction save() { api('/save'); }", "a.tsx");
  assert.deepEqual(callsIn(findFunction(first, "save")), callsIn(findFunction(moved, "save")));
});

test("semantic TS queries retain scope", () => {
  const first = parseText("function save() { api('/save'); }", "a.tsx");
  const moved = parseText("function other() { api('/save'); }", "a.tsx");
  assert.notDeepEqual(scopedCalls(first), scopedCalls(moved));
});
```

- [ ] **Step 6: Implement TypeScript compiler semantic queries**

```javascript
import ts from "typescript";
import { readFile } from "node:fs/promises";

export function parseText(source, fileName) {
  return ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
}

export async function parseModule(relativePath) {
  const url = new URL(`../${relativePath}`, import.meta.url);
  return parseText(await readFile(url, "utf8"), relativePath);
}

export function findFunction(sourceFile, name) {
  let match;
  function visit(node) {
    if (
      (ts.isFunctionDeclaration(node) ||
        ts.isMethodDeclaration(node) ||
        ts.isVariableDeclaration(node)) &&
      node.name?.getText(sourceFile) === name
    ) {
      match = node;
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  if (!match) throw new Error(`function not found: ${name}`);
  return match;
}

export function callsIn(node) {
  const calls = [];
  function visit(child) {
    if (ts.isCallExpression(child)) calls.push(child.expression.getText());
    ts.forEachChild(child, visit);
  }
  visit(node);
  return calls.sort();
}
```

Add equivalent import, property-access, string-literal, JSX role/name, and
scope queries as the existing semantic guards need them. Never return
`getStart()`, `getEnd()`, or line numbers as comparable data.

- [ ] **Step 7: Centralize approved semantic scans and prohibit direct reads**

```javascript
export const STATIC_SOURCE_CONTRACTS = Object.freeze({
  architectureBoundaries: {
    category: "architecture",
    reason: "prevents component/model duplication across module boundaries",
    roots: ["page.tsx", "answer-panel.tsx", "kg-type-mark.tsx"],
  },
  askModeVocabulary: {
    category: "protocol-vocabulary",
    reason: "enforces a repository-wide single source of display names",
    roots: ["."],
  },
  trustedErrors: {
    category: "security",
    reason: "prevents raw diagnostic text from reaching user-visible sinks",
    roots: ["."],
  },
  rawEnumFallback: {
    category: "user-visible-vocabulary",
    reason: "prevents unlabelled backend enum values from rendering",
    roots: ["."],
  },
});
```

The policy test scans all `*.test.mjs` and allows `node:fs` imports only in
`frontend/app/test/semantic-source.mjs` and the policy test itself. It also
rejects `.indexOf(`, `.slice(`, `.substring(`, `.getStart(`, `.getEnd(`, and
line-number identity patterns in source-contract tests.

- [ ] **Step 8: Run both runners**

Run:

```bash
cd frontend
npm run test:node
npm run test:component
```

Expected: Node runner passes except for the policy test listing the 16 existing
direct-source tests; Vitest exits cleanly after a temporary one-line smoke
component test proves collection. Remove the smoke test before committing.

- [ ] **Step 9: Commit the frontend test foundation**

```bash
git add frontend/package.json frontend/package-lock.json \
  frontend/vitest.config.ts frontend/app/test \
  frontend/app/test-runner-config.test.mjs
git commit -m "test: add frontend component and semantic test foundations"
```

---

### Task 5: Migrate frontend behavior tests away from source layout

**Files:**
- Create: `frontend/app/ask-composer.tsx`
- Create: `frontend/app/ask-composer.component.test.tsx`
- Create: `frontend/app/account-menu.tsx`
- Create: `frontend/app/account-menu.component.test.tsx`
- Create: `frontend/app/workspace-transitions.ts`
- Create: `frontend/app/workspace-transitions.test.mjs`
- Modify: `frontend/app/page.tsx`
- Modify: all 16 source-reading test files listed by:
  `rg -l 'readFileSync|readFile\\(' frontend/app --glob '*test.mjs'`

**Interfaces:**
- Produces: `AskComposer`, `AccountMenu`, `ownsWorkspaceRun`, `historyModeForTransition`, `restoreLatestConversation`, and semantic source guards.
- Consumes: existing workspace callbacks and view state; no test-only production APIs.

- [ ] **Step 1: Write failing Ask composer behavior tests**

```tsx
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { AskComposer } from "./ask-composer";

test("Enter submits and Shift+Enter inserts a newline", async () => {
  const user = userEvent.setup();
  const onSubmit = vi.fn();
  render(
    <AskComposer
      value="question"
      onChange={() => undefined}
      onSubmit={onSubmit}
      onAbort={() => undefined}
      running={false}
    />,
  );
  const input = screen.getByRole("textbox", { name: "提问" });
  await user.type(input, "{Shift>}{Enter}{/Shift}");
  expect(onSubmit).not.toHaveBeenCalled();
  await user.type(input, "{Enter}");
  expect(onSubmit).toHaveBeenCalledOnce();
});

test("running state locks input and turns send into interrupt", async () => {
  const user = userEvent.setup();
  const onAbort = vi.fn();
  render(
    <AskComposer
      value="question"
      onChange={() => undefined}
      onSubmit={() => undefined}
      onAbort={onAbort}
      running
    />,
  );
  expect(screen.getByRole("textbox", { name: "提问" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "中断生成" }));
  expect(onAbort).toHaveBeenCalledOnce();
});
```

- [ ] **Step 2: Implement and wire `AskComposer`**

```tsx
type AskComposerProps = {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onAbort: () => void;
  running: boolean;
};

export function AskComposer(props: AskComposerProps) {
  function onKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!props.running && props.value.trim()) props.onSubmit();
    }
  }

  return (
    <div className="chat-composer">
      <textarea
        aria-label="提问"
        className="chat-input"
        value={props.value}
        disabled={props.running}
        onChange={(event) => props.onChange(event.target.value)}
        onKeyDown={onKeyDown}
      />
      <button
        aria-label={props.running ? "中断生成" : "发送"}
        className={`send-button${props.running ? " stop" : ""}`}
        disabled={!props.running && !props.value.trim()}
        onClick={props.running ? props.onAbort : props.onSubmit}
      >
        {props.running ? <Square size={16} /> : <Send size={16} />}
      </button>
    </div>
  );
}
```

Move the existing JSX and callbacks intact from `page.tsx`; keep classes and
icons so runtime styling does not change.

- [ ] **Step 3: Write and implement accessible account-menu tests**

Test closed-by-default, click-to-open, `aria-expanded`, menu role, outside/Esc
close, and logout callback. Implement:

```tsx
type AccountMenuProps = {
  username: string;
  onLogout: () => void | Promise<void>;
};

export function AccountMenu({ username, onLogout }: AccountMenuProps) {
  // existing ref/outside-click lifecycle moved intact from page.tsx
  return (
    <div className="user-menu" ref={menuRef}>
      <button
        className="user-menu-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        {username}
      </button>
      {open && (
        <div className="user-menu-popover" role="menu">
          <button className="user-logout" role="menuitem" onClick={onLogout}>
            退出登录
          </button>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Extract and directly test workspace transition decisions**

```typescript
export function ownsWorkspaceRun(
  expectedRun: number,
  currentRun: number,
  expectedWorkspace: number,
  currentWorkspace: number,
): boolean {
  return expectedRun === currentRun && expectedWorkspace === currentWorkspace;
}

export function historyModeForTransition(
  currentNotebookId: string | null,
  nextNotebookId: string,
): "push" | "replace" {
  return currentNotebookId === nextNotebookId ? "replace" : "push";
}

export async function restoreLatestConversation<T>(
  sessions: readonly { id: string }[],
  apply: (id: string) => Promise<T>,
): Promise<T | null> {
  if (!sessions[0]) return null;
  try {
    return await apply(sessions[0].id);
  } catch {
    return null;
  }
}
```

Use these exact helpers in `page.tsx`. Unit tests cover same/different epochs,
same/different notebook history, empty sessions, success, and graceful restore
failure.

- [ ] **Step 5: Classify and migrate every direct-source test**

Use this disposition:

| Existing file | Replacement |
| --- | --- |
| `workspace-layout.test.mjs` | Ask/account component tests, transition model tests, semantic architecture checks; delete CSS geometry pins |
| `answer-memory.test.mjs` | render existing Answer/Memory components where user interaction is asserted; semantic scope query for abort-before-logout ownership |
| `agent-token-model.test.mjs` | retain pure model assertions; render Memory panel component for action availability |
| `memory-navigation.test.mjs` | retain hash/model tests; use transition model and component roles for navigation |
| `memory-promotion.test.mjs` | pure eligibility model plus component action visibility |
| `kg-sidebar-layout.test.mjs` | component roles/cards/overlay state; delete wrapping/stacking CSS text pins |
| `notebook-bases.test.mjs` | retain pure models; semantic query only for the repository-wide mutually-exclusive hint contract |
| `knowhow-cell-editor.test.mjs` | retain pure logic; migrate upload/leave/button wiring assertions to focused component tests |
| `architecture-boundaries.test.mjs` | central semantic architecture scan |
| `ask-modes.test.mjs` | central repository-wide vocabulary scan |
| `errors-guard.test.mjs` | central security scan |
| `kg-object-vocabulary.test.mjs` | pure vocabulary plus central single-source scan |
| `notebook-default-name.test.mjs` | pure/cross-stack contract via central semantic literal scan |
| `raw-enum-fallback.test.mjs` | central semantic security/vocabulary scan |
| `vocabulary.test.mjs` | pure/cross-stack contract via central semantic scan |
| `test-runner-config.test.mjs` | direct package/config contract only; registered as test-entry infrastructure |

CSS assertions for exact column sizes, `max-width`, property order, source
action widths, and z-index order are removed because jsdom cannot validate
them. The preserved replacement asserts accessible structure, action
availability, overlay/modal state, and semantic component boundaries.

- [ ] **Step 6: Make the source-policy test green**

Run:

```bash
cd frontend
node --test app/test/static-source-policy.test.mjs
```

Expected: no direct production-source read, source slice/order assertion, or
line identity remains outside `semantic-source.mjs`.

- [ ] **Step 7: Run all frontend verification**

Run:

```bash
cd frontend
npm run test
npm run lint
npm run build
```

Expected: Node and Vitest suites pass, TypeScript passes, production build
passes, and component tests contain no real timers or network calls.

- [ ] **Step 8: Commit the frontend behavior migration**

```bash
git add frontend/app frontend/package.json frontend/package-lock.json \
  frontend/vitest.config.ts
git commit -m "test: replace frontend source layout assertions with behavior"
```

---

### Task 6: Remove avoidable backend waits and expensive fixture construction

**Files:**
- Modify: `backend/tests/test_llm_client.py`
- Modify: `backend/tests/test_large_lib_index_required.py`
- Modify: only additional files named by a fresh `--durations=40` profile

**Interfaces:**
- Consumes: existing `sleep_or_cancel` boundary and scale-index artifact probe.
- Produces: deterministic retry tests and a minimum indexed-notebook fixture.

- [ ] **Step 1: Prove retry tests currently call the real wait boundary**

Add:

```python
def test_connection_retry_uses_injected_wait_boundary(monkeypatch):
    waits = []
    monkeypatch.setattr(
        llm_mod,
        "sleep_or_cancel",
        lambda seconds, cancel_event: waits.append((seconds, cancel_event)),
    )
    err = APIConnectionError(request=httpx.Request("POST", "https://x"))
    create = _FakeCreate([err, _Resp()])
    client = _make(monkeypatch, create)
    assert client.chat_json([{"role": "user", "content": "hi"}], "{}") == '{"ok":1}'
    assert len(waits) == 1
    assert 1 <= waits[0][0] <= 2
```

- [ ] **Step 2: Replace incorrect time mocks**

Replace every:

```python
monkeypatch.setattr(llm_mod.time, "sleep", lambda *_a, **_k: None)
```

with:

```python
monkeypatch.setattr(
    llm_mod,
    "sleep_or_cancel",
    lambda _seconds, _cancel_event: None,
)
```

Run:

```bash
PYTHONPATH=backend \
  /usr/bin/time -p \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -n0 backend/tests/test_llm_client.py -q
```

Expected: all tests pass and the file completes without the former
approximately five-second retry waits.

- [ ] **Step 3: Write a minimum indexed-artifact fixture contract**

```python
def test_indexed_fixture_is_detected_without_rebuild(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="indexed"))
    _write_minimum_valid_scale_manifest(repo, notebook.id)
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    assert repo._needs_index(notebook.id) is False
```

The helper must use the production scale-artifact path and exact minimum
manifest schema read by `_needs_index`; it may not monkeypatch `_needs_index`
or the probe being tested.

- [ ] **Step 4: Replace the full rebuild/index fixture**

Remove:

```python
repo.rebuild_unified_kg(nb.id)
repo.build_scale_index(nb.id)
```

from `_index_nb`. Write the minimal on-disk artifact state through the same
manifest writer/helper the production builder uses. Then run:

```bash
PYTHONPATH=backend \
  /usr/bin/time -p \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -n0 \
  backend/tests/test_large_lib_index_required.py::test_needs_index_false_when_indexed \
  --durations=5 -q
```

Expected: pass and complete far below the `9.71s` baseline.

- [ ] **Step 5: Profile the complete backend suite with default load**

Run:

```bash
PYTHONPATH=backend \
  /usr/bin/time -p \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider backend/tests --durations=40
```

Expected: 3,949 baseline tests plus newly added tests all pass. Record the new
top 40. Do not use `--dist=worksteal`; the measured baseline worsened to
`129.25s`.

- [ ] **Step 6: Fix only newly proven avoidable hotspots**

For each profiled candidate, require one of:

- a real sleep replaced by an existing injectable wait boundary;
- a whole-product build used only to manufacture a probe fixture replaced by
  the minimum valid production-shaped artifact;
- repeated AST/source parsing redirected to the session index;
- immutable setup lifted to module/session scope with a test proving per-test
  mutable state remains isolated.

Keep genuine product performance tests such as large ANN/merge budgets. After
each change, run the focused file serially and under the selected xdist mode.

- [ ] **Step 7: Commit deterministic backend performance fixes**

```bash
git add backend/tests/test_llm_client.py \
  backend/tests/test_large_lib_index_required.py \
  backend/tests
git commit -m "test: remove avoidable backend verification latency"
```

---

### Task 7: Run the complete gate in bounded, timed lanes

**Files:**
- Create: `scripts/check_backend.sh`
- Create: `scripts/check_contracts.sh`
- Create: `scripts/check_frontend.sh`
- Modify: `scripts/check.sh`
- Modify: `backend/pytest.ini`
- Modify: `backend/tests/conftest.py`
- Test: `backend/tests/test_test_architecture_policy.py`
- Test: `frontend/app/test-runner-config.test.mjs`

**Interfaces:**
- Produces: three executable lane scripts with `CHECK_TIMING_FILE` output and one aggregate coordinator.
- Consumes: `ROOT_DIR`, `PYTHON_BIN`, offline environment, and worktree-local frontend dependencies.

- [ ] **Step 1: Write failing verification-entry contract tests**

Assert `scripts/check.sh` references all three lane scripts and that the lane
scripts contain these exact responsibilities:

```python
REQUIRED_LAYERS = {
    "scripts/check_backend.sh": ("backend/tests",),
    "scripts/check_contracts.sh": (
        "smoke_backend.py",
        "smoke_memory_mcp.py",
        "check_ask_modes_contract.py",
        "check_object_type_labels_contract.py",
        "check_ui_vocabulary.py",
        "fangan/testcases/harness/tests",
    ),
    "scripts/check_frontend.sh": (
        "npm run test",
        "npm run lint",
        "npm run build",
    ),
}
```

The test also asserts no lane uses `-m "not slow"`, `--ignore`, `skip`, or
`xfail`.

- [ ] **Step 2: Move commands into lane scripts without changing coverage**

Each lane begins:

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
START_SECONDS=$SECONDS
trap 'printf "%s=%s\n" "${CHECK_LANE_NAME}" "$((SECONDS - START_SECONDS))" > "${CHECK_TIMING_FILE}"' EXIT
```

`check_backend.sh` runs the complete backend pytest suite.
`check_contracts.sh` runs Python compile/dependency preflight, both smoke
scripts, all three contract scripts, and the complete deterministic harness.
`check_frontend.sh` checks `node_modules`, then runs `npm run test`, `npm run
lint`, and `npm run build`.

- [ ] **Step 3: Implement the bounded coordinator**

`scripts/check.sh` keeps the existing offline environment exports, then:

```bash
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/silicon-check.XXXXXX")"
declare -a PIDS=()
declare -a LANES=(contracts backend frontend)
cleanup() {
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

for lane in "${LANES[@]}"; do
  CHECK_LANE_NAME="$lane" \
  CHECK_TIMING_FILE="$TMP_DIR/$lane.time" \
  ROOT_DIR="$ROOT_DIR" \
  PYTHON_BIN="$PYTHON_BIN" \
    "$ROOT_DIR/scripts/check_${lane}.sh" \
    >"$TMP_DIR/$lane.log" 2>&1 &
  PIDS+=("$!")
done

status=0
for index in "${!LANES[@]}"; do
  if ! wait "${PIDS[$index]}"; then
    status=1
  fi
done
for lane in "${LANES[@]}"; do
  printf "\n===== %s =====\n" "$lane"
  cat "$TMP_DIR/$lane.log"
  cat "$TMP_DIR/$lane.time"
done
exit "$status"
```

Use one cleanup owner. Do not delete logs before printing them. If a lane
fails, wait for the other lanes so the developer receives the complete failure
inventory in one run.

- [ ] **Step 4: Tune inner pytest concurrency against the outer lanes**

Measure complete-gate `real` time with `-n 4`, `-n 6`, `-n 8`, `-n 10`, and
`-n auto` using default `--dist=load`. Run each candidate at least twice after
one warm-up. Choose the lowest stable complete-gate result and pin the explicit
worker count in `backend/pytest.ini`.

If architecture tests benefit from one-worker index reuse, register:

```ini
markers =
    architecture_contract: semantic repository-wide source contract
```

and use a collection hook to place only those tests early or in one resource
group. Do not switch the whole suite to `worksteal`; measured evidence shows it
is slower.

- [ ] **Step 5: Verify the coordinator preserves failures**

Temporarily make one lane command return non-zero in a shell copy under
`/tmp`, then verify:

- aggregate exit is non-zero;
- all three logs print;
- timing records print;
- no source file changes.

Do not commit an intentional failing command.

- [ ] **Step 6: Run the complete gate and profile lanes**

Run:

```bash
/usr/bin/time -p env \
  PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python \
  bash scripts/check.sh
```

Expected: all layers pass. If `real >= 60`, use lane timings and backend
durations to return to Task 6. Do not remove work from the gate.

- [ ] **Step 7: Commit the parallel quality gate**

```bash
git add scripts/check.sh scripts/check_backend.sh \
  scripts/check_contracts.sh scripts/check_frontend.sh \
  backend/pytest.ini backend/tests/conftest.py \
  backend/tests/test_test_architecture_policy.py \
  frontend/app/test-runner-config.test.mjs
git commit -m "test: run complete verification in bounded parallel lanes"
```

---

### Task 8: Synchronize documentation and complete acceptance verification

**Files:**
- Modify: `README.md`
- Modify: `README_zh.md`
- Modify: `AGENTS.md`
- Verify: entire worktree and draft PR

**Interfaces:**
- Consumes: final runner names, semantic policy, measured worker count, and verification evidence.
- Produces: synchronized contributor contract and PR evidence.

- [ ] **Step 1: Update the three documentation files together**

Document:

```text
- scripts/check.sh is the complete offline local gate and runs bounded lanes.
- frontend *.test.mjs files use node:test for pure/semantic contracts.
- frontend *.component.test.tsx files use Vitest/jsdom/Testing Library.
- source positions are diagnostics only; line numbers cannot identify expected sites.
- semantic static scans are limited to architecture/security/vocabulary/entry contracts.
- component behavior is tested through roles, actions, and state rather than CSS/source layout.
- local acceptance uses PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python.
```

State the 60-second target as the verified development baseline, not a portable
hard assertion that every machine must meet.

- [ ] **Step 2: Run documentation and policy guards**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -n0 \
  backend/tests/test_architecture_documentation.py \
  backend/tests/test_test_architecture_policy.py -q

cd frontend
node --test app/test/static-source-policy.test.mjs \
  app/test-runner-config.test.mjs
```

Expected: all pass with no skip or xfail.

- [ ] **Step 3: Run the complete backend and frontend layers once**

Run:

```bash
PYTHONPATH=backend \
  /opt/homebrew/Caskroom/miniconda/base/bin/python \
  -m pytest -p no:cacheprovider backend/tests --durations=40

cd frontend
npm run test
npm run lint
npm run build
```

Expected: all tests pass, zero skips, zero xfails, TypeScript passes, and the
production build succeeds.

- [ ] **Step 4: Run one clean complete-gate verification**

Run:

```bash
/usr/bin/time -p env \
  PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python \
  bash scripts/check.sh
```

Expected: exit zero and every required layer is visible in the lane logs.

- [ ] **Step 5: Run three consecutive warm acceptance measurements**

Run the same command three times without source changes. Record:

| Run | Contracts lane | Backend lane | Frontend lane | Total real |
| --- | ---: | ---: | ---: | ---: |
| warm 1 | measured | measured | measured | `<60.00s` |
| warm 2 | measured | measured | measured | `<60.00s` |
| warm 3 | measured | measured | measured | `<60.00s` |

Expected: each run exits zero, has no skip/xfail, and is under 60 seconds. One
miss invalidates acceptance and returns work to the slowest measured lane.

- [ ] **Step 6: Inspect the final diff and commit documentation**

Run:

```bash
git diff --check
git status --short
git diff --stat origin/master...HEAD
git diff origin/master...HEAD -- backend/tests/fixtures/repository_v9
```

Expected: no whitespace errors, no v9 fixture changes, no generated build
artifacts, and only intended source/test/docs changes.

Commit:

```bash
git add README.md README_zh.md AGENTS.md
git commit -m "docs: define durable sub-minute test contracts"
```

- [ ] **Step 7: Push and create one draft PR**

Push `codex/test-architecture-governance` and open a draft PR. The PR body
contains:

- baseline `146.13s`;
- final three warm totals and lane timings;
- backend test count and frontend Node/component counts;
- a table mapping each deleted/materially rewritten brittle assertion to its
  replacement contract;
- explicit confirmation of zero skip/xfail and unchanged frozen v9 fixture;
- statement that hosted GitHub CI is a follow-up.

- [ ] **Step 8: Dispatch exactly one independent reviewer after PR creation**

Create one subagent with model `gpt-5.6-sol`, reasoning `high`, and ask it to:

```text
Review the open PR for coverage loss, semantic-source false negatives,
line-number identity leakage, fixture-generator drift, process cleanup/failure
aggregation, timing shortcuts, frontend behavior regressions, and documentation
accuracy. Re-run focused tests as needed. End with:
Ready to merge: Yes
or
Ready to merge: With fixes
and list actionable findings with file/line diagnostics.
```

- [ ] **Step 9: Validate findings and obtain final reviewer approval**

Use `superpowers:receiving-code-review` before changing code. Reproduce each
finding, fix only confirmed issues through TDD, rerun affected layers and all
three warm acceptance measurements if timing/runner behavior changed, push the
updates, and send the same reviewer a follow-up. Completion requires that same
reviewer to report:

```text
Ready to merge: Yes
```

- [ ] **Step 10: Final handoff**

Report the draft PR URL, final commit, three timing totals, test counts,
zero-skip/xfail status, frozen-fixture status, and reviewer conclusion. Do not
merge the PR unless the user separately requests it.
