# KG Generation Contract — operational definitions

The authoritative, operational definitions of every node and edge type. Both the
gold generator and human curators apply THESE rules (this is the root fix for the
old "fine types with no definitions" problem).

## Node types (exactly 4)
Each node currently carries only: `id`, `type`, `name` (the node's text),
`section_path`, `evidence[]`, `mentions[]`. Richer per-type **attributes are
deferred** — their shape is undecided (see fangan_todo.md "KG 重构"). Until then the
node's text lives wholly in `name`.

- **Concept** — a named noun-like entity (term, concept, method, component, device,
  system, material). Test: it can be a grammatical subject/object and recurs across
  sentences. `name` = the entity name.
- **Claim** — a truth-evaluable assertion ABOUT one or more Concepts (a claim,
  finding, principle, mechanism, or definitional statement). Test: it has a predicate
  and asserts one fact. `name` = the full assertion statement.
- **Formula** — an equation/expression. Test: contains `=` or a math operator.
  `name` = the expression.
- **Procedure** — an ordered process (fabrication flow, worked-example solution,
  derivation chain). Test: >= 2 ordered steps. `name` = the procedure name.

## Edge types (source_type -> target_type : trigger)
- defines: Claim -> Concept : the claim states what the concept IS.
- part_of / composed_of: Concept -> Concept : structural containment.
- contrasts_with: Concept -> Concept : explicit contrast/vs.
- kind_of: Concept -> Concept : taxonomic is-a.
- about: Claim|Formula -> Concept : the statement/formula is about the concept.
- supports: Claim|Formula -> Claim : evidence/derivation supporting a claim.
- derived_from: Formula -> Formula : derived from a prior formula.
- depends_on / prerequisite_of: Concept|Formula -> Concept : needs the target first.
- used_in: Formula -> Procedure : the formula is used by the procedure/example.
- precedes: within a Procedure's steps, ordering.

## Evidence (hard invariant)
Every node and edge carries evidence: one or more verbatim source spans such that
source_text[char_start:char_end] == quote. Ungroundable items are dropped.

## Canonicalization
Concept nodes with the same normalized name (or listed alias) merge into ONE node
across sections and documents; mentions[] records every source span.
