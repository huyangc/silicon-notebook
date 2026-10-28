"""Backend-neutral cases for the ``source_paper_cards`` store methods (SQLite v104 /
PostgreSQL 0085). SQLite: ``tests/test_paper_card_store.py``; PostgreSQL twin:
``tests/postgres/test_paper_card_store_pg.py``.

Each case takes an ``Env``: the backend's ``SourceStore``, a ``run`` that executes a
write with ``?`` placeholders, a ``rows`` that reads dicts back, and ``connect`` that
opens a read connection for ``paper_cards_for_sources``.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import pytest

NB = "nb-pc"
OTHER_NB = "nb-pc-other"
SRC = "src-pc"
OWNER = "u-pc"
NOW = "2026-10-28T12:00:00+00:00"
ELEMENT_STAMP = "2026-10-28T11:00:00+00:00"
NEWER_STAMP = "2026-10-28T13:00:00+00:00"
CARD = {"problem": "p", "metrics": [{"name": "m", "value": "1.0"}]}


@dataclass
class Env:
    store: Any
    run: Callable[[str, tuple], None]
    rows: Callable[[str, tuple], list[dict]]
    connect: Callable[[], AbstractContextManager]

    def seed(self) -> None:
        self.run(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (OWNER, f"{OWNER}@x", "pc", "user", NOW, NOW),
        )
        for notebook_id in (NB, OTHER_NB):
            self.run(
                "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (notebook_id, notebook_id, OWNER, "ready", NOW, NOW),
            )
        self.source(SRC)
        self.element("e-seed", ELEMENT_STAMP)

    def source(self, source_id: str, notebook_id: str = NB) -> None:
        self.run(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, notebook_id, source_id, "pdf", "extracted", "extracted",
             f"{source_id}.pdf", f"p/{source_id}.pdf", NOW, NOW),
        )

    def element(self, element_id: str, created_at: str, source_id: str = SRC) -> None:
        self.run(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (element_id, source_id, "paragraph", "p1", "t", created_at),
        )

    def meta(self, source_id: str = SRC, notebook_id: str = NB) -> None:
        self.store.upsert_paper_meta(
            source_id, notebook_id,
            {"is_paper": True, "paper_title": "T", "keywords": [], "authors": [], "model": "m"},
        )

    def basis(self, source_id: str = SRC) -> str:
        return self.store.source_card_basis(source_id)

    def card(self, source_id: str = SRC, notebook_id: str = NB, **overrides) -> bool:
        args: dict[str, Any] = dict(
            basis=self.basis(source_id), card_version=1, status="ready", card=CARD,
            raw_json={"dropped": 0}, model="model-a",
        )
        args.update(overrides)
        return self.store.upsert_paper_card(source_id, notebook_id, **args)

    def read(self, ids=(SRC,), notebook_id: str = NB) -> dict[str, dict]:
        with self.connect() as db:
            return self.store.paper_cards_for_sources(db, list(ids), notebook_id=notebook_id)

    def count(self) -> int:
        return int(self.rows("SELECT COUNT(*) AS n FROM source_paper_cards", ())[0]["n"])


def assert_ready_roundtrip(env: Env) -> None:
    env.seed()
    assert env.card() is True
    row = env.read()[SRC]
    assert row["notebook_id"] == NB
    assert row["basis"] == env.basis()
    assert row["card_version"] == 1
    assert row["status"] == "ready"
    assert row["card"] == CARD
    assert row["raw_json"] == {"dropped": 0}
    assert row["model"] == "model-a"
    assert row["attempts"] == 0
    # Both timestamps come back as timezone-aware UTC ISO strings.
    for key in ("created_at", "updated_at"):
        assert datetime.fromisoformat(row[key]).utcoffset() == timedelta(0)


def assert_read_is_narrowed_and_batched(env: Env) -> None:
    env.seed()
    env.source("src-pc-2")
    env.source("src-pc-3")
    env.element("e-2", ELEMENT_STAMP, "src-pc-2")
    env.card(SRC)
    env.card("src-pc-2")
    assert set(env.read([SRC, "src-pc-2", "src-pc-3", "src-none"])) == {SRC, "src-pc-2"}
    # Narrowed by the notebook the sources live in: another library sees nothing.
    assert env.read([SRC], notebook_id=OTHER_NB) == {}
    assert env.read([]) == {}


def assert_upsert_checks_ownership(env: Env) -> None:
    env.seed()
    with pytest.raises(KeyError):
        env.card(SRC, OTHER_NB)  # the source lives in NB
    with pytest.raises(KeyError):
        env.card("src-gone", NB)
    with pytest.raises(KeyError):
        env.card(SRC, "nb-gone")
    assert env.count() == 0


def assert_source_delete_cascades(env: Env) -> None:
    env.seed()
    env.source("src-keep")
    env.element("e-keep", ELEMENT_STAMP, "src-keep")
    env.card(SRC)
    env.card("src-keep")
    env.run("DELETE FROM sources WHERE id=?", (SRC,))
    assert [r["source_id"] for r in env.rows("SELECT source_id FROM source_paper_cards", ())] == ["src-keep"]


def assert_notebook_delete_cascades(env: Env) -> None:
    env.seed()
    env.source("src-other", OTHER_NB)
    env.element("e-other", ELEMENT_STAMP, "src-other")
    env.card(SRC)
    env.card("src-other", OTHER_NB)
    env.run("DELETE FROM notebooks WHERE id=?", (NB,))
    assert [r["source_id"] for r in env.rows("SELECT source_id FROM source_paper_cards", ())] == ["src-other"]


def assert_clear_paper_meta_deletes_the_card(env: Env) -> None:
    env.seed()
    env.source("src-keep")
    env.element("e-keep", ELEMENT_STAMP, "src-keep")
    env.meta()
    env.meta("src-keep")
    env.card(SRC)
    env.card("src-keep")
    env.store.clear_paper_meta(SRC)
    assert env.rows("SELECT 1 AS x FROM source_paper_meta WHERE source_id=?", (SRC,)) == []
    assert [r["source_id"] for r in env.rows("SELECT source_id FROM source_paper_cards", ())] == ["src-keep"]
    # Idempotent: no row, no error.
    env.store.clear_paper_meta(SRC)


def assert_failed_never_overwrites_a_ready_card_of_the_same_basis(env: Env) -> None:
    env.seed()
    assert env.card(status="ready") is True
    before = env.read()[SRC]
    # Same basis, same card version, already ready: the failure is refused and
    # leaves the row byte-for-byte as it was (attempts included).
    assert env.card(status="failed", card={}, raw_json={"reason": "late"}, model="model-b") is False
    assert env.read()[SRC] == before


def assert_a_stale_basis_is_refused_and_keeps_the_newer_card(env: Env) -> None:
    env.seed()
    stale = env.basis()
    env.element("e-new", NEWER_STAMP)  # the document was re-parsed: a newer witness
    fresh = env.basis()
    assert fresh != stale
    assert env.card(basis=fresh, status="ready", card={"problem": "new"}) is True
    before = env.read()[SRC]
    # An in-flight task that sampled the old content: neither its ready nor its
    # failed write may displace the card built on the new content.
    assert env.card(basis=stale, status="ready", card={"problem": "old"}) is False
    assert env.card(basis=stale, status="failed", card={}) is False
    assert env.read()[SRC] == before


def assert_failed_for_another_card_version_overwrites(env: Env) -> None:
    env.seed()
    env.card(status="ready", card_version=1)
    assert env.card(status="failed", card_version=2, card={}) is True
    row = env.read()[SRC]
    assert (row["status"], row["card_version"], row["attempts"]) == ("failed", 2, 1)


def assert_attempts_count_in_sql_and_reset_on_ready(env: Env) -> None:
    env.seed()
    assert env.card(status="failed", card={}, raw_json={"reason": "a"}) is True
    assert env.read()[SRC]["attempts"] == 1
    assert env.card(status="failed", card={}, raw_json={"reason": "b"}) is True
    assert env.card(status="failed", card={}, raw_json={"reason": "c"}) is True
    row = env.read()[SRC]
    assert (row["status"], row["attempts"], row["raw_json"]) == ("failed", 3, {"reason": "c"})
    first_created = row["created_at"]
    assert env.card(status="ready") is True
    row = env.read()[SRC]
    assert (row["status"], row["attempts"], row["card"]) == ("ready", 0, CARD)
    assert row["created_at"] == first_created  # created_at survives an overwrite
    # A ready card replaces a ready card of another basis, too.
    env.element("e-new", NEWER_STAMP)
    assert env.card(status="ready", card={"problem": "q"}) is True
    row = env.read()[SRC]
    assert (row["basis"], row["card"]) == (env.basis(), {"problem": "q"})


def assert_attempts_restart_when_basis_or_version_changes(env: Env) -> None:
    env.seed()
    for _ in range(3):
        env.card(status="failed", card={})
    assert env.read()[SRC]["attempts"] == 3
    # Same basis, new card version: the old cooldown does not weigh on it.
    assert env.card(status="failed", card_version=2, card={}) is True
    assert env.read()[SRC]["attempts"] == 1
    assert env.card(status="failed", card_version=2, card={}) is True
    assert env.read()[SRC]["attempts"] == 2
    # New content (new basis): restart at 1 as well.
    env.element("e-new", NEWER_STAMP)
    assert env.card(status="failed", card_version=2, card={}) is True
    assert env.read()[SRC]["attempts"] == 1


def assert_unknown_status_is_rejected_before_any_sql(env: Env) -> None:
    env.seed()
    for status in ("", "pending", "READY", "deferred"):
        with pytest.raises(ValueError):
            env.card(status=status)
    # Even for an unknown source: validation precedes the lock and ownership checks.
    with pytest.raises(ValueError):
        env.card("src-gone", status="bogus")
    assert env.count() == 0


def assert_card_basis_is_the_catalog_witness_on_sqlite_and_max_stamp_in_general(env: Env) -> None:
    env.seed()
    env.source("src-empty")
    assert env.store.source_card_basis("src-empty") == ""
    env.element("e2", "2026-10-28T12:30:00+00:00")
    env.element("e3", "2026-10-28T09:15:00+00:00")
    value = env.store.source_card_basis(SRC)
    assert datetime.fromisoformat(value) == datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc)
    # Another source's elements never leak in.
    env.source("src-other")
    env.element("e9", "2026-10-29T00:00:00+00:00", "src-other")
    assert env.store.source_card_basis(SRC) == value


def assert_card_bases_batch_matches_the_single_read(env: Env) -> None:
    env.seed()
    env.source("src-b")
    env.source("src-empty")
    env.element("e-b1", "2026-10-28T12:30:00+00:00", "src-b")
    env.element("e-b2", "2026-10-28T10:00:00+00:00", "src-b")
    with env.connect() as db:
        bases = env.store.source_card_bases(db, [SRC, "src-b", "src-empty", "src-none"])
        assert env.store.source_card_bases(db, []) == {}
        many = [f"src-x-{i}" for i in range(2500)] + [SRC]
        assert env.store.source_card_bases(db, many) == {SRC: env.store.source_card_basis(SRC)}
    assert set(bases) == {SRC, "src-b"}  # no elements: absent
    assert bases[SRC] == env.store.source_card_basis(SRC)
    assert bases["src-b"] == env.store.source_card_basis("src-b")
