"""Answer-side glue for the ``read_document`` reflect action (PR-A T5).

``app.services.document_source_overview`` is the zero-LLM sampling executor; it
never knows an ``AskResponse`` exists.  ``app.services.reasoning_retrieval``
drives the reflect loop and collects one ``DocumentReadOutcome`` per sampled
document; it never renders prompt text either.  This module is the seam between
those two closed-out layers and the answer-synthesis prompt, exactly the way
``collection_enumeration_answer`` is the seam for typed-collection enumeration:

* ``DOCUMENT_READ_GUIDANCE`` -- the English, model-facing wrapper head that
  tells the writing model what these blocks are and how an "introduce each
  document" answer must be shaped.  It sits in the same position and plays the
  same role as ``document_catalog_overview._SUMMARY_GUIDANCE`` does for the
  deterministic catalog lane;
* ``document_read_prompt_block`` -- the bounded preview spliced into the
  synthesis evidence block, plus the reverse bindings and citations the
  response contract needs to turn a ``[kN]`` the model wrote back into an
  anchor and a citation card.

Both are pure functions over what the run already produced: no I/O, no model
calls, no mutation of the outcomes they read.

Two design points are load-bearing and easy to undo by accident:

* **The roster key, not a fresh handle.**  Each block's header is built with
  ``document_source_overview.supplemental_excerpt_header`` against the key the
  document's own row already occupies in the enumeration preview
  (``k5001`` etc.), so "the row you listed" and "the original text I sampled
  from it" are visibly the same document to the model.  When that reverse
  lookup fails -- the row was squeezed out of the preview by its budget, or the
  enumeration block was dropped altogether -- the header degrades to a
  title-only sentence.  It never invents a ``kN``: a handle the evidence map
  does not carry would bind to nothing and teach the model to cite a key that
  cannot be resolved.
* **Empty outcomes never reach here.**  A witness failure still produces a
  ``DocumentReadOutcome`` (with an empty ``context_block``) because the reflect
  loop's feedback ledger has to tell the model that document was attempted and
  failed.  That ledger is a retrieval-side concern; on the synthesis side an
  empty sample is nothing to read, so such outcomes contribute no header, no
  evidence and no citation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.services.document_source_overview import supplemental_excerpt_header


DOCUMENT_READ_GUIDANCE = (
    "[Bounded original-text samples read from the specific documents named "
    "below, each sampled passage carrying its own [kN] handle. Introduce each "
    "document in its own paragraph headed by that document's title, and write "
    "its purpose, method and contribution only from that document's own "
    "samples or its stored summary; when the supplied evidence does not "
    "establish one of them, leave it empty and say so explicitly. Name one by "
    "one every document in the directory listing that has neither a stored "
    "summary nor a sample in this turn as lacking evidence sufficient to "
    "introduce its body; never skip such a document and never infer its "
    "contents from its title. A complete directory listing is not a full "
    "reading: the sampling coverage of each block is stated by the coverage "
    "line that follows its header.]"
)


@dataclass(frozen=True)
class DocumentReadPreview:
    """What one run's document samples contribute to answer assembly.

    ``text`` is the block appended to the synthesis evidence; ``evidence_by_id``
    merges every sampled position's reverse binding (``kN`` -> element) so a
    cited handle resolves to an anchor; ``citations`` carries the executor's own
    ``Citation`` objects, which already hold the cross-notebook ``notebook_id``
    convention decided by ``prepare_source_overview``.
    """

    text: str = ""
    evidence_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    citations: list = field(default_factory=list)


def _roster_key(roster_map: Mapping[str, Mapping[str, Any]], source_id: str) -> str:
    """The enumeration-preview key this document's directory row occupies.

    Same predicate ``document_catalog_overview.supplement_missing_summaries``
    uses on the deterministic lane (``object_type == "source"`` plus a matching
    ``object_id``).  Returns ``""`` when the row is not in the preview, which is
    a normal outcome rather than an error: the enumeration block has its own
    character budget and can legitimately deliver fewer rows than the roster
    the reflect loop resolved the title against.
    """
    for key, entry in roster_map.items():
        if (entry.get("object_type") == "source"
                and str(entry.get("object_id") or "") == source_id):
            return key
    return ""


def document_read_prompt_block(
    reads: Sequence[object], *, roster_map: Mapping[str, Mapping[str, Any]],
) -> DocumentReadPreview:
    """Render this run's document samples for the synthesis evidence block.

    ``roster_map`` is the enumeration preview's ``evidence_by_id`` (the same
    dict answer assembly passes to ``_answer_reasoning`` as ``structured_map``),
    used only to look each document's directory row key back up.

    Every block is ``header + coverage_note + sampled lines``.  The coverage
    note rides *inside* the block rather than being summarised once at the end,
    because coverage is per document: one document may have been read whole
    while the next gave three positions out of forty, and a single trailing
    sentence would let the model apply the wrong one to either.
    """
    blocks: list[str] = []
    evidence_by_id: dict[str, dict[str, Any]] = {}
    citations: list = []
    for outcome in reads:
        context_block = str(getattr(outcome, "context_block", "") or "")
        if not context_block:
            continue
        title = str(getattr(outcome, "source_title", "") or "")
        key = _roster_key(roster_map, str(getattr(outcome, "source_id", "") or ""))
        # An empty ``key`` is a supported input, not a caller error: the renderer
        # degrades to the title-only sentence itself, so the escaping rule lives
        # in exactly one place (see that function's docstring).
        header = supplemental_excerpt_header(key, title)
        note = str(getattr(outcome, "coverage_note", "") or "")
        blocks.append(header + (f"{note}\n" if note else "") + context_block)
        evidence_by_id.update(getattr(outcome, "id_map", {}) or {})
        citations.extend(getattr(outcome, "citations", ()) or ())
    if not blocks:
        return DocumentReadPreview()
    return DocumentReadPreview(
        text=DOCUMENT_READ_GUIDANCE + "".join(blocks),
        evidence_by_id=evidence_by_id,
        citations=citations,
    )
