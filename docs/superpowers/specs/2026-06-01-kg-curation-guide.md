# KG Gold Curation Guide

The gold generator (`scripts/kg_goldgen.py`) writes a DRAFT KG per chapter to
`fangan/testcases_kg/<doc>/<chapter>/gold_kg.yaml`. A human curator reviews each
draft against the generation contract
(`docs/superpowers/specs/2026-06-01-kg-generation-contract.md`) and commits the
curated result (the draft dir is gitignored until curated).

## How to curate one chapter's gold_kg.yaml
1. **Node types** — confirm each node is one of Concept / Claim / Formula /
   Procedure per the contract's test rules. Re-type or drop misfits.
2. **Concepts** — merge over-split Concepts that name the same entity (the
   generator merges by normalized name, but synonyms/abbreviations may remain);
   add missing aliases. Drop noise Concepts (figure labels, citations, fragments).
3. **Claims/Formulas/Procedures** — drop narrative/filler that slipped in; ensure
   each Formula has a meaningful `role`; ensure each Procedure's `steps` are ordered.
4. **Edges** — confirm endpoint types match the contract's source->target rule;
   fix/drop wrong-type or dangling edges; add obvious missing edges (defines,
   about, derived_from, depends_on, used_in).
5. **Evidence** — every node/edge must keep a verbatim evidence span. Do not
   hand-edit char offsets; if a quote is wrong, drop the item or re-run the
   generator for that chapter.

## Committing curated gold
Once a chapter is curated, remove its path from `.gitignore` (or move curated
files out of the ignored tree) and commit, so it becomes the authoritative gold
that the product pipeline is scored against (graph-matching, `score_kg`).
