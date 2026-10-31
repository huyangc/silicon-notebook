"""Conservative JSON-example projection into the strict structured-output subset.

Hints describe shapes, not complete domain contracts. Scalar examples are never
constants; pipe-delimited strings advertise enums (including the legacy empty
value, except on a required root field). All object properties must be required
for strict structured output, so optional fields are represented with null.
Existing domain validation remains authoritative after the scheduler removes
unused null fields.

An empty list does not describe its item type. Only the semantic string-list
fields below and the spreadsheet plan's explicit filter contract supply that
missing information. Open objects and placeholder-key maps cannot be closed
without changing their meaning; those hints deliberately retain JSON mode.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


_STRING_LIST_FIELDS = frozenset({
    "keywords", "tags", "high_level_keywords", "low_level_keywords", "types", "region",
    "assumptions", "based_on_row_ids", "evidence_keys", "columns",
})
_NULL_STRING_FIELDS = frozenset({"parent", "edge_type", "suggestion_md"})
_DYNAMIC_FIELDS = frozenset({"source_queries", "frame_assignments"})
_ENUM_EXAMPLE = re.compile(r"[^\s|<>]+(?:\|[^\s|<>]+)+\Z")


class StructuredModelResponseError(ValueError):
    """Wire-schema mismatch; raw content is for shared parsing/private capture."""

    reason = "invalid_type"

    def __init__(self, content: str) -> None:
        super().__init__("Model response does not match the requested JSON Schema")
        self.content = content


class _UnsupportedHint(ValueError):
    pass


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _finite(value: Any) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True


def _load_json(content: str) -> Any:
    value = json.loads(
        content, object_pairs_hook=_unique_object, parse_constant=_reject_constant,
    )
    if not _finite(value):
        raise ValueError("non-finite JSON number")
    return value


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    if "type" in schema:
        types = schema["type"]
        types = [types] if isinstance(types, str) else types
        result = {**schema, "type": list(dict.fromkeys([*types, "null"]))}
        if "enum" in result and None not in result["enum"]:
            result["enum"] = [*result["enum"], None]
        return result
    variants = schema["anyOf"]
    if {"type": "null"} in variants:
        return schema
    return {"anyOf": [*variants, {"type": "null"}]}


def nullable_root_fields(example: dict[str, Any]) -> set[str]:
    """Root fields the strict projection lets a reply set to null.

    Nested object fields are always nullable. The JSON-mode shape walk uses
    this same set, so a null is "unused" or drift identically in both modes.
    """
    return _optional_root_fields(example)


def _optional_root_fields(example: dict[str, Any]) -> set[str]:
    if "is_paper" in example:
        # A negative classification explicitly omits all bibliography fields.
        return set(example) - {"is_paper"}
    if {"sufficient", "next_action"}.issubset(example):
        # Each reflect turn uses only its selected action's parameter branch.
        return set(example) - {"sufficient", "next_action", "reason"}
    if {"query", "sub_queries"}.issubset(example):
        return set(example) - {"query", "sub_queries"}
    return {"frame"}.intersection(example)


def _filter_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "column": {"type": "string"},
            "operator": {
                "type": "string",
                "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "contains"],
            },
            # Spreadsheet cells are scalar; numbers and booleans are not text.
            "value": {"type": ["string", "number", "boolean", "null"]},
        },
        "required": ["column", "operator", "value"],
        "additionalProperties": False,
    }


def _is_spreadsheet_filters(path: tuple[str, ...], root: dict[str, Any]) -> bool:
    """The spreadsheet planner's ``targets[].filters`` (an empty example list).

    Only there does an empty ``filters`` list have a known item type: every
    target names a workbook plan (``source_id``/``sheet``/``operation``/
    ``aggregation``) and its filters are the closed scalar-cell contract.
    """
    targets = root.get("targets")
    return (
        path == ("targets", "[]", "filters")
        and isinstance(targets, list)
        and bool(targets)
        and all(
            isinstance(target, dict)
            and {"source_id", "sheet", "operation", "aggregation"}.issubset(target)
            for target in targets
        )
    )


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise _UnsupportedHint()


def _project(
    examples: list[Any], path: tuple[str, ...], root: dict[str, Any],
) -> dict[str, Any]:
    non_null = [example for example in examples if example is not None]
    if not non_null:
        if path and path[-1] in _NULL_STRING_FIELDS:
            return {"type": ["string", "null"]}
        raise _UnsupportedHint()
    by_kind: dict[str, list[Any]] = {}
    for example in non_null:
        by_kind.setdefault(_kind(example), []).append(example)
    variants = [
        _project_kind(kind, values, path, root) for kind, values in by_kind.items()
    ]
    schema = variants[0] if len(variants) == 1 else {"anyOf": variants}
    return _nullable(schema) if len(non_null) != len(examples) else schema


def _project_kind(
    kind: str, examples: list[Any], path: tuple[str, ...], root: dict[str, Any],
) -> dict[str, Any]:
    if kind == "object":
        if any(not example for example in examples):
            raise _UnsupportedHint()
        properties: dict[str, list[Any]] = {}
        for example in examples:
            for key, value in example.items():
                if key in _DYNAMIC_FIELDS or "<" in key or ">" in key or key == "facet-id":
                    raise _UnsupportedHint()
                properties.setdefault(key, []).append(value)
        optional = _optional_root_fields(root) if not path else set(properties)
        projected = {}
        for key, values in properties.items():
            field = _project(values, (*path, key), root)
            projected[key] = _nullable(field) if key in optional else field
        return {
            "type": "object", "properties": projected,
            "required": list(projected), "additionalProperties": False,
        }
    if kind == "array":
        items = [item for example in examples for item in example]
        if items:
            item_schema = _project(items, (*path, "[]"), root)
        elif path and path[-1] in _STRING_LIST_FIELDS:
            item_schema = {"type": "string"}
        elif _is_spreadsheet_filters(path, root):
            item_schema = _filter_item_schema()
        else:
            raise _UnsupportedHint()
        return {"type": "array", "items": item_schema}
    if kind == "string":
        # A placeholder or one scalar value describes its type, never a const.
        if all(_ENUM_EXAMPLE.fullmatch(example) for example in examples):
            values = [value for example in examples for value in example.split("|")]
            # A required root field is the decision itself (reflect's
            # next_action, an intent's result_scope): "" there is not "unused"
            # but an invalid choice. Elsewhere "" keeps meaning "not used".
            required = len(path) == 1 and path[0] not in _optional_root_fields(root)
            empty = [] if required else [""]
            return {"type": "string", "enum": list(dict.fromkeys([*empty, *values]))}
        return {"type": "string"}
    return {"type": kind}


def response_format_for_hint(hint: str) -> dict[str, Any] | None:
    """Return strict OpenAI JSON Schema format, or None when inference is unsafe."""
    try:
        example = _load_json(hint)
        if not isinstance(example, dict) or not example:
            return None
        schema = _project([example], (), example)
        canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return {
            "type": "json_schema",
            "json_schema": {
                "name": f"model_response_{digest}", "strict": True, "schema": schema,
            },
        }
    except (TypeError, ValueError, RecursionError):
        return None


def validate_structured_response(content: str, response_format: dict[str, Any]) -> bool:
    """Validate the raw strict reply without coercing types or repairing JSON."""
    try:
        if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
            return False
        schema = response_format["json_schema"]["schema"]
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema).is_valid(_load_json(content))
    except (KeyError, TypeError, ValueError, RecursionError, SchemaError):
        return False
