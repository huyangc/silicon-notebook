"""Prompt templates and JSON schema hints for silicon-notebook LLM tasks.

Kept separate from business logic so prompts can be versioned and tuned
without touching the repository/extraction code.
"""

from __future__ import annotations

from typing import List, Optional


DESCRIPTION_SCHEMA_HINT = '{"description":""}'

CONCEPT_DESC_SCHEMA_HINT = '{"description":""}'


def concept_description_prompt(name: str, evidence_block: str) -> str:
    return (
        "Write a concise 1-2 sentence technical description of the concept "
        f'"{name}" for a semiconductor/IC-design knowledge base, synthesizing the '
        "source snippets below (which mention it across documents). Merge the "
        "snippets, resolve any contradictions into a single coherent description, "
        "stay factual to the snippets, third person, include the concept name. "
        "Preserve entity/concept names, formula expressions and canonical labels "
        "EXACTLY as they appear in the source, in their original language — do NOT "
        "translate or transliterate them; write the description in the language of "
        "the source snippets. "
        "Return JSON only with a 'description' field.\n\n"
        f"Concept: {name}\n\nSource snippets:\n{evidence_block}"
    )


def notebook_description_prompt(sources_block: str) -> str:
    return (
        "Write a concise 1-2 sentence description, in the dominant language of the "
        "sources, of what this knowhow notebook covers, based on the sources its "
        "curator has added. Describe the subject matter and document types; do not "
        "invent scope beyond the sources. Return valid JSON only with a "
        "'description' field.\n\n"
        f"Sources:\n{sources_block}"
    )


NOTEBOOK_META_SCHEMA_HINT = '{"name":"","description":""}'


def notebook_meta_prompt(sources_block: str) -> str:
    return (
        "Based on the sources a curator added to this semiconductor knowhow "
        "notebook, propose a concise notebook NAME (<= 20 characters, no quotes) "
        "and a 1-2 sentence DESCRIPTION, both in the dominant language of the "
        "sources, of what it covers. Describe the actual subject matter and "
        "document types; do not invent scope beyond the sources. Return valid "
        "JSON only with 'name' and 'description'.\n\n"
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
        "If nothing was missed, return an empty nodes list. "
        "Preserve entity/concept names, formula expressions and canonical labels "
        "EXACTLY as they appear in the source text, in their original language — "
        "do NOT translate or transliterate them. Return JSON only."
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
        "new_types list if nothing new is warranted.\n"
        "- Keep object_type / field KEYS as snake_case ASCII identifiers; but write "
        "the human-facing label / description / rationale in the language of the "
        "source material, and preserve any entity/concept names from the source "
        "in their original language — do NOT translate them.\n\n"
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
        "4. Answer in the question's language. Be concrete.\n"
        "5. Items tagged [base] come from the authoritative reference knowledge "
        "base; items tagged [personal] are the user's own notes. If a personal "
        "item contradicts a base item, defer to the base item's position and "
        "briefly note the discrepancy (e.g. '(note: your notebook states X, but "
        "the base reference says Y)').\n"
        "6. Typeset ALL math as LaTeX so the UI can render it; never write math "
        "as plain text. Wrap inline expressions, variables and symbols in single "
        "dollar signs — e.g. $A_{dm}$, $\\mathrm{CMRR}=|A_{dm}/A_{cm}|$, "
        "$\\Delta V_{OS}$ — and put a standalone equation on its OWN line wrapped "
        "in double dollar signs, e.g.\n"
        "$$\\mathrm{CMRR}=\\frac{A_{dm}}{A_{cm}}$$\n"
        "Use real LaTeX commands (\\frac, _{}, ^{}, \\Delta, \\approx, \\mathrm); "
        "do NOT emit plain forms like A_dm or V_OS1. Inline $...$ must stay on one "
        "line with no '$' inside it. Keep [k] markers OUTSIDE the math (after the "
        "sentence), never inside $...$.\n"
        "7. For a question that asks for a multi-layer mechanism or derivation, "
        "organize the answer layer by layer (e.g. circuit principle -> device "
        "physics -> statistical/solid-state physics -> quantum/lattice origin -> "
        "engineering practice) and keep the derivation chain complete within each "
        "layer; where the knowledge items lack a link of the chain, bridge it "
        "explicitly as （推断）.\n"
        "8. Keep formulas dimensionally consistent and prefer the circuit-"
        "realizable form the sources use (e.g. $\\Delta V_{BE}=V_T\\ln N$ rather "
        "than an abstract $K\\cdot V_T$); when converting between energy and "
        "voltage, state the conversion (e.g. $E_g=qV_{G0}$) — as a （推断） note "
        "if the items use a different notation.\n"
        "9. When a specific numeric value comes from a single source, attribute "
        "it as that source's stated value; you may add the typical engineering "
        "range or the factors that shift it, marked as （推断）.\n\n"
        f"{history_section}"
        f"Question: {question}\n\n"
        f"Knowledge items (id: [type][tier] name — context):\n{context_block}\n\n"
        'Return JSON only: {"answer":"<text with [k] markers>","grounded":true|false}'
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


REFLECT_SCHEMA_HINT = (
    '{"sufficient":false,"next_action":"answer|expand_graph|add_subquery|'
    'search_elements|ppr_retrieve|expand_community","expand":{"object_id":"","edge_type":null,'
    '"direction":"out|in|both"},"new_sub_query":{"query":"","types":[],'
    '"prefer":"balanced","reason":""},"community_focal":"","elements_query":"","ppr_query":"","reason":""}'
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
        "sub-query (set new_sub_query). Never re-submit a sub-query already "
        "listed as tried in the context; rephrase it substantially or choose "
        "a different action.\n"
        "- search_elements: the KG is too thin; fall back to raw document "
        "passages (set elements_query).\n"
        "- ppr_retrieve: the question compares across models/sources or needs "
        "breadth across documents; pull cross-document source passages via PPR "
        "(set ppr_query). Prefer this for comparison / cross-paper questions where "
        "single-document evidence isn't enough, or when a multi-layer derivation "
        "needs supporting passages scattered across documents.\n"
        "- expand_community: the question compares an entity with its peers / other "
        "of-its-kind, and those peers are missing from candidates; pull the entity's "
        "SEMANTIC COMMUNITY members across documents (set community_focal to the entity "
        "name, e.g. 'DeepSeek-V4'). Use for 'X vs other Y' questions.\n"
        "Before choosing answer, check aspect by aspect that every part the "
        "question explicitly asks for (each layer / entity / requirement it "
        "names) is covered by the candidates; if an asked-for aspect has no "
        "evidence yet, prefer a retrieval action targeting it. Set "
        "sufficient=true only when that per-aspect check passes (or further "
        "retrieval keeps failing). reason: one line.\n\n"
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
        "sentence). Stay factual to the members. Preserve entity/concept names, "
        "formula expressions and canonical labels EXACTLY as they appear, in their "
        "original language — do NOT translate them; write the title/summary/findings "
        "in the language of the source material. Return JSON only with "
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
        "structured. Answer in the SAME language as the question. If the points do "
        "not cover the question, say so and set grounded=false. Return JSON only "
        "with 'answer' and 'grounded'.\n\n"
        f"Question: {question}\n\nKey points:\n{points_block}"
    )


EVIDENCE_REFINE_SCHEMA_HINT = '{"relevant":[""]}'


def evidence_refine_prompt(question: str, evidence_block: str) -> str:
    return (
        "From the retrieved knowledge items below, extract ONLY the statements "
        "directly relevant to answering the question, as a concise list (verbatim "
        "or lightly compressed, faithful to the items). Drop irrelevant items. "
        "Keep entity/concept names, formula expressions and canonical labels EXACTLY "
        "as they appear, in their original language — do NOT translate them. If "
        "none are relevant, return an empty list. Return JSON only with 'relevant'.\n\n"
        f"Question: {question}\n\nRetrieved items:\n{evidence_block}"
    )


EXPAND_SCHEMA_HINT = ('{"query":"","high_level_keywords":[],"low_level_keywords":[],'
                      '"sub_queries":[{"query":"","types":[],"prefer":"balanced","reason":""}]}')


def expand_query_prompt(question: str, history_block: str = "", want_types: bool = False,
                        max_subqueries: int = 4,
                        corpus_langs: Optional[List[str]] = None) -> str:
    history_section = (
        "Prior conversation (resolve pronouns/ellipsis against it):\n"
        f"{history_block}\n\n" if history_block else "")
    types_line = (
        "- types: which KG node types to search (subset of concept/claim/formula/"
        "procedure; omit/empty = all). prefer: keyword|semantic|balanced.\n"
        if want_types else "")
    types_schema = ',"types":[],"prefer":"balanced"' if want_types else ""
    langs = [l for l in (corpus_langs or ["zh", "en"]) if l] or ["zh", "en"]
    if len(langs) > 1:
        kw_langs_rule = (
            "provide terms in EACH of these corpus languages: "
            f"{', '.join(langs)} — for a term with a well-known form in another "
            "listed language (e.g. an English acronym for a Chinese concept, or "
            "vice-versa), include BOTH forms — so lexical search matches documents "
            "in any of them.")
    else:
        kw_langs_rule = (
            f"provide terms in the corpus language ({langs[0]}); a single-language "
            "corpus needs only single-language keywords.")
    return (
        "You prepare an engineer's question for retrieval over a document "
        "corpus. Produce:\n"
        "1. query: the question rewritten cleanly IN ITS OWN LANGUAGE (spell "
        "entity/version names canonically, e.g. 'deepseekv2' -> 'DeepSeek-V2').\n"
        "2. high_level_keywords: themes / relationship types / abstract topics "
        f"(used to retrieve RELATIONS) — {kw_langs_rule}\n"
        "3. low_level_keywords: concrete entities / names / specifics (used to "
        f"retrieve ENTITIES) — {kw_langs_rule}\n"
        f"4. sub_queries: 1-{max_subqueries} focused, standalone retrieval queries IN "
        "THE QUESTION'S LANGUAGE that together cover the question. For a COMPARISON, "
        "emit ONE sub-query per entity (e.g. 'DeepSeek-V2 architecture and features', "
        "'DeepSeek-V3 improvements'). For a BROAD/overview question, emit one per "
        "distinct dimension. For a simple single-topic question, ONE sub-query is "
        "fine. For a DEEP MECHANISM/DERIVATION question that spans abstraction "
        "levels, emit one sub-query per level it crosses (e.g. circuit principle / "
        "device physics / statistical or solid-state physics / quantum-lattice "
        "origin / engineering constraints such as packaging & materials). Use "
        "canonical entity names.\n"
        f"{types_line}"
        "Keep sub-queries non-redundant.\n\n"
        f"{history_section}"
        f"Question: {question}\n\n"
        'Return JSON only: {"query":"","high_level_keywords":[],'
        '"low_level_keywords":[],"sub_queries":[{"query":""' + types_schema + "}]}"
    )


# ---------------------------------------------------------------------------
# 深度报告(report_engine)
# ---------------------------------------------------------------------------

REPORT_OUTLINE_SCHEMA_HINT = (
    '{"sections":[{"title":"","scope":"","sub_queries":[""]}]}')


def report_outline_prompt(question: str, max_sections: int = 6,
                          history_block: str = "") -> str:
    history_section = (
        "Prior conversation (for context):\n" f"{history_block}\n\n"
        if history_block else "")
    return (
        "You plan the OUTLINE of a deep technical report that answers an "
        "engineer's question from a document corpus. Produce 3-" f"{max_sections} "
        "sections. Rules:\n"
        "- Sections follow the question's own structure; for a multi-layer "
        "mechanism question, one section per abstraction layer (e.g. circuit "
        "principle / device physics / statistical & solid-state physics / "
        "quantum-lattice origin / engineering requirements such as packaging & "
        "materials).\n"
        "- Do NOT include executive-summary / references / knowledge-gap "
        "sections — the system appends those automatically.\n"
        "- Each section: title (in the question's language), scope (one line, "
        "what the section must establish), sub_queries (2-4 focused ENGLISH "
        "retrieval queries for that section's evidence).\n\n"
        f"{history_section}"
        f"Question: {question}\n\n"
        'Return JSON only: {"sections":[{"title":"","scope":"","sub_queries":[""]}]}'
    )


REPORT_SECTION_SCHEMA_HINT = '{"markdown":"","grounded":true}'


def report_section_prompt(section_title: str, section_scope: str, question: str,
                          context_block: str, allow_parametric: bool = True) -> str:
    parametric_rule = (
        "4. You MAY use domain general knowledge beyond the items when the "
        "items do not cover a needed link — but EVERY such sentence must start "
        "with the marker 【通识】, carry NO [k] marker, and numeric values must "
        "be given as typical ranges, not point values.\n"
        if allow_parametric else
        "4. Do NOT introduce facts beyond the knowledge items; where evidence "
        "is missing, state the gap explicitly.\n")
    return (
        "You write ONE section of a deep technical report for an engineer. "
        "Write ONLY this section — no report title, no executive summary, no "
        "other sections' content.\n"
        f"Report question: {question}\n"
        f"Section title: {section_title}\n"
        f"Section scope: {section_scope}\n"
        "Rules:\n"
        "1. When a sentence uses a knowledge item, append its id marker like "
        "[k1] at the end of that sentence. A [k] marker may ONLY be attached "
        "to a sentence whose content comes DIRECTLY from that item.\n"
        "2. When a sentence is your own inference bridging the items, prefix "
        "it with （推断） and attach NO [k].\n"
        "3. Keep the derivation chain complete within this section's scope; "
        "keep formulas dimensionally consistent and prefer circuit-realizable "
        "forms; single-source numeric values: attribute as that source's "
        "stated value, ranges may be added as （推断）.\n"
        f"{parametric_rule}"
        "5. Answer in the question's language. Typeset ALL math as LaTeX "
        "($...$ inline, $$...$$ display); keep [k] markers outside math.\n"
        "6. Start the section body directly with a '## <section title>' "
        "heading, then prose (tables allowed in GitHub markdown).\n"
        "7. grounded=true only if at least one [k] appears in the section.\n"
        "8. Items tagged [base] come from the authoritative reference knowledge "
        "base; items tagged [personal] are the user's own notebook. If a personal "
        "item contradicts a base item, defer to the base item's position and "
        "briefly note the discrepancy. Relevance comes first: cite a [base] item "
        "ONLY when it actually supports THIS section — if a base item is not "
        "relevant to this section, do NOT force it in.\n\n"
        f"Knowledge items (id: [type][tier] name — context):\n{context_block}\n\n"
        'Return JSON only: {"markdown":"","grounded":true|false}'
    )


def report_summary_prompt(question: str, sections_block: str) -> str:
    return (
        "Write the EXECUTIVE SUMMARY (one tight paragraph, 120-250 words, in "
        "the question's language) of the report below: the direct answer "
        "first, then the load-bearing findings and the key engineering "
        "recommendations. No new facts, no citations markers, no headings.\n\n"
        f"Question: {question}\n\nReport sections:\n{sections_block}\n\n"
        'Return JSON only: {"summary":""}'
    )


REPORT_STORM_SCHEMA_HINT = (
    '{"sections":[{"title":"","scope":"","sub_queries":[""],'
    '"perspectives":[""],"tensions":[""]}]}')


def report_storm_outline_prompt(question: str, corpus_map: str,
                                max_sections: int = 6, history_block: str = "") -> str:
    history_section = (f"Prior conversation:\n{history_block}\n\n" if history_block else "")
    return (
        "You plan the OUTLINE of a deep, insightful technical report — NOT a shallow "
        "summary. Derive it by PRE-WRITING, not by writing it directly:\n"
        "1. Adopt 3-4 expert perspectives (lenses): first dynamically generate 2-3 "
        "PERSPECTIVES tailored to THIS question & corpus; then add 1-2 from the "
        "general set (domain expert / hands-on practitioner / risk-skeptic). "
        "Perspectives must serve answering the user's question — do not add lenses "
        "for mere variety.\n"
        "2. From each perspective, RAISE (raise) 2-3 deep questions about the user's "
        "question (e.g. the skeptic asks about failure modes / risks / missing "
        "evidence).\n"
        "3. Dedup and cluster (CLUSTER) these questions by theme into report "
        "sections.\n"
        "4. PRESERVE the TENSION (tension): where perspectives disagree, keep the "
        "conflict explicit as an insight — never flatten into one-sided "
        "praise/summary.\n"
        "5. Sections must be MECE (mutually exclusive, no overlap; collectively cover "
        "the question).\n"
        "6. Ground in the corpus map below: sub_queries MUST reuse the actual "
        "vocabulary / entity names that appear in the map (verbatim spelling).\n"
        "7. Keep a section the question explicitly asks for EVEN IF the map lacks it "
        "(the map is a sample; the writing stage can bridge gaps as 【通识】).\n"
        "8. If the question compares an entity with its peers, plan ONE dedicated "
        "cross-model comparison section (横向对比) whose sub_queries target the peer "
        "entities' corresponding dimensions.\n"
        f"Produce 3-{max_sections} sections. Do NOT include executive-summary / "
        "references / knowledge-gap sections (auto-appended). Each section: title "
        "(question's language), scope (one line), sub_queries (2-4 focused ENGLISH "
        "retrieval queries), perspectives (which lenses it came from), tensions "
        "(one line each; which other section/lens it conflicts with, or []).\n\n"
        f"{history_section}"
        f"Question: {question}\n\n"
        f"Corpus map (what the library actually contains):\n{corpus_map}\n\n"
        'Return JSON only: {"sections":[{"title":"","scope":"","sub_queries":[""],'
        '"perspectives":[""],"tensions":[""]}]}'
    )


REPORT_SUFFICIENCY_SCHEMA_HINT = (
    '{"verdicts":[{"title":"","sufficiency":"充足|薄弱|缺失",'
    '"gap_note":"","action":"keep|supplement|external"}]}')


def report_sufficiency_prompt(question: str, probe_block: str) -> str:
    return (
        "You judge whether the notebook library has ENOUGH evidence for each planned "
        "report section. You are given each section's title and its OBJECTIVE retrieval "
        "hit counts (hits = distinct knowledge items its sub-queries matched; base_hits "
        "= from the authoritative base library). Trust the counts as the ground truth "
        "of coverage; your job is to interpret them into a verdict + a one-line gap note "
        "+ a suggested action. Rough guide: many hits → 充足(keep); few/only-tangential "
        "→ 薄弱(supplement, note what's missing); ~0 hits → 缺失(external, the library "
        "cannot support it). Do not invent coverage the counts don't show.\n\n"
        f"Report question: {question}\n\n"
        f"Sections with hit counts:\n{probe_block}\n\n"
        'Return JSON only: {"verdicts":[{"title":"","sufficiency":"","gap_note":"","action":""}]}'
    )
