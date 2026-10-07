"""E1 (E-1 end to end): a global run never reads a library the plan did not
select, even when a selected anchor mounts it.

The anchor library mounts another library the asker could select but did not.
``global_ask_run`` freezes per-library ceilings for the selected participants
only and sets ``ceilings_total``: a library with no frozen entry participates
in nothing.  Every channel of a real global run -- the first-round KG search,
graph expansion, community peers, chains, element search and the chunk lane --
must leave the mounted library's text out of every model prompt and out of the
answer's references.
"""
from __future__ import annotations

import json

import pytest

from tests import global_citation_e2e_kit as kit

SECRET = "MOUNTSECRET"
TERM = "低温增益"


def _chunk(repo, source_id, notebook_id):
    repo._chunk_and_embed_source(source_id)
    repo.backfill_chunk_fts(notebook_id)


def _library(repo, name, source_id, marker=""):
    nb = kit.notebook(repo, name)
    elements = kit.seed_document(repo, nb.id, source_id, f"{name}文档", [
        f"{TERM} 实验记录 {marker}", f"{TERM} 结论 {marker}",
    ])
    repo.store_kg(nb.id, source_id, [
        {"local_id": "A", "object_type": "claim",
         "payload": {"name": f"{TERM}论断{marker}", "section_path": "1"},
         "evidence": [kit.evidence(source_id, elements[0], f"{TERM} 实验记录 {marker}")]},
        {"local_id": "B", "object_type": "claim",
         "payload": {"name": f"{TERM}结论{marker}", "section_path": "2"},
         "evidence": [kit.evidence(source_id, elements[1], f"{TERM} 结论 {marker}")]},
    ], [
        {"source_local_id": "A", "target_local_id": "B", "edge_type": "derived_from",
         "evidence": [kit.evidence(source_id, elements[0], f"{TERM} 实验记录 {marker}")]},
    ])
    _chunk(repo, source_id, nb.id)
    return nb


def _anchor_mounting_an_unselected_library(repo):
    anchor = _library(repo, "锚点", "s-anchor")
    mounted = _library(repo, "挂载", "s-mounted", SECRET)
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebook_bases(notebook_id,base_notebook_id,created_at,created_by) "
            "VALUES (?,?,?,?)",
            (anchor.id, mounted.id, kit.NOW, repo.current_user().id),
        )
    repo.collection_catalog.invalidate()
    return anchor, mounted


def _reflects(repo, anchor_id):
    start = kit.object_ids(repo, anchor_id)[f"{TERM}论断"]
    return [
        {"sufficient": False, "next_action": "expand_graph",
         "expand": {"object_id": start, "direction": "both"}, "reason": "深挖"},
        {"sufficient": False, "next_action": "expand_community",
         "community_focal": f"{TERM}论断", "reason": "横向对比"},
        {"sufficient": False, "next_action": "follow_chain",
         "follow_chain": {"start_object_id": start, "edge_type": "derived_from",
                          "direction": "out"}, "reason": "推导"},
        {"sufficient": False, "next_action": "search_elements",
         "search_query": TERM, "reason": "原文"},
        {"sufficient": False, "next_action": "search_chunks", "chunks_query": TERM,
         "chunks_keywords": f"{TERM} 实验", "reason": "段落"},
    ]


def _assert_nothing_from(mounted_id, answerer, result):
    assert result.status == "done", result.error
    assert answerer.prompts, "the run reached the answer model"
    for prompt in answerer.prompts:
        assert SECRET not in prompt
    dumped = json.dumps(result.answer.model_dump(mode="json"), ensure_ascii=False)
    assert SECRET not in dumped
    assert mounted_id not in {ref.notebook_id for ref in kit.references(result.answer)}


@pytest.mark.parametrize("mode", ["reasoning", "chunk"])
def test_the_same_library_selected_is_read(tmp_path, monkeypatch, mode):
    """Control: the seeding is reachable -- selecting the mounted library puts
    its text in front of the model on the same channels."""
    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        anchor, mounted = _anchor_mounting_an_unselected_library(repo)
        agent = kit.ScriptedAgent(_reflects(repo, anchor.id), plan_query=TERM)
        answerer = kit.bind_models(repo, agent)
        result = kit.global_answer(repo, [anchor.id, mounted.id], f"{TERM}的结论", mode=mode)

        assert result.status == "done", result.error
        assert any(SECRET in prompt for prompt in answerer.prompts)
    finally:
        repo.close()


@pytest.mark.parametrize("mode", ["reasoning", "chunk"])
def test_a_global_run_reads_nothing_from_an_unselected_mounted_library(
    tmp_path, monkeypatch, mode,
):
    repo = kit.make_repo(tmp_path, monkeypatch)
    try:
        anchor, mounted = _anchor_mounting_an_unselected_library(repo)
        agent = kit.ScriptedAgent(_reflects(repo, anchor.id), plan_query=TERM)
        answerer = kit.bind_models(repo, agent)
        result = kit.global_answer(repo, [anchor.id], f"{TERM}的结论", mode=mode)

        _assert_nothing_from(mounted.id, answerer, result)
        assert result.resolved_notebook_ids == [anchor.id]
        assert kit.references(result.answer), "the anchor's own evidence was found"
        if mode == "reasoning":
            walked = {step.step_type for step in result.answer.reasoning_trace}
            assert {"expand", "expand_community", "follow_chain",
                    "search_chunks"} <= walked, walked
    finally:
        repo.close()
