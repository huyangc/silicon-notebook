"""Store-level coverage for ``CatalogStorePort.source_text_stats`` (方案 C v2).

Deliberately built directly on ``app.repositories.sqlite.catalog_store.CatalogStore``
plus a bare migrated ``SqliteDatabase`` — NOT through the full
``SQLiteRepository``/``app.services.repository_runtime`` composition, which at
the time of writing transitively imports ``app.services.catalog_job`` (a
concurrent, in-progress rewrite of the command-catalog extraction pipeline
that this task does not touch). This file only proves the STORE primitive;
job-layer behaviour belongs in ``test_command_catalog_job.py``.

``source_text_stats`` must read the exact same row universe as
``preview_elements`` — both are ``WHERE source_id=?`` with no other predicate
(no ``element_type`` filter, no status filter) — so the two are exercised
side by side rather than in isolation.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.sqlite.catalog_store import CatalogStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator


NOW = "2026-08-10T00:00:00+00:00"


@pytest.fixture
def catalog(tmp_path: Path) -> CatalogStore:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    migrated = SqliteMigrator(database, settings).migrate()
    assert migrated, "fresh database must actually run the migration ladder"

    counter = {"n": 0}

    def new_id(prefix: str) -> str:
        counter["n"] += 1
        return f"{prefix}-{counter['n']:04d}"

    with database.write() as db:
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,"
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            ("user-1", "u@example.test", "U", "admin", "active", NOW, NOW),
        )
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,"
            "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            ("nb-1", "NB", "", "engineering", "ready", "user-1", NOW, NOW),
        )
        for source_id, title in (("src-1", "Manual"), ("src-2", "Other")):
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (source_id, "nb-1", title, "markdown", "extracted",
                 "extracted", NOW, NOW),
            )

    return CatalogStore(database, new_id=new_id, now=lambda: NOW)


def _insert_elements(catalog: CatalogStore, source_id: str, texts: list[str]) -> None:
    with catalog.database.write() as db:
        for index, text in enumerate(texts):
            db.execute(
                "INSERT INTO source_elements(id,source_id,element_type,"
                "location_label,text,metadata,created_at) VALUES (?,?,?,?,?,?,?)",
                (f"{source_id}-el-{index}", source_id, "paragraph", "p1", text,
                 "{}", NOW),
            )


# --------------------------------------------------------------- empty source
def test_source_text_stats_of_an_empty_source_is_zero_zero(catalog: CatalogStore):
    assert catalog.source_text_stats("src-1") == (0, 0)
    # preview_elements agrees: same row universe, nothing to hydrate either.
    rows, clipped = catalog.preview_elements("src-1", limit=10, text_chars=100)
    assert rows == []
    assert clipped is False


# -------------------------------------------------- multi-element aggregation
def test_source_text_stats_counts_elements_and_sums_char_lengths(catalog: CatalogStore):
    texts = [
        "set_thing -density value",
        "a" * 5000,
        "中文测试字符统计",  # non-ASCII: SQLite's length() counts characters, not bytes
        "",  # an empty-text row still counts as one element
    ]
    _insert_elements(catalog, "src-1", texts)

    element_count, total_chars = catalog.source_text_stats("src-1")
    assert element_count == len(texts)
    assert total_chars == sum(len(t) for t in texts)


def test_source_text_stats_is_scoped_to_its_own_source(catalog: CatalogStore):
    _insert_elements(catalog, "src-1", ["one", "two"])
    _insert_elements(catalog, "src-2", ["three three three"])

    assert catalog.source_text_stats("src-1") == (2, len("one") + len("two"))
    assert catalog.source_text_stats("src-2") == (1, len("three three three"))
    # A source id nobody used is the same empty answer as a genuinely empty
    # source — no exception, no negative counts.
    assert catalog.source_text_stats("does-not-exist") == (0, 0)


# --------------------------------------------- same row universe as preview
def test_source_text_stats_shares_preview_elements_row_universe(catalog: CatalogStore):
    """``preview_elements`` has exactly one predicate — ``WHERE source_id=?``,
    no ``element_type``/status filter — so there is no row this feature could
    construct that preview_elements would exclude and source_text_stats would
    include, or vice versa. This proves that identity two ways: with an
    unbounded limit, preview_elements' returned ids are exactly the set
    source_text_stats counted; and source_text_stats counts elements
    preview_elements' own row LIMIT would have left off (its count is not
    capped by ``limit`` the way preview_elements' result list is).
    """
    texts = [f"element number {i}" for i in range(5)]
    _insert_elements(catalog, "src-1", texts)

    element_count, total_chars = catalog.source_text_stats("src-1")
    assert element_count == 5
    assert total_chars == sum(len(t) for t in texts)

    # Unbounded-enough limit: preview_elements' ids are exactly what
    # source_text_stats counted — same WHERE, same rows.
    rows, _clipped = catalog.preview_elements("src-1", limit=100, text_chars=10_000)
    assert len(rows) == element_count
    assert {row["id"] for row in rows} == {f"src-1-el-{i}" for i in range(5)}

    # A tight limit bounds preview_elements' returned rows but must NOT bound
    # source_text_stats — the row universes are the same, but one caller asks
    # for a capped page and the other asks for a full aggregate.
    limited_rows, _clipped2 = catalog.preview_elements("src-1", limit=2, text_chars=10_000)
    assert len(limited_rows) == 2
    assert catalog.source_text_stats("src-1")[0] == 5


def test_source_text_stats_char_sum_is_unclipped_unlike_preview_elements_text(
    catalog: CatalogStore,
):
    """``preview_elements`` clips each row's returned text to ``text_chars``
    (and reports it via ``clipped``); ``source_text_stats`` must sum the
    FULL, unclipped length regardless — the whole point of adding a
    SQL-side aggregate is to know the true character budget without ever
    materializing (or clipping) the text itself.

    Each preview row carries that same full length for ITSELF (``full_chars``),
    which is what lets the caller subtract the prefix from the total exactly.
    Deriving it from the returned ``text`` is impossible — that string has
    already lost everything past ``text_chars``."""
    long_text = "x" * 9000
    _insert_elements(catalog, "src-1", [long_text])

    rows, clipped = catalog.preview_elements("src-1", limit=10, text_chars=10)
    assert clipped is True
    assert len(rows[0]["text"]) == 10  # preview_elements' own clip
    assert rows[0]["full_chars"] == 9000  # the element's WHOLE length

    element_count, total_chars = catalog.source_text_stats("src-1")
    assert element_count == 1
    assert total_chars == 9000  # NOT clipped to text_chars


def test_char_sums_mirror_the_packers_own_whitespace_strip(catalog: CatalogStore):
    """Both char numbers count what the PACKER counts, i.e. after stripping.

    `command_catalog._window_elements` strips every element before packing,
    so a sum of raw lengths describes a document the packer never sees. That
    difference is not cosmetic: the preview publishes its window count as a
    LOWER bound, and raw lengths make it over-count — 2,001 elements of "one
    character plus twenty trailing spaces" is 42,021 raw characters (four
    windows by arithmetic) and one single window in reality. A bound that
    sits above the truth is worse than no bound.
    """
    texts = [
        "  set_thing -density value  ",  # both ends
        "\n\ttrailing tab and newline\r\n",
        "   ",  # whitespace only: contributes nothing to any window
        "no padding here",
    ]
    _insert_elements(catalog, "src-1", texts)

    element_count, total_chars = catalog.source_text_stats("src-1")

    # Every row still counts as an ELEMENT — only its characters are
    # normalised — so the count keeps agreeing with `preview_elements`' rows.
    assert element_count == 4
    assert total_chars == sum(len(text.strip()) for text in texts)
    assert total_chars < sum(len(text) for text in texts)

    # `full_chars` uses the same normalisation, or the caller's subtraction
    # would mix two different definitions of "how long is this element".
    rows, _clipped = catalog.preview_elements("src-1", limit=10, text_chars=10_000)
    assert [row["full_chars"] for row in rows] == [
        len(text.strip()) for text in texts
    ]
    assert rows[2]["full_chars"] == 0  # the whitespace-only element


# ------------------------------------------------- update_candidate_payload
def _one_candidate(catalog: CatalogStore, *, state: str = "candidate") -> dict:
    job = catalog.create_job("nb-1", "src-1", "user-1")
    catalog.add_candidates(
        [
            {
                "job_id": job["id"],
                "notebook_id": "nb-1",
                "source_id": "src-1",
                "position": 1,
                "section_path": "window 1",
                "command_name": "set_db",
                "payload": {"args": [{"name": "-a"}], "excerpt": "first"},
                "state": state,
                "reject_info": {"fields": []},
            }
        ]
    )
    return catalog.list_candidates(
        job["id"], state=state, cursor=0, limit=10
    )[0]


def test_update_candidate_payload_revises_payload_and_reject_info_only(
    catalog: CatalogStore,
):
    """v2's cross-window merge writes through this: a command documented in
    two windows keeps ONE row, revised, rather than gaining a second."""
    row = _one_candidate(catalog)
    assert catalog.update_candidate_payload(
        row["id"],
        {"args": [{"name": "-a"}, {"name": "-b"}], "excerpt": "first"},
        {"fields": [{"field": "arg", "value": "-c", "reason": "not_in_text"}]},
    ) is True

    revised = catalog.list_candidates(
        row["job_id"], state="candidate", cursor=0, limit=10
    )[0]
    assert [arg["name"] for arg in revised["payload"]["args"]] == ["-a", "-b"]
    assert revised["reject_info"]["fields"][0]["value"] == "-c"
    # Identity, ordering and lifecycle columns are NOT the merge's to touch:
    # `position` is the review page's keyset cursor, `state` is the review
    # lifecycle, and the other two say which command this row is.
    assert revised["id"] == row["id"]
    assert revised["position"] == row["position"]
    assert revised["state"] == "candidate"
    assert revised["command_name"] == "set_db"
    assert revised["section_path"] == "window 1"


def test_update_candidate_payload_refuses_a_row_a_person_already_reviewed(
    catalog: CatalogStore,
):
    """A row that left `candidate` state is what a reviewer confirmed or
    skipped. The knowhow row it produced was written from the payload they
    saw, so a later window revising it under them would only make the two
    disagree — and the `False` is what says the revision did not land."""
    row = _one_candidate(catalog, state="applied")
    assert catalog.update_candidate_payload(row["id"], {"args": []}, {}) is False
    unchanged = catalog.list_candidates(
        row["job_id"], state="applied", cursor=0, limit=10
    )[0]
    assert [arg["name"] for arg in unchanged["payload"]["args"]] == ["-a"]


def test_update_candidate_payload_of_a_missing_row_is_false_not_an_error(
    catalog: CatalogStore,
):
    assert catalog.update_candidate_payload("cnd-nope", {}, {}) is False
