"""J2 for single-notebook reasoning answers: no card for a dangling element.

Plan ruling J2: an element id that no longer has a ``source_elements`` row --
a KG occurrence left behind by a re-ingest that produced fewer elements, a
row-level Knowhow deletion -- produces no citation card, identically in
single-notebook and global runs. In a global run the evidence producers learn
it from ``evidence_attestation.attest_pointers`` at retrieval time; outside
one that seam is a no-op by J8 (a single-notebook answer has no terminal
check), so the single-notebook half lives here, at the one point where a
reasoning answer's references become final (``AskService.
_commit_reasoning_draft``), and costs ONE batched primary-key read over the
distinct element ids its citations and anchors name.

What happens to a reference whose element is gone:

* a ``Citation`` -- its card IS the element's excerpt -- is dropped;
* an ``AnswerAnchor`` whose subject is the element itself (``object_type ==
  "element"``) is dropped, and its key is removed from the ``[k]`` markers in
  the answer text, so the page never shows a bare marker bound to nothing;
* any other anchor (a KG object, a chunk, a relation) keeps its card -- its
  subject is still there -- and loses only the dead locator (``element_id``),
  becoming a source-level reference (J3), which is what the global producers
  do for the same objects.

No verification marker and no notice (J8). A failed read keeps every
reference exactly as it was (fail-open: this only removes cards that would
open on nothing); cancellation and participant-attestation failures
propagate. When nothing is dead the response is untouched, byte for byte.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

from app.domain.retrieval_control import RetrievalControlError
from app.services.cancellation import AskCancelled
from app.services.citation_markers import LOOSE_MARKER_RE, marker_keys

_MARKER_WITH_LEADING_SPACE = re.compile(r"(?P<space>[ \t]*)(?P<marker>" + LOOSE_MARKER_RE.pattern + r")")


def drop_dangling_references(
    response: Any, read_fingerprints: Callable[[Sequence[str]], Mapping[str, Any]],
) -> None:
    """Remove the references of ``response`` whose element no longer exists.

    ``read_fingerprints`` is the by-id ``evidence_fingerprints`` read (an
    element with no row is ABSENT from its answer). One call, only when at
    least one reference names an element.
    """
    references = [*(response.citations or ()), *(response.anchors or ())]
    element_ids = list(dict.fromkeys(
        reference.element_id for reference in references if reference.element_id
    ))
    if not element_ids:
        return
    try:
        live = read_fingerprints(element_ids)
    except (AskCancelled, RetrievalControlError):
        raise
    except Exception:  # noqa: BLE001 - fail-open: keep every card as it was
        return
    dead = {element_id for element_id in element_ids if element_id not in live}
    if not dead:
        return
    response.citations = [
        citation for citation in response.citations if citation.element_id not in dead
    ]
    dropped_keys: set = set()
    kept = []
    for anchor in response.anchors:
        if anchor.element_id not in dead:
            kept.append(anchor)
        elif anchor.object_type == "element":
            dropped_keys.add(anchor.key)
        else:
            kept.append(anchor.model_copy(update={"element_id": ""}))
    response.anchors = kept
    if dropped_keys:
        response.answer = strip_marker_keys(response.answer, dropped_keys)
        response.conclusion = strip_marker_keys(response.conclusion, dropped_keys)


def strip_marker_keys(text: str, keys: set) -> str:
    """Remove ``keys`` from every ``[k…]`` marker group in ``text``.

    A group keeps its other keys (``[k1, k2]`` minus ``k2`` is ``[k1]``); a
    group left empty disappears together with the whitespace before it. Every
    marker that names none of ``keys`` is kept byte for byte.
    """
    def _sub(match: re.Match) -> str:
        names = marker_keys(match.group("marker"))
        remaining = [name for name in names if name not in keys]
        if len(remaining) == len(names):
            return match.group(0)
        if not remaining:
            return ""
        return match.group("space") + "[" + ", ".join(remaining) + "]"

    return _MARKER_WITH_LEADING_SPACE.sub(_sub, text or "")
