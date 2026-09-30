"""``refreshed_ceiling_context`` over every outer shape it can meet.

A property grid (adopted from the third E1-1 review): 10 outer scopes x 4
refreshed dimensions x 3 Memory-channel states (open; closed at the outer
freeze; closed only at the refresh).  Between the outer freeze and the refresh
a library is mounted (``nb-late``) and a source uploaded (``src-new``), then
every (notebook, source) pair is probed through ``allows``.  The rule the
refresh must keep (see its docstring):

* the SELECTION never widens: on a local dimension the caller did not
  refresh, nothing the outer refused is admitted; on a library dimension the
  caller did not refresh, a library is admitted only where the outer's
  library SELECTION admits it -- an unsubmitted or exclusion-form library
  dimension resolves against the current mount set, so a library mounted
  since the freeze may join, visible sources only;
* a refreshed dimension never goes beyond the caller's refreshed selection;
* with the Memory channel closed, no Memory source is admitted;
* another member's Memory is never admitted;
* a mounted library never exposes a non-visible source the outer did not.
"""
from __future__ import annotations

import contextlib
import itertools

import pytest

from app.services.source_scope import (
    ActiveSourceScope,
    CeilingReaders,
    current_source_scope,
    default_ceiling_context,
    memory_access_context,
    refreshed_ceiling_context,
    source_scope_context,
)

NB = "nb-active"
MEMORY = {"src-memory-bob", "src-memory-alice"}


class _Store:
    def __init__(self) -> None:
        self.visible_by = {
            NB: ["src-a", "src-b"], "nb-lib": ["lib-1"],
            "nb-lib2": ["lib2-1"], "nb-late": ["late-1"],
        }
        self.mounts = ["nb-lib", "nb-lib2"]
        self.hidden_by = {
            (NB, "bob"): ["src-knowhow", "src-memory-bob"],
            ("nb-lib", "bob"): ["lib-knowhow"],
            ("nb-late", "bob"): ["late-knowhow"],
        }

    def readers(self) -> CeilingReaders:
        return CeilingReaders(
            participants=lambda nb: [nb, *self.mounts],
            visible=lambda nb: list(self.visible_by.get(nb, [])),
            hidden=lambda nb, owner: list(self.hidden_by.get((nb, owner), [])),
            memory_sources=lambda nb: sorted(MEMORY) if nb == NB else [],
        )


LOCAL = {
    "mode": "include", "source_ids": ["src-a", "src-new"],
    "hidden_source_ids": ["src-knowhow", "src-memory-bob"],
    "narrowed": True, "owner_id": "bob",
}
LIBRARY = {"mode": "include", "notebook_ids": ["nb-lib", "nb-late"], "narrowed": True}


def _outer(kind: str, store: _Store):
    readers = store.readers()
    return {
        "none": contextlib.nullcontext,
        "default": lambda: default_ceiling_context(NB, "bob", readers),
        "legacy_narrowed": lambda: source_scope_context(
            NB,
            {"mode": "include", "source_ids": ["src-a"], "narrowed": True,
             "owner_id": "bob"},
            {"mode": "exclude", "notebook_ids": ["nb-lib2"], "narrowed": True},
        ),
        "legacy_exclude": lambda: source_scope_context(
            NB, {"mode": "exclude", "source_ids": ["src-b"]},
        ),
        "legacy_binds_nothing": lambda: source_scope_context(
            NB, None,
            {"mode": "include", "notebook_ids": ["nb-lib"], "narrowed": False},
        ),
        "legacy_own_entry": lambda: source_scope_context(
            NB, None, None, {NB: ["src-a"]},
        ),
        "legacy_own_entry_with_memory": lambda: source_scope_context(
            NB, None, None, {NB: ["src-a", "src-memory-bob"]},
        ),
        "subjectless": lambda: source_scope_context(
            NB, None, None, {NB: ["src-a"], "nb-lib": ["lib-1"]},
            subjectless=True,
        ),
        "another_notebook": lambda: source_scope_context(
            "nb-other", {"mode": "include", "source_ids": ["o-1"]},
        ),
        "ceilings_total_own_entry": lambda: source_scope_context(
            NB, None, None, {NB: ["src-a"]}, ceilings_total=True,
        ),
    }[kind]()


NOTEBOOKS = [NB, "nb-lib", "nb-lib2", "nb-late", "nb-unnamed"]
SOURCES = [
    "src-a", "src-b", "src-new", "src-knowhow", "src-memory-bob",
    "src-memory-alice", "lib-1", "lib-knowhow", "lib2-1", "late-1",
    "late-knowhow", "x",
]


def _allows(scope: ActiveSourceScope | None, notebook_id: str, source_id: str) -> bool:
    return True if scope is None else scope.allows(notebook_id, source_id)


def _selection_admits(outer: ActiveSourceScope | None, notebook_id: str) -> bool:
    """The outer's library SELECTION alone -- not its frozen mount set."""
    if outer is None or not outer.base_ceiling_active:
        return True
    if outer.base_mode == "include":
        return notebook_id in outer.base_notebook_ids
    return notebook_id not in outer.base_notebook_ids


def _violations(outer, new, store, local, base, closed) -> list[str]:
    found: list[str] = []
    for notebook_id, source_id in itertools.product(NOTEBOOKS, SOURCES):
        if not _allows(new, notebook_id, source_id):
            continue
        before = _allows(outer, notebook_id, source_id)
        if notebook_id == NB:
            if local is None and not before:
                found.append(f"unrefreshed local dimension widened: {source_id}")
            if local is not None and source_id not in {
                *LOCAL["source_ids"], *LOCAL["hidden_source_ids"],
            }:
                found.append(f"beyond the refreshed selection: {source_id}")
            if closed and source_id in MEMORY:
                found.append(f"Memory admitted with the channel closed: {source_id}")
            if source_id == "src-memory-alice":
                found.append("another member's Memory admitted")
            continue
        visible = source_id in store.visible_by.get(notebook_id, [])
        if base is None and not before and not (
            _selection_admits(outer, notebook_id)
            and notebook_id in store.mounts and visible
        ):
            found.append(f"unrefreshed library selection widened: {notebook_id}/{source_id}")
        if base is not None and notebook_id not in LIBRARY["notebook_ids"] and not new.subjectless:
            found.append(f"library beyond the refreshed selection: {notebook_id}")
        if not visible and not before:
            found.append(f"non-visible source of a mounted library: {notebook_id}/{source_id}")
    return found


KINDS = [
    "none", "default", "legacy_narrowed", "legacy_exclude",
    "legacy_binds_nothing", "legacy_own_entry", "legacy_own_entry_with_memory",
    "subjectless", "another_notebook", "ceilings_total_own_entry",
]
CHANNELS = ["open", "closed", "closed_at_refresh"]
CELLS = list(itertools.product(KINDS, ["local", "library", "both", "neither"], CHANNELS))


@pytest.mark.parametrize("kind, dims, channel", CELLS)
def test_a_refresh_never_widens_the_selection(kind, dims, channel):
    store = _Store()
    local = LOCAL if dims in ("local", "both") else None
    base = LIBRARY if dims in ("library", "both") else None
    closed = channel != "open"
    at_freeze = memory_access_context(False) if channel == "closed" else contextlib.nullcontext()
    at_refresh = (
        memory_access_context(False) if channel == "closed_at_refresh"
        else contextlib.nullcontext()
    )
    with at_freeze, _outer(kind, store):
        outer = current_source_scope()
        store.mounts.append("nb-late")          # mounted after the outer freeze
        store.visible_by[NB].append("src-new")  # uploaded after the outer freeze
        with at_refresh:
            if kind == "another_notebook":
                with pytest.raises(ValueError):
                    with refreshed_ceiling_context(
                        NB, "bob", store.readers(), local_scope=local, base_scope=base,
                    ):
                        pass
                return
            with refreshed_ceiling_context(
                NB, "bob", store.readers(), local_scope=local, base_scope=base,
            ):
                new = current_source_scope()
                if kind == "subjectless":
                    assert new is outer
                    return
                assert new is not None and new.ceilings_total
                assert _violations(outer, new, store, local, base, closed) == []
