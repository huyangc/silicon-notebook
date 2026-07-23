"""Transport-free helpers for parsing JSON model output."""
from __future__ import annotations

import json
import re


def safe_json(raw: str) -> dict:
    """Return the first JSON object in *raw*, or an empty object on failure."""
    if not raw:
        return {}
    text = raw.strip()
    text = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
