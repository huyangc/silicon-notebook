"""Question-answer session sharing T1 (schema + store only, no caller yet).

Mirrors backend/tests/test_conversation_share_store.py (the SQLite adapter's
own copy of this contract test) — see its module docstring for the design
doc's C-3 decision this proves. PostgreSQL's tie-break is `ordinal` (the
BY DEFAULT identity column standing in for SQLite's rowid), not `id`.
"""
from __future__ import annotations

import threading

import pytest

from app.repositories.postgres.ask_state_store import (
    AskStateStore as PostgresAskStateStore,
)
from app.repositories.postgres.migrator import PostgresMigrator
from app.services.repository_runtime import RepositoryCompatibilitySeams


pytestmark = pytest.mark.postgres_integration


def _seams() -> RepositoryCompatibilitySeams:
    lock = threading.Lock()
    counter: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        with lock:
            counter[prefix] = counter.get(prefix, 0) + 1
            return f"{prefix}-share-{counter[prefix]:04d}"

    return RepositoryCompatibilitySeams(
        new_id=new_id,
        now=lambda: "2026-01-01T00:00:09+00:00",
        copy_chunk_size=lambda: 100,
        remap_json_ids=lambda value, _mapping: value,
        in_chunk_size=lambda: 100,
    )


def _seed_conversation_with_tied_timestamps(database) -> tuple[str, str]:
    """One conversation, four answers across three distinct instants.

    ``ans-c`` and ``ans-b`` share the SAME ``created_at`` instant but are
    inserted in reverse alphabetical/physical order (``ans-c`` first, so its
    ``ordinal`` is lower) — a query that tie-broke on ``id`` instead of
    ``ordinal`` would silently put ``ans-b`` first for one of the two paths
    and not the other, and this test would catch that.
    """
    with database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s)",
            (
                "nb-share-pg",
                "n",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                "conv-share-pg",
                "nb-share-pg",
                "t",
                "user-local",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:03+00:00",
            ),
        )
        for answer_id, created_at in (
            ("ans-a", "2026-01-01T00:00:00+00:00"),
            ("ans-c", "2026-01-01T00:00:01+00:00"),
            ("ans-b", "2026-01-01T00:00:01+00:00"),
            ("ans-d", "2026-01-01T00:00:03+00:00"),
        ):
            connection.execute(
                "INSERT INTO answers "
                "(id,notebook_id,conversation_id,question,payload,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (
                    answer_id,
                    "nb-share-pg",
                    "conv-share-pg",
                    "q",
                    '{"conclusion": "c"}',
                    created_at,
                ),
            )
    return "nb-share-pg", "conv-share-pg"


def test_public_snapshot_turn_order_matches_get_conversation_bit_for_bit(
    postgres_database,
):
    assert PostgresMigrator(postgres_database).migrate() == 30
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(
        postgres_database
    )
    store = PostgresAskStateStore(postgres_database, _seams())

    authored_order = [
        turn.answer_id for turn in store.get_conversation(conversation_id).turns
    ]

    share = store.share_conversation(notebook_id, conversation_id)
    public = store.public_conversation_by_token(share["share_token"])
    public_order = [turn["answer_id"] for turn in public["turns"]]

    assert authored_order == public_order
    # Pins the actual resolved order, not just "the two paths agree with
    # each other" -- both must resolve the tied instant as ans-c before
    # ans-b (insertion/ordinal order), never the reverse and never
    # id-lexical order (which would also put ans-b before ans-c, matching
    # by accident).
    assert authored_order == ["ans-a", "ans-c", "ans-b", "ans-d"]


def test_public_snapshot_freezes_at_the_watermark_and_advances_on_reshare(
    postgres_database,
):
    """"Freeze + explicit update" (design doc §二): a turn written after the
    share call must NOT appear until the conversation is explicitly
    re-shared ("update to latest"), which reuses the same token."""
    assert PostgresMigrator(postgres_database).migrate() == 30
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(
        postgres_database
    )
    store = PostgresAskStateStore(postgres_database, _seams())

    share = store.share_conversation(notebook_id, conversation_id)
    token = share["share_token"]

    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO answers "
            "(id,notebook_id,conversation_id,question,payload,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                "ans-e",
                "nb-share-pg",
                "conv-share-pg",
                "q",
                '{"conclusion": "c"}',
                "2026-01-01T00:00:05+00:00",
            ),
        )

    frozen = store.public_conversation_by_token(token)
    assert [t["answer_id"] for t in frozen["turns"]] == [
        "ans-a", "ans-c", "ans-b", "ans-d",
    ]

    updated = store.share_conversation(notebook_id, conversation_id)
    assert updated["share_token"] == token  # same link, not a new one

    refreshed = store.public_conversation_by_token(token)
    assert [t["answer_id"] for t in refreshed["turns"]] == [
        "ans-a", "ans-c", "ans-b", "ans-d", "ans-e",
    ]


def test_unshare_revokes_the_public_link(postgres_database):
    """After ``unshare_conversation`` the token resolves to nothing — a
    revoked link 404s exactly like a token that never existed."""
    assert PostgresMigrator(postgres_database).migrate() == 30
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(
        postgres_database
    )
    store = PostgresAskStateStore(postgres_database, _seams())

    token = store.share_conversation(notebook_id, conversation_id)["share_token"]
    assert store.public_conversation_by_token(token) is not None

    store.unshare_conversation(notebook_id, conversation_id)
    assert store.public_conversation_by_token(token) is None


def test_unknown_and_blank_tokens_return_none(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 30
    store = PostgresAskStateStore(postgres_database, _seams())

    assert store.public_conversation_by_token("no-such-token") is None
    assert store.public_conversation_by_token("") is None
    assert store.public_conversation_by_token("   ") is None


def test_zero_answer_conversation_fails_closed(postgres_database):
    """The core "never serve an ungated conversation" defence: a conversation
    with no answers can be handed a token by ``share_conversation`` (the
    "at least one written answer" policy lives in the API layer, not here),
    but its watermark stays NULL — and ``public_conversation_by_token`` must
    return None on a NULL watermark rather than serve an unbounded snapshot.
    """
    assert PostgresMigrator(postgres_database).migrate() == 30
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s)",
            ("nb-empty-pg", "n", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            ("conv-empty-pg", "nb-empty-pg", "t", "user-local",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    store = PostgresAskStateStore(postgres_database, _seams())

    issued = store.share_conversation("nb-empty-pg", "conv-empty-pg")
    assert issued["share_token"]  # a token IS minted
    assert issued["shared_through_at"] == ""  # but the watermark stays NULL

    # fail closed: an ungated (NULL-watermark) conversation is never served.
    assert store.public_conversation_by_token(issued["share_token"]) is None


def test_conversation_share_state_reads_back_token_and_watermark(
    postgres_database,
):
    assert PostgresMigrator(postgres_database).migrate() == 30
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(
        postgres_database
    )
    store = PostgresAskStateStore(postgres_database, _seams())

    # Unshared: empty token, empty watermark (never raises for an existing row).
    before = store.conversation_share_state(notebook_id, conversation_id)
    assert before == {
        "share_token": "",
        "shared_through_at": "",
        "shared_through_id": "",
    }

    issued = store.share_conversation(notebook_id, conversation_id)
    after = store.conversation_share_state(notebook_id, conversation_id)
    assert after["share_token"] == issued["share_token"]
    assert after["shared_through_id"] == "ans-d"  # newest answer at share time


def test_discard_unwatermarked_share_spares_a_watermarked_link(postgres_database):
    """The conditional rollback (codex T2 review, concurrency P2): once a
    concurrent share advances the watermark, a late rollback must NOT revoke
    the now-live link. Mirrors the SQLite copy of this contract test."""
    assert PostgresMigrator(postgres_database).migrate() == 30
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s)",
            ("nb-race-pg", "n", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            ("conv-race-pg", "nb-race-pg", "t", "user-local",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    store = PostgresAskStateStore(postgres_database, _seams())

    issued_a = store.share_conversation("nb-race-pg", "conv-race-pg")
    token = issued_a["share_token"]
    assert issued_a["shared_through_at"] == ""

    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO answers "
            "(id,notebook_id,conversation_id,question,payload,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            ("ans-1", "nb-race-pg", "conv-race-pg", "q",
             '{"conclusion": "c"}', "2026-01-01T00:00:02+00:00"),
        )
    issued_b = store.share_conversation("nb-race-pg", "conv-race-pg")
    assert issued_b["share_token"] == token  # COALESCE reuse
    assert issued_b["shared_through_at"]

    store.discard_unwatermarked_share("nb-race-pg", "conv-race-pg")
    assert store.public_conversation_by_token(token) is not None
    assert store.conversation_share_state(
        "nb-race-pg", "conv-race-pg"
    )["share_token"] == token


def test_discard_unwatermarked_share_clears_a_genuinely_empty_conversation(
    postgres_database,
):
    """The other half: no answer ever landed -> watermark stays NULL -> the
    conditional rollback DOES discard the token."""
    assert PostgresMigrator(postgres_database).migrate() == 30
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s)",
            ("nb-e-pg", "n", "2026-01-01T00:00:00+00:00",
             "2026-01-01T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO conversations "
            "(id,notebook_id,title,created_by,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            ("conv-e-pg", "nb-e-pg", "t", "user-local",
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    store = PostgresAskStateStore(postgres_database, _seams())

    issued = store.share_conversation("nb-e-pg", "conv-e-pg")
    assert issued["shared_through_at"] == ""

    store.discard_unwatermarked_share("nb-e-pg", "conv-e-pg")
    assert store.conversation_share_state(
        "nb-e-pg", "conv-e-pg"
    )["share_token"] == ""
    assert store.public_conversation_by_token(issued["share_token"]) is None
