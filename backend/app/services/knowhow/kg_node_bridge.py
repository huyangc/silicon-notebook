"""Gate-0 sidecar for knowhow KG-node retrieval (default-off feature —
``Settings.knowhow_kg_node_retrieval_enabled``): the PURE reduction that turns
a *chunk-reverse-lookup* into ``{ko_id: sim}`` so a knowhow cell KO
(``object_type = 列名``, id ``ko-kh-{sha1(table_id|column_name|value_key)[:32]}``)
can pick up a SEMANTIC score in ``_retrieve_scored`` even though the
structural-only projection (see ``projection.py``) deliberately never writes a
``knowledge_embeddings`` / ``kg_objects_fts`` row for it.

The whole design turns on one asymmetry: knowhow cells DO carry vectors — just
on their per-cell CHUNK rows (``chunk_embeddings``), not on their KO payloads.
So the only vectors that can speak for a cell KO are its chunk vectors. The
CALLER (``CandidateRetrievalService._knowhow_ko_candidates``) does every DB
read through its stores — the notebook's hidden-knowhow chunk rows, their
vectors, and the cell elements — and hands the already-fetched data here; this
module is a side-effect-free transform (no SQL, no stores) so it stays trivially
unit-testable and keeps the "no raw SQL in a service" boundary intact.

Chunk → KO id reconstruction mirrors ``KnowhowProjector`` exactly so the id this
produces is byte-identical to the one the projector wrote:
  * chunk → ``element_ids[0]`` → the cell element's ``text`` + its
    ``metadata.knowhow.{table_id, column_name, role}``;
  * ``role == "entity"`` cells were split by the projector into one KO per
    ``textops.split_tools`` item, so we split the same way and emit one id per
    item (every other role is one KO for the whole cell's net text);
  * ``ko_id = _cell_ko_id(table_id, column_name, textops.value_key(value))``.
The element's stored ``text`` is the same net cell text the projector hashed
(``_write_elements`` and ``_merge_cell`` both key off it), so ``value_key``
reproduces the projector's merge identity; a cell split across multiple chunks
(>4000 chars) collapses back onto one KO id via the ``max`` fold.

Returns ``{}`` (never raises for an absent feature) on: no query vector, no
knowhow chunk vectors, or a zero-similarity/dim-mismatch result — the caller
treats ``{}`` as "inject nothing", so a notebook with no knowhow content is a
pure no-op.
"""
from __future__ import annotations

import json
from typing import Dict, Mapping, Sequence

from app.services.knowhow import textops
from app.services.knowhow.projection import _cell_ko_id
from app.services.vector_index import build_matrix, query_sims


def knowhow_ko_candidates(
    chunk_to_element: Mapping[str, str],
    chunk_vector_rows: Sequence,
    elements: Mapping[str, Mapping],
    query_vector,
    *,
    recall: int,
) -> Dict[str, float]:
    """``{ko_id: max cosine sim}`` for knowhow cell KOs whose chunk vectors are
    similar to ``query_vector``.

    Inputs (all pre-fetched by the caller through its stores):
      * ``chunk_to_element`` — ``{chunk_id: element_id}`` (the cell element the
        chunk belongs to; ``element_ids[0]``);
      * ``chunk_vector_rows`` — rows with ``["vid"]`` (chunk id) + ``["vector"]``
        (BLOB) for those chunks' ``chunk_embeddings``;
      * ``elements`` — ``{element_id: {"text": ..., "metadata": <json str>}}``
        for the cell elements.

    Cosine sims are already in the retrieval [0,1]/tau space (query + corpus both
    L2-normalized by ``build_matrix``/``query_sims``, same runtime-truncation
    reconciliation as every other chunk path). Empty dict when there is nothing
    to contribute."""
    if query_vector is None:
        return {}
    ids, matrix = build_matrix((r["vid"], r["vector"]) for r in chunk_vector_rows)
    if not ids:
        return {}
    sims = query_sims(query_vector, ids, matrix)  # {chunk_id: cosine}
    if not sims:
        return {}
    # Bound to the top-``recall`` chunks by similarity (knowhow is tiny, so this
    # is usually all of them — the cap only bites if a future table grows large).
    ranked = sorted(sims.items(), key=lambda kv: kv[1], reverse=True)[: max(recall, 1)]

    out: Dict[str, float] = {}
    for chunk_id, sim in ranked:
        element_id = chunk_to_element.get(chunk_id)
        element = elements.get(element_id) if element_id else None
        if element is None:
            continue
        meta = json.loads(element.get("metadata") or "{}") or {}
        knowhow = meta.get("knowhow") or {}
        table_id = knowhow.get("table_id")
        column_name = knowhow.get("column_name")
        if not table_id or not column_name:
            continue
        text = element.get("text") or ""
        # Mirror the projector's per-cell KO split: entity cells fan out into one
        # KO per list item, every other role is one KO for the whole cell.
        values = textops.split_tools(text) if knowhow.get("role") == "entity" else [text]
        for value in values:
            value_key = textops.value_key(value)
            if not value_key:
                continue
            ko_id = _cell_ko_id(table_id, column_name, value_key)
            if sim > out.get(ko_id, -1.0):
                out[ko_id] = sim
    return out


__all__ = ["knowhow_ko_candidates"]
