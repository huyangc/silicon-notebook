"""E4-3 / M1 write-route scenarios, shared by the SQLite and PostgreSQL route tests.

Two members of one shared notebook (A = owner, B = notebook admin through a
user grant) plus a site admin R who curates the promotion queue. A has a
confirmed Memory; one knowledge object is derived from it (its primary source
is A's Memory source, its evidence quotes the Memory text). Everything below
goes through the real HTTP routes except the seeding of that object and of the
"queued before the fix" proposal, which are the data states under test.

``q`` rewrites the SQLite ``?`` placeholders for the backend under test.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from app.domain.memory_kg_isolation import (
    CROSS_CLASS_MESSAGE,
    MEMORY_PROMOTION_REJECTED_REASON,
    PROMOTION_APPROVE_MESSAGE,
    PROMOTION_OBJECT_MISSING_MESSAGE,
    PROMOTION_OBJECT_MISSING_REASON,
    PROMOTION_PROPOSE_MESSAGE,
    PUBLISH_HOLDS_MEMORY_MESSAGE,
    SAME_OWNER_MESSAGE,
)
from app.models.schemas import NotebookCreate

NOW = "2026-09-29T00:00:00+00:00"
MEMORY_TEXT = "Private loop tuning notes 私人调参笔记 zq7"
MEMORY_OBJECT = "ko-memory-derived"
MEMORY_OBJECT_2 = "ko-memory-derived-2"
ORDINARY_OBJECT = "ko-ordinary"
ORDINARY_OBJECT_2 = "ko-ordinary-2"
MEMORY_RELATION = "kr-memory-derived"
ORDINARY_RELATION = "kr-ordinary"
UNKNOWN_OBJECT = "ko-does-not-exist"
UNKNOWN_RELATION = "kr-does-not-exist"


@dataclass
class World:
    client: object
    repo: object
    q: Callable[[str], str]
    nb: str
    base: str
    a_id: str
    r_id: str
    a: dict
    b: dict
    r: dict
    memory_id: str


def _register(client, username: str) -> tuple[dict, str]:
    body = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    ).json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def build(client, repo, q) -> World:
    a, a_id = _register(client, "a00300001")
    b, b_id = _register(client, "b00300002")
    r, r_id = _register(client, "r00300003")
    nb = client.post("/api/notebooks", headers=a, json={"name": "Shared"}).json()["id"]
    base = repo.create_notebook(NotebookCreate(name="Public base"))
    repo.mark_notebook_base(base.id)
    repo.replace_notebook_bases(nb, [base.id], a_id)
    repo.add_member(nb, b_id)
    memory = repo.create_memory_candidate(
        nb, a_id, None, "req-m1-promotion", "Private title", MEMORY_TEXT,
        [], "", {}, [],
    )
    memory = repo.confirm_memory(memory.id, a_id)
    with repo._write() as db:
        db.execute(q("UPDATE users SET role='admin' WHERE id=?"), (r_id,))
        db.execute(
            q("INSERT INTO notebook_grants "
              "(id,notebook_id,principal_type,principal_id,role,created_by,created_at) "
              "VALUES (?,?,?,?,?,?,?)"),
            ("grant-m1-b", nb, "user", b_id, "admin", a_id, NOW),
        )
        for source_id, source_type, memory_id in (
            ("src-memory-a", "memory", memory.id),
            ("src-document", "file", None),
        ):
            db.execute(
                q("INSERT INTO sources(id,notebook_id,title,source_type,memory_id,"
                  "created_at,updated_at) VALUES (?,?,?,?,?,?,?)"),
                (source_id, nb, source_id, source_type, memory_id, NOW, NOW),
            )
        for object_id, source_id, name, text in (
            (MEMORY_OBJECT, "src-memory-a", "private loop tuning", MEMORY_TEXT),
            (MEMORY_OBJECT_2, "src-memory-a", "private loop margin", MEMORY_TEXT),
            (ORDINARY_OBJECT, "src-document", "shared loop stability", "Shared text."),
            (ORDINARY_OBJECT_2, "src-document", "shared loop margin", "Shared text."),
        ):
            db.execute(
                q("INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
                  "payload,evidence,source_id,created_at,updated_at) "
                  "VALUES (?,?,'claim','approved',?,?,?,?,?)"),
                (
                    object_id, nb, json.dumps({"name": name}),
                    json.dumps([{"source_id": source_id, "element_id": f"el-{source_id}",
                                 "source_title": source_id, "element_type": "paragraph",
                                 "location_label": "p1", "confidence": 1.0,
                                 "quoted_span": text}], ensure_ascii=False),
                    source_id, NOW, NOW,
                ),
            )
        for relation_id, source_id, a_obj, b_obj in (
            (MEMORY_RELATION, "src-memory-a", MEMORY_OBJECT, MEMORY_OBJECT_2),
            (ORDINARY_RELATION, "src-document", ORDINARY_OBJECT, ORDINARY_OBJECT_2),
        ):
            db.execute(
                q("INSERT INTO knowledge_relations (id,notebook_id,source_object_id,"
                  "target_object_id,edge_type,evidence,source_id,created_at) "
                  "VALUES (?,?,?,?,?,?,?,?)"),
                (relation_id, nb, a_obj, b_obj, "depends_on", "[]", source_id, NOW),
            )
    return World(client, repo, q, nb, base.id, a_id, r_id, a, b, r, memory.id)


def _rows(world: World, sql: str, params: tuple) -> list:
    with world.repo._runtime.database.connect() as db:
        return [dict(row) for row in db.execute(world.q(sql), params).fetchall()]


def _base_snapshot(world: World) -> dict:
    objects = _rows(
        world,
        "SELECT id, payload, evidence FROM knowledge_objects WHERE notebook_id=?",
        (world.base,),
    )
    provenance = _rows(
        world,
        "SELECT object_id, source_id FROM knowledge_object_sources WHERE notebook_id=?",
        (world.base,),
    )
    return {"objects": objects, "provenance": provenance}


def _graph_seqs(world: World) -> dict:
    """``graph_seq_row`` (kg_mutation_seq first) of both libraries: a refused
    approval wrote no knowledge row, so it must announce nothing."""
    with world.repo._runtime.database.connect() as db:
        return {
            nb: tuple(world.repo._runtime.unified_kg.graph_seq_row(db, nb) or ())
            for nb in (world.nb, world.base)
        }


def _assert_no_memory_text_in_base(world: World) -> None:
    for row in _base_snapshot(world)["objects"]:
        blob = json.dumps(row, ensure_ascii=False, default=str)
        assert "zq7" not in blob and "私人调参笔记" not in blob, row


def _kg_rows(world: World) -> dict:
    objects = _rows(
        world,
        "SELECT id, status, payload, evidence FROM knowledge_objects WHERE notebook_id=?",
        (world.nb,),
    )
    relations = _rows(
        world,
        "SELECT id, review_status FROM knowledge_relations WHERE notebook_id=?",
        (world.nb,),
    )
    return {
        "objects": sorted(
            (json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) for row in objects)
        ),
        "relations": sorted((row["id"], row["review_status"]) for row in relations),
    }


def _write_calls(world: World, obj: str, rel: str) -> dict:
    """Every write route that addresses a knowledge object or relation by id."""
    nb = world.nb
    return {
        "patch": ("patch", f"/api/notebooks/{nb}/knowledge/{obj}", {"status": "deprecated"}),
        "merge_from": ("post", f"/api/notebooks/{nb}/knowledge/{obj}/merge",
                       {"into_id": ORDINARY_OBJECT}),
        "merge_into": ("post", f"/api/notebooks/{nb}/knowledge/{ORDINARY_OBJECT}/merge",
                       {"into_id": obj}),
        "promote": ("post", f"/api/notebooks/{nb}/knowledge/{obj}/promote", {}),
        "review": ("post", f"/api/notebooks/{nb}/relations/{rel}/review",
                   {"status": "rejected"}),
    }


def _without(text: str, ids: tuple) -> str:
    for echoed in ids:
        text = text.replace(echoed, "<id>")
    return text


def _call(world: World, headers: dict, spec: tuple):
    method, url, body = spec
    return getattr(world.client, method)(url, headers=headers, json=body)


def non_owner_write_routes_answer_like_a_missing_id(world: World) -> None:
    """F3: for member B (a notebook admin, not the Memory's creator), every
    write route that takes an object or relation id answers for A's
    Memory-derived object/relation exactly what it answers for an id that
    does not exist — 404, the same body (modulo the id the route echoes), no
    ``X-User-Message`` — and writes nothing."""
    before = _kg_rows(world)
    memory_calls = _write_calls(world, MEMORY_OBJECT, MEMORY_RELATION)
    unknown_calls = _write_calls(world, UNKNOWN_OBJECT, UNKNOWN_RELATION)
    for name, spec in memory_calls.items():
        got = _call(world, world.b, spec)
        missing = _call(world, world.b, unknown_calls[name])
        assert got.status_code == missing.status_code == 404, (name, got.text, missing.text)
        assert _without(got.text, (MEMORY_OBJECT, MEMORY_RELATION)) == _without(
            missing.text, (UNKNOWN_OBJECT, UNKNOWN_RELATION)
        ), name
        assert got.headers.get("X-User-Message") == missing.headers.get("X-User-Message")
        assert "记忆" not in got.text, name
    assert _kg_rows(world) == before
    assert _rows(
        world, "SELECT id FROM promotion_candidates WHERE object_id=?", (MEMORY_OBJECT,)
    ) == []


def owner_keeps_the_reason(world: World) -> None:
    """The Memory's creator A still gets the 409 with the reason where the
    write is refused (merge, promote), and can edit/review her own rows."""
    before = _kg_rows(world)
    nb = world.nb
    for source_id, into_id, message in (
        (MEMORY_OBJECT, ORDINARY_OBJECT, CROSS_CLASS_MESSAGE),
        (ORDINARY_OBJECT, MEMORY_OBJECT, CROSS_CLASS_MESSAGE),
        (MEMORY_OBJECT, MEMORY_OBJECT_2, SAME_OWNER_MESSAGE),
    ):
        response = world.client.post(
            f"/api/notebooks/{nb}/knowledge/{source_id}/merge",
            headers=world.a, json={"into_id": into_id},
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == message
        assert response.headers.get("X-User-Message") == "1"
    promote = world.client.post(
        f"/api/notebooks/{nb}/knowledge/{MEMORY_OBJECT}/promote", headers=world.a, json={},
    )
    assert promote.status_code == 409, promote.text
    assert promote.json()["detail"] == PROMOTION_PROPOSE_MESSAGE
    assert promote.headers.get("X-User-Message") == "1"
    assert _kg_rows(world) == before
    assert _rows(
        world, "SELECT id FROM promotion_candidates WHERE object_id=?", (MEMORY_OBJECT,)
    ) == []
    # Her own rows stay editable by her.
    review = world.client.post(
        f"/api/notebooks/{nb}/relations/{MEMORY_RELATION}/review",
        headers=world.a, json={"status": "verified"},
    )
    assert review.status_code == 200, review.text
    patch = world.client.patch(
        f"/api/notebooks/{nb}/knowledge/{MEMORY_OBJECT}",
        headers=world.a, json={"status": "deprecated"},
    )
    assert patch.status_code == 200, patch.text


def member_b_cannot_propose_a_memory_object(world: World) -> None:
    """B gets the 404 of an unknown id (no existence probe); nothing queued."""
    response = world.client.post(
        f"/api/notebooks/{world.nb}/knowledge/{MEMORY_OBJECT}/promote",
        headers=world.b, json={},
    )
    unknown = world.client.post(
        f"/api/notebooks/{world.nb}/knowledge/{UNKNOWN_OBJECT}/promote",
        headers=world.b, json={},
    )
    assert response.status_code == unknown.status_code == 404, response.text
    assert response.json() == unknown.json()
    assert response.headers.get("X-User-Message") is None
    assert _rows(
        world, "SELECT id FROM promotion_candidates WHERE object_id=?", (MEMORY_OBJECT,)
    ) == []


def owner_cannot_propose_her_memory_object_here(world: World) -> None:
    """A, the Memory's creator, gets the 409 with the reason; nothing queued."""
    response = world.client.post(
        f"/api/notebooks/{world.nb}/knowledge/{MEMORY_OBJECT}/promote",
        headers=world.a, json={},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == PROMOTION_PROPOSE_MESSAGE
    assert response.headers.get("X-User-Message") == "1"
    assert _rows(
        world, "SELECT id FROM promotion_candidates WHERE object_id=?", (MEMORY_OBJECT,)
    ) == []


def queued_memory_proposal_is_refused_on_approval(world: World) -> None:
    with world.repo._write() as db:
        world.repo._runtime.governance.insert_promotion_candidate(
            db, "promo-legacy", world.nb, MEMORY_OBJECT, "claim", NOW,
            target_base_id=world.base,
        )
    before = _base_snapshot(world)
    seqs_before = _graph_seqs(world)
    response = world.client.post(
        "/api/promotion-queue/promo-legacy/approve", headers=world.r
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == PROMOTION_APPROVE_MESSAGE
    assert _base_snapshot(world) == before == {"objects": [], "provenance": []}
    _assert_no_memory_text_in_base(world)
    (row,) = _rows(
        world, "SELECT status, reason, reviewed_by FROM promotion_candidates WHERE id=?",
        ("promo-legacy",),
    )
    assert row["status"] == "rejected"
    assert row["reason"] == MEMORY_PROMOTION_REJECTED_REASON
    assert row["reviewed_by"] == world.r_id  # the approving admin did act
    assert _graph_seqs(world) == seqs_before  # neither library marked dirty
    queue = world.client.get("/api/promotion-queue", headers=world.r)
    assert queue.status_code == 200
    assert "promo-legacy" not in {item["id"] for item in queue.json()}
    # A second click is a plain "already rejected", never an approval.
    again = world.client.post("/api/promotion-queue/promo-legacy/approve", headers=world.r)
    assert again.status_code == 400, again.text
    assert _base_snapshot(world) == before


def withdrawn_memory_proposal_stays_withdrawn(world: World) -> None:
    """A proposal already closed (e.g. withdrawn because its Memory was
    deleted) is neither resurrected nor re-stamped by the M1 refusal."""
    with world.repo._write() as db:
        world.repo._runtime.governance.insert_promotion_candidate(
            db, "promo-withdrawn", world.nb, MEMORY_OBJECT, "claim", NOW,
            target_base_id=world.base,
        )
        db.execute(
            world.q("UPDATE promotion_candidates SET status='rejected', "
                    "reason='memory_deleted', reviewed_by='' WHERE id=?"),
            ("promo-withdrawn",),
        )
    response = world.client.post(
        "/api/promotion-queue/promo-withdrawn/approve", headers=world.r
    )
    assert response.status_code == 400, response.text
    (row,) = _rows(
        world, "SELECT status, reason, reviewed_by FROM promotion_candidates WHERE id=?",
        ("promo-withdrawn",),
    )
    assert row == {"status": "rejected", "reason": "memory_deleted", "reviewed_by": ""}
    assert _base_snapshot(world) == {"objects": [], "provenance": []}


def creator_memory_promotion_still_works(world: World) -> None:
    proposed = world.client.post(f"/api/memories/{world.memory_id}/promote", headers=world.a)
    assert proposed.status_code in (200, 201), proposed.text
    candidate_id = proposed.json()["id"]
    approved = world.client.post(
        f"/api/promotion-queue/{candidate_id}/approve", headers=world.r
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["base_object_ids"]
    assert _base_snapshot(world)["objects"]


def ordinary_object_still_promotes(world: World) -> None:
    proposed = world.client.post(
        f"/api/notebooks/{world.nb}/knowledge/{ORDINARY_OBJECT}/promote",
        headers=world.b, json={},
    )
    assert proposed.status_code == 201, proposed.text
    approved = world.client.post(
        f"/api/promotion-queue/{proposed.json()['id']}/approve", headers=world.r
    )
    assert approved.status_code == 200, approved.text
    names = {
        (json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"])["name"]
        for row in _base_snapshot(world)["objects"]
    }
    assert names == {"shared loop stability"}


def proposal_of_a_vanished_object_is_closed_on_approval(world: World) -> None:
    """Q5: the proposal exists but its object is gone. Approval answers 409
    with the reason (not the candidate-missing 404), closes the proposal as
    rejected with ``object_missing`` in the same transaction, and writes
    nothing to the public library; an unknown candidate id stays 404."""
    proposed = world.client.post(
        f"/api/notebooks/{world.nb}/knowledge/{ORDINARY_OBJECT}/promote",
        headers=world.b, json={},
    )
    assert proposed.status_code == 201, proposed.text
    candidate_id = proposed.json()["id"]
    with world.repo._write() as db:
        db.execute(world.q("DELETE FROM knowledge_relations WHERE source_object_id=? "
                           "OR target_object_id=?"), (ORDINARY_OBJECT, ORDINARY_OBJECT))
        db.execute(world.q("DELETE FROM knowledge_objects WHERE id=?"), (ORDINARY_OBJECT,))
    response = world.client.post(
        f"/api/promotion-queue/{candidate_id}/approve", headers=world.r
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == PROMOTION_OBJECT_MISSING_MESSAGE
    assert response.headers.get("X-User-Message") == "1"
    (row,) = _rows(
        world, "SELECT status, reason, reviewed_by FROM promotion_candidates WHERE id=?",
        (candidate_id,),
    )
    assert row == {"status": "rejected", "reason": PROMOTION_OBJECT_MISSING_REASON,
                   "reviewed_by": world.r_id}
    assert _base_snapshot(world) == {"objects": [], "provenance": []}
    queue = world.client.get("/api/promotion-queue", headers=world.r)
    assert candidate_id not in {item["id"] for item in queue.json()}
    missing = world.client.post("/api/promotion-queue/promo-nope/approve", headers=world.r)
    assert missing.status_code == 404, missing.text
    assert missing.json()["detail"] == "Promotion candidate not found"


def publishing_a_notebook_with_memory_is_refused(world: World) -> None:
    """F5: a deployment admin cannot publish a notebook that holds members'
    Memory as a public library; the tier is unchanged. Once the Memory
    source is gone, publishing works."""
    # R is a deployment admin; give R the notebook-admin capability the
    # route guard asks for, like any curator managing that notebook.
    with world.repo._write() as db:
        db.execute(
            world.q("INSERT INTO notebook_grants "
                    "(id,notebook_id,principal_type,principal_id,role,created_by,"
                    "created_at) VALUES (?,?,?,?,?,?,?)"),
            ("grant-m1-r", world.nb, "user", world.r_id, "admin", world.a_id, NOW),
        )
    response = world.client.post(
        f"/api/notebooks/{world.nb}/tier", headers=world.r, json={"tier": "base"}
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == PUBLISH_HOLDS_MEMORY_MESSAGE
    assert response.headers.get("X-User-Message") == "1"
    (row,) = _rows(world, "SELECT tier FROM notebooks WHERE id=?", (world.nb,))
    assert row["tier"] == "personal"
    checkup = world.repo.checkup.run(world.nb)
    assert {c.code: c.count for c in checkup.checks}["H11"] == 0  # not public

    with world.repo._write() as db:
        db.execute(world.q("UPDATE sources SET source_type='file' "
                           "WHERE notebook_id=? AND source_type='memory'"), (world.nb,))
    ok = world.client.post(
        f"/api/notebooks/{world.nb}/tier", headers=world.r, json={"tier": "base"}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["tier"] == "base"


def checkup_counts_memory_in_a_public_library(world: World) -> None:
    """F5: a public library that already holds Memory sources (published
    before this guard) shows up in the read-only checkup item H11."""
    with world.repo._write() as db:
        db.execute(world.q("UPDATE notebooks SET tier='base' WHERE id=?"), (world.nb,))
    counts = {c.code: (c.count, c.fix) for c in world.repo.checkup.run(world.nb).checks}
    assert counts["H11"] == (1, "none")
    base_counts = {c.code: c.count for c in world.repo.checkup.run(world.base).checks}
    assert base_counts["H11"] == 0

    # The natural way out for such a library is withdrawing the publication:
    # tier=personal is never refused, Memory or not (quality re-review P3-2).
    with world.repo._write() as db:
        db.execute(
            world.q("INSERT INTO notebook_grants "
                    "(id,notebook_id,principal_type,principal_id,role,created_by,"
                    "created_at) VALUES (?,?,?,?,?,?,?)"),
            ("grant-m1-r2", world.nb, "user", world.r_id, "admin", world.a_id, NOW),
        )
    withdrawn = world.client.post(
        f"/api/notebooks/{world.nb}/tier", headers=world.r, json={"tier": "personal"}
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["tier"] == "personal"
    counts = {c.code: c.count for c in world.repo.checkup.run(world.nb).checks}
    assert counts["H11"] == 0


def shared_review_surfaces_leave_memory_rows_out(world: World) -> None:
    """Quality re-review P2-1: the edge review queue and the duplicate groups
    are per-notebook shared tools. Rows derived from a member's Memory are left
    out WHOLE — for everyone, the owner included — so no one is shown an item
    they can never act on (non-owners get 404 on those writes) and ``total``
    counts only actionable edges. Controls: the shared relation is listed, and
    two shared objects with one name still form a duplicate group."""
    with world.repo._write() as db:
        for object_id, name in (
            ("ko-dup-shared-1", "private loop tuning"),   # same name as MEMORY_OBJECT
            ("ko-dup-shared-2", "shared loop margin"),    # same name as ORDINARY_OBJECT_2
        ):
            db.execute(
                world.q("INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
                        "payload,evidence,source_id,created_at,updated_at) "
                        "VALUES (?,?,'claim','approved',?,'[]','src-document',?,?)"),
                (object_id, world.nb, json.dumps({"name": name}), NOW, NOW),
            )
    for headers in (world.a, world.b):
        queue = world.client.get(
            f"/api/notebooks/{world.nb}/edge-review-queue", headers=headers
        )
        assert queue.status_code == 200, queue.text
        body = queue.json()
        ids = {item["rel_id"] for item in body["items"]}
        assert ORDINARY_RELATION in ids and MEMORY_RELATION not in ids, body
        assert body["total"] == len(body["items"]) == 1, body

        dups = world.client.get(
            f"/api/notebooks/{world.nb}/duplicates", params={"type": "claim"}, headers=headers
        )
        assert dups.status_code == 200, dups.text
        members = [{m["id"] for m in group["members"]} for group in dups.json()]
        assert {ORDINARY_OBJECT_2, "ko-dup-shared-2"} in members, members   # control
        flat = set().union(*members) if members else set()
        assert MEMORY_OBJECT not in flat and MEMORY_OBJECT_2 not in flat, members

        if headers is not world.a:
            continue  # the bell lists governance items of the notebooks one created
        # The bell's 「关系审核」 count agrees with the queue it opens: pending
        # edges only, Memory-derived ones left out the same way.
        bell = world.client.get("/api/me/pending-actions", headers=headers)
        assert bell.status_code == 200, bell.text
        edge_counts = [
            item["count"] for item in bell.json()["items"]
            if item.get("type") == "governance" and item.get("subtype") == "edge"
            and item.get("notebook_id") == world.nb
        ]
        pending = [item for item in body["items"] if item["review_status"] == "pending"]
        assert edge_counts == [len(pending)] == [1], (bell.json(), body)

    # procedure pair: the SQLite dedup read has its own procedure branch.
    with world.repo._write() as db:
        for object_id, source_id in (
            ("ko-proc-memory", "src-memory-a"),
            ("ko-proc-shared-1", "src-document"),
            ("ko-proc-shared-2", "src-document"),
        ):
            db.execute(
                world.q("INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
                        "payload,evidence,source_id,created_at,updated_at) "
                        "VALUES (?,?,'procedure','approved',?,'[]',?,?,?)"),
                (object_id, world.nb,
                 json.dumps({"name": "loop calibration", "steps": ["measure", "trim"]}),
                 source_id, NOW, NOW),
            )
    for headers in (world.a, world.b):
        procs = world.client.get(
            f"/api/notebooks/{world.nb}/duplicates", params={"type": "procedure"},
            headers=headers,
        )
        assert procs.status_code == 200, procs.text
        groups = [{m["id"] for m in group["members"]} for group in procs.json()]
        assert {"ko-proc-shared-1", "ko-proc-shared-2"} in groups, groups   # control
        assert all("ko-proc-memory" not in group for group in groups), groups
