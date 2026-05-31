# qiefen P1 (LLM objects/relations/mentions) baseline

- Date: 2026-05-31
- LLM: DeepSeek `deepseek-v4-flash` @ https://api.deepseek.com (OpenAI-compatible)
- Command: `PYTHONPATH=backend python scripts/qiefen_score.py --out harness_out/qiefen_full`
- Concurrency: `ThreadPoolExecutor`, 8 workers (`QIEFEN_LLM_WORKERS`). Full run ~1050s, ~390 object calls + 14 relation calls.

## Headline (and the artifact behind it)

| run | mean weighted |
| --- | --: |
| deterministic only (`--no-llm`, current code) | **35.7** |
| + LLM objects/relations/mentions | **28.3** |

The composite **fell 7.4** even though extraction quality rose sharply. Cause: a
harness scoring artifact in the empty-prediction case.

### Per-stage, deterministic vs +LLM (same code)

| stage | weight | det (no-llm) | +LLM | Δ × weight |
| --- | --: | --: | --: | --: |
| evidence_atoms | 0.20 | 0.198 | 0.198 | 0.000 |
| semantic_chunks | 0.15 | 0.081 | 0.081 | 0.000 |
| objects | 0.12 | 0.000 | **0.474** | +0.057 |
| object_payload | 0.13 | **1.000 (vacuous)** | 0.141 | **-0.112** |
| object_evidence | 0.10 | **1.000 (vacuous)** | 0.353 | **-0.065** |
| relations | 0.15 | 0.000 | 0.115 | +0.017 |
| context_packages | 0.05 | 0.107 | **0.685** | +0.029 |
| do_not_extract | 0.05 | 1.000 | 0.972 | -0.001 |
| structure | 0.05 | 0.389 | 0.417 | +0.001 |

### The artifact

`harness/stages.py` scores `object_payload` and `object_evidence` only over
**matched** object pairs. With zero predicted objects there are no matches, so
`metrics.prf(0,0,0)` returns **f1 = 1.0** ("vacuously perfect") and
`object_evidence` mean-Jaccard defaults to **1.0**. That is **0.23 of total
weight handed to a candidate that extracts nothing.** Real objects with
imperfect payloads/evidence necessarily score below that free 1.0, so adding a
working object stage *lowers* the composite. The metric, as written, rewards
extracting nothing on these two buckets.

This is an edge-case flaw in the harness, not a regression in extraction.

## Resolution: harness fixed (recall-aware), re-scored

`harness/stages.py` was fixed so `object_payload`/`object_evidence` average over
ALL gold objects (unmatched gold contributes 0/fn) instead of only matched
pairs — zero predictions now score 0, not a vacuous 1.0. Gold-vs-gold still
scores 100 (54 self-tests pass; every gold object matches itself).

Re-scored with the fixed harness (same predictions):

| | mean weighted |
| --- | --: |
| deterministic only (no objects) | **12.7** |
| **+ LLM (P1)** | **27.1** (+14.4, +113%) |

| stage | weight | det | +LLM | Δ |
| --- | --: | --: | --: | --: |
| evidence_atoms | 0.20 | 0.198 | 0.198 | 0.000 |
| semantic_chunks | 0.15 | 0.081 | 0.081 | 0.000 |
| objects | 0.12 | 0.000 | 0.474 | +0.474 |
| object_payload | 0.13 | 0.000 | 0.107 | +0.107 |
| object_evidence | 0.10 | 0.000 | 0.276 | +0.276 |
| relations | 0.15 | 0.000 | 0.115 | +0.115 |
| context_packages | 0.05 | 0.107 | 0.685 | +0.577 |
| do_not_extract | 0.05 | 1.000 | 0.972 | -0.028 |
| structure | 0.05 | 0.389 | 0.417 | +0.029 |

Under fair scoring P1 is a clear, large net gain. Article (engram) 32.0,
textbook (cmos) 18.3. Remaining levers: object_payload (0.107), relations
(0.115), and the textbook atom-curation ceiling (atoms 0.198 / chunks 0.081).

## Prompt tuning + LLM judge (semantic scoring) + concurrency

- **Concurrency**: per-package object calls + chapter-level fan-out (14×) +
  200-way judge-cache pre-warm. Full judged scan ~212s (was 1050s serial).
- **Prompt tuning**: concise/atomic payload values, structured numeric fields for
  ExperimentResult, relation type-signatures. Mean 27.1 -> 27.5.
- **LLM judge** (`--llm-judge`, DeepSeek yes/no semantic equivalence, cached +
  pre-warmed): credits correct paraphrases the substring matcher misses.

Final fair + judged (14 ch): **mean 28.68**; article (engram) **34.2**, textbook
(cmos) 18.8; **abstract chapter 64.1**.

| object bucket | substr-scored | judge-scored |
| --- | --: | --: |
| objects | 0.464 | 0.473 |
| object_payload | 0.126 | **0.242** |
| object_evidence | 0.288 | 0.260 |
| relations | 0.117 | 0.112 |
| context_packages | 0.691 | 0.683 |

On fixed predictions the judge lifts object_payload 0.19 -> 0.43 (the composite
lift is smaller because payload is 0.13 weight and textbook atoms/chunks drag).
The article pipeline is strong end-to-end; the remaining ceiling is **textbook
atom over-extraction** (atoms 0.20 + chunks 0.15 = 0.35 weight, near-zero on cmos).

## Textbook atom ceiling — investigated, not cheaply fixable

The textbook composite (cmos 18.8) is dragged by atom over-extraction (atoms
0.20 + chunks 0.15 = 0.35 weight near-zero). Three curation approaches were
tried and ALL are net-negative:
1. deterministic selectivity (P0.5c) — cue heuristics over/under-match.
2. curate atoms to object **evidence** — LLM cites a different subset than gold.
3. explicit LLM **core_atom_ids** selection — atoms ticked up (ch01 0.16->0.19,
   count 136->48 vs gold 38) but **objects dropped** (0.59->0.44) and total fell.

Root cause (robust): the harness object alignment keys on `atom_p2g` (local-
evidence atom overlap). **Dropping any atoms perturbs that alignment**, so the
small atom-precision gain is outweighed by object-alignment loss — regardless of
how atoms are picked. The ceiling is the over-extraction at atomization time
(matching a human's "which 38 of 530 sentences matter"), an irreducible semantic-
curation gap without a much heavier (and uncertain) dedicated atomizer. All three
experiments were reverted; the pipeline stays at mean 28.68 (article 34.2).

## Real extraction quality (this is what actually improved)

- **objects 0.474** type-strict — per chapter: engram/ch00 **0.89**, ch01 0.64,
  ch03 0.62, ch06 0.61; cmos/ch01 0.65, ch02 0.49. Real typed objects matching
  the gold vocabulary, from nothing.
- **context_packages 0.685** — the LLM recovers most of each package's
  `expected_objects`.
- **relations 0.115** — endpoint+type matches; weak (LLM proposes plausible but
  off-vocabulary or wrong-endpoint edges); biggest object-side gap.
- **object_payload 0.141** — the LLM's payload field *values* rarely match gold
  values closely; the main quality lever to lift.
- **object_evidence 0.353** — capped by the atom-curation mismatch (our atoms
  differ from gold's curated atoms, so even matched objects cite different ids).

### Profile split
- Article (engram, n=9): mean total **33.4**; objects **0.525**, evidence 0.437, relations 0.157.
- Textbook (cmos, n=5): mean total **19.1**; objects 0.382, evidence 0.200, relations 0.040.
  Textbook is capped at every layer by atom over-extraction vs gold's heavy curation.

## Decisions for next round

1. **Harness artifact**: should `object_payload`/`object_evidence` score **0
   (not 1.0)** when gold has objects but the candidate matched none? If yes, the
   composite becomes a fair measure and P1 is a clear net gain. (User's harness.)
2. **Payload quality** (0.141): tune object prompts to fill gold-shaped field
   values (the cheapest real lever once the artifact is settled).
3. **Relations** (0.115): tighter prompt constraining endpoints to id pairs and
   relation types; possibly per-relation evidence.
4. **Textbook ceiling**: still atom-curation bound; the LLM object stage already
   curates, so consider deriving atoms FROM the objects' evidence on textbook.

## Speed
~1050s for the full run at 8 workers (~2.7s/call effective — below the ~8x
ideal, likely big-prompt latency on textbook packages + per-chapter sequential
relation calls). Raise `QIEFEN_LLM_WORKERS` and/or parallelize chapters to cut
this further.
