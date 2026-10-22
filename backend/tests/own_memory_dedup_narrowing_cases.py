"""The owner's duplicate groups read in two narrow passes, with the evidence
test in SQL; shared by the SQLite and PostgreSQL twins.

The oracle is the read this replaced: every live own-Memory object of the type
with its FULL payload and evidence, the evidence judged in Python by
``evidence_only_from_own_memory`` (the merge's own reading), then the very same
blocking and grouping. On a seeded corpus mixing names that block together,
procedures, statuses, merge records and evidence shapes (own, shared, another
member's Memory, entries without a source id, non-object entries, and the empty
shapes ``[]`` / ``{}`` / ``""``), the new answer's groups must equal the
oracle's byte for byte, and ``merged_away`` must be the oracle's deprecated
objects that still carry a merge record. ``q`` rewrites ``?`` placeholders.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Callable

from app.core.request_context import reset_request_user, set_request_user
from app.domain.memory_kg_isolation import (
    MERGED_INTO_PAYLOAD_KEY,
    evidence_only_from_own_memory,
)
from app.models.schemas import NotebookCreate

NOW = "2026-10-22T00:00:00+00:00"
TYPES = ("concept", "claim", "procedure")
NAMES = ("Loop Gain", "loop  gain", "LOOP GAIN", "phase margin", "PLL",
         "phase locked loop", "Phase-Locked Loop", "ripple", "Settling Time",
         "settling time", "")


@dataclass
class World:
    repo: object
    q: Callable[[str], str]
    nb: str
    viewer: object
    other: object


def build(repo, q, *, seed: int = 1022) -> World:
    rng = random.Random(seed)
    viewer = repo._runtime.identity.create_user("a00909091", "pw123456")
    other = repo._runtime.identity.create_user("b00909092", "pw123456")
    token = set_request_user(viewer)
    try:
        nb = repo.create_notebook(NotebookCreate(name="narrowing")).id
        repo._runtime.sharing.add_member(nb, other.id)
    finally:
        reset_request_user(token)
    sources = {"own": ["src-own-1", "src-own-2"], "other": ["src-other-1"], "doc": ["src-doc"]}
    with repo._write() as db:
        for owner, memory_ids in ((viewer.id, ("m-own-1", "m-own-2")), (other.id, ("m-other-1",))):
            for memory_id in memory_ids:
                db.execute(
                    q("INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
                      "content_md,created_at,updated_at) "
                      "VALUES (?,?,?,'ask_answer','confirmed','t','c',?,?)"),
                    (memory_id, nb, owner, NOW, NOW),
                )
        for source_id, memory_id in (("src-own-1", "m-own-1"), ("src-own-2", "m-own-2"),
                                     ("src-other-1", "m-other-1"), ("src-doc", None)):
            db.execute(
                q("INSERT INTO sources(id,notebook_id,title,source_type,memory_id,"
                  "created_at,updated_at) VALUES (?,?,?,?,?,?,?)"),
                (source_id, nb, source_id, "memory" if memory_id else "file", memory_id,
                 NOW, NOW),
            )

        def entry(source_id):
            return {"source_id": source_id, "element_id": f"el-{rng.randint(0, 9)}",
                    "quoted_span": "q"}

        evidence_shapes = [
            lambda: [entry("src-own-1")],
            lambda: [entry("src-own-1"), entry("src-own-2")],
            lambda: [entry("src-own-2"), entry("src-own-2")],
            lambda: [entry("src-own-1"), entry("src-doc")],
            lambda: [entry("src-other-1")],
            lambda: [{"element_id": "el-x", "quoted_span": "no source"}],
            lambda: [{"source_id": "", "element_id": "el-y"}],
            lambda: [entry("src-own-1"), "a bare string entry"],
            lambda: [],
            lambda: {},
            lambda: "",
            # an object, not a list: the Python reading iterates its keys
            # (strings, not entries) and refuses it
            lambda: {"source_id": "src-own-1"},
        ]
        for i in range(400):
            object_type = rng.choice(TYPES)
            source_id = rng.choice(sources["own"] * 3 + sources["other"] + sources["doc"])
            payload = {"name": rng.choice(NAMES), "statement": rng.choice(["a b", "a c", ""])}
            if object_type == "procedure":
                payload["steps"] = rng.choice([["s1", "s2"], ["s1"]])
            marker = rng.random()
            if marker < 0.2:
                payload[MERGED_INTO_PAYLOAD_KEY] = f"ko-n{rng.randint(0, 399):04d}"
            elif marker < 0.25:
                payload[MERGED_INTO_PAYLOAD_KEY] = ""
            status = rng.choice(["approved", "approved", "candidate", "deprecated"])
            created = f"2026-10-{rng.randint(1, 20):02d}T00:00:00+00:00"
            db.execute(
                q("INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                  "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)"),
                (f"ko-n{i:04d}", nb, object_type, status,
                 json.dumps(payload, ensure_ascii=False),
                 json.dumps(rng.choice(evidence_shapes)(), ensure_ascii=False),
                 source_id, created, created),
            )
    return World(repo, q, nb, viewer, other)


def _parsed(value, default):
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value)


def _oracle(world: World, user, object_type: str):
    """The replaced read: full rows, Python evidence test, same grouping."""
    service = world.repo._runtime.knowledge_governance
    with world.repo._runtime.database.connect() as db:
        own = {
            row["id"] for row in db.execute(
                world.q("SELECT s.id FROM sources s JOIN memory_items m ON m.id = s.memory_id "
                        "WHERE s.notebook_id=? AND s.source_type='memory' AND m.created_by=?"),
                (world.nb, user.id),
            ).fetchall()
        }
        rows = [dict(row) for row in db.execute(
            world.q("SELECT id, status, payload, evidence, source_id, created_at "
                    "FROM knowledge_objects WHERE notebook_id=? AND object_type=?"),
            (world.nb, object_type),
        ).fetchall()]
        space = service.governance_store.published_concept_seed_space(db, world.nb)
    rows = [r for r in rows if r["source_id"] in own]
    rows.sort(key=lambda r: (str(r["created_at"]), r["id"]))
    kept = []
    for r in rows:
        payload = _parsed(r["payload"], {})
        if not evidence_only_from_own_memory(_parsed(r["evidence"], []), own):
            continue
        kept.append({"id": r["id"], "status": r["status"], "payload": payload,
                     "name": payload.get("name")})
    live = [r for r in kept if r["status"] != "deprecated"]
    by_seed = service._duplicate_blocks(object_type, live, *space)
    groups = service._duplicate_groups(object_type, by_seed, {r["id"]: r["payload"] for r in live})
    merged = [
        service._knowledge_ref(r, object_type) for r in kept
        if r["status"] == "deprecated" and r["payload"].get(MERGED_INTO_PAYLOAD_KEY)
    ]
    return groups, merged


def two_passes_answer_what_the_full_read_answered(world: World) -> None:
    service = world.repo._runtime.knowledge_governance
    compared = 0
    for user in (world.viewer, world.other):
        for object_type in TYPES:
            expected_groups, expected_merged = _oracle(world, user, object_type)
            token = set_request_user(user)
            try:
                answer = service.find_own_memory_duplicates(world.nb, object_type)
            finally:
                reset_request_user(token)
            assert json.dumps([g.model_dump() for g in answer.groups], ensure_ascii=False) == \
                json.dumps([g.model_dump() for g in expected_groups], ensure_ascii=False), \
                (user.username, object_type)
            assert [m.model_dump() for m in answer.merged_away] == \
                [m.model_dump() for m in expected_merged], (user.username, object_type)
            compared += len(expected_groups) + len(expected_merged)
    # the corpus is not vacuous: groups and merged-away objects for both users
    assert compared >= 20, compared


def blocks_follow_the_published_seed_space(world: World) -> None:
    """The owner's groups block in the notebook's PUBLISHED concept seed space,
    as the shared groups do: "Q-Former" and "Qformer" (her own Memory) share a
    seed, hence a group, only once the library publishes space 2."""
    ids = ("ko-q1", "ko-q2")
    with world.repo._write() as db:
        for object_id, name in zip(ids, ("Q-Former", "Qformer")):
            db.execute(
                world.q("INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
                        "payload,evidence,source_id,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)"),
                (object_id, world.nb, "concept", "approved",
                 json.dumps({"name": name}),
                 json.dumps([{"source_id": "src-own-1", "element_id": "el-0",
                              "quoted_span": "q"}]),
                 "src-own-1", NOW, NOW),
            )

    def groups_of_the_pair(published_space: int) -> list:
        with world.repo._write() as db:
            db.execute(world.q("DELETE FROM unified_kg_state WHERE notebook_id=?"),
                       (world.nb,))
            db.execute(
                world.q("INSERT INTO unified_kg_state (notebook_id, concept_seed_version, "
                        "updated_at) VALUES (?,?,?)"),
                (world.nb, published_space, NOW),
            )
        token = set_request_user(world.viewer)
        try:
            answer = world.repo._runtime.knowledge_governance.find_own_memory_duplicates(
                world.nb, "concept")
        finally:
            reset_request_user(token)
        return [
            sorted(member.id for member in group.members)
            for group in answer.groups
            if set(ids) <= {member.id for member in group.members}
        ]

    assert groups_of_the_pair(1) == []
    assert groups_of_the_pair(2) == [list(ids)]
