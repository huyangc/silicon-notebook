"""Backend-neutral repository compatibility values.

These objects are data/callable bundles only.  Keeping them below services
prevents composition-time compatibility seams from creating service cycles.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class RepositoryCompatibilitySeams:
    new_id: Callable[[str], str]
    now: Callable[[], str]
    copy_chunk_size: Callable[[], int]
    remap_json_ids: Callable[[Any, dict], Any]
    in_chunk_size: Callable[[], int]


def remap_json_ids(value: Any, maps: dict) -> Any:
    """Recursively rewrite copied source/element/object references in JSON."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in ("element_id", "source_id", "object_id") and isinstance(item, str):
                out[key] = maps.get(key, {}).get(item, item)
            elif key == "element_ids" and isinstance(item, list):
                mapping = maps.get("element_ids", {})
                out[key] = [
                    mapping.get(child, child)
                    if isinstance(child, str)
                    else remap_json_ids(child, maps)
                    for child in item
                ]
            else:
                out[key] = remap_json_ids(item, maps)
        return out
    if isinstance(value, list):
        return [remap_json_ids(item, maps) for item in value]
    return value

