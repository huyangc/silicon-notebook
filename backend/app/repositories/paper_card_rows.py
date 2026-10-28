"""``source_paper_cards`` 读回的行的后端中性投影。

两个适配器读回同样的列（SQLite 的 ``card`` / ``raw_json`` 是 TEXT JSON、时间是 ISO
文本；PostgreSQL 是 jsonb 与带时区的 ``datetime``），所以"折成调用方要的形状"与
"时间渲染成 UTC ISO"只写一份，放在这里而不是任一侧——适配器之间绝不互相 import。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping


def utc_iso(value: Any) -> str:
    """把一个时刻渲染成 UTC ISO 字符串；空值给 ``""``。

    ``datetime`` 带时区就换算成 UTC；朴素的 ``datetime`` / ISO 文本按系统本地时间解释
    （与 ``local_datetime`` 同一约定）。结果与 PostgreSQL 会话时区无关，所以 PostgreSQL
    上的卡片 ``basis`` 在会话时区变化时不会让全部卡片失效。SQLite 的 ``basis`` 不走这里
    （它用 ``MAX(created_at)`` 的原文本）；这里只渲染行里的 ``created_at`` / ``updated_at``。
    """
    if value is None or value == "":
        return ""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return parsed.astimezone(timezone.utc).isoformat()


def _json_object(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value or "{}")
        except (ValueError, TypeError):
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def paper_card_row(row: Mapping[str, Any]) -> dict:
    """一行 ``source_paper_cards`` 的 dict 形态（JSON 列已解码，时间为 UTC ISO）。"""
    return {
        "source_id": row["source_id"],
        "notebook_id": row["notebook_id"],
        "basis": row["basis"],
        "card_version": int(row["card_version"]),
        "status": row["status"],
        "card": _json_object(row["card"]),
        "raw_json": _json_object(row["raw_json"]),
        "model": row["model"] or "",
        "attempts": int(row["attempts"]),
        "created_at": utc_iso(row["created_at"]),
        "updated_at": utc_iso(row["updated_at"]),
    }
