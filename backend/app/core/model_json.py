"""Conservative normalization for JSON objects returned by chat models.

The transport asks for JSON, but some OpenAI-compatible endpoints accept that
request without enforcing it. Keep valid JSON byte-for-byte unchanged and use
the repair parser only for complete object-shaped replies. Repaired string
values are accepted only when they remain verbatim in the raw response, so
syntax recovery cannot silently rewrite an answer, query, or action argument.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any

import json_repair


@dataclass(frozen=True)
class ModelJsonObject:
    content: str
    repaired: bool = False


class ModelJsonRepairError(ValueError):
    """The response was neither strict JSON nor safely repairable."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _strict_object(content: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise ModelJsonRepairError("invalid_json") from exc
    if not isinstance(value, dict):
        raise ModelJsonRepairError("non_object")
    return value


def _schema_example(schema_hint: str) -> dict[str, Any]:
    try:
        value = json.loads(schema_hint)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _assert_json_domain(value: Any) -> None:
    """Reject repaired values that JSON itself cannot represent faithfully."""
    pending = [value]
    while pending:
        item = pending.pop()
        if item is None or isinstance(item, (str, bool, int)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ModelJsonRepairError("non_finite_number")
            continue
        if isinstance(item, list):
            pending.extend(item)
            continue
        if isinstance(item, dict):
            if not all(isinstance(key, str) for key in item):
                raise ModelJsonRepairError("non_string_key")
            pending.extend(item.values())
            continue
        raise ModelJsonRepairError("non_json_value")


_BARE_TOKEN_RE = re.compile(r"[^\s{}\[\]:,]+")
_JSON_NUMBER_RE = re.compile(
    r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z"
)


def _mask_quoted_strings(raw: str) -> str:
    """Hide quoted contents while preserving offsets for surface checks."""
    chars = list(raw)
    quote: str | None = None
    escaped = False
    for index, char in enumerate(raw):
        if quote is None:
            if char in {'"', "'"}:
                quote = char
                chars[index] = " "
            continue
        chars[index] = " "
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            quote = None
    return "".join(chars)


def _validate_complete_structure(raw: str) -> None:
    """Refuse to invent a closing quote, object, or array after truncation."""
    closing_for = {"{": "}", "[": "]"}
    stack: list[str] = []
    quote: str | None = None
    escaped = False
    for char in raw:
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char in closing_for:
            stack.append(closing_for[char])
        elif char in {"}", "]"}:
            if not stack or stack.pop() != char:
                raise ModelJsonRepairError("incomplete_object")
    if quote is not None or stack:
        raise ModelJsonRepairError("incomplete_object")


def _walk_strings_and_keys(value: Any) -> tuple[list[str], set[str]]:
    strings: list[str] = []
    keys: set[str] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            strings.append(item)
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            keys.update(item)
            pending.extend(item.values())
    return strings, keys


def _container_at(surface: str, offset: int) -> str | None:
    stack: list[str] = []
    for char in surface[:offset]:
        if char in {"{", "["}:
            stack.append(char)
        elif char in {"}", "]"} and stack:
            stack.pop()
    return stack[-1] if stack else None


def _is_bare_value_position(raw: str, surface: str, offset: int) -> bool:
    """Distinguish a bare value from an orphan token in an object."""
    container = _container_at(surface, offset)
    boundary_index = next(
        (
            index
            for index in range(offset - 1, -1, -1)
            if raw[index] in "{}[],:\"'"
        ),
        None,
    )
    if boundary_index is None or raw[boundary_index + 1:offset].strip():
        # Accept one bare token per value position. Ambiguous multi-word prose
        # and a primitive followed by an orphan token must be retried quoted.
        return False
    boundary = raw[boundary_index]
    if container == "{":
        return boundary == ":"
    if container == "[":
        return boundary in {"[", ","}
    return False


def _validate_repair_surface(raw: str, value: dict[str, Any]) -> None:
    """Reject permissive parser extensions beyond quote/comma recovery.

    ``json-repair`` deliberately accepts comments, semicolons, Python literals,
    and orphan tokens. Those are not formatting repairs: accepting them can
    silently discard or reinterpret model output. Every bare token must instead
    be an emitted key, a JSON primitive, or text preserved in an emitted string.
    """
    surface = _mask_quoted_strings(raw)
    if "//" in surface or "/*" in surface or "*/" in surface:
        raise ModelJsonRepairError("unsupported_syntax")
    if ";" in surface or "#" in surface:
        raise ModelJsonRepairError("unsupported_syntax")

    strings, keys = _walk_strings_and_keys(value)
    for match in _BARE_TOKEN_RE.finditer(surface):
        token = match.group()
        suffix = surface[match.end():].lstrip()
        if token in keys and suffix.startswith(":"):
            continue
        if not _is_bare_value_position(raw, surface, match.start()):
            raise ModelJsonRepairError("unsupported_syntax")
        if token in {"True", "False", "None", "NaN", "Infinity"}:
            raise ModelJsonRepairError("unsupported_syntax")
        if token in {"true", "false", "null"}:
            continue
        if _JSON_NUMBER_RE.fullmatch(token):
            continue
        if any(token in item for item in strings):
            continue
        raise ModelJsonRepairError("unsupported_syntax")


#: Keys a domain parser may receive without the hint advertising them, at the
#: TOP LEVEL of the response object only (see the ``is_root`` guard below).
#
# The repair branch below otherwise rejects every unadvertised key
# (``unknown_key``), while the strict branch tolerates extras.  That asymmetry
# is deliberate for keys a repair could have INVENTED, but it must not decide
# the fate of a field the product knowingly accepts and merely does not ask the
# model for yet: reflect v2's ``assessment`` was parsed and stored before its
# own hint advertised it, so with a stray trailing comma the same payload would
# be rejected while its strict twin passes.  Named, bounded, and — no other
# hint describes a key by this name — inert for every other workload.
#
# T4 advertises ``assessment`` in ``reflect_v2_schema_hint``, which makes this
# exemption INERT for reflect v2 (an advertised key needs none).  It stays
# because the LEGACY reflect hint still does not advertise it and legacy is a
# frozen byte-for-byte baseline: removing the entry would TIGHTEN the
# closed-state gate, which is a behaviour change in the rejecting direction.
_TOLERATED_UNADVERTISED_KEYS = frozenset({"assessment"})


def _is_open_object(example: Any) -> bool:
    """Whether an example describes an object whose KEYS it does not describe.

    An empty object example (``"arguments":{}``) is the only way a hint can say
    "an object goes here, and its fields are specified elsewhere".  Reflect v2
    uses it because the per-action field contract lives in the system prompt
    and the typed validation lives in the reasoning layer; advertising a
    non-empty example instead would make the legal ``"arguments": {}`` that
    ``answer`` sends fail ``missing_expected_key``.

    Both validation paths share this one rule.  The strict path already behaved
    this way implicitly (it only ever descends into keys the example names);
    the repair path did not, so a non-empty ``arguments`` object plus one
    trailing comma was rejected as ``unknown_key`` — a real retrieval action
    losing its repair net over a syntax fault the layer exists to absorb.

    JSON ``null`` counts as such an object on both paths (see the two call
    sites).  A hint that says "the fields are specified elsewhere" is in no
    position to insist the object be present at all: the domain parser that
    owns those fields is the one that knows whether absence is legal, and for
    every current open object it is (``arguments`` and ``assessment`` both
    normalise ``None`` to "not sent").  Rejecting it here would spend the
    retries and drop the whole call into its fail-open fallback over a value
    the parser one layer down accepts.
    """
    return isinstance(example, dict) and not example


def _validate_against_example(
    value: Any, example: Any, *, is_root: bool = True,
) -> None:
    """Validate repaired JSON against the example-shaped schema hint.

    This layer owns SHAPE only — types, containers, and the advertised key set.
    Semantic whitelisting stays with each domain parser, which already narrows
    every enum field it reads (``kind if kind in ENUMERABLE_ELEMENT_KINDS else
    ""``, ``direction`` falling back to ``"both"``, an unrecognised
    ``next_action`` handled by the fail-closed/answer contract). A second,
    stricter semantic gate here can only manufacture disagreements between the
    prompt and the validator.

    Hence the enum rule: a hint string containing ``|`` means "IF filled, it
    must be one of these values". The empty string means "this run does not use
    the field" and is always accepted; a non-empty value outside the set is
    still ``invalid_enum``.

    Root cause this rule fixes (2026-09-07): ``reflect_schema_hint`` spells
    ``"kind":"formula|table|image|code_block"`` while the reflect prompt tells
    the model to leave ``kind`` empty when listing the document roster
    (``enumerate.collection="sources"``). Treating ``|`` as a closed set made
    the model's prompt-conforming decision fail ``invalid_enum``, which showed
    up as ``MalformedModelResponse`` and pushed ``reflect()`` into its fail-open
    "answer" fallback — so the document-roster and ``enumerate_kg_objects``
    actions were dead in production.

    ``is_root`` gates ``_TOLERATED_UNADVERTISED_KEYS``: the top-level response
    object may carry a tolerated key the hint does not advertise, but a nested
    object at any depth may not smuggle the same key past ``unknown_key`` — the
    tolerance is for one named, product-known top-level field, not a general
    escape hatch that follows the key name into every dict in the payload.
    """
    if example is None:
        # Hints use null for optional scalar fields whose concrete value may be
        # null or a string (for example an optional edge type).
        if value is not None and not isinstance(value, str):
            raise ModelJsonRepairError("invalid_type")
        return
    if isinstance(example, bool):
        if not isinstance(value, bool):
            raise ModelJsonRepairError("invalid_boolean")
        return
    if isinstance(example, str):
        if not isinstance(value, str):
            raise ModelJsonRepairError("invalid_type")
        if "|" in example and value and value not in example.split("|"):
            raise ModelJsonRepairError("invalid_enum")
        return
    if isinstance(example, int):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ModelJsonRepairError("invalid_type")
        return
    if isinstance(example, float):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            raise ModelJsonRepairError("invalid_type")
        return
    if isinstance(example, list):
        if not isinstance(value, list):
            raise ModelJsonRepairError("invalid_type")
        if example:
            for item in value:
                _validate_against_example(item, example[0], is_root=False)
        return
    if isinstance(example, dict):
        if _is_open_object(example):
            # Checked BEFORE the dict type check: an open object also accepts
            # JSON null (see ``_is_open_object``).
            if value is None or isinstance(value, dict):
                return
            raise ModelJsonRepairError("invalid_type")
        if not isinstance(value, dict):
            raise ModelJsonRepairError("invalid_type")
        tolerated = _TOLERATED_UNADVERTISED_KEYS if is_root else frozenset()
        extra = set(value) - tolerated
        if not extra.issubset(example):
            raise ModelJsonRepairError("unknown_key")
        for key, item in value.items():
            if key in example:
                _validate_against_example(item, example[key], is_root=False)
        return
    raise ModelJsonRepairError("invalid_type")


def _validate_known_shape(
    value: Any,
    example: Any,
    *,
    field_name: str = "",
) -> None:
    """Validate fields described by a schema example without rejecting extras.

    Schema hints are examples rather than full JSON Schema documents.  Some
    callers intentionally tolerate provider-added fields, but a named field
    with the wrong container/scalar type is never usable by their parser.
    """
    # A schema hint is prompt prose encoded as a JSON example, not JSON Schema.
    # These two current workload contracts deliberately use values that the
    # example cannot express: conflict review's payload is null or an object,
    # and its winner placeholder explicitly permits JSON null.
    if field_name == "resolved_payload" and (
        value is None or isinstance(value, dict)
    ):
        return
    if (
        value is None
        and isinstance(example, str)
        and example.startswith("<")
        and "null" in example.lower()
    ):
        return
    if isinstance(example, dict):
        if _is_open_object(example):
            # Same rule as the repair path, null included (see
            # ``_is_open_object``). Spelled out rather than left implicit in
            # the ``if example and ...`` below, so the two paths cannot drift
            # apart again.
            if value is None or isinstance(value, dict):
                return
            raise ModelJsonRepairError("invalid_type")
        if not isinstance(value, dict):
            raise ModelJsonRepairError("invalid_type")
        if field_name == "frame_assignments":
            # The hint's ``facet-id`` is a placeholder. Actual keys come from
            # the report frame and are checked against that frame downstream;
            # this shared shape gate owns only the advertised value type.
            item_example = next(iter(example.values()), "")
            for item in value.values():
                _validate_known_shape(item, item_example)
            return
        shared_keys = set(value).intersection(example)
        # These named nested objects have entirely optional children in their
        # downstream contracts. Other described nested objects still need at
        # least one usable field, so an empty plan item remains a mismatch.
        # (Reflect v2's ``assessment`` needs no entry here: its hint advertises
        # an OPEN object, which returns above.)
        if example and not shared_keys and not (
            field_name in {"frame", "validity_scope"} and not value
        ):
            raise ModelJsonRepairError("missing_expected_key")
        for key in shared_keys:
            _validate_known_shape(value[key], example[key], field_name=key)
        return
    if isinstance(example, list):
        if not isinstance(value, list):
            raise ModelJsonRepairError("invalid_type")
        if example:
            for item in value:
                _validate_known_shape(item, example[0], field_name=field_name)
        return
    _validate_against_example(value, example)


def validate_model_json_shape(content: str, schema_hint: str) -> None:
    """Reject parseable objects whose advertised fields violate the hint.

    Missing all top-level advertised fields (including ``{}``) is a contract
    failure.  Individual fields remain optional because the product's hints
    are examples and several workloads deliberately omit optional members.
    """
    value = _strict_object(content)
    example = _schema_example(schema_hint)
    if not example:
        return
    if not set(value).intersection(example):
        raise ModelJsonRepairError("missing_expected_key")
    _validate_known_shape(value, example)


def _validate_repaired_shape(
    raw: str,
    value: dict[str, Any],
    schema_hint: str,
) -> None:
    example = _schema_example(schema_hint)
    if example:
        expected_keys = set(example)
        if not set(value).intersection(expected_keys):
            raise ModelJsonRepairError("missing_expected_key")
        _validate_against_example(value, example)

    _validate_repair_surface(raw, value)

    # Repair may restore delimiters, never author semantic text. Apply this to
    # nested planning queries/actions too, not only the final answer field.
    pending = list(value.values())
    while pending:
        item = pending.pop()
        if isinstance(item, str) and item:
            escaped = json.dumps(item, ensure_ascii=False)[1:-1]
            if item not in raw and escaped not in raw:
                raise ModelJsonRepairError("string_changed")
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            pending.extend(item.values())
    _assert_json_domain(value)


def parse_model_json_object(
    content: str,
    schema_hint: str,
    *,
    allow_repair: bool,
) -> ModelJsonObject:
    """Return a canonical object response, repairing only bounded syntax faults."""
    if not isinstance(content, str) or not content.strip():
        raise ModelJsonRepairError("empty")
    try:
        _strict_object(content)
    except ModelJsonRepairError as strict_error:
        if not allow_repair or strict_error.reason == "non_object":
            raise
    else:
        return ModelJsonObject(content=content)

    stripped = content.strip()
    # Never let a repair library turn a token-budget truncation into an
    # apparently complete decision or answer by inventing closing structure.
    if not (stripped.startswith("{") and stripped.endswith("}")):
        raise ModelJsonRepairError("incomplete_object")
    _validate_complete_structure(stripped)
    try:
        repaired = json_repair.loads(stripped, skip_json_loads=True)
    except Exception as exc:  # json-repair exposes several ValueError variants
        raise ModelJsonRepairError("repair_failed") from exc
    if not isinstance(repaired, dict):
        raise ModelJsonRepairError("non_object")
    _validate_repaired_shape(stripped, repaired, schema_hint)
    try:
        canonical = json.dumps(
            repaired,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ModelJsonRepairError("serialization_failed") from exc
    _strict_object(canonical)
    return ModelJsonObject(content=canonical, repaired=True)
