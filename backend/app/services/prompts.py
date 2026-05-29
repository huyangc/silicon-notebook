"""Prompt templates and JSON schema hints for silicon-notebook LLM tasks.

Kept separate from business logic so prompts can be versioned and tuned
without touching the repository/extraction code.
"""

from __future__ import annotations

EXTRACTION_SCHEMA_HINT = (
    '{"rules":[{"title":"","statement":"","applies_to":[""],'
    '"recommendation":"","risk_if_ignored":"","severity":"high|medium|low",'
    '"quoted_span":"verbatim text copied from a source element"}],'
    '"methods":[{"name":"","use_when":"","benefit":"","limitation":"",'
    '"quoted_span":""}],'
    '"risks":[{"title":"","description":"","severity":"high|medium|low",'
    '"quoted_span":""}],'
    '"cases":[{"symptom":"","context":"","root_cause":"","resolution":"",'
    '"lesson_learned":"","quoted_span":""}],'
    '"checklist":[{"question":"","severity":"high|medium|low",'
    '"required_evidence":"","quoted_span":""}],'
    '"glossary":[{"term":"","definition":"","quoted_span":""}]}'
)


def extraction_prompt(source_title: str, elements_block: str) -> str:
    return (
        "You extract structured engineering knowhow from a semiconductor "
        "document for a knowhow notebook. The document may mix Chinese and "
        "English. From the source elements below, extract design rules, "
        "methods/best-practices, risks, historical cases, checklist items, and "
        "glossary terms.\n\n"
        "Rules:\n"
        "- Only extract items that are clearly supported by the text.\n"
        "- For every item, copy a short verbatim `quoted_span` (10-200 chars) "
        "from one source element so the claim can be traced to evidence.\n"
        "- Do not invent facts. If a category has nothing, return an empty list.\n"
        "- Return valid JSON only, matching the schema hint.\n\n"
        f"Source title: {source_title}\n\n"
        f"Source elements:\n{elements_block}"
    )


ANSWER_SCHEMA_HINT = (
    '{"conclusion":"","applicable_scenario":[""],"recommended_methods":[""],'
    '"potential_risks":[""],"checklist":[""],"missing_information":[""]}'
)


def answer_prompt(question: str, scenario_block: str, context_block: str) -> str:
    return (
        "You are the answer engine for a semiconductor knowhow notebook. "
        "Answer the engineer's question using ONLY the retrieved notebook "
        "knowledge below. Be concrete and engineering-oriented.\n\n"
        "Output JSON with: conclusion (2-4 sentences), applicable_scenario "
        "(short tags), recommended_methods, potential_risks, checklist "
        "(actionable check questions), missing_information (what context is "
        "missing or what the notebook does not yet cover).\n"
        "If the retrieved knowledge is insufficient, say so in conclusion and "
        "list the gaps in missing_information. Return valid JSON only.\n\n"
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
