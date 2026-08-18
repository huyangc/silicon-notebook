"""Question-answer session sharing T1 (schema + store only, no caller yet).

Design doc's C-3 decision: the public-share snapshot must read a
conversation's turns in the EXACT same order the author sees them via
``get_conversation``. That contract is implemented as one shared module
constant (``CONVERSATION_ANSWERS_ORDER_ASC``) consumed by both queries — this
test proves the two queries actually agree, on data deliberately shaped to
exercise the tie-break (not just the common case where ``created_at`` alone
already orders every row).

See docs/superpowers/specs/2026-08-18-conversation-sharing-design_zh.md §3.2.
"""
from __future__ import annotations

from app.core.config import Settings
from app.repositories.sqlite.ask_state_store import AskStateStore
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path, monkeypatch) -> SQLiteRepository:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    return SQLiteRepository(Settings())


def _seed_conversation_with_tied_timestamps(repo: SQLiteRepository) -> tuple[str, str]:
    """One conversation, four answers across three distinct instants.

    ``ans-c`` and ``ans-b`` share the SAME ``created_at`` instant but are
    inserted in reverse alphabetical/physical order (``ans-c`` first, so its
    ``rowid`` is lower) — a query that tie-broke on ``id`` instead of
    ``rowid`` would silently put ``ans-b`` first for one of the two paths
    and not the other, and this test would catch that.
    """
    db = repo._runtime.database
    with db.write() as conn:
        conn.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-share', 'n', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO conversations "
            "(id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES ('conv-share', 'nb-share', 't', 'user-local', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:03')"
        )
        for answer_id, created_at in (
            ("ans-a", "2026-01-01T00:00:00"),
            ("ans-c", "2026-01-01T00:00:01"),
            ("ans-b", "2026-01-01T00:00:01"),
            ("ans-d", "2026-01-01T00:00:03"),
        ):
            conn.execute(
                "INSERT INTO answers "
                "(id, notebook_id, conversation_id, question, payload, created_at) "
                "VALUES (?, 'nb-share', 'conv-share', 'q', "
                "'{\"conclusion\": \"c\"}', ?)",
                (answer_id, created_at),
            )
    return "nb-share", "conv-share"


def test_public_snapshot_turn_order_matches_get_conversation_bit_for_bit(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    authored_order = [
        turn.answer_id for turn in store.get_conversation(conversation_id).turns
    ]

    share = store.share_conversation(notebook_id, conversation_id)
    public = store.public_conversation_by_token(share["share_token"])
    public_order = [turn["answer_id"] for turn in public["turns"]]

    assert authored_order == public_order
    # Pins the actual resolved order, not just "the two paths agree with
    # each other" -- both must resolve the tied instant as ans-c before
    # ans-b (insertion/rowid order), never the reverse and never id-lexical
    # order (which would also put ans-b before ans-c, matching by accident).
    assert authored_order == ["ans-a", "ans-c", "ans-b", "ans-d"]


def test_public_snapshot_freezes_at_the_watermark_and_advances_on_reshare(
    tmp_path, monkeypatch
):
    """"Freeze + explicit update" (design doc §二): a turn written after the
    share call must NOT appear until the conversation is explicitly
    re-shared ("update to latest"), which reuses the same token."""
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    share = store.share_conversation(notebook_id, conversation_id)
    token = share["share_token"]

    db = repo._runtime.database
    with db.write() as conn:
        conn.execute(
            "INSERT INTO answers "
            "(id, notebook_id, conversation_id, question, payload, created_at) "
            "VALUES ('ans-e', 'nb-share', 'conv-share', 'q', "
            "'{\"conclusion\": \"c\"}', '2026-01-01T00:00:05')"
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
