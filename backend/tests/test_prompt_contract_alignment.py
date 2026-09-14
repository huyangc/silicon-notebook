"""Prompt ↔ parser contract alignment (harness principle, 2026-09-14).

The prompt must be precise: the JSON example the model reads in the user
message is the same constant the transport advertises as the schema hint, and
every "optional / leave empty / omit" instruction names the exact spelling the
parser accepts. These pin the wording fixed after the 2026-09-14 audit
(items A3, A4, A6, A8, A9, A10, A12, B7) so it cannot drift back.
"""
from __future__ import annotations

import inspect
import json

from app.services import catalog_job, concept_merge_review, document_guide, paper_meta, prompts


def test_sufficiency_prompt_closes_with_the_schema_hint_and_names_every_verdict():
    prompt = prompts.report_sufficiency_prompt("Q", "sections block")
    assert prompt.endswith(f"Return JSON only: {prompts.REPORT_SUFFICIENCY_SCHEMA_HINT}")
    # Every value of the enum hint is taught in the prose, "充足(keep)" included.
    assert "充足(keep)" in prompt and "薄弱(supplement" in prompt and "缺失(external" in prompt
    assert "exactly one of 充足 / 薄弱 / 缺失" in prompt
    assert "exactly one of keep / supplement / external" in prompt


def test_section_prompt_says_one_string_value_per_facet():
    prompt = prompts.report_section_prompt("T", "S", "Q", "CTX")
    assert "exactly ONE value per facet as a string" in prompt


def test_reflect_outline_hint_spells_optional_parent_as_null():
    hint = prompts.reflect_schema_hint((), (), True, False, False, False)
    section = json.loads(hint)["outline"]["sections"][0]
    assert section["parent"] is None
    prose = prompts.reflect_prompt("q", "candidates", outline=True)
    assert "null or omitted for a top-level section" in prose


def test_expand_prompt_closes_with_the_advertised_schema_hint():
    for want_types in (False, True):
        prompt = prompts.expand_query_prompt("Q", want_types=want_types)
        assert prompt.endswith(f"Return JSON only: {prompts.EXPAND_SCHEMA_HINT}")
        assert "reason: an optional one-line note" in prompt
    assert "types and prefer are not used in this run" in prompts.expand_query_prompt("Q")
    assert "types: which KG node types" in prompts.expand_query_prompt("Q", want_types=True)


def test_document_guide_hint_has_empty_slots_and_the_instruction_embeds_it():
    hint = json.loads(document_guide.GUIDE_SCHEMA_HINT)
    document = hint["documents"][0]
    assert (document["purpose"], document["method"], document["contribution"]) == ("", "", "")
    assert hint["relationships"][0]["description"] == ""
    assert hint["reading_order"][0]["reason"] == ""
    source = inspect.getsource(document_guide.guide_style_instruction)
    assert "GUIDE_SCHEMA_HINT" in source
    assert "empty list [] (never null)" in source
    assert "never null, never" in source and "a placeholder sentence" in source


def test_paper_meta_prompt_says_omit_and_integer_year():
    prompt = paper_meta.paper_meta_prompt("head text")
    assert 'return exactly {"is_paper": false} and OMIT every other key' in prompt
    assert "year: the publication year as a bare integer" in prompt
    assert "omit the key otherwise" in prompt
    assert "Every string field is a plain string, never a list or object" in prompt


def test_concept_merge_prompt_says_confidence_is_a_number():
    prompt = concept_merge_review._prompt([
        {"id": "c1", "score": 0.9, "canonical_a": "MoE", "canonical_b": "Mixture-of-Experts"},
    ])
    assert "confidence is a number between 0 and 1" in prompt


def test_catalog_prompt_spells_scalar_and_container_types():
    source = inspect.getsource(catalog_job._prompt)
    assert "write it as a JSON boolean (true/false), never as a quoted string" in source
    assert '`examples` is always an array of strings' in source
    assert 'is absent write "" (never null)' in source


def test_intent_prompt_tells_the_model_its_scope_decides_and_how_confidence_is_read():
    prompt = prompts.query_intent_prompt("Q")
    assert "Your result_scope decides the retrieval executor" in prompt
    assert "confidence below 0.5 is treated as a guess" in prompt
