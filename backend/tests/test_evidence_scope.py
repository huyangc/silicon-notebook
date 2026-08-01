from __future__ import annotations

import sqlite3

import pytest

from app.models.common import Evidence
from app.services.evidence_scope import (
    EvidenceScopeResolutionError,
    ResolvedEvidenceScope,
    SourceIdentity,
    SourceIdentitySnapshot,
    resolve_evidence_scope,
    restrict_evidence_candidates,
    source_identity_from_row,
)
from app.services.retrieval import RetrievedChunk, RetrievedElement, RetrievedKnowledge


def _identity(source_id: str, title: str, file_name: str = "") -> SourceIdentity:
    return SourceIdentity(source_id, "nb", title, file_name)


def _evidence(source_id: str) -> Evidence:
    return Evidence(
        source_id=source_id,
        source_title=source_id,
        element_id=f"e-{source_id}",
        element_type="text",
        location_label="p1",
        quoted_span=source_id,
        confidence=1.0,
    )


def test_omitted_refs_are_the_only_all_source_representation():
    scope = resolve_evidence_scope(None, [])
    assert scope == ResolvedEvidenceScope.all()
    with pytest.raises(ValueError):
        resolve_evidence_scope([], [])
    with pytest.raises(ValueError):
        ResolvedEvidenceScope.selected([])


def test_exact_id_display_title_and_file_name_resolution_is_normalized():
    sources = [
        _identity("s1", "Innovus User Guide", "innovus.pdf"),
        _identity("s2", "ICC2 Command Reference", "icc2.pdf"),
    ]
    by_title = resolve_evidence_scope(["  ＩＮＮＯＶＵＳ USER GUIDE "], sources)
    by_file = resolve_evidence_scope(["ICC2.PDF"], sources)
    by_id = resolve_evidence_scope(["s1"], sources)
    assert by_title.allowed_source_ids == frozenset({"s1"})
    assert by_file.allowed_source_ids == frozenset({"s2"})
    assert by_id.allowed_source_ids == frozenset({"s1"})


def test_missing_ambiguous_and_truncated_catalogs_fail_closed():
    sources = [_identity("s1", "Manual"), _identity("s2", "Manual")]
    with pytest.raises(EvidenceScopeResolutionError) as missing:
        resolve_evidence_scope(["Other"], sources)
    assert missing.value.status == "not_found"
    with pytest.raises(EvidenceScopeResolutionError) as ambiguous:
        resolve_evidence_scope(["Manual"], sources)
    assert ambiguous.value.status == "ambiguous"
    assert {item.source_id for item in ambiguous.value.candidates} == {"s1", "s2"}
    with pytest.raises(EvidenceScopeResolutionError) as truncated:
        resolve_evidence_scope(["s1"], sources, catalog_truncated=True)
    assert truncated.value.status == "unavailable"


def test_selected_scope_deduplicates_and_never_broadens():
    scope = resolve_evidence_scope(
        ["s1", "Manual A", "s2"],
        [_identity("s1", "Manual A"), _identity("s2", "Manual B")],
    )
    assert [item.source_id for item in scope.sources] == ["s1", "s2"]
    assert scope.narrow_to(["s2"]).allowed_source_ids == frozenset({"s2"})
    with pytest.raises(ValueError):
        scope.narrow_to(["s3"])
    with pytest.raises(ValueError):
        scope.narrow_to([])


def test_display_identity_uses_the_canonical_paper_title_rule():
    identity = source_identity_from_row({
        "id": "s1",
        "notebook_id": "nb",
        "title": "Upload title",
        "file_name": "paper.pdf",
        "is_paper": True,
        "paper_title": "Parsed paper title",
    })
    assert identity.title == "Parsed paper title"
    assert identity.source_file_name == "paper.pdf"


def test_display_identity_accepts_the_sqlite_source_row_shape():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE source_identity (id TEXT, notebook_id TEXT, title TEXT, "
        "file_name TEXT, is_paper INTEGER, paper_title TEXT)"
    )
    db.execute(
        "INSERT INTO source_identity VALUES (?, ?, ?, ?, ?, ?)",
        ("s1", "nb", "Upload", "paper.pdf", 1, "Paper"),
    )
    row = db.execute("SELECT * FROM source_identity").fetchone()
    assert source_identity_from_row(row) == SourceIdentity(
        "s1", "nb", "Paper", "paper.pdf"
    )


def test_selected_scope_rejects_empty_identifiers_and_guard_type_mismatch():
    with pytest.raises(ValueError):
        SourceIdentitySnapshot("", "nb", "Missing id")
    with pytest.raises(ValueError):
        SourceIdentitySnapshot("s", "", "Missing notebook")
    scope = ResolvedEvidenceScope.selected([
        SourceIdentitySnapshot("s1", "nb", "A")
    ])
    with pytest.raises(ValueError):
        restrict_evidence_candidates(
            scope, "chunk", [RetrievedKnowledge("k", "claim", {})]
        )


def test_candidate_guard_removes_third_manual_and_trims_merged_evidence():
    scope = ResolvedEvidenceScope.selected([
        SourceIdentitySnapshot("s1", "nb", "A"),
        SourceIdentitySnapshot("s2", "nb", "B"),
    ])
    elements = [
        RetrievedElement("e1", "s1", "A", "p1", "text", "a"),
        RetrievedElement("e3", "s3", "C", "p1", "text", "c"),
    ]
    chunks = [
        RetrievedChunk("c1", "s2", "B", "cmd", "b"),
        RetrievedChunk("c3", "s3", "C", "cmd", "c"),
    ]
    merged = RetrievedKnowledge(
        object_id="k1",
        object_type="claim",
        payload={"name": "shared"},
        evidence=[_evidence("s1"), _evidence("s3")],
    )
    third_only = RetrievedKnowledge(
        object_id="k3",
        object_type="claim",
        payload={"name": "third"},
        evidence=[_evidence("s3")],
    )

    assert [item.element_id for item in restrict_evidence_candidates(
        scope, "element", elements
    )] == ["e1"]
    assert [item.chunk_id for item in restrict_evidence_candidates(
        scope, "chunk", chunks
    )] == ["c1"]
    knowledge = restrict_evidence_candidates(
        scope, "knowledge", [merged, third_only]
    )
    assert [item.object_id for item in knowledge] == ["k1"]
    assert [ev.source_id for ev in knowledge[0].evidence] == ["s1"]
    assert [ev.source_id for ev in merged.evidence] == ["s1", "s3"]


def test_all_source_guard_preserves_candidate_objects():
    item = RetrievedChunk("c", "s", "S", "section", "text")
    result = restrict_evidence_candidates(ResolvedEvidenceScope.all(), "chunk", [item])
    assert result == [item]
    assert result[0] is item
    with pytest.raises(ValueError):
        restrict_evidence_candidates(
            ResolvedEvidenceScope.all(),
            "chunk",
            [RetrievedKnowledge("k", "claim", {})],
        )
