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


ANSWER_SCHEMA_HINT = '{"answer":"","grounded":true}'


def answer_prompt(question: str, context_block: str, history_block: str = "") -> str:
    history_section = (
        "Prior conversation (for context; the current question may refer to it):\n"
        f"{history_block}\n\n"
        if history_block
        else ""
    )
    return (
        "You answer an engineer's question using the notebook knowledge below, "
        "and you may reason beyond it.\n"
        "Rules:\n"
        "1. When a sentence uses a knowledge item, append its id marker like [k1] "
        "(multiple allowed: [k1][k3]) at the end of that sentence.\n"
        "2. When a sentence is your own inference (not supported by the items), do "
        "NOT add any [k] marker, and make clear it is your reasoning (e.g. prefix "
        "with '（推断）' / 'Likely,'). Never attach a marker to an unsupported claim.\n"
        "3. If the items don't cover the question, still answer from general "
        "knowledge and set grounded=false; otherwise grounded=true.\n"
        "4. Answer in the question's language. Be concrete.\n\n"
        f"{history_section}"
        f"Question: {question}\n\n"
        f"Knowledge items (id: [type] name — context):\n{context_block}\n\n"
        'Return JSON only: {"answer":"<text with [k] markers>","grounded":true|false}'
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
