# KG Frontend Alignment — Implementation Plan

> **For agentic workers:** execute as one cohesive refactor of `frontend/app/page.tsx` (single 2,826-line file). Gate on `npm run lint` (`tsc --noEmit`).

**Goal:** Make the Next.js frontend consistent with the KG-only backend — remove all calls to deleted endpoints and all consumers of removed `AskResponse` fields/object-types, and drive knowledge browse purely from the dynamic `/knowledge-types`.

**Context:** Backend is now KG-only (object types `concept/claim/formula/procedure`). Deleted endpoints: `/rules`, `/methods`, `/risks`, `/glossary`, `/rules/{id}/explain`, `/scenario-query`, `/case-search`, `/checklist`. `AskResponse` slimmed to `{answer_id, conclusion, related_knowledge: KnowledgeRecord[], citations: Citation[], llm_mode}`. KEPT & already KG-agnostic: `/graph`, `/knowledge?type=`, `/knowledge-types`, `/doc-types`, `/object-schemas`, upload/sources/notebooks, `/ask`.

**Single file:** `frontend/app/page.tsx`. All line numbers below are from the pre-edit file (they shift as you edit — locate by symbol, not line).

---

## Change list (all in `frontend/app/page.tsx`)

### A. Delete dead API-call functions + their UI
1. **`runScenario()`** (~1021–1030, calls `/scenario-query`) — delete the function, the scenario chat-mode UI block (~1561–1589), and the button at ~1577.
2. **`explainRule()`** (~1130–1136, calls `/rules/{id}/explain`) — delete the function, the explain modal (~2130–2196), and the `KnowledgeBrowser` prop wiring at ~1636.
3. **`runCaseSearch()`** (~1032–1039, calls `/case-search`) — delete function + the "case" chat-mode UI (~1591–1604).
4. **`runChecklist()`** (~1041–1048, calls `/checklist`) — delete function + the "checklist" chat-mode UI (~1606–1619).

### B. Remove the hardcoded bespoke knowledge types
5. **`KNOWLEDGE_KINDS`** (~217–224, the `[rule, method, risk, glossary]` array) and **`BESPOKE_KINDS`** — delete. Knowledge browse must be driven ENTIRELY by the dynamic `/knowledge-types` response.
6. **`loadKnowledge()`** (~1050–1072) — remove the `bespoke`/`KNOWLEDGE_KINDS` branch that fetched `/rules` etc.; always fetch generic `/notebooks/{id}/knowledge?type=<kind>`.
7. **`KnowledgeBrowser` tabs** (~2620–2625) — build tabs purely from the `types` (`/knowledge-types`) list; drop the `...KNOWLEDGE_KINDS.map(...)` merge.
8. **`findDuplicates()`** (~1101–1109) — drop the `KNOWLEDGE_KINDS.find(...)` path mapping; pass the raw `kind` to `/duplicates?type=`.

### C. Slim AskResponse + its renderer
9. **`AskResponse` type** (~91–117) — keep only `answer_id, conclusion, related_knowledge, citations, llm_mode` (+ keep `answer_id`/`llm_mode` if already optional). Delete `related_rules, related_cases, recommended_methods, potential_risks, checklist, applicable_scenario, missing_information`.
10. **`AnswerView` component** (~2725–2826) — delete the rendered sections for the removed fields (related_rules ~2768–2774, related_cases ~2778–2784, checklist ~2787, missing_information ~2807, potential_risks ~2809). Keep/render: `conclusion`, `related_knowledge` (render each as a KnowledgeRecord with headline + type + evidence), `citations`. If `related_knowledge` is not already rendered, add a simple section listing each record's `headline`, `object_type`, and first evidence.

### D. Remove now-unused types + components
11. Delete unused types: `RuleExplanation` (~317–326), `CaseCard` (~150–158), `ChecklistItem` (~160–166), `ScenarioForm` (~168–192), `RuleCard` (~138–148) — and their list components `CaseList` (~2324), `ChecklistList` (~2347) if not referenced after the above edits. Grep each symbol before deleting to confirm zero remaining references.

### E. Copy + counts cleanup
12. **Featured filter** (~671–676) — it reads `notebook.counts.rules/.cases/.article_claims`. Replace with a check over the KG types (e.g. featured if total approved knowledge > 0, or `counts.concept/claim/formula/procedure`), or remove the "featured" filter option if it no longer makes sense. Pick the minimal change that compiles and is sensible; do not invent new backend fields.
13. **`CHAT_MODES`** (~206–210) — keep `["ask","问答"]` and the knowledge-library mode `["rules","知识库"]` (this key drives the `KnowledgeBrowser`, NOT the deleted `/rules` endpoint — keep it, optionally relabel). Remove any mode entries that only drove scenario/case/checklist.
14. **Review-queue copy** (~1956) — replace the sentence mentioning "规则、方法、风险、案例、checklist 和术语" with the KG types ("概念、论断、公式、过程").

---

## Gate
- `cd frontend && npm run lint` (`tsc --noEmit`) → ZERO type errors. (Deps are installed in the main session; node_modules is present.)
- `grep -nE "related_rules|related_cases|recommended_methods|potential_risks|checklist|applicable_scenario|missing_information|scenario-query|/rules|/methods|/risks|/glossary|case-search|RuleExplanation|CaseCard|ChecklistItem|ScenarioForm|KNOWLEDGE_KINDS|BESPOKE_KINDS|runScenario|explainRule|runCaseSearch|runChecklist" frontend/app/page.tsx` → no remaining references (except legitimate ones like the `"rules"` CHAT_MODES key for the knowledge browser, and `citations`/`related_knowledge`). Review each hit.

## Commit
`git add frontend/app/page.tsx && git commit -m "feat(kg): align frontend to KG-only backend (remove dead endpoints/types; dynamic knowledge browse)"`
