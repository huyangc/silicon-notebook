"""Public context keeps evidence identity and never rehydrates on read."""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace

from app.services.public_share_evidence import (
    capture_share_evidence,
    overlay_share_evidence,
)


class Snapshots:
    def __init__(self):
        self.data = {}

    def share_evidence(self, token):
        return deepcopy(self.data.get(token, {}))

    def put_share_evidence(self, token, payload):
        for turn, refs in payload.items():
            target = self.data.setdefault(token, {}).setdefault(turn, {})
            for key, ref in refs.items():
                target.setdefault(key, deepcopy(ref))


def _repo():
    snapshots = Snapshots()
    repo = SimpleNamespace(
        settings=SimpleNamespace(public_share_evidence_max_chars=1200),
        _runtime=SimpleNamespace(answer_links=snapshots,
            chunk_store=SimpleNamespace(database=SimpleNamespace(connect=lambda: nullcontext(None)),
                chunks_for_element_ids=lambda db, notebook, ids: [], rows_by_ids=lambda db, ids: []),
            memory_store=SimpleNamespace(memory_sources_for_source_ids=lambda ids, owner: {})),
        source_notebook_id=lambda sid, viewer_id: "notebook" if viewer_id == "author" else None,
        evidence_elements=lambda ids: {"el": {"source_id": "source", "text": "before quotation after", "location_label": "page 2"}},
    )
    return repo


def _row(identity="answer-a", quote="quotation"):
    return {"created_by": "author", "turns": [{"answer_id": identity, "payload": {
        "answer": "claim [k1]", "anchors": [{"key": "k1", "source_id": "source",
        "element_id": "el", "snippet": quote, "source_title": "paper"}],
    }}]}


def _public():
    return {"turns": [{"references": [{"key": "k1"}]}]}


def test_legacy_read_never_hydrates_and_new_capture_is_immutable():
    repo = _repo()
    assert "evidence" not in overlay_share_evidence(repo, "legacy", _row(), _public())["turns"][0]["references"][0]
    capture_share_evidence(repo, "fresh", _row())
    repo.evidence_elements = lambda ids: (_ for _ in ()).throw(AssertionError("read must not hydrate"))
    capture_share_evidence(repo, "fresh", _row())
    evidence = overlay_share_evidence(repo, "fresh", _row(), _public())["turns"][0]["references"][0]["evidence"]
    assert evidence == {"kind": "original", "text": "before quotation after", "quote": "quotation", "location": "page 2", "truncated": False}


def test_same_reference_number_in_different_turns_keeps_its_own_evidence():
    repo = _repo()
    capture_share_evidence(repo, "link", _row())
    capture_share_evidence(repo, "link", _row("answer-b", "other quotation"))
    assert "evidence" not in overlay_share_evidence(repo, "link", _row("answer-b"), _public())["turns"][0]["references"][0]


def test_memory_uses_only_shared_excerpt_and_unreadable_source_is_not_hydrated():
    repo = _repo()
    repo._runtime.memory_store.memory_sources_for_source_ids = lambda ids, owner: {"source": "memory"}
    repo.evidence_elements = lambda ids: (_ for _ in ()).throw(AssertionError("memory source must not be hydrated"))
    capture_share_evidence(repo, "memory", _row())
    evidence = overlay_share_evidence(repo, "memory", _row(), _public())["turns"][0]["references"][0]["evidence"]
    assert evidence["kind"] == "memory"
    assert evidence["text"] == "quotation"


def test_context_truncation_is_disclosed_and_retains_quote():
    repo = _repo()
    repo.evidence_elements = lambda ids: {"el": {"source_id": "source", "text": "a" * 2000 + "quotation" + "b" * 2000}}
    capture_share_evidence(repo, "bounded", _row())
    evidence = overlay_share_evidence(repo, "bounded", _row(), _public())["turns"][0]["references"][0]["evidence"]
    assert evidence["truncated"] is True
    assert len(evidence["text"]) == 1200
    assert evidence["quote"] in evidence["text"]


def test_unavailable_context_is_frozen_even_if_source_later_recovers():
    repo = _repo()
    repo.evidence_elements = lambda ids: {}
    capture_share_evidence(repo, "missing", _row())
    repo.evidence_elements = lambda ids: (_ for _ in ()).throw(AssertionError("missing capture is frozen"))
    capture_share_evidence(repo, "missing", _row())
    assert "evidence" not in overlay_share_evidence(repo, "missing", _row(), _public())["turns"][0]["references"][0]
