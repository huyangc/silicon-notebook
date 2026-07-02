"""Track F — governance / promotion workflow.

Covers the propose → under_review → approved/rejected state machine that
promotes a personal-KG node into the base corpus (with dedup), plus the base
strong-review gate (store_kg into a base notebook lands as 'reviewed', not
'approved') and the matching HTTP routes.
"""
import json

import pytest

from app.core.config import Settings
from app.models.schemas import Evidence, KnowledgeUpdate, NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import USABLE_STATUSES, SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _make_personal_nb(repo, name="personal"):
    return repo.create_notebook(NotebookCreate(name=name))


def _make_base_nb(repo, name="base"):
    nb = repo.create_notebook(NotebookCreate(name=name))
    repo.mark_notebook_base(nb.id)
    return nb


def _insert_claim(repo, notebook_id, name):
    """Insert a claim into a (personal) notebook via the test helper."""
    return repo._test_insert_object(
        notebook_id, "claim", {"name": name, "section_path": "1"}
    )


def _status_of(repo, object_id):
    with repo._connect() as db:
        row = db.execute(
            "SELECT status FROM knowledge_objects WHERE id=?", (object_id,)
        ).fetchone()
    return row["status"] if row else None


def _objects_in(repo, notebook_id, object_type="claim"):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT id, payload, status FROM knowledge_objects "
            "WHERE notebook_id=? AND object_type=?",
            (notebook_id, object_type),
        ).fetchall()
    return rows


# ---------------------------------------------------------------------------
# Task 1 — promotion state machine
# ---------------------------------------------------------------------------


class TestPromotionStateMachine:
    def test_propose_creates_candidate_in_proposed_state(self, repo):
        nb = _make_personal_nb(repo)
        oid = _insert_claim(repo, nb.id, "cascode raises output resistance")
        cand = repo.propose_promotion(nb.id, oid)
        assert cand["status"] == "proposed"
        assert cand["object_id"] == oid
        assert cand["notebook_id"] == nb.id
        assert cand["object_type"] == "claim"
        assert cand["id"].startswith("promo-")

    def test_propose_object_not_in_personal_notebook_raises(self, repo):
        nb = _make_personal_nb(repo)
        with pytest.raises(KeyError):
            repo.propose_promotion(nb.id, "ko-does-not-exist")

    def test_propose_unknown_notebook_raises(self, repo):
        with pytest.raises(KeyError):
            repo.propose_promotion("nb-nope", "ko-nope")

    def test_propose_from_base_notebook_raises(self, repo):
        base = _make_base_nb(repo)
        # store_kg into base lands as 'reviewed'; insert directly for the test.
        oid = repo._test_insert_object(base.id, "claim", {"name": "base claim"})
        with pytest.raises(ValueError):
            repo.propose_promotion(base.id, oid)

    def test_propose_object_already_proposed_is_idempotent(self, repo):
        nb = _make_personal_nb(repo)
        oid = _insert_claim(repo, nb.id, "g_m over g_ds sets intrinsic gain")
        first = repo.propose_promotion(nb.id, oid)
        second = repo.propose_promotion(nb.id, oid)
        assert first["id"] == second["id"]
        # Only one active row exists.
        with repo._connect() as db:
            n = db.execute(
                "SELECT COUNT(*) AS c FROM promotion_candidates WHERE object_id=?",
                (oid,),
            ).fetchone()["c"]
        assert n == 1

    def test_list_promotion_queue_returns_only_under_review_and_proposed(self, repo):
        nb = _make_personal_nb(repo)
        base = _make_base_nb(repo)
        o1 = _insert_claim(repo, nb.id, "claim one")
        o2 = _insert_claim(repo, nb.id, "claim two")
        o3 = _insert_claim(repo, nb.id, "claim three")
        c1 = repo.propose_promotion(nb.id, o1)
        c2 = repo.propose_promotion(nb.id, o2)
        c3 = repo.propose_promotion(nb.id, o3)
        repo.approve_promotion(c2["id"])  # leaves the queue
        repo.reject_promotion(c3["id"], reason="nope")  # leaves the queue
        queue = repo.list_promotion_queue()
        ids = {c["id"] for c in queue}
        assert c1["id"] in ids
        assert c2["id"] not in ids
        assert c3["id"] not in ids

    def test_list_promotion_queue_populates_payload_and_evidence(self, repo):
        nb = _make_personal_nb(repo)
        _make_base_nb(repo)
        oid = _insert_claim(repo, nb.id, "the denormalised payload claim")
        repo.propose_promotion(nb.id, oid)
        queue = repo.list_promotion_queue()
        cand = next(c for c in queue if c["object_id"] == oid)
        assert cand["payload"]["name"] == "the denormalised payload claim"
        assert isinstance(cand["evidence"], list)

    def test_list_promotion_queue_batches_object_lookup_not_n_plus_1(self, repo, monkeypatch):
        """C5: list_promotion_queue must issue ONE batched knowledge_objects
        lookup for the whole queue, not one SELECT per candidate row (was N+1).
        Spy on connection.execute call count for the notebook_id-independent
        knowledge_objects payload query."""
        nb = _make_personal_nb(repo)
        _make_base_nb(repo)
        oids = [_insert_claim(repo, nb.id, f"claim {i}") for i in range(12)]
        for oid in oids:
            repo.propose_promotion(nb.id, oid)

        # sqlite3.Connection is a C type — its .execute cannot be monkeypatched
        # directly — so wrap at the repo._connect() boundary instead.
        calls = {"knowledge_objects_selects": 0}
        orig_connect = repo._connect

        class _SpyConn:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *a, **kw):
                if "FROM knowledge_objects" in sql and "payload" in sql:
                    calls["knowledge_objects_selects"] += 1
                return self._inner.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

        monkeypatch.setattr(repo, "_connect", lambda: _SpyConn(orig_connect()))
        queue = repo.list_promotion_queue()
        assert len(queue) == 12
        assert calls["knowledge_objects_selects"] == 1, (
            f"expected exactly 1 batched knowledge_objects query for 12 "
            f"candidates, got {calls['knowledge_objects_selects']} (N+1 regression)")

    def test_list_promotion_queue_equals_per_row_oracle(self, repo):
        """Output equality oracle: batched list_promotion_queue must return the
        SAME payload/evidence per candidate as the old per-row-SELECT
        implementation (recomputed here verbatim)."""
        nb = _make_personal_nb(repo)
        _make_base_nb(repo)
        oids = [_insert_claim(repo, nb.id, f"oracle claim {i}") for i in range(5)]
        for oid in oids:
            repo.propose_promotion(nb.id, oid)

        got = repo.list_promotion_queue()

        with repo._connect() as db:
            rows = db.execute(
                "SELECT * FROM promotion_candidates "
                "WHERE status IN ('proposed','under_review') "
                "ORDER BY created_at ASC, id ASC"
            ).fetchall()
            oracle = []
            for row in rows:
                obj = db.execute(
                    "SELECT payload, evidence FROM knowledge_objects WHERE id=?",
                    (row["object_id"],),
                ).fetchone()
                payload = json.loads(obj["payload"] or "{}") if obj else {}
                evidence = (
                    [Evidence(**e) for e in json.loads(obj["evidence"] or "[]")]
                    if obj else []
                )
                oracle.append(repo._promotion_row_to_dict(row, payload=payload, evidence=evidence))

        assert got == oracle

    def test_approve_promotion_copies_object_to_base_corpus(self, repo):
        nb = _make_personal_nb(repo)
        base = _make_base_nb(repo)
        oid = _insert_claim(repo, nb.id, "approved claim copies to base")
        cand = repo.propose_promotion(nb.id, oid)
        result = repo.approve_promotion(cand["id"])
        assert result["base_object_id"]
        base_rows = _objects_in(repo, base.id, "claim")
        assert len(base_rows) == 1
        assert json.loads(base_rows[0]["payload"])["name"] == "approved claim copies to base"

    def test_approve_promotion_sets_base_object_status_approved(self, repo):
        nb = _make_personal_nb(repo)
        base = _make_base_nb(repo)
        oid = _insert_claim(repo, nb.id, "status approved on base")
        cand = repo.propose_promotion(nb.id, oid)
        result = repo.approve_promotion(cand["id"])
        assert _status_of(repo, result["base_object_id"]) == "approved"

    def test_approve_promotion_stamps_candidate_approved(self, repo):
        nb = _make_personal_nb(repo)
        _make_base_nb(repo)
        oid = _insert_claim(repo, nb.id, "candidate stamped approved")
        cand = repo.propose_promotion(nb.id, oid)
        repo.approve_promotion(cand["id"])
        with repo._connect() as db:
            row = db.execute(
                "SELECT status, reviewed_by FROM promotion_candidates WHERE id=?",
                (cand["id"],),
            ).fetchone()
        assert row["status"] == "approved"
        assert row["reviewed_by"] == "curator"

    def test_approve_promotion_is_idempotent(self, repo):
        nb = _make_personal_nb(repo)
        base = _make_base_nb(repo)
        oid = _insert_claim(repo, nb.id, "idempotent approve")
        cand = repo.propose_promotion(nb.id, oid)
        first = repo.approve_promotion(cand["id"])
        second = repo.approve_promotion(cand["id"])
        assert first["base_object_id"] == second["base_object_id"]
        # Exactly one base object was created.
        assert len(_objects_in(repo, base.id, "claim")) == 1

    def test_approve_promotion_no_base_notebook_raises(self, repo):
        nb = _make_personal_nb(repo)
        oid = _insert_claim(repo, nb.id, "no base around")
        cand = repo.propose_promotion(nb.id, oid)
        with pytest.raises(ValueError):
            repo.approve_promotion(cand["id"])

    def test_approve_promotion_deduplicates_against_existing_base_objects(self, repo):
        nb = _make_personal_nb(repo)
        base = _make_base_nb(repo)
        # Existing base object with the SAME normalized claim text.
        existing = repo._test_insert_object(
            base.id, "claim", {"name": "Cascode raises output resistance."}
        )
        # Personal object with equivalent text (different casing/punct).
        oid = _insert_claim(repo, nb.id, "cascode raises output resistance")
        cand = repo.propose_promotion(nb.id, oid)
        result = repo.approve_promotion(cand["id"])
        # No NEW base object: the personal object deduped into the existing one.
        assert len(_objects_in(repo, base.id, "claim")) == 1
        assert result["merged_into"] == existing
        assert result["base_object_id"] == existing

    def test_approve_promotion_unknown_candidate_raises(self, repo):
        with pytest.raises(KeyError):
            repo.approve_promotion("promo-nope")

    def test_approve_rejected_candidate_raises(self, repo):
        nb = _make_personal_nb(repo)
        _make_base_nb(repo)
        oid = _insert_claim(repo, nb.id, "rejected then approve")
        cand = repo.propose_promotion(nb.id, oid)
        repo.reject_promotion(cand["id"], reason="no")
        with pytest.raises(ValueError):
            repo.approve_promotion(cand["id"])

    def test_reject_promotion_leaves_personal_object_untouched(self, repo):
        nb = _make_personal_nb(repo)
        oid = _insert_claim(repo, nb.id, "personal untouched after reject")
        cand = repo.propose_promotion(nb.id, oid)
        repo.reject_promotion(cand["id"], reason="not canonical")
        # Personal object still present and status unchanged.
        assert _status_of(repo, oid) == "approved"
        assert len(_objects_in(repo, nb.id, "claim")) == 1

    def test_reject_promotion_records_reason_on_candidate(self, repo):
        nb = _make_personal_nb(repo)
        oid = _insert_claim(repo, nb.id, "records the reason")
        cand = repo.propose_promotion(nb.id, oid)
        updated = repo.reject_promotion(cand["id"], reason="duplicate of base node")
        assert updated["status"] == "rejected"
        assert updated["reason"] == "duplicate of base node"
        assert updated["reviewed_by"] == "curator"

    def test_reject_unknown_candidate_raises(self, repo):
        with pytest.raises(KeyError):
            repo.reject_promotion("promo-nope", reason="x")

    def test_reject_approved_candidate_raises(self, repo):
        nb = _make_personal_nb(repo)
        _make_base_nb(repo)
        oid = _insert_claim(repo, nb.id, "approved then reject")
        cand = repo.propose_promotion(nb.id, oid)
        repo.approve_promotion(cand["id"])
        with pytest.raises(ValueError):
            repo.reject_promotion(cand["id"], reason="too late")

    def test_rejected_object_does_not_appear_in_base_corpus(self, repo):
        nb = _make_personal_nb(repo)
        base = _make_base_nb(repo)
        oid = _insert_claim(repo, nb.id, "must not leak into base")
        cand = repo.propose_promotion(nb.id, oid)
        repo.reject_promotion(cand["id"], reason="rejected")
        # Base corpus has zero objects — nothing leaked.
        assert _objects_in(repo, base.id, "claim") == []

    def test_reproposal_allowed_after_rejection(self, repo):
        nb = _make_personal_nb(repo)
        _make_base_nb(repo)
        oid = _insert_claim(repo, nb.id, "re-proposed after reject")
        cand = repo.propose_promotion(nb.id, oid)
        repo.reject_promotion(cand["id"], reason="first time no")
        # The partial UNIQUE index excludes rejected rows, so re-proposal works.
        again = repo.propose_promotion(nb.id, oid)
        assert again["id"] != cand["id"]
        assert again["status"] == "proposed"


# ---------------------------------------------------------------------------
# Task 1 — base strong-review gate (store_kg)
# ---------------------------------------------------------------------------


class TestBaseStrongReviewGate:
    def test_store_kg_to_base_notebook_inserts_as_reviewed_not_approved(self, repo):
        base = _make_base_nb(repo)
        repo.store_kg(
            base.id,
            "s1",
            [{"local_id": "C1", "object_type": "claim",
              "payload": {"name": "base reviewed claim"}, "evidence": []}],
            [],
        )
        rows = _objects_in(repo, base.id, "claim")
        assert len(rows) == 1
        assert rows[0]["status"] == "reviewed"

    def test_store_kg_to_personal_notebook_still_inserts_as_approved(self, repo):
        nb = _make_personal_nb(repo)
        repo.store_kg(
            nb.id,
            "s1",
            [{"local_id": "C1", "object_type": "claim",
              "payload": {"name": "personal approved claim"}, "evidence": []}],
            [],
        )
        rows = _objects_in(repo, nb.id, "claim")
        assert len(rows) == 1
        assert rows[0]["status"] == "approved"

    def test_reviewed_is_in_usable_statuses(self, repo):
        # Guard: the gate only works if 'reviewed' surfaces in retrieval.
        assert "reviewed" in USABLE_STATUSES


# ---------------------------------------------------------------------------
# Task 2 — HTTP routes
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from fastapi.testclient import TestClient

    from app.api.routes import repository
    from app.main import app

    c = TestClient(app)
    # Expose the shared repository (same DB file) for seeding objects.
    c._repo = repository()  # type: ignore[attr-defined]
    return c


def _seed_personal_object(client, name="route claim"):
    nb = client.post("/api/notebooks", json={"name": "personal"}).json()
    oid = client._repo._test_insert_object(nb["id"], "claim", {"name": name})
    return nb["id"], oid


def _seed_base(client):
    nb = client.post("/api/notebooks", json={"name": "base"}).json()
    client._repo.mark_notebook_base(nb["id"])
    return nb["id"]


class TestPromotionRoutes:
    def test_propose_returns_201(self, client):
        nb_id, oid = _seed_personal_object(client)
        r = client.post(f"/api/notebooks/{nb_id}/knowledge/{oid}/promote")
        assert r.status_code == 201
        body = r.json()
        assert body["status"] == "proposed"
        assert body["object_id"] == oid

    def test_queue_lists_proposed(self, client):
        nb_id, oid = _seed_personal_object(client, name="queued claim")
        client.post(f"/api/notebooks/{nb_id}/knowledge/{oid}/promote")
        r = client.get("/api/promotion-queue")
        assert r.status_code == 200
        items = r.json()
        assert any(c["object_id"] == oid for c in items)

    def test_approve_returns_200_and_base_object_id(self, client):
        base_id = _seed_base(client)
        nb_id, oid = _seed_personal_object(client, name="approve via route")
        cand = client.post(f"/api/notebooks/{nb_id}/knowledge/{oid}/promote").json()
        r = client.post(f"/api/promotion-queue/{cand['id']}/approve")
        assert r.status_code == 200
        body = r.json()
        assert body["candidate_id"] == cand["id"]
        assert body["base_object_id"]

    def test_reject_returns_200_and_candidate_with_reason(self, client):
        nb_id, oid = _seed_personal_object(client, name="reject via route")
        cand = client.post(f"/api/notebooks/{nb_id}/knowledge/{oid}/promote").json()
        r = client.post(
            f"/api/promotion-queue/{cand['id']}/reject",
            json={"reason": "not canonical"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "rejected"
        assert body["reason"] == "not canonical"

    def test_propose_unknown_notebook_returns_404(self, client):
        r = client.post("/api/notebooks/nb-nope/knowledge/ko-nope/promote")
        assert r.status_code == 404

    def test_propose_unknown_object_returns_404(self, client):
        nb = client.post("/api/notebooks", json={"name": "empty"}).json()
        r = client.post(f"/api/notebooks/{nb['id']}/knowledge/ko-nope/promote")
        assert r.status_code == 404

    def test_propose_base_notebook_returns_400(self, client):
        base_id = _seed_base(client)
        oid = client._repo._test_insert_object(base_id, "claim", {"name": "base obj"})
        r = client.post(f"/api/notebooks/{base_id}/knowledge/{oid}/promote")
        assert r.status_code == 400

    def test_approve_unknown_candidate_returns_404(self, client):
        r = client.post("/api/promotion-queue/promo-nope/approve")
        assert r.status_code == 404

    def test_reject_unknown_candidate_returns_404(self, client):
        r = client.post("/api/promotion-queue/promo-nope/reject", json={"reason": "x"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Task 3 — base strong-review gate & promotion edge cases
# ---------------------------------------------------------------------------


def _store_claim(repo, notebook_id, name, source="s1"):
    repo.store_kg(
        notebook_id,
        source,
        [{"local_id": "C1", "object_type": "claim",
          "payload": {"name": name, "section_path": "1"}, "evidence": []}],
        [],
    )


class TestBaseReviewGateEdgeCases:
    def test_store_kg_base_objects_appear_in_retrieval_as_reviewed(self, repo):
        """Objects stored to base with status='reviewed' are in USABLE_STATUSES
        and therefore appear in list_knowledge queries."""
        base = _make_base_nb(repo)
        _store_claim(repo, base.id, "reviewed base claim about gain")
        records = repo.list_knowledge(base.id, "claim")
        assert len(records.items) == 1
        assert _objects_in(repo, base.id, "claim")[0]["status"] == "reviewed"

    def test_curator_can_upgrade_reviewed_to_approved(self, repo):
        """update_knowledge(status='approved') on a base reviewed object works."""
        base = _make_base_nb(repo)
        _store_claim(repo, base.id, "to be upgraded")
        oid = _objects_in(repo, base.id, "claim")[0]["id"]
        assert _status_of(repo, oid) == "reviewed"
        repo.update_knowledge(base.id, oid, KnowledgeUpdate(status="approved"))
        assert _status_of(repo, oid) == "approved"

    def test_ask_surfaces_base_reviewed_objects(self, repo):
        """federated_retrieve() on a personal notebook surfaces base objects at status='reviewed'.
        Regression guard for USABLE_STATUSES inclusion.
        P4-5: ask_fast retired; test now calls federated_retrieve directly."""
        from app.models.schemas import AskRequest

        base = _make_base_nb(repo)
        _store_claim(repo, base.id, "capacitance scales with area")
        personal = _make_personal_nb(repo)
        _store_claim(repo, personal.id, "personal note on capacitance")
        # All base claims are 'reviewed' (the gate); confirm before asking.
        assert all(r["status"] == "reviewed" for r in _objects_in(repo, base.id, "claim"))
        hits = repo.federated_retrieve(personal.id, "capacitance")
        all_ids = {h.object_id for h in hits}
        base_ids = {r["id"] for r in _objects_in(repo, base.id, "claim")}
        assert all_ids & base_ids, "reviewed base object did not reach the answer"

    def test_reject_promotion_does_not_affect_personal_retrieval(self, repo):
        """After rejection the personal object is still retrievable from its
        personal notebook (no side effects on the personal corpus).
        P4-5: ask_fast retired; test now calls _retrieve_scored directly."""
        from app.models.schemas import AskRequest

        _make_base_nb(repo)
        personal = _make_personal_nb(repo)
        _store_claim(repo, personal.id, "miller effect increases input capacitance")
        oid = _objects_in(repo, personal.id, "claim")[0]["id"]
        cand = repo.propose_promotion(personal.id, oid)
        repo.reject_promotion(cand["id"], reason="not canonical")
        hits = repo._retrieve_scored(personal.id, "miller effect")
        all_ids = {h.object_id for h in hits}
        assert oid in all_ids, "rejected personal object vanished from its own notebook"

    def test_rejected_object_does_not_leak_into_base_only_ask(self, repo):
        """A base-only ask() must NOT surface a rejected personal object."""
        from app.models.schemas import AskRequest

        base = _make_base_nb(repo)
        _store_claim(repo, base.id, "base only claim about noise figure")
        personal = _make_personal_nb(repo)
        _store_claim(repo, personal.id, "personal claim about noise figure")
        p_oid = _objects_in(repo, personal.id, "claim")[0]["id"]
        cand = repo.propose_promotion(personal.id, p_oid)
        repo.reject_promotion(cand["id"], reason="rejected")
        # Ask against the BASE notebook directly (base-only view).
        resp = repo.ask(base.id, AskRequest(question="noise figure"))
        all_ids = {a.object_id for a in resp.anchors}
        all_ids |= {r.id for r in resp.related_knowledge}
        assert p_oid not in all_ids, "rejected personal object leaked into base retrieval"

    def test_approve_promotion_makes_base_copy_live_in_federation(self, repo):
        """After approval the base copy is live and reachable via federated
        retrieval from the personal notebook."""
        base = _make_base_nb(repo)
        personal = _make_personal_nb(repo)
        _store_claim(repo, personal.id, "thermal noise sets the noise floor")
        p_oid = _objects_in(repo, personal.id, "claim")[0]["id"]
        cand = repo.propose_promotion(personal.id, p_oid)
        result = repo.approve_promotion(cand["id"])
        hits = repo.federated_retrieve(personal.id, "thermal noise")
        hit_ids = {h.object_id for h in hits}
        assert result["base_object_id"] in hit_ids

    def test_double_promotion_is_idempotent(self, repo):
        """propose_promotion() called twice for the same object returns the same
        candidate id without inserting a duplicate row."""
        _make_base_nb(repo)
        personal = _make_personal_nb(repo)
        _store_claim(repo, personal.id, "idempotent double propose")
        oid = _objects_in(repo, personal.id, "claim")[0]["id"]
        first = repo.propose_promotion(personal.id, oid)
        second = repo.propose_promotion(personal.id, oid)
        assert first["id"] == second["id"]
        with repo._connect() as db:
            n = db.execute(
                "SELECT COUNT(*) AS c FROM promotion_candidates WHERE object_id=?",
                (oid,),
            ).fetchone()["c"]
        assert n == 1
