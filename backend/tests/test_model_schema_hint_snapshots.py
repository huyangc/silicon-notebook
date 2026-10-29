"""Projection snapshot of every model JSON hint in the backend.

A hint is a JSON example; ``response_format_for_hint`` turns it into the
strict wire schema a capable service enforces. The projection rules are keyed
on field NAMES (``_optional_root_fields``, ``_STRING_LIST_FIELDS``,
``_NULL_STRING_FIELDS``, ``_DYNAMIC_FIELDS``) and therefore reach every hint
that happens to use one of those names, including a hint written long after
the rule. This file pins, per hint, whether it projects at all, the schema's
content digest, and which root fields a reply can never leave null, so any
change to a hint or to a rule shows up here as an explicit diff that has to be
read against the hint's prompt prose and its consumer.

Literal hints are discovered from the source tree; a new or edited literal
that is not in ``_LITERAL_HINTS`` fails the coverage test. Hints assembled at
import or call time (f-strings, ``json.dumps``, the gated reflect schema) are
listed in ``_BUILT_HINTS``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.core.model_schema import response_format_for_hint
from app.services import prompts
from app.services.collection_catalog import (
    ENUMERABLE_ELEMENT_KINDS,
    ENUMERABLE_KG_OBJECT_TYPES,
)
from app.services.kg.extract import _KG_SCHEMA_HINT
from app.services.notebook_question_suggestions import _SCHEMA as _QUESTION_SUGGESTION_HINT

_APP = Path(__file__).resolve().parents[1] / "app"

# (path under app/, literal hint, schema name or None for JSON-mode fallback,
#  root fields that may never be null)
_LITERAL_HINTS: tuple[tuple[str, str, str | None, tuple[str, ...]], ...] = (
    ('domain/kg_attrs.py', '{"variables":[{"symbol":"","meaning":""}]}',
     'model_response_bbd49601311b6a41', ('variables',)),
    ('eval/find_sources_calibrate.py', '{"label":"strong|related|unrelated","reason":""}',
     'model_response_2b3dd00f0b7dd0c1', ('label', 'reason')),
    ('eval/kg_attrs_calibration.py', '{"pairs":[{"symbol":"","meaning":""}]}',
     'model_response_1df33dae7a7cca2e', ('pairs',)),
    ('eval/kg_attrs_calibration.py', '{"verdict":"correct|incorrect|unsure","reason":""}',
     'model_response_1439e5d05dcef9b6', ('reason', 'verdict')),
    ('eval/kg_attrs_calibration.py', '{"judgements":[{"i":0,"verdict":"correct|incorrect|unsure","reason":""}]}',
     'model_response_3d4701058a7495f5', ('judgements',)),
    ('eval/inference.py', '{"correctness":0,"inference_quality":0,"grounding_consistency":true,"fabricated_citation":false,"reason":""}',
     'model_response_6edbc3ff91a9fa41', ('correctness', 'fabricated_citation', 'grounding_consistency', 'inference_quality', 'reason')),
    ('scripts/gen_recall_gold.py', '{"question":""}',
     'model_response_9e9526a0ad4c423f', ('question',)),
    ('services/ask_service.py', '{"source_queries": {"<source_id>": ["<query>"]}}', None, ()),
    ('services/ask_service.py', '{"text": "", "conflicts": [{"kb_says": "", "external_says": "", "note": ""}]}',
     'model_response_bb0b5c91b9a41780', ('conflicts', 'text')),
    ('services/catalog_job.py', '{"entries": [{"command_name": "", "syntax": "", "description": "", "args": [{"name": "", "required": false, "desc": "", "default": ""}], "examples": [""]}]}',
     'model_response_19973db098da9bb3', ('entries',)),
    ('services/chunk_question_index.py', '{"questions":[""]}',
     'model_response_ea85082958c85aad', ('questions',)),
    ('services/concept_merge_review.py', '{"decisions":[{"candidate_id":"","decision":"merge|keep_separate|unsure","confidence":0.0,"rationale":""}]}',
     'model_response_ef7780dc38684978', ('decisions',)),
    ('services/document_guide.py', '{"documents":[{"reference":"k5001","purpose":"","method":"","contribution":""}],"relationships":[{"description":"","references":["k5001","k5002"]}],"reading_order":[{"reference":"k5001","reason":""}]}',
     'model_response_0518911fcc9a93a3', ('documents', 'reading_order', 'relationships')),
    ('services/graph_retrieval.py', '{"relevant_ids": [""]}',
     'model_response_801678e3ad61661b', ('relevant_ids',)),
    ('services/kg/conflict_review.py', '{"conflict_type":"none|mutual|temporal|granularity","resolution":"keep|discard|modify","winner_ref":"","resolved_name":"","confidence":0.0,"rationale":""}',
     'model_response_b0611db6db7e6147', ('confidence', 'conflict_type', 'rationale', 'resolution', 'resolved_name', 'winner_ref')),
    ('services/kg/graph_reason.py', '{"valid": true, "reason": ""}',
     'model_response_c9227363418950c1', ('reason', 'valid')),
    ('services/kg/relation_completion.py', '{"relations":[{"candidate_id":"","source_object_id":"","target_object_id":"","edge_type":"","evidence_element_ids":[""],"confidence":0.0}]}',
     'model_response_39b4b468987199df', ('relations',)),
    ('services/kg/relation_completion.py', '{"verdicts":[{"candidate_id":"","valid":true,"reason":""}]}',
     'model_response_015c8c2e46341162', ('verdicts',)),
    ('services/kg/run_control.py', '{"ok":true}',
     'model_response_4528a3c79c4aaee2', ('ok',)),
    ('services/knowhow/api.py', '{"suggestion_md": ""}',
     'model_response_a77da1b7956f00f3', ('suggestion_md',)),
    ('services/knowhow/api.py', '{"suggestions":[{"column_id":"","suggestion_md":"","confidence":"high|medium|low","based_on_row_ids":[],"evidence_keys":[],"basis":"","abstain_reason":""}]}',
     'model_response_aa91bd17b7dff97e', ('suggestions',)),
    ('services/knowhow/api.py', '{"reformatted_md": ""}',
     'model_response_36062ed0ec3507e0', ('reformatted_md',)),
    ('services/model_provider.py', '{"ok":true}',
     'model_response_4528a3c79c4aaee2', ('ok',)),
    ('services/paper_cards.py', '{"tldr":{"text":"","anchors":["e1"]},"problem":{"text":"","anchors":["e1"]},"method":{"text":"","anchors":["e1"]},"contributions":[{"text":"","anchors":["e1"]}],"metrics":[{"name":"","value":"","unit":"","setting":"","compared_to":"","anchor":"e1"}],"datasets":[{"name":"","anchors":["e1"]}]}',
     'model_response_bce52f4a4bdd0287', ('contributions', 'datasets', 'method', 'metrics', 'problem', 'tldr')),
    ('services/paper_extraction.py', '{"values":[{"field_index":0,"value":"","anchor":"c1"}]}',
     'model_response_32bc10e27cbde210', ('values',)),
    ('services/paper_meta.py', '{"is_paper":true,"title":"","authors":[{"name":"","affiliations":[""]}],"venue":"","year":2024,"doi":"","keywords":[""]}',
     'model_response_6ed0a223d30c606d', ('is_paper',)),
    ('services/plugin_ask_engine.py', '{"text":""}',
     'model_response_54669b5e6a1bfebb', ('text',)),
    ('services/prompts.py', '{"description":""}',
     'model_response_26b3b20fbc896674', ('description',)),
    ('services/prompts.py', '{"title":"","content_md":"","tags":[""]}',
     'model_response_9c1f9973000db2e2', ('content_md', 'tags', 'title')),
    ('services/prompts.py', '{"name":"","description":""}',
     'model_response_a5f05c85ae74371b', ('description', 'name')),
    ('services/prompts.py', '{"drop":[0]}',
     'model_response_2eaaa3d3ff03ea81', ('drop',)),
    ('services/prompts.py', '{"new_types":[{"object_type":"snake_case_id","plural":"","label":"","primary":"","fields":[""],"description":"","rationale":""}]}',
     'model_response_613c70553719f224', ('new_types',)),
    ('services/prompts.py', '{"query":""}',
     'model_response_10798ac8d2df2a31', ('query',)),
    ('services/prompts.py', '{"answer":"","grounded":true}',
     'model_response_d8dc5e6ec5f1d297', ('answer', 'grounded')),
    ('services/prompts.py', '{"relevant":[""]}',
     'model_response_c2349be026e7d4dd', ('relevant',)),
    ('services/prompts.py', '{"query":"","high_level_keywords":[],"low_level_keywords":[],"sub_queries":[{"query":"","types":[],"prefer":"keyword|semantic|balanced","reason":""}],"comparison":{"focal":""}}',
     'model_response_aaf9440771b350ae', ('query', 'sub_queries')),
    ('services/prompts.py', '{"sections":[{"title":"","scope":"","sub_queries":[""]}]}',
     'model_response_e5e18fcf3d7a7ef9', ('sections',)),
    ('services/prompts.py', '{"normalized_question":"","intent_type":"explain|compare|diagnose|design|review|other","result_scope":"ranked|complete|aggregate|hybrid","completeness_required":false,"entities":[""],"mandatory_topics":[{"title":"","question":"","retrieval_queries":[""]}],"comparison_axes":[""],"constraints":[""],"excluded_topics":[""],"expected_output":"","assumptions":[""],"ambiguities":[{"question":"","reason":"","required":true,"options":[""]}],"confidence":0.0,"needs_clarification":false}',
     'model_response_5d29a2edb5590072', ('ambiguities', 'assumptions', 'comparison_axes', 'completeness_required', 'confidence', 'constraints', 'entities', 'excluded_topics', 'expected_output', 'intent_type', 'mandatory_topics', 'needs_clarification', 'normalized_question', 'result_scope')),
    # QUERY_INTENT_KEYWORDS_SCHEMA_HINT: the intent contract plus expand_query's
    # keyword pair. Both keys project as required string lists (``[]`` is an
    # admitted answer: no lexical arm); the parser reads an absent/off-type
    # list as empty (query_intent._intent_keyword_list).
    ('services/prompts.py', '{"normalized_question":"","intent_type":"explain|compare|diagnose|design|review|other","result_scope":"ranked|complete|aggregate|hybrid","completeness_required":false,"entities":[""],"mandatory_topics":[{"title":"","question":"","retrieval_queries":[""]}],"high_level_keywords":[],"low_level_keywords":[],"comparison_axes":[""],"constraints":[""],"excluded_topics":[""],"expected_output":"","assumptions":[""],"ambiguities":[{"question":"","reason":"","required":true,"options":[""]}],"confidence":0.0,"needs_clarification":false}',
     'model_response_ee3b187fa9481c29', ('ambiguities', 'assumptions', 'comparison_axes', 'completeness_required', 'confidence', 'constraints', 'entities', 'excluded_topics', 'expected_output', 'high_level_keywords', 'intent_type', 'low_level_keywords', 'mandatory_topics', 'needs_clarification', 'normalized_question', 'result_scope')),
    ('services/prompts.py', '{"markdown":"","grounded":true,"claims":[{"claim_id":"","statement":"","type":"fact|comparison|trend|inference|general","entities":[""],"evidence_keys":["k1"],"conditions":[""],"same_paper_baseline":false,"confidence":0.0,"frame_assignments":{"facet-id":"value"}}]}', None, ()),
    ('services/prompts.py', '{"summary":"","coverage":[{"intent_id":"","covered":true,"note":""}],"contradictions":[""]}',
     'model_response_a28a1d22d448d4a9', ('contradictions', 'coverage', 'summary')),
    ('services/prompts.py', '{"central_answer":"","shared_definitions":[{"term":"","definition":"","evidence_keys":[""]}],"claims":[{"id":"c1","statement":"","type":"fact|comparison|trend|inference|general","facet_id":"","evidence_keys":[""],"counterevidence_keys":[""],"conditions":[""],"owner_section_id":"section-1"}],"sections":[{"section_id":"section-1","thesis":"","must_contrast":[""],"handoff":"","do_not_repeat":[""]}]}',
     'model_response_c2dcc875da3d35a0', ('central_answer', 'claims', 'sections', 'shared_definitions')),
    ('services/prompts.py', '{"sections":[{"title":"","scope":"","sub_queries":[""],"intent_ids":[""],"perspectives":[""],"tensions":[""]}],"frame":{"subject_kind":"","facets":[{"id":"","name":"","values":[""],"exclusive":true}],"axes":[{"id":"","name":"","condition_fields":[""]}],"instance_policy":""}}',
     'model_response_1e0a644772ca6023', ('sections',)),
    ('services/prompts.py', '{"verdicts":[{"index":1,"sufficiency":"充足|薄弱|缺失","gap_note":""}]}',
     'model_response_8172fb5e09ddd404', ('verdicts',)),
    ('services/prompts.py', '{"blocks":[{"label":"corpus_shape","value":"","evidence":["source_id"],"retire":false},{"label":"corpus_gaps","value":"","retire":true}]}',
     'model_response_c99f0058476374ed', ('blocks',)),
    ('services/prompts.py', '{"blocks":[{"label":"retrieval_notes","value":"","retire":false},{"label":"usage_gaps","value":"","retire":true}]}',
     'model_response_15809580e3191fe9', ('blocks',)),
    ('services/prompts.py', '{"entries":[{"op":"ADD|UPDATE","situation":"s0","action":"exact_lookup","polarity":"good|bad","rationale":""}]}',
     'model_response_59266bcf3a6b4911', ('entries',)),
    ('services/repository_facade.py', '{"relevant_ids": [""]}',
     'model_response_801678e3ad61661b', ('relevant_ids',)),
    ('services/source_ingestion.py', '{"summary": ""}',
     'model_response_35de5b90eb8853f6', ('summary',)),
    ('services/spreadsheet_analysis.py', '{"targets":[{"source_id":"id","sheet":"name","operation":"profile","aggregation":"count","measure":"","group_by":"","sort_by":"","order":"desc","filters":[],"columns":[]}]}',
     'model_response_b7bfb7e6fff1ba84', ('targets',)),
)

_BUILT_HINTS: tuple[tuple[str, str, str | None, tuple[str, ...]], ...] = (
    ("kg.extract._KG_SCHEMA_HINT", _KG_SCHEMA_HINT,
     "model_response_3c7f45efb29c0b23", ("edges", "nodes")),
    ("notebook_question_suggestions._SCHEMA", _QUESTION_SUGGESTION_HINT,
     "model_response_6e0e9726ac796601", ("questions",)),
    ("prompts.REFLECT_SCHEMA_HINT", prompts.REFLECT_SCHEMA_HINT,
     "model_response_b168d194e5366cc8", ("next_action", "reason", "sufficient")),
    ("reflect_schema_hint(every gate open)", prompts.reflect_schema_hint(
        ENUMERABLE_ELEMENT_KINDS, ENUMERABLE_KG_OBJECT_TYPES, outline=True,
        consult_memory=True, search_chunks=True, read_document=True,
        keyword_search=True, section_nav=True, find_sources=True,
        paper_cards=True, extract=True,
    ), "model_response_be5bfb96fe1dfecd", ("next_action", "reason", "sufficient")),
    ("reflect_schema_hint(kg_actions=False)", prompts.reflect_schema_hint(kg_actions=False),
     "model_response_17f33a17ec8fd780", ("next_action", "reason", "sufficient")),
)


def _literal_hints_in_source() -> set[tuple[str, str]]:
    found = set()
    for path in sorted(_APP.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            try:
                value = json.loads(node.value)
            except ValueError:
                continue
            if isinstance(value, dict) and value:
                found.add((path.relative_to(_APP).as_posix(), node.value))
    return found


def test_every_literal_hint_in_the_source_is_snapshotted():
    pinned = {(path, hint) for path, hint, _name, _required in _LITERAL_HINTS}
    found = _literal_hints_in_source()
    assert found - pinned == set(), "new or edited hint: add its projection snapshot"
    assert pinned - found == set(), "snapshotted hint no longer in the source"


def _never_null_root_fields(schema: dict) -> tuple[str, ...]:
    def nullable(field: dict) -> bool:
        types = field.get("type")
        types = types if isinstance(types, list) else [types]
        return "null" in types or {"type": "null"} in field.get("anyOf", [])

    return tuple(sorted(
        name for name, field in schema["properties"].items() if not nullable(field)
    ))


@pytest.mark.parametrize(
    ("label", "hint", "name", "required"),
    [*_LITERAL_HINTS, *_BUILT_HINTS],
    ids=[f"{row[0]}#{index}" for index, row in enumerate([*_LITERAL_HINTS, *_BUILT_HINTS])],
)
def test_hint_projection_snapshot(label, hint, name, required):
    response_format = response_format_for_hint(hint)
    if name is None:
        assert response_format is None, label
        return
    assert response_format is not None, label
    assert response_format["json_schema"]["name"] == name, label
    assert _never_null_root_fields(response_format["json_schema"]["schema"]) == required, label
