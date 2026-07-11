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

# Exact non-product SQL seats.  Values are reviewable reasons; keys are call
# sites so both a newly added call and a stale exception fail the contract.
INDEPENDENT_SQL_SITES: dict[tuple[str, int, str], str] = {
    # Separate local LLM response cache database.
    **{
        site: "independent LLM cache database"
        for site in {
            ("backend/app/core/llm_cache.py", 49, "db.execute"),
            ("backend/app/core/llm_cache.py", 58, "conn.execute"),
            ("backend/app/core/llm_cache.py", 59, "conn.execute"),
            ("backend/app/core/llm_cache.py", 64, "db.execute"),
            ("backend/app/core/llm_cache.py", 72, "db.execute"),
        }
    },
    # Separate evaluation-results database.
    **{
        site: "independent evaluation-results database"
        for site in {
            ("backend/app/eval/db.py", 29, "db.execute"),
            ("backend/app/eval/db.py", 53, "db.execute"),
            ("backend/app/eval/db.py", 64, "db.execute"),
        }
    },
    ("scripts/bench_sqlite_writes.py", 86, "db.execute"): (
        "synthetic temporary write benchmark; never opens the product database"
    ),
    # Host diagnostic opens the product database with mode=ro; the dedicated
    # read-only test below also rejects app imports and DML.
    **{
        site: "host diagnostic opens the product database mode=ro"
        for site in {
            ("scripts/diag_slow.py", 373, "conn.execute"),
            ("scripts/diag_slow.py", 403, "conn.execute"),
            ("scripts/diag_slow.py", 410, "conn.execute"),
            ("scripts/diag_slow.py", 416, "conn.execute"),
            ("scripts/diag_slow.py", 469, "conn.execute"),
            ("scripts/diag_slow.py", 482, "conn.execute"),
            ("scripts/diag_slow.py", 491, "conn.execute"),
            ("scripts/diag_slow.py", 550, "conn.execute"),
            ("scripts/diag_slow.py", 556, "conn.execute"),
            ("scripts/diag_slow.py", 713, "conn.execute"),
        }
    },
    # Baseline fixture generator writes only disposable fixture databases.
    **{
        site: "contract fixture generator writes a disposable fixture database"
        for site in {
            ("scripts/generate_repository_contract_fixtures.py", 780, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 796, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 818, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 832, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 846, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 855, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 890, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 897, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1017, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1020, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1030, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1034, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1170, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1203, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1228, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1239, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1266, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1270, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1294, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1319, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1344, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1351, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1359, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1367, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1400, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1408, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1415, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1431, "db.executemany"),
            ("scripts/generate_repository_contract_fixtures.py", 1439, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1444, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1456, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1468, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1487, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1504, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1528, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1700, "db.execute"),
            ("scripts/generate_repository_contract_fixtures.py", 1992, "db.execute"),
        }
    },
    # Snapshot verifier attaches only to a private backup/probe opened ro.
    **{
        site: "snapshot verifier reads only its backup/probe database"
        for site in {
            ("scripts/verify_repository_snapshot.py", 358, "digest_conn.execute"),
            ("scripts/verify_repository_snapshot.py", 365, "digest_conn.execute"),
            ("scripts/verify_repository_snapshot.py", 387, "meta_conn.execute"),
            ("scripts/verify_repository_snapshot.py", 391, "meta_conn.execute"),
            ("scripts/verify_repository_snapshot.py", 416, "meta_conn.execute"),
            ("scripts/verify_repository_snapshot.py", 417, "meta_conn.execute"),
            ("scripts/verify_repository_snapshot.py", 434, "meta_conn.execute"),
            ("scripts/verify_repository_snapshot.py", 446, "meta_conn.execute"),
            ("scripts/verify_repository_snapshot.py", 773, "probe.execute"),
            ("scripts/verify_repository_snapshot.py", 780, "probe.execute"),
            ("scripts/verify_repository_snapshot.py", 809, "probe.execute"),
        }
    },
}

# ---------------------------------------------------------------------------
# rule 1 — the concrete facade class is a composition-root-only import
# ---------------------------------------------------------------------------
# The three compatibility facades plus every API/CLI/eval composition root
# that constructs SQLiteRepository(Settings()) itself.  Application services
# other than the batch-ingest CLI module never see the concrete class.
FACADE_CLASS_IMPORT_SITES: dict[tuple[str, int], str] = {
    ("backend/app/api/deps.py", 12): "API composition root",
    ("backend/app/eval/inference.py", 67): "evaluation composition root",
    ("backend/app/eval/run_all.py", 68): "evaluation composition root",
    ("backend/app/eval/sa_calibration.py", 180): "evaluation composition root",
    ("backend/app/eval/speed.py", 98): "evaluation composition root",
    ("backend/app/scripts/backfill_relation_embeddings.py", 5): "offline CLI composition root",
    ("backend/app/scripts/build_kg.py", 5): "offline CLI composition root",
    ("backend/app/scripts/gen_recall_gold.py", 8): "offline CLI composition root",
    ("backend/app/scripts/recluster_kg.py", 5): "offline CLI composition root",
    ("backend/app/scripts/reembed_kg.py", 7): "offline CLI composition root",
    ("backend/app/services/batch_ingest.py", 29): "batch-ingest CLI composition root",
    ("scripts/backfill_kg_embeddings.py", 21): "offline CLI composition root",
    ("scripts/bench_sqlite_writes.py", 20): "synthetic benchmark composition root",
    ("scripts/build_chunks.py", 5): "offline CLI composition root",
    ("scripts/denoise_reextract_nb.py", 18): "offline CLI composition root",
    ("scripts/diag_base_report.py", 26): "offline diagnostic composition root",
    ("scripts/generate_repository_contract_fixtures.py", 40): "contract fixture composition root",
    ("scripts/generate_repository_contract_fixtures.py", 268): "contract fixture composition root",
    ("scripts/generate_repository_contract_fixtures.py", 758): "contract fixture composition root",
    ("scripts/kg_product_smoke.py", 16): "offline smoke composition root",
    ("scripts/reextract_notebook.py", 10): "offline CLI composition root",
    ("scripts/replay_retrieval.py", 40): "offline CLI composition root",
    ("scripts/smoke_backend.py", 24): "offline smoke composition root",
    ("scripts/verify_repository_snapshot.py", 59): "backup-only verifier composition root",
}

# Exact exceptions must name the call site and explain why it is outside the
# product repository boundary.  A whole-file or (file, attribute) exception
# would let new private reaches silently accumulate beside an old one.
INDEPENDENT_PRIVATE_SITES: dict[tuple[str, int, str], str] = {
    **{
        site: "API dependency composition root extracts a narrow runtime port"
        for site in {
            ("backend/app/api/deps.py", 20, "_runtime"),
            ("backend/app/api/deps.py", 23, "_runtime"),
            ("backend/app/api/deps.py", 26, "_runtime"),
            ("backend/app/api/deps.py", 29, "_runtime"),
            ("backend/app/api/deps.py", 32, "_runtime"),
        }
    },
    ("scripts/bench_sqlite_writes.py", 85, "_connect"): (
        "synthetic temporary write benchmark; never opens the product database"
    ),
    **{
        site: "contract fixture generator operates on a disposable fixture database"
        for site in {
            ("scripts/generate_repository_contract_fixtures.py", 779, "_write"),
            ("scripts/generate_repository_contract_fixtures.py", 1028, "_connect"),
            ("scripts/generate_repository_contract_fixtures.py", 1169, "_write"),
            (
                "scripts/generate_repository_contract_fixtures.py",
                1581,
                "_scale_index_version",
            ),
            ("scripts/generate_repository_contract_fixtures.py", 1699, "_connect"),
            ("scripts/generate_repository_contract_fixtures.py", 1989, "_connect"),
        }
    },
}

LIFECYCLE_STORE_CALLS = {
    "knowledge": {
        "active_object_count", "community_context_rows", "delete_notebook_graph_rows",
        "embedding_rows", "embedding_rows_for_objects", "incremental_object_rows",
        "insert_kg_fts_rows", "insert_object_chunk", "insert_object_source_rows",
        "insert_relation_chunk", "neighbor_relation_rows", "notebook_tier_row",
        "object_meta_rows_for_notebook", "relink_rows", "source_build_rows",
        "unified_graph_rows",
    },
    "governance_store": {
        "delete_clusters", "delete_pending_merges", "incremental_cluster_rows",
        "insert_clusters", "insert_merge_candidate", "insert_pending_merge_rows",
        "merge_candidate_pairs", "sweep_orphan_clusters", "valid_object_ids",
    },
    "unified_kg": {
        "canonical_relation_seed_rows", "canonical_relations_count", "checkpoint_clear",
        "checkpoint_gc", "checkpoint_load", "checkpoint_put", "claim_name_rows",
        "clear_mention_bridge", "clear_scratch_run", "cluster_description_rows",
        "cluster_evidence_rows", "cluster_input_facts", "communities_count",
        "community_graph_rows", "community_member_ids", "community_reports",
        "community_rows_for_summary", "concept_clusters_count", "distinct_cluster_count",
        "finish_rebuild_state", "insert_scratch_rows", "mention_edges_count",
        "mention_scan_matches", "mention_seed_rows", "replace_canonical_relations",
        "replace_cluster_rows_streamed", "replace_communities", "replace_mention_bridge",
        "scratch_vector_rows", "seed_payload_rows", "set_community_seq",
        "set_community_summary", "state_row", "stream_scratch_rows", "stream_seed_rows",
    },
}
def _lifecycle_store_calls() -> dict[str, set[str]]:
    path = BACKEND_APP / "services" / "knowledge_lifecycle.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    actual = {owner: set() for owner in LIFECYCLE_STORE_CALLS}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        seat = node.func.value
        if isinstance(seat, ast.Attribute) and isinstance(seat.value, ast.Name):
            if seat.value.id == "self" and seat.attr in actual:
                actual[seat.attr].add(node.func.attr)
    return actual
def test_lifecycle_service_is_sql_free_and_uses_exact_store_seams():
    assert _lifecycle_store_calls() == LIFECYCLE_STORE_CALLS
    path = BACKEND_APP / "services" / "knowledge_lifecycle.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sql_calls = [(n.lineno, n.func.attr) for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"execute", "executemany", "executescript"}
    ]
    assert sql_calls == []
CLOSED_REMEDIATION_MODULES = {
    "product_sql": (
        "backend/app/services/scale_index_builder.py",
    ),
    "private_repository": (
        "backend/app/services/communities.py",
        "backend/app/services/report_engine.py",
    ),
}
CLOSED_REMEDIATION_ATTRIBUTES = {
    "product_sql": {"execute", "executemany", "executescript"},
    "private_repository": {"_runtime"},
}
EXPECTED_REMEDIATION_SITES: dict[str, set[tuple[str, int, str]]] = {
    "product_sql": set(),
    "private_repository": set(),
}

RETIRED_RETRIEVAL_PRIVATES = {
    "_retrieve_scored",
    "_ppr_retrieve",
    "_answer_context",
    "_chunk_answer_context",
}


def test_task4_application_services_contain_no_sql_calls() -> None:
    """Gate D: catalog/sharing/governance/scale orchestration owns no SQL."""
    modules = (
        "backend/app/services/notebook_catalog.py",
        "backend/app/services/notebook_sharing.py",
        "backend/app/services/knowledge_governance.py",
        "backend/app/services/scale_artifact_runtime.py",
    )
    offenders: list[tuple[str, int, str]] = []
    for relative in modules:
        tree = ast.parse(
            (ROOT / relative).read_text(),
            filename=relative,
        )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr in {"execute", "executemany", "executescript"}:
                offenders.append((relative, node.lineno, node.func.attr))
    assert offenders == []

# ---------------------------------------------------------------------------
# rule 4 — the exact files allowed to open a SQLite connection themselves
# ---------------------------------------------------------------------------
SQLITE_CONNECT_SITES: dict[tuple[str, int, str], str] = {
    ("backend/app/core/llm_cache.py", 56, "sqlite3.connect"): "independent LLM cache database",
    ("backend/app/eval/db.py", 23, "sqlite3.connect"): "independent evaluation database",
    ("scripts/diag_slow.py", 396, "sqlite3.connect"): "host diagnostic opens mode=ro",
    ("scripts/diag_slow.py", 544, "sqlite3.connect"): "host diagnostic opens mode=ro",
    ("scripts/diag_slow.py", 708, "sqlite3.connect"): "host diagnostic opens mode=ro",
    ("scripts/generate_repository_contract_fixtures.py", 1649, "sqlite3.connect"): (
        "contract fixture generator opens disposable fixture databases"
    ),
    ("scripts/generate_repository_contract_fixtures.py", 1650, "sqlite3.connect"): (
        "contract fixture generator opens disposable fixture databases"
    ),
    ("scripts/verify_repository_snapshot.py", 412, "sqlite3.connect"): (
        "snapshot verifier reads only backup/probe databases"
    ),
    ("scripts/verify_repository_snapshot.py", 413, "sqlite3.connect"): (
        "snapshot verifier reads only backup/probe databases"
    ),
    ("scripts/verify_repository_snapshot.py", 767, "sqlite3.connect"): (
        "snapshot verifier reads only backup/probe databases"
    ),
    ("scripts/verify_repository_snapshot.py", 914, "sqlite3.connect"): (
        "snapshot verifier reads only backup/probe databases"
    ),
    ("scripts/verify_repository_snapshot.py", 916, "sqlite3.connect"): (
        "snapshot verifier reads only backup/probe databases"
    ),
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


def call_name(node: ast.AST) -> str:
    """Return a dotted name for a call/attribute expression when statically known."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _dotted(node: ast.AST) -> str:
    return call_name(node)


def product_sql_sites() -> list[tuple[str, int, str]]:
    """Return every SQL-shaped call outside the canonical SQLite stores.

    This deliberately follows injected connection seats (``db.execute`` and
    ``self._connect().execute``), not just literal ``sqlite3.connect`` text.
    Independent databases/read-only tools are filtered by the test through an
    exact, reasoned site map rather than hidden here.
    """
    hits: list[tuple[str, int, str]] = []
    for path, rel in _production_files():
        if rel.startswith("backend/app/repositories/sqlite/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and call_name(node.func).rsplit(".", 1)[-1]
                in {"execute", "executemany", "executescript"}
            ):
                hits.append((rel, node.lineno, call_name(node.func)))
    return sorted(hits)


def _annotation_name(node: ast.AST | None) -> str:
    if node is None:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return call_name(node)


def _is_repository_annotation(node: ast.AST | None) -> bool:
    name = _annotation_name(node).rsplit(".", 1)[-1].strip("'\"")
    return name.endswith(("Repository", "RepositoryPort")) or name in {
        "AskStreamPort",
        "SQLiteMaintenancePort",
    }


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
                if _is_repository_annotation(arg.annotation):
                    names.add(arg.arg)
    return names


def _repo_bindings(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Find direct names and ``self.<name>`` aliases that retain a repository."""
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


def _is_repo_expression(node: ast.AST, names: set[str], attrs: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in names or bool(REPO_NAME_RE.match(node.id))
    if isinstance(node, ast.Attribute):
        if (
            isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and (node.attr in attrs or bool(REPO_NAME_RE.match(node.attr)))
        ):
            return True
        return False
    if isinstance(node, ast.Call):
        return call_name(node.func).rsplit(".", 1)[-1] in {
            "SQLiteRepository",
            "repository",
        }
    return False


def private_repository_sites() -> list[tuple[str, int, str]]:
    """Return private facade reaches, including aliases and ``self.repo`` fields."""
    hits: list[tuple[str, int, str]] = []
    for path, rel in _production_files():
        if rel == "backend/app/services/sqlite_repository.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        repo_names, repo_attrs = _repo_bindings(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            attr = node.attr
            if not attr.startswith("_") or attr.startswith("__"):
                continue
            if _is_repo_expression(node.value, repo_names, repo_attrs):
                hits.append((rel, node.lineno, attr))
    return sorted(hits)


def _private_member_hits():
    return private_repository_sites()


def test_product_database_sql_is_store_owned():
    assert set(product_sql_sites()) == (
        set(INDEPENDENT_SQL_SITES) | EXPECTED_REMEDIATION_SITES["product_sql"]
    )


def test_routes_and_services_do_not_reach_private_repository_state():
    assert set(private_repository_sites()) == (
        set(INDEPENDENT_PRIVATE_SITES)
        | EXPECTED_REMEDIATION_SITES["private_repository"]
    )


def test_facade_class_imports_only_in_composition_roots():
    actual = set()
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
            if imported:
                actual.add((rel, node.lineno))
    assert actual == set(FACADE_CLASS_IMPORT_SITES)


def test_composition_root_allowlist_stays_exact():
    for rel, _line in sorted(FACADE_CLASS_IMPORT_SITES):
        assert (ROOT / rel).is_file(), f"stale allowlist entry: {rel}"
    assert all(FACADE_CLASS_IMPORT_SITES.values())
    assert all(SQLITE_CONNECT_SITES.values())


def test_no_private_facade_member_access_outside_frozen_seams():
    offenders = [
        (rel, line, attr)
        for rel, line, attr in _private_member_hits()
        if (rel, line, attr) not in INDEPENDENT_PRIVATE_SITES
    ]
    assert set(offenders) == EXPECTED_REMEDIATION_SITES["private_repository"]


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
    actual = set()
    for path, rel in _production_files():
        if rel.startswith("backend/app/repositories/sqlite/"):
            continue  # the database boundary owns its connections
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        actual.update(
            (rel, node.lineno, call_name(node.func))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and call_name(node.func) == "sqlite3.connect"
        )
    assert actual == set(SQLITE_CONNECT_SITES)


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
