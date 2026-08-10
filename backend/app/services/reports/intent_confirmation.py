"""Deterministic freeze of one reviewed Deep Report intent contract.

Confirmation is a commit, never a second interpretation pass: the contract that
reached ``intent_ready`` is overlaid with the visible wording and the
clarification answers, and with nothing else.  No model is called here.

There are two callers and there must never be a third implementation:

* the owner-facing ``POST .../reports/{id}/intent`` endpoint, and
* the direct-run path, which confirms a *clear* intent on the owner's behalf
  when the report was created with ``auto_generate=true``.

Both reach ``intent_ready → planning`` through this one function, so an
auto-confirmed report claims byte-for-byte the contract a manual confirmation
would have claimed.  The atomic claim itself stays with each caller: the
endpoint turns a lost claim into 409, while the automatic path fails open and
leaves the report for the owner to confirm by hand.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping


# User-facing copy stays here so both callers reject an attempt with the same
# words; the HTTP layer is still the only place allowed to ship it (via
# ``user_error``), and the automatic path never shows it at all.
MISSING_ANSWERS_MESSAGE = "请先回答所有必填澄清问题"
EMPTY_QUESTION_MESSAGE = "确认后的研究问题不能为空"


class ReportIntentConfirmationError(ValueError):
    """A confirmation attempt the server refuses deterministically."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def confirmed_understanding(
    understanding: Mapping[str, Any],
    *,
    resolved_question: str,
    answers: Iterable[Mapping[str, Any]] = (),
) -> dict:
    """Return the reviewed contract staged for the ``intent_ready`` claim.

    ``answers`` rows are ``{"id", "answer"}`` mappings; unknown ids and blank
    answers are dropped, and every ambiguity that is not explicitly optional
    must still be answered afterwards.
    """
    ambiguities = {
        str(row.get("id") or ""): row
        for row in (understanding.get("ambiguities") or [])
        if isinstance(row, dict) and row.get("id")
    }
    submitted: dict[str, str] = {}
    for row in answers:
        if not isinstance(row, Mapping):
            continue
        ambiguity_id = str(row.get("id") or "").strip()
        answer = str(row.get("answer") or "").strip()
        if ambiguity_id and answer and ambiguity_id in ambiguities:
            submitted[ambiguity_id] = answer
    if any(
        row.get("required") is not False and not submitted.get(ambiguity_id)
        for ambiguity_id, row in ambiguities.items()
    ):
        raise ReportIntentConfirmationError(MISSING_ANSWERS_MESSAGE)
    question = str(resolved_question or "").strip()
    if not question:
        raise ReportIntentConfirmationError(EMPTY_QUESTION_MESSAGE)
    frozen = dict(understanding)
    frozen["confirmed_input"] = {
        "resolved_question": question,
        "answers": [
            {
                "id": ambiguity_id,
                "question": str(ambiguities[ambiguity_id].get("question") or ""),
                "answer": answer,
            }
            for ambiguity_id, answer in submitted.items()
        ],
    }
    frozen["confirmed"] = True
    return frozen


__all__ = [
    "EMPTY_QUESTION_MESSAGE",
    "MISSING_ANSWERS_MESSAGE",
    "ReportIntentConfirmationError",
    "confirmed_understanding",
]
