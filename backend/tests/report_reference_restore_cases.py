"""Backend-neutral world and scenarios for the report reference restore
(``CandidateRetrievalService.restore_report_references``, plan 2026-10-21 T4).

A follow-up question about a finished deep report reads the report's cited
items back from the notebook's CURRENT text. The read belongs to the retrieval
layer, so every scenario here calls the restore DIRECTLY, under the real
default ceiling the Ask path installs (``default_ceiling_context`` over the
production readers) and without the report link verdict in front of it: what
comes back is exactly what the retrieval layer itself admits.

The world (Alice asks; Bob is another member of her notebook):

* ``nb``: two ordinary sources ``src-a`` / ``src-b`` with a passage, elements
  and knowledge objects each; a relation ``rel-a`` (``kg-a`` -> ``kg-a2``)
  whose evidence is ``el-a2``; Alice's confirmed Memory ``mem-alice`` (and her
  candidate ``mem-alice-cand``), projected as ``src-mem-alice`` with a passage;
  Bob's confirmed Memory ``mem-bob``, projected as ``src-mem-bob`` with a
  passage, an element and a Memory-derived knowledge object ``kg-bob``;
* ``lib``: a library ``nb`` mounts, with a source, a passage, an element and a
  knowledge object;
* ``other``: a library nobody mounted, with a source, a passage and an element.

``tests/test_report_reference_restore.py`` runs these on SQLite and
``tests/postgres/test_report_reference_restore_pg.py`` on PostgreSQL.
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.domain.report_followup import CitedReportReference, RestoredReportEvidence
from app.models.notebooks import NotebookCreate
from app.services.source_scope import (
    default_ceiling_context,
    memory_access_context,
    source_scope_context,
)
from tests.test_default_source_ceiling import real_readers

NOW = "2026-10-21T00:00:00+00:00"


@dataclass
class World:
    repo: Any
    nb: str
    lib: str
    other: str
    alice: Any
    bob: Any
    ph: str
    js: str

    def write(self):
        return self.repo._write()

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self.write() as db:
            db.execute(sql.replace("{ph}", self.ph).replace("{js}", self.js), params)


def _evidence(source_id: str, element_id: str, quote: str) -> list[dict]:
    return [{
        "source_id": source_id, "source_title": "", "element_id": element_id,
        "element_type": "paragraph", "location_label": "p1",
        "quoted_span": quote, "confidence": 1.0,
    }]


def build_world(repo, *, postgres: bool) -> World:
    identity = repo._runtime.identity
    alice = identity.create_user("a00777001", "password-12")
    bob = identity.create_user("b00777002", "password-12")
    token = set_request_user(alice)
    try:
        nb = repo.create_notebook(NotebookCreate(name="报告库")).id
        lib = repo.create_notebook(NotebookCreate(name="参考库")).id
        other = repo.create_notebook(NotebookCreate(name="未挂载库")).id
    finally:
        reset_request_user(token)
    repo._runtime.sharing.add_member(nb, bob.id)
    world = World(
        repo=repo, nb=nb, lib=lib, other=other, alice=alice, bob=bob,
        ph="%s" if postgres else "?", js="%s::jsonb" if postgres else "?",
    )
    sources = repo._runtime.source_store

    def source(notebook_id, source_id, source_type="pdf", memory_id=""):
        sources.insert_source(
            source_id=source_id, notebook_id=notebook_id, title=f"{source_id} 标题",
            source_type=source_type, status="active", parse_status="parsed",
            file_name="", file_path="", file_size=0, file_hash="",
            summary="", doc_type="", memory_id=memory_id,
        )

    def memory(notebook_id, owner, memory_id, status="confirmed"):
        world.execute(
            "INSERT INTO memory_items (id,notebook_id,created_by,agent_profile_id,"
            "source_answer_id,origin,status,title,content_md,created_at,updated_at) "
            "VALUES ({ph},{ph},{ph},NULL,NULL,'ask_answer',{ph},{ph},{ph},{ph},{ph})",
            (memory_id, notebook_id, owner, status, f"{memory_id} 题目",
             f"{memory_id} 原文", NOW, NOW),
        )

    def element(source_id, element_id, text):
        world.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES ({ph},{ph},'paragraph','p1',{ph},{js},{ph})",
            (element_id, source_id, text, "{}", NOW),
        )

    def chunk(notebook_id, source_id, chunk_id, text, element_ids):
        world.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES ({ph},{ph},{ph},{ph},'节',{js},{ph})",
            (chunk_id, notebook_id, source_id, text, json.dumps(element_ids), NOW),
        )

    def knowledge(notebook_id, object_id, source_id, element_id, name):
        evidence = _evidence(source_id, element_id, name)
        world.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,"
            "payload,evidence,source_id,created_at,updated_at) "
            "VALUES ({ph},{ph},'concept','approved','',{js},{js},{ph},{ph},{ph})",
            (object_id, notebook_id, json.dumps({"name": name}),
             json.dumps(evidence), source_id, NOW, NOW),
        )
        world.execute(
            "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
            "VALUES ({ph},{ph},{ph})",
            (object_id, source_id, notebook_id),
        )

    source(nb, "src-a")
    source(nb, "src-b")
    memory(nb, alice.id, "mem-alice")
    memory(nb, alice.id, "mem-alice-cand", status="candidate")
    source(nb, "src-mem-alice", "memory", memory_id="mem-alice")
    memory(nb, bob.id, "mem-bob")
    source(nb, "src-mem-bob", "memory", memory_id="mem-bob")
    source(lib, "lib-src")
    source(other, "other-src")

    element("src-a", "el-a1", "甲元素原文")
    element("src-a", "el-a2", "甲关系证据原文")
    element("src-b", "el-b1", "乙元素原文")
    element("src-mem-bob", "el-mem-bob", "鲍勃私人记忆元素")
    element("lib-src", "el-lib", "参考库元素原文")
    element("other-src", "el-other", "未挂载库元素")

    chunk(nb, "src-a", "c-a", "甲段落原文", ["el-a1", "el-a2"])
    chunk(nb, "src-b", "c-b", "乙段落原文", ["el-b1"])
    chunk(nb, "src-mem-alice", "c-mem-alice", "爱丽丝记忆段落", [])
    chunk(nb, "src-mem-bob", "c-mem-bob", "鲍勃私人记忆段落", ["el-mem-bob"])
    chunk(lib, "lib-src", "c-lib", "参考库段落原文", ["el-lib"])
    chunk(other, "other-src", "c-other", "未挂载库段落", ["el-other"])

    knowledge(nb, "kg-a", "src-a", "el-a1", "甲概念")
    knowledge(nb, "kg-a2", "src-a", "el-a2", "甲二概念")
    knowledge(nb, "kg-b", "src-b", "el-b1", "乙概念")
    knowledge(nb, "kg-bob", "src-mem-bob", "el-mem-bob", "鲍勃记忆概念")
    knowledge(lib, "kg-lib", "lib-src", "el-lib", "参考库概念")
    knowledge(other, "kg-other", "other-src", "el-other", "未挂载库概念")
    world.execute(
        "INSERT INTO knowledge_relations (id,notebook_id,source_id,source_object_id,"
        "target_object_id,edge_type,evidence,created_at) "
        "VALUES ({ph},{ph},{ph},{ph},{ph},'supports',{js},{ph})",
        ("rel-a", nb, "src-a", "kg-a", "kg-a2",
         json.dumps(_evidence("src-a", "el-a2", "关系")), NOW),
    )
    world.execute(
        "INSERT INTO notebook_bases (notebook_id,base_notebook_id,created_at,created_by) "
        "VALUES ({ph},{ph},{ph},{ph})",
        (nb, lib, NOW, alice.id),
    )
    return world


def ref(key: str, object_type: str, object_id: str, *, notebook_id: str = "",
        source_id: str = "", element_id: str = "") -> CitedReportReference:
    return CitedReportReference(
        key=key, object_type=object_type, object_id=object_id,
        notebook_id=notebook_id, source_id=source_id, element_id=element_id,
    )


@contextmanager
def asked_by(world: World, user: Any = None, **ceiling: Any) -> Iterator[Any]:
    """The Ask run's frame: the asker is the request user (the participant
    set is resolved for him) and the production default ceiling is
    installed for him over ``nb``."""
    user = user or world.alice
    token = set_request_user(user)
    try:
        with default_ceiling_context(
            world.nb, user.id, real_readers(world.repo), **ceiling,
        ):
            yield world.repo.retrieval
    finally:
        reset_request_user(token)


def restore(world: World, cited: list[CitedReportReference], user: Any = None,
            **ceiling: Any) -> RestoredReportEvidence:
    user = user or world.alice
    with asked_by(world, user, **ceiling) as retrieval:
        return retrieval.restore_report_references(world.nb, user.id, cited)


def _restored_ids(restored: RestoredReportEvidence) -> set[tuple[str, str]]:
    return set(restored.key_by_object)


# --------------------------------------------------------------- scenarios
def scenario_each_kind_reads_back_the_current_text(world: World) -> None:
    """A passage, an element, a knowledge object and the asker's own Memory
    each read back once -- with the text they hold NOW, not when the report
    was written -- and each object names the report keys pointing at it."""
    world.execute("UPDATE chunks SET text={ph} WHERE id={ph}", ("甲段落改写后", "c-a"))
    world.execute(
        "UPDATE source_elements SET text={ph} WHERE id={ph}", ("甲元素改写后", "el-a1"),
    )
    world.execute(
        "UPDATE knowledge_objects SET payload={js} WHERE id={ph}",
        (json.dumps({"name": "甲概念改写后"}), "kg-a"),
    )
    world.execute(
        "UPDATE memory_items SET content_md={ph} WHERE id={ph}",
        ("爱丽丝记忆改写后", "mem-alice"),
    )

    restored = restore(world, [
        ref("k1", "chunk", "c-a", source_id="src-a"),
        ref("k2", "element", "el-a1", source_id="src-a", element_id="el-a1"),
        ref("k3", "concept", "kg-a", source_id="src-a"),
        ref("k4", "memory", "mem-alice"),
        ref("k5", "chunk", "c-a", source_id="src-a"),
    ])

    assert [chunk.text for chunk in restored.chunks] == ["甲段落改写后"]
    assert restored.chunks[0].element_ids == ["el-a1", "el-a2"]
    assert restored.chunks[0].notebook_id == world.nb
    assert [element.text for element in restored.elements] == ["甲元素改写后"]
    assert restored.elements[0].source_title == "src-a 标题"
    assert [hit.payload["name"] for hit in restored.knowledge] == ["甲概念改写后"]
    assert [hit.text for hit in restored.memory] == ["爱丽丝记忆改写后"]
    assert restored.memory[0].status == "confirmed"
    assert dict(restored.key_by_object) == {
        ("chunk", "c-a"): ("k1", "k5"),
        ("element", "el-a1"): ("k2",),
        ("kg", "kg-a"): ("k3",),
        ("memory", "mem-alice"): ("k4",),
    }
    assert restored.gone == 0


def scenario_mounted_library_items_carry_their_library(world: World) -> None:
    restored = restore(world, [
        ref("k1", "chunk", "c-lib", notebook_id=world.lib, source_id="lib-src"),
        ref("k2", "element", "el-lib", notebook_id=world.lib, source_id="lib-src"),
        ref("k3", "concept", "kg-lib", notebook_id=world.lib, source_id="lib-src"),
    ])

    assert [(c.chunk_id, c.notebook_id) for c in restored.chunks] == [
        ("c-lib", world.lib),
    ]
    assert [(e.element_id, e.notebook_id) for e in restored.elements] == [
        ("el-lib", world.lib),
    ]
    assert [(k.object_id, k.notebook_id) for k in restored.knowledge] == [
        ("kg-lib", world.lib),
    ]
    assert restored.gone == 0


def scenario_deleted_and_reparsed_items_are_gone(world: World) -> None:
    """A source deleted since the report, and a source re-parsed into new
    passage/element ids: the old ids read back nothing and each counts as
    gone; the new ids are not substituted."""
    for table, column in (
        ("chunks", "source_id"), ("source_elements", "source_id"),
        ("knowledge_object_sources", "source_id"),
    ):
        world.execute(f"DELETE FROM {table} WHERE {column}={{ph}}", ("src-b",))
    world.execute("DELETE FROM knowledge_objects WHERE id={ph}", ("kg-b",))
    world.execute("DELETE FROM sources WHERE id={ph}", ("src-b",))
    # Re-parse of src-a: new passage and element ids.
    world.execute("DELETE FROM chunks WHERE id={ph}", ("c-a",))
    world.execute("DELETE FROM source_elements WHERE id={ph}", ("el-a1",))
    world.execute(
        "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,"
        "created_at) VALUES ({ph},{ph},{ph},{ph},'节',{js},{ph})",
        ("c-a-new", world.nb, "src-a", "甲段落新解析", json.dumps([]), NOW),
    )

    restored = restore(world, [
        ref("k1", "chunk", "c-b", source_id="src-b"),
        ref("k2", "element", "el-b1", source_id="src-b"),
        ref("k3", "concept", "kg-b", source_id="src-b"),
        ref("k4", "chunk", "c-a", source_id="src-a"),
        ref("k5", "element", "el-a1", source_id="src-a"),
        ref("k6", "element", "el-a2", source_id="src-a"),
    ])

    assert _restored_ids(restored) == {("element", "el-a2")}
    assert restored.gone == 5


def scenario_outside_the_ceiling_reads_nothing(world: World) -> None:
    """Called directly, with no verdict in front of it: what the run's ceiling
    leaves out does not read back, whatever the request claims."""
    narrowed = {"mode": "include", "source_ids": ["src-a"], "narrowed": True}
    cited = [
        ref("k1", "chunk", "c-a", source_id="src-a"),
        ref("k2", "chunk", "c-b", source_id="src-b"),
        ref("k3", "element", "el-b1", source_id="src-b"),
        ref("k4", "concept", "kg-b", source_id="src-b"),
        # The asker's own Memory: a narrowed run reads no Memory.
        ref("k5", "memory", "mem-alice"),
        ref("k6", "chunk", "c-mem-alice", source_id="src-mem-alice"),
        # Lie about their source: both live in src-b.
        ref("k7", "chunk", "c-b", source_id="src-a"),
        ref("k8", "element", "el-b1", source_id="src-a"),
    ]
    restored = restore(world, cited, local_scope=narrowed)
    assert _restored_ids(restored) == {("chunk", "c-a")}
    assert restored.gone == 7

    # The control: the same references under the all-selected ceiling.
    wide = restore(world, cited)
    assert _restored_ids(wide) == {
        ("chunk", "c-a"), ("chunk", "c-b"), ("element", "el-b1"),
        ("kg", "kg-b"), ("memory", "mem-alice"), ("chunk", "c-mem-alice"),
    }
    assert wide.key_by_object[("chunk", "c-b")] == ("k2",)
    assert wide.key_by_object[("element", "el-b1")] == ("k3",)
    assert wide.gone == 2  # k7 and k8 still lie about their source

    # An unchecked reference library contributes nothing.
    unchecked = restore(world, [
        ref("k1", "chunk", "c-lib", notebook_id=world.lib, source_id="lib-src"),
        ref("k2", "element", "el-lib", notebook_id=world.lib, source_id="lib-src"),
        ref("k3", "concept", "kg-lib", notebook_id=world.lib, source_id="lib-src"),
    ], base_scope={"mode": "exclude", "notebook_ids": [world.lib], "narrowed": True})
    assert _restored_ids(unchecked) == set()
    assert unchecked.gone == 3

    # A library nobody mounted, named honestly or passed off as this notebook
    # (or as the mounted library).
    foreign = restore(world, [
        ref("k1", "chunk", "c-other", notebook_id=world.other, source_id="other-src"),
        ref("k2", "chunk", "c-other", source_id="other-src"),
        ref("k3", "element", "el-other", source_id="other-src"),
        ref("k4", "element", "el-other", notebook_id=world.lib, source_id="other-src"),
        ref("k5", "concept", "kg-lib", source_id="lib-src"),
    ])
    assert _restored_ids(foreign) == set()
    assert foreign.gone == 5

    # Under a scope that freezes no library and admits every source id, an
    # unmounted library's items still read back nothing: the participant set,
    # the passage statement's notebook predicate and the element / knowledge
    # object ownership checks are the restore's own, not only the default
    # ceiling's.
    token = set_request_user(world.alice)
    try:
        with source_scope_context(world.nb, {
            "mode": "exclude", "source_ids": [], "narrowed": False,
        }):
            unmounted = world.repo.retrieval.restore_report_references(
                world.nb, world.alice.id, [
                    ref("k1", "chunk", "c-other", notebook_id=world.other,
                        source_id="other-src"),
                    ref("k2", "element", "el-other", notebook_id=world.other,
                        source_id="other-src"),
                    ref("k3", "chunk", "c-lib", notebook_id=world.lib,
                        source_id="lib-src"),
                    ref("k4", "element", "el-other", source_id="other-src"),
                    ref("k5", "concept", "kg-other", source_id="other-src"),
                    ref("k6", "chunk", "c-other", source_id="other-src"),
                ],
            )
    finally:
        reset_request_user(token)
    assert _restored_ids(unmounted) == {("chunk", "c-lib")}
    assert unmounted.gone == 5


def scenario_other_members_memory_never_reads_back(world: World) -> None:
    """Bob's Memory -- the entry, its projection's passage and element, and the
    knowledge object derived from it -- never reads back for Alice; Bob reads
    his own back (the control)."""
    cited = [
        ref("k1", "memory", "mem-bob"),
        ref("k2", "chunk", "c-mem-bob", source_id="src-mem-bob"),
        ref("k3", "element", "el-mem-bob", source_id="src-mem-bob"),
        ref("k4", "concept", "kg-bob", source_id="src-mem-bob"),
    ]
    as_alice = restore(world, cited)
    assert _restored_ids(as_alice) == set()
    assert as_alice.gone == 4

    as_bob = restore(world, cited, user=world.bob)
    assert _restored_ids(as_bob) == {
        ("memory", "mem-bob"), ("chunk", "c-mem-bob"),
        ("element", "el-mem-bob"), ("kg", "kg-bob"),
    }
    # ... and Alice's Memory is not Bob's.
    assert _restored_ids(restore(world, [
        ref("k1", "memory", "mem-alice"),
        ref("k2", "chunk", "c-mem-alice", source_id="src-mem-alice"),
    ], user=world.bob)) == set()

    # The passage owner predicate holds even under a ceiling that admits
    # Bob's projection source (an all-admitting local scope).
    token = set_request_user(world.alice)
    try:
        with source_scope_context(world.nb, {
            "mode": "exclude", "source_ids": [], "narrowed": False,
        }):
            loose = world.repo.retrieval.restore_report_references(
                world.nb, world.alice.id, [cited[1]],
            )
    finally:
        reset_request_user(token)
    assert _restored_ids(loose) == set()


def scenario_closed_memory_channel_reads_no_memory(world: World) -> None:
    cited = [
        ref("k1", "memory", "mem-alice"),
        ref("k2", "chunk", "c-mem-alice", source_id="src-mem-alice"),
        ref("k3", "chunk", "c-a", source_id="src-a"),
    ]
    with memory_access_context(False):
        restored = restore(world, cited)
    assert restored.memory == ()
    assert _restored_ids(restored) == {("chunk", "c-a")}
    assert restored.gone == 2


def scenario_relation_reads_back_as_its_evidence_element(world: World) -> None:
    """D2: a relation reference restores as its evidence element -- the stored
    one, else the relation's first current evidence element -- and shares the
    element with a plain element reference to it."""
    restored = restore(world, [
        ref("k1", "relation", "rel-a", source_id="src-a", element_id="el-a2"),
        ref("k2", "relation", "rel-a", source_id="src-a"),
        ref("k3", "relation", "rel-a", source_id="src-a", element_id="el-reparsed"),
        ref("k4", "element", "el-a2", source_id="src-a"),
        ref("k5", "relation", "rel-a", notebook_id=world.lib, source_id="src-a"),
        ref("k6", "relation", "rel-missing", source_id="src-a"),
    ])

    assert [element.element_id for element in restored.elements] == ["el-a2"]
    assert dict(restored.key_by_object) == {
        ("element", "el-a2"): ("k1", "k2", "k3", "k4"),
    }
    assert restored.gone == 2

    # The relation's evidence element outside the ceiling: nothing.
    narrowed = {"mode": "include", "source_ids": ["src-b"], "narrowed": True}
    outside = restore(world, [
        ref("k1", "relation", "rel-a", source_id="src-a", element_id="el-a2"),
        ref("k2", "relation", "rel-a", source_id="src-a"),
    ], local_scope=narrowed)
    assert _restored_ids(outside) == set()
    assert outside.gone == 2


def scenario_unreadable_types_are_gone(world: World) -> None:
    restored = restore(world, [
        ref("k1", "external", "https://example.org/paper"),
        ref("k2", "source", "src-a", source_id="src-a"),
        ref("k3", "", "c-a", source_id="src-a"),
        ref("k4", "concept", "c-a", source_id="src-a"),
    ])
    assert _restored_ids(restored) == set()
    assert restored.gone == 4


def scenario_needs_the_runs_own_ceiling(world: World) -> None:
    cited = [ref("k1", "chunk", "c-a", source_id="src-a")]
    retrieval = world.repo.retrieval
    token = set_request_user(world.alice)
    try:
        with pytest.raises(RuntimeError):
            retrieval.restore_report_references(world.nb, world.alice.id, cited)
        with default_ceiling_context(
            world.lib, world.alice.id, real_readers(world.repo),
        ):
            with pytest.raises(RuntimeError):
                retrieval.restore_report_references(world.nb, world.alice.id, cited)
    finally:
        reset_request_user(token)


def scenario_memory_hits_by_ids(world: World) -> None:
    """The retriever's by-id read: own, this notebook, confirmed; in the
    order given, repeats dropped; empty with the channel closed."""
    retriever = world.repo._runtime.memory_retriever
    ids = ["mem-alice", "mem-bob", "mem-alice-cand", "missing", "mem-alice"]
    hits = retriever.memory_hits_by_ids(world.alice.id, world.nb, ids)
    assert [(hit.memory_id, hit.score, hit.authority) for hit in hits] == [
        ("mem-alice", 0.0, 3),
    ]
    assert retriever.memory_hits_by_ids(world.alice.id, world.lib, ids) == []
    with memory_access_context(False):
        assert retriever.memory_hits_by_ids(world.alice.id, world.nb, ids) == []


SCENARIOS = (
    scenario_each_kind_reads_back_the_current_text,
    scenario_mounted_library_items_carry_their_library,
    scenario_deleted_and_reparsed_items_are_gone,
    scenario_outside_the_ceiling_reads_nothing,
    scenario_other_members_memory_never_reads_back,
    scenario_closed_memory_channel_reads_no_memory,
    scenario_relation_reads_back_as_its_evidence_element,
    scenario_unreadable_types_are_gone,
    scenario_needs_the_runs_own_ceiling,
    scenario_memory_hits_by_ids,
)
