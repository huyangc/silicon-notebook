"""Backend-neutral cases for "a source's own paper metadata" (#1023 follow-up).

A ``source_paper_meta`` / ``source_authors`` row counts as its source's paper
metadata only when the child row's own ``notebook_id`` is the notebook the
source lives in -- the ``report_source_rows`` reading, which #1023 extended to
``paper_meta_for_sources``. This module covers the remaining readers:

* the missing-metadata predicate (``PAPER_META_NO_META_SQL``) behind the
  backfill queue (``sources_missing_paper_meta``), the offline backfill's
  pages (``maintenance.paper_metadata_source_ids_page``, batch_ingest
  ``--phase metadata``), the 「补全论文信息」 button probe
  (``notebook_paper_meta_missing``) and the analytics missing count;
* the analytics has_meta / marker counts (one pass with the missing count);
* the four display joins that only read ``is_paper`` / ``paper_title``:
  ``source_display_rows`` (enumeration labels), ``source_listing_rows`` /
  ``source_metadata`` (source cards), the notebook-delete activity snapshot
  (``_retain_user_activity_before_delete``) and the activity feed
  (``list_user_activity``).

Writers cannot produce a child row whose notebook disagrees with its source;
the cases model such a legacy row by corrupting one after a normal write --
the same technique as ``tests/test_sources_pagination.py``. Runners:
``tests/test_paper_meta_own_row.py`` (SQLite) and
``tests/postgres/test_paper_meta_own_row_pg.py`` (PostgreSQL).
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any

from app.domain.source_display import source_display_title
from app.models.schemas import NotebookCreate
from app.repositories.ports import SourceElementWrite
from tests.model_testkit import bind_chat_client

HEAD_TEXT = (
    "Gate Sizing Under Variability\nChen Hao\nFudan University\nDAC 2025\n"
    "Abstract: ..."
)
LLM_PAYLOAD = {
    "is_paper": True, "title": "Gate Sizing Under Variability",
    "authors": [{"name": "Chen Hao", "affiliations": ["Fudan University"]}],
    "venue": "DAC", "year": 2025, "doi": "", "keywords": [],
}


@dataclass(frozen=True)
class World:
    owner_id: str
    notebook_id: str
    other_id: str


class FakePaperLLM:
    configured = True
    model = "fake-paper"

    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def chat_json(self, messages, schema_hint, **kwargs):
        with self._lock:
            self.calls += 1
        return json.dumps(LLM_PAYLOAD)


def _meta(title: str | None, *, is_paper: bool = True, authors=()) -> dict:
    return {
        "is_paper": is_paper, "paper_title": title, "venue": "DAC",
        "pub_year": 2025, "doi": None, "keywords": [], "raw_json": {},
        "model": "seed",
        "authors": [
            {"position": index, "name": name, "affiliation": ""}
            for index, name in enumerate(authors)
        ],
    }


def _source(repo, notebook_id: str, source_id: str, title: str) -> None:
    store = repo._runtime.source_store
    store.insert_source(
        source_id=source_id, notebook_id=notebook_id, title=title,
        source_type="document", status="parsed", parse_status="parsed",
        file_name=f"{source_id}.pdf", file_path=f"/tmp/{source_id}.pdf",
        file_size=0, file_hash=f"h-{source_id}", summary="", doc_type="",
    )
    with store.database.write() as db:
        store.replace_elements(
            db, source_id,
            [SourceElementWrite(id=f"el-{source_id}-0001", element_type="text",
                                location_label="", text=HEAD_TEXT, metadata={})],
            created_at="2026-01-01T00:00:00+00:00",
        )


def build_world(repo) -> World:
    """Target notebook: s-legacy (a paper; its rows get corrupted by
    ``corrupt``), s-good (a paper), s-marker (judged not a paper), s-none (no
    row yet). A second notebook of the same owner holds s-other (a paper)."""
    runtime = repo._runtime
    owner = runtime.identity.create_user("p00731001", "password-731")
    notebook_id = runtime.notebook_store.create_row(
        NotebookCreate(name="own-row target"), owner.id)
    other_id = runtime.notebook_store.create_row(
        NotebookCreate(name="own-row other"), owner.id)
    store = runtime.source_store
    _source(repo, notebook_id, "s-legacy", "Scanned upload")
    store.upsert_paper_meta("s-legacy", notebook_id,
                            _meta("Needle in a Haystack", authors=("Zeta Needleman",)))
    _source(repo, notebook_id, "s-good", "Plain upload")
    store.upsert_paper_meta("s-good", notebook_id,
                            _meta("Good Paper", authors=("Ada Good",)))
    _source(repo, notebook_id, "s-marker", "Lecture notes")
    store.upsert_paper_meta("s-marker", notebook_id, _meta(None, is_paper=False))
    _source(repo, notebook_id, "s-none", "Fresh upload")
    _source(repo, other_id, "s-other", "Other upload")
    store.upsert_paper_meta("s-other", other_id, _meta("Other Paper"))
    return World(owner.id, notebook_id, other_id)


def corrupt(repo, world: World, ph: str) -> None:
    """Move s-legacy's metadata and author rows to the other notebook, as a
    pre-writer legacy row would look; the source itself stays put."""
    with repo._runtime.source_store.database.write() as db:
        db.execute(
            f"UPDATE source_paper_meta SET notebook_id = {ph} WHERE source_id = {ph}",
            (world.other_id, "s-legacy"),
        )
        db.execute(
            f"UPDATE source_authors SET notebook_id = {ph} WHERE source_id = {ph}",
            (world.other_id, "s-legacy"),
        )


def _missing_probe(repo, notebook_id: str) -> bool:
    store = repo._runtime.source_store
    with store.database.connect() as db:
        return bool(repo._runtime.queries.notebook_paper_meta_missing(db, notebook_id))


def _offline_page(repo, notebook_id: str) -> list[str]:
    """The offline backfill's candidate pages (batch_ingest --phase metadata)."""
    return repo.maintenance.paper_metadata_source_ids_page(notebook_id, limit=500)


def _counts(repo, notebook_id: str) -> dict[str, int]:
    return dict(repo._runtime.queries.notebook_analytics(notebook_id).paper_meta_counts)


def _child_notebook(repo, source_id: str, ph: str) -> str:
    with repo._runtime.source_store.database.connect() as db:
        return db.execute(
            f"SELECT notebook_id FROM source_paper_meta WHERE source_id = {ph}",
            (source_id,),
        ).fetchone()["notebook_id"]


def case_backfill_finds_and_heals_a_legacy_row(repo, ph: str, monkeypatch) -> None:
    """codex #1023 P2: the narrowed reads show the legacy source as missing, so
    the backfill path must queue it too -- and the re-extraction's upsert then
    writes the source's notebook back, after which every reader agrees."""
    world = build_world(repo)
    store = repo._runtime.source_store
    notebook_id = world.notebook_id
    # s-none is the only source without metadata before the corruption.
    assert store.sources_missing_paper_meta(notebook_id) == ["s-none"]
    with store.database.write() as db:
        db.execute(f"DELETE FROM source_paper_meta WHERE source_id = {ph}", ("s-none",))
        db.execute(f"DELETE FROM sources WHERE id = {ph}", ("s-none",))
    assert _missing_probe(repo, notebook_id) is False
    assert _offline_page(repo, notebook_id) == []
    assert _counts(repo, notebook_id) == {"has_meta": 2, "marker": 1, "missing": 0}

    corrupt(repo, world, ph)

    assert store.sources_missing_paper_meta(notebook_id) == ["s-legacy"]
    # The offline backfill pages the same candidate set (opus review P1).
    assert _offline_page(repo, notebook_id) == ["s-legacy"]
    assert _missing_probe(repo, notebook_id) is True
    assert _counts(repo, notebook_id) == {"has_meta": 1, "marker": 1, "missing": 1}
    # The row does not count for the notebook its child row was written under
    # either: that notebook does not hold the source.
    assert _counts(repo, world.other_id) == {"has_meta": 1, "marker": 0, "missing": 0}

    from app.services import pending_bus as pending_bus_module

    monkeypatch.setattr(pending_bus_module.pending_bus, "emit", lambda *_a, **_k: None)
    monkeypatch.setattr(pending_bus_module.pending_bus, "mark_dirty", lambda *_a, **_k: None)
    fake = FakePaperLLM()
    bind_chat_client(repo, "paper_metadata", fake)

    assert repo.backfill_paper_metadata(notebook_id) == {"total": 1, "stored": 1}

    assert fake.calls == 1
    assert _child_notebook(repo, "s-legacy", ph) == notebook_id
    assert store.sources_missing_paper_meta(notebook_id) == []
    assert _offline_page(repo, notebook_id) == []
    assert _missing_probe(repo, notebook_id) is False
    assert _counts(repo, notebook_id) == {"has_meta": 2, "marker": 1, "missing": 0}
    meta = store.get_paper_meta("s-legacy", notebook_id)
    assert meta["paper_title"] == "Gate Sizing Under Variability"
    assert [author["name"] for author in meta["authors"]] == ["Chen Hao"]
    assert repo.backfill_paper_metadata(notebook_id) == {"total": 0}


def _activity_items(repo, owner_id: str) -> dict[str, dict]:
    result = repo._runtime.queries.list_user_activity(
        owner_id, activity_type="source", limit=50)
    return {item["id"]: item for item in result["items"]}


def _retained_titles(repo, notebook_id: str, ph: str) -> dict[str, str]:
    runtime = repo._runtime
    with runtime.source_store.database.write() as db:
        runtime.notebook_store._retain_user_activity_before_delete(db, notebook_id)
    with runtime.source_store.database.connect() as db:
        return {
            row["record_id"]: row["display_title"]
            for row in db.execute(
                "SELECT record_id, display_title FROM retained_user_activity "
                f"WHERE activity_type = 'source' AND notebook_id = {ph}",
                (notebook_id,),
            ).fetchall()
        }


def case_display_joins_drop_a_legacy_row(repo, ph: str) -> None:
    """The four display joins read is_paper / paper_title of the source's own
    row only: on the legacy row they read nothing (the source's own title
    shows), on a well-formed paper they read its metadata as before."""
    world = build_world(repo)
    corrupt(repo, world, ph)
    runtime = repo._runtime
    store = runtime.source_store
    ids = ["s-legacy", "s-good"]

    with store.database.connect() as db:
        labels = {row["id"]: dict(row) for row in store.source_display_rows(db, ids)}
    assert (labels["s-legacy"]["is_paper"], labels["s-legacy"]["paper_title"]) == (None, None)
    assert labels["s-good"]["paper_title"] == "Good Paper"
    assert labels["s-good"]["is_paper"]

    cards = store.source_metadata(ids)
    assert (cards["s-legacy"]["is_paper"], cards["s-legacy"]["paper_title"]) == (None, None)
    assert cards["s-good"]["paper_title"] == "Good Paper"
    with store.database.connect() as db:
        rows = {row["id"]: dict(row) for row in store.source_listing_rows(db, ids)}
    assert rows == cards

    items = _activity_items(repo, world.owner_id)
    assert (items["s-legacy"]["is_paper"], items["s-legacy"]["paper_title"],
            items["s-legacy"]["paper_meta_status"]) == (False, "", "missing")
    assert (items["s-good"]["is_paper"], items["s-good"]["paper_title"],
            items["s-good"]["paper_meta_status"]) == (True, "Good Paper", "has_meta")

    titles = _retained_titles(repo, world.notebook_id, ph)
    assert titles["s-legacy"] == "Scanned upload"
    assert titles["s-good"] == "Good Paper"


def _old_join(alias: str) -> str:
    """The pre-narrowing join every display reader used: source_id only."""
    return f"LEFT JOIN source_paper_meta {alias} ON {alias}.source_id = s.id"


def _rows(db, sql: str, params) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def case_well_formed_rows_read_byte_identically(
    repo, ph: str, *, eligible_sql: str, missing_order: str,
) -> None:
    """Differential on the only shape the writers produce (every child row in
    its source's notebook): each narrowed reader returns, byte for byte, what
    the pre-narrowing statement returns. The oracles are those statements
    verbatim (source_id-only joins / the source_id-only NOT EXISTS / the two
    analytics statements)."""
    world = build_world(repo)
    runtime = repo._runtime
    store = runtime.source_store
    ids = ["s-legacy", "s-good", "s-marker", "s-none", "s-other"]
    marks = ",".join(ph for _ in ids)
    old_no_meta = " AND NOT EXISTS (SELECT 1 FROM source_paper_meta m WHERE m.source_id = s.id)"

    with store.database.connect() as db:
        new_labels = sorted(
            (dict(row) for row in store.source_display_rows(db, ids)), key=lambda r: r["id"])
        old_labels = sorted(_rows(
            db,
            "SELECT s.id, s.notebook_id, s.title, s.file_name, m.is_paper, m.paper_title "
            f"FROM sources s {_old_join('m')} WHERE s.id IN ({marks})",
            ids,
        ), key=lambda r: r["id"])
        new_cards = sorted(
            (dict(row) for row in store.source_listing_rows(db, ids)), key=lambda r: r["id"])
        old_cards = sorted(_rows(
            db,
            "SELECT s.id, s.notebook_id, s.title, s.file_name, s.summary, s.doc_type, "
            f"s.source_type, m.is_paper, m.paper_title FROM sources s {_old_join('m')} "
            f"WHERE s.id IN ({marks})",
            ids,
        ), key=lambda r: r["id"])
        for notebook_id in (world.notebook_id, world.other_id):
            old_missing = [row["id"] for row in _rows(
                db,
                f"SELECT s.id FROM sources s WHERE s.notebook_id = {ph}"
                f"{eligible_sql}{old_no_meta}{missing_order}",
                (notebook_id,),
            )]
            assert store.sources_missing_paper_meta(notebook_id) == old_missing
            assert _offline_page(repo, notebook_id) == sorted(old_missing)
            assert _missing_probe(repo, notebook_id) is bool(old_missing)
            by_is_paper = {
                int(row["is_paper"]): int(row["c"]) for row in _rows(
                    db,
                    "SELECT is_paper, COUNT(*) AS c FROM source_paper_meta "
                    f"WHERE notebook_id = {ph} GROUP BY is_paper",
                    (notebook_id,),
                )
            }
            assert _counts(repo, notebook_id) == {
                "has_meta": by_is_paper.get(1, 0),
                "marker": by_is_paper.get(0, 0),
                "missing": len(old_missing),
            }
        joined = {
            row["id"]: row for row in _rows(
                db,
                "SELECT s.id, s.title, s.file_name, s.source_type, m.is_paper, "
                f"m.paper_title FROM sources s {_old_join('m')} WHERE s.id IN ({marks})",
                ids,
            )
        }
    assert _canonical(new_labels) == _canonical(old_labels)
    assert _canonical(new_cards) == _canonical(old_cards)

    items = _activity_items(repo, world.owner_id)
    for source_id in ids:
        old = joined[source_id]
        assert (items[source_id]["is_paper"], items[source_id]["paper_title"]) == (
            bool(old["is_paper"]), old["paper_title"] or ""), source_id
    titles = _retained_titles(repo, world.notebook_id, ph)
    for source_id in ("s-legacy", "s-good", "s-marker", "s-none"):
        assert titles[source_id] == source_display_title(joined[source_id]), source_id
