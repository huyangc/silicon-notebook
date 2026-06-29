"""Focused tests: _prompt() must include the CONNECTIVITY REQUIRED block."""
import pytest
from app.services.kg.extract import _prompt


def _get_prompt(base_filter: bool = False) -> str:
    return _prompt(
        labeled_text="[0] Kirchhoff's current law states that the sum of currents at a node is zero.",
        section_path="2.1 Circuit Laws",
        doc_type="textbook",
        base_filter=base_filter,
    )


def test_connectivity_header_present():
    p = _get_prompt()
    assert "CONNECTIVITY" in p, "Prompt must contain the CONNECTIVITY section header"


def test_connectivity_required_label():
    p = _get_prompt()
    assert "REQUIRED" in p, "Prompt must label connectivity as REQUIRED"


def test_claim_must_have_edge():
    p = _get_prompt()
    assert "every Claim MUST appear in at least one edge" in p


def test_about_edge_fallback():
    p = _get_prompt()
    assert "`about`" in p or "about" in p, "Prompt must mention `about` as fallback edge"


def test_no_disconnected_node_instruction():
    p = _get_prompt()
    assert "Do NOT emit a node you cannot connect" in p


def test_concept_should_have_edge():
    p = _get_prompt()
    # phrase wraps across a line; test on the stable prefix
    assert "every Concept SHOULD appear in at least" in p


def test_connectivity_block_present_with_base_filter():
    """CONNECTIVITY block must also appear when base_filter=True."""
    p = _get_prompt(base_filter=True)
    assert "CONNECTIVITY" in p
    assert "every Claim MUST appear in at least one edge" in p


def test_existing_edges_section_intact():
    """Regression: the EDGES block must still be present after the edit."""
    p = _get_prompt()
    assert "EDGES" in p
    assert "supports" in p
    assert "derived_from" in p
    assert "about" in p


def test_base_rule_placeholder_intact():
    """base_filter=True injects a BASE-TIER QUALITY FILTER line; verify it still works."""
    p_base = _get_prompt(base_filter=True)
    assert "BASE-TIER QUALITY FILTER" in p_base
    p_no_base = _get_prompt(base_filter=False)
    assert "BASE-TIER QUALITY FILTER" not in p_no_base
