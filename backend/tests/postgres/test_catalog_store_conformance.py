"""PostgreSQL conformance for the C1b review-fix additions to the command-
catalog and knowhow stores (方案 C·C1b, review round).

Scope is deliberately narrow: the SQLite side already has full behavioural
coverage in ``tests/test_command_catalog_job.py`` (identical fixtures,
identical assertions, same service layer). This file only proves the things
that are genuinely backend-specific — the migration actually adds the two new
``catalog_jobs`` columns on PostgreSQL, and the bounded ``KnowhowStorePort``
methods (``knowhow_table_columns``, ``knowhow_anchor_existing_values``,
``knowhow_table_id_by_title``) behave the same way against real PostgreSQL
SQL (``btrim`` instead of SQLite's ``trim``, ``= ANY(%s)`` instead of
``IN (...)``, ``COLLATE "C"`` instead of SQLite's default binary collation)
as they do against SQLite.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from app.repositories.postgres.catalog_store import CatalogStore as PostgresCatalogStore
from app.repositories.postgres.knowhow_store import KnowhowStore as PostgresKnowhowStore
from app.services.repository_runtime import RepositoryCompatibilitySeams


NOW = "2026-07-31T00:00:00+00:00"


pytestmark = pytest.mark.postgres_integration


def _seams() -> RepositoryCompatibilitySeams:
    lock = threading.Lock()
    counter: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        with lock:
            counter[prefix] = counter.get(prefix, 0) + 1
            return f"{prefix}-catalog-{counter[prefix]:04d}"

    return RepositoryCompatibilitySeams(
        new_id=new_id,
        now=lambda: NOW,
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )


def _seed(database, *, notebook_id: str, source_id: str) -> None:
    mark = "%s"
    with database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            f"VALUES ({','.join([mark] * 11)})",
            (
                "user-catalog", "catalog@example.test", "Catalog", "admin", "active",
                NOW, NOW, "c00654321", "", "", 0,
            ),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) "
            f"VALUES ({','.join([mark] * 9)})",
            (
                notebook_id, "Catalog", "", "engineering", "ready", "user-catalog",
                NOW, NOW, "personal",
            ),
        )
        connection.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) "
            f"VALUES ({','.join([mark] * 8)})",
            (source_id, notebook_id, "OpenROAD 手册", "markdown", "extracted",
             "extracted", NOW, NOW),
        )


@dataclass
class CatalogHarness:
    database: object
    catalog: PostgresCatalogStore
    knowhow: PostgresKnowhowStore
    notebook_id: str
    source_id: str


@pytest.fixture
def catalog_harness(request) -> CatalogHarness:
    seams = _seams()
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 17
    notebook_id, source_id = "nb-catalog", "src-catalog"
    _seed(database, notebook_id=notebook_id, source_id=source_id)
    yield CatalogHarness(
        database=database,
        catalog=PostgresCatalogStore(database, new_id=seams.new_id, now=seams.now),
        knowhow=PostgresKnowhowStore(database, new_id=seams.new_id, now=seams.now),
        notebook_id=notebook_id,
        source_id=source_id,
    )


def test_catalog_jobs_gains_truncated_sections_and_applied_table_id_columns(
    catalog_harness,
):
    """The migration 0016 review-fix columns exist, default correctly, and
    round-trip through the store's own read/write methods — not just a raw
    `information_schema` probe."""
    harness = catalog_harness
    job = harness.catalog.create_job(harness.notebook_id, harness.source_id, "user-catalog")
    assert job["truncated_sections"] == 0
    assert job["applied_table_id"] == ""
    # R8: the third pre-release column, defaulting to '' when the caller does
    # not snapshot a generation.
    assert job["source_generation"] == ""

    # `record_section` is `running`-scoped (see the SQLite mirror's own
    # docstring): a call before `start_job` claims the row is a no-op.
    assert harness.catalog.record_section(
        job["id"], entries=1, rejected=0, uncovered=0, truncated=1
    ) is False
    assert harness.catalog.get_job(job["id"])["truncated_sections"] == 0

    assert harness.catalog.start_job(job["id"], 1) is True
    assert harness.catalog.record_section(
        job["id"], entries=1, rejected=0, uncovered=0, truncated=1
    ) is True
    assert harness.catalog.get_job(job["id"])["truncated_sections"] == 1

    assert harness.catalog.set_applied_table_id(job["id"], "khtbl-example") is True
    assert harness.catalog.get_job(job["id"])["applied_table_id"] == "khtbl-example"


def test_mark_candidates_dismissed_converges_pending_past_a_conflicting_page(
    catalog_harness,
):
    """Store-level mirror of the SQLite convergence fix: a conflict candidate
    must leave `state='candidate'` (into `dismissed`, carrying why) or the
    `pending_candidates` cursor=0 keyset read would return the same page
    forever. Full apply()-level behaviour is exhaustively covered by the
    SQLite service tests (identical service layer); this only proves the new
    ``CatalogStorePort`` method behaves the same against real PostgreSQL SQL
    (``id=ANY(%s)`` instead of SQLite's ``IN (...)``, jsonb instead of JSON
    text)."""
    harness = catalog_harness
    job = harness.catalog.create_job(harness.notebook_id, harness.source_id, "user-catalog")
    harness.catalog.add_candidates(
        [
            {
                "job_id": job["id"],
                "notebook_id": harness.notebook_id,
                "source_id": harness.source_id,
                "position": i + 1,
                "section_path": f"cmd_{i}",
                "command_name": f"cmd_{i}",
                "payload": {},
                "state": "candidate",
            }
            for i in range(3)
        ]
    )
    conflicts = harness.catalog.pending_candidates(job["id"], limit=10)
    assert len(conflicts) == 3

    dismissed_count = harness.catalog.mark_candidates_dismissed(
        job["id"],
        [row["id"] for row in conflicts],
        reject_info={"reason": "conflict_existing_row"},
    )
    assert dismissed_count == 3

    # `pending_candidates` (state='candidate', cursor=0) must now be empty —
    # the exact "same page forever" bug this method exists to fix.
    assert harness.catalog.pending_candidates(job["id"], limit=10) == []
    counts = harness.catalog.candidate_counts(job["id"])
    assert counts["candidate"] == 0
    assert counts["dismissed"] == 3
    dismissed_rows = harness.catalog.list_candidates(
        job["id"], state="dismissed", cursor=0, limit=10
    )
    assert {row["reject_info"].get("reason") for row in dismissed_rows} == {
        "conflict_existing_row"
    }

    # A no-op call (candidate already dismissed) touches nothing.
    assert harness.catalog.mark_candidates_dismissed(
        job["id"],
        [row["id"] for row in conflicts],
        reject_info={"reason": "conflict_existing_row"},
    ) == 0


def test_source_element_generation_and_complete_set_expiry_on_postgres(
    catalog_harness,
):
    """R8's two new store primitives against real PostgreSQL SQL.

    Both are genuinely backend-specific and therefore belong here rather than
    only in the SQLite service tests: ``source_element_generation`` aggregates a
    ``timestamptz`` (not an ISO string) and has to render it deterministically
    through ``iso_timestamp``, and ``expire_pending_candidates`` writes ``jsonb``
    in one statement whose predicate carries no id list at all.
    """
    harness = catalog_harness
    mark = "%s"

    # No elements yet: the empty token, which compares equal to itself and so
    # can never produce a spurious "the source was reparsed".
    assert harness.catalog.source_element_generation(harness.source_id) == ""

    def write_elements(created_at: str, count: int) -> None:
        with harness.database.write() as connection:
            connection.execute(
                "DELETE FROM source_elements WHERE source_id=%s",
                (harness.source_id,),
            )
            for index in range(count):
                connection.execute(
                    "INSERT INTO source_elements"
                    "(id,source_id,element_type,location_label,text,metadata,created_at)"
                    f" VALUES ({','.join([mark] * 7)})",
                    (
                        f"el-{created_at}-{index}",
                        harness.source_id,
                        "paragraph",
                        "p1",
                        "set_thing -density value",
                        "{}",
                        created_at,
                    ),
                )

    write_elements(NOW, 3)
    first = harness.catalog.source_element_generation(harness.source_id)
    assert first
    # Deterministic: the same underlying instant always renders identically,
    # which is the only property the stored snapshot relies on.
    assert first == harness.catalog.source_element_generation(harness.source_id)

    job = harness.catalog.create_job(
        harness.notebook_id, harness.source_id, "user-catalog",
        source_generation=first,
    )
    assert harness.catalog.get_job(job["id"])["source_generation"] == first
    harness.catalog.add_candidates(
        [
            {
                "job_id": job["id"],
                "notebook_id": harness.notebook_id,
                "source_id": harness.source_id,
                "position": index + 1,
                "section_path": f"cmd_{index}",
                "command_name": f"cmd_{index}",
                "payload": {},
                "state": "candidate",
            }
            for index in range(4)
        ]
    )

    # A whole-batch element swap (what `replace_elements` does) moves the token.
    write_elements("2026-08-01T00:00:00+00:00", 3)
    second = harness.catalog.source_element_generation(harness.source_id)
    assert second != first

    expired = harness.catalog.expire_pending_candidates(
        job["id"], reject_info={"reason": "source_reparsed"}
    )
    assert expired == 4
    counts = harness.catalog.candidate_counts(job["id"])
    assert counts["candidate"] == 0
    assert counts["dismissed"] == 4
    rows = harness.catalog.list_candidates(
        job["id"], state="dismissed", cursor=0, limit=10
    )
    assert {row["reject_info"].get("reason") for row in rows} == {"source_reparsed"}
    # Idempotent: nothing is left in `candidate` state to expire twice.
    assert harness.catalog.expire_pending_candidates(
        job["id"], reject_info={"reason": "source_reparsed"}
    ) == 0


def test_knowhow_table_columns_never_hydrates_rows_and_raises_on_a_missing_table(
    catalog_harness,
):
    harness = catalog_harness
    table_id = harness.knowhow.create_knowhow_table(
        harness.notebook_id,
        "命令目录：OpenROAD 手册",
        "",
        [
            {"name": "命令", "role": "anchor"},
            {"name": "说明", "role": "attribute"},
        ],
        "user-catalog",
    )
    columns = harness.knowhow.knowhow_table_columns(table_id)
    assert {column["name"] for column in columns} == {"命令", "说明"}
    assert all("cells" not in column and "rows" not in column for column in columns)

    with pytest.raises(KeyError):
        harness.knowhow.knowhow_table_columns("khtbl-does-not-exist")


def test_knowhow_anchor_existing_values_is_bounded_and_normalizes_whitespace(
    catalog_harness,
):
    """Mirrors the SQLite test's contract: an anchor cell written with
    incidental whitespace still matches a trimmed candidate name, matching
    the old `_existing_command_names`'s Python-side `.strip()` behaviour, and
    only the requested values come back — never a scan keyed by anything but
    `column_id`."""
    harness = catalog_harness
    table_id = harness.knowhow.create_knowhow_table(
        harness.notebook_id,
        "命令目录：OpenROAD 手册",
        "",
        [{"name": "命令", "role": "anchor"}],
        "user-catalog",
    )
    table = harness.knowhow.get_knowhow_table(table_id)
    anchor = table["columns"][0]["id"]
    harness.knowhow.add_knowhow_row(table_id, {anchor: "set_thing_0"})
    harness.knowhow.add_knowhow_row(table_id, {anchor: "  set_thing_1  "})

    existing = harness.knowhow.knowhow_anchor_existing_values(
        anchor, ["set_thing_0", "set_thing_1", "set_thing_2"]
    )
    assert existing == {"set_thing_0", "set_thing_1"}

    # Bounded: an empty ask costs nothing and returns nothing.
    assert harness.knowhow.knowhow_anchor_existing_values(anchor, []) == set()


def test_knowhow_table_id_by_title_is_a_bounded_point_lookup_matching_list_ordering(
    catalog_harness,
):
    """R11 P2: `_find_table` (command-catalog's by-title table resolution)
    resolves through this bounded point lookup, not `list_knowhow_tables`'s
    health-aggregated scan. The genuinely PostgreSQL-specific thing this
    proves: the tie-break for two tables sharing a derived title —
    ``ORDER BY created_at,id COLLATE "C"`` — surfaces the SAME row
    `list_knowhow_tables`'s own ``ORDER BY created_at,id COLLATE "C"`` would
    put first (creation order), so a caller migrated off the list-based scan
    sees byte-identical results."""
    harness = catalog_harness
    title = "命令目录：重名手册"

    first_id = harness.knowhow.create_knowhow_table(
        harness.notebook_id, title, "", [{"name": "命令", "role": "anchor"}],
        "user-catalog",
    )
    second_id = harness.knowhow.create_knowhow_table(
        harness.notebook_id, title, "", [{"name": "命令", "role": "anchor"}],
        "user-catalog",
    )
    assert first_id != second_id

    listed = harness.knowhow.list_knowhow_tables(harness.notebook_id)
    expected = next(t["id"] for t in listed if t["title"] == title)
    assert expected == first_id  # creation order: the earliest table wins

    assert (
        harness.knowhow.knowhow_table_id_by_title(harness.notebook_id, title)
        == expected
    )

    # A title nobody used resolves to "", not an exception.
    assert (
        harness.knowhow.knowhow_table_id_by_title(harness.notebook_id, "从未出现过")
        == ""
    )
