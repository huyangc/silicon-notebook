"""PostgreSQL conformance for the C1b review-fix additions to the command-
catalog and knowhow stores (方案 C·C1b, review round).

Scope is deliberately narrow: the SQLite side already has full behavioural
coverage in ``tests/test_command_catalog_job.py`` (identical fixtures,
identical assertions, same service layer). This file only proves the things
that are genuinely backend-specific — the migration actually adds the two new
``catalog_jobs`` columns on PostgreSQL, and the bounded ``KnowhowStorePort``
methods (``knowhow_table_columns``, ``knowhow_anchor_existing_values``,
``knowhow_table_id_by_title``, ``knowhow_table_title``,
``knowhow_table_notebook_id``,
``append_knowhow_rows_skipping_existing_anchors``) behave the same way
against real PostgreSQL SQL (``btrim`` instead of SQLite's ``trim``,
``= ANY(%s)`` instead of ``IN (...)``, ``COLLATE "C"`` instead of SQLite's
default binary collation, ``FOR UPDATE`` row locking instead of SQLite's
process-wide write mutex) as they do against SQLite.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from app.repositories.postgres.catalog_store import CatalogStore as PostgresCatalogStore
from app.repositories.postgres.knowhow_store import KnowhowStore as PostgresKnowhowStore
from app.services.command_catalog import STRIP_CHARS
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

    assert PostgresMigrator(database).migrate() == 40
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


def test_latest_applied_table_id_is_a_bounded_point_lookup_across_jobs(catalog_harness):
    """R18 (codex PR #412 review round 18): `latest_applied_table_id` is the
    store primitive `_resolve_target_table` (SQLite side; behaviourally
    covered exhaustively in `tests/test_command_catalog_job.py`) reads so a
    rerun's brand-new job converges on the table an EARLIER job for the same
    source already applied to. This only proves the primitive itself against
    real PostgreSQL SQL: no job for the source yet -> "", the MOST RECENT
    (`created_at DESC, id COLLATE "C" DESC`) job with a non-empty
    `applied_table_id` wins over an older one, a job that has not applied
    yet is invisible to it, and a different source's jobs never leak in."""
    harness = catalog_harness

    assert harness.catalog.latest_applied_table_id(harness.source_id) == ""

    job_a = harness.catalog.create_job(
        harness.notebook_id, harness.source_id, "user-catalog"
    )
    # Created, but has not applied yet -> still nothing to inherit.
    assert harness.catalog.latest_applied_table_id(harness.source_id) == ""

    harness.catalog.set_applied_table_id(job_a["id"], "khtbl-first")
    assert harness.catalog.latest_applied_table_id(harness.source_id) == "khtbl-first"
    assert harness.catalog.finish_job(job_a["id"], "succeeded") is True

    job_b = harness.catalog.create_job(
        harness.notebook_id, harness.source_id, "user-catalog"
    )
    # job_b exists but has not applied yet -> job_a's target is still the
    # most recent non-empty one.
    assert harness.catalog.latest_applied_table_id(harness.source_id) == "khtbl-first"

    harness.catalog.set_applied_table_id(job_b["id"], "khtbl-second")
    assert harness.catalog.latest_applied_table_id(harness.source_id) == "khtbl-second"
    assert harness.catalog.finish_job(job_b["id"], "succeeded") is True

    # A different source's jobs are invisible to this lookup.
    other_source = "src-catalog-other"
    mark = "%s"
    with harness.database.write() as connection:
        connection.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) "
            f"VALUES ({','.join([mark] * 8)})",
            (other_source, harness.notebook_id, "另一个手册", "markdown", "extracted",
             "extracted", NOW, NOW),
        )
    assert harness.catalog.latest_applied_table_id(other_source) == ""


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


def test_update_candidate_payload_revises_only_unreviewed_rows_on_postgres(
    catalog_harness,
):
    """方案 C v2's cross-window merge primitive against real PostgreSQL SQL.

    Behaviour is exhaustively covered on the SQLite side (identical service
    layer, ``tests/test_catalog_store.py`` plus the job-level merge tests);
    this proves the PostgreSQL statement itself — ``jsonb`` parameters instead
    of JSON text — makes the same three promises: it revises the two payload
    columns, it leaves identity/ordering/lifecycle alone, and it refuses a row
    that has already left ``candidate`` state.
    """
    harness = catalog_harness
    job = harness.catalog.create_job(
        harness.notebook_id, harness.source_id, "user-catalog"
    )
    harness.catalog.add_candidates(
        [
            {
                "job_id": job["id"],
                "notebook_id": harness.notebook_id,
                "source_id": harness.source_id,
                "position": index + 1,
                "section_path": "window 1",
                "command_name": f"cmd_{index}",
                "payload": {"args": [{"name": "-a"}], "excerpt": "first"},
                "state": "candidate",
                "reject_info": {"fields": []},
            }
            for index in range(2)
        ]
    )
    rows = harness.catalog.pending_candidates(job["id"], limit=10)
    first, second = rows[0], rows[1]

    assert harness.catalog.update_candidate_payload(
        first["id"],
        {"args": [{"name": "-a"}, {"name": "-b"}], "excerpt": "first"},
        {"fields": [{"field": "arg", "value": "-c", "reason": "not_in_text"}]},
    ) is True
    revised = harness.catalog.list_candidates(
        job["id"], state="candidate", cursor=0, limit=10
    )[0]
    assert [arg["name"] for arg in revised["payload"]["args"]] == ["-a", "-b"]
    assert revised["reject_info"]["fields"][0]["value"] == "-c"
    assert revised["id"] == first["id"]
    assert revised["position"] == first["position"]
    assert revised["state"] == "candidate"
    assert revised["command_name"] == first["command_name"]
    # The sibling row is untouched — the UPDATE is by primary key, not by job.
    assert harness.catalog.list_candidates(
        job["id"], state="candidate", cursor=first["position"], limit=10
    )[0]["payload"]["args"] == [{"name": "-a"}]

    # Already reviewed: refused, and the row keeps exactly what the reviewer
    # saw.
    assert harness.catalog.mark_candidates_applied(job["id"], [second["id"]]) == 1
    assert harness.catalog.update_candidate_payload(
        second["id"], {"args": []}, {}
    ) is False
    applied = harness.catalog.list_candidates(
        job["id"], state="applied", cursor=0, limit=10
    )[0]
    assert applied["payload"]["args"] == [{"name": "-a"}]

    assert harness.catalog.update_candidate_payload("cnd-nope", {}, {}) is False


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


def test_source_text_stats_reads_the_same_row_universe_as_preview_elements(
    catalog_harness,
):
    """方案 C v2's ``source_text_stats`` against real PostgreSQL SQL.

    Genuinely backend-specific here: the aggregate's ``ORDER BY``-free
    ``SELECT COUNT(*), COALESCE(SUM(LENGTH(text)),0) ... WHERE source_id=%s``
    has to behave the same as SQLite's ``length()``-based version even though
    PostgreSQL's ``LENGTH(text)`` is a different builtin. Row universe parity
    with ``preview_elements`` (``WHERE source_id=%s``, no other predicate) is
    proven directly: an unbounded ``preview_elements`` read returns exactly
    the ids ``source_text_stats`` counted, and the aggregate is NOT bounded by
    ``preview_elements``' own row ``limit`` or its per-row ``text_chars``
    clip."""
    harness = catalog_harness
    mark = "%s"

    # Empty source -> (0, 0), same as an empty preview_elements read.
    assert harness.catalog.source_text_stats(harness.source_id) == (0, 0)
    rows, clipped = harness.catalog.preview_elements(
        harness.source_id, limit=10, text_chars=100
    )
    assert rows == []
    assert clipped is False

    other_source = "src-catalog-stats-other"
    with harness.database.write() as connection:
        connection.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) "
            f"VALUES ({','.join([mark] * 8)})",
            (other_source, harness.notebook_id, "另一份手册", "markdown", "extracted",
             "extracted", NOW, NOW),
        )
        texts = [
            "set_thing -density value",
            "b" * 9000,
            "中文测试字符统计",
            "",
        ]
        for index, text in enumerate(texts):
            connection.execute(
                "INSERT INTO source_elements"
                "(id,source_id,element_type,location_label,text,metadata,created_at)"
                f" VALUES ({','.join([mark] * 7)})",
                (f"stats-el-{index}", harness.source_id, "paragraph", "p1", text,
                 "{}", NOW),
            )
        connection.execute(
            "INSERT INTO source_elements"
            "(id,source_id,element_type,location_label,text,metadata,created_at)"
            f" VALUES ({','.join([mark] * 7)})",
            ("stats-el-other", other_source, "paragraph", "p1", "unrelated",
             "{}", NOW),
        )

    element_count, total_chars = harness.catalog.source_text_stats(harness.source_id)
    assert element_count == len(texts)
    assert total_chars == sum(len(t) for t in texts)
    # A different source's elements never leak into either aggregate or count.
    assert harness.catalog.source_text_stats(other_source) == (1, len("unrelated"))

    # Same row universe as preview_elements: an unbounded read's ids are
    # exactly what source_text_stats counted.
    all_rows, _ = harness.catalog.preview_elements(
        harness.source_id, limit=100, text_chars=100_000
    )
    assert {row["id"] for row in all_rows} == {f"stats-el-{i}" for i in range(4)}

    # preview_elements' own row limit does not bound the aggregate.
    limited_rows, _ = harness.catalog.preview_elements(
        harness.source_id, limit=1, text_chars=100_000
    )
    assert len(limited_rows) == 1
    assert harness.catalog.source_text_stats(harness.source_id)[0] == 4

    # preview_elements' per-row text clip does not bound the char sum: the
    # long element is clipped in the preview read but counted in full here.
    clipped_rows, clipped_flag = harness.catalog.preview_elements(
        harness.source_id, limit=10, text_chars=10
    )
    assert clipped_flag is True
    assert harness.catalog.source_text_stats(harness.source_id) == (
        len(texts), sum(len(t) for t in texts)
    )


# The exact mixture the two sides can disagree on: the four characters SQL's
# BTRIM strips, plus four that `str.strip()` would also take and SQL must not
# (vertical tab, form feed, the ideographic space, NBSP). Content in the middle
# so a wrong strip set shows up as a LENGTH difference rather than as 0.
_MIXED_WHITESPACE = "\v\u3000 \t\xa0content\r\n \f\u3000"


def test_the_sql_strip_set_matches_the_packers_constant_on_postgres(
    catalog_harness,
):
    """`STRIP_CHARS` has three readers and this is one of them — asserted.

    The packer normalises elements by that constant, the cost preview judges
    "was this row truncated" by it, and this store strips it in SQL — as the
    literal `E' \\t\\n\\r'`, with nothing but a comment connecting the two.
    That seam is what the preview's LOWER bound rests on: strip more in Python
    than SQL does and the arithmetic floor lands above the truth.

    Genuinely backend-specific: PostgreSQL's `btrim` with an E-string of
    escapes is a different construct from SQLite's
    `trim(text, char(32)||char(9)||char(10)||char(13))`, and the escape
    sequence itself is one typo away from stripping the wrong set. Mirrors
    `test_the_sql_strip_set_matches_the_packers_constant_character_for_character`
    in `tests/test_catalog_store.py`.
    """
    harness = catalog_harness
    mark = "%s"
    with harness.database.write() as connection:
        connection.execute(
            "INSERT INTO source_elements"
            "(id,source_id,element_type,location_label,text,metadata,created_at)"
            f" VALUES ({','.join([mark] * 7)})",
            ("strip-el-0", harness.source_id, "paragraph", "p1",
             _MIXED_WHITESPACE, "{}", NOW),
        )
    expected = len(_MIXED_WHITESPACE.strip(STRIP_CHARS))

    _element_count, total_chars = harness.catalog.source_text_stats(
        harness.source_id
    )
    rows, _clipped = harness.catalog.preview_elements(
        harness.source_id, limit=10, text_chars=10_000
    )

    assert total_chars == expected
    assert rows[0]["full_chars"] == expected
    # And the mixture really does distinguish the two strip sets, or the
    # assertions above would hold under either.
    assert len(_MIXED_WHITESPACE.strip()) != expected


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


def test_knowhow_table_title_is_a_bare_point_lookup_and_raises_on_a_missing_table(
    catalog_harness,
):
    """R15 (codex PR #412 评审第 15 轮,P2): the new bounded title-only
    accessor `apply`'s `applied_table_id` fast path uses to report a
    non-stale `table_title`. Mirrors `knowhow_table_columns` above — one
    point lookup by primary key, KeyError when the table is gone."""
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
    assert harness.knowhow.knowhow_table_title(table_id) == "命令目录：OpenROAD 手册"

    with pytest.raises(KeyError):
        harness.knowhow.knowhow_table_title("khtbl-does-not-exist")


def test_knowhow_table_notebook_id_is_a_bare_point_lookup_and_raises_on_a_missing_table(
    catalog_harness,
):
    """R20 (codex PR #412 评审第 20 轮,P2): the bounded owning-notebook
    accessor `_inherit_applied_table` uses to re-verify an inherited apply
    target directly by id, instead of a title round-trip that a colliding
    title can resolve to the wrong table. Mirrors `knowhow_table_title`
    above — one point lookup by primary key, KeyError when the table is
    gone."""
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
    assert harness.knowhow.knowhow_table_notebook_id(table_id) == harness.notebook_id

    with pytest.raises(KeyError):
        harness.knowhow.knowhow_table_notebook_id("khtbl-does-not-exist")


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


def _knowhow_change_kinds(database, table_id: str) -> list[str]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT kind FROM knowhow_changes WHERE table_id=%s ORDER BY seq",
            (table_id,),
        ).fetchall()
    return [row["kind"] for row in rows]


def test_append_knowhow_rows_skipping_existing_anchors_dedups_atomically(
    catalog_harness,
):
    """R16 P2 (codex PR #412 review round 16): the anchor-membership check
    and the row insert must share ONE write transaction, or a row landed
    through the ordinary knowhow row/cell edit endpoints — not covered by
    the command catalog's own apply lock — between a caller's pre-read and a
    separate write could double a command up.

    Mirrors the SQLite store-level test's contract exactly
    (``test_append_knowhow_rows_skipping_existing_anchors_store_semantics``):
    a row already present (real PostgreSQL ``btrim`` + ``= ANY(%s)``
    normalized match) or duplicated within the SAME batch is skipped; only
    genuinely new rows are inserted, and exactly one ``import_append``
    change entry is recorded, covering only the rows that landed."""
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
    table = harness.knowhow.get_knowhow_table(table_id)
    anchor = table["columns"][0]["id"]
    harness.knowhow.add_knowhow_row(table_id, {anchor: "set_thing_0"})
    before_kinds = _knowhow_change_kinds(harness.database, table_id)

    result = harness.knowhow.append_knowhow_rows_skipping_existing_anchors(
        table_id,
        anchor,
        [
            {anchor: "set_thing_0"},  # already has a row -> skipped
            {anchor: "set_thing_1"},  # new -> inserted
            {anchor: "set_thing_1"},  # duplicate WITHIN this batch -> skipped
            {anchor: "  set_thing_2  "},  # incidental whitespace, still inserted
        ],
        actor="user-catalog",
        origin="import",
    )
    assert result["skipped_anchor_values"] == {"set_thing_0", "set_thing_1"}
    assert set(result["row_ids"]) == {"set_thing_1", "set_thing_2"}

    after = harness.knowhow.get_knowhow_table(table_id)
    names = {row["cells"].get(anchor, "") for row in after["rows"]}
    assert names == {"set_thing_0", "set_thing_1", "  set_thing_2  "}

    # One new change entry for the two rows that landed.
    after_kinds = _knowhow_change_kinds(harness.database, table_id)
    assert after_kinds == before_kinds + ["import_append"]

    # A batch that skips EVERY row records nothing new — the empty-batch
    # convention `append_knowhow_rows` already has.
    noop = harness.knowhow.append_knowhow_rows_skipping_existing_anchors(
        table_id, anchor, [{anchor: "set_thing_0"}],
        actor="user-catalog", origin="import",
    )
    assert noop == {"row_ids": {}, "skipped_anchor_values": {"set_thing_0"}}
    assert _knowhow_change_kinds(harness.database, table_id) == after_kinds

    empty = harness.knowhow.append_knowhow_rows_skipping_existing_anchors(
        table_id, anchor, [], actor="user-catalog", origin="import",
    )
    assert empty == {"row_ids": {}, "skipped_anchor_values": set()}
    assert _knowhow_change_kinds(harness.database, table_id) == after_kinds


def test_append_knowhow_rows_skipping_existing_anchors_requires_the_live_anchor_column(
    catalog_harness,
):
    """``anchor_column_id`` is re-verified as the table's CURRENT anchor
    column under the SAME `FOR UPDATE` lock this method takes — not trusted
    from a caller's earlier read. Mirrors the SQLite store-level test."""
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
    table = harness.knowhow.get_knowhow_table(table_id)
    anchor = next(c["id"] for c in table["columns"] if c["role"] == "anchor")
    attribute = next(c["id"] for c in table["columns"] if c["role"] != "anchor")

    with pytest.raises(ValueError):
        harness.knowhow.append_knowhow_rows_skipping_existing_anchors(
            table_id, attribute, [{anchor: "set_thing_0"}], actor="user-catalog",
        )

    harness.knowhow.set_knowhow_anchor_column(table_id, None, actor="user-catalog")
    with pytest.raises(ValueError):
        harness.knowhow.append_knowhow_rows_skipping_existing_anchors(
            table_id, anchor, [{anchor: "set_thing_0"}], actor="user-catalog",
        )

    assert harness.knowhow.get_knowhow_table(table_id)["rows"] == []

    with pytest.raises(KeyError):
        harness.knowhow.append_knowhow_rows_skipping_existing_anchors(
            "khtbl-does-not-exist", anchor, [{anchor: "set_thing_0"}],
            actor="user-catalog",
        )
