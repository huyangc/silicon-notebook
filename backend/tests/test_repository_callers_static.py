"""Task 27 static caller / SQL ownership rules.

Production callers (API routes/deps, application services, eval harnesses,
`backend/app/scripts` and `scripts/` CLI tools) own **no SQLite plumbing**:

1. the concrete ``SQLiteRepository`` may be imported only by the API/CLI
   composition roots and the three transitional compatibility facades;
2. no production caller reaches a private facade member (``repo._x``) — the
   exact residual seams (typed-accessor/runtime reaches, the synthetic write
   benchmark) are frozen below;
3. the retired retrieval privates (``_retrieve_scored`` / ``_ppr_retrieve`` /
   ``_answer_context`` / ``_chunk_answer_context``) and the test-only
   ``eval_insert_source_for_test`` helper have zero production consumers;
4. main-database SQL lives under ``backend/app/repositories/sqlite`` — the
   only files allowed to open a SQLite connection themselves are the exact,
   documented exception list (independent DBs, the host diagnostic, the
   baseline-guarded contract tooling).

Every allowlist is exact: adding a new file or a new operation fails this
suite until the constant is consciously edited in review.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND_APP = ROOT / "backend" / "app"
SCRIPTS = ROOT / "scripts"

# ---------------------------------------------------------------------------
# rule 1 — the concrete facade class is a composition-root-only import
# ---------------------------------------------------------------------------
# The three compatibility facades plus every API/CLI/eval composition root
# that constructs SQLiteRepository(Settings()) itself.  Application services
# other than the batch-ingest CLI module never see the concrete class.
FACADE_CLASS_IMPORT_ALLOWED = {
    # transitional compatibility facades
    "backend/app/services/sqlite_repository.py",
    "backend/app/services/sqlite_identity.py",
    "backend/app/services/sqlite_notebook_sharing.py",
    # API composition root
    "backend/app/api/deps.py",
    # CLI composition root for scripts/batch_ingest.py (main() builds the repo)
    "backend/app/services/batch_ingest.py",
    # eval harness composition roots
    "backend/app/eval/inference.py",
    "backend/app/eval/run_all.py",
    "backend/app/eval/sa_calibration.py",
    "backend/app/eval/speed.py",
    # offline CLI composition roots
    "backend/app/scripts/backfill_relation_embeddings.py",
    "backend/app/scripts/build_kg.py",
    "backend/app/scripts/gen_recall_gold.py",
    "backend/app/scripts/recluster_kg.py",
    "backend/app/scripts/reembed_kg.py",
    "scripts/backfill_kg_embeddings.py",
    "scripts/bench_sqlite_writes.py",
    "scripts/build_chunks.py",
    "scripts/denoise_reextract_nb.py",
    "scripts/diag_base_report.py",
    "scripts/kg_product_smoke.py",
    "scripts/reextract_notebook.py",
    "scripts/replay_retrieval.py",
    "scripts/smoke_backend.py",
    "scripts/generate_repository_contract_fixtures.py",
    "scripts/verify_repository_snapshot.py",
}

# ---------------------------------------------------------------------------
# rule 2 — private facade member access is frozen to these exact seams
# ---------------------------------------------------------------------------
PRIVATE_MEMBER_ALLOWED = {
    # typed-accessor composition plumbing (deps builds the narrow ports)
    ("backend/app/api/deps.py", "_runtime"),
    # Task-23 frozen seam: _stream_ask_events reaches the runtime-owned
    # AskExecutionCoordinator through the repo it is handed
    ("backend/app/api/routes.py", "_runtime"),
    # ledgered production runtime reaches (ACTIVE_PRODUCTION_MEMBER_SITES)
    ("backend/app/services/communities.py", "_runtime"),
    ("backend/app/services/reasoning_retrieval.py", "_runtime"),
    # Task-25 frozen-call-site adapter (from_repository extracts narrow ports)
    ("backend/app/services/report_engine.py", "_runtime"),
    # synthetic temporary write benchmark — never the product DB
    ("scripts/bench_sqlite_writes.py", "_connect"),
}

# Baseline-guarded contract tooling replays frozen private call patterns on
# purpose; both files are pinned read-only/backup-only by their own suites.
PRIVATE_MEMBER_EXEMPT_FILES = {
    "scripts/generate_repository_contract_fixtures.py",
    "scripts/verify_repository_snapshot.py",
}

RETIRED_RETRIEVAL_PRIVATES = {
    "_retrieve_scored",
    "_ppr_retrieve",
    "_answer_context",
    "_chunk_answer_context",
}

# ---------------------------------------------------------------------------
# rule 4 — the exact files allowed to open a SQLite connection themselves
# ---------------------------------------------------------------------------
SQLITE_CONNECT_ALLOWED = {
    "backend/app/core/llm_cache.py",        # independent LLM cache DB
    "backend/app/eval/db.py",               # independent evaluation DB (mode=ro)
    "scripts/bench_sqlite_writes.py",       # synthetic temporary write benchmark
    "scripts/diag_slow.py",                 # stdlib-only host diagnostic, mode=ro
    "scripts/generate_repository_contract_fixtures.py",  # baseline-guarded fixture generator
    "scripts/verify_repository_snapshot.py",             # mode=ro + sqlite backup only (Task 28)
}

REPO_NAME_RE = re.compile(r"^(?:repo\d*|repository|[A-Za-z_]\w*_repo)$")
WRITE_SQL_RE = re.compile(r"(?<!\.)\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


def _production_files():
    for base in (BACKEND_APP, SCRIPTS):
        for path in sorted(base.rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            if "__pycache__" in rel:
                continue
            yield path, rel


def _dotted(node: ast.AST) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _repo_like_names(tree: ast.AST) -> set[str]:
    """Names bound to SQLiteRepository(...) / repository() results plus
    parameters annotated with the facade or its portable protocol."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Call):
                fn = _dotted(value.func).rsplit(".", 1)[-1]
                if fn in {"SQLiteRepository", "repository"}:
                    names.update(t.id for t in targets if isinstance(t, ast.Name))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                ann = arg.annotation
                ann_name = ""
                if isinstance(ann, ast.Name):
                    ann_name = ann.id
                elif isinstance(ann, ast.Attribute):
                    ann_name = ann.attr
                elif isinstance(ann, ast.Constant) and isinstance(ann.value, str):
                    ann_name = ann.value.rsplit(".", 1)[-1]
                if ann_name in {"SQLiteRepository", "NotebookRepository", "AskStreamPort"}:
                    names.add(arg.arg)
    return names


def _private_member_hits():
    hits = []
    for path, rel in _production_files():
        if rel in PRIVATE_MEMBER_EXEMPT_FILES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        repo_names = _repo_like_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            attr = node.attr
            if not attr.startswith("_") or attr.startswith("__"):
                continue
            base = node.value
            repo_base = False
            if isinstance(base, ast.Name):
                repo_base = bool(REPO_NAME_RE.match(base.id)) or base.id in repo_names
            elif isinstance(base, ast.Call):
                fn = _dotted(base.func).rsplit(".", 1)[-1]
                repo_base = fn in {"SQLiteRepository", "repository"}
            if repo_base:
                hits.append((rel, node.lineno, attr))
    return hits


def test_facade_class_imports_only_in_composition_roots():
    offenders = []
    for path, rel in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            imported = False
            if isinstance(node, ast.ImportFrom):
                if node.module == "app.services.sqlite_repository" and any(
                    alias.name == "SQLiteRepository" for alias in node.names
                ):
                    imported = True
            elif isinstance(node, ast.Import):
                if any(alias.name == "app.services.sqlite_repository" for alias in node.names):
                    imported = True
            if imported and rel not in FACADE_CLASS_IMPORT_ALLOWED:
                offenders.append((rel, node.lineno))
    assert not offenders, (
        f"SQLiteRepository imported outside the frozen composition roots: {offenders}"
    )


def test_composition_root_allowlist_stays_exact():
    # a stale allowlist entry (file deleted / no longer importing the facade)
    # must be pruned consciously — verify_repository_snapshot.py lands in
    # Task 28 and is the only tolerated not-yet-existing entry.
    for rel in sorted(FACADE_CLASS_IMPORT_ALLOWED | SQLITE_CONNECT_ALLOWED):
        if rel == "scripts/verify_repository_snapshot.py" and not (ROOT / rel).is_file():
            continue
        assert (ROOT / rel).is_file(), f"stale allowlist entry: {rel}"


def test_no_private_facade_member_access_outside_frozen_seams():
    offenders = [
        (rel, line, attr)
        for rel, line, attr in _private_member_hits()
        if (rel, attr) not in PRIVATE_MEMBER_ALLOWED
    ]
    assert not offenders, (
        "production callers must use ports/typed accessors or repo.maintenance, "
        f"not private facade members: {sorted(offenders)}"
    )


def test_retired_retrieval_privates_have_no_production_callers():
    offenders = [
        (rel, line, attr)
        for rel, line, attr in _private_member_hits()
        if attr in RETIRED_RETRIEVAL_PRIVATES
    ]
    assert not offenders, (
        f"retired retrieval privates called from production: {sorted(offenders)}"
    )


def test_eval_insert_source_helper_has_no_production_consumer():
    offenders = []
    for path, rel in _production_files():
        if rel == "backend/app/services/sqlite_repository.py":
            continue  # the compatibility definition itself
        if rel == "backend/app/repositories/ownership_manifest.py":
            continue  # frozen manifest string record
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "eval_insert_source_for_test":
                offenders.append((rel, node.lineno))
            elif isinstance(node, ast.Name) and node.id == "eval_insert_source_for_test":
                offenders.append((rel, node.lineno))
    assert not offenders, f"new production consumer of eval_insert_source_for_test: {offenders}"


def test_sqlite_connect_call_sites_are_frozen():
    offenders = []
    for path, rel in _production_files():
        if rel.startswith("backend/app/repositories/sqlite/"):
            continue  # the database boundary owns its connections
        if "sqlite3.connect(" not in path.read_text(encoding="utf-8"):
            continue
        if rel not in SQLITE_CONNECT_ALLOWED:
            offenders.append(rel)
    assert not offenders, (
        "SQLite connections outside repositories/sqlite are frozen to the "
        f"documented exception list: {offenders}"
    )


def test_diag_slow_stays_host_safe_and_read_only():
    """AGENTS.md contract: scripts/diag_slow.py is a stdlib-only host
    diagnostic — no app imports, product DB opened mode=ro only, no DML."""
    path = SCRIPTS / "diag_slow.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("app"), alias.name
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("app"), node.module
    for lineno, line in enumerate(source.splitlines(), 1):
        if "sqlite3.connect(" in line:
            assert "mode=ro" in line, f"diag_slow.py:{lineno} opens the DB read-write"
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            statement = node.value.lstrip().upper()
            assert not statement.startswith(
                ("INSERT INTO", "UPDATE ", "DELETE FROM", "REPLACE INTO")
            ), f"diag_slow.py contains DML: {node.value[:60]!r}"


def test_routes_use_narrow_ports_not_notebook_repository():
    """Route/helper code must not be typed as NotebookRepository when a
    narrow accessor exists (ask streaming uses AskStreamPort)."""
    path = BACKEND_APP / "api" / "routes.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in list(node.args.args) + list(node.args.kwonlyargs):
                ann = arg.annotation
                name = ""
                if isinstance(ann, ast.Name):
                    name = ann.id
                elif isinstance(ann, ast.Attribute):
                    name = ann.attr
                if name == "NotebookRepository":
                    offenders.append((node.name, arg.arg, node.lineno))
    assert not offenders, f"routes.py parameters typed as NotebookRepository: {offenders}"


def test_portable_application_ports_never_include_maintenance_operations():
    from app.repositories.ports import NotebookRepository, SQLiteMaintenancePort

    maintenance_ops = (
        "delete_notebook_kg",
        "backfill_kg_fts",
        "backfill_chunk_fts",
        "build_scale_index",
        "fold_scale_index_delta",
    )
    for name in maintenance_ops:
        assert hasattr(SQLiteMaintenancePort, name), name
    for name in ("backfill_kg_fts", "backfill_chunk_fts", "fold_scale_index_delta",
                 "build_scale_index", "eval_insert_source_for_test", "maintenance"):
        assert not hasattr(NotebookRepository, name), (
            f"portable NotebookRepository port must not expose {name}"
        )


def test_maintenance_adapter_implements_the_port():
    from app.repositories.sqlite.maintenance import (
        ReadOnlySQLiteInspector,
        SQLiteMaintenanceAdapter,
    )

    for name in (
        "delete_notebook_kg", "backfill_kg_fts", "backfill_chunk_fts",
        "build_scale_index", "fold_scale_index_delta",
    ):
        assert name in SQLiteMaintenanceAdapter.__dict__, name
    # the mode-ro inspector serves MRL eval + arbitrary-path validation tools
    source = (BACKEND_APP / "repositories" / "sqlite" / "maintenance.py").read_text(
        encoding="utf-8"
    )
    assert "mode=ro" in source
    for name in ("connect", "table_count", "vector_blocks", "detect_vector_dim"):
        assert name in ReadOnlySQLiteInspector.__dict__, name


def test_facade_exposes_the_maintenance_adapter():
    from app.services.sqlite_repository import SQLiteRepository

    assert isinstance(
        __import__("inspect").getattr_static(SQLiteRepository, "maintenance"),
        property,
    )
