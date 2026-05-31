# qiefen P0 deterministic baseline

- Date: 2026-05-31
- Command: `PYTHONPATH=backend python scripts/qiefen_score.py --out harness_out/qiefen_pred`
- Pipeline: deterministic S1–S5 + do_not_extract only (no LLM objects/relations/mentions — those are P1).

## Headline

**Mean weighted score: 32.98 / 100** over 14/14 chapters. Every emitted atom satisfies the harness's required invariant `source_file[span] == raw_text` (14/14 chapters pass `test_harness_invariant`).

## Per-stage means (weights from `harness/config.py`)

| stage | weight | mean score | note |
| --- | ---: | ---: | --- |
| evidence_atoms | 0.20 | **0.137** | dragged by over-extraction precision (see below) |
| semantic_chunks | 0.15 | **0.102** | one-chunk-per-section is too coarse vs gold's many small chunks |
| objects | 0.12 | 0.000 | P1 (LLM) |
| object_payload | 0.13 | 0.000 | P1 (LLM) |
| object_evidence | 0.10 | 1.000 | vacuous: no objects to match → harness defaults to 1.0 |
| relations | 0.15 | 0.000 | P1 (LLM) |
| context_packages | 0.05 | 0.107 | object_recall is ~0 without objects (P1) |
| do_not_extract | 0.05 | 1.000 | deterministic negatives working |
| structure | 0.05 | **0.036** | section paths don't match gold strings + no mentions yet |

Of the deterministic ~50% of weight, the live contributors are atoms (0.20), chunks (0.15), structure (0.05), packages (0.05), dne (0.05). The 32.98 is mostly: `object_evidence` vacuous 1.0 (0.10) + `dne` 1.0 (0.05) + partial atoms/chunks.

## Per-chapter (atoms / chunks columns)

| doc | chapter | score | atoms | chunks | pred atoms | gold atoms |
| --- | --- | --: | --: | --: | --: | --: |
| engram | ch00_abstract | 47.24 | 76% | 0% | 10 | 11 |
| engram | ch03_scaling_laws | 39.08 | 5% | 67% | 57 | 17 |
| engram | ch08_conclusion | 37.92 | 42% | 0% | 11 | 8 |
| engram | ch01_introduction | 37.00 | 37% | 0% | 30 | 18 |
| engram | ch02_architecture | 34.45 | 5% | 36% | 82 | 39 |
| engram | ch06_analysis | 32.22 | 11% | 13% | 100 | 44 |
| engram | ch07_related_work | 30.26 | 11% | 0% | 40 | 13 |
| cmos | ch03_device_modeling | 30.30 | 0% | 15% | 1529 | 75 |
| engram | ch04_pretraining | 29.50 | 0% | 0% | 36 | 27 |
| cmos | ch01_introduction | 29.85 | 2% | 0% | 532 | 38 |
| cmos | ch02_cmos_technology | 29.32 | 0% | 9% | 1808 | 86 |
| cmos | ch09_switched_capacitor | 28.56 | 0% | 3% | 4844 | 73 |
| engram | ch04_analog_subcircuits | 28.02 | 0% | 0% | 1958 | 75 |
| engram | ch05_long_context | 28.00 | 0% | 0% | 55 | 17 |

## Diagnosis — the iteration levers

1. **Atom over-extraction (the dominant lever).** The atomizer emits *every* sentence; gold keeps only a curated high-value subset (`_AGENT_SPEC`: "大章节可只收高价值段落"). The gap scales with chapter size:
   - engram/ch00_abstract: 10 pred vs 11 gold → **76%** (a single dense paragraph gold fully atomizes — best case).
   - cmos/ch09: 4844 pred vs 73 gold → **0%** (huge line range; gold curates < 2% of sentences).
   Spans are correct; the loss is precision. Closing this needs **selectivity** — either strong cues that keep only claim/result/formula/definition-bearing atoms, or the LLM/anchor stage deciding which atoms are evidence-worthy.

2. **Section-path mismatch (structure 0.036).** Our breadcrumb strings don't match gold's exact path normalization, and mentions are empty (P1). Section paths are the cheapest win — align the path strings to gold's convention.

3. **Chunk granularity (0.102).** One chunk per section is too coarse; gold splits a section into several typed knowledge-unit chunks (formula+explanation, example, table). Needs the anchor-based boundary logic (qiefen §5.3).

4. **Free buckets:** `do_not_extract` = 1.0 already; `object_evidence` = 1.0 is vacuous and will *drop* once P1 adds objects (so the headline may dip before it climbs).

## Proposed P0 acceptance line (for discussion)

The exhaustive-vs-curated mismatch means a pure "atomize every sentence" P0 cannot reach high atom F1 on the textbook chapters. Two honest options:

- **(A) Re-scope P0 to structural correctness, not curation parity** — accept atoms at recall-oriented quality, fix the cheap wins (section paths → structure, chunk boundaries), and let *selectivity* be owned by P1 (the anchor/LLM stage picks evidence atoms). Target: structure ≥ 0.6, chunks ≥ 0.35, atoms recall-focused.
- **(B) Add deterministic atom selectivity now** — only emit atoms whose sentence matches an evidence cue (claim/result/formula/definition/process/example), suppressing filler. This trades recall for precision and should lift textbook-chapter atom F1 materially before P1.

Recommendation: **(B) for a quick precision lift on textbook chapters, then (A)'s cheap structural wins**, re-baseline, and set the firm number from the second run. Decision pending user input.

## P0.5 deterministic tuning round (same day)

After the 32.98 baseline, three deterministic improvements were applied:

| change | stage moved | before -> after |
| --- | --- | --- |
| numeric-chain section paths (`2 > 2.1`, `Chapter 1` -> `1`) | structure | 0.036 -> **0.389** |
| table `<tr>` -> header/row atoms; `Table N` caption detection | atoms (recall) | — |
| textbook atom selectivity + de-noised cues (drop narrative; image-embed & `<details>` noise; numbered-only problem cue) | atoms (precision) | 0.137 -> **0.198** |

**Mean weighted: 32.98 -> 35.41 / 100.**

Where the gains landed and the honest ceiling:
- **Structure** was the cheap 10x win (section-path F1 ~0.78; the rest of the 0.05 bucket is mentions, which are P1).
- **Article atoms improved broadly** (ch04 0->0.37, ch05 0->0.19, ch06 0.11->0.23; abstract stays 0.76) because gold curates article sentences lightly and our keep-all + cue typing tracks it.
- **Textbook large-chapter atoms stay precision-bound.** cmos/ch01 recall is decent (21/38 gold atoms match at IoU>=0.5) but we still emit ~3.5x gold's count; cmos/ch02–09 stay ~0. The residual is **semantic curation** — *which* of many definitions/principles gold keeps — which cue heuristics cannot capture without overfitting one chapter. This is precisely the job of the P1 LLM object stage (it selects `local_evidence_atom_ids`), so further deterministic atom tuning was stopped here to avoid overfitting.

## Recommended firm P0 acceptance line

- evidence_atoms (det. ceiling): **>= 0.20 mean** (reached: 0.198) — higher requires P1 selectivity.
- semantic_chunks: downstream of atoms; revisit after P1.
- structure: **>= 0.38** (reached: 0.389).
- do_not_extract: **= 1.0** (reached).
- The path to a materially higher total is **P1 (LLM objects/relations/mentions)** — 0.50 of the weight is currently 0 — not more deterministic atom tuning.

## Artifacts

`harness_out/` is git-ignored (generated; some pred.yaml are large). Regenerate with the command at the top.
