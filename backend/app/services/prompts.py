"""Prompt templates and JSON schema hints for silicon-notebook LLM tasks.

Kept separate from business logic so prompts can be versioned and tuned
without touching the repository/extraction code.
"""

from __future__ import annotations

import json

from app.services.extraction_profiles import (
    ENUM_FIELDS,
    LIST_FIELDS,
    ExtractionProfile,
    get_profile,
)


def build_extraction_schema_hint(profile: ExtractionProfile, registry=None) -> str:
    """Render the JSON schema hint for exactly the profile's object types."""
    example: dict = {}
    for schema in profile.schemas(registry):
        item: dict = {}
        for field_name in schema.fields:
            if field_name in LIST_FIELDS or field_name in schema.list_fields:
                item[field_name] = [""]
            elif field_name in ENUM_FIELDS:
                item[field_name] = ENUM_FIELDS[field_name]
            else:
                item[field_name] = ""
        item["quoted_span"] = "verbatim text copied from a source element"
        example[schema.plural] = [item]
    return json.dumps(example, ensure_ascii=False)


# Backwards-compatible default (general profile) for callers/tests that do not
# pass an explicit profile.
EXTRACTION_SCHEMA_HINT = build_extraction_schema_hint(get_profile("general"))


def extraction_prompt(
    source_title: str,
    elements_block: str,
    profile: ExtractionProfile | None = None,
    registry=None,
) -> str:
    profile = profile or get_profile("general")
    type_lines = "\n".join(
        f"- {schema.plural}: {schema.description}"
        for schema in profile.schemas(registry)
    )
    return (
        "You extract structured engineering knowhow from a semiconductor "
        "document for a knowhow notebook. The document may mix Chinese and "
        f"English. This document looks like {profile.focus}.\n\n"
        "Extract ONLY these object types (return an empty list for any that "
        "have nothing):\n"
        f"{type_lines}\n\n"
        "Rules:\n"
        "- Only extract items that are clearly supported by the text; do not "
        "invent facts or rules the document does not actually state.\n"
        "- For every item, copy a short verbatim `quoted_span` (10-200 chars) "
        "from one source element so the claim can be traced to evidence.\n"
        "- Fill relation fields (related_rules / related_cases / "
        "related_methods / related_concepts) only when the text explicitly "
        "connects items; otherwise leave them empty.\n"
        "- Return valid JSON only, matching the schema hint.\n\n"
        f"Source title: {source_title}\n\n"
        f"Source elements:\n{elements_block}"
    )


DESCRIPTION_SCHEMA_HINT = '{"description":""}'


def notebook_description_prompt(sources_block: str) -> str:
    return (
        "Write a concise 1-2 sentence description (Chinese ok) of what this "
        "knowhow notebook covers, based on the sources its curator has added. "
        "Describe the subject matter and document types; do not invent scope "
        "beyond the sources. Return valid JSON only with a 'description' field.\n\n"
        f"Sources:\n{sources_block}"
    )


REFINE_SCHEMA_HINT = (
    '{"items":[{"index":0,"keep":true,"quoted_span":"","reason":""}]}'
)


def refine_prompt(source_title: str, records_block: str, elements_block: str) -> str:
    return (
        "You verify extracted knowledge items against their source document "
        "(self-refinement pass). For EACH numbered item decide:\n"
        "- keep=false if the item is NOT supported by the source text "
        "(hallucinated), is too vague to be useful, or merely restates a "
        "heading; otherwise keep=true.\n"
        "- If a more faithful VERBATIM span exists in the source for a kept "
        "item, return it in quoted_span (copied exactly from the source); "
        "otherwise leave quoted_span empty.\n"
        "- reason: a short justification.\n"
        "Return JSON only, one entry per input index.\n\n"
        f"Source title: {source_title}\n\n"
        f"Extracted items:\n{records_block}\n\n"
        f"Source elements (ground truth):\n{elements_block}"
    )


SCHEMA_INDUCTION_HINT = (
    '{"new_types":[{"object_type":"snake_case_id","plural":"","label":"",'
    '"primary":"","fields":[""],"description":"","rationale":""}]}'
)


def schema_induction_prompt(existing_types: list, sample_block: str) -> str:
    return (
        "You help curate the knowledge schema of a semiconductor knowhow "
        "notebook. Look at the document sample and the existing object types. "
        "Propose NEW typed object types that recur in this material but are NOT "
        "already covered, so the extractor can capture them.\n\n"
        "Rules:\n"
        "- Only propose a type if the material clearly contains several "
        "instances of it; do not invent speculative types.\n"
        "- object_type: short snake_case id; fields: 3-7 concise snake_case "
        "payload keys; primary: the main text field; rationale: one line on why "
        "it is needed and not covered by existing types.\n"
        "- Do NOT repeat any existing type. Return valid JSON only; empty "
        "new_types list if nothing new is warranted.\n\n"
        f"Existing object types: {', '.join(existing_types)}\n\n"
        f"Document sample:\n{sample_block}"
    )


ANSWER_SCHEMA_HINT = '{"conclusion":""}'


def answer_prompt(question: str, scenario_block: str, context_block: str) -> str:
    return (
        "You are the answer engine for a semiconductor knowhow notebook. "
        "Answer the engineer's question using ONLY the retrieved notebook "
        "knowledge below. Be concrete and engineering-oriented.\n\n"
        "Write a grounded conclusion (2-4 sentences) that directly answers "
        "the question, citing evidence from the retrieved knowledge. "
        "If the retrieved knowledge is insufficient, state that clearly in "
        "the conclusion. Return valid JSON only with a single 'conclusion' "
        "string field.\n\n"
        f"Question: {question}\n\n"
        f"Scenario: {scenario_block}\n\n"
        f"Retrieved notebook knowledge:\n{context_block}"
    )


ARTICLE_SCHEMA_HINT = (
    '{"core_contribution":"","claims":[{"statement":"","claim_type":'
    '"mechanism|result|recommendation|warning|comparison","quoted_span":""}],'
    '"limitations":[""],"validation_plan":[""],'
    '"derived_rule_candidates":[{"proposed_rule":"","rationale":"",'
    '"quoted_span":""}]}'
)


def article_prompt(title: str, elements_block: str, rules_block: str) -> str:
    return (
        "You analyze a technical article for a semiconductor knowhow notebook "
        "and relate it to the notebook's existing rules. The article may mix "
        "Chinese and English.\n\n"
        "Produce: core_contribution, key claims (each with a verbatim "
        "`quoted_span` from the article), limitations, a validation_plan, and "
        "derived_rule_candidates (proposed new rules with rationale and a "
        "supporting quoted_span). Do not invent facts; only use the article "
        "text. Return valid JSON only.\n\n"
        f"Article title: {title}\n\n"
        f"Article elements:\n{elements_block}\n\n"
        f"Existing notebook rules (for relationship analysis):\n{rules_block}"
    )
