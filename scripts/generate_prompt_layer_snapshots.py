#!/usr/bin/env python3
"""Generate the prompt-layering byte-equivalence snapshot fixtures.

This is the baseline for the L0/L1/L2 prompt-layering refactor
(`backend/app/services/prompt_layers.py`): it imports the CURRENT
`backend/app/services/prompts.py` (before that refactor touches it) and
renders a fixed set of representative calls to every public prompt-building
function, writing each rendering verbatim to
`backend/tests/fixtures/prompt_layer_snapshots/<case>.txt`.

`backend/tests/test_prompt_layers.py` re-renders the SAME calls against the
post-refactor `prompts.py` and asserts byte-for-byte equality against these
files — that is the "对账" (reconciliation) step; this script only generates.

IMPORTANT — generate-once contract: these fixtures must be produced BEFORE
the prompt-layering refactor lands, and never regenerated from the
refactored code. Regenerating afterwards would make the byte-equivalence
test compare the refactored output against itself, turning the acceptance
criterion into a tautology. Treat the files under
`backend/tests/fixtures/prompt_layer_snapshots/` as frozen once this script
has been run against pre-refactor `prompts.py`.

Baseline: generated against `backend/app/services/prompts.py` as it stood at
commit `319f7aad` (before any L0/L1/L2 splicing), with zero manual edits
after generation. See `backend/tests/fixtures/prompt_layer_snapshots/
README.md` for the on-disk contract (byte-exact, no trailing newline beyond
what each prompt already produces, how to re-verify).

RETIREMENT — this snapshot net is refactor-scoped, not permanent. Once the
L0/L1/L2 layering refactor this baseline exists to gate has landed and
stabilized, its job is done: mirror the precedent already recorded in
`docs/development.md` for `ReasoningRetriever.run` — "the refactor-only
byte-for-byte golden snapshot has been retired" — and retire this one the
same way once it has served its purpose. Concretely: the NEXT time someone
makes a deliberate change to any prompt's rendered text (a new/edited L1
fragment default, a new L0 rule, a schema hint change, …), that same diff
must either (a) regenerate every affected fixture here and say so in the
PR description, or (b) retire this fixture set outright (delete the
directory and `test_prompt_layers.py`'s snapshot section, or replace it with
whatever coverage the change's own tests provide) and say so in the PR
description. An untouched, silently-stale fixture next to a real behavior
change is worse than either choice.

Cases cover (see the task's own enumeration for the authoritative list):
  * every call shape actually touched by the L1 fragment extraction
    (answer_prompt sectioning/history/style, expand_query_prompt's
    decomposition guidance, query_intent_prompt's cross-tool mapping,
    report_storm_outline_prompt's lenses/frame example,
    report_section_prompt's domain conventions, plan_prompt's backup
    spelling);
  * every other public prompt-building function in `prompts.py`, at one
    minimal call each, purely as a tripwire against incidental changes —
    this refactor does not touch their text.

Usage:
    python3 scripts/generate_prompt_layer_snapshots.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable, Dict

ROOT = Path(__file__).resolve().parent.parent
BACKEND_ROOT = ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import prompts  # noqa: E402

FIXTURES_DIR = ROOT / "backend" / "tests" / "fixtures" / "prompt_layer_snapshots"


def _cases() -> Dict[str, Callable[[], str]]:
    return {
        # ------------------------------------------------------------------
        # Cases exercising the 9 fragments extracted into prompt_layers.py.
        # ------------------------------------------------------------------
        "answer_default": lambda: prompts.answer_prompt(
            "q?", "k1: [concept] X — ctx",
        ),
        "answer_history": lambda: prompts.answer_prompt(
            "q?", "k1: [concept] X — ctx",
            history_block="User: prev\nAssistant: ans",
        ),
        "answer_sectioned_style": lambda: prompts.answer_prompt(
            "q?", "k1: [concept] X — ctx",
            sectioned=True, section_title="T", section_index=2,
            section_total=5, style_block="[style] concise",
        ),
        "expand_basic": lambda: prompts.expand_query_prompt(
            "compare A and B", corpus_langs=["zh", "en"],
        ),
        "expand_full": lambda: prompts.expand_query_prompt(
            "compare A and B",
            want_types=True,
            corpus_langs=["en"],
            history_block="[History] User: prev turn\nAssistant: prev answer",
            collection_map="[Collections in scope] concept=3, claim=2",
            profile_block="[Profile] This library covers RF amplifiers.",
            experience_block="[Experience] ppr_retrieve pays off for comparisons.",
            style_block="[style] concise",
        ),
        "plan_backup": lambda: prompts.plan_prompt(
            "q?", collection_map="[Collections] c=1", profile_block="[P]",
            experience_block="[E]", style_block="[S]",
        ),
        "reflect_min": lambda: prompts.reflect_prompt("q?", "(none)"),
        "reflect_full": lambda: prompts.reflect_prompt(
            "q?", "(none)",
            element_kinds=("formula", "table"),
            object_types=("concept", "claim"),
            outline=True, consult_memory=True,
        ),
        "intent_default": lambda: prompts.query_intent_prompt(
            "how do I do Innovus's place_opt_design in ICC2?",
        ),
        "intent_confirm": lambda: prompts.query_intent_prompt(
            "q?", confirmation_mode=True, history_block="prior",
        ),
        "outline_plain": lambda: prompts.report_outline_prompt("q?"),
        "outline_storm": lambda: prompts.report_storm_outline_prompt(
            "q?", corpus_map="CM", intent_block="IB", coverage_block="CB",
        ),
        "section_parametric": lambda: prompts.report_section_prompt(
            "T", "S", "q?", "k1: x",
            allow_parametric=True, discovered_structure="DS",
            assumptions="AS", report_frame="RF", synthesis_commitment="SC",
        ),
        "section_strict": lambda: prompts.report_section_prompt(
            "T", "S", "q?", "k1: x", allow_parametric=False,
        ),
        "sufficiency": lambda: prompts.report_sufficiency_prompt(
            "q?", "probe", result_scope="complete", completeness_required=True,
        ),
        "synthesis": lambda: prompts.report_synthesis_prompt(
            "q?", "IB", "FB", "EV", facet_ids=("f1", "f2"),
        ),
        "summary": lambda: prompts.report_summary_prompt(
            "q?", "SB", intent_block="IB",
        ),
        "followup": lambda: prompts.followup_rewrite_prompt("H", "q?"),
        "evidence_refine": lambda: prompts.evidence_refine_prompt("q?", "items"),
        # ------------------------------------------------------------------
        # Remaining public prompt functions: one minimal case each, frozen
        # only as a tripwire — this refactor does not touch their text.
        # ------------------------------------------------------------------
        "memory_preview": lambda: prompts.memory_preview_prompt("q?", "a."),
        "concept_description": lambda: prompts.concept_description_prompt(
            "concept name", "evidence block",
        ),
        "notebook_description": lambda: prompts.notebook_description_prompt(
            "sources block",
        ),
        "notebook_meta": lambda: prompts.notebook_meta_prompt("sources block"),
        "refine": lambda: prompts.refine_prompt("§1", "records", "elements"),
        "gleaning": lambda: prompts.gleaning_prompt("§1", "textbook"),
        "schema_induction": lambda: prompts.schema_induction_prompt(
            ["concept", "claim"], "sample block",
        ),
        "community_report": lambda: prompts.community_report_prompt(
            "members", "relations",
        ),
        "agent_profile_base": lambda: prompts.agent_profile_base_prompt(
            "corpus block", "current block", value_max_chars=80,
        ),
        "agent_profile_overlay_no_obs": lambda: prompts.agent_profile_overlay_prompt(
            "usage block", "current block", value_max_chars=80,
            has_observations=False,
        ),
        "agent_profile_overlay_has_obs": lambda: prompts.agent_profile_overlay_prompt(
            "usage block", "current block", value_max_chars=80,
            has_observations=True,
        ),
        "retrieval_experience": lambda: prompts.retrieval_experience_prompt(
            "observation block", "existing block",
            actions=("ppr_retrieve", "exact_lookup"), rationale_max_chars=80,
        ),
    }


def main() -> int:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    cases = _cases()
    for name, render in cases.items():
        text = render()
        path = FIXTURES_DIR / f"{name}.txt"
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} ({len(text)} chars)")
    print(f"generated {len(cases)} prompt layer snapshots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
