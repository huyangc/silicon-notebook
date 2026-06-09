# KG Schema — unified analog-circuit knowledge base (authoritative spec)

> Machine-readable source of truth: **`kg-schema.yaml`** (extraction / validation / tooling read that). This doc is the human rationale, examples, and committee guidance.
> **Status: LOCKED (v1.0.0, 2026-06-09).** Resolved decisions in §8. Changes are high-ceremony (committee + version bump).

This schema is **built on the current 4-type extractor** (`backend/app/services/kg/extract.py`, `extraction_profiles.py`, `kg/models.py`) and **locks + extends** it. It was validated empirically against the existing corpus (7 sources: Razavi ×2, Gray, Allen-Holberg ×2, an Innovus manual; 36,929 objects / 46,470 relations).

---

## 1. Why 4 node types are enough (empirical)

Object distribution in the real corpus: **claim 45.7% · formula 27.1% · concept 22.9% · procedure 4.2%**. `claim + formula + concept = 96%` — exactly the strict-reasoning core. All 36,929 objects mapped cleanly into the 4 types; no large "doesn't fit" bucket emerged.

- **Parameters/quantities** (gain, GBW, phase margin, IIP3, roll-off factor) already live as **Concepts** in the data and work — trade-offs between them are `contrasts_with` Claims. A separate `Quantity` type would add extraction cost for marginal reasoning gain → **not added**. (A `concept_subtype` tag is reserved but not extracted.)
- Adding node types is expensive at 100k-doc scale (more tokens, more prompt complexity, lower per-type precision). **Decision: lock at 4.**

## 2. The atomic unit

**One node = one atom**, and the same atom is the unit for **reasoning, citation, review, merge, and promotion**:

| type | atom |
|---|---|
| Concept | one named reusable entity |
| Claim | one self-contained, verifiable proposition |
| Formula | one equation |
| Procedure | one ordered process (with `steps[]`) |

Current claims are already fairly atomic (e.g. *"Increasing C_Lc decreases ω_u,cm"*), but extraction is **selective** (≈3.4k claims/textbook ≪ the true atomic-proposition count). Atomic + higher recall will **increase node count → higher token cost**, traded for reasoning precision + reviewability. This cost is accepted.

## 3. The one essential addition: `validity_scope` (structured conditions)

Analog reasoning is **condition-sensitive** — a result true in saturation is wrong in triode. The corpus shows conditions are **everywhere but unstructured**, currently buried in claim text or split into *dangling* claims (real examples):

- *"**This holds for DC and low-frequency AC.**"* ← subject lost (dangling)
- *"Calculations **assume perfect matching**; mismatches degrade IRR further"*
- *"where **R_in3 ≪ R_C26 is assumed** in the last approximation"* ← derivation precondition
- *"Severe ringing **for ζ<0.5**; ζ chosen >√2/2"*

→ Add `validity_scope` as a **structured attribute** on **Claim & Formula** (`{region, assumptions[], approximation, range}`). It is **a field, not a node type** → modest extraction cost, large reasoning-safety gain (prevents out-of-scope application — a primary hallucination source). Conditional claims are **split** so the condition lands here, not as prose. (Upgrade to a `Condition` node + `holds_under` edge only if retrieval-by-condition is later needed.)

## 4. The real risk is EDGE coverage, not node types

Relation distribution today:

```
about 27689 (60%, connective)   supports 6068 ✓   derived_from 4160 ✓
used_in 2630   part_of 1544   defines 1364   kind_of 1144
depends_on 791   contrasts_with 556   composed_of 352   precedes 104   prerequisite_of 68 ⚠
```

- **Well-fed:** `derived_from` + `supports` → derivation/support chains have edges to walk. ✓
- **Sparse:** `depends_on 791 · contrasts_with 556 · prerequisite_of 68 · precedes 104`. Reasoning chains *are* these edges; **sparse edges = broken chains.**
- **Two root causes, both fixed in this schema:**
  1. The current prompt **over-constrains** reasoning edges: `derived_from(Formula→Formula)`, `contrasts_with(Concept→Concept)`. But strict reasoning lives on **Claims/Formulas**, and trade-offs/contradictions are **claim↔claim** — which the Concept-only constraint forbids. → **Broadened** (see `kg-schema.yaml` `edge_types`): reasoning edges now connect Claim/Formula/Concept.
  2. The prompt does not **explicitly hunt** the sparse relations. → Extraction directive: prioritize `depends_on / contrasts_with / prerequisite_of`.

**Reasoning-edge recall is the #1 quality lever for the 100k run** — more important than any node-type change.

## 5. Edges: two priority tiers

- **Reasoning-bearing (HIGH priority, committee curation focus):** `supports, derived_from, depends_on, contrasts_with, prerequisite_of, used_in, precedes`.
- **Structural/connective (normal, cheap):** `about, defines, part_of, composed_of, kind_of`.

Every edge carries `evidence` (element-anchored), `confidence`, `tier`, and `corroboration` (# independent docs asserting it — a free trust signal at scale). Full type constraints in `kg-schema.yaml`.

## 6. Tiers & provenance

- **Base** (foundation): committee-maintained, authoritative (**wins on conflict**), batch-built + rare gated additions, shared-read (single-user for POC).
- **Personal**: per-owner, scenario-specific, lightweight, **not** auto-filtered. Promotion into base is **owner-triggered → committee final review**, at the **node** level.
- Reasoning chains may span **both** tiers (personal experience is allowed in a chain); each hop is tier-tagged so the answer shows which steps are authoritative vs personal/unverified, and grounding takes the chain's weakest authority.

## 7. Edge trust without reviewing every edge

Reviewing all edges at 100k scale is infeasible. Layered trust (detail in the scale roadmap):

1. **Automatic signals:** extraction confidence (self-rate × cross-window agreement × endpoint confidence) · evidence-anchoring (no/weak evidence ⇒ suspect) · cross-doc corroboration · type-constraint validation.
2. **Targeted human review** (committee): the **schema + high-centrality backbone edges** (centrality from the in-memory graph) + **flagged conflicts** + **low-confidence-but-high-impact** + **edges actually traversed in answers** (review-on-use).
3. **Answer-time chain verification:** adversarially verify only the **few edges in a chain about to be asserted** (bounded cost, makes strict reasoning trustworthy without pre-verifying the whole graph).
4. **Feedback loop:** wrong-chain reports demote the offending edge.

## 8. Resolved decisions (locked v1.0.0, 2026-06-09)

1. `validity_scope` = **structured attribute** on Claim/Formula (not a `Condition` node yet). — **LOCKED.** Upgrade to a node only if retrieval-by-condition is later needed.
2. Reasoning edges **broadened** to connect Claim/Formula (fixes the thin `contrasts_with`/`derived_from`). — **LOCKED.**
3. **Base-tier meta-text filtering** (drop pedagogical asides / tool trivia; personal tier unfiltered). — **LOCKED.**
4. **Calibration gate**: 50–100 docs → measure edge density / atomicity / cost → then commit the 100k run. — **LOCKED.**

## 9. Provenance / change control

The committee owns this schema. Changes are **versioned** (`version` in `kg-schema.yaml`) and, once base extraction has run, a schema change implies a **re-extraction cost** assessment. Treat schema edits as high-ceremony after lock.
