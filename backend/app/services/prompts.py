"""Prompt templates and JSON schema hints for silicon-notebook LLM tasks.

Kept separate from business logic so prompts can be versioned and tuned
without touching the repository/extraction code.
"""

from __future__ import annotations


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
