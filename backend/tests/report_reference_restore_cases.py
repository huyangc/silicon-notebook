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
from app.domain.report_followup import (
    CitedReportReference,
    ReportRestoreScopeError,
    RestoredReportEvidence,
)
from app.models.notebooks import NotebookCreate
from app.services.source_scope import (
    current_source_scope,
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


class _Spy:
    """Forwards every attribute of ``target``; a call is logged first."""

    def __init__(self, target: Any, label: str, calls: list[str]) -> None:
        self._target, self._label, self._calls = target, label, calls

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._target, name)
        if not callable(value):
            return value

        def logged(*args: Any, **kwargs: Any) -> Any:
            self._calls.append(f"{self._label}.{name}")
            return value(*args, **kwargs)

        return logged


_SPIED_SEATS = ("sources", "chunks", "knowledge", "notebooks", "memory_retriever")


@contextmanager
def counting_reads(world: World) -> Iterator[list[str]]:
    """Every connection the candidate owner opens and every call it makes on
    its stores and its Memory retriever, in order."""
    candidates = world.repo.retrieval.candidates
    calls: list[str] = []
    connect = candidates._connect
    saved = {name: getattr(candidates, name) for name in _SPIED_SEATS}

    def counted_connect():
        calls.append("connect")
        return connect()

    candidates._connect = counted_connect
    for name, value in saved.items():
        setattr(candidates, name, None if value is None else _Spy(value, name, calls))
    try:
        yield calls
    finally:
        del candidates._connect
        for name, value in saved.items():
            setattr(candidates, name, value)


@contextmanager
def as_request_user(user: Any) -> Iterator[None]:
    token = set_request_user(user)
    try:
        yield
    finally:
        reset_request_user(token)


def assert_refused_before_any_read(
    world: World, cited: list[CitedReportReference], actor_id: str,
) -> None:
    """Under whatever frame the caller installed: the restore raises the
    wiring error and has opened no connection and called no store."""
    with counting_reads(world) as calls:
        with pytest.raises(ReportRestoreScopeError):
            world.repo.retrieval.restore_report_references(
                world.nb, actor_id, cited,
            )
    assert calls == []


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

    # A ceiling of the right shape that lists MORE than this run may read --
    # other libraries' sources in the notebook's own include list, and a
    # frozen list for a library nobody mounted: the participant set, the
    # passage statement's notebook predicate and the element / knowledge
    # object ownership checks are the restore's own, so none of it reads back.
    overwide_local = {
        "mode": "include", "source_ids": ["src-a", "other-src", "lib-src"],
        "narrowed": False, "owner_id": world.alice.id,
    }
    with as_request_user(world.alice), source_scope_context(
        world.nb, overwide_local, None,
        {world.lib: ["lib-src"], world.other: ["other-src"]},
        ceilings_total=True,
    ):
        overwide = world.repo.retrieval.restore_report_references(
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
                ref("k7", "element", "el-lib", source_id="lib-src"),
            ],
        )
    assert _restored_ids(overwide) == {("chunk", "c-lib")}
    assert overwide.gone == 6


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

    # A loose scope -- an ``exclude`` local dimension admits Bob's projection
    # source -- is refused before anything is read, for every kind of Bob's
    # Memory and its derivatives, whether or not it claims to freeze its
    # libraries.
    loose = {
        "mode": "exclude", "source_ids": [], "narrowed": False,
        "owner_id": world.alice.id,
    }
    for frozen_libraries in (False, True):
        with as_request_user(world.alice), source_scope_context(
            world.nb, loose, ceilings_total=frozen_libraries,
        ):
            for reference in cited:
                assert_refused_before_any_read(world, [reference], world.alice.id)

    # The passage owner predicate is the statement's own: under a ceiling of
    # the right shape that (wrongly) lists Bob's projection source for Alice,
    # his passage still does not read back.
    with as_request_user(world.alice), source_scope_context(world.nb, {
        "mode": "include", "source_ids": ["src-a", "src-mem-bob"],
        "narrowed": False, "owner_id": world.alice.id,
    }, ceilings_total=True):
        listed = world.repo.retrieval.restore_report_references(
            world.nb, world.alice.id, [cited[1]],
        )
    assert _restored_ids(listed) == set()
    assert listed.gone == 1


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


def scenario_source_and_external_references_are_skipped(world: World) -> None:
    """A whole-source row and external material name no single item to give
    back: not read, not restored, and NOT gone (nothing was deleted). A row
    with no type, or a type that finds no knowledge object, is gone."""
    skipped = [
        ref("k1", "external", "https://example.org/paper"),
        ref("k2", "source", "src-a", source_id="src-a"),
    ]
    with counting_reads(world) as calls:
        only_skipped = restore(world, skipped)
    assert calls == []
    assert _restored_ids(only_skipped) == set()
    assert only_skipped.gone == 0

    mixed = restore(world, [
        *skipped,
        ref("k3", "", "c-a", source_id="src-a"),
        ref("k4", "concept", "c-a", source_id="src-a"),
        ref("k5", "chunk", "c-a", source_id="src-a"),
    ])
    assert _restored_ids(mixed) == {("chunk", "c-a")}
    assert mixed.gone == 2


def scenario_reads_only_the_kinds_asked_for(world: World) -> None:
    """An element-only request reads the participant set and the element
    tables, and opens no connection for passages, relations or objects."""
    with counting_reads(world) as calls:
        restored = restore(world, [ref("k1", "element", "el-a1", source_id="src-a")])
    assert _restored_ids(restored) == {("element", "el-a1")}
    assert calls == [
        "connect", "notebooks.participant_tiers",
        "sources.evidence_elements", "sources.source_metadata",
    ]


def scenario_wiring_faults_refuse_before_any_read(world: World) -> None:
    """Every frame but the run's own frozen ceiling, read as its owner, is
    refused with the dedicated wiring error before a connection is opened."""
    cited = [
        ref("k1", "chunk", "c-a", source_id="src-a"),
        ref("k2", "memory", "mem-bob"),
        ref("k3", "element", "el-mem-bob", source_id="src-mem-bob"),
        ref("k4", "concept", "kg-bob", source_id="src-mem-bob"),
    ]
    alice, bob = world.alice.id, world.bob.id
    with as_request_user(world.alice):
        # No ceiling at all.
        assert_refused_before_any_read(world, cited, alice)
        # Another notebook's ceiling.
        with default_ceiling_context(world.lib, alice, real_readers(world.repo)):
            assert_refused_before_any_read(world, cited, alice)
        # A subjectless (global) run's ceiling, although it names this notebook.
        with source_scope_context(
            world.nb, None, None,
            {world.nb: ["src-a", "src-b"], world.lib: ["lib-src"]},
            subjectless=True, ceilings_total=True,
        ):
            assert_refused_before_any_read(world, cited, alice)
        # This notebook's include list, but its libraries are not frozen.
        with source_scope_context(world.nb, {
            "mode": "include", "source_ids": ["src-a", "src-b"],
            "hidden_source_ids": ["src-mem-alice"], "narrowed": False,
            "owner_id": alice,
        }):
            assert_refused_before_any_read(world, cited, alice)
        # Libraries frozen, but an exclude local dimension.
        with source_scope_context(world.nb, {
            "mode": "exclude", "source_ids": ["src-b"], "owner_id": alice,
        }, ceilings_total=True):
            assert_refused_before_any_read(world, cited, alice)
        # Alice's own Ask ceiling, read as Bob (or as nobody).
        with default_ceiling_context(world.nb, alice, real_readers(world.repo)):
            assert_refused_before_any_read(world, cited, bob)
            assert_refused_before_any_read(world, cited, "")
        # A submitted local scope that carries no owner belongs to the run's
        # viewer: Alice's run cannot read it as Bob either.
        with default_ceiling_context(
            world.nb, alice, real_readers(world.repo),
            local_scope={"mode": "include", "source_ids": ["src-a"], "narrowed": True},
        ):
            assert current_source_scope().owner_id == ""
            assert_refused_before_any_read(world, cited, bob)
            assert _restored_ids(world.repo.retrieval.restore_report_references(
                world.nb, alice, cited[:1],
            )) == {("chunk", "c-a")}
    # Alice's ceiling while the run's viewer is Bob: neither Alice (not the
    # viewer) nor Bob (not the ceiling's owner) may read under it.
    with as_request_user(world.bob), default_ceiling_context(
        world.nb, alice, real_readers(world.repo),
    ):
        assert_refused_before_any_read(world, cited, alice)
        assert_refused_before_any_read(world, cited, bob)


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
    scenario_source_and_external_references_are_skipped,
    scenario_reads_only_the_kinds_asked_for,
    scenario_wiring_faults_refuse_before_any_read,
    scenario_memory_hits_by_ids,
)
