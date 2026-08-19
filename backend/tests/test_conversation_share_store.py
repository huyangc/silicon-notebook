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

import pytest

from app.core.config import Settings
from app.repositories.ports import ConversationShareWatermarkStale
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


def test_expected_through_id_pins_the_watermark_to_that_exact_answer(
    tmp_path, monkeypatch
):
    """The disclosure TOCTOU fix (codex #522 R2 P1 + P2): sharing with an
    explicit ``expected_through_id`` pins the watermark to EXACTLY that answer,
    and the keyset boundary excludes both later answers AND the tie-break-later
    answer sharing the watermark's instant.

    Order is [ans-a(t0), ans-c(t1), ans-b(t1), ans-d(t3)]. Pinning to ans-c
    (the earlier-inserted of the t1 tie) must publish only [ans-a, ans-c]:
      * ans-d (t3) is later — excluded, proving we did NOT jump to "latest";
      * ans-b shares ans-c's instant but sorts AFTER it (higher rowid) — the
        pure ``created_at <= watermark`` predicate would wrongly include it;
        the keyset excludes it.

    Mutation guard: replacing the keyset with the pure timestamp interval makes
    ans-b appear (public becomes [ans-a, ans-c, ans-b]); ignoring
    ``expected_through_id`` and pinning to latest makes it [ans-a..ans-d].
    """
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    share = store.share_conversation(
        notebook_id, conversation_id, expected_through_id="ans-c"
    )
    assert share["shared_through_id"] == "ans-c"

    public = store.public_conversation_by_token(share["share_token"])
    assert [t["answer_id"] for t in public["turns"]] == ["ans-a", "ans-c"]


def test_expected_through_id_that_no_longer_resolves_is_rejected(
    tmp_path, monkeypatch
):
    """Safe direction on a deleted disclosed boundary (codex #522 R2 P1): the
    store raises ``ConversationShareWatermarkStale`` (the route maps it to 409)
    rather than silently pinning to "latest" and publishing an unreviewed span.
    No token is issued."""
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    with pytest.raises(ConversationShareWatermarkStale):
        store.share_conversation(
            notebook_id, conversation_id, expected_through_id="ans-gone"
        )
    # Nothing was shared — the raise happens before the token UPDATE.
    assert store.conversation_share_state(
        notebook_id, conversation_id
    )["share_token"] == ""


def test_share_watermark_is_advance_only_rejecting_a_stale_expected(
    tmp_path, monkeypatch
):
    """The watermark may only ADVANCE (codex #522 R3). Once a share pins it to a
    newer answer, a stale request pinning to an OLDER answer must be rejected
    (``ConversationShareWatermarkStale`` → 409) rather than silently REGRESS the
    published link and unpublish turns another share already made public.

    Two-tab scenario: tab A shares up to ans-d (latest); tab B, holding a stale
    view, POSTs expected=ans-a. It must be rejected and the watermark must stay
    at ans-d.

    Mutation guard: dropping the advance-only comparison makes the stale reshare
    succeed and the watermark regress to ans-a.
    """
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    store.share_conversation(notebook_id, conversation_id, expected_through_id="ans-d")

    with pytest.raises(ConversationShareWatermarkStale):
        store.share_conversation(
            notebook_id, conversation_id, expected_through_id="ans-a"
        )
    assert store.conversation_share_state(
        notebook_id, conversation_id
    )["shared_through_id"] == "ans-d"


def test_share_watermark_rejects_a_tiebreak_earlier_answer(tmp_path, monkeypatch):
    """Advance-only respects the canonical tie-break, not just the timestamp
    (codex #522 R3). ans-c and ans-b share the same instant t1, ordered
    ans-c → ans-b by rowid. Pinning to ans-b then requesting ans-a's tie-mate
    ans-c (earlier in canonical order, SAME instant) is a REGRESSION and must be
    rejected.

    Mutation guard: a pure ``created_at`` comparison (dropping the rowid term)
    would treat ans-c and ans-b as equal and let the reshare succeed."""
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    store.share_conversation(notebook_id, conversation_id, expected_through_id="ans-b")

    with pytest.raises(ConversationShareWatermarkStale):
        store.share_conversation(
            notebook_id, conversation_id, expected_through_id="ans-c"
        )
    assert store.conversation_share_state(
        notebook_id, conversation_id
    )["shared_through_id"] == "ans-b"


def test_resharing_the_same_watermark_is_an_idempotent_no_op(tmp_path, monkeypatch):
    """Re-sharing the SAME boundary is not a regression — it is the normal
    "share this exact snapshot again" and must return the current state without
    raising (codex #522 R3)."""
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    first = store.share_conversation(
        notebook_id, conversation_id, expected_through_id="ans-c"
    )
    again = store.share_conversation(
        notebook_id, conversation_id, expected_through_id="ans-c"
    )
    assert again["shared_through_id"] == "ans-c"
    assert again["share_token"] == first["share_token"]


def test_share_watermark_still_advances_forward(tmp_path, monkeypatch):
    """The advance-only guard must not block a legitimate forward move: pin to an
    early answer, then reshare to a later one — the watermark advances."""
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    store.share_conversation(notebook_id, conversation_id, expected_through_id="ans-a")
    advanced = store.share_conversation(
        notebook_id, conversation_id, expected_through_id="ans-d"
    )
    assert advanced["shared_through_id"] == "ans-d"


def test_public_snapshot_falls_back_to_timestamp_when_boundary_answer_deleted(
    tmp_path, monkeypatch
):
    """If the watermark answer is deleted AFTER a share, the keyset has no
    anchor — but the already-shared link must NOT fail closed to 404. It falls
    back to the pure ``created_at`` interval (design edge case). Mutation guard:
    failing closed here (returning None) reds this."""
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    # Default share pins to the latest answer (ans-d, t3).
    token = store.share_conversation(notebook_id, conversation_id)["share_token"]

    db = repo._runtime.database
    with db.write() as conn:
        conn.execute("DELETE FROM answers WHERE id='ans-d'")

    public = store.public_conversation_by_token(token)
    assert public is not None  # not fail-closed
    # created_at <= t3 fallback: every surviving answer is at or before t3.
    assert [t["answer_id"] for t in public["turns"]] == ["ans-a", "ans-c", "ans-b"]


def test_unshare_revokes_the_public_link(tmp_path, monkeypatch):
    """After ``unshare_conversation`` the token resolves to nothing — a
    revoked link 404s exactly like a token that never existed."""
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    token = store.share_conversation(notebook_id, conversation_id)["share_token"]
    assert store.public_conversation_by_token(token) is not None

    store.unshare_conversation(notebook_id, conversation_id)
    assert store.public_conversation_by_token(token) is None


def test_unknown_and_blank_tokens_return_none(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

    assert store.public_conversation_by_token("no-such-token") is None
    assert store.public_conversation_by_token("") is None
    assert store.public_conversation_by_token("   ") is None


def test_zero_answer_conversation_fails_closed(tmp_path, monkeypatch):
    """The core "never serve an ungated conversation" defence: a conversation
    with no answers can be handed a token by ``share_conversation`` (the
    "at least one written answer" policy lives in the API layer, not here),
    but its watermark stays NULL — and ``public_conversation_by_token`` must
    return None on a NULL watermark rather than serve an unbounded snapshot.
    """
    repo = _repo(tmp_path, monkeypatch)
    db = repo._runtime.database
    with db.write() as conn:
        conn.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-empty', 'n', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO conversations "
            "(id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES ('conv-empty', 'nb-empty', 't', 'user-local', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
    store = AskStateStore(db, repo._runtime.seams)

    issued = store.share_conversation("nb-empty", "conv-empty")
    assert issued["share_token"]  # a token IS minted
    assert issued["shared_through_at"] == ""  # but the watermark stays NULL

    # fail closed: an ungated (NULL-watermark) conversation is never served.
    assert store.public_conversation_by_token(issued["share_token"]) is None


def test_conversation_share_state_reads_back_token_and_watermark(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path, monkeypatch)
    notebook_id, conversation_id = _seed_conversation_with_tied_timestamps(repo)
    store = AskStateStore(repo._runtime.database, repo._runtime.seams)

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


def test_discard_unwatermarked_share_spares_a_watermarked_link(
    tmp_path, monkeypatch
):
    """The conditional rollback (codex T2 review, concurrency P2): once a
    concurrent share has advanced the watermark, a late rollback must NOT
    revoke the now-live link. Interleaving reproduced at the store layer:

      A: share_conversation on a zero-answer conversation -> token, NULL wm
      (first answer lands)
      B: share_conversation -> reuses A's token, sets a real watermark
      A: discard_unwatermarked_share -> must be a no-op (wm no longer NULL)
    """
    repo = _repo(tmp_path, monkeypatch)
    db = repo._runtime.database
    with db.write() as conn:
        conn.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-race', 'n', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO conversations "
            "(id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES ('conv-race', 'nb-race', 't', 'user-local', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
    store = AskStateStore(db, repo._runtime.seams)

    # A shares while the conversation is still empty: token minted, watermark NULL.
    issued_a = store.share_conversation("nb-race", "conv-race")
    token = issued_a["share_token"]
    assert issued_a["shared_through_at"] == ""

    # The first answer lands, then B shares: same token, real watermark.
    with db.write() as conn:
        conn.execute(
            "INSERT INTO answers "
            "(id, notebook_id, conversation_id, question, payload, created_at) "
            "VALUES ('ans-1', 'nb-race', 'conv-race', 'q', "
            "'{\"conclusion\": \"c\"}', '2026-01-01T00:00:02')"
        )
    issued_b = store.share_conversation("nb-race", "conv-race")
    assert issued_b["share_token"] == token  # COALESCE reuse
    assert issued_b["shared_through_at"]  # B handed a live link to its caller

    # A's late rollback must spare B's now-watermarked link.
    store.discard_unwatermarked_share("nb-race", "conv-race")
    assert store.public_conversation_by_token(token) is not None
    state = store.conversation_share_state("nb-race", "conv-race")
    assert state["share_token"] == token


def test_discard_unwatermarked_share_clears_a_genuinely_empty_conversation(
    tmp_path, monkeypatch
):
    """The other half: when no answer ever landed, the watermark stays NULL and
    the conditional rollback DOES discard the token — an empty conversation
    must not keep a public link."""
    repo = _repo(tmp_path, monkeypatch)
    db = repo._runtime.database
    with db.write() as conn:
        conn.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-e', 'n', '2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
        conn.execute(
            "INSERT INTO conversations "
            "(id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES ('conv-e', 'nb-e', 't', 'user-local', "
            "'2026-01-01T00:00:00', '2026-01-01T00:00:00')"
        )
    store = AskStateStore(db, repo._runtime.seams)

    issued = store.share_conversation("nb-e", "conv-e")
    assert issued["shared_through_at"] == ""  # NULL watermark

    store.discard_unwatermarked_share("nb-e", "conv-e")
    assert store.conversation_share_state("nb-e", "conv-e")["share_token"] == ""
    assert store.public_conversation_by_token(issued["share_token"]) is None
