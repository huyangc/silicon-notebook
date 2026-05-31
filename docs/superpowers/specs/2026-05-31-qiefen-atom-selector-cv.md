# LLM atom selector — cross-validated evaluation (textbook→article parity goal)

- Date: 2026-05-31
- Goal: lift textbook (cmos) scores to article (engram) parity via an LLM atom selector.
- Method: general, profile-aware LLM atom selector (`atom_selector.py`, `QIEFEN_ATOM_SELECT`), prompt developed ONLY on validation chapters (cmos/ch02 + engram/ch02 gold), no document-specific rules. Applied select-first (before chunking) so objects build on the curated set.
- Protocol: held-out cross-validation. Validation = {cmos/ch02, engram/ch02} (prompt developed here). Test = the other 12 chapters (never inspected). Plus 10-fold 2-val:8-test rotation. Score: `scripts/qiefen_cv.py`.

## Result (held-out — clean negative)

| held-out | atoms | chunks | objects | TOTAL |
| --- | --: | --: | --: | --: |
| Textbook (4) OFF | 0.043 | 0.022 | 0.336 | 17.8 |
| Textbook (4) **ON** | 0.059 | 0.018 | 0.335 | **18.0** |
| Article (8) OFF | 0.316 | 0.108 | 0.510 | 34.2 |
| Article (8) **ON** | 0.297 | 0.042 | 0.533 | **31.9** |

10-fold (2:8) held-out composite: OFF **27.1 ± 2.8** → ON **26.5 ± 2.5** (selector slightly worse, stable across folds).

Per held-out textbook chapter (atoms; total): ch01 0.16→0.22 (26.9→28.4), ch03 0.00→0.01 (14.2→13.7), ch04 0.00→0.00 (15.0→14.3), ch09 0.00→0.01 (15.1→15.7).

## Conclusion

The LLM atom selector does NOT close the textbook gap and slightly regresses overall. The CV (held-out, never tuned on test) rules out overfitting as the cause. Structural reasons:

1. **Span + type alignment, not count.** Selecting the right *sentences* still doesn't IoU-match gold's specific curated atom spans/types on the big chapters → textbook atoms stay ~0.
2. **Object-coverage tradeoff.** Dropping atoms starves object evidence; objects held flat only because the selector was mild — more aggressive selection drops objects.
3. **Article was already optimal at keep-all** (papers are lightly curated in gold), so a general selector can only hurt it (atoms 0.316→0.297, chunks halved).

**Textbook→article parity is not reachable via atom-level LLM selection** under this harness (span-IoU + type + object-alignment scoring of heavily-curated textbook gold). The selector is kept opt-in, default off (production unaffected).

## What this implies for the goal

The textbook deficit is dominated by `evidence_atoms` (0.20 weight) + `semantic_chunks` (0.15) being near-zero because gold curates ~5–10% of sentences and our atomizer is exhaustive. Closing it would require either (a) matching gold's sparse hand-curation at span+type level without starving objects — not achieved by 4 methods tried (deterministic cues, object-evidence curation, post-hoc core-selection, select-first LLM), or (b) reconsidering whether exhaustive-atomize + score-against-curated-gold is the right target for textbook (e.g., score textbook on object/relation quality, where cmos already reaches objects ~0.34–0.45, closer to article's ~0.51).
