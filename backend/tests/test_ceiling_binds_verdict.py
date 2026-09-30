"""``source_scope.ceiling_binds`` — when the source ceiling binds KG re-reads.

Re-review F1(b): a ceiling that cannot exclude anything relevant must not
reach ``node_context``, so the ordinary UI request (everything ticked, nothing
changed since the freeze, no hidden source of the library unreadable to the
asker) takes the O(1) no-ceiling path and returns the bytes of a run without a
scope — hub fused descriptions included.  Each of the four arms turns the
verdict true on its own.  Also: F2 (cluster de-duplication on the two
pre-existing skip paths), F4 (the two memo keys), F5 (blank library id).
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.domain.retrieval import RetrievedKnowledge
from app.services.source_scope import (
    ceiling_binds,
    current_source_scope,
    library_source_ceiling,
    scoped_allowed_source_ids,
    scoped_source_ceiling,
    source_scope_context,
)
from tests.test_knowledge_context_source_ceiling import (
    ACTIVE,
    HIDDEN_SOURCE,
    OPEN_SOURCE,
    OPEN_TEXT,
    PEER,
    _ClusterKnowledge,
    _cluster_hit,
    _Notebooks,
    _occurrence,
    _Sources,
)
from app.services.evidence_context import EvidenceContextService


ALL = [OPEN_SOURCE, "doc-two"]


def _all_ticked(**extra):
    return source_scope_context(
        ACTIVE,
        {"mode": "include", "source_ids": ALL, "narrowed": False,
         "owner_id": "u-asker", **extra},
        None,
    )


class _Probes:
    def __init__(self, *, drifted=False, foreign=False):
        self.drifted_value, self.foreign_value = drifted, foreign
        self.calls = []

    def drifted(self):
        self.calls.append("drifted")
        return self.drifted_value

    def foreign(self):
        self.calls.append("foreign")
        return self.foreign_value


def _verdict(notebook_id, probes):
    return ceiling_binds(
        current_source_scope(), notebook_id,
        drifted=probes.drifted, foreign_hidden=probes.foreign,
    )


# ------------------------------------------------------------ the four arms
def test_all_ticked_undrifted_without_foreign_hidden_sources_does_not_bind():
    probes = _Probes()
    with _all_ticked():
        assert _verdict(ACTIVE, probes) is False
        assert _verdict(ACTIVE, probes) is False
    # Memoised per run and library: each probe ran once.
    assert probes.calls == ["drifted", "foreign"]


def test_arm_narrowed_by_the_user_binds():
    probes = _Probes()
    with source_scope_context(
        ACTIVE, {"mode": "include", "source_ids": [OPEN_SOURCE], "narrowed": True}, None,
    ):
        assert _verdict(ACTIVE, probes) is True
    assert probes.calls == []


def test_arm_drifted_binds():
    with _all_ticked():
        assert _verdict(ACTIVE, _Probes(drifted=True)) is True


def test_arm_subjectless_binds():
    with source_scope_context(
        ACTIVE, None, None,
        notebook_source_ceilings={ACTIVE: ALL, PEER: [OPEN_SOURCE]}, subjectless=True,
    ):
        assert _verdict(ACTIVE, _Probes()) is True
        assert _verdict(PEER, _Probes()) is True


def test_arm_foreign_hidden_source_binds():
    with _all_ticked():
        assert _verdict(ACTIVE, _Probes(foreign=True)) is True


def test_no_ceiling_never_binds_and_probes_nothing():
    probes = _Probes()
    with source_scope_context(ACTIVE, {"mode": "exclude", "source_ids": []}, None):
        assert _verdict(ACTIVE, probes) is False
        # A mounted library is never bound by the local checkboxes.
        assert _verdict(PEER, probes) is False
    assert probes.calls == []


# ------------------------------------------------- service: verdict false/true
class _HubKnowledge(_ClusterKnowledge):
    """A hub member whose store row carries a fused cluster description."""

    def node_context(self, notebook_id, object_id, **kwargs):
        row = super().node_context(notebook_id, object_id, **kwargs)
        row.update(definition="HUB fused description",
                   definition_basis="cluster_description")
        return row


def _knowledge():
    return _HubKnowledge({"ko-hub": [_occurrence(OPEN_SOURCE, OPEN_TEXT)]})


def _service(knowledge, verdict):
    return EvidenceContextService(
        notebooks=_Notebooks(), sources=_Sources(), knowledge=knowledge,
        settings=Settings(), ceiling_verdict=verdict,
    )


def test_verdict_false_passes_no_ceiling_and_keeps_the_unscoped_bytes():
    hits = [_cluster_hit("ko-hub")]
    plain_knowledge = _knowledge()
    plain = _service(plain_knowledge, lambda _nb: False).knowledge_context(ACTIVE, hits)
    knowledge = _knowledge()
    with _all_ticked():
        scoped = _service(knowledge, lambda _nb: False).knowledge_context(ACTIVE, hits)
    assert knowledge.pushed == ["<absent>"]
    assert scoped == plain
    assert "HUB fused description" in scoped[0]


def test_verdict_true_pushes_the_frozen_ceiling():
    knowledge = _knowledge()
    with _all_ticked():
        block, _ = _service(knowledge, lambda _nb: True).knowledge_context(
            ACTIVE, [_cluster_hit("ko-hub")])
    assert knowledge.pushed == [frozenset(ALL)]
    # A description judged by the store it was pushed to is kept.
    assert "HUB fused description" in block


def test_retrieval_service_skips_the_ceiling_when_it_does_not_bind():
    from app.services.retrieval_service import RetrievalService

    seen = []

    class _Graph:
        def node_context(self, notebook_id, object_id, **kwargs):
            seen.append(kwargs)
            return {"id": object_id, "name": "N", "occurrences": []}

    for verdict, expected in ((False, {}), (True, {"allowed_source_ids": frozenset(ALL)})):
        seen.clear()
        service = RetrievalService(candidates=None, graph=_Graph(), community_queries=None,
                                   ceiling_verdict=lambda _nb, v=verdict: v)
        with _all_ticked():
            row = service.node_context(ACTIVE, "ko-1")
        assert seen == [expected]
        # Bound and no readable occurrence left → the object is gone.
        assert row == ({"id": "ko-1", "name": "N", "occurrences": []} if not verdict else {})


# ----------------------------------------------------------- F2 skip paths
def test_a_hit_deleted_after_recall_still_marks_its_cluster_seen():
    """No ceiling: the old de-duplication stands — the first member's re-read
    raising KeyError marks the cluster seen, the second member is skipped."""

    class _Deleted(_ClusterKnowledge):
        def node_context(self, notebook_id, object_id, **kwargs):
            if object_id == "ko-first":
                raise KeyError(object_id)
            return super().node_context(notebook_id, object_id, **kwargs)

    knowledge = _Deleted({"ko-first": [], "ko-second": [_occurrence(OPEN_SOURCE, OPEN_TEXT)]})
    block, id_map = _service(knowledge, None).knowledge_context(
        ACTIVE, [_cluster_hit("ko-first"), _cluster_hit("ko-second")])
    assert id_map == {} and "ko-second" not in block


def test_a_hit_of_an_unticked_library_still_marks_its_cluster_seen():
    knowledge = _ClusterKnowledge({
        "ko-first": [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        "ko-second": [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
    })
    first = RetrievedKnowledge(
        object_id="ko-first", object_type="concept", payload={"name": "ko-first"},
        evidence=[], notebook_id=PEER, tier="base", relevance=0.9)
    with source_scope_context(ACTIVE, None, {"mode": "include", "notebook_ids": []}):
        block, id_map = _service(knowledge, None).knowledge_context(
            ACTIVE, [first, _cluster_hit("ko-second")])
    assert id_map == {} and "ko-second" not in block


def test_a_ceiling_drop_does_not_mark_the_cluster_seen():
    knowledge = _ClusterKnowledge({
        "ko-first": [_occurrence(HIDDEN_SOURCE, "HIDDEN")],
        "ko-second": [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
    })
    with source_scope_context(
        ACTIVE, {"mode": "include", "source_ids": [OPEN_SOURCE], "narrowed": True}, None,
    ):
        _block, id_map = _service(knowledge, None).knowledge_context(
            ACTIVE, [_cluster_hit("ko-first"), _cluster_hit("ko-second")])
    assert [entry["object_id"] for entry in id_map.values()] == ["ko-second"]


# ------------------------------------------------------------ F4 / F5 memo
def test_the_set_memo_never_hands_sql_producers_an_unordered_set():
    wide = [f"s{index:03d}" for index in range(50, 0, -1)]
    with source_scope_context(
        ACTIVE, {"mode": "include", "source_ids": wide, "narrowed": True}, None,
    ):
        assert isinstance(scoped_source_ceiling(ACTIVE), frozenset)
        assert scoped_allowed_source_ids(ACTIVE) == tuple(sorted(wide))


@pytest.mark.parametrize("mode", ["include", "exclude"])
def test_blank_library_id_reads_as_the_scopes_own_notebook(mode):
    with source_scope_context(
        ACTIVE, {"mode": mode, "source_ids": [OPEN_SOURCE], "narrowed": True}, None,
    ):
        scope = current_source_scope()
        assert scope.source_ceiling_binds("") is scope.source_ceiling_binds(ACTIVE) is True
        assert library_source_ceiling(scope, "") == library_source_ceiling(scope, ACTIVE)


# ------------------------------------------- real SQLite store, real verdict
@pytest.fixture
def repo(tmp_path, monkeypatch):
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings(_env_file=None))


def test_all_ticked_run_keeps_the_hub_description_and_a_narrowed_run_does_not(
    repo, monkeypatch,
):
    """The production verdict (drift + foreign-hidden probes) on a real store:
    an all-ticked, un-drifted run on a hub above the member bound gets no
    ceiling and exactly the unscoped bytes; a narrowed run binds, and the hub's
    description, unverifiable within budget, falls back to in-ceiling evidence."""
    from app.domain.knowledge_contracts import NODE_CONTEXT_CLUSTER_MEMBER_PROBE as bound
    from tests.test_node_context import _seed_hub

    nb, hub, src = _seed_hub(repo, bound + 1)
    runtime = repo._runtime
    service = runtime.evidence_context_component
    pushed = []
    original = runtime.knowledge.node_context

    def spy(*args, **kwargs):
        pushed.append(kwargs.get("allowed_source_ids", "<absent>"))
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.knowledge, "node_context", spy)
    hit = RetrievedKnowledge(object_id=hub, object_type="concept", payload={"name": "Hub"},
                             evidence=[], notebook_id=nb, tier="personal", relevance=0.9)
    plain = service.knowledge_context(nb, [hit], budget_chars=10_000)
    visible = runtime.source_store.all_visible_source_ids(nb)
    with source_scope_context(nb, {"mode": "include", "source_ids": visible,
                                   "narrowed": False, "owner_id": "u-asker"}, None):
        ticked = service.knowledge_context(nb, [hit], budget_chars=10_000)
    assert ticked == plain and "HUB fused description" in ticked[0]
    assert pushed == ["<absent>", "<absent>"]
    with source_scope_context(nb, {"mode": "include", "source_ids": [src],
                                   "narrowed": True, "owner_id": "u-asker"}, None):
        narrowed = service.knowledge_context(nb, [hit], budget_chars=10_000)
    assert "HUB fused description" not in narrowed[0]
    assert "HUB definition text" in narrowed[0]
    assert pushed[-1] == frozenset([src])


# ------------------------------------------- verify-on-read (codex #806 r4)
# A False verdict is memoised per run and library, but the library can change
# after it: a concurrent upload or Memory write merges evidence of a source
# outside the frozen ceiling into an object recall already admitted.  A row
# read without a ceiling is used as read only when every source it attributes
# text to is inside the ceiling; the first one that is not flips the memo and
# is itself re-read bound.
NEW_SOURCE = "doc-uploaded-after-the-verdict"   # outside the frozen ceiling
NEW_TEXT = "NEW upload text that arrived after the freeze"
DEF_TEXT = "definition quoted from the new upload"


def _row(object_id, occurrences, **extra):
    return {"id": object_id, "name": object_id,
            "occurrences": [dict(item) for item in occurrences],
            "definition": None, "definition_basis": None,
            "definition_source_id": None, "definition_element_id": None,
            "steps": None, **extra}


class _Drifting:
    """A store whose rows are fixed per object and which IGNORES
    ``allowed_source_ids`` (the service backstop is what these tests pin; the
    push itself is pinned by ``pushed``).  ``fold`` puts objects in clusters."""

    def __init__(self, rows, fold=None):
        self.rows, self.fold = rows, dict(fold or {})
        self.pushed: list[object] = []

    def cluster_fold(self, notebook_id, object_ids):
        return {oid: self.fold[oid] for oid in object_ids if oid in self.fold}

    def node_context(self, notebook_id, object_id, **kwargs):
        self.pushed.append(kwargs.get("allowed_source_ids", "<absent>"))
        row = self.rows[object_id]
        return {**row, "occurrences": [dict(item) for item in row["occurrences"]]}

    def in_network_relations(self, participant_ids, object_ids):
        return []

    def relation_support_counts(self, notebook_id, triples):
        return {triple: 1 for triple in triples}


def _hub_row(object_id):
    return _row(object_id, [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
                definition="HUB fused description",
                definition_basis="cluster_description")


def _real_verdict(probes):
    return lambda notebook_id: _verdict(notebook_id, probes)


def test_a_row_inside_the_ceiling_is_used_as_read_and_the_verdict_stands():
    rows = {"ko-hub": _hub_row("ko-hub"), "ko-two": _hub_row("ko-two")}
    hits = [_cluster_hit("ko-hub"), _cluster_hit("ko-two")]
    plain = _service(_Drifting(rows), lambda _nb: False).knowledge_context(ACTIVE, hits)
    probes, knowledge = _Probes(), _Drifting(rows)
    with _all_ticked():
        scoped = _service(knowledge, _real_verdict(probes)).knowledge_context(ACTIVE, hits)
        assert current_source_scope()._ceiling_binds_memo == {ACTIVE: False}
    assert scoped == plain and "HUB fused description" in scoped[0]
    assert knowledge.pushed == ["<absent>", "<absent>"]
    assert probes.calls == ["drifted", "foreign"]


def test_an_occurrence_outside_the_ceiling_flips_the_verdict_and_rereads_bound():
    rows = {
        "ko-merged": _row("ko-merged", [_occurrence(OPEN_SOURCE, OPEN_TEXT),
                                        _occurrence(NEW_SOURCE, NEW_TEXT)]),
        "ko-next": _hub_row("ko-next"),
    }
    probes, knowledge = _Probes(), _Drifting(rows)
    with _all_ticked():
        block, id_map = _service(knowledge, _real_verdict(probes)).knowledge_context(
            ACTIVE, [_cluster_hit("ko-merged"), _cluster_hit("ko-next")])
        assert current_source_scope()._ceiling_binds_memo == {ACTIVE: True}
    assert NEW_TEXT not in block and NEW_SOURCE not in str(id_map)
    assert [entry["object_id"] for entry in id_map.values()] == ["ko-merged", "ko-next"]
    assert id_map["k1"]["snippet"] == OPEN_TEXT
    # This hit: read unbounded, then re-read bound.  The next hit goes bound
    # directly, and the probes are not consulted again.
    assert knowledge.pushed == ["<absent>", frozenset(ALL), frozenset(ALL)]
    assert probes.calls == ["drifted", "foreign"]


def test_a_drifted_hit_with_nothing_inside_is_dropped_and_its_cluster_stays_open():
    rows = {
        "ko-first": _row("ko-first", [_occurrence(NEW_SOURCE, NEW_TEXT)]),
        "ko-second": _row("ko-second", [_occurrence(OPEN_SOURCE, OPEN_TEXT)]),
    }
    knowledge = _Drifting(rows, fold={"ko-first": "K", "ko-second": "K"})
    with _all_ticked():
        block, id_map = _service(knowledge, _real_verdict(_Probes())).knowledge_context(
            ACTIVE, [_cluster_hit("ko-first"), _cluster_hit("ko-second")])
    assert NEW_TEXT not in block
    assert [entry["object_id"] for entry in id_map.values()] == ["ko-second"]
    assert knowledge.pushed == ["<absent>", frozenset(ALL), frozenset(ALL)]


def test_a_definition_outside_the_ceiling_is_dropped_as_under_the_bound_path():
    rows = {"ko-defined": _row(
        "ko-defined", [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
        definition=DEF_TEXT, definition_basis="defines_evidence",
        definition_source_id=NEW_SOURCE, definition_element_id=f"el-{NEW_SOURCE}")}
    knowledge = _Drifting(rows)
    with _all_ticked():
        block, id_map = _service(knowledge, _real_verdict(_Probes())).knowledge_context(
            ACTIVE, [_cluster_hit("ko-defined")])
        assert current_source_scope()._ceiling_binds_memo == {ACTIVE: True}
    assert DEF_TEXT not in block
    # Falls back to the first in-ceiling occurrence, as the bound path does.
    assert id_map["k1"]["definition"] == OPEN_TEXT
    assert knowledge.pushed == ["<absent>", frozenset(ALL)]


def _retrieval_service(rows, probes):
    from app.services.retrieval_service import RetrievalService

    seen = []

    class _Graph:
        def node_context(self, notebook_id, object_id, **kwargs):
            seen.append(kwargs)
            row = rows[object_id]
            return {**row, "occurrences": [dict(item) for item in row["occurrences"]]}

    service = RetrievalService(candidates=None, graph=_Graph(), community_queries=None,
                               ceiling_verdict=_real_verdict(probes))
    return service, seen


def test_retrieval_service_uses_a_row_inside_the_ceiling_as_read():
    rows = {"ko-hub": _hub_row("ko-hub")}
    probes = _Probes()
    service, seen = _retrieval_service(rows, probes)
    with _all_ticked():
        row = service.node_context(ACTIVE, "ko-hub")
        assert current_source_scope()._ceiling_binds_memo == {ACTIVE: False}
    assert row == rows["ko-hub"] and seen == [{}]


def test_retrieval_service_flips_on_a_drifted_row_and_rereads_it_bound():
    rows = {
        "ko-merged": _row("ko-merged", [_occurrence(OPEN_SOURCE, OPEN_TEXT),
                                        _occurrence(NEW_SOURCE, NEW_TEXT)]),
        "ko-gone": _row("ko-gone", [_occurrence(NEW_SOURCE, NEW_TEXT)]),
        "ko-defined": _row(
            "ko-defined", [_occurrence(OPEN_SOURCE, OPEN_TEXT)],
            definition=DEF_TEXT, definition_basis="defines_evidence",
            definition_source_id=NEW_SOURCE),
        "ko-next": _hub_row("ko-next"),
    }
    bound = {"allowed_source_ids": frozenset(ALL)}
    for object_id in ("ko-merged", "ko-gone", "ko-defined"):
        probes = _Probes()
        service, seen = _retrieval_service(rows, probes)
        with _all_ticked():
            row = service.node_context(ACTIVE, object_id)
            assert current_source_scope()._ceiling_binds_memo == {ACTIVE: True}
            following = service.node_context(ACTIVE, "ko-next")
        assert NEW_TEXT not in str(row) and DEF_TEXT not in str(row)
        assert seen == [{}, bound, bound], object_id
        assert probes.calls == ["drifted", "foreign"]
        assert following["definition"] == "HUB fused description"
    assert row["definition"] is None and row["occurrences"][0]["source_id"] == OPEN_SOURCE


def test_retrieval_service_drops_a_drifted_row_with_nothing_inside():
    service, _seen = _retrieval_service(
        {"ko-gone": _row("ko-gone", [_occurrence(NEW_SOURCE, NEW_TEXT)])}, _Probes())
    with _all_ticked():
        assert service.node_context(ACTIVE, "ko-gone") == {}


def test_a_merge_after_the_verdict_is_caught_on_a_real_store(repo, monkeypatch):
    """The production verdict and SQLite store: the first re-read of the run
    takes the verdict (False: all ticked, nothing drifted); then a new source is
    uploaded and its evidence merged into the recalled hub.  The next re-read of
    the same run must not carry the new source's text or locator."""
    import datetime
    import json

    from tests.test_node_context import _nc_ev, _seed_hub

    nb, hub, src = _seed_hub(repo, 2)
    runtime = repo._runtime
    service = runtime.evidence_context_component
    pushed = []
    original = runtime.knowledge.node_context

    def spy(*args, **kwargs):
        pushed.append(kwargs.get("allowed_source_ids", "<absent>"))
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.knowledge, "node_context", spy)
    hit = RetrievedKnowledge(object_id=hub, object_type="concept", payload={"name": "Hub"},
                             evidence=[], notebook_id=nb, tier="personal", relevance=0.9)
    visible = runtime.source_store.all_visible_source_ids(nb)
    new_src, new_el = f"src-new-{nb}", f"el-new-{nb}"
    with source_scope_context(nb, {"mode": "include", "source_ids": visible,
                                   "narrowed": False, "owner_id": "u-asker"}, None):
        before = service.knowledge_context(nb, [hit], budget_chars=10_000)
        assert current_source_scope()._ceiling_binds_memo == {nb: False}
        now = datetime.datetime.now().isoformat()
        with repo._connect() as db:
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,file_name,file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) VALUES (?,?,'new','markdown','extracted','parsed','n.md','',0,'','','academic_paper',?,?)", (new_src, nb, now, now))
            db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) VALUES (?,?,'paragraph','p',?,'{}',?)", (new_el, new_src, NEW_TEXT, now))
            db.execute("UPDATE knowledge_objects SET evidence=? WHERE id=?",
                       (json.dumps([_nc_ev(new_el, new_src), _nc_ev(f"el-{nb}", src)]), hub))
            db.execute("INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) VALUES (?,?,?)", (hub, new_src, nb))
        after = service.knowledge_context(nb, [hit], budget_chars=10_000)
        assert current_source_scope()._ceiling_binds_memo == {nb: True}
    assert "HUB fused description" in before[0] and pushed[0] == "<absent>"
    assert NEW_TEXT not in after[0] and new_src not in json.dumps(after[1])
    assert pushed[1:] == ["<absent>", frozenset(visible)]
    assert after[1]["k1"]["source_id"] == src
