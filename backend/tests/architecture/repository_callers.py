from __future__ import annotations

import ast
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
import re

from tests.architecture.semantic_source import (
    PythonSourceIndex,
    SemanticFinding,
    SemanticKey,
    dotted_name,
    qualified_scopes,
)


ROOT = Path(__file__).resolve().parents[3]
PRODUCTION_ROOTS = (ROOT / "backend" / "app", ROOT / "scripts")
REPOSITORY_SQL_PREFIXES = (
    "backend/app/repositories/sqlite/",
    "backend/app/repositories/postgres/",
)
SQLITE_REPOSITORY_PREFIX = "backend/app/repositories/sqlite/"
REPO_NAME_RE = re.compile(r"^(?:repo\d*|repository|[A-Za-z_]\w*_repo)$")

SQL_REASON_BY_PATH = {
    "backend/app/migration/shadow/postgres_catalog.py": (
        "forward-shadow baseline COPY validates the exact migration-derived PostgreSQL business catalog at the running schema pair before copying and checkpoint publication"
    ),
    "backend/app/migration/shadow/capture.py": (
        "temporary migration boundary owns run-scoped SQLite capture DDL"
    ),
    "backend/app/migration/shadow/control.py": (
        "temporary migration boundary owns shadow preflight and control SQL"
    ),
    "backend/app/migration/shadow/identity.py": (
        "temporary migration boundary binds redacted SQLite and PostgreSQL identities"
    ),
    "backend/app/migration/shadow/manifest.py": (
        "temporary migration boundary validates source and target schema keys"
    ),
    "backend/app/migration/shadow/snapshot.py": (
        "temporary migration boundary validates and publishes a SQLite backup"
    ),
    "backend/app/migration/shadow/bulk_copy.py": (
        "temporary migration boundary owns resumable baseline COPY and checkpoints"
    ),
    "backend/app/migration/shadow/replicator.py": (
        "temporary migration boundary owns fail-stop forward apply and checkpoints"
    ),
    "backend/app/migration/shadow/verifier.py": (
        "temporary migration boundary owns barrier-aware cross-database verification and redacted reports"
    ),
    "backend/app/migration/shadow/cli.py": (
        "explicit shadow operator composition root reads redacted control status and resumable baseline state"
    ),
    "backend/app/migration/shadow/worker.py": (
        "temporary migration worker owns its database-clock lease lifecycle"
    ),
    "backend/app/migration/shadow/retention.py": (
        "temporary migration worker owns conservative verified change-log retention"
    ),
    "backend/app/repositories/source_subgraph_projection.py": (
        "backend-neutral selected-source graph reads bind the caller's own "
        "read transaction, outside the sqlite/postgres adapter prefixes this "
        "scan already exempts"
    ),
    "backend/app/core/cache/sqlite_backend.py": (
        "independent content-addressed LLM/embedding cache database"
    ),
    "backend/app/core/llm_cache.py": "independent LLM response cache database",
    "backend/app/eval/db.py": "independent evaluation-results database",
    "backend/app/migration/sqlite_to_postgres.py": (
        "owned offline cross-backend snapshot-import boundary"
    ),
    "scripts/bench_sqlite_writes.py": "synthetic temporary write benchmark database",
    "scripts/backfill_knowhow_md.py": "read-only planning for an operator-supplied maintenance database",
    "scripts/diag_open_latency.py": "read-only host database diagnostic",
    "scripts/diag_db.py": "read-only host database diagnostic",
    "scripts/diag_pg_hotpaths.py": "read-only host database diagnostic",
    "scripts/diag_slow.py": "read-only host database diagnostic",
    "scripts/generate_repository_contract_fixtures.py": "disposable contract fixture databases",
    "scripts/kg_quality_audit.py": "read-only host database diagnostic",
    "scripts/audit_kg_edge_contract.py": "read-only host database diagnostic",
    "scripts/audit_source_facts.py": "read-only host database diagnostic",
    "scripts/merge_dbs.py": "temporary copies used by the offline two-database merge tool",
    "scripts/verify_repository_snapshot.py": "read-only backup and probe databases",
}

FACADE_IMPORT_REASON_BY_PATH = {
    "backend/app/repositories/factory.py": "formal backend-selection composition root",
    "backend/app/eval/inference.py": "evaluation composition root",
    "backend/app/eval/run_all.py": "evaluation composition root",
    "backend/app/eval/sa_calibration.py": "evaluation composition root",
    "backend/app/eval/speed.py": "evaluation composition root",
    "backend/app/scripts/gen_recall_gold.py": "offline CLI composition root",
    # Kept although FACADE_IMPORT_TARGETS currently misses it: the shim still
    # late-binds facade privates via `from app.services.sqlite_repository
    # import _new_id / _COPY_CHUNK`, member imports the target set does not
    # match. The boundary is real; only the scan is blind to this spelling.
    "backend/app/services/sqlite_notebook_sharing.py": (
        "compatibility shim late-binds legacy facade monkeypatch seams"
    ),
    "scripts/backfill_knowhow_md.py": "offline maintenance CLI composition root",
    "scripts/bench_sqlite_writes.py": "synthetic benchmark composition root",
    "scripts/generate_repository_contract_fixtures.py": "contract fixture composition root",
    "scripts/kg_product_smoke.py": "offline smoke composition root",
    "scripts/replay_retrieval.py": "offline CLI composition root",
    "scripts/smoke_backend.py": "offline smoke composition root",
    "scripts/verify_repository_snapshot.py": "backup-only verifier composition root",
}

FACADE_IMPORT_TARGETS = {
    "app.services.sqlite_repository",
    "app.services.sqlite_repository:SQLiteRepository",
    "app.services:sqlite_repository",
}

PRIVATE_REASON_BY_PATH = {
    "backend/app/api/deps.py": "composition root extracts narrow runtime ports",
    "backend/app/bootstrap.py": "composition root primes the extension admission snapshot from the runtime toggle seat",
    "backend/app/api/kg_routes.py": "API readiness checks the process-owned model provider",
    "backend/app/api/knowhow_routes.py": "API readiness checks the process-owned model provider",
    "backend/app/api/mcp_tools/maintenance.py": "API readiness checks the process-owned model provider",
    "backend/app/api/report_routes.py": "API readiness checks the process-owned model provider",
    "backend/app/api/source_routes.py": "API readiness checks the process-owned model provider",
    "backend/app/services/knowhow/api.py": "knowhow orchestration constructs narrow services from runtime ports",
    "backend/app/services/knowhow/transfer.py": "knowhow transfer orchestration constructs its store and runtime seams",
    "backend/app/services/startup_warmup.py": "server lifespan is the sole owner of crash recovery; deliberately not a public repository port",
    "scripts/bench_sqlite_writes.py": "synthetic temporary write benchmark",
    "scripts/generate_repository_contract_fixtures.py": "disposable contract fixture databases",
    "scripts/verify_repository_snapshot.py": "models a server start on a disposable backup, including the startup recovery sweep",
}

SQLITE_CONNECT_REASON_BY_PATH = {
    "backend/app/migration/shadow/cli.py": (
        "explicit shadow operator preflight opens the active SQLite source read-only"
    ),
    "backend/app/migration/shadow/snapshot.py": (
        "temporary migration boundary opens its private validated backup"
    ),
    "backend/app/migration/shadow/bulk_copy.py": (
        "temporary migration boundary streams its immutable private snapshot"
    ),
    "backend/app/migration/shadow/verifier.py": (
        "temporary migration boundary streams facts through its private disposable SQLite spool"
    ),
    "backend/app/core/cache/sqlite_backend.py": (
        "independent content-addressed LLM/embedding cache database"
    ),
    "backend/app/core/llm_cache.py": "independent LLM response cache database",
    "backend/app/eval/db.py": "independent evaluation-results database",
    "backend/app/migration/sqlite_to_postgres.py": (
        "owned offline cross-backend snapshot-import boundary"
    ),
    "scripts/backfill_promotion_targets.py": "operator-supplied offline maintenance database",
    "scripts/backfill_knowhow_md.py": "read-only planning connection to an operator-supplied database",
    "scripts/diag_open_latency.py": "read-only host database diagnostic",
    "scripts/diag_db.py": "read-only host database diagnostic",
    "scripts/diag_slow.py": "read-only host database diagnostic",
    "scripts/generate_repository_contract_fixtures.py": "disposable contract fixture databases",
    "scripts/kg_quality_audit.py": "read-only host database diagnostic",
    "scripts/audit_kg_edge_contract.py": "read-only host database diagnostic",
    "scripts/audit_source_facts.py": "read-only host database diagnostic",
    "scripts/merge_dbs.py": "temporary copies used by the offline two-database merge tool",
    "scripts/verify_repository_snapshot.py": "read-only backup and probe databases",
}


def _production_files() -> tuple[tuple[Path, str], ...]:
    return tuple(
        (path, path.relative_to(ROOT).as_posix())
        for base in PRODUCTION_ROOTS
        for path in sorted(base.rglob("*.py"))
        if "__pycache__" not in path.parts
    )


@lru_cache(maxsize=1)
def production_source_index() -> PythonSourceIndex:
    return PythonSourceIndex.from_paths(
        ROOT,
        (path for path, _relative in _production_files()),
    )


def _matching_calls(
    targets: set[str],
    *,
    exclude_repository_sql: bool = False,
) -> tuple[SemanticFinding, ...]:
    return tuple(
        finding
        for finding in production_source_index().calls()
        if finding.key.target.rsplit(".", 1)[-1] in targets
        and (
            not exclude_repository_sql
            or not finding.key.path.startswith(REPOSITORY_SQL_PREFIXES)
        )
    )


def product_sql_sites() -> tuple[SemanticFinding, ...]:
    return _matching_calls(
        {"execute", "executemany", "executescript"},
        exclude_repository_sql=True,
    )


def sqlite_connect_sites() -> tuple[SemanticFinding, ...]:
    return tuple(
        finding
        for finding in production_source_index().calls(target="sqlite3.connect")
        if not finding.key.path.startswith(SQLITE_REPOSITORY_PREFIX)
    )


def facade_import_sites() -> tuple[SemanticFinding, ...]:
    return tuple(
        finding
        for finding in production_source_index().imports()
        if finding.key.target in FACADE_IMPORT_TARGETS
    )


def _annotation_name(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return dotted_name(node)


def _is_repository_annotation(node: ast.AST | None) -> bool:
    name = _annotation_name(node).rsplit(".", 1)[-1].strip("'\"")
    return name.endswith(("Repository", "RepositoryPort")) or name in {
        "AskStreamPort",
        "SQLiteMaintenancePort",
    }


def _is_repo_expression(
    node: ast.AST,
    names: set[str],
    attrs: set[str],
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names or bool(REPO_NAME_RE.match(node.id))
    if isinstance(node, ast.Attribute):
        return (
            isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and (node.attr in attrs or bool(REPO_NAME_RE.match(node.attr)))
        )
    if isinstance(node, ast.Call):
        return dotted_name(node.func).rsplit(".", 1)[-1] in {
            "SQLiteRepository",
            "repository",
        }
    return False


def _repo_like_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if isinstance(value, ast.Call):
                function = dotted_name(value.func).rsplit(".", 1)[-1]
                if function in {"SQLiteRepository", "repository"}:
                    names.update(
                        target.id for target in targets if isinstance(target, ast.Name)
                    )
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in (*node.args.args, *node.args.kwonlyargs):
                if _is_repository_annotation(argument.annotation):
                    names.add(argument.arg)
    return names


def _repo_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    names = _repo_like_names(tree)
    attrs: set[str] = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            if value is None:
                continue
            value_is_repo = _is_repo_expression(value, names, attrs)
            if not value_is_repo and isinstance(node, ast.AnnAssign):
                value_is_repo = _is_repository_annotation(node.annotation)
            if not value_is_repo:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
                elif (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr not in attrs
                ):
                    attrs.add(target.attr)
                    changed = True
    return names, attrs


def private_repository_sites() -> tuple[SemanticFinding, ...]:
    diagnostic_lines_by_key: dict[SemanticKey, list[int]] = defaultdict(list)
    for path, relative in _production_files():
        if relative == "backend/app/services/sqlite_repository.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        scopes = qualified_scopes(tree)
        repo_names, repo_attrs = _repo_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if not node.attr.startswith("_") or node.attr.startswith("__"):
                continue
            if not _is_repo_expression(node.value, repo_names, repo_attrs):
                continue
            key = SemanticKey(
                path=relative,
                scope=scopes[node],
                kind="attribute",
                target=node.attr,
            )
            diagnostic_lines_by_key[key].append(node.lineno)
    return tuple(
        SemanticFinding(
            key=key,
            count=len(lines),
            diagnostic_lines=tuple(sorted(lines)),
        )
        for key, lines in sorted(diagnostic_lines_by_key.items())
    )


def _entries(
    *,
    kind: str,
    findings: tuple[SemanticFinding, ...],
    reasons: dict[str, str],
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for finding in findings:
        reason = reasons.get(finding.key.path)
        if reason is None:
            raise AssertionError(
                f"unreasoned {kind} boundary: {finding.key} "
                f"(lines {finding.diagnostic_lines})"
            )
        entries.append(
            {
                "path": finding.key.path,
                "scope": finding.key.scope,
                "kind": kind,
                "target": finding.key.target,
                "count": finding.count,
                "reason": reason,
            }
        )
    return sorted(
        entries,
        key=lambda entry: (
            entry["path"],
            entry["scope"],
            entry["target"],
        ),
    )


def collect_caller_contract() -> dict[str, list[dict[str, object]]]:
    return {
        "facade_imports": _entries(
            kind="facade_imports",
            findings=facade_import_sites(),
            reasons=FACADE_IMPORT_REASON_BY_PATH,
        ),
        "independent_private": _entries(
            kind="independent_private",
            findings=private_repository_sites(),
            reasons=PRIVATE_REASON_BY_PATH,
        ),
        "independent_sql": _entries(
            kind="independent_sql",
            findings=product_sql_sites(),
            reasons=SQL_REASON_BY_PATH,
        ),
        "sqlite_connect": _entries(
            kind="sqlite_connect",
            findings=sqlite_connect_sites(),
            reasons=SQLITE_CONNECT_REASON_BY_PATH,
        ),
    }
