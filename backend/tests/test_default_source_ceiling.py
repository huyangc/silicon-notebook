"""The ONE default-ceiling constructor, ``ceilings_total`` and the Memory switch.

Plan 2026-09-29 retrieval-permission remediation, task E1-1 (D1, D5, ledger
E-1).  Nothing calls ``default_ceiling_context`` in production yet -- the entry
points are switched over by E1-2/E1-3 -- so these tests pin the constructor's
own contract:

* no outer scope -> include ceiling = visible ∪ the asker's own hidden sources,
  plus a VISIBLE-only ceiling per mounted participant, while both persistable
  payloads stay ``None`` and ``source_scope_restricted()`` stays False;
* an outer scope passes through untouched, with zero reads;
* ``ceilings_total``: a library no ceiling names participates in nothing, on
  every scope interface -- a library mounted mid-run, and (E-1) a public
  library a subjectless run never selected;
* ``memory_channel_allowed()`` defaults to True, is False inside
  ``memory_access_context(False)``, and then the hidden half carries no Memory;
* the constructor's read count depends on neither sources nor libraries.
"""
from __future__ import annotations

from typing import Any

import pytest

import app.services.source_scope as source_scope_module
from app.domain.retrieval import RetrievedChunk
from app.models.common import Evidence
from app.services.retrieval import RetrievedKnowledge
from app.services.source_scope import (
    ActiveSourceScope,
    CeilingReaders,
    current_base_scope_payload,
    current_source_scope,
    current_source_scope_payload,
    default_ceiling_context,
    evidence_json_allowed,
    filter_retrieval_items,
    memory_access_context,
    memory_channel_allowed,
    notebook_in_scope,
    partition_memory_sources,
    scoped_allowed_source_ids,
    scoped_participants,
    scoped_subgraph_nodes,
    source_allowed,
    source_scope_ceiling_active,
    source_scope_context,
    source_scope_restricted,
    source_scope_visible_universe_matches,
    subjectless_run_active,
)


NB = "nb-active"


class _Store:
    """In-memory stand-in for the four production readers, counting calls.

    ``hidden`` is keyed by owner and returns the RAW owner-scoped half (own
    Memory + notebook-wide Knowhow) -- the SQL of ``hidden_source_ids`` already
    keeps another member's Memory out, so this double does the same.
    """

    def __init__(
        self,
        *,
        visible: dict[str, list[str]],
        mounts: list[str],
        hidden: dict[str, list[str]] | None = None,
        types: dict[str, str] | None = None,
    ) -> None:
        self.visible = visible
        self.mounts = list(mounts)
        self.hidden_by_owner = hidden or {}
        self.types = types or {}
        self.calls: list[str] = []

    def participants(self, notebook_id: str) -> list[str]:
        self.calls.append("participants")
        return [notebook_id, *self.mounts]

    def visible_by_notebook(self, notebook_ids) -> dict[str, list[str]]:
        self.calls.append("visible_by_notebook")
        return {nb: list(self.visible.get(nb, [])) for nb in notebook_ids}

    def hidden(self, notebook_id: str, owner_id: str) -> list[str]:
        self.calls.append("hidden")
        return list(self.hidden_by_owner.get(owner_id, []))

    def source_metadata(self, source_ids) -> dict[str, dict[str, Any]]:
        self.calls.append("source_metadata")
        return {
            sid: {"id": sid, "source_type": self.types[sid]}
            for sid in source_ids if sid in self.types
        }

    def readers(self) -> CeilingReaders:
        return CeilingReaders(
            participants=self.participants,
            visible_by_notebook=self.visible_by_notebook,
            hidden=self.hidden,
            source_metadata=self.source_metadata,
        )


def _shared_store(**overrides) -> _Store:
    """A shared notebook: Bob asks; Alice owns a confirmed Memory there; one
    mounted library ``nb-lib`` has its own visible source plus hidden
    Memory/Knowhow projections that belong to ITS members."""
    kwargs: dict[str, Any] = dict(
        visible={NB: ["src-a", "src-b"], "nb-lib": ["lib-visible"]},
        mounts=["nb-lib"],
        hidden={
            "bob": ["src-knowhow", "src-memory-bob"],
            "alice": ["src-knowhow", "src-memory-alice"],
        },
        types={
            "src-knowhow": "knowhow",
            "src-memory-bob": "memory",
            "src-memory-alice": "memory",
        },
    )
    kwargs.update(overrides)
    return _Store(**kwargs)


def _chunk(source_id: str, notebook_id: str = "") -> RetrievedChunk:
    return RetrievedChunk(
        f"c-{source_id}", source_id, source_id, "", source_id,
        notebook_id=notebook_id,
    )


def _knowledge(source_id: str, notebook_id: str) -> RetrievedKnowledge:
    return RetrievedKnowledge(
        object_id=f"ko-{source_id}",
        object_type="claim",
        payload={"name": source_id},
        evidence=[Evidence(
            source_id=source_id, source_title=source_id,
            element_id=f"el-{source_id}", element_type="paragraph",
            location_label="p1", quoted_span="evidence", confidence=1.0,
        )],
        notebook_id=notebook_id,
    )


# ---------------------------------------------------------------------------
# Shape of the synthesised default ceiling
# ---------------------------------------------------------------------------


def test_unscoped_run_freezes_visible_plus_own_hidden_and_is_not_narrowed():
    store = _shared_store()
    assert current_source_scope() is None
    with default_ceiling_context(NB, "bob", store.readers()):
        scope = current_source_scope()
        assert scope is not None
        assert scope.notebook_id == NB
        assert scope.mode == "include"
        assert scope.source_ids == frozenset({"src-a", "src-b"})
        assert scope.hidden_source_ids == frozenset(
            {"src-knowhow", "src-memory-bob"}
        )
        assert scope.withheld_hidden_source_ids == frozenset()
        assert scope.narrowed is False
        assert scope.owner_id == "bob"
        assert scope.ceilings_total is True
        assert scope.subjectless is False
        assert subjectless_run_active() is False
        # Binds like the browser's all-selected freeze ...
        assert source_scope_ceiling_active() is True
        # ... and is not a narrowing: no channel is switched off.
        assert source_scope_restricted() is False
        assert source_allowed(NB, "src-a") is True
        assert source_allowed(NB, "src-memory-bob") is True, "own Memory stays in"
        assert source_allowed(NB, "src-knowhow") is True, "Knowhow is shared"
        assert source_allowed(NB, "src-memory-alice") is False, (
            "another member's private Memory never enters the ceiling"
        )
        assert source_allowed(NB, "src-uploaded-later") is False, (
            "a source that appears after the freeze does not join the run"
        )
        assert set(scoped_allowed_source_ids(NB)) == {
            "src-a", "src-b", "src-knowhow", "src-memory-bob",
        }
    assert current_source_scope() is None, "the scope is reset on exit"


def test_report_persisted_payload_is_still_none_for_the_synthesised_scope():
    """``report_engine.prepare_intent`` persists these two values into the
    report ``understanding`` contract; a synthesised ceiling must not become a
    persisted user selection (it would be re-frozen on confirm)."""
    store = _shared_store()
    with default_ceiling_context(NB, "bob", store.readers()):
        scope = current_source_scope()
        assert scope is not None and scope.ceiling_active
        assert scope.source_provided is False
        assert scope.base_provided is False
        assert current_source_scope_payload() is None
        assert current_base_scope_payload() is None


def test_mounted_library_exposes_its_visible_sources_only():
    store = _shared_store(
        types={
            "src-knowhow": "knowhow",
            "src-memory-bob": "memory",
            "lib-memory": "memory",
            "lib-knowhow": "knowhow",
        },
    )
    with default_ceiling_context(NB, "bob", store.readers()):
        scope = current_source_scope()
        assert scope is not None
        ceiling = scope.source_ceiling_for("nb-lib")
        assert ceiling == frozenset({"lib-visible"})
        assert isinstance(ceiling, frozenset)
        assert scope.source_ceiling_for(NB) is None, (
            "the active notebook is bound by the local ceiling, never twice"
        )
        assert notebook_in_scope("nb-lib") is True
        assert source_allowed("nb-lib", "lib-visible") is True
        assert source_allowed("nb-lib", "lib-memory") is False
        assert source_allowed("nb-lib", "lib-knowhow") is False
        assert scoped_allowed_source_ids("nb-lib") == ("lib-visible",)
        assert scoped_allowed_source_ids(
            "nb-lib", ["lib-memory", "lib-visible", "lib-knowhow"]
        ) == ("lib-visible",)
        kept = filter_retrieval_items(NB, "chunk", [
            _chunk("lib-visible", "nb-lib"), _chunk("lib-memory", "nb-lib"),
        ])
        assert [c.source_id for c in kept] == ["lib-visible"]
        knowledge = filter_retrieval_items(NB, "knowledge", [
            _knowledge("lib-visible", "nb-lib"),
            _knowledge("lib-knowhow", "nb-lib"),
        ])
        assert [k.object_id for k in knowledge] == ["ko-lib-visible"], (
            "a mounted-library node supported only by a hidden projection "
            "is dropped, not kept evidence-less"
        )


def test_library_mounted_mid_run_is_refused_by_every_interface():
    """``ceilings_total``: the mount set was frozen with the ceilings, and a
    library mounted afterwards (readers such as ``communities.mounted_base_ids``
    resolve mounts live) participates in nothing."""
    store = _shared_store()
    with default_ceiling_context(NB, "bob", store.readers()):
        store.mounts.append("nb-late")  # mounted after the freeze
        scope = current_source_scope()
        assert scope is not None
        assert scope.covers_notebook("nb-late") is False
        assert scope.allows("nb-late", "late-src") is False
        assert notebook_in_scope("nb-late") is False
        assert source_allowed("nb-late", "late-src") is False
        assert scoped_allowed_source_ids("nb-late") == ()
        assert scoped_allowed_source_ids("nb-late", ["late-src"]) == ()
        assert filter_retrieval_items(NB, "chunk", [
            _chunk("late-src", "nb-late"),
        ]) == []
        assert filter_retrieval_items(NB, "knowledge", [
            _knowledge("late-src", "nb-late"),
        ]) == []
        assert filter_retrieval_items(NB, "relation", [
            {"notebook_id": "nb-late", "evidence": [{"source_id": "late-src"}]},
        ]) == []
        assert evidence_json_allowed(
            "nb-late", '[{"source_id": "late-src"}]'
        ) is False
        assert scoped_participants([NB, "nb-lib", "nb-late"]) == (NB, "nb-lib")
        assert scoped_subgraph_nodes([
            ({"name": "x", "notebook_id": "nb-late"}, 1.0),
            ({"name": "y", "notebook_id": "nb-lib"}, 1.0),
            ({"name": "z", "notebook_id": NB}, 1.0),
        ]) == [
            ({"name": "y", "notebook_id": "nb-lib"}, 1.0),
            ({"name": "z", "notebook_id": NB}, 1.0),
        ]
        # The active notebook is never refused by the total-ceilings rule.
        assert scope.covers_notebook(NB) is True
        assert scope.covers_notebook("") is True


def test_ceilings_total_without_any_mount_refuses_every_other_library():
    store = _Store(visible={NB: ["src-a"]}, mounts=[])
    with default_ceiling_context(NB, "bob", store.readers()):
        scope = current_source_scope()
        assert scope is not None
        assert scope.notebook_source_ceilings == ()
        assert scope.covers_notebook("nb-other") is False
        assert filter_retrieval_items(NB, "chunk", [
            _chunk("src-a"), _chunk("x", "nb-other"),
        ]) == [_chunk("src-a")]


def test_without_ceilings_total_a_missing_entry_fails_open():
    """The hole ``ceilings_total`` closes, pinned so the bit stays load-bearing:
    the same per-notebook ceilings without it admit an unnamed library."""
    scope = ActiveSourceScope(
        notebook_id=NB, mode="include", source_ids=frozenset({"src-a"}),
        narrowed=False, source_provided=False, base_provided=False,
        notebook_source_ceilings={"nb-lib": ["lib-visible"]},
    )
    assert scope.covers_notebook("nb-late") is True
    assert scope.allows("nb-late", "late-src") is True
    total = ActiveSourceScope(
        notebook_id=NB, mode="include", source_ids=frozenset({"src-a"}),
        narrowed=False, source_provided=False, base_provided=False,
        notebook_source_ceilings={"nb-lib": ["lib-visible"]},
        ceilings_total=True,
    )
    assert total.covers_notebook("nb-late") is False
    assert total.allows("nb-late", "late-src") is False
    assert total.allows("nb-lib", "lib-visible") is True


def test_subjectless_run_refuses_an_unselected_public_library():
    """Ledger E-1: in a global/peer run the participants are exactly the
    selected libraries; a public library the user did not select (reachable
    through public-tier readers) must not be covered."""
    ceilings = {"nb-x": ["x-1"], "nb-y": ["y-1"]}
    with source_scope_context(
        "nb-x", None, None, ceilings, subjectless=True, ceilings_total=True,
    ):
        assert subjectless_run_active() is True
        scope = current_source_scope()
        assert scope is not None and scope.ceilings_total
        assert scope.covers_notebook("nb-public") is False
        assert notebook_in_scope("nb-public") is False
        assert source_allowed("nb-public", "pub-1") is False
        assert scoped_allowed_source_ids("nb-public") == ()
        assert filter_retrieval_items("nb-x", "chunk", [
            _chunk("pub-1", "nb-public"), _chunk("y-1", "nb-y"),
        ]) == [_chunk("y-1", "nb-y")]
        assert scope.covers_notebook("nb-y") is True
        assert source_allowed("nb-y", "y-1") is True
    # Today's global run (no ``ceilings_total``) still covers it: the gap E1-2
    # closes by setting the bit in ``global_run``.
    with source_scope_context("nb-x", None, None, ceilings, subjectless=True):
        assert current_source_scope().covers_notebook("nb-public") is True


def test_ceilings_total_alone_installs_a_scope():
    with source_scope_context(NB, None, ceilings_total=True):
        scope = current_source_scope()
        assert scope is not None
        assert scope.covers_notebook("nb-any") is False
        assert current_source_scope_payload() is None
        assert source_scope_restricted() is False


# ---------------------------------------------------------------------------
# Pass-through and submitted dimensions
# ---------------------------------------------------------------------------


def test_outer_scope_passes_through_with_zero_reads():
    store = _shared_store()
    ceilings = {"nb-x": ["x-1"], NB: ["src-a"]}
    with source_scope_context(NB, None, None, ceilings, subjectless=True):
        outer = current_source_scope()
        with default_ceiling_context(
            NB, "bob", store.readers(),
            local_scope={"mode": "include", "source_ids": ["zzz"]},
        ):
            assert current_source_scope() is outer
            assert subjectless_run_active() is True
        assert current_source_scope() is outer
    assert store.calls == []


def test_submitted_local_scope_is_used_as_is():
    """A route-frozen narrowing passes through field-faithfully: it carries no
    hidden half (the asker's own hidden sources do not take part when the user
    narrowed) and the constructor does not add one."""
    store = _shared_store()
    submitted = {
        "mode": "include", "source_ids": ["src-a"], "narrowed": True,
        "owner_id": "bob",
    }
    with default_ceiling_context(
        NB, "bob", store.readers(), local_scope=submitted,
    ):
        scope = current_source_scope()
        assert scope is not None
        assert scope.source_provided is True
        assert scope.hidden_source_ids == frozenset()
        assert source_scope_restricted() is True
        assert current_source_scope_payload() == {
            "mode": "include", "source_ids": ["src-a"], "narrowed": True,
        }
        assert source_allowed(NB, "src-memory-bob") is False
        # Mounted libraries are still frozen to their visible sources.
        assert scope.source_ceiling_for("nb-lib") == frozenset({"lib-visible"})
        assert scope.ceilings_total is True
    assert "hidden" not in store.calls
    assert store.calls == ["participants", "visible_by_notebook"]


def test_submitted_base_scope_is_used_as_is():
    store = _shared_store(
        visible={NB: ["src-a"], "nb-lib": ["lib-1"], "nb-lib2": ["lib2-1"]},
        mounts=["nb-lib", "nb-lib2"],
    )
    base = {"mode": "include", "notebook_ids": ["nb-lib"], "narrowed": True}
    with default_ceiling_context(NB, "bob", store.readers(), base_scope=base):
        scope = current_source_scope()
        assert scope is not None
        assert scope.base_provided is True
        assert current_base_scope_payload() == {
            "mode": "include", "notebook_ids": ["nb-lib"], "narrowed": True,
        }
        assert current_source_scope_payload() is None
        assert scope.covers_notebook("nb-lib") is True
        assert scope.covers_notebook("nb-lib2") is False
        assert source_allowed("nb-lib", "lib-1") is True
        assert source_allowed("nb-lib2", "lib2-1") is False


# ---------------------------------------------------------------------------
# The Memory channel switch
# ---------------------------------------------------------------------------


def test_memory_channel_defaults_open_and_closes_tighten_only():
    assert memory_channel_allowed() is True
    with memory_access_context(False):
        assert memory_channel_allowed() is False
        with memory_access_context(True):
            assert memory_channel_allowed() is False, (
                "a nested frame cannot reopen a channel the caller closed"
            )
        assert memory_channel_allowed() is False
    assert memory_channel_allowed() is True
    with memory_access_context(True):
        assert memory_channel_allowed() is True


def test_closed_memory_channel_keeps_memory_out_of_the_hidden_half():
    store = _shared_store()
    with memory_access_context(False), default_ceiling_context(
        NB, "bob", store.readers()
    ):
        scope = current_source_scope()
        assert scope is not None
        assert scope.hidden_source_ids == frozenset({"src-knowhow"})
        assert scope.withheld_hidden_source_ids == frozenset({"src-memory-bob"})
        assert source_allowed(NB, "src-memory-bob") is False, (
            "withheld ids are never admitted by any gate"
        )
        assert source_allowed(NB, "src-knowhow") is True
        assert "src-memory-bob" not in scoped_allowed_source_ids(NB)
        assert source_scope_restricted() is False
        assert current_source_scope_payload() is None
        # The drift probe re-reads the RAW owner-scoped hidden half; the
        # withheld Memory must not read as drift ...
        assert source_scope_visible_universe_matches(
            NB, ["src-a", "src-b"], ["src-knowhow", "src-memory-bob"],
        ) is True
        # ... while a Memory confirmed mid-run still does.
        assert source_scope_visible_universe_matches(
            NB, ["src-a", "src-b"],
            ["src-knowhow", "src-memory-bob", "src-memory-bob-2"],
        ) is False


def test_open_memory_channel_does_not_classify_the_hidden_half():
    store = _shared_store()
    with default_ceiling_context(NB, "bob", store.readers()):
        pass
    assert "source_metadata" not in store.calls


def test_partition_memory_sources_is_order_preserving_and_fail_closed():
    types = {"k1": "knowhow", "m1": "memory", "k2": "knowhow"}

    def metadata(ids):
        return {sid: {"source_type": types[sid]} for sid in ids if sid in types}

    kept, memory = partition_memory_sources(
        ["k2", "m1", "gone", "k1", "k2", ""], metadata
    )
    assert kept == ("k2", "k1")
    assert memory == ("m1",)
    assert partition_memory_sources([], metadata) == ((), ())


# ---------------------------------------------------------------------------
# Cost: bounded reads, each ceiling built once, nothing sorted
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_count", [3, 20_000])
@pytest.mark.parametrize("mount_count", [0, 1, 6])
def test_read_count_is_independent_of_sources_and_libraries(
    source_count, mount_count
):
    mounts = [f"nb-lib-{i}" for i in range(mount_count)]
    visible = {NB: [f"s-{i}" for i in range(source_count)]}
    visible.update({
        lib: [f"{lib}-s-{i}" for i in range(source_count)] for lib in mounts
    })
    hidden_ids = [f"h-{i}" for i in range(source_count)] + ["mem-own"]
    types = {sid: "knowhow" for sid in hidden_ids}
    types["mem-own"] = "memory"

    open_store = _Store(
        visible=visible, mounts=mounts, hidden={"bob": hidden_ids}, types=types,
    )
    with default_ceiling_context(NB, "bob", open_store.readers()):
        assert len(current_source_scope().source_ids) == source_count
    assert open_store.calls == ["participants", "visible_by_notebook", "hidden"]

    closed_store = _Store(
        visible=visible, mounts=mounts, hidden={"bob": hidden_ids}, types=types,
    )
    with memory_access_context(False), default_ceiling_context(
        NB, "bob", closed_store.readers()
    ):
        assert "mem-own" not in current_source_scope().hidden_source_ids
    assert closed_store.calls == [
        "participants", "visible_by_notebook", "hidden", "source_metadata",
    ]

    submitted_store = _Store(visible=visible, mounts=mounts)
    with default_ceiling_context(
        NB, "bob", submitted_store.readers(),
        local_scope={"mode": "include", "source_ids": ["s-0"]},
    ):
        pass
    assert submitted_store.calls == (
        ["participants", "visible_by_notebook"] if mounts else ["participants"]
    )


def test_ceiling_is_built_once_and_nothing_is_sorted(monkeypatch):
    """A ceiling can hold ~49k ids; sorting or re-normalising it per build is
    what cost 406 ms per context build.  ``sorted`` may only ever see the
    per-library pairs (one per participant), never an id list, and the
    frozenset built from the reader's list is the one the scope keeps."""
    big = [f"lib-s-{i}" for i in range(49_000)]
    store = _Store(
        visible={NB: [f"s-{i}" for i in range(49_000)], "nb-lib": big,
                 "nb-lib2": ["x"]},
        mounts=["nb-lib", "nb-lib2"],
        hidden={"bob": ["h-1"]},
    )
    sorted_sizes: list[int] = []
    real_sorted = sorted

    def recording_sorted(iterable, *args, **kwargs):
        values = list(iterable)
        sorted_sizes.append(len(values))
        return real_sorted(values, *args, **kwargs)

    monkeypatch.setattr(
        source_scope_module, "sorted", recording_sorted, raising=False
    )
    built: list[int] = []
    real_frozen = source_scope_module._frozen_source_ids

    def counting_frozen(source_ids):
        result = real_frozen(source_ids)
        if result is not source_ids:
            built.append(len(result))
        return result

    monkeypatch.setattr(source_scope_module, "_frozen_source_ids", counting_frozen)
    with default_ceiling_context(NB, "bob", store.readers()):
        scope = current_source_scope()
        assert isinstance(scope.source_ceiling_for("nb-lib"), frozenset)
        assert len(scope.source_ceiling_for("nb-lib")) == 49_000
    assert max(sorted_sizes, default=0) <= 2, sorted_sizes
    # One build per mounted library -- ``__post_init__`` reuses the frozenset
    # ``source_scope_context`` already built instead of copying it again.
    assert real_sorted(built) == [1, 49_000]


def test_frozen_source_ids_reuses_canonical_input_and_coerces_the_rest():
    canonical = frozenset({"a", "b"})
    assert source_scope_module._frozen_source_ids(canonical) is canonical
    assert source_scope_module._frozen_source_ids([1, "b"]) == frozenset(
        {"1", "b"}
    )
    assert source_scope_module._frozen_source_ids(frozenset({1})) == frozenset(
        {"1"}
    )


# ---------------------------------------------------------------------------
# Real SQLite stores wired as the entry points will wire them
# ---------------------------------------------------------------------------

_NOW = "2026-09-29T00:00:00+00:00"


def test_default_ceiling_over_real_sqlite_stores(tmp_path, monkeypatch):
    """Two members share a notebook, each with a confirmed Memory; a mounted
    library (same owner) holds a visible source plus Memory/Knowhow projections
    of its own.  Wired through the production readers, Bob's default ceiling
    admits his own Memory and the shared Knowhow, never Alice's Memory, and
    only the mounted library's visible source; a library mounted mid-run is
    refused."""
    from app.core.config import Settings
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import (
        SQLiteRepository,
        reset_request_user,
        set_request_user,
    )

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ceiling.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    alice = repo.create_user("a00123456", "password-12")
    bob = repo.create_user("b00123456", "password-12")
    token = set_request_user(alice)
    try:
        nb = repo.create_notebook(NotebookCreate(name="共享库")).id
        lib = repo.create_notebook(NotebookCreate(name="参考库")).id
        late = repo.create_notebook(NotebookCreate(name="后挂库")).id
    finally:
        reset_request_user(token)
    repo._runtime.sharing.add_member(nb, bob.id)
    sources = repo._runtime.source_store

    def insert(notebook_id, source_id, source_type, memory_id=""):
        sources.insert_source(
            source_id=source_id, notebook_id=notebook_id, title=source_id,
            source_type=source_type, status="active", parse_status="parsed",
            file_name="", file_path="", file_size=0, file_hash="",
            summary="", doc_type="", memory_id=memory_id,
        )

    def memory(notebook_id, user_id, memory_id, source_id):
        with repo._write() as db:
            db.execute(
                "INSERT INTO memory_items"
                "(id,notebook_id,created_by,agent_profile_id,source_answer_id,"
                "origin,status,title,content_md,created_at,updated_at) "
                "VALUES (?,?,?,NULL,NULL,'ask_answer','confirmed',?,?,?,?)",
                (memory_id, notebook_id, user_id, "记忆", "内容", _NOW, _NOW),
            )
        insert(notebook_id, source_id, "memory", memory_id=memory_id)

    def mount(base_id):
        with repo._write() as db:
            db.execute(
                "INSERT INTO notebook_bases"
                "(notebook_id,base_notebook_id,created_at,created_by) "
                "VALUES (?,?,?,?)",
                (nb, base_id, _NOW, alice.id),
            )

    insert(nb, "src-visible", "pdf")
    insert(nb, "src-knowhow", "knowhow")
    memory(nb, alice.id, "mem-alice", "src-memory-alice")
    memory(nb, bob.id, "mem-bob", "src-memory-bob")
    insert(lib, "lib-visible", "pdf")
    insert(lib, "lib-knowhow", "knowhow")
    memory(lib, alice.id, "mem-lib", "lib-memory")
    insert(late, "late-visible", "pdf")
    mount(lib)

    readers = CeilingReaders(
        participants=repo._runtime.notebook_store.participant_notebook_ids,
        visible_by_notebook=sources.visible_source_ids_by_notebook,
        hidden=sources.hidden_source_ids,
        source_metadata=sources.source_metadata,
    )
    with default_ceiling_context(nb, bob.id, readers):
        scope = current_source_scope()
        assert scope is not None
        assert scope.source_ids == frozenset({"src-visible"})
        assert scope.hidden_source_ids == frozenset(
            {"src-knowhow", "src-memory-bob"}
        )
        assert source_allowed(nb, "src-memory-alice") is False
        assert scope.source_ceiling_for(lib) == frozenset({"lib-visible"})
        assert source_allowed(lib, "lib-memory") is False
        assert source_allowed(lib, "lib-knowhow") is False
        assert current_source_scope_payload() is None
        assert source_scope_restricted() is False
        # The live universe equals the freeze: no drift is reported.
        assert source_scope_visible_universe_matches(
            nb, sources.all_visible_source_ids(nb),
            sources.hidden_source_ids(nb, bob.id),
        ) is True

        mount(late)
        assert late in repo._runtime.notebook_store.participant_notebook_ids(nb)
        assert notebook_in_scope(late) is False
        assert scoped_allowed_source_ids(late) == ()

    with memory_access_context(False), default_ceiling_context(
        nb, bob.id, readers
    ):
        scope = current_source_scope()
        assert scope.hidden_source_ids == frozenset({"src-knowhow"})
        assert scope.withheld_hidden_source_ids == frozenset({"src-memory-bob"})
        assert source_scope_visible_universe_matches(
            nb, sources.all_visible_source_ids(nb),
            sources.hidden_source_ids(nb, bob.id),
        ) is True
