"""Backend-neutral deterministic identifiers for Knowhow projections."""
from __future__ import annotations

import hashlib


def _h(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _cell_ko_id(table_id: str, column_name: str, val_key: str) -> str:
    return f"ko-kh-{_h(table_id, column_name, val_key)[:32]}"


def element_id(row_id: str, column_id: str) -> str:
    return f"el-kh-{_h(row_id, column_id)[:32]}"


def _chunk_row_hash(row_id: str) -> str:
    return _h(row_id)[:16]


def cell_chunk_id(row_id: str, part: int) -> str:
    return f"chunk-kh-{_chunk_row_hash(row_id)}-{part}"


def _relation_id(source_object_id: str, edge_type: str, target_object_id: str) -> str:
    return f"kr-kh-{_h(source_object_id, edge_type, target_object_id)[:32]}"
