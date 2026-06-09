# `schema/` — KG schema for the unified analog-circuit knowledge base

Canonical, version-controlled definition of the knowledge-graph schema that the
**~100k-doc base extraction must be locked against** (locking the schema before that
token-expensive run is the whole point of this directory).

## Files

| file | role |
|---|---|
| `kg-schema.yaml` | **Machine-readable source of truth.** Extraction-prompt generation, ingest validation, and committee tooling read this. |
| `kg-schema.md` | **Human spec**: rationale, empirical validation, examples, edge-trust model, committee guidance, open decisions. |
| `README.md` | This file. |

## Status

**LOCKED (v1.0.0, 2026-06-09).** The four prior open decisions are resolved as recommended (see below). Schema changes are now high-ceremony: committee review + version bump (and, once base extraction has run, a re-extraction-cost assessment).

## What's locked (v1.0.0)

- 4 node types (Concept/Claim/Formula/Procedure); atomic-proposition unit; element-anchored evidence; same atom for reasoning/citation/review/merge/promotion; two tiers (base authoritative / personal); rustworkx in-memory graph (not a graph DB); Postgres+pgvector+FTS store of record.
- **Resolved 2026-06-09:** `validity_scope` structured attribute on Claim/Formula; broadened reasoning-edge type constraints (connect Claim/Formula, not Concept-only); base-tier meta-text filtering; the 50–100-doc calibration gate before the 100k run.

## Lock workflow

1. ~~Confirm the 4 open decisions~~ — DONE (locked v1.0.0, 2026-06-09).
2. ~~Set `status: locked` + bump `version`~~ — DONE.
3. Update the extraction prompt (`backend/app/services/kg/extract.py`) and object registry (`extraction_profiles.py`) to match (notably: `validity_scope` field; broadened reasoning-edge constraints; explicit reasoning-edge hunting; base meta-text filter).
4. Run the **calibration gate** (50–100 docs) — measure reasoning-edge density, atomicity, cost/doc.
5. Only then commit the full ~100k-doc extraction.

## Relationship to the plans

This schema is the data-model contract underneath the scale roadmap:
`docs/superpowers/plans/2026-06-09-unified-kg-scale-roadmap.md` (corpus model, substrate, two-stage retrieval, batch merge/community, governance/promotion).
