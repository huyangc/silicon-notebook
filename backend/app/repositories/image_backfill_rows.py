"""`backfill-images` 读回的行的后端中性投影。

两个适配器把同样的列读回来（SQLite 的 ``metadata`` 是 TEXT JSON、``element_ids``
是 TEXT JSON；PostgreSQL 两者都是 jsonb，驱动直接还成 dict/list），所以"把行折成
离线阶段要的形状"这一半只写一份——放在这里而不是任一侧，因为适配器之间绝不
互相 import（与相邻的 ``chunk_elements`` 同一条理由）。
"""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from app.repositories.chunk_elements import decode_element_ids


def decode_metadata(value: Any) -> dict:
    """一行 ``source_elements.metadata`` 的 dict 形态。

    畸形载荷降级成空 dict 而不抛：一条历史坏行不该掀翻整个 notebook 的回填，
    而空 metadata 只会让这条元素被当成"没有 asset_id 的图片"，也就是回填照常
    对它一视同仁。"""
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value or "{}")
        except (ValueError, TypeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def image_backfill_state(
    element_rows: Iterable[Mapping[str, Any]],
    chunk_rows: Iterable[Mapping[str, Any]],
) -> dict:
    """``{elements, chunks, element_created_at}``。

    ``element_created_at`` 取既有元素批次里最大的那个原样值（SQLite 是 ISO 文本、
    PostgreSQL 是 ``datetime``），调用方只把它当不透明句柄传回写入口——回填出来
    的图片元素必须与既有批次共享这一刻，详情分页的 ``(created_at,id)`` 排序与
    命令目录的 ``source_element_generation`` 才不会漂。"""
    elements: list[dict] = []
    newest: Any = None
    for row in element_rows:
        created_at = row["created_at"]
        if newest is None or (created_at is not None and created_at > newest):
            newest = created_at
        elements.append(
            {
                "id": row["id"],
                "element_type": row["element_type"] or "",
                "text": row["text"] or "",
                "metadata": decode_metadata(row["metadata"]),
            }
        )
    chunks = [
        {"id": row["id"], "element_ids": decode_element_ids(row["element_ids"])}
        for row in chunk_rows
    ]
    return {
        "elements": elements,
        "chunks": chunks,
        "element_created_at": newest,
    }
