"""Prompt templates and JSON schema hints for silicon-notebook LLM tasks.

Kept separate from business logic so prompts can be versioned and tuned
without touching the repository/extraction code.
"""

from __future__ import annotations


DESCRIPTION_SCHEMA_HINT = '{"description":""}'

CONCEPT_DESC_SCHEMA_HINT = '{"description":""}'


def concept_description_prompt(name: str, evidence_block: str) -> str:
    return (
        "Write a concise 1-2 sentence technical description of the concept "
        f'"{name}" for a semiconductor/IC-design knowledge base, synthesizing the '
        "source snippets below (which mention it across documents). Merge the "
        "snippets, resolve any contradictions into a single coherent description, "
        "stay factual to the snippets, third person, include the concept name. "
        "Return JSON only with a 'description' field.\n\n"
        f"Concept: {name}\n\nSource snippets:\n{evidence_block}"
    )


def notebook_description_prompt(sources_block: str) -> str:
    return (
        "Write a concise 1-2 sentence description (Chinese ok) of what this "
        "knowhow notebook covers, based on the sources its curator has added. "
        "Describe the subject matter and document types; do not invent scope "
        "beyond the sources. Return valid JSON only with a 'description' field.\n\n"
        f"Sources:\n{sources_block}"
    )


NOTEBOOK_META_SCHEMA_HINT = '{"name":"","description":""}'


def notebook_meta_prompt(sources_block: str) -> str:
    return (
        "Based on the sources a curator added to this semiconductor knowhow "
        "notebook, propose a concise notebook NAME (<= 20 characters, no quotes) "
        "and a 1-2 sentence DESCRIPTION (Chinese ok) of what it covers. Describe "
        "the actual subject matter and document types; do not invent scope beyond "
        "the sources. Return valid JSON only with 'name' and 'description'.\n\n"
        f"Sources:\n{sources_block}"
    )


REFINE_SCHEMA_HINT = '{"items":[{"index":0,"keep":true}]}'


def refine_prompt(section_path: str, records_block: str, elements_block: str) -> str:
    return (
        "You verify extracted knowledge items against their source document "
        "(self-refinement pass). For EACH numbered item decide keep=true or "
        "keep=false:\n"
        "- keep=false if the item is NOT supported by the source text "
        "(hallucinated), is too vague to be useful, or merely restates a "
        "heading; otherwise keep=true.\n"
        "Return JSON only, one entry per input index.\n\n"
        f"Source section: {section_path}\n\n"
        f"Extracted items:\n{records_block}\n\n"
        f"Source elements (ground truth):\n{elements_block}"
    )


def gleaning_prompt(section_path: str, doc_type: str) -> str:
    return (
        "You already extracted a knowledge-graph fragment from this passage "
        f"(section: {section_path}, doc type: {doc_type}). MANY valid nodes may "
        "have been missed. Add "
        "ONLY the NODES that were missed — use the SAME node types (Concept, "
        "Claim, Formula, Procedure) and the SAME JSON schema, each with its "
        'integer "ev" element label. Do NOT repeat nodes you already extracted. '
        "If nothing was missed, return an empty nodes list. Return JSON only."
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


FOLLOWUP_REWRITE_SCHEMA_HINT = '{"query":""}'


def followup_rewrite_prompt(history_block: str, question: str) -> str:
    return (
        "You rewrite a possibly-elliptical follow-up question into ONE standalone "
        "search query for a knowledge base, using the prior conversation to "
        "resolve pronouns and omissions (e.g. '这个流程' -> the concrete flow named "
        "earlier).\n"
        "Rules:\n"
        "- Output ONE concise query in the SAME language as the question.\n"
        "- Resolve references to concrete entities mentioned in the conversation.\n"
        "- Keep it search-friendly (keywords + the resolved entity); do NOT answer.\n"
        "- If the question is already standalone, return it essentially unchanged.\n\n"
        f"Prior conversation:\n{history_block}\n\n"
        f"Follow-up question: {question}\n\n"
        'Return JSON only: {"query":"<standalone search query>"}'
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
        "2. A [k] marker may ONLY be attached to a sentence whose content comes "
        "DIRECTLY from that specific knowledge item. NEVER attach [k] to an "
        "inference, a general-knowledge statement, or any sentence the items do not "
        "support. When a sentence is your own inference, add NO [k] marker and make "
        "clear it is reasoning (prefix with '（推断）' / 'Likely,'). If the knowledge "
        "items do not cover the question at all, set grounded=false AND the answer "
        "MUST NOT contain any [k] marker.\n"
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


PLAN_SCHEMA_HINT = (
    '{"sub_queries":[{"query":"","types":["concept","claim","formula","procedure"],'
    '"prefer":"keyword|semantic|balanced","reason":""}]}'
)


def plan_prompt(question: str, history_block: str = "") -> str:
    history_section = (
        "Prior conversation (resolve pronouns/ellipsis against it):\n"
        f"{history_block}\n\n" if history_block else ""
    )
    return (
        "You plan how to retrieve a knowledge graph (KG) to answer an "
        "engineer's question. The KG has 4 node types: concept (definitions), "
        "claim (conclusions), formula (math/models), procedure (step flows).\n"
        "Decompose the question into 1-N standalone sub-queries. For EACH:\n"
        "- query: a self-contained search string (resolve any references using "
        "the prior conversation).\n"
        "- types: which node types to search (subset of the 4; omit/empty = all).\n"
        "- prefer: keyword (exact terms/codes), semantic (paraphrase/concept), "
        "or balanced.\n"
        "- reason: one line on why this sub-query.\n"
        "Keep sub-queries focused and non-redundant.\n\n"
        f"{history_section}"
        f"Question: {question}\n\n"
        'Return JSON only: {"sub_queries":[{"query":"","types":[],'
        '"prefer":"balanced","reason":""}]}'
    )


RERANK_SCHEMA_HINT = '{"items":[{"index":0,"score":0.0}]}'


def rerank_prompt(query: str, candidates_block: str) -> str:
    return (
        "Score how relevant each candidate knowledge item is to the user question "
        "on a 0.0-1.0 scale (1.0 = directly answers it, 0.0 = irrelevant). "
        "Return JSON only: one entry per candidate index.\n\n"
        f"Question: {query}\n\n"
        f"Candidates:\n{candidates_block}"
    )


REFLECT_SCHEMA_HINT = (
    '{"sufficient":false,"next_action":"answer|expand_graph|add_subquery|'
    'search_elements","expand":{"object_id":"","edge_type":null,'
    '"direction":"out|in|both"},"new_sub_query":{"query":"","types":[],'
    '"prefer":"balanced","reason":""},"elements_query":"","reason":""}'
)


def reflect_prompt(question: str, candidates_summary: str) -> str:
    return (
        "You decide the NEXT retrieval step for answering a question from a "
        "knowledge graph. Below are the candidates gathered so far.\n"
        "Choose next_action:\n"
        "- answer: candidates suffice — stop and answer.\n"
        "- expand_graph: a candidate looks central; follow its relations one "
        "more hop (set expand.object_id, optional edge_type/direction). You may "
        "expand repeatedly across turns — go as deep as the question needs.\n"
        "- add_subquery: an aspect of the question is uncovered; add one "
        "sub-query (set new_sub_query).\n"
        "- search_elements: the KG is too thin; fall back to raw document "
        "passages (set elements_query).\n"
        "Set sufficient=true only when you can answer well. reason: one line.\n\n"
        f"Question: {question}\n\n"
        f"Candidates so far:\n{candidates_summary}\n\n"
        'Return JSON only matching the schema (omit unused branch fields).'
    )


COMMUNITY_REPORT_SCHEMA_HINT = '{"title":"","summary":"","findings":[""]}'


def community_report_prompt(members_block: str, relations_block: str) -> str:
    return (
        "You are summarizing a community of related items from a semiconductor/IC "
        "design knowledge graph into a short report. Given the member items and "
        "their internal relationships, produce: a short title (the community's "
        "theme), a 2-4 sentence summary, and 3-6 key findings (each a concise "
        "sentence). Stay factual to the members. Return JSON only with "
        "'title','summary','findings'.\n\n"
        f"Members:\n{members_block}\n\nInternal relationships:\n{relations_block}"
    )


# ---------------------------------------------------------------------------
# Global map-reduce 问答 (R4, GraphRAG-style)
# ---------------------------------------------------------------------------

GLOBAL_MAP_SCHEMA_HINT = '{"points":[{"description":"","score":0}]}'


def global_map_prompt(question: str, report_block: str) -> str:
    return (
        "You extract, from ONE community report, the points relevant to the user "
        "question, each with an importance score 0-100 (0 = irrelevant). If the "
        "report is irrelevant, return an empty points list. Be faithful to the "
        "report. Return JSON only with 'points':[{'description','score'}].\n\n"
        f"Question: {question}\n\nCommunity report:\n{report_block}"
    )


GLOBAL_REDUCE_SCHEMA_HINT = '{"answer":"","grounded":true}'


def global_reduce_prompt(question: str, points_block: str) -> str:
    return (
        "You answer the user question by synthesizing the key points below "
        "(gathered from community reports, sorted by importance). Be concrete and "
        "structured. If the points do not cover the question, say so and set "
        "grounded=false. Return JSON only with 'answer' and 'grounded'.\n\n"
        f"Question: {question}\n\nKey points:\n{points_block}"
    )
