"""The single-library empty-scope refusal judges ticked reference libraries by
EVIDENCE, not by count (user ruling 2026-10-28: Ask and reports alike).

``ask_routes._scope_is_empty`` (the 409 「当前检索范围为空」 that Ask, report
create/confirm/generate and the report follow-up scope share) used to read the
library dimension as ``bool(notebook_ids)``: clear every local source, tick one
library that holds nothing retrieval can reach, and the request went through
with an empty evidence universe.  It now asks the same question
``ask_available`` asks of the mounted libraries
(``NotebookSummaryQuery._base_evidence_available``), narrowed to the ticked
ones: a usable graph, or visible text the federated passage lane reaches --
and the lane's bound applies to the ticked libraries in mount order, exactly as
the retrieval seat applies it.

Defined once; ``test_scope_empty_by_evidence.py`` runs every case on SQLite and
``postgres/test_scope_empty_by_evidence_pg.py`` on PostgreSQL, through the real
HTTP routes.  No model is configured: an ``/ask`` that passes the gate answers
deterministically.
"""
from __future__ import annotations

from itertools import count
from typing import Any, Callable

from app.core.request_context import reset_request_user, set_request_user
from app.models.identity import UserProfile
from app.models.source_scope import BaseNotebookScope
from app.repositories.ports import ChunkWrite
from tests.ask_report_followup_api_cases import (
    EMPTY_SCOPE,
    followup,
    ready,
    scoped_report,
)
from tests.public_page_mount_recheck_cases import mount
from tests.report_share_disclosure_cases import World

NOW = "2026-10-28T00:00:00"
CLEARED = {"mode": "include", "source_ids": []}
_KEYS = count(1)


# --- helpers -----------------------------------------------------------------


def _sql(world: World, statement: str) -> str:
    if type(world.repo).__name__ == "PostgresRepository":
        return statement.replace("?", "%s")
    return statement


def library(
    world: World, name: str, *, text: bool = False, graph: str | None = None,
    document: bool = True,
) -> tuple[str, str]:
    """``(library id, its document source id)``: an owner's library Alice can
    read.  ``text`` gives the document one chunk (what the passage lane
    reaches); ``graph`` adds one knowledge object in that status;
    ``document=False`` leaves the library without any source at all."""
    lib = world.client.post(
        "/api/notebooks", json={"name": name}, headers=world.owner.headers
    ).json()["id"]
    world.repo._runtime.sharing.add_member(lib, world.alice.id)
    source_id = f"src-{name}-{next(_KEYS)}"
    if document:
        world.repo._runtime.source_ingestion.sources.insert_source(
            source_id=source_id, notebook_id=lib, title=name,
            source_type="upload", status="active", parse_status="parsed",
            file_name=f"{name}.md", file_path="", file_size=1, file_hash="h",
            summary="", doc_type="",
        )
    if text:
        give_text(world, lib, source_id)
    if graph is not None:
        with world.repo._runtime.database.write() as db:
            db.execute(
                _sql(world, "INSERT INTO knowledge_objects "
                     "(id,notebook_id,object_type,status,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?)"),
                (f"ko-{source_id}", lib, "concept", graph, NOW, NOW),
            )
    return lib, source_id


def give_text(world: World, lib: str, source_id: str) -> None:
    world.repo._runtime.chunk_store.replace_source_chunks(
        source_id, lib,
        [ChunkWrite(id=f"chunk-{source_id}", text="退火工艺对比", section_path="",
                    element_ids=())],
        created_at=f"{NOW}+00:00",
    )


def take_text(world: World, lib: str, source_id: str) -> None:
    world.repo._runtime.chunk_store.replace_source_chunks(
        source_id, lib, [], created_at=f"{NOW}+00:00",
    )


def _bases(ids: list[str]) -> dict:
    return {"mode": "include", "notebook_ids": list(ids)}


def ask(world: World, *, sources: dict | None, bases: dict | None,
        notebook: str | None = None):
    body: dict[str, Any] = {"question": "两种退火工艺的差异？", "mode": "chunk"}
    if sources is not None:
        body["source_scope"] = sources
    if bases is not None:
        body["base_scope"] = bases
    return world.client.post(
        f"/api/notebooks/{notebook or world.notebook}/ask",
        json=body, headers=world.alice.headers,
    )


def create_report(world: World, *, sources: dict | None, bases: dict | None,
                  notebook: str | None = None):
    body: dict[str, Any] = {"question": "两种退火工艺的差异？"}
    if sources is not None:
        body["source_scope"] = sources
    if bases is not None:
        body["base_scope"] = bases
    return world.client.post(
        f"/api/notebooks/{notebook or world.notebook}/reports",
        json=body, headers=world.alice.headers,
    )


def _refused(response) -> bool:
    return (
        response.status_code == 409
        and response.json() == {"detail": EMPTY_SCOPE}
        and response.headers.get("X-User-Message") == "1"
    )


def _assert_both_entries(world: World, bases: list[str], *, empty: bool,
                         where: str) -> None:
    """The Ask entry and the report entry give the same verdict."""
    asked = ask(world, sources=CLEARED, bases=_bases(bases))
    created = create_report(world, sources=CLEARED, bases=_bases(bases))
    if empty:
        assert _refused(asked), (where, asked.status_code, asked.text)
        assert _refused(created), (where, created.status_code, created.text)
    else:
        assert asked.status_code == 200, (where, asked.text)
        assert created.status_code == 200, (where, created.text)


def _as_alice(world: World):
    return set_request_user(UserProfile(
        id=world.alice.id, email="", display_name="alice", role="user",
    ))


# --- the refusal ---------------------------------------------------------------


def case_a_ticked_library_nothing_can_be_retrieved_from_is_an_empty_scope(
    world: World,
) -> None:
    """Local cleared, one library ticked that has neither a usable graph nor
    visible text: 409 on Ask and on report create.  Ticking a library with
    text next to it lifts the refusal -- the verdict is the conjunction."""
    ready(world)  # Alice's confirmed Memory: the notebook is askable at all
    live, _ = library(world, "z-live", text=True)
    # Mounted but never ticked: a usable graph elsewhere must not hold up a
    # selection that leaves it out.
    unticked_graph, _ = library(world, "graph", graph="approved")
    shapes = {
        "unparsed document": library(world, "unparsed")[0],
        "no document": library(world, "bare", document=False)[0],
        "deprecated graph only": library(world, "deprecated", graph="deprecated")[0],
    }
    mount(world, [live, unticked_graph, *shapes.values()])
    for where, dead in shapes.items():
        _assert_both_entries(world, [dead], empty=True, where=where)
        _assert_both_entries(world, [dead, live], empty=False, where=f"{where} + text")


def case_a_ticked_library_with_reachable_evidence_holds_the_scope(
    world: World,
) -> None:
    """Visible text, or a usable graph without any chunk, each hold the scope."""
    ready(world)
    text, _ = library(world, "text", text=True)
    graph, _ = library(world, "graph", graph="approved")
    mount(world, [text, graph])
    _assert_both_entries(world, [text], empty=False, where="text")
    _assert_both_entries(world, [graph], empty=False, where="usable graph")


def case_the_passage_bound_applies_to_the_ticked_libraries_in_mount_order(
    world: World,
) -> None:
    """``CHUNK_FEDERATION_MAX_PARTICIPANTS=2`` keeps the active notebook and ONE
    library.  The seat filters by the ticked set first and bounds second, in
    mount order -- not in the order the request lists them.  So ticking a
    text-less library that sorts first next to one with text leaves the text
    past the bound (empty), while ticking the text alone brings it inside."""
    world.monkeypatch.setattr(world.repo.settings, "chunk_federation_max_participants", 2)
    ready(world)
    first, _ = library(world, "a-first")
    second, _ = library(world, "b-second", text=True)
    mount(world, [second, first])
    marker = _as_alice(world)
    try:
        order = [ref.id for ref in world.repo.get_notebook(world.notebook).base_notebooks]
    finally:
        reset_request_user(marker)
    assert order == [first, second]
    _assert_both_entries(world, [second, first], empty=True, where="text past the bound")
    _assert_both_entries(world, [second], empty=False, where="text alone, inside")


def case_the_counted_libraries_are_the_ones_the_passage_lane_searches(
    world: World,
) -> None:
    """Same-source pin: for every ticked set and bound, the libraries the
    refusal counts text in are exactly the ones the federated passage lane
    searches under that same library scope."""
    from app.services.chunk_federation import federation_participant_ids
    from app.services.source_scope import source_scope_context

    libraries = [library(world, f"lib-{i}", text=True)[0] for i in range(4)]
    mount(world, libraries)
    summaries = world.repo._runtime.notebook_summaries
    selections = [libraries, libraries[2:], [libraries[3], libraries[0]], libraries[1:2]]
    marker = _as_alice(world)
    try:
        for enabled, cap in ((True, 8), (True, 2), (True, 3), (False, 8)):
            world.monkeypatch.setattr(world.repo.settings, "chunk_federation_enabled", enabled)
            world.monkeypatch.setattr(
                world.repo.settings, "chunk_federation_max_participants", cap)
            summary = world.repo.get_notebook(world.notebook)
            for ticked in selections:
                counted = summaries._chunk_lane_base_ids(
                    summary, summaries._base_selection(summary, ticked))
                with source_scope_context(
                    world.notebook, None,
                    BaseNotebookScope(mode="include", notebook_ids=ticked),
                ):
                    searched = federation_participant_ids(
                        world.repo.retrieval.candidates, world.notebook,
                    ) - {world.notebook}
                assert set(counted) == searched, (enabled, cap, ticked)
    finally:
        reset_request_user(marker)


def case_with_the_passage_lane_off_only_a_graph_holds_the_scope(world: World) -> None:
    """``CHUNK_FEDERATION_ENABLED=0``: library text is not retrieved at all, so
    it holds nothing; a usable graph is not part of that lane and still does."""
    world.monkeypatch.setattr(world.repo.settings, "chunk_federation_enabled", False)
    ready(world)
    text, _ = library(world, "text", text=True)
    graph, _ = library(world, "graph", graph="approved")
    mount(world, [text, graph])
    _assert_both_entries(world, [text], empty=True, where="text, lane off")
    _assert_both_entries(world, [graph], empty=False, where="graph, lane off")


# --- what did not change ---------------------------------------------------------


def case_a_non_empty_local_dimension_never_asks_the_libraries(world: World) -> None:
    """Most requests keep a local source ticked: the library half is never
    evaluated, so they pay nothing new -- even with only a dead library ticked."""
    ready(world)
    dead, _ = library(world, "dead")
    mount(world, [dead])

    def boom(*_args, **_kwargs):
        raise AssertionError("the library half must not be asked")

    world.monkeypatch.setattr(world.repo._runtime.catalog, "base_evidence_available", boom)
    kept = {"mode": "include", "source_ids": [world.doc_source]}
    everything = {"mode": "exclude", "source_ids": []}
    for sources, bases in ((kept, _bases([dead])), (everything, everything),
                           (None, _bases([dead]))):
        asked = ask(world, sources=sources, bases=bases)
        assert asked.status_code == 200, asked.text
        created = create_report(world, sources=sources, bases=bases)
        assert created.status_code == 200, created.text


def case_an_untouched_library_dimension_reuses_the_probe_the_open_paid(
    world: World,
) -> None:
    """A library-only notebook (nothing local) asked with every library ticked
    -- the browser's default request -- asks the libraries exactly the question
    the notebook summary already answered for ``ask_available``: reused, no
    second probe.  Only a real narrowing probes the ticked set."""
    headers = world.alice.headers
    bare = world.client.post(
        "/api/notebooks", json={"name": "只挂参考库"}, headers=headers).json()["id"]
    text, _ = library(world, "text", text=True)
    other, _ = library(world, "other", text=True)
    assert world.client.put(f"/api/notebooks/{bare}/bases",
                            json={"base_notebook_ids": [text, other]},
                            headers=headers).status_code == 200
    summaries = world.repo._runtime.notebook_summaries
    real_probe = summaries._base_evidence_available
    real_selected = summaries.selected_base_evidence_available
    inside: list = []
    consulted: list = []
    probes: list = []  # (asked from inside the refusal's check?, selection)

    def selected(*args, **kwargs):
        consulted.append(args[1])
        inside.append(True)
        try:
            return real_selected(*args, **kwargs)
        finally:
            inside.pop()

    def probe(db, summary, user_id, selection=None):
        probes.append((bool(inside), selection))
        return real_probe(db, summary, user_id, selection)

    world.monkeypatch.setattr(summaries, "selected_base_evidence_available", selected)
    world.monkeypatch.setattr(summaries, "_base_evidence_available", probe)
    everything = {"mode": "exclude", "source_ids": []}
    for bases in (everything, _bases([other, text]), None):
        for entry in (ask, create_report):
            consulted.clear()
            probes.clear()
            response = entry(world, sources=everything, bases=bases, notebook=bare)
            where = (entry.__name__, bases)
            assert response.status_code == 200, (where, response.text)
            assert consulted, ("the refusal must ask the library half", where)
            assert probes, ("the notebook open probes the libraries", where)
            assert all(not from_refusal for from_refusal, _ in probes), (where, probes)
    consulted.clear()
    probes.clear()
    asked = ask(world, sources=everything, bases=_bases([other]), notebook=bare)
    assert asked.status_code == 200, asked.text
    assert (True, [other]) in probes


# --- the report's later gates ------------------------------------------------------


def case_report_confirmation_refuses_a_library_that_lost_its_evidence(
    world: World,
) -> None:
    """Created with one text library ticked; the text goes away before the
    intent is confirmed -- the re-frozen scope is empty and confirmation is
    refused instead of planning against nothing."""
    ready(world)
    lib, source_id = library(world, "text", text=True)
    mount(world, [lib])
    created = create_report(world, sources=CLEARED, bases=_bases([lib]))
    assert created.status_code == 200, created.text
    rid = created.json()["report_id"]
    take_text(world, lib, source_id)
    understanding = world.repo.get_report(world.notebook, rid)["understanding"]
    understanding.update(resolved_question="q?", ambiguities=[], needs_clarification=False)
    world.repo.update_report(world.notebook, rid, status="intent_ready",
                             understanding=understanding)
    confirmed = world.client.post(
        f"/api/notebooks/{world.notebook}/reports/{rid}/intent",
        json={"resolved_question": "q?", "answers": []}, headers=world.alice.headers,
    )
    assert _refused(confirmed), confirmed.text
    assert world.repo.get_report(world.notebook, rid)["status"] == "intent_ready"


def case_followup_falls_back_when_the_ticked_library_holds_nothing_any_more(
    world: World,
) -> None:
    """The follow-up scope hands back only a selection ``/ask`` accepts: once
    the report's one ticked library holds nothing retrievable, both dimensions
    go back to everything."""
    ready(world)
    lib, source_id = library(world, "text", text=True)
    other, _ = library(world, "other", text=True)
    mount(world, [lib, other])
    report_id = scoped_report(world, source_scope=CLEARED, base_scope=_bases([lib]))
    body = followup(world, world.alice, report_id).json()
    assert body["fell_back_to_all"] is False
    assert body["base_scope"]["notebook_ids"] == [lib]
    take_text(world, lib, source_id)
    body = followup(world, world.alice, report_id).json()
    assert body["fell_back_to_all"] is True
    assert body["base_scope"]["mode"] == "exclude"
    assert body["base_scope"]["notebook_ids"] == []
    assert body["dropped_libraries"] == 0


CASES: dict[str, Callable[[World], None]] = {
    name: case for name, case in globals().items()
    if name.startswith("case_") and callable(case)
}
