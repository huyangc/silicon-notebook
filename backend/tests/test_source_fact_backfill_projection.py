from app.repositories.source_fact_backfill import project_historical_source_fact


def _row(**overrides):
    row = {
        "id": "ko-1",
        "source_id": "src-a",
        "object_type": "concept",
        "payload": '{"name":"A"}',
        "evidence": '[{"source_id":"src-a","element_id":"el-1"}]',
    }
    row.update(overrides)
    return row


def test_historical_projection_requires_single_source_grounding():
    fact, reason = project_historical_source_fact(_row(), "src-a", "run-1")
    assert reason == ""
    assert fact is not None
    assert fact.local_object_id == "legacy:ko-1"
    assert fact.element_ids == ("el-1",)

    for row, expected in [
        (_row(source_id="src-b"), "ambiguous_owner"),
        (
            _row(
                evidence=[
                    {"source_id": "src-a", "element_id": "el-1"},
                    {"source_id": "src-b", "element_id": "el-2"},
                ]
            ),
            "mixed_source_evidence",
        ),
        (_row(evidence=[]), "missing_evidence"),
        (_row(evidence=[{"source_id": "src-a"}]), "missing_element"),
    ]:
        fact, reason = project_historical_source_fact(row, "src-a", "run-1")
        assert fact is None
        assert reason == expected


def test_historical_projection_id_is_generation_stable():
    first, _ = project_historical_source_fact(_row(), "src-a", "run-1")
    retry, _ = project_historical_source_fact(_row(), "src-a", "run-1")
    next_generation, _ = project_historical_source_fact(_row(), "src-a", "run-2")
    assert first is not None and retry is not None and next_generation is not None
    assert first.fact_id == retry.fact_id
    assert first.fact_id != next_generation.fact_id
