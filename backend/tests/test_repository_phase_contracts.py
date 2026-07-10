from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "backend" / "tests" / "fixtures" / "repository_contract"

REQUIRED_PHASES = {
    "process_source",
    "store_kg",
    "delete_source",
    "parse_source",
    "update_knowledge",
    "merge_knowledge",
    "approve_promotion",
    "confirm_conflict",
    "set_edge_review",
    "copy_notebook",
    "streaming_ask",
    "migration_recovery_seed",
}
REQUIRED_ERROR_POLICIES = {
    "append_ask_trace",
    "report_corpus_map",
    "model_error_recording",
    "update_report_missing",
    "delete_report_missing",
    "source_chunk_build",
    "source_embedding",
    "source_extraction",
}


def _load(name: str) -> dict[str, object]:
    path = FIXTURES / name
    assert path.is_file(), f"missing repository phase fixture: {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_phase_contracts_list_every_required_operation():
    tx = _load("transaction_phases.json")
    err = _load("error_policies.json")

    assert set(tx) == REQUIRED_PHASES
    assert set(err) == REQUIRED_ERROR_POLICIES


def test_transaction_contracts_freeze_order_and_failure_boundaries():
    tx = _load("transaction_phases.json")

    for operation, contract in tx.items():
        assert set(contract) >= {
            "sequence",
            "commit_boundaries",
            "failure_boundary",
        }, operation
        assert contract["sequence"], operation
        assert contract["commit_boundaries"], operation
        assert contract["failure_boundary"], operation


def test_mutation_matrix_freezes_every_semantic_side_effect():
    mutation = _load("mutation_phases.json")

    assert set(mutation) == REQUIRED_PHASES
    for operation, contract in mutation.items():
        assert set(contract) == {
            "semantic_mutation",
            "cache_invalidation",
            "unified_dirty",
            "version_bump",
            "index_scheduling",
            "exemption",
        }, operation
        assert all(
            isinstance(contract[key], bool)
            for key in {
                "semantic_mutation",
                "cache_invalidation",
                "unified_dirty",
                "version_bump",
                "index_scheduling",
            }
        ), operation
        assert isinstance(contract["exemption"], str), operation


def test_error_contracts_distinguish_raise_record_and_best_effort_paths():
    policies = _load("error_policies.json")

    for operation, contract in policies.items():
        assert set(contract) >= {"policy", "observable_result"}, operation
        assert contract["policy"] in {
            "raise",
            "return_none",
            "record_and_continue",
            "best_effort",
        }, operation
        assert contract["observable_result"], operation
