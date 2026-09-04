"""Compatibility re-export shim (definitions sunk to app.domain in B3).

The vector codec/matrix helpers now live in ``app.domain.vector_index`` (pure
functions, zero ``app.services``/``app.repositories`` dependency, so
``app.repositories`` adapters can import them directly). This module
re-exports every name unchanged so existing importers keep resolving to the
SAME objects without any call-site changes.
"""
from __future__ import annotations

from app.domain.vector_index import (
    build_matrix,
    decode_vector,
    encode_vector,
    matrix_pages,
    query_sims,
    resolve_runtime_dim,
    top_k_sims,
    truncate_vec,
)

__all__ = [
    "build_matrix",
    "decode_vector",
    "encode_vector",
    "matrix_pages",
    "query_sims",
    "resolve_runtime_dim",
    "top_k_sims",
    "truncate_vec",
]
