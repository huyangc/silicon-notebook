from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.config import Settings
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SCHEMA_VERSION, SqliteMigrator
from app.repositories.postgres.schema_manifest import (
    POSTGRES_BUSINESS_TABLES,
    POSTGRES_BYTEA_COLUMNS,
    POSTGRES_EMPTY_JSON_LIST_SENTINELS,
    POSTGRES_EMPTY_TIME_SENTINELS,
    POSTGRES_JSON_COLUMNS,
    POSTGRES_ROWID_ORDINAL_TABLES,
    POSTGRES_SCHEMA_MANIFEST,
    SQLITE_MIGRATION_INTERNAL_TABLES,
    SQLITE_RETIRED_TABLES,
)
from tests.postgres.conftest import (
    _database_catalog,
    _url_with_search_path,
    _validate_database_catalog,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = REPO_ROOT / "backend" / "tests" / "fixtures" / "postgres_schema_contract.json"
MIGRATIONS_PATH = (
    REPO_ROOT / "backend" / "app" / "repositories" / "postgres" / "migrations"
)
SQLITE_SHADOW_INTERNAL_TABLES = {
    "shadow_change_log",
    "shadow_capture_control",
}

# These are reviewed application-owned JSON values. Keeping the classification
# explicit prevents a new TEXT column from silently becoming jsonb merely
# because its name happens to contain "json".
JSON_COLUMNS = POSTGRES_JSON_COLUMNS
BYTEA_COLUMNS = POSTGRES_BYTEA_COLUMNS
EMPTY_JSON_LIST_SENTINELS = POSTGRES_EMPTY_JSON_LIST_SENTINELS
EMPTY_TIME_SENTINELS = POSTGRES_EMPTY_TIME_SENTINELS

EXPECTED_FTS_ROOTS = {"chunks_fts", "kg_objects_fts", "memory_items_fts"}
EXPECTED_ORDINARY_TABLE_COUNT = 60
EXPECTED_REBUILT_TABLE_COUNT = 17
TEST_SCHEMA_PATTERN = re.compile(r"^sn_test_[0-9a-f]{32}$")

CHECK_CONSTRAINTS = {
    "agent_profiles": {
        "ck_agent_profiles_status": "status IN ('active', 'revoked')",
    },
    "memory_embeddings": {
        "ck_memory_embeddings_dimension": "dimension > 0",
    },
    "memory_items": {
        "ck_memory_items_origin": "origin IN ('ask_answer', 'external_agent')",
        "ck_memory_items_promotion_state": (
            "promotion_state IN ('none', 'proposed', 'approved', 'rejected')"
        ),
        "ck_memory_items_status": (
            "status IN ('candidate', 'confirmed', 'rejected', 'deprecated')"
        ),
    },
    "memory_provenance": {
        "ck_memory_provenance_origin": "origin IN ('ask_answer', 'external_agent')",
    },
    "memory_revisions": {
        "ck_memory_revisions_promotion_state": (
            "promotion_state IN ('none', 'proposed', 'approved', 'rejected')"
        ),
        "ck_memory_revisions_revision": "revision > 0",
        "ck_memory_revisions_status": (
            "status IN ('candidate', 'confirmed', 'rejected', 'deprecated')"
        ),
    },
    "model_service_status": {
        "ck_model_service_status_status": "status IN ('ok', 'error')",
        "ck_model_service_status_trigger": (
            "trigger IN ('manual_test', 'observed_failure')"
        ),
    },
    "system_model_service_status": {
        "ck_system_model_service_status_status": "status IN ('ok', 'error')",
        "ck_system_model_service_status_trigger": (
            "trigger IN ('manual_test', 'observed_failure', 'recovery_probe')"
        ),
    },
    "notebook_bases": {
        "ck_notebook_bases_distinct": "notebook_id <> base_notebook_id",
    },
}

ROWID_ORDER_EVIDENCE = {
    "answers": [
        "app.repositories.sqlite.ask_state_store:AskStateStore.get_conversation",
        "app.repositories.sqlite.ask_state_store:AskStateStore.list_conversations",
    ],
    "chunks": [
        "app.repositories.sqlite.chunk_store:ChunkStore.language_probe_rows",
    ],
    "concept_merge_candidates": [
        "app.repositories.sqlite.governance_store:GovernanceStore.pending_merges_batch",
    ],
    "extraction_runs": [
        "app.repositories.sqlite.maintenance:SQLiteMaintenanceAdapter.count_sources_missing_kg",
        "app.repositories.sqlite.source_store:SourceStore.source_from_row",
        "app.repositories.sqlite.source_store:SourceStore.sources_from_rows",
    ],
    "kg_build_jobs": [
        "app.repositories.sqlite.kg_build_job_store:KgBuildJobStore.latest_on",
    ],
    "knowledge_objects": [
        "app.repositories.sqlite.unified_kg_store:UnifiedKgStore.seed_payload_rows",
        "app.repositories.sqlite.unified_kg_store:UnifiedKgStore.stream_seed_rows",
    ],
    "source_elements": [
        "app.repositories.sqlite.source_store:SourceStore.notebook_element_sample",
    ],
}

# ROWID_ORDER_EVIDENCE above is the curated per-table narrative ("why is this
# table ordinal"), and comparing it against itself proves nothing.  The map
# below is the machine-checkable counterpart: EVERY site in the SQLite
# repository package whose SQL mentions ``rowid``, classified by hand.
#
# A site maps to the FULL tuple of what its rowid SQL touches — one entry per
# site is not enough, because a single method can order two different tables by
# rowid (``get_conversation`` orders both ``answers`` and ``ask_jobs``), and a
# scalar classification silently hides the second one.  The scanner resolves
# each SQL literal's tables independently and the test requires every table it
# can resolve to appear here, so an omission cannot pass.
#
# A business table listed here declares "this table's rowid order is
# observable", which is exactly the property PostgreSQL can only reproduce
# through the ``ordinal`` identity column — so it must be in
# POSTGRES_ROWID_ORDINAL_TABLES or carry a reviewed exception below.  The
# sentinels cover the non-business objects:
#
#   fts-ddl              FTS trigger/DDL text inside a SQLite migration
#   temp-fts             a per-query ``temp.`` FTS scratch table
#   sqlite-keyset-paging SQLite-only keyset pagination over a full scan whose
#                        output is de-duplicated and order-independent (the
#                        PostgreSQL adapter streams a server-side cursor)
ROWID_TOKEN_SITES: dict[str, tuple[str, ...]] = {
    "app.repositories.sqlite.ask_state_store:"
    "AskStateStore.conversation_history": ("answers",),
    "app.repositories.sqlite.ask_state_store:"
    "AskStateStore.get_conversation": ("answers", "ask_jobs"),
    "app.repositories.sqlite.ask_state_store:"
    "AskStateStore.list_conversations": ("answers",),
    "app.repositories.sqlite.chunk_store:ChunkStore.language_probe_rows": ("chunks",),
    "app.repositories.sqlite.index_projection_store:"
    "IndexProjectionStore.embedding_matrix._stream_rows": ("sqlite-keyset-paging",),
    "app.repositories.sqlite.index_projection_store:"
    "IndexProjectionStore.graph_rows": ("chunks", "knowledge_objects"),
    "app.repositories.sqlite.kg_build_job_store:"
    "KgBuildJobStore.latest_on": ("kg_build_jobs",),
    "app.repositories.sqlite.knowledge_counts_cache:"
    "_pending_source_count_query": ("extraction_runs",),
    "app.repositories.sqlite.knowledge_store:"
    "KnowledgeStore.graph_object_rows": ("knowledge_objects",),
    "app.repositories.sqlite.knowledge_store:"
    "KnowledgeStore.source_build_rows": ("extraction_runs",),
    "app.repositories.sqlite.knowledge_store:"
    "KnowledgeStore.source_has_kg": ("extraction_runs",),
    "app.repositories.sqlite.maintenance:"
    "SQLiteMaintenanceAdapter.count_sources_missing_kg": ("extraction_runs",),
    "app.repositories.sqlite.maintenance:"
    "SQLiteMaintenanceAdapter.partial_kg_source_ids": ("extraction_runs",),
    "app.repositories.sqlite.memory_store:MemoryStore.list_memories": ("memory_items",),
    "app.repositories.sqlite.memory_store:"
    "MemoryStore.memory_retrieval_rows": ("memory_items",),
    "app.repositories.sqlite.migrations:SqliteMigrator._migration_13": ("fts-ddl",),
    "app.repositories.sqlite.source_store:"
    "SourceStore.notebook_element_sample": ("source_elements",),
    "app.repositories.sqlite.source_store:"
    "SourceStore.source_from_row": ("extraction_runs",),
    "app.repositories.sqlite.source_store:"
    "SourceStore.sources_from_rows": ("extraction_runs",),
    "app.repositories.sqlite.unified_kg_store:"
    "UnifiedKgStore.claim_name_rows": ("temp-fts",),
    "app.repositories.sqlite.unified_kg_store:"
    "UnifiedKgStore.mention_alias_candidate_batches.batches": ("temp-fts",),
    "app.repositories.sqlite.unified_kg_store:"
    "UnifiedKgStore.mention_scan_matches": ("temp-fts",),
    "app.repositories.sqlite.unified_kg_store:"
    "UnifiedKgStore.scratch_vector_rows": ("kg_cluster_scratch",),
    "app.repositories.sqlite.unified_kg_store:"
    "UnifiedKgStore.seed_payload_rows": ("knowledge_objects",),
    "app.repositories.sqlite.unified_kg_store:"
    "UnifiedKgStore.stream_seed_rows": ("knowledge_objects",),
    "app.repositories.sqlite.unified_kg_store:"
    "UnifiedKgStore.swap_cluster_map_from_scratch": ("kg_cluster_scratch",),
}

ROWID_NON_CONTRACT_SENTINELS = frozenset(
    {"fts-ddl", "temp-fts", "sqlite-keyset-paging"}
)

# Business tables whose rowid a site touches WITHOUT the value being a
# cross-backend ordering contract.  Every entry is a reviewed judgement with
# its reason, not a way to silence the scan: dropping one turns the test red.
ROWID_REVIEWED_NON_ORDINAL: dict[tuple[str, str], str] = {
    (
        "app.repositories.sqlite.ask_state_store:AskStateStore.get_conversation",
        "ask_jobs",
    ): (
        "Defensive tie-break only. SQLite picks the newest running job with "
        "`ORDER BY created_at DESC, rowid DESC LIMIT 1`; PostgreSQL breaks the "
        "same tie with `id COLLATE \"C\" DESC`, which is a random surrogate "
        "key and NOT insertion order. Reaching the tie needs two running jobs "
        "in one conversation whose created_at values are equal at microsecond "
        "precision (repository_facade._now) — rare, not impossible: two "
        "submissions less than a microsecond apart do tie, and then the two "
        "backends can surface different active jobs. This entry records that "
        "as a KNOWN, BOUNDED divergence rather than a proof of impossibility. "
        "Bounded because both rows are legitimate running jobs of the same "
        "conversation, so the only user-visible effect is which one's trace is "
        "restored; no answer, ownership, or durability is affected. Closing it "
        "properly means giving ask_jobs an ordinal column — a PostgreSQL "
        "migration plus a manifest bump, i.e. a schema change that belongs in "
        "its own reviewed PR, not in a test-only guard. Raised by codex review "
        "rounds 2 and 3; round 2's specific argument (that reading `now` "
        "before the write transaction removes the sub-microsecond requirement) "
        "is wrong — the transaction boundary decides which row lands first, "
        "not whether two `datetime.now()` reads return the same microsecond. "
        "The waiver's real dependency is timestamp resolution, which "
        "test_ask_job_tie_waiver_rests_on_microsecond_timestamps pins: at "
        "second resolution an ordinary double submit would tie and this "
        "bounded divergence would stop being rare."
    ),
    (
        "app.repositories.sqlite.memory_store:MemoryStore.list_memories",
        "memory_items",
    ): (
        "FTS docid join (`memory_items_fts f ON f.rowid=m.rowid`), not an "
        "ordering contract. PostgreSQL indexes the base row with GIN and has "
        "no shadow table to join, so there is nothing to reproduce."
    ),
    (
        "app.repositories.sqlite.memory_store:MemoryStore.memory_retrieval_rows",
        "memory_items",
    ): (
        "FTS docid join (`memory_items_fts f ON f.rowid=m.rowid`), not an "
        "ordering contract — same as list_memories above."
    ),
    (
        "app.repositories.sqlite.unified_kg_store:"
        "UnifiedKgStore.scratch_vector_rows",
        "kg_cluster_scratch",
    ): (
        "Rebuild scratch rows are transient and never copied as historical "
        "business state. PostgreSQL reproduces their SQLite insertion order "
        "by joining knowledge_objects and ordering by its preserved ordinal."
    ),
    (
        "app.repositories.sqlite.unified_kg_store:"
        "UnifiedKgStore.swap_cluster_map_from_scratch",
        "kg_cluster_scratch",
    ): (
        "Rebuild scratch rows, repopulated from knowledge_objects every run "
        "and never read across a migration. The PostgreSQL adapter reproduces "
        "the same canonical order by joining knowledge_objects and ordering on "
        "its ordinal instead (postgres/unified_kg_store.py)."
    ),
}

# The one ordering contract carried by SQLite's *implicit* rowid order — the
# query deliberately omits ORDER BY (see the method's docstring), so no scan
# can find it from the SQL text.  Pinned here so the evidence list and the
# scanned sites can be compared without hiding it.
ROWID_IMPLICIT_ORDER_SITES = frozenset(
    {"app.repositories.sqlite.governance_store:GovernanceStore.pending_merges_batch"}
)

SQLITE_REPOSITORY_PACKAGE = (
    REPO_ROOT / "backend" / "app" / "repositories" / "sqlite"
)
_ROWID_TOKEN = re.compile(r"\browid\b", re.I)
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
_SQL_TABLE_REF = re.compile(
    r"\b(?:from|join|update|into)\s+(?:temp\.)?(\{\}|[a-z_][a-z0-9_]*)"
    r"(?:\s+(?:as\s+)?([a-z_][a-z0-9_]*))?",
    re.I,
)
_QUALIFIED_ROWID = re.compile(r"\b([a-z_][a-z0-9_]*)\.rowid\b", re.I)
_BARE_ROWID = re.compile(r"(?<![.\w])rowid\b", re.I)
_SQL_ALIAS_STOPWORDS = frozenset(
    {
        "and", "as", "by", "cross", "from", "group", "having", "inner", "into",
        "join", "left", "limit", "natural", "on", "or", "order", "outer",
        "select", "set", "union", "using", "values", "where",
    }
)


def _string_values(node: ast.AST) -> list[str]:
    """The literal text a node contributes, f-strings included (holes elided)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [
            "".join(
                part.value
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else "{}"
                for part in node.values
            )
        ]
    return []


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Docstrings discuss rowid in prose; they are not SQL."""
    nodes: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ) or not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and _string_values(first.value):
            nodes.add(id(first.value))
    return nodes


def _business_tables_touching_rowid(sql: str) -> set[str]:
    """Business tables this SQL literal uses rowid on.

    Deliberately conservative: a qualified ``alias.rowid`` resolves through the
    literal's own FROM/JOIN aliases, and a bare ``rowid`` is attributed only
    when the literal names exactly one table.  Anything it cannot resolve —
    an alias bound in another literal, an f-string table hole, a temp/FTS
    object — simply drops out instead of being guessed, so the scan never
    invents a table.  Its job is to catch omissions, not to be exhaustive.
    """
    base: list[str] = []
    aliases: dict[str, str] = {}
    for match in _SQL_TABLE_REF.finditer(sql):
        table, alias = match.group(1), match.group(2)
        base.append(table)
        if alias and alias.lower() not in _SQL_ALIAS_STOPWORDS:
            aliases[alias.lower()] = table
    touched = {
        aliases.get(match.group(1).lower(), match.group(1).lower())
        for match in _QUALIFIED_ROWID.finditer(sql)
    }
    if _BARE_ROWID.search(sql) and len(set(base)) == 1:
        touched.add(base[0])
    return {table for table in touched if table in set(POSTGRES_BUSINESS_TABLES)}


def _scan_rowid_sites() -> dict[str, set[str]]:
    sites: dict[str, set[str]] = {}
    for path in sorted(SQLITE_REPOSITORY_PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        skip = _docstring_nodes(tree)
        module = f"app.repositories.sqlite.{path.stem}"

        def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    visit(child, scope + (child.name,))
                    continue
                if id(child) not in skip:
                    for text in _string_values(child):
                        sql = _SQL_LINE_COMMENT.sub("", text)
                        if not _ROWID_TOKEN.search(sql):
                            continue
                        site = f"{module}:{'.'.join(scope) or '<module>'}"
                        sites.setdefault(site, set()).update(
                            _business_tables_touching_rowid(sql)
                        )
                visit(child, scope)

        visit(tree, ())
    return sites


def test_every_rowid_site_is_classified_and_ordinal_backed():
    scanned = _scan_rowid_sites()
    assert set(scanned) == set(ROWID_TOKEN_SITES), (
        "unclassified rowid SQL "
        f"(new={sorted(set(scanned) - set(ROWID_TOKEN_SITES))}, "
        f"stale={sorted(set(ROWID_TOKEN_SITES) - set(scanned))})"
    )
    for site, resolved in sorted(scanned.items()):
        classified_tables = set(ROWID_TOKEN_SITES[site]) - ROWID_NON_CONTRACT_SENTINELS
        if not resolved:
            # Nothing resolvable here (cross-literal alias, f-string table
            # hole, temp/FTS object). The hand classification stands alone.
            continue
        # Equality, not subset: a subset check would let a STALE classification
        # survive after its SQL is deleted, keeping a waiver alive for an
        # ordering the code no longer has (codex review round 4 P2). A site
        # that legitimately mixes a resolvable and an unresolvable business
        # table would fail here — that is intentional, it needs a human.
        assert resolved == classified_tables, (
            f"{site} rowid tables disagree with the classification "
            f"(missing={sorted(resolved - classified_tables)}, "
            f"stale={sorted(classified_tables - resolved)})"
        )
    unbacked = {
        (site, table)
        for site, tables in ROWID_TOKEN_SITES.items()
        for table in tables
        if table not in ROWID_NON_CONTRACT_SENTINELS
        and table not in set(POSTGRES_ROWID_ORDINAL_TABLES)
    }
    assert unbacked == set(ROWID_REVIEWED_NON_ORDINAL), (
        "rowid on a table PostgreSQL cannot order the same way "
        f"(unreviewed={sorted(unbacked - set(ROWID_REVIEWED_NON_ORDINAL))}, "
        f"stale={sorted(set(ROWID_REVIEWED_NON_ORDINAL) - unbacked)})"
    )
    classified = {
        table for tables in ROWID_TOKEN_SITES.values() for table in tables
    } - ROWID_NON_CONTRACT_SENTINELS
    assert classified <= set(POSTGRES_BUSINESS_TABLES)
    evidence = {site for sites in ROWID_ORDER_EVIDENCE.values() for site in sites}
    assert evidence - set(scanned) == ROWID_IMPLICIT_ORDER_SITES
    for table, sites in ROWID_ORDER_EVIDENCE.items():
        for site in sites:
            if site in ROWID_TOKEN_SITES:
                assert table in ROWID_TOKEN_SITES[site]


def test_schema_manifest_pairing_is_pinned_without_a_live_database():
    """The paired versions used to be asserted only inside the PostgreSQL lane,
    so a `check.sh`-green change could ship a manifest that refuses to import
    (the importer and PostgresMigrator both fail closed, but only at runtime).
    """
    from app.repositories.postgres.migrator import load_migrations

    migrations = load_migrations(MIGRATIONS_PATH)
    assert POSTGRES_SCHEMA_MANIFEST.sqlite_version == SCHEMA_VERSION
    assert POSTGRES_SCHEMA_MANIFEST.postgres_version == len(migrations)
    # Counting files is not enough: a duplicated or misnumbered migration keeps
    # the count intact and only fails later, inside _validate_manifest, against
    # a live database (codex review round 2 P2).
    assert [migration.version for migration in migrations] == list(
        range(1, len(migrations) + 1)
    )


def test_report_understanding_stays_in_its_forward_postgres_migration():
    """Keep fresh PostgreSQL column order aligned with SQLite v32.

    SQLite adds ``reports.understanding_json`` with its v32 ALTER TABLE, so
    shadow COPY sees it at the end of the row.  Backfilling the same column
    into PostgreSQL's immutable initial migration moves it ahead of ``depth``
    on fresh databases even though migration 0010 remains idempotent.
    """
    from app.repositories.postgres.migrator import load_migrations

    migrations = load_migrations(MIGRATIONS_PATH)
    assert "understanding_json" not in migrations[0].sql
    report_migration = next(item for item in migrations if item.version == 10)
    assert "ALTER TABLE reports" in report_migration.sql
    assert "understanding_json" in report_migration.sql
    assert migrations[-1].version == 11


def test_ask_job_tie_waiver_rests_on_microsecond_timestamps():
    """Reverse guardrail for the ask_jobs entry in ROWID_REVIEWED_NON_ORDINAL.

    The waiver does not claim the tie is impossible — two submissions inside
    one microsecond do tie.  It claims the tie stays rare and its effect stays
    bounded.  Rarity rests entirely on timestamp resolution: at second
    resolution an ordinary double submit ties and the divergence becomes
    routine.  So pin the resolution itself, both the rendered format and the
    clock behind it.  This is a guardrail on the waiver's premise, not a proof
    that equal timestamps cannot occur.
    """
    from app.services.repository_facade import _now

    samples = [_now() for _ in range(2000)]
    fractions = []
    for stamp in samples:
        rendered = re.search(r"\.(\d+)", stamp)
        assert rendered is not None and len(rendered.group(1)) == 6, (
            "ask_jobs tie waiver assumes microsecond-precision created_at"
        )
        fractions.append(int(rendered.group(1)))
    assert len(set(samples)) > 1, (
        "created_at renders microseconds but the clock behind it never "
        "advances; the ask_jobs tie is now routine"
    )
    # Rendering six digits proves nothing about the clock: a millisecond-
    # quantized source renders .123000 and still ties every sub-millisecond
    # double submit (codex review round 4 P2). Every fraction being a multiple
    # of 1000 is exactly that signature, so require at least one sample to
    # carry a sub-millisecond remainder.
    assert any(value % 1000 for value in fractions), (
        "created_at is quantized to milliseconds or coarser across 2000 "
        "samples; the ask_jobs tie stops being rare and the waiver in "
        "ROWID_REVIEWED_NON_ORDINAL no longer holds"
    )


def _is_time_column(table: str, column: str) -> bool:
    qualified = f"{table}.{column}"
    return column.endswith("_at") or qualified == "knowledge_objects.last_reviewed"


def _sqlite_default(raw: object, *, qualified: str, postgres_type: str) -> Any:
    if raw is None or str(raw).upper() == "NULL":
        return None
    value = str(raw)
    if value.startswith("'") and value.endswith("'"):
        value = value[1:-1].replace("''", "'")
    elif re.fullmatch(r"-?[0-9]+", value):
        return int(value)
    elif re.fullmatch(r"-?(?:[0-9]+\.[0-9]*|[0-9]*\.[0-9]+)", value):
        return float(value)
    if postgres_type == "jsonb":
        if value == "" and qualified in EMPTY_JSON_LIST_SENTINELS:
            return []
        return json.loads(value)
    if qualified in EMPTY_TIME_SENTINELS and value == "":
        return None
    return value


def _postgres_type(table: str, column: str, sqlite_type: str) -> str:
    qualified = f"{table}.{column}"
    if qualified in JSON_COLUMNS:
        return "jsonb"
    if qualified in BYTEA_COLUMNS:
        return "bytea"
    if _is_time_column(table, column):
        return "timestamp with time zone"
    return {
        "TEXT": "text",
        "INTEGER": "bigint",
        "REAL": "double precision",
        "BLOB": "bytea",
    }[sqlite_type.upper()]


def _partial_predicate(index_sql: str | None) -> str | None:
    if not index_sql:
        return None
    match = re.search(r"\bWHERE\b(?P<predicate>.*)$", index_sql, re.IGNORECASE | re.DOTALL)
    if match is None:
        return None
    return " ".join(match.group("predicate").split())


def _canonical_predicate(predicate: str | None) -> str | None:
    if predicate is None:
        return None
    value = re.sub(r"::(?:text|character varying)", "", predicate, flags=re.I)
    value = value.replace("<>", "!=")
    value = re.sub(
        r"([a-z_][a-z0-9_]*)\s*!=\s*ALL\s*\(ARRAY\[(.*?)\]\)",
        lambda match: f"{match.group(1)} NOT IN ({match.group(2)})",
        value,
        flags=re.I,
    )
    # pg_get_expr adds grouping parentheses around simple boolean terms.
    previous = None
    while value != previous:
        previous = value
        value = re.sub(r"\(([^()]+)\)", r"\1", value)
    value = re.sub(r"\s*!=\s*", " != ", value)
    value = re.sub(r"\s*=\s*", " = ", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    return " ".join(value.upper().split())


def _check_expressions(create_sql: str) -> list[str]:
    expressions: list[str] = []
    cursor = 0
    pattern = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)
    while match := pattern.search(create_sql, cursor):
        start = match.end()
        depth = 1
        quote = False
        index = start
        while index < len(create_sql) and depth:
            char = create_sql[index]
            if char == "'":
                if quote and index + 1 < len(create_sql) and create_sql[index + 1] == "'":
                    index += 2
                    continue
                quote = not quote
            elif not quote and char == "(":
                depth += 1
            elif not quote and char == ")":
                depth -= 1
            index += 1
        if depth:
            raise AssertionError("unbalanced SQLite CHECK constraint")
        expressions.append(create_sql[start : index - 1])
        cursor = index
    return expressions


def _canonical_check(expression: str) -> str:
    value = expression.strip()
    if value.upper().startswith("CHECK"):
        match = re.fullmatch(r"CHECK\s*\((.*)\)", value, re.IGNORECASE | re.DOTALL)
        assert match is not None, value
        value = match.group(1)
    value = re.sub(r"::(?:text|character varying|bigint|integer)", "", value, flags=re.I)
    previous = None
    while value != previous:
        previous = value
        value = re.sub(r"^\((.*)\)$", r"\1", value.strip(), flags=re.DOTALL)
    value = re.sub(
        r"([a-z_][a-z0-9_]*)\s*=\s*ANY\s*\(ARRAY\[(.*?)\]\)",
        lambda match: f"{match.group(1)} IN ({match.group(2)})",
        value,
        flags=re.I,
    )
    previous = None
    while value != previous:
        previous = value
        value = re.sub(r"^\((.*)\)$", r"\1", value.strip(), flags=re.DOTALL)
    value = value.replace("!=", "<>")
    value = re.sub(r"\s*<>\s*", " <> ", value)
    value = re.sub(r"\s*=\s*", " = ", value)
    value = re.sub(r"\s*>\s*", " > ", value)
    value = re.sub(r"\s*,\s*", ", ", value)
    return " ".join(value.upper().split())


def _sqlite_explicit_indexes(conn, tables: list[str]) -> list[dict[str, Any]]:
    indexes: list[dict[str, Any]] = []
    for table in tables:
        for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
            name = str(row["name"])
            if name.startswith("sqlite_"):
                continue
            keys = []
            for key in conn.execute(f'PRAGMA index_xinfo("{name}")').fetchall():
                if not bool(key["key"]):
                    continue
                column = key["name"]
                if column is None:
                    if name != "idx_knowhow_cells_column_normalized_anchor_row":
                        raise AssertionError(f"unreviewed SQLite expression index: {name}")
                    column = "__js_trim_content_md__"
                keys.append({"column": str(column), "descending": bool(key["desc"])})
            sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)
            ).fetchone()
            indexes.append(
                {
                    "name": name,
                    "table": table,
                    "unique": bool(row["unique"]),
                    "keys": keys,
                    "predicate": _partial_predicate(str(sql_row["sql"]) if sql_row else None),
                }
            )
    return sorted(indexes, key=lambda item: item["name"])


def _sqlite_schema_contract(conn) -> dict[str, Any]:
    table_rows = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    fts_roots = {
        str(row["name"])
        for row in table_rows
        if str(row["sql"] or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE")
    }
    rebuilt_tables = {
        str(row["name"])
        for row in table_rows
        if row["name"] in fts_roots
        or any(str(row["name"]).startswith(f"{root}_") for root in fts_roots)
    }
    sqlite_internal_tables = {
        str(row["name"])
        for row in table_rows
        if str(row["name"]).startswith("sqlite_")
        or str(row["name"]) in SQLITE_SHADOW_INTERNAL_TABLES
    }
    ordinary_tables = [
        str(row["name"])
        for row in table_rows
        if row["name"] not in rebuilt_tables
        and row["name"] not in sqlite_internal_tables
    ]
    sqlite_checks = {
        str(row["name"]): sorted(
            _canonical_check(expression)
            for expression in _check_expressions(str(row["sql"] or ""))
        )
        for row in table_rows
        if row["name"] in ordinary_tables
        and _check_expressions(str(row["sql"] or ""))
    }

    tables: dict[str, Any] = {}
    for table in ordinary_tables:
        columns = []
        primary_key_parts: list[tuple[int, str]] = []
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
            column = str(row["name"])
            qualified = f"{table}.{column}"
            pk_position = int(row["pk"])
            postgres_type = _postgres_type(table, column, str(row["type"]))
            if pk_position:
                primary_key_parts.append((pk_position, column))
            columns.append(
                {
                    "name": column,
                    "sqlite_type": str(row["type"]).upper(),
                    "postgres_type": postgres_type,
                    "nullable": (
                        qualified in EMPTY_TIME_SENTINELS
                        or not bool(row["notnull"] or pk_position)
                    ),
                    "sqlite_not_null": bool(row["notnull"]),
                    "default": _sqlite_default(
                        row["dflt_value"],
                        qualified=qualified,
                        postgres_type=postgres_type,
                    ),
                }
            )

        foreign_keys = [
            {
                "columns": [str(row["from"])],
                "references_table": str(row["table"]),
                "references_columns": [str(row["to"])],
                "on_update": str(row["on_update"]).upper(),
                "on_delete": str(row["on_delete"]).upper(),
            }
            for row in conn.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        ]
        foreign_keys.sort(
            key=lambda item: (
                item["columns"],
                item["references_table"],
                item["references_columns"],
            )
        )

        unique_keys = []
        for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
            if not bool(row["unique"]) or str(row["origin"]) == "pk":
                continue
            index_name = str(row["name"])
            index_columns = [
                str(info["name"])
                for info in conn.execute(f'PRAGMA index_info("{index_name}")').fetchall()
            ]
            index_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (index_name,),
            ).fetchone()
            unique_keys.append(
                {
                    "columns": index_columns,
                    "predicate": _partial_predicate(
                        str(index_sql_row["sql"])
                        if index_sql_row is not None and index_sql_row["sql"] is not None
                        else None
                    ),
                }
            )
        unique_keys.sort(key=lambda item: (item["columns"], item["predicate"] or ""))

        tables[table] = {
            "classification": "replicated",
            "columns": columns,
            "primary_key": [name for _position, name in sorted(primary_key_parts)],
            "unique_keys": unique_keys,
            "foreign_keys": foreign_keys,
        }

    return {
        "sqlite_version": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        "sqlite_table_count": len(table_rows),
        "sqlite_internal_tables": sorted(sqlite_internal_tables),
        "postgres_version": POSTGRES_SCHEMA_MANIFEST.postgres_version,
        "ordinary_table_count": len(ordinary_tables),
        "rebuilt_table_count": len(rebuilt_tables),
        "rebuilt": {
            "fts_virtual_roots": sorted(fts_roots),
            "fts_internal_tables": sorted(rebuilt_tables - fts_roots),
        },
        "postgres_internal_tables": ["silicon_schema_migrations"],
        "postgres_extensions": ["pg_trgm"],
        "postgres_extension_schemas": {"pg_trgm": "public"},
        "business_check_constraints": {
            table: {
                name: _canonical_check(expression)
                for name, expression in constraints.items()
            }
            for table, constraints in CHECK_CONSTRAINTS.items()
        },
        "sqlite_check_expressions": sqlite_checks,
        "sqlite_explicit_indexes": _sqlite_explicit_indexes(conn, ordinary_tables),
        "ordinal_tables": sorted(ROWID_ORDER_EVIDENCE),
        "ordinal_evidence": ROWID_ORDER_EVIDENCE,
        "normalizations": {
            "sqlite_empty_json_array_to_postgres_array": sorted(
                EMPTY_JSON_LIST_SENTINELS
            ),
            "sqlite_empty_time_to_postgres_null": sorted(EMPTY_TIME_SENTINELS),
            "postgres_null_time_to_domain_empty": sorted(EMPTY_TIME_SENTINELS),
            "sqlite_rowid_to_postgres_ordinal": sorted(ROWID_ORDER_EVIDENCE),
            "postgres_ordinal_sequence_reseed_after_copy": sorted(
                ROWID_ORDER_EVIDENCE
            ),
        },
        "tables": tables,
    }


def _fresh_sqlite_contract(tmp_path: Path) -> dict[str, Any]:
    db_path = tmp_path / "fresh-v33.db"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    database = SqliteDatabase(settings, REPO_ROOT)
    try:
        assert SqliteMigrator(database, settings).migrate() == list(
            range(1, SCHEMA_VERSION + 1)
        )
        return _sqlite_schema_contract(database.connect())
    finally:
        database.close_local()


def _reviewed_contract() -> dict[str, Any]:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_fresh_sqlite_v33_matches_reviewed_postgres_contract(tmp_path):
    actual = _fresh_sqlite_contract(tmp_path)
    if os.environ.get("UPDATE_POSTGRES_SCHEMA_CONTRACT") == "1":
        CONTRACT_PATH.write_text(
            json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    expected = _reviewed_contract()
    assert actual == expected
    assert actual["ordinary_table_count"] == EXPECTED_ORDINARY_TABLE_COUNT
    assert actual["rebuilt_table_count"] == EXPECTED_REBUILT_TABLE_COUNT
    assert set(actual["rebuilt"]["fts_virtual_roots"]) == EXPECTED_FTS_ROOTS
    expected_check_expressions = {
        table: sorted(_canonical_check(expression) for expression in values.values())
        for table, values in CHECK_CONSTRAINTS.items()
    }
    assert actual["sqlite_check_expressions"] == expected_check_expressions
    assert len(actual["sqlite_explicit_indexes"]) == 86
    assert set(actual["ordinal_tables"]) == set(ROWID_ORDER_EVIDENCE)
    assert actual["sqlite_table_count"] == (
        actual["ordinary_table_count"]
        + actual["rebuilt_table_count"]
        + len(actual["sqlite_internal_tables"])
    )
    assert set(actual["tables"]) == {
        name for name, facts in expected["tables"].items() if facts["classification"] == "replicated"
    }


def _postgres_schema_facts(conn) -> dict[str, Any]:
    tables = [
        str(row["table_name"])
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_type='BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
    ]
    columns: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT table_name, column_name, data_type, is_nullable, column_default, "
        "collation_name, "
        "ordinal_position FROM information_schema.columns "
        "WHERE table_schema=current_schema() ORDER BY table_name, ordinal_position"
    ).fetchall():
        columns.setdefault(str(row["table_name"]), []).append(dict(row))

    primary_keys: dict[str, list[str]] = {}
    foreign_keys: dict[str, list[dict[str, Any]]] = {}
    constraints = conn.execute(
        "SELECT c.conname, c.contype, child.relname AS table_name, "
        "parent.relname AS references_table, c.confupdtype, c.confdeltype, "
        "ARRAY(SELECT a.attname FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord) "
        "      JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum "
        "      ORDER BY k.ord) AS columns, "
        "ARRAY(SELECT a.attname FROM unnest(c.confkey) WITH ORDINALITY k(attnum, ord) "
        "      JOIN pg_attribute a ON a.attrelid=c.confrelid AND a.attnum=k.attnum "
        "      ORDER BY k.ord) AS references_columns "
        "FROM pg_constraint c "
        "JOIN pg_class child ON child.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=child.relnamespace "
        "LEFT JOIN pg_class parent ON parent.oid=c.confrelid "
        "WHERE n.nspname=current_schema() AND c.contype IN ('p','f') "
        "ORDER BY child.relname, c.conname"
    ).fetchall()
    action = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
    for row in constraints:
        table = str(row["table_name"])
        if row["contype"] == "p":
            primary_keys[table] = list(row["columns"])
        else:
            foreign_keys.setdefault(table, []).append(
                {
                    "columns": list(row["columns"]),
                    "references_table": str(row["references_table"]),
                    "references_columns": list(row["references_columns"]),
                    "on_update": action[str(row["confupdtype"])],
                    "on_delete": action[str(row["confdeltype"])],
                }
            )
    for values in foreign_keys.values():
        values.sort(
            key=lambda item: (
                item["columns"], item["references_table"], item["references_columns"]
            )
        )

    unique_keys: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(
        "SELECT table_name, columns, predicate FROM ("
        " SELECT t.relname AS table_name, i.indexrelid, "
        " ARRAY(SELECT a.attname FROM unnest(i.indkey) WITH ORDINALITY k(attnum, ord) "
        "       JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum "
        "       WHERE k.attnum > 0 ORDER BY k.ord) AS columns, "
        " pg_get_expr(i.indpred, i.indrelid) AS predicate "
        " FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid "
        " JOIN pg_namespace n ON n.oid=t.relnamespace "
        " WHERE n.nspname=current_schema() AND i.indisunique AND NOT i.indisprimary"
        ") q ORDER BY table_name, columns::text, predicate"
    ).fetchall():
        unique_keys.setdefault(str(row["table_name"]), []).append(
            {
                "columns": list(row["columns"]),
                "predicate": (
                    " ".join(str(row["predicate"]).split())
                    if row["predicate"] is not None
                    else None
                ),
            }
        )

    return {
        "tables": tables,
        "columns": columns,
        "primary_keys": primary_keys,
        "foreign_keys": foreign_keys,
        "unique_keys": unique_keys,
    }


def _postgres_explicit_indexes(conn) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT idx.relname AS name, tbl.relname AS table_name, i.indisunique, "
        "pg_get_expr(i.indpred, i.indrelid) AS predicate, "
        "ARRAY(SELECT pg_get_indexdef(i.indexrelid, position, true) "
        "      FROM generate_series(1, i.indnkeyatts) AS position "
        "      ORDER BY position) AS keys, "
        "ARRAY(SELECT c.collname FROM unnest(i.indcollation) WITH ORDINALITY x(collation_oid, position) "
        "      LEFT JOIN pg_collation c ON c.oid=x.collation_oid "
        "      ORDER BY position) AS collations, "
        "i.indoption::smallint[] AS options "
        "FROM pg_index i JOIN pg_class idx ON idx.oid=i.indexrelid "
        "JOIN pg_class tbl ON tbl.oid=i.indrelid "
        "JOIN pg_namespace n ON n.oid=tbl.relnamespace "
        "WHERE n.nspname=current_schema() ORDER BY idx.relname"
    ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["name"])
        keys = []
        for position, (key, option) in enumerate(
            zip(row["keys"], row["options"], strict=True)
        ):
            if name == "idx_knowhow_cells_column_normalized_anchor_row" and position == 1:
                column = "__js_trim_content_md__"
            else:
                column = re.sub(r"\s+(?:ASC|DESC)(?:\s+NULLS\s+(?:FIRST|LAST))?$", "", str(key), flags=re.I)
                column = column.strip('"')
            keys.append({"column": column, "descending": bool(int(option) & 1)})
        result[name] = {
            "name": name,
            "table": str(row["table_name"]),
            "unique": bool(row["indisunique"]),
            "keys": keys,
            "collations": list(row["collations"]),
            "predicate": (
                " ".join(str(row["predicate"]).split())
                if row["predicate"] is not None
                else None
            ),
        }
    return result


def _normalize_postgres_default(raw: object, postgres_type: str) -> Any:
    if raw is None:
        return None
    value = str(raw)
    if postgres_type == "jsonb":
        match = re.fullmatch(r"'(.*)'::jsonb", value, re.DOTALL)
        assert match is not None, value
        return json.loads(match.group(1).replace("''", "'"))
    if postgres_type == "text":
        match = re.fullmatch(r"'(.*)'::text", value, re.DOTALL)
        assert match is not None, value
        return match.group(1).replace("''", "'")
    if postgres_type == "bigint":
        match = re.fullmatch(r"'?(-?[0-9]+)'?(?:::(?:smallint|integer|bigint))?", value)
        assert match is not None, value
        return int(match.group(1))
    if postgres_type == "double precision":
        return float(re.sub(r"::double precision$", "", value))
    raise AssertionError(f"unexpected default {value!r} for {postgres_type}")


def _assert_packaged_postgres_schema_has_bidirectional_semantic_parity(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator

    contract = _reviewed_contract()
    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate() == contract["postgres_version"]
    with postgres_database.connect() as conn:
        facts = _postgres_schema_facts(conn)
        server_encoding = conn.execute(
            "SELECT current_setting('server_encoding') AS value"
        ).fetchone()["value"]

    assert server_encoding == "UTF8"
    expected_business = set(contract["tables"])
    expected_internal = set(contract["postgres_internal_tables"])
    assert set(facts["tables"]) == expected_business | expected_internal
    assert expected_business.isdisjoint(expected_internal)

    for table, expected in contract["tables"].items():
        actual_columns = facts["columns"][table]
        expected_ordinal = table in set(contract["ordinal_tables"])
        assert [row["column_name"] for row in actual_columns] == [
            column["name"] for column in expected["columns"]
        ] + (["ordinal"] if expected_ordinal else [])
        for actual, column in zip(
            actual_columns[: len(expected["columns"])], expected["columns"], strict=True
        ):
            assert actual["data_type"] == column["postgres_type"], (table, column["name"])
            assert actual["collation_name"] == (
                "C" if column["postgres_type"] == "text" else None
            ), (table, column["name"], actual["collation_name"])
            assert (actual["is_nullable"] == "YES") == column["nullable"], (
                table,
                column["name"],
            )
            assert _normalize_postgres_default(
                actual["column_default"], column["postgres_type"]
            ) == column["default"], (table, column["name"])
        assert facts["primary_keys"].get(table, []) == expected["primary_key"]
        assert facts["foreign_keys"].get(table, []) == expected["foreign_keys"]
        actual_unique = [
            {**item, "predicate": _canonical_predicate(item["predicate"])}
            for item in facts["unique_keys"].get(table, [])
        ]
        expected_unique = [
            {**item, "predicate": _canonical_predicate(item["predicate"])}
            for item in expected["unique_keys"]
        ]
        ordinal_unique = [
            item for item in actual_unique if item["columns"] == ["ordinal"]
        ]
        actual_unique = [
            item for item in actual_unique if item["columns"] != ["ordinal"]
        ]
        assert ordinal_unique == (
            [{"columns": ["ordinal"], "predicate": None}]
            if expected_ordinal
            else []
        )
        assert actual_unique == expected_unique

    with postgres_database.connect() as conn:
        extension_schemas = {
            str(row["extname"]): str(row["nspname"])
            for row in conn.execute(
                "SELECT e.extname, n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname='pg_trgm'"
            ).fetchall()
        }
        vector_extension = conn.execute(
            "SELECT 1 FROM pg_extension WHERE extname='vector'"
        ).fetchone()
        ordinal_columns = conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable, is_identity, "
            "identity_generation FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND is_identity='YES'"
        ).fetchall()
        business_constraints = conn.execute(
            "SELECT child.relname AS table_name, c.conname, c.contype, c.condeferrable, "
            "pg_get_constraintdef(c.oid, true) AS definition "
            "FROM pg_constraint c JOIN pg_class child ON child.oid=c.conrelid "
            "JOIN pg_namespace n ON n.oid=child.relnamespace "
            "WHERE n.nspname=current_schema() AND child.relname <> 'silicon_schema_migrations' "
            "ORDER BY child.relname, c.conname"
        ).fetchall()
        index_rows = conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes "
            "WHERE schemaname=current_schema() ORDER BY indexname"
        ).fetchall()
        explicit_indexes = _postgres_explicit_indexes(conn)
    assert set(extension_schemas) == set(contract["postgres_extensions"])
    assert extension_schemas == contract["postgres_extension_schemas"]
    assert vector_extension is None
    assert {
        str(row["table_name"]): {
            "column": str(row["column_name"]),
            "type": str(row["data_type"]),
            "nullable": str(row["is_nullable"]),
            "identity": str(row["is_identity"]),
            "generation": str(row["identity_generation"]),
        }
        for row in ordinal_columns
    } == {
        table: {
            "column": "ordinal",
            "type": "bigint",
            "nullable": "NO",
            "identity": "YES",
            "generation": "BY DEFAULT",
        }
        for table in contract["ordinal_tables"]
    }
    assert all(
        str(row["conname"]).startswith(("pk_", "fk_", "uq_", "ck_"))
        for row in business_constraints
    )
    assert not any(bool(row["condeferrable"]) for row in business_constraints)
    actual_checks: dict[str, dict[str, str]] = {}
    for row in business_constraints:
        if row["contype"] == "c":
            actual_checks.setdefault(str(row["table_name"]), {})[
                str(row["conname"])
            ] = _canonical_check(str(row["definition"]))
    assert actual_checks == contract["business_check_constraints"]
    index_definitions = {str(row["indexname"]): str(row["indexdef"]) for row in index_rows}
    for name in (
        "idx_chunks_text_trgm",
        "idx_knowledge_objects_name_trgm",
        "idx_memory_items_title_trgm",
        "idx_memory_items_content_md_trgm",
        "idx_memory_items_tags_trgm",
    ):
        assert "USING gin" in index_definitions[name]
        assert "gin_trgm_ops" in index_definitions[name]
    assert len(contract["sqlite_explicit_indexes"]) == 86
    for expected_index in contract["sqlite_explicit_indexes"]:
        actual_index = explicit_indexes[expected_index["name"]]
        assert actual_index["table"] == expected_index["table"]
        assert actual_index["unique"] == expected_index["unique"]
        assert actual_index["keys"] == expected_index["keys"]
        assert _canonical_predicate(actual_index["predicate"]) == _canonical_predicate(
            expected_index["predicate"]
        )
    assert all(
        collation in (None, "C")
        for index in explicit_indexes.values()
        for collation in index["collations"]
    )
    for name in (
        "idx_knowhow_cells_column_normalized_anchor_row",
        "idx_knowledge_objects_name_trgm",
        "idx_memory_items_tags_trgm",
    ):
        assert "C" in explicit_indexes[name]["collations"], name


@pytest.mark.postgres_integration
def test_packaged_postgres_schema_has_bidirectional_semantic_parity(postgres_database):
    _assert_packaged_postgres_schema_has_bidirectional_semantic_parity(
        postgres_database
    )


@pytest.mark.postgres_integration
def test_schema_parity_on_utf8_database_with_non_c_default_collation(
    postgres_non_c_database,
):
    with postgres_non_c_database.connect() as conn:
        row = conn.execute(
            "SELECT current_database() AS database, "
            "current_setting('server_encoding') AS encoding, "
            "to_jsonb(d) AS catalog FROM pg_database AS d "
            "WHERE datname=current_database()"
        ).fetchone()
    catalog = _database_catalog(row)
    _validate_database_catalog(catalog, expected="non-c")
    assert catalog.encoding == "UTF8"
    assert catalog.provider == "i"
    assert catalog.provider_locale == "en-US"
    _assert_packaged_postgres_schema_has_bidirectional_semantic_parity(
        postgres_non_c_database
    )


@pytest.mark.postgres_integration
def test_packaged_migrations_are_idempotent_from_empty_schema(postgres_database):
    from app.repositories.postgres.migrator import PostgresMigrator
    from app.repositories.postgres.schema_manifest import POSTGRES_SCHEMA_MANIFEST

    migrator = PostgresMigrator(postgres_database)
    assert migrator.current_version() == 0
    assert migrator.migrate() == 11
    assert migrator.migrate() == 11
    assert migrator.current_version() == 11
    assert POSTGRES_SCHEMA_MANIFEST.sqlite_version == 33
    assert POSTGRES_SCHEMA_MANIFEST.postgres_version == 11


@pytest.mark.postgres_integration
def test_packaged_migration_checksum_drift_is_rejected(postgres_database, tmp_path):
    from app.repositories.postgres.migrator import PostgresMigrator, load_migrations

    migrator = PostgresMigrator(postgres_database)
    assert migrator.migrate() == 11

    copied = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_PATH, copied)
    first = copied / "0001_initial.sql"
    first.write_text(first.read_text(encoding="utf-8") + "\n-- drift\n", encoding="utf-8")
    changed = PostgresMigrator(postgres_database, migrations=load_migrations(copied))
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        changed.migrate()


@pytest.mark.postgres_integration
def test_pg_trgm_is_shared_outside_disposable_schema_lifetimes(postgres_scope):
    import psycopg
    from psycopg import sql

    from app.repositories.postgres.database import PostgresDatabase
    from app.repositories.postgres.migrator import PostgresMigrator

    schemas = [f"sn_test_{uuid.uuid4().hex}" for _ in range(2)]
    assert all(TEST_SCHEMA_PATTERN.fullmatch(schema) for schema in schemas)
    databases = []
    with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
        for schema in schemas:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        for schema in schemas:
            settings = Settings(
                database_url=_url_with_search_path(postgres_scope.base_url, schema),
                postgres_pool_min_size=1,
                postgres_pool_max_size=1,
            )
            databases.append(PostgresDatabase(settings, REPO_ROOT))

        barrier = threading.Barrier(2)
        versions: list[int] = []
        failures: list[BaseException] = []

        def migrate(database) -> None:
            try:
                barrier.wait(timeout=5)
                versions.append(PostgresMigrator(database).migrate())
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        workers = [
            threading.Thread(target=migrate, args=(database,)) for database in databases
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=20)
        assert not any(worker.is_alive() for worker in workers)
        assert failures == []
        assert sorted(versions) == [
            POSTGRES_SCHEMA_MANIFEST.postgres_version,
            POSTGRES_SCHEMA_MANIFEST.postgres_version,
        ]

        with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
            extension_schema = conn.execute(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname='pg_trgm'"
            ).fetchone()[0]
            assert extension_schema == "public"
            conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schemas[0]))
            )

        with databases[1].connect() as conn:
            remaining = conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname=current_schema() "
                "AND indexname='idx_chunks_text_trgm'"
            ).fetchone()
            extension_schema = conn.execute(
                "SELECT n.nspname FROM pg_extension e "
                "JOIN pg_namespace n ON n.oid=e.extnamespace "
                "WHERE e.extname='pg_trgm'"
            ).fetchone()["nspname"]
        assert remaining == {"indexname": "idx_chunks_text_trgm"}
        assert extension_schema == "public"
        assert PostgresMigrator(databases[1]).migrate() == 11
    finally:
        for database in databases:
            database.close()
        with psycopg.connect(postgres_scope.base_url, autocommit=True) as conn:
            for schema in schemas:
                if TEST_SCHEMA_PATTERN.fullmatch(schema) is None:
                    raise RuntimeError("refusing to drop an unvalidated PostgreSQL schema")
                exists = conn.execute(
                    "SELECT 1 FROM pg_namespace WHERE nspname=%s", (schema,)
                ).fetchone()
                if exists is not None:
                    conn.execute(
                        sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                    )


def test_reviewed_json_and_binary_mappings_cover_only_real_columns(tmp_path):
    from app.repositories.postgres.schema_manifest import POSTGRES_ROWID_ORDINAL_TABLES

    contract = _fresh_sqlite_contract(tmp_path)
    qualified = {
        f"{table}.{column['name']}"
        for table, facts in contract["tables"].items()
        for column in facts["columns"]
    }
    assert JSON_COLUMNS <= qualified
    assert BYTEA_COLUMNS <= qualified
    assert EMPTY_JSON_LIST_SENTINELS <= qualified
    assert EMPTY_TIME_SENTINELS <= qualified
    assert set(POSTGRES_ROWID_ORDINAL_TABLES) == set(ROWID_ORDER_EVIDENCE)
    assert set(POSTGRES_BUSINESS_TABLES) == set(contract["tables"])
    assert set(SQLITE_RETIRED_TABLES).isdisjoint(POSTGRES_BUSINESS_TABLES)
    assert set(SQLITE_MIGRATION_INTERNAL_TABLES).isdisjoint(POSTGRES_BUSINESS_TABLES)
    assert contract["ordinal_evidence"] == ROWID_ORDER_EVIDENCE
    fingerprint = hashlib.sha256(
        "\n".join(sorted(contract["tables"])).encode("utf-8")
    ).hexdigest()
    assert fingerprint == "87b641597f5fd2d5fab4b235f9c0d1c00a203d4ba1063a96b8943813967d35fc"


def test_packaged_index_migration_phases_are_exact():
    from app.repositories.postgres.migrator import load_migrations

    migrations = {migration.version: migration for migration in load_migrations(MIGRATIONS_PATH)}
    assert [(version, migrations[version].name) for version in migrations] == [
        (1, "initial"),
        (2, "integrity_indexes"),
        (3, "core_indexes"),
        (4, "knowledge_indexes"),
        (5, "memory_knowhow_governance_indexes"),
        (6, "search_gin"),
        (7, "cluster_membership_unique"),
        (8, "master_v28_features"),
        (9, "sources_file_hash_index"),
        (10, "report_understanding"),
        (11, "relation_endpoint_keyset_indexes"),
    ]

    def index_declarations(version: int) -> list[tuple[bool, str]]:
        return [
            (bool(unique), name)
            for unique, name in re.findall(
                r"(?mi)^CREATE\s+(UNIQUE\s+)?INDEX\s+([a-z0-9_]+)",
                migrations[version].sql,
            )
        ]

    integrity_names = {
        "idx_kg_build_jobs_one_running",
        "idx_memory_answer_once",
        "idx_notebooks_share_token",
        "idx_promotion_object",
        "idx_sources_memory_id",
        "idx_users_username",
    }
    assert index_declarations(1) == []
    assert index_declarations(2) == [
        (True, name)
        for name in (
            "idx_kg_build_jobs_one_running",
            "idx_memory_answer_once",
            "idx_notebooks_share_token",
            "idx_promotion_object",
            "idx_sources_memory_id",
            "idx_users_username",
        )
    ]
    operational = [
        declaration
        for version in (3, 4, 5, 8)
        for declaration in index_declarations(version)
    ]
    assert len(operational) == 76
    assert not any(unique for unique, _name in operational)
    gin_names = {
        "idx_chunks_text_trgm",
        "idx_knowledge_objects_name_trgm",
        "idx_memory_items_title_trgm",
        "idx_memory_items_content_md_trgm",
        "idx_memory_items_tags_trgm",
    }
    gin_declarations = index_declarations(6)
    assert len(gin_declarations) == 5
    assert not any(unique for unique, _name in gin_declarations)
    assert {name for _unique, name in gin_declarations} == gin_names
    assert all("USING gin" in line for line in re.findall(
        r"(?mis)^CREATE INDEX idx_.*?;", migrations[6].sql
    ))
    cluster_unique = index_declarations(7)
    assert cluster_unique == [(True, "uq_clusters_notebook_type_member")]

    # 0008_master_v28_features packs the SQLite v24–v28 reconciliation indexes
    # (canonical scratch + knowhow change/milestone history). They mirror the
    # SQLite contract one-for-one, so they must be counted toward the parity
    # assertion below or `packaged_names` would fall short by exactly these three.
    v28_feature_indexes = index_declarations(8)
    assert v28_feature_indexes == [
        (False, "idx_kg_canonical_scratch_nb_run_seed"),
        (False, "idx_knowhow_changes_table"),
        (False, "idx_knowhow_milestones_table"),
    ]

    # 0009_sources_file_hash_index mirrors SQLite v30's sources(notebook_id,
    # file_hash) dedup lookup index — the first SQLite index added after the
    # PostgreSQL adapter landed, so it establishes the "one SQLite index ->
    # one packaged PostgreSQL migration" pattern rather than SQLite-only drift.
    v30_index = index_declarations(9)
    assert v30_index == [(False, "idx_sources_notebook_file_hash")]

    # PostgreSQL v10 is reserved for SQLite v32's report-understanding column;
    # the relation endpoint indexes pair with SQLite v33 in PostgreSQL v11.
    v33_indexes = index_declarations(11)
    assert v33_indexes == [
        (False, "idx_knowledge_relations_nb_source_id"),
        (False, "idx_knowledge_relations_nb_target_id"),
    ]

    contract_names = {item["name"] for item in _reviewed_contract()["sqlite_explicit_indexes"]}
    packaged_names = (
        integrity_names
        | {name for _unique, name in operational}
        | gin_names
        | {name for _unique, name in cluster_unique}
        | {name for _unique, name in v28_feature_indexes}
        | {name for _unique, name in v30_index}
        | {name for _unique, name in v33_indexes}
    )
    assert packaged_names == contract_names | gin_names


def test_initial_migration_guards_utf8_before_business_ddl():
    from app.repositories.postgres.migrator import load_migrations

    initial = load_migrations(MIGRATIONS_PATH)[0].sql
    guard_position = initial.index("current_setting('server_encoding')")
    first_business_ddl = initial.index("CREATE TABLE agent_access_tokens")
    assert guard_position < first_business_ddl
    assert "server_encoding must be UTF8" in initial
