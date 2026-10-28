"""Pure pieces of the concept-description input and of its explicit rewrite.

A concept's fused description is generated from two inputs only: the
canonical name and the quote set of its members' evidence (the sorted,
de-duplicated first eight ``quoted_span`` values). ``evidence_quotes`` and
``description_quotes`` are that normalization as the ``kg rewrite-descriptions``
maintenance command computes it: the same steps, in the same order, as the
unified rebuild's description stage (``_concept_description_stage``, which
keeps its own inline loop on that hot path). The command's tests pin that a
rewrite leaves every canonical with the signature the rebuild stored and that
the next rebuild reuses the rewritten text, so the two cannot drift apart
silently.

The rewrite command stages what it generated in ``kg_rebuild_checkpoint``
under its own stage, ``CONCEPT_DESC_REWRITE_STAGE``, with the generation origin
as the version: ``rewrite_checkpoint_version`` of the description model label
and a fingerprint of the description prompt. A staged row says "for input
signature S (the item key), the model and prompt of this origin produced the
text in the payload". The rebuild's own checkpoint GC and ``--fresh`` clear
leave this stage alone (the rebuild never reads it); deleting a notebook or
its knowledge graph removes it with every other checkpoint row.

Pure: only ``hashlib`` and ``json``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable, List

#: The ``kg_rebuild_checkpoint.stage`` of the rewrite command's staged texts.
CONCEPT_DESC_REWRITE_STAGE = "concept_desc_rewrite"
#: The rebuild description stage's own checkpoint (keyed by canonical id, with
#: the text and its signature). The rebuild reuses it BEFORE the published
#: descriptions, so a rewrite that publishes drops it: otherwise the next
#: rebuild whose input version did not move would put the old texts back.
REBUILD_DESC_CHECKPOINT_STAGE = "concept_desc"
#: How many quotes (after sort + de-dup) a description is generated from.
CONCEPT_DESC_QUOTE_LIMIT = 8
#: Bumped only if the meaning of a staged row changes; part of every version.
_ORIGIN_SCHEME = "1"
_VERSION_PREFIX = "descrw-"


def evidence_quotes(evidence_text: "str | None") -> List[str]:
    """The non-empty, stripped ``quoted_span`` values of one evidence JSON
    array, in order. Exactly the rebuild description stage's per-row read."""
    quotes: List[str] = []
    for item in json.loads(evidence_text or "[]"):
        quote = (item.get("quoted_span") or "").strip()
        if quote:
            quotes.append(quote)
    return quotes


def description_quotes(spans: Iterable[str]) -> List[str]:
    """The quote set a description is generated from: sorted, de-duplicated,
    at most ``CONCEPT_DESC_QUOTE_LIMIT``. Deterministic whatever the row order."""
    return sorted(set(spans))[:CONCEPT_DESC_QUOTE_LIMIT]


def prompt_fingerprint(prompt_text: str, schema_hint: str) -> str:
    """Fingerprint of the description prompt template: the caller renders the
    template with fixed placeholder inputs, so any edit of the wording (or of
    the schema hint) changes it, and nothing else does."""
    payload = f"{prompt_text}\x00{schema_hint}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rewrite_checkpoint_version(model_label: str, prompt_print: str) -> str:
    """The staged rows' ``input_version``: the origin of a staged text -- the
    description model label and the prompt fingerprint. Another model, or an
    edited prompt, is another version, so nothing staged under the old one
    counts as already rewritten."""
    payload = f"{_ORIGIN_SCHEME}\x00{model_label}\x00{prompt_print}"
    return _VERSION_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
