"""J2 outside a global run: no card for a dangling element.

Plan ruling J2: an element id that no longer has a ``source_elements`` row --
a KG occurrence left behind by a re-ingest that produced fewer elements, a
row-level Knowhow deletion -- produces no citation card, identically in
single-notebook and global runs. In a global run the evidence producers learn
it from ``evidence_attestation.attest_pointers`` at retrieval time; outside
one that seam is a no-op by J8 (a single-notebook answer has no terminal
check), so the single-notebook half lives here and runs at the two points
where references become final:

* ``drop_dangling_references`` -- called from ``AskService._save_answer``, the
  one exit every single-notebook answer takes (chunk including its mix
  branch's KG evidence, reasoning, catalog overview, plugin engine);
* ``prune_dead_report_elements`` -- called from ``ReportEngine.
  run_final_audit_stage`` (through ``_sections_without_dead_elements``) after
  every section is drafted and right before ``_assemble`` turns the sections'
  cited keys into the report's stored reference list.

Each costs ONE batched primary-key read over the distinct element ids the
answer's references (or the report's cited section keys) name, and none when
they name no element. Both callers skip it while a global run plan is
installed: there an element deleted DURING the run must reach the terminal
check as ``source_gone`` instead of vanishing here.

What happens to a reference whose element is gone:

* a ``Citation`` -- its card IS the element's excerpt -- is dropped;
* an ``AnswerAnchor`` (or report context) whose subject is the element itself
  (``object_type == "element"``) is dropped, and its key is removed from the
  ``[k]`` markers of the text, so the page never shows a bare marker bound to
  nothing;
* any other anchor or report context (a KG object, a chunk, a relation) keeps
  its card -- its subject is still there -- and loses only the dead locator
  (``element_id``), becoming a source-level reference (J3), which is what the
  global producers do for the same objects.

No verification marker and no notice (J8). A failed read keeps every
reference exactly as it was (fail-open: this only removes cards that would
open on nothing) and emits ONE content-free ``reference_liveness_read_failed``
event (surface, exception class name, element count) so a broken adapter is
visible; cancellation and participant-attestation failures propagate. When
nothing is dead the input is untouched, byte for byte.
"""
from __future__ import annotations

import re
from typing import Any, Callable, List, Mapping, Sequence

from app.domain.retrieval_control import RetrievalControlError
from app.services.cancellation import AskCancelled
from app.services.citation_markers import LOOSE_MARKER_RE, marker_keys

_MARKER_WITH_LEADING_SPACE = re.compile(r"(?P<space>[ \t]*)(?P<marker>" + LOOSE_MARKER_RE.pattern + r")")

ReadFingerprints = Callable[[Sequence[str]], Mapping[str, Any]]
Emit = Callable[[dict], None]


def _live_ids(element_ids: list, read: ReadFingerprints, emit: Emit | None,
              surface: str):
    """The live subset of ``element_ids`` (one read), or None when it failed."""
    try:
        return read(element_ids)
    except (AskCancelled, RetrievalControlError):
        raise
    except Exception as exc:  # noqa: BLE001 - fail-open: keep every card as it was
        if emit is not None:
            try:
                emit({
                    "kind": "reference_liveness_read_failed",
                    "surface": surface,
                    "error_type": type(exc).__name__,
                    "elements": len(element_ids),
                })
            except Exception:  # noqa: BLE001 - telemetry never breaks the answer
                pass
        return None


def drop_dangling_references(
    response: Any, read_fingerprints: ReadFingerprints, *, emit: Emit | None = None,
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
    live = _live_ids(element_ids, read_fingerprints, emit, "answer")
    if live is None:
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


def _cited_keys(markdown: str) -> set:
    return {
        key for match in LOOSE_MARKER_RE.finditer(markdown or "")
        for key in marker_keys(match.group(0))
    }


def prune_dead_report_elements(
    sections: Sequence[Mapping[str, Any]], read_fingerprints: ReadFingerprints,
    *, emit: Emit | None = None,
) -> List[Mapping[str, Any]]:
    """The report's sections without cards on elements already gone.

    Only the contexts a section's markdown actually cites are looked at (the
    rest never become references). ONE read for the whole report, only when a
    cited context names an element. A section with a dead element is returned
    as a COPY -- its ``id_map`` without the dead element contexts (and dead
    locators cleared on the others), its ``markdown`` without their marker
    keys -- so the section dicts it was given are never rewritten. Every other
    section is returned as is (the same object).
    """
    cited = [
        (section, _cited_keys(str(section.get("markdown") or "")))
        for section in sections
    ]
    element_ids = list(dict.fromkeys(
        str(ctx.get("element_id") or "")
        for section, keys in cited
        for key, ctx in (section.get("id_map") or {}).items()
        if key in keys and isinstance(ctx, Mapping) and ctx.get("element_id")
    ))
    if not element_ids:
        return list(sections)
    live = _live_ids(element_ids, read_fingerprints, emit, "report")
    if live is None:
        return list(sections)
    dead = {element_id for element_id in element_ids if element_id not in live}
    if not dead:
        return list(sections)
    pruned: List[Mapping[str, Any]] = []
    for section, keys in cited:
        id_map = section.get("id_map") or {}
        touched = {
            key for key in keys
            if isinstance(id_map.get(key), Mapping)
            and str(id_map[key].get("element_id") or "") in dead
        }
        if not touched:
            pruned.append(section)
            continue
        new_map = dict(id_map)
        dropped: set = set()
        for key in touched:
            if str(id_map[key].get("object_type") or "") == "element":
                dropped.add(key)
                del new_map[key]
            else:
                new_map[key] = {**id_map[key], "element_id": ""}
        copy = {**section, "id_map": new_map}
        if dropped:
            copy["markdown"] = strip_marker_keys(str(section.get("markdown") or ""), dropped)
        pruned.append(copy)
    return pruned


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
