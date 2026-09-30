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
    refreshed_ceiling_context,
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

    ``hidden`` is keyed by ``(notebook, owner)`` and returns the RAW
    owner-scoped half (own Memory + notebook-wide Knowhow) -- the SQL of
    ``hidden_source_ids`` already keeps another member's Memory out, so this
    double does the same.  Every reader honours its notebook id, so a
    constructor that reads the wrong library's set gets the wrong answer.
    ``fail`` maps a notebook id to the exception its visible read raises.
    """

    def __init__(
        self,
        *,
        visible: dict[str, list[str]],
        mounts: list[str],
        hidden: dict[str, list[str]] | None = None,
        types: dict[str, str] | None = None,
        fail: dict[str, BaseException] | None = None,
        home: str = NB,
    ) -> None:
        self.visible_by_nb = visible
        self.mounts = list(mounts)
        # Owner-keyed shorthand for the home notebook; other notebooks have no
        # hidden sources unless listed as ``(notebook, owner)`` keys.
        self.hidden_rows = {
            (key if isinstance(key, tuple) else (home, key)): rows
            for key, rows in (hidden or {}).items()
        }
        self.types = types or {}
        self.fail = fail or {}
        self.calls: list[str] = []
        self.visible_calls: list[str] = []
        self.budgets: list[Any] = []
        self.events: list[dict] = []

    def participants(self, notebook_id: str) -> list[str]:
        self.calls.append("participants")
        return [notebook_id, *self.mounts]

    def visible(self, notebook_id: str) -> list[str]:
        from app.repositories.read_budget import current_read_budget

        self.calls.append("visible")
        self.visible_calls.append(notebook_id)
        self.budgets.append(current_read_budget())
        if notebook_id in self.fail:
            raise self.fail[notebook_id]
        return list(self.visible_by_nb.get(notebook_id, []))

    def hidden(self, notebook_id: str, owner_id: str) -> list[str]:
        self.calls.append("hidden")
        return list(self.hidden_rows.get((notebook_id, owner_id), []))

    def memory_sources(self, notebook_id: str) -> list[str]:
        self.calls.append("memory_sources")
        return [
            sid for (nb, _owner), rows in self.hidden_rows.items()
            if nb == notebook_id for sid in rows
            if self.types.get(sid) == "memory"
        ]

    def readers(self) -> CeilingReaders:
        return CeilingReaders(
            participants=self.participants,
            visible=self.visible,
            hidden=self.hidden,
            memory_sources=self.memory_sources,
            emit=self.events.append,
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
    """The one shape where ``ceilings_total`` is the ONLY ceiling in force: no
    local, library or per-notebook ceiling.  ``filter_retrieval_items`` must
    still walk its items (its short-circuit names ``ceilings_total``)."""
    with source_scope_context(NB, None, ceilings_total=True):
        scope = current_source_scope()
        assert scope is not None
        assert not (
            scope.ceiling_active or scope.base_ceiling_active
            or scope.peer_ceiling_active
        )
        assert scope.covers_notebook("nb-any") is False
        assert current_source_scope_payload() is None
        assert source_scope_restricted() is False
        assert filter_retrieval_items(NB, "chunk", [
            _chunk("s-1"), _chunk("x", "nb-any"),
        ]) == [_chunk("s-1")]
        assert filter_retrieval_items(NB, "knowledge", [
            _knowledge("x", "nb-any"),
        ]) == []


def test_a_submitted_exclude_form_library_dimension_does_not_readmit_a_mid_run_mount():
    """``ceilings_total`` is checked BEFORE the library dimension, whatever its
    form: an exclude-form ``base_scope`` names only what it excludes, so a
    library mounted after the freeze is "not excluded" -- and must still be
    refused because no ceiling names it."""
    store = _two_mount_store()
    base = {"mode": "exclude", "notebook_ids": ["nb-lib2"], "narrowed": True}
    with default_ceiling_context(NB, "bob", store.readers(), base_scope=base):
        scope = current_source_scope()
        assert scope.base_ceiling_active is True
        assert scope.covers_notebook("nb-lib") is True
        assert scope.covers_notebook("nb-lib2") is False, "excluded by the user"
        assert scope.covers_notebook("nb-late") is False, "mounted after the freeze"
        assert source_allowed("nb-late", "late-1") is False
        assert scoped_participants([NB, "nb-lib", "nb-lib2", "nb-late"]) == (
            NB, "nb-lib",
        )


# ---------------------------------------------------------------------------
# Rebase pin for claude/scope-ceiling-remediation (review P2-4)
# ---------------------------------------------------------------------------

_BINDS_AVAILABLE = hasattr(ActiveSourceScope, "source_ceiling_binds")
_VERDICT_AVAILABLE = hasattr(source_scope_module, "ceiling_binds")


def test_source_ceiling_binds_honours_ceilings_total():
    """FEATURE-DETECTED, and meant to go live at rebase time.

    ``ActiveSourceScope.source_ceiling_binds`` and
    ``source_scope.scoped_node_context_row`` arrive with branch
    claude/scope-ceiling-remediation, which merges before this branch.  Until
    this branch is rebased onto it the symbols do not exist and this test
    SKIPS; after the rebase it runs and fails unless ``source_ceiling_binds``
    answers True for a library ``covers_notebook`` refuses -- a library mounted
    mid-run under ``ceilings_total`` is bound by an empty ceiling, so
    ``scoped_node_context_row`` drops its row instead of handing it back.

    Second probed assertion (spec re-review F-1): with the Memory channel
    closed and the asker's own Memory withheld from the freeze, the
    ``node_context`` verdict ``source_scope.ceiling_binds`` must answer "binds".
    The drift probe matches there by design (it folds the withheld ids in) and
    no FOREIGN hidden source exists, so without an explicit
    ``withheld_hidden_source_ids`` arm the verdict would let the store read the
    library unbounded and hand Bob's own Memory back into a run that may not
    read it.

    It cannot stay skipped once the symbol exists:
    ``test_rebase_pin_runs_once_its_symbol_exists`` below calls this body
    directly whenever ``source_ceiling_binds`` is present.
    """
    if not (_BINDS_AVAILABLE or _VERDICT_AVAILABLE):
        pytest.skip(
            "source_ceiling_binds arrives with claude/scope-ceiling-remediation"
        )
    assert _BINDS_AVAILABLE and _VERDICT_AVAILABLE, (
        "source_ceiling_binds and ceiling_binds ship together"
    )
    store = _two_mount_store()
    with default_ceiling_context(NB, "bob", store.readers()):
        store.mounts.append("nb-late")
        scope = current_source_scope()
        assert scope.covers_notebook("nb-late") is False
        assert scope.source_ceiling_binds("nb-late") is True
        row = {
            "occurrences": [{"source_id": "late-1", "quote": "q"}],
            "definition": "d", "definition_basis": "defines_name",
            "steps": [],
        }
        assert source_scope_module.scoped_node_context_row(
            "nb-late", row, ceiling_pushed=False,
        ) is None
        # A named mounted library keeps its normal answer.
        assert scope.source_ceiling_binds("nb-lib") is True
        assert source_scope_module.scoped_node_context_row(
            "nb-lib", {**row, "occurrences": [{"source_id": "lib-1"}]},
            ceiling_pushed=False,
        ) is not None

    # Second probed assertion: the node_context verdict (spec F-1).
    shared = _shared_store()
    with memory_access_context(False), default_ceiling_context(
        NB, "bob", shared.readers()
    ):
        scope = current_source_scope()
        assert scope.withheld_hidden_source_ids == frozenset({"src-memory-bob"})
        live_visible = ["src-a", "src-b"]
        live_hidden = ["src-knowhow", "src-memory-bob"]
        assert source_scope_visible_universe_matches(
            NB, live_visible, live_hidden,
        ) is True, "the probe matches here by design"
        assert source_scope_module.ceiling_binds(
            scope, NB,
            drifted=lambda: not source_scope_visible_universe_matches(
                NB, live_visible, live_hidden,
            ),
            foreign_hidden=lambda: False,
        ) is True, "own Memory withheld -> the ceiling must bind the re-read"


def test_rebase_pin_runs_once_its_symbol_exists():
    """Fails if ``source_ceiling_binds`` exists and the pin above did not run:
    here its body is executed directly (so neither a skip nor a deselection of
    the pin can hide it), and while the symbol is absent the pin's only exit is
    its explicit skip."""
    assert _BINDS_AVAILABLE == hasattr(ActiveSourceScope, "source_ceiling_binds")
    assert _VERDICT_AVAILABLE == hasattr(source_scope_module, "ceiling_binds")
    if _BINDS_AVAILABLE or _VERDICT_AVAILABLE:
        test_source_ceiling_binds_honours_ceilings_total()
    else:
        with pytest.raises(pytest.skip.Exception):
            test_source_ceiling_binds_honours_ceilings_total()


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
    assert store.calls == ["participants", "visible"]
    assert store.visible_calls == ["nb-lib"], "the active visible set is not re-read"


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
        # Fail-closed until E2-2: with Memory withheld the probe reports
        # drift even when the live universe matches, so the whole-graph, PPR,
        # relation and exact-lookup channels are off for this run.
        assert source_scope_visible_universe_matches(
            NB, ["src-a", "src-b"], ["src-knowhow", "src-memory-bob"],
        ) is False
    # Open channel, same store: nothing withheld, and the matching live
    # universe reads as no drift.
    with default_ceiling_context(NB, "bob", store.readers()):
        assert current_source_scope().withheld_hidden_source_ids == frozenset()
        assert source_scope_visible_universe_matches(
            NB, ["src-a", "src-b"], ["src-knowhow", "src-memory-bob"],
        ) is True


def test_closed_channel_strips_memory_from_a_submitted_scope_too():
    """The constructor does not rely on "nobody submits a scope while closing
    the channel": a submitted all-selected freeze carrying the asker's own
    Memory in its hidden half (and, defensively, a Memory id in its visible
    half) loses both, and the probe reports drift (fail-closed until E2-2)."""
    store = _shared_store()
    submitted = {
        "mode": "include",
        "source_ids": ["src-a", "src-b", "src-memory-bob"],
        "hidden_source_ids": ["src-knowhow", "src-memory-bob"],
        "narrowed": False,
        "owner_id": "bob",
    }
    with memory_access_context(False), default_ceiling_context(
        NB, "bob", store.readers(), local_scope=submitted,
    ):
        scope = current_source_scope()
        assert scope.source_provided is True
        assert scope.source_ids == frozenset({"src-a", "src-b"})
        assert scope.hidden_source_ids == frozenset({"src-knowhow"})
        assert scope.withheld_hidden_source_ids == frozenset({"src-memory-bob"})
        assert source_allowed(NB, "src-memory-bob") is False
        assert source_scope_visible_universe_matches(
            NB, ["src-a", "src-b"], ["src-knowhow", "src-memory-bob"],
        ) is False
        assert current_source_scope_payload() == {
            "mode": "include", "source_ids": ["src-a", "src-b"], "narrowed": False,
        }
    # Open channel: the submission is used exactly as given.
    with default_ceiling_context(
        NB, "bob", store.readers(), local_scope=submitted,
    ):
        assert source_allowed(NB, "src-memory-bob") is True


def test_closed_channel_fails_closed_without_the_narrowed_bit():
    """The fail-closed check comes BEFORE the probe's ``narrowed`` short-cuts:
    a submitted include freeze that carries no narrowed bit (``narrowed`` is
    None, which otherwise means "legacy scope, keep the channels") still
    switches the non-partitioned channels off once Memory is withheld."""
    store = _shared_store()
    submitted = {
        "mode": "include",
        "source_ids": ["src-a", "src-b"],
        "hidden_source_ids": ["src-knowhow", "src-memory-bob"],
        "owner_id": "bob",
    }
    with memory_access_context(False), default_ceiling_context(
        NB, "bob", store.readers(), local_scope=submitted,
    ):
        scope = current_source_scope()
        assert scope.narrowed is None
        assert scope.withheld_hidden_source_ids == frozenset({"src-memory-bob"})
        assert source_scope_visible_universe_matches(
            NB, ["src-a", "src-b"], ["src-knowhow", "src-memory-bob"],
        ) is False
    with default_ceiling_context(
        NB, "bob", store.readers(), local_scope=submitted,
    ):
        assert source_scope_visible_universe_matches(
            NB, ["src-a", "src-b"], ["src-knowhow", "src-memory-bob"],
        ) is True, "channel open: the legacy short-cut stands"


def test_closed_channel_refuses_an_exclude_form_submission():
    store = _shared_store()
    submitted = {"mode": "exclude", "source_ids": []}
    with memory_access_context(False):
        with pytest.raises(ValueError):
            with default_ceiling_context(
                NB, "bob", store.readers(), local_scope=submitted,
            ):
                pytest.fail("an unbounded local scope must not run")
    with default_ceiling_context(NB, "bob", store.readers(), local_scope=submitted):
        assert current_source_scope().mode == "exclude"


def test_closed_channel_strips_memory_on_refresh():
    store = _shared_store()
    with default_ceiling_context(NB, "bob", store.readers()):
        with memory_access_context(False), refreshed_ceiling_context(
            NB, "bob", store.readers(),
        ):
            scope = current_source_scope()
            assert scope.hidden_source_ids == frozenset({"src-knowhow"})
            assert scope.withheld_hidden_source_ids == frozenset({"src-memory-bob"})
        with memory_access_context(False), refreshed_ceiling_context(
            NB, "bob", store.readers(),
            local_scope={"mode": "include", "source_ids": ["src-a"],
                         "hidden_source_ids": ["src-memory-bob"],
                         "narrowed": True},
        ):
            assert source_allowed(NB, "src-memory-bob") is False


def test_a_payload_cannot_supply_withheld_ids():
    """``withheld_hidden_source_ids`` widens what the drift probe expects; a
    submitted dict carrying that key must not suppress drift detection."""
    payload = {
        "mode": "include", "source_ids": ["src-a"],
        "hidden_source_ids": ["src-knowhow"], "narrowed": False,
        "owner_id": "bob",
        "withheld_hidden_source_ids": ["src-memory-new"],
    }
    with source_scope_context(NB, payload):
        scope = current_source_scope()
        assert scope.withheld_hidden_source_ids == frozenset()
        assert source_scope_visible_universe_matches(
            NB, ["src-a"], ["src-knowhow", "src-memory-new"],
        ) is False, "a new hidden source is drift, whatever the payload said"
    store = _shared_store()
    with default_ceiling_context(NB, "bob", store.readers(), local_scope=payload):
        assert current_source_scope().withheld_hidden_source_ids == frozenset()


def test_open_memory_channel_does_not_classify_the_hidden_half():
    store = _shared_store()
    with default_ceiling_context(NB, "bob", store.readers()):
        pass
    assert "memory_sources" not in store.calls


def test_partition_memory_sources_is_order_preserving_and_deduplicated():
    kept, memory = partition_memory_sources(
        ["k2", "m1", "k1", "k2", "", "m2"], ["m1", "m2", "m-other"]
    )
    assert kept == ("k2", "k1")
    assert memory == ("m1", "m2")
    assert partition_memory_sources([], ["m1"]) == ((), ())


# ---------------------------------------------------------------------------
# Re-installing a refreshed freeze (report auto-confirm)
# ---------------------------------------------------------------------------


def _refresh_store() -> _Store:
    return _Store(
        visible={
            NB: ["src-a", "src-b"], "nb-lib": ["lib-1"],
            "nb-late": ["late-1"], "nb-late2": ["late2-1"],
        },
        mounts=["nb-lib"],
        hidden={"bob": ["src-knowhow"]},
    )


def test_refreshed_scope_is_adopted_and_ceilings_are_inherited():
    store = _refresh_store()
    refreshed = {
        "mode": "include", "source_ids": ["src-a", "src-new"],
        "hidden_source_ids": ["src-knowhow"], "narrowed": False,
        "owner_id": "bob",
    }
    with default_ceiling_context(NB, "bob", store.readers()):
        outer = current_source_scope()
        frozen_lib = outer.source_ceiling_for("nb-lib")
        store.calls.clear()
        store.visible_calls.clear()
        with refreshed_ceiling_context(
            NB, "bob", store.readers(), local_scope=refreshed,
        ):
            scope = current_source_scope()
            assert scope is not outer
            assert scope.source_ids == frozenset({"src-a", "src-new"})
            assert source_allowed(NB, "src-new") is True
            assert source_allowed(NB, "src-b") is False
            assert current_source_scope_payload() == {
                "mode": "include", "source_ids": ["src-a", "src-new"],
                "narrowed": False,
            }
            # Mounted libraries stay visible-only and the ceilings stay total.
            assert scope.ceilings_total is True
            assert scope.source_ceiling_for("nb-lib") is frozen_lib
            assert source_allowed("nb-lib", "lib-1") is True
            assert scope.covers_notebook("nb-unnamed") is False
        assert current_source_scope() is outer, "the outer freeze is restored"
    assert store.visible_calls == [], "no inherited library is read again"
    assert store.calls == ["participants"]


def test_a_library_mounted_before_the_refresh_follows_the_refreshed_library_dimension():
    store = _refresh_store()
    with default_ceiling_context(NB, "bob", store.readers()):
        store.mounts += ["nb-late", "nb-late2"]  # mounted between freeze and refresh
        admits_late = {"mode": "include", "notebook_ids": ["nb-lib", "nb-late"],
                       "narrowed": True}
        with refreshed_ceiling_context(
            NB, "bob", store.readers(), base_scope=admits_late,
        ):
            scope = current_source_scope()
            assert scope.source_ceiling_for("nb-late") == frozenset({"late-1"})
            assert source_allowed("nb-late", "late-1") is True
            assert scope.source_ceiling_for("nb-late2") is None
            assert scope.covers_notebook("nb-late2") is False, (
                "not admitted by the refreshed library dimension -> refused"
            )
            assert current_base_scope_payload() == {
                "mode": "include", "notebook_ids": ["nb-late", "nb-lib"],
                "narrowed": True,
            }
            # The local dimension was not refreshed: the default freeze stands.
            assert scope.source_ids == frozenset({"src-a", "src-b"})
            assert current_source_scope_payload() is None
        assert "nb-late2" not in store.visible_calls, "an unadmitted library is not read"
        excludes_late = {"mode": "include", "notebook_ids": ["nb-lib"],
                         "narrowed": True}
        with refreshed_ceiling_context(
            NB, "bob", store.readers(), base_scope=excludes_late,
        ):
            assert current_source_scope().covers_notebook("nb-late") is False
            assert source_allowed("nb-late", "late-1") is False


def test_refresh_inside_a_subjectless_run_passes_through():
    store = _refresh_store()
    with source_scope_context(
        NB, None, None, {NB: ["src-a"], "nb-x": ["x-1"]}, subjectless=True,
    ):
        outer = current_source_scope()
        with refreshed_ceiling_context(
            NB, "bob", store.readers(),
            local_scope={"mode": "include", "source_ids": ["zzz"]},
        ):
            assert current_source_scope() is outer
    assert store.calls == []


def test_refresh_without_a_default_ceiling_outside_installs_a_fresh_one():
    store = _refresh_store()
    refreshed = {"mode": "include", "source_ids": ["src-a"], "narrowed": True}
    with refreshed_ceiling_context(
        NB, "bob", store.readers(), local_scope=refreshed,
    ):
        scope = current_source_scope()
        assert scope.ceilings_total is True
        assert scope.source_ids == frozenset({"src-a"})
        assert scope.source_ceiling_for("nb-lib") == frozenset({"lib-1"})
    # A legacy outer scope (no ceilings_total) is replaced, not trusted.
    with source_scope_context(NB, {"mode": "exclude", "source_ids": []}):
        with refreshed_ceiling_context(
            NB, "bob", store.readers(), local_scope=refreshed,
        ):
            scope = current_source_scope()
            assert scope.ceilings_total is True
            assert scope.source_ceiling_for("nb-lib") == frozenset({"lib-1"})


def test_refresh_for_another_notebook_is_refused():
    store = _refresh_store()
    with default_ceiling_context(NB, "bob", store.readers()):
        with pytest.raises(ValueError):
            with refreshed_ceiling_context("nb-other", "bob", store.readers()):
                pass
    # Decided before any other branch: an older-style outer scope and a
    # subjectless one for another notebook are refused too, never replaced.
    with source_scope_context("nb-other", {"mode": "include", "source_ids": ["o-1"]}):
        with pytest.raises(ValueError):
            with refreshed_ceiling_context(NB, "bob", store.readers()):
                pass
    with source_scope_context(
        "nb-other", None, None, {"nb-other": ["o-1"]}, subjectless=True,
    ):
        with pytest.raises(ValueError):
            with refreshed_ceiling_context(NB, "bob", store.readers()):
                pass
    assert store.calls == ["participants", "visible", "hidden", "visible"]


def test_refresh_inside_a_legacy_scope_keeps_every_dimension_it_does_not_refresh():
    """Quality re-review P2-1: an older-style outer scope (installed by
    ``source_scope_context``, no ``ceilings_total``) that narrowed the local
    dimension to ``src-a`` must not come out of a library-only refresh as the
    whole notebook; likewise a submitted library exclusion survives a
    local-only refresh.  Mounted libraries still get visible-only ceilings and
    the ceilings become total."""
    store = _refresh_store()
    legacy_local = {
        "mode": "include", "source_ids": ["src-a"], "narrowed": True,
        "owner_id": "bob",
    }
    with source_scope_context(NB, legacy_local):
        assert source_allowed(NB, "src-b") is False
        with refreshed_ceiling_context(
            NB, "bob", store.readers(),
            base_scope={"mode": "include", "notebook_ids": ["nb-lib"],
                        "narrowed": False},
        ):
            scope = current_source_scope()
            assert source_allowed(NB, "src-b") is False, "wider than the outer"
            assert source_allowed(NB, "src-a") is True
            assert source_scope_restricted() is True
            assert current_source_scope_payload() == {
                "mode": "include", "source_ids": ["src-a"], "narrowed": True,
            }
            assert scope.ceilings_total is True
            assert scope.source_ceiling_for("nb-lib") == frozenset({"lib-1"})
            assert scope.covers_notebook("nb-unnamed") is False
    assert "hidden" not in store.calls, "the outer local binds: nothing synthesised"

    legacy_base = {"mode": "exclude", "notebook_ids": ["nb-lib"], "narrowed": True}
    store.visible_calls.clear()
    with source_scope_context(NB, legacy_local, legacy_base):
        with refreshed_ceiling_context(
            NB, "bob", store.readers(),
            local_scope={"mode": "include", "source_ids": ["src-a", "src-b"],
                         "narrowed": False},
        ):
            assert current_source_scope().covers_notebook("nb-lib") is False
            assert source_allowed("nb-lib", "lib-1") is False
            assert source_allowed(NB, "src-b") is True
            assert current_base_scope_payload() == {
                "mode": "exclude", "notebook_ids": ["nb-lib"], "narrowed": True,
            }
    assert store.visible_calls == [], "an excluded library is not read"


def test_refresh_inside_a_legacy_scope_with_an_own_notebook_entry():
    """An older-style outer may bind its OWN notebook through a per-notebook
    entry.  Not refreshing the local dimension keeps that entry binding (so
    ``src-b`` stays refused); refreshing it replaces the entry -- the refreshed
    list is the local dimension now, and keeping both would be the ambiguous
    shape ``ActiveSourceScope`` refuses."""
    store = _refresh_store()
    with source_scope_context(NB, None, None, {NB: ["src-a"]}):
        assert source_allowed(NB, "src-b") is False
        with refreshed_ceiling_context(NB, "bob", store.readers()):
            scope = current_source_scope()
            assert scope.source_ceiling_for(NB) == frozenset({"src-a"})
            assert source_allowed(NB, "src-b") is False
            assert scope.ceilings_total is True
            assert scope.source_ceiling_for("nb-lib") == frozenset({"lib-1"})
        with refreshed_ceiling_context(
            NB, "bob", store.readers(),
            local_scope={"mode": "include", "source_ids": ["src-a", "src-b"],
                         "narrowed": False},
        ):
            scope = current_source_scope()
            assert scope.source_ceiling_for(NB) is None
            assert source_allowed(NB, "src-b") is True
    assert "hidden" not in store.calls


def test_refresh_inside_a_legacy_scope_that_binds_nothing_only_narrows():
    """The one case a None dimension does not copy the outer: an older-style
    outer whose local dimension binds nothing (library-only) gets the
    constructor's synthesised ``visible ∪ hidden(owner)`` -- narrower, never
    wider -- and it stays unpersisted."""
    store = _shared_store()
    with source_scope_context(
        NB, None, {"mode": "include", "notebook_ids": ["nb-lib"], "narrowed": False},
    ):
        assert source_allowed(NB, "src-memory-alice") is True, "unbound outer"
        with refreshed_ceiling_context(NB, "bob", store.readers()):
            scope = current_source_scope()
            assert source_allowed(NB, "src-memory-alice") is False
            assert source_allowed(NB, "src-memory-bob") is True
            assert scope.source_ids == frozenset({"src-a", "src-b"})
            assert current_source_scope_payload() is None
            assert current_base_scope_payload() == {
                "mode": "include", "notebook_ids": ["nb-lib"], "narrowed": False,
            }


def test_refresh_in_a_closed_channel_keeps_the_own_memory_out_of_every_inherited_shape():
    """Third review P3-4 (mutant X1) and P3-3.  With the Memory channel closed
    at the refresh, a local dimension the refresh re-derives from an
    older-style outer must lose the asker's own Memory in BOTH halves -- the
    synthesised ``visible ∪ hidden(owner)`` for an outer that binds nothing,
    and the per-notebook entry an outer binds its own notebook by."""
    store = _shared_store()
    with source_scope_context(
        NB, None, {"mode": "include", "notebook_ids": ["nb-lib"], "narrowed": False},
    ), memory_access_context(False), refreshed_ceiling_context(
        NB, "bob", store.readers(),
    ):
        scope = current_source_scope()
        assert "src-memory-bob" not in scope.hidden_source_ids
        assert scope.hidden_source_ids == frozenset({"src-knowhow"})
        assert source_allowed(NB, "src-memory-bob") is False
        assert source_allowed(NB, "src-knowhow") is True
    with source_scope_context(
        NB, None, None, {NB: ["src-a", "src-memory-bob"]},
    ):
        assert source_allowed(NB, "src-memory-bob") is True, "the outer admits it"
        with memory_access_context(False), refreshed_ceiling_context(
            NB, "bob", store.readers(),
        ):
            scope = current_source_scope()
            assert scope.source_ceiling_for(NB) == frozenset({"src-a"})
            assert source_allowed(NB, "src-memory-bob") is False
            assert source_allowed(NB, "src-a") is True
        with refreshed_ceiling_context(NB, "bob", store.readers()):
            assert source_allowed(NB, "src-memory-bob") is True, (
                "channel open: the entry is inherited as it is"
            )


def test_refresh_keeps_an_own_entry_even_when_the_outer_ceilings_are_total():
    """Third review P3-2: an outer that binds its own notebook by a
    per-notebook entry AND carries ``ceilings_total`` has an unbound
    ``exclude []`` local dimension.  A refresh that does not pass the local
    dimension must keep the entry, not inherit the unbound local dimension
    (which would admit every source, another member's Memory included)."""
    store = _shared_store()
    with source_scope_context(NB, None, None, {NB: ["src-a"]}, ceilings_total=True):
        for base in (None, {"mode": "include", "notebook_ids": ["nb-lib"],
                            "narrowed": False}):
            with refreshed_ceiling_context(
                NB, "bob", store.readers(), base_scope=base,
            ):
                scope = current_source_scope()
                assert scope.source_ceiling_for(NB) == frozenset({"src-a"})
                assert source_allowed(NB, "src-a") is True
                assert source_allowed(NB, "src-b") is False
                assert source_allowed(NB, "src-memory-alice") is False
                assert source_allowed(NB, "src-memory-bob") is False


def test_refresh_re_expresses_an_inherited_exclusion_list_narrower():
    """An older-style outer whose local dimension is an EXCLUSION list admits
    everything it does not name -- another member's Memory included -- and
    cannot be bounded when the Memory channel closes.  A refresh that does not
    pass the local dimension re-expresses it as the synthesised
    ``visible ∪ hidden(owner)`` minus the excluded ids: never wider than the
    outer, still excluding what it excluded, still narrowed."""
    store = _shared_store()
    with source_scope_context(NB, {"mode": "exclude", "source_ids": ["src-b"]}):
        assert source_allowed(NB, "src-memory-alice") is True, "the outer admits it"
        with refreshed_ceiling_context(NB, "bob", store.readers()):
            scope = current_source_scope()
            assert scope.mode == "include"
            assert source_allowed(NB, "src-b") is False
            assert source_allowed(NB, "src-a") is True
            assert source_allowed(NB, "src-memory-bob") is True
            assert source_allowed(NB, "src-memory-alice") is False
            assert source_scope_restricted() is True
            assert current_source_scope_payload() == {
                "mode": "include", "source_ids": ["src-a"], "narrowed": True,
            }
        with memory_access_context(False), refreshed_ceiling_context(
            NB, "bob", store.readers(),
        ):
            scope = current_source_scope()
            assert source_allowed(NB, "src-memory-bob") is False
            assert source_allowed(NB, "src-b") is False
            assert source_allowed(NB, "src-knowhow") is True
            assert scope.withheld_hidden_source_ids == frozenset({"src-memory-bob"})


def test_refresh_re_expression_keeps_an_excluded_hidden_source_out():
    """Fourth review R4: the exclusion list may name a HIDDEN source (here the
    notebook-wide Knowhow projection).  Re-expressed as the synthesised freeze,
    the exclusion must be subtracted from the hidden half too, or the refresh
    readmits a source the outer refused."""
    store = _shared_store()
    with source_scope_context(
        NB, {"mode": "exclude", "source_ids": ["src-knowhow"]},
    ):
        assert source_allowed(NB, "src-knowhow") is False
        with refreshed_ceiling_context(NB, "bob", store.readers()):
            scope = current_source_scope()
            assert "src-knowhow" not in scope.hidden_source_ids
            assert source_allowed(NB, "src-knowhow") is False
            assert source_allowed(NB, "src-memory-bob") is True
            assert source_allowed(NB, "src-a") is True


def test_refresh_of_the_local_dimension_keeps_a_submitted_library_exclusion():
    """Mutant M8: the outer default ceiling SUBMITTED a library exclusion and
    the refresh passes only ``local_scope`` -- the excluded library (never
    read, so it has no ceiling to inherit) must stay excluded."""
    store = _two_mount_store()
    base = {"mode": "exclude", "notebook_ids": ["nb-lib2"], "narrowed": True}
    with default_ceiling_context(NB, "bob", store.readers(), base_scope=base):
        assert current_source_scope().covers_notebook("nb-lib2") is False
        with refreshed_ceiling_context(
            NB, "bob", store.readers(),
            local_scope={"mode": "include", "source_ids": ["src-a"],
                         "narrowed": True},
        ):
            scope = current_source_scope()
            assert scope.covers_notebook("nb-lib2") is False
            assert source_allowed("nb-lib2", "lib2-1") is False
            assert scoped_allowed_source_ids("nb-lib2") == ()
            assert source_allowed("nb-lib", "lib-1") is True
            assert current_base_scope_payload() == base


def test_refresh_inside_a_closed_channel_keeps_the_withheld_memory():
    """Mutant M5: the outer freeze was taken with the Memory channel closed and
    the refresh inherits the local dimension -- the withheld ids must survive,
    so the refreshed scope's drift probe still reports NO drift (otherwise every
    run of a user holding a confirmed Memory loses its whole-graph channels)."""
    store = _shared_store()
    live_visible = ["src-a", "src-b"]
    live_hidden = ["src-knowhow", "src-memory-bob"]
    with memory_access_context(False), default_ceiling_context(
        NB, "bob", store.readers()
    ):
        with refreshed_ceiling_context(
            NB, "bob", store.readers(),
            base_scope={"mode": "include", "notebook_ids": ["nb-lib"],
                        "narrowed": False},
        ):
            scope = current_source_scope()
            assert scope.hidden_source_ids == frozenset({"src-knowhow"})
            assert scope.withheld_hidden_source_ids == frozenset({"src-memory-bob"})
            assert source_allowed(NB, "src-memory-bob") is False
            # Inherited, so the refreshed scope stays fail-closed too.
            assert source_scope_visible_universe_matches(
                NB, live_visible, live_hidden,
            ) is False


def test_refresh_without_a_library_dimension_admits_a_library_mounted_since_the_freeze():
    """Spec re-review F-5(a): neither dimension refreshed, and a library was
    mounted between the outer freeze and the refresh.  The unsubmitted library
    dimension admits it, so it is read once and gets a VISIBLE-only ceiling
    (its own hidden projections stay out); the outer freeze keeps refusing it."""
    store = _Store(
        visible={NB: ["src-a"], "nb-lib": ["lib-1"], "nb-late": ["late-1"]},
        mounts=["nb-lib"],
        hidden={"bob": ["src-knowhow"], ("nb-late", "bob"): ["late-knowhow"]},
    )
    with default_ceiling_context(NB, "bob", store.readers()):
        outer = current_source_scope()
        store.mounts.append("nb-late")
        assert outer.covers_notebook("nb-late") is False
        store.calls.clear()
        store.visible_calls.clear()
        with refreshed_ceiling_context(NB, "bob", store.readers()):
            scope = current_source_scope()
            assert scope.source_ceiling_for("nb-late") == frozenset({"late-1"})
            assert source_allowed("nb-late", "late-1") is True
            assert source_allowed("nb-late", "late-knowhow") is False
            assert scope.source_ceiling_for("nb-lib") is outer.source_ceiling_for(
                "nb-lib"
            )
            assert scope.source_ids == outer.source_ids
            assert current_base_scope_payload() is None
        assert current_source_scope() is outer
        assert source_allowed("nb-late", "late-1") is False
    assert store.calls == ["participants", "visible"]
    assert store.visible_calls == ["nb-late"]


# ---------------------------------------------------------------------------
# Mounted-library reads: one budget per library, failures isolated
# ---------------------------------------------------------------------------


def _two_mount_store(**kwargs) -> _Store:
    return _Store(
        visible={NB: ["src-a"], "nb-lib": ["lib-1"], "nb-lib2": ["lib2-1"]},
        mounts=["nb-lib", "nb-lib2"],
        hidden={"bob": ["src-knowhow"]},
        **kwargs,
    )


def test_each_mounted_library_is_read_under_its_own_budget():
    store = _two_mount_store()
    with default_ceiling_context(
        NB, "bob", store.readers(), mounted_read_seconds=2.5,
    ):
        pass
    assert store.visible_calls == [NB, "nb-lib", "nb-lib2"]
    active_budget, *mounted_budgets = store.budgets
    assert active_budget is None, "the active notebook's own read is not isolated"
    assert all(budget is not None for budget in mounted_budgets)
    assert mounted_budgets[0] is not mounted_budgets[1], "one budget per library"
    import time as _time

    for budget in mounted_budgets:
        assert 0 < budget.deadline - _time.monotonic() <= 2.5


@pytest.mark.parametrize("error, reason", [
    (RuntimeError("boom"), "unavailable"),
    (None, "timeout"),  # ReadBudgetExceeded, filled in below
])
def test_a_failing_mounted_library_is_denied_not_fatal(error, reason):
    from app.repositories.read_budget import ReadBudgetExceeded

    exc = error if error is not None else ReadBudgetExceeded("read budget exhausted")
    store = _two_mount_store(fail={"nb-lib": exc})
    with default_ceiling_context(NB, "bob", store.readers()):
        scope = current_source_scope()
        assert scope.source_ceiling_for("nb-lib") == frozenset(), "deny, not open"
        assert scope.covers_notebook("nb-lib") is True
        assert source_allowed("nb-lib", "lib-1") is False
        assert scoped_allowed_source_ids("nb-lib") == ()
        # The healthy library and the active notebook are unaffected.
        assert source_allowed("nb-lib2", "lib2-1") is True
        assert source_allowed(NB, "src-a") is True
    assert store.events == [{
        "kind": "default_ceiling_library_skipped",
        "notebook_id": "nb-lib",
        "reason": reason,
    }], "content-free: notebook id and reason code only"


def test_mounted_libraries_are_read_in_parallel_up_to_the_worker_bound():
    """Fourth review P2-4(a): mounted libraries are read at most
    ``mounted_read_workers`` at a time -- each in a copy of the caller's
    context, so a budget the caller holds still caps every read -- and the
    result is assembled in library order.  Four libraries that each take
    0.25 s finish in about one wave per two libraries with two workers, never
    with more than two reads in flight."""
    import threading
    import time as _time
    from dataclasses import replace as _replace

    from app.repositories.read_budget import current_read_budget, read_budget

    libraries = [f"nb-lib{i}" for i in range(4)]
    store = _Store(
        visible={NB: ["src-a"], **{lib: [f"{lib}-1"] for lib in libraries}},
        mounts=libraries,
    )
    original = store.visible
    lock = threading.Lock()
    in_flight = {"now": 0, "max": 0}
    deadlines: list[float] = []

    def slow(notebook_id):
        if notebook_id == NB:
            return original(notebook_id)
        with lock:
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            deadlines.append(current_read_budget().deadline)
        try:
            _time.sleep(0.25)
            return original(notebook_id)
        finally:
            with lock:
                in_flight["now"] -= 1

    # Shorter than the per-library budget (5 s), so a worker that did not
    # inherit the caller's context would read under a later deadline.
    caller_deadline = _time.monotonic() + 3.0
    started = _time.monotonic()
    with read_budget(caller_deadline), default_ceiling_context(
        NB, "bob", _replace(store.readers(), visible=slow),
        mounted_read_workers=2,
    ):
        scope = current_source_scope()
        for lib in libraries:
            assert scope.source_ceiling_for(lib) == frozenset({f"{lib}-1"})
        assert list(scope._ceiling_hand_out_memo) == libraries
    elapsed = _time.monotonic() - started
    assert in_flight["max"] == 2, in_flight
    assert elapsed < 0.9, elapsed  # serial would be >= 1.0 s
    assert all(deadline <= caller_deadline for deadline in deadlines)
    assert store.events == []


def test_an_excluded_library_is_never_read():
    """Fourth review P3-1: the fresh constructor filters mounted participants
    by the library dimension, as the refresh does.  An excluded library is
    not read, gets no ceiling, and is refused; so two excluded libraries that
    would never finish cannot spend the stage budget a selected one needs."""
    import time as _time
    from dataclasses import replace as _replace

    from app.repositories.read_budget import current_read_budget

    store = _Store(
        visible={NB: ["src-a"], "nb-ok": ["ok-1"]},
        mounts=["nb-slow1", "nb-slow2", "nb-ok"],
    )
    original = store.visible

    def never_finishes(notebook_id):
        if notebook_id not in ("nb-slow1", "nb-slow2"):
            return original(notebook_id)
        store.visible_calls.append(notebook_id)
        while True:
            current_read_budget().check()
            _time.sleep(0.005)

    with default_ceiling_context(
        NB, "bob", _replace(store.readers(), visible=never_finishes),
        base_scope={"mode": "exclude", "notebook_ids": ["nb-slow1", "nb-slow2"],
                    "narrowed": True},
        mounted_total_seconds=0.3, mounted_read_workers=1,
    ):
        scope = current_source_scope()
        assert scope.source_ceiling_for("nb-ok") == frozenset({"ok-1"})
        assert scope.source_ceiling_for("nb-slow1") is None
        assert scope.covers_notebook("nb-slow1") is False
        assert source_allowed("nb-slow2", "anything") is False
    assert store.visible_calls == [NB, "nb-ok"]
    assert store.events == []


def test_a_reader_returning_non_string_ids_is_coerced():
    """Fourth review R3: ``_ordered_source_ids`` coerces every id to ``str``
    (in C), so a reader that hands back other types yields a ``str`` ceiling
    and a ``str`` hand-out in the reader's order."""
    from dataclasses import replace as _replace

    store = _two_mount_store()
    original = store.visible

    def numeric(notebook_id):
        if notebook_id == "nb-lib":
            return [3, 1, 2]
        return original(notebook_id)

    with default_ceiling_context(
        NB, "bob", _replace(store.readers(), visible=numeric),
    ):
        scope = current_source_scope()
        assert scope.source_ceiling_for("nb-lib") == frozenset({"1", "2", "3"})
        assert scope.ceiling_hand_out("nb-lib") == ("3", "1", "2")
        assert source_allowed("nb-lib", "1") is True


def test_a_skipped_mounted_library_is_recorded_for_the_answer():
    """Third review P2-1: a mounted library left out because its visible list
    could not be read in time changes the answer, so the scope records it --
    library id and reason code only -- for the entry point's result notice.
    A healthy run records nothing; a library the user's library dimension
    excludes is not reported (its absence is the user's choice); a refresh
    carries an inherited skip and records its own."""
    from app.repositories.read_budget import ReadBudgetExceeded
    from app.services.source_scope import current_skipped_mounted_libraries

    healthy = _two_mount_store()
    with default_ceiling_context(NB, "bob", healthy.readers()):
        assert current_skipped_mounted_libraries() == {}
        assert current_source_scope().skipped_mounted_libraries() == {}
    assert current_skipped_mounted_libraries() == {}, "no scope, nothing skipped"

    store = _two_mount_store(fail={
        "nb-lib": ReadBudgetExceeded("read budget exhausted"),
        "nb-lib2": RuntimeError("boom"),
    })
    with default_ceiling_context(NB, "bob", store.readers()):
        assert current_skipped_mounted_libraries() == {
            "nb-lib": "timeout", "nb-lib2": "unavailable",
        }
        # Not a gate and not part of what the scope admits.
        scope = current_source_scope()
        assert scope == ActiveSourceScope(**{
            name: getattr(scope, name)
            for name in scope.__dataclass_fields__ if name != "_skipped_libraries"
        })
    with default_ceiling_context(
        NB, "bob", store.readers(),
        base_scope={"mode": "exclude", "notebook_ids": ["nb-lib2"],
                    "narrowed": True},
    ):
        assert current_skipped_mounted_libraries() == {"nb-lib": "timeout"}

    late = _two_mount_store(fail={"nb-late": RuntimeError("boom")})
    late.visible_by_nb["nb-late"] = ["late-1"]
    lib_fails = _two_mount_store(fail={"nb-lib": RuntimeError("boom")})
    with default_ceiling_context(NB, "bob", lib_fails.readers()):
        late.mounts.append("nb-late")
        with refreshed_ceiling_context(NB, "bob", late.readers()):
            assert current_skipped_mounted_libraries() == {
                "nb-lib": "unavailable",   # inherited with its empty ceiling
                "nb-late": "unavailable",  # newly admitted, read now, failed
            }
        with refreshed_ceiling_context(
            NB, "bob", late.readers(),
            base_scope={"mode": "include", "notebook_ids": ["nb-lib2"],
                        "narrowed": True},
        ):
            assert current_skipped_mounted_libraries() == {}

    # Fourth review R6: a library that never got a turn before the stage
    # deadline is a skip too, and must be recorded as one -- healthy readers,
    # a stage budget already spent when the reads start.
    queued = _two_mount_store()
    with default_ceiling_context(
        NB, "bob", queued.readers(), mounted_total_seconds=0.0,
    ):
        assert current_skipped_mounted_libraries() == {
            "nb-lib": "queue_deadline", "nb-lib2": "queue_deadline",
        }
    assert "nb-lib" not in queued.visible_calls


def test_an_unclassified_failure_after_the_deadline_counts_as_timeout():
    """Third review P3-6 (mutant X4): a driver may surface its own interrupt as
    a generic error that ``classify_read_failure`` cannot name.  Raised once
    the library's deadline has passed it is a ``timeout``; raised before it,
    ``unavailable``."""
    import time as _time

    from dataclasses import replace as _replace

    store = _two_mount_store()
    original = store.visible

    def generic_error_after_the_deadline(notebook_id):
        if notebook_id != "nb-lib":
            return original(notebook_id)
        _time.sleep(0.25)
        raise RuntimeError("driver said something unhelpful")

    with default_ceiling_context(
        NB, "bob",
        _replace(store.readers(), visible=generic_error_after_the_deadline),
        mounted_read_seconds=0.1,
    ):
        assert current_source_scope().source_ceiling_for("nb-lib") == frozenset()
    assert [(e["notebook_id"], e["reason"]) for e in store.events] == [
        ("nb-lib", "timeout"),
    ]


def test_a_zero_budget_denies_each_mounted_library_at_the_entry_check(
    tmp_path, monkeypatch,
):
    """With the production readers, a zero per-library budget fails
    ``read_budget``'s entry check before any statement runs: each mounted
    library is denied -- classified ``timeout`` by the repository's own
    ``classify_read_failure`` -- while the active notebook, which is not under
    that budget, keeps its full ceiling.  (The real in-statement overrun is
    ``test_a_real_budget_overrun_on_sqlite_is_classified_as_timeout``.)"""
    repo, ids = _real_sqlite_fixture(tmp_path, monkeypatch)
    events: list[dict] = []
    readers = _real_readers(repo, emit=events.append)
    with default_ceiling_context(
        ids["nb"], ids["bob"], readers, mounted_read_seconds=0.0,
    ):
        scope = current_source_scope()
        assert scope.source_ceiling_for(ids["lib"]) == frozenset()
        assert scope.source_ceiling_for(ids["lib2"]) == frozenset()
        assert scope.source_ids == frozenset({"src-visible"})
    assert events == [
        {"kind": "default_ceiling_library_skipped", "notebook_id": lib,
         "reason": "timeout"}
        for lib in (ids["lib"], ids["lib2"])
    ]


def test_the_active_notebooks_own_read_failing_fails_the_request():
    store = _two_mount_store(fail={NB: RuntimeError("active down")})
    with pytest.raises(RuntimeError, match="active down"):
        with default_ceiling_context(NB, "bob", store.readers()):
            pytest.fail("must not run unscoped")
    assert current_source_scope() is None


def test_emit_failure_is_swallowed():
    store = _two_mount_store(fail={"nb-lib": RuntimeError("x")})
    readers = store.readers()

    def broken(_event):
        raise ValueError("log sink down")

    from dataclasses import replace as _replace

    with default_ceiling_context(NB, "bob", _replace(readers, emit=broken)):
        assert current_source_scope().source_ceiling_for("nb-lib") == frozenset()


def test_cancellation_propagates_instead_of_skipping():
    import threading

    from app.services.cancellation import AskCancelled

    cancel = threading.Event()
    store = _two_mount_store()
    original = store.visible

    def cancelling_visible(notebook_id):
        if notebook_id == "nb-lib":
            cancel.set()
            raise RuntimeError("statement cancelled")
        return original(notebook_id)

    readers = store.readers()
    from dataclasses import replace as _replace

    with pytest.raises(AskCancelled):
        with default_ceiling_context(
            NB, "bob", _replace(readers, visible=cancelling_visible),
            cancel_event=cancel,
        ):
            pytest.fail("a stopped run must not start")
    assert store.events == [], "a stop is not reported as a library failure"
    # Already cancelled: no read at all.
    fresh = _two_mount_store()
    with pytest.raises(AskCancelled):
        with default_ceiling_context(
            NB, "bob", fresh.readers(), cancel_event=cancel,
        ):
            pass
    assert fresh.calls == []


# A statement SQLite needs many seconds for, interrupted only by the read
# budget's progress handler (deadline or cancel token).
_SLOW_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c "
    "WHERE x < 2000000000) SELECT count(*) FROM c"
)


def _slow_sqlite_visible(repo, slow: set[str], started=None, failures=None):
    """The production visible reader, except that for libraries in ``slow`` it
    first runs ``_SLOW_SQL`` on the same budgeted connection.  ``failures``
    records the exception each slow read raised (then re-raises it)."""
    sources = repo._runtime.source_store

    def visible(notebook_id: str) -> list[str]:
        if notebook_id in slow:
            if started is not None:
                started.set()
            try:
                with sources.database.connect() as db:
                    db.execute(_SLOW_SQL).fetchone()
            except BaseException as exc:
                if failures is not None:
                    failures.append(exc)
                raise
        return sources.all_visible_source_ids(notebook_id)

    return visible


def _cancel_when_started(started, cancel, delay: float = 0.3):
    import threading
    import time as _time

    fired: dict[str, float] = {}

    def fire():
        started.wait(30)
        _time.sleep(delay)
        fired["at"] = _time.monotonic()
        cancel.set()

    thread = threading.Thread(target=fire, daemon=True)
    thread.start()
    return thread, fired


def test_a_stop_during_a_mounted_read_in_flight_propagates(tmp_path, monkeypatch):
    """Quality re-review P3-1/P3-2 and mutant M13.  The cancel fires from
    another thread while a real SQLite statement of a mounted library is
    running (not set-then-raise inside the reader): the statement is
    interrupted by the read budget's cancel token within moments, and the stop
    propagates as ``AskCancelled`` -- never recorded as a skipped library or a
    timeout -- both when the caller passes ``cancel_event`` and when it only
    holds a cancellable ``read_budget``."""
    import sqlite3
    import threading
    import time as _time
    from dataclasses import replace as _replace

    from app.repositories.read_budget import read_budget
    from app.services.cancellation import AskCancelled

    repo, ids = _real_sqlite_fixture(tmp_path, monkeypatch)
    # Serial, and in parallel (the mounted reads on worker threads, each in a
    # copy of the caller's context): the stop reaches the read in flight
    # either way.
    for explicit, workers in ((True, 1), (False, 1), (True, 2), (False, 2)):
        cancel, started = threading.Event(), threading.Event()
        events: list[dict] = []
        failures: list[BaseException] = []
        readers = _replace(
            _real_readers(repo, emit=events.append),
            visible=_slow_sqlite_visible(repo, {ids["lib"]}, started, failures),
            read_workers=workers,
        )
        thread, fired = _cancel_when_started(started, cancel)
        with pytest.raises(AskCancelled):
            if explicit:
                with default_ceiling_context(
                    ids["nb"], ids["bob"], readers, cancel_event=cancel,
                    mounted_read_seconds=8.0, mounted_total_seconds=8.0,
                ):
                    pytest.fail("a stopped run must not start")
            else:
                with read_budget(_time.monotonic() + 60, cancel), \
                        default_ceiling_context(
                            ids["nb"], ids["bob"], readers,
                            mounted_read_seconds=8.0, mounted_total_seconds=8.0,
                        ):
                    pytest.fail("a stopped run must not start")
        stopped_after = _time.monotonic() - fired["at"]
        thread.join(5)
        assert stopped_after < 2.0, (explicit, workers, stopped_after)
        assert events == [], "a stop is not a skipped library"
        assert len(failures) == 1 and isinstance(failures[0], sqlite3.OperationalError)
        assert "interrupted" in str(failures[0]).lower()


def test_a_real_budget_overrun_on_sqlite_is_classified_as_timeout(tmp_path, monkeypatch):
    """Quality re-review P3-4: a short NON-zero budget and a statement that
    outlives it.  SQLite's progress handler interrupts the running statement,
    the real ``sqlite3.OperationalError`` is classified ``timeout``, that
    library is denied, and the healthy one keeps its ceiling."""
    import sqlite3
    import time as _time
    from dataclasses import replace as _replace

    repo, ids = _real_sqlite_fixture(tmp_path, monkeypatch)
    events: list[dict] = []
    failures: list[BaseException] = []
    readers = _replace(
        _real_readers(repo, emit=events.append),
        visible=_slow_sqlite_visible(repo, {ids["lib"]}, failures=failures),
    )
    started = _time.monotonic()
    with default_ceiling_context(
        ids["nb"], ids["bob"], readers, mounted_read_seconds=0.4,
    ):
        scope = current_source_scope()
        assert scope.source_ceiling_for(ids["lib"]) == frozenset()
        assert scope.source_ceiling_for(ids["lib2"]) == frozenset({"lib2-visible"})
        assert scope.source_ids == frozenset({"src-visible"})
    assert _time.monotonic() - started < 3.0
    assert len(failures) == 1 and type(failures[0]) is sqlite3.OperationalError
    assert "interrupted" in str(failures[0]).lower()
    assert events == [{
        "kind": "default_ceiling_library_skipped", "notebook_id": ids["lib"],
        "reason": "timeout",
    }]


@pytest.mark.parametrize("workers", [1, 2])
def test_mounted_reads_share_one_stage_deadline(workers):
    """Quality re-review P3-6: every mounted read is bounded by ``min(stage
    deadline, now + per-library budget)``, like ``_prepared_peer``.  Libraries
    that never finish cost one stage (not one per-library budget each).  With
    one worker the first is interrupted at the stage deadline (``timeout``),
    the second gets no turn at all (``queue_deadline``, never read); with two
    workers both slow ones run at once and both hit the stage deadline.  Either
    way the healthy library queued behind them gets its turn only after the
    stage deadline and is refused (``queue_deadline``) rather than stretching
    the stage."""
    import time as _time

    from app.repositories.read_budget import current_read_budget

    store = _Store(
        visible={NB: ["src-a"], "nb-ok": ["ok-1"]},
        mounts=["nb-slow1", "nb-slow2", "nb-ok"],
    )
    original = store.visible

    def never_finishes(notebook_id):
        if notebook_id not in ("nb-slow1", "nb-slow2"):
            return original(notebook_id)
        store.visible_calls.append(notebook_id)
        while True:  # a driver honouring its budget, as the progress handler does
            current_read_budget().check()
            _time.sleep(0.005)

    from dataclasses import replace as _replace

    started = _time.monotonic()
    with default_ceiling_context(
        NB, "bob", _replace(store.readers(), visible=never_finishes),
        mounted_read_seconds=5.0, mounted_total_seconds=0.3,
        mounted_read_workers=workers,
    ):
        scope = current_source_scope()
        assert scope.source_ceiling_for("nb-slow1") == frozenset()
        assert scope.source_ceiling_for("nb-slow2") == frozenset()
        assert scope.source_ceiling_for("nb-ok") == frozenset()
    assert _time.monotonic() - started < 1.5
    read = ["nb-slow1"] if workers == 1 else ["nb-slow1", "nb-slow2"]
    assert store.visible_calls[0] == NB
    assert sorted(store.visible_calls[1:]) == read, "no turn after the stage"
    # Events come in library order, whatever order the reads finished in.
    assert [(e["notebook_id"], e["reason"]) for e in store.events] == [
        ("nb-slow1", "timeout"),
        ("nb-slow2", "timeout" if workers == 2 else "queue_deadline"),
        ("nb-ok", "queue_deadline"),
    ]


def test_a_control_error_from_a_mounted_read_propagates():
    """Quality re-review P3-3 (mutant M7).  Readers are injected, so a caller
    may wire a mounted library's visible read through a path that consults the
    participant override; its identity check (``RetrievalControlError``) is
    control flow, not a library failure, and must reach the caller -- the rule
    every fail-soft handler on the retrieval path follows
    (``app.domain.retrieval_control``)."""
    from app.domain.retrieval_control import ParticipantOverrideError

    store = _two_mount_store(fail={"nb-lib": ParticipantOverrideError("mismatch")})
    with pytest.raises(ParticipantOverrideError):
        with default_ceiling_context(NB, "bob", store.readers()):
            pytest.fail("must not run")
    assert store.events == []
    assert current_source_scope() is None


def test_generator_readers_are_frozen_where_they_enter():
    """Quality re-review P3-5: readers are typed ``Iterable``.  A reader that
    returns a generator must not be consumed by the Memory disjointness check
    (closed channel) and leave the ceiling empty; every result is materialised
    where it enters, on the constructor and on the refresh."""
    from dataclasses import replace as _replace

    store = _shared_store()
    readers = _replace(
        store.readers(),
        participants=lambda nb: (value for value in store.participants(nb)),
        visible=lambda nb: (value for value in store.visible(nb)),
        hidden=lambda nb, owner: (value for value in store.hidden(nb, owner)),
        memory_sources=lambda nb: (value for value in store.memory_sources(nb)),
    )
    for channel_open in (False, True):
        with memory_access_context(channel_open), default_ceiling_context(
            NB, "bob", readers,
        ):
            scope = current_source_scope()
            assert scope.source_ids == frozenset({"src-a", "src-b"})
            assert scope.source_ceiling_for("nb-lib") == frozenset({"lib-visible"})
            expected_hidden = {"src-knowhow"} | (
                {"src-memory-bob"} if channel_open else set()
            )
            assert scope.hidden_source_ids == frozenset(expected_hidden)
    with memory_access_context(False), source_scope_context(
        NB, None, {"mode": "exclude", "notebook_ids": []},
    ), refreshed_ceiling_context(NB, "bob", readers):
        assert current_source_scope().source_ids == frozenset({"src-a", "src-b"})


def test_peer_visible_sources_returns_the_frozen_ceiling_without_reading():
    from types import SimpleNamespace

    from app.services import chunk_federation

    store = _two_mount_store()
    live_reads: list[str] = []
    candidates = SimpleNamespace(sources=SimpleNamespace(
        all_visible_source_ids=lambda nb: live_reads.append(nb) or ["live"],
    ))
    from app.services.retrieval_run import retrieval_run

    # A reader order that is NOT sorted order: the hand-out must be the
    # reader's (the production read's ``ORDER BY id``), not a sort of the set
    # and never hash order (which varies with PYTHONHASHSEED).
    read_order = [f"lib-{i:03d}" for i in range(200)][::-1]
    many = _Store(
        visible={NB: ["src-a"], "nb-lib": read_order},
        mounts=["nb-lib"],
    )
    with default_ceiling_context(NB, "bob", many.readers()), \
            retrieval_run(run_kind="ask_chunk"):
        outer = current_source_scope()
        frozen = outer.source_ceiling_for("nb-lib")
        handed = chunk_federation._peer_visible_sources(candidates, "nb-lib")
        assert handed == tuple(read_order)
        assert frozenset(handed) == frozen
        assert chunk_federation._peer_visible_sources(candidates, "nb-lib") is handed
        # A refresh inherits the ceiling and its hand-out with it ...
        with refreshed_ceiling_context(NB, "bob", many.readers()):
            assert chunk_federation._peer_visible_sources(
                candidates, "nb-lib"
            ) is handed
        # ... while a scope installed with a DIFFERENT ceiling for the same
        # library inside the same run hands out its own, never the outer's
        # (the hand-out lives on the scope, not in the run memo).
        with source_scope_context(
            NB, None, None, {"nb-lib": ["lib-001", "lib-000"]},
        ):
            assert chunk_federation._peer_visible_sources(
                candidates, "nb-lib"
            ) == ("lib-000", "lib-001"), "no reader order -> sorted once"
        assert chunk_federation._peer_visible_sources(candidates, "nb-lib") is handed
    # A read order is kept only for the very frozenset it was read into: an
    # order paired with a different set is never handed out.
    stale = frozenset({"lib-000", "lib-001", "lib-gone"})
    with source_scope_context(
        NB, None, None, {"nb-lib": frozenset({"lib-001", "lib-000"})},
        _ceiling_read_order={"nb-lib": (stale, ("lib-gone", "lib-001", "lib-000"))},
    ):
        assert current_source_scope().ceiling_hand_out("nb-lib") == (
            "lib-000", "lib-001",
        )
    with default_ceiling_context(NB, "bob", store.readers()):
        assert chunk_federation._peer_visible_sources(candidates, "nb-lib") == (
            "lib-1",
        )
        # A library the scope never froze still takes the live read.
        assert chunk_federation._peer_visible_sources(candidates, "nb-x") == ("live",)
    assert live_reads == ["nb-x"]
    # Without a scope the historical live read is unchanged.
    assert chunk_federation._peer_visible_sources(candidates, "nb-lib") == ("live",)
    # A subjectless (global) run keeps its live, budgeted coverage read.
    with source_scope_context(
        "nb-x", None, None, {"nb-x": ["x-1"], "nb-lib": ["lib-1"]},
        subjectless=True,
    ):
        assert chunk_federation._peer_visible_sources(
            candidates, "nb-lib"
        ) == ("live",)


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
    per_mount = ["visible"] * mount_count
    with default_ceiling_context(NB, "bob", open_store.readers()):
        assert len(current_source_scope().source_ids) == source_count
    # 3 + M: participants, visible(nb), hidden, one visible read per mount.
    assert open_store.calls == ["participants", "visible", "hidden", *per_mount]

    closed_store = _Store(
        visible=visible, mounts=mounts, hidden={"bob": hidden_ids}, types=types,
    )
    with memory_access_context(False), default_ceiling_context(
        NB, "bob", closed_store.readers()
    ):
        assert "mem-own" not in current_source_scope().hidden_source_ids
    assert closed_store.calls == [
        "participants", "visible", "hidden", "memory_sources", *per_mount,
    ]

    submitted_store = _Store(visible=visible, mounts=mounts)
    with default_ceiling_context(
        NB, "bob", submitted_store.readers(),
        local_scope={"mode": "include", "source_ids": ["s-0"]},
    ):
        pass
    # 1 + M.
    assert submitted_store.calls == ["participants", *per_mount]


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
    real_ordered = source_scope_module._ordered_source_ids

    def counting_ordered(values):
        order, frozen = real_ordered(values)
        built.append(len(frozen))
        return order, frozen

    monkeypatch.setattr(source_scope_module, "_ordered_source_ids", counting_ordered)
    set_sizes: list[int] = []
    real_set = set

    def recording_set(iterable=()):
        values = list(iterable)
        set_sizes.append(len(values))
        return real_set(values)

    # The element-type check (``set(map(type, ids))``) never runs on a ceiling
    # this constructor built: it is a ``_CheckedSourceIds``, so neither
    # normalisation on install re-checks it.
    monkeypatch.setattr(source_scope_module, "set", recording_set, raising=False)
    with default_ceiling_context(NB, "bob", store.readers()):
        scope = current_source_scope()
        assert isinstance(scope.source_ceiling_for("nb-lib"), frozenset)
        assert len(scope.source_ceiling_for("nb-lib")) == 49_000
        # The hand-out is the reader's own order, not a sort of the set.
        assert scope.ceiling_hand_out("nb-lib") == tuple(big)
    assert max(sorted_sizes, default=0) <= 2, sorted_sizes
    assert max(set_sizes, default=0) < 49_000, set_sizes
    # One build per id list -- the active notebook's visible (49k) and hidden
    # (1) halves and the two mounted libraries (49k, 1) -- and none twice:
    # ``source_scope_context`` and ``__post_init__`` reuse the frozenset the
    # per-library read already built instead of copying it again.
    assert real_sorted(built) == [1, 1, 49_000, 49_000]


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
# Real stores wired as the entry points will wire them.  The builders are
# backend-neutral; ``tests/postgres/test_default_source_ceiling_pg.py`` runs
# the same assertions against PostgreSQL.
# ---------------------------------------------------------------------------

_NOW = "2026-09-29T00:00:00+00:00"


def build_real_fixture(repo, placeholder: str) -> dict[str, Any]:
    """Two members share ``nb``, each with a confirmed Memory; ``lib`` (mounted,
    same owner) holds a visible source plus Memory/Knowhow projections of its
    own; ``lib2`` (mounted) holds one visible source; ``late`` is not mounted
    yet.  Returns the ids plus a ``mount(base_id)`` callable."""
    from app.models.schemas import NotebookCreate
    from app.services.sqlite_repository import reset_request_user, set_request_user

    ph = placeholder
    alice = repo.create_user("a00123456", "password-12")
    bob = repo.create_user("b00123456", "password-12")
    token = set_request_user(alice)
    try:
        nb = repo.create_notebook(NotebookCreate(name="共享库")).id
        lib = repo.create_notebook(NotebookCreate(name="参考库")).id
        lib2 = repo.create_notebook(NotebookCreate(name="参考库二")).id
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
                f"VALUES ({ph},{ph},{ph},NULL,NULL,'ask_answer','confirmed',"
                f"{ph},{ph},{ph},{ph})",
                (memory_id, notebook_id, user_id, "记忆", "内容", _NOW, _NOW),
            )
        insert(notebook_id, source_id, "memory", memory_id=memory_id)

    def mount(base_id):
        with repo._write() as db:
            db.execute(
                "INSERT INTO notebook_bases"
                "(notebook_id,base_notebook_id,created_at,created_by) "
                f"VALUES ({ph},{ph},{ph},{ph})",
                (nb, base_id, _NOW, alice.id),
            )

    insert(nb, "src-visible", "pdf")
    insert(nb, "src-knowhow", "knowhow")
    memory(nb, alice.id, "mem-alice", "src-memory-alice")
    memory(nb, bob.id, "mem-bob", "src-memory-bob")
    insert(lib, "lib-visible", "pdf")
    insert(lib, "lib-knowhow", "knowhow")
    memory(lib, alice.id, "mem-lib", "lib-memory")
    insert(lib2, "lib2-visible", "pdf")
    insert(late, "late-visible", "pdf")
    mount(lib)
    mount(lib2)
    return {
        "nb": nb, "lib": lib, "lib2": lib2, "late": late,
        "alice": alice.id, "bob": bob.id, "mount": mount,
    }


def real_readers(repo, emit=None, read_workers: int = 1) -> CeilingReaders:
    """The production wiring: bound store methods, nothing else (plus the
    store's ``read_workers``: 1 for SQLite, ``POSTGRES_MOUNTED_READ_WORKERS``
    for PostgreSQL)."""
    sources = repo._runtime.source_store

    def memory_sources(notebook_id: str) -> list[str]:
        with sources.database.connect() as db:
            return sources.memory_source_ids(db, notebook_id)

    return CeilingReaders(
        participants=repo._runtime.notebook_store.participant_notebook_ids,
        visible=sources.all_visible_source_ids,
        hidden=sources.hidden_source_ids,
        memory_sources=memory_sources,
        emit=emit,
        read_workers=read_workers,
    )


def _real_sqlite_fixture(tmp_path, monkeypatch):
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ceiling.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())
    return repo, build_real_fixture(repo, "?")


def _real_readers(repo, emit=None) -> CeilingReaders:
    return real_readers(repo, emit=emit)


def assert_default_ceiling_over_real_stores(repo, ids, read_workers: int = 1) -> None:
    """Bob's default ceiling admits his own Memory and the shared Knowhow,
    never Alice's Memory, and only a mounted library's visible source; a
    library mounted mid-run is refused; with the Memory channel closed his own
    Memory leaves the ceiling without the drift probe reporting drift."""
    sources = repo._runtime.source_store
    nb, lib, late, bob = ids["nb"], ids["lib"], ids["late"], ids["bob"]
    # Ids whose store order (``ORDER BY id`` under the database's collation)
    # need not match a Python sort: the hand-out must be the store's order.
    extra = ("lib2-Zeta", "lib2-alpha", "lib2_beta", "lib2-10", "lib2-9")
    for source_id in extra:
        sources.insert_source(
            source_id=source_id, notebook_id=ids["lib2"], title=source_id,
            source_type="pdf", status="active", parse_status="parsed",
            file_name="", file_path="", file_size=0, file_hash="",
            summary="", doc_type="", memory_id="",
        )
    readers = real_readers(repo, read_workers=read_workers)
    with default_ceiling_context(nb, bob, readers):
        scope = current_source_scope()
        assert scope is not None
        assert scope.source_ids == frozenset({"src-visible"})
        assert scope.hidden_source_ids == frozenset(
            {"src-knowhow", "src-memory-bob"}
        )
        assert source_allowed(nb, "src-memory-alice") is False
        assert scope.source_ceiling_for(lib) == frozenset({"lib-visible"})
        assert scope.source_ceiling_for(ids["lib2"]) == frozenset(
            {"lib2-visible", *extra}
        )
        assert scope.ceiling_hand_out(ids["lib2"]) == tuple(
            sources.all_visible_source_ids(ids["lib2"])
        )
        assert source_allowed(lib, "lib-memory") is False
        assert source_allowed(lib, "lib-knowhow") is False
        assert current_source_scope_payload() is None
        assert source_scope_restricted() is False
        # The live universe equals the freeze: no drift is reported.
        assert source_scope_visible_universe_matches(
            nb, sources.all_visible_source_ids(nb),
            sources.hidden_source_ids(nb, bob),
        ) is True

        ids["mount"](late)
        assert late in repo._runtime.notebook_store.participant_notebook_ids(nb)
        assert notebook_in_scope(late) is False
        assert scoped_allowed_source_ids(late) == ()

    with memory_access_context(False), default_ceiling_context(
        nb, bob, readers
    ):
        scope = current_source_scope()
        assert scope.hidden_source_ids == frozenset({"src-knowhow"})
        assert scope.withheld_hidden_source_ids == frozenset({"src-memory-bob"})
        # Fail-closed until E2-2: withheld Memory switches the
        # non-partitioned channels off.
        assert source_scope_visible_universe_matches(
            nb, sources.all_visible_source_ids(nb),
            sources.hidden_source_ids(nb, bob),
        ) is False


def test_default_ceiling_over_real_sqlite_stores(tmp_path, monkeypatch):
    repo, ids = _real_sqlite_fixture(tmp_path, monkeypatch)
    assert_default_ceiling_over_real_stores(repo, ids)


def test_each_mounted_library_visible_set_is_read_once_per_run(tmp_path, monkeypatch):
    """Statement count on the real SQLite store: constructor + federation read
    each mounted library's visible set ONCE in total per run.  Federation
    preparation (``_prepared_peer``, what every federated chunk arm calls) and
    the KG peer-ceiling reuse (``_peer_visible_sources`` again) hand back the
    frozen ceiling instead of re-reading it."""
    from types import SimpleNamespace

    from app.services import chunk_federation
    from app.services.retrieval_run import retrieval_run

    repo, ids = _real_sqlite_fixture(tmp_path, monkeypatch)
    sources = repo._runtime.source_store
    statements: list[str] = []
    sources.database.connect().set_trace_callback(statements.append)
    # Mounted libraries are read on worker threads, each with its own
    # connection: trace those too.
    new_connection = sources.database._new_connection

    def traced_connection(*args, **kwargs):
        conn = new_connection(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sources.database, "_new_connection", traced_connection)
    candidates = SimpleNamespace(
        sources=sources, notebook_copy_stats=lambda _nb: {"copyable": True},
    )
    with default_ceiling_context(ids["nb"], ids["bob"], real_readers(repo)), \
            retrieval_run(run_kind="ask_chunk"):
        for _arm in range(3):  # semantic, keyword and exact arms
            for lib in (ids["lib"], ids["lib2"]):
                visible, _peek = chunk_federation._prepared_peer(
                    candidates, lib, None, 0.0,
                )
                assert set(visible) == current_source_scope().source_ceiling_for(lib)
                chunk_federation._peer_visible_sources(candidates, lib)

    def visible_reads(notebook_id: str) -> int:
        return sum(
            1 for sql in statements
            if f"FROM sources WHERE notebook_id='{notebook_id}'" in sql
            and "NOT IN ('memory', 'knowhow')" in sql
        )

    assert visible_reads(ids["lib"]) == 1
    assert visible_reads(ids["lib2"]) == 1
    assert visible_reads(ids["nb"]) == 1
    sources.database.connect().set_trace_callback(None)
