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


def _is_open_object(example: Any) -> bool:
    """Whether an example describes an object whose KEYS it does not describe.

    Empty object examples permit caller-defined keys; JSON null is also
    accepted. Required fields and semantic validity remain the downstream
    domain parser's responsibility. Both strict and repaired JSON use this
    same object-shape rule.
    """
    return isinstance(example, dict) and not example


@dataclass(frozen=True)
class ShapeDeviation:
    """One advertised field whose reply value did not match the hint.

    ``path`` is a dotted/indexed locator such as ``sub_queries[0].types``;
    ``reason`` reuses the historical rejection vocabulary (``invalid_type`` /
    ``invalid_boolean`` / ``invalid_enum`` / ``missing_expected_key``) so
    dashboards and the analysis artifacts keep one set of words. ``fix`` says
    what the boundary did about it: ``""`` (delivered as written — the
    parser decides), ``"dropped"`` (a JSON null where the hint advertised a
    value: the key is removed, absent and null mean the same thing to every
    consumer), ``"coerced"`` (``"true"``/``"false"`` for a boolean hint, a
    numeric string for a number hint), ``"wrapped"`` (one scalar where a
    list of scalars was advertised becomes a one-item list).
    """

    path: str
    reason: str
    fix: str = ""


@dataclass(frozen=True)
class ModelJsonShape:
    """Outcome of the lenient shape walk over one delivered reply."""

    content: str
    deviations: tuple[ShapeDeviation, ...] = ()

    @property
    def normalised(self) -> bool:
        return any(item.fix for item in self.deviations)


# Cap on deviations one reply may report: the walk is bounded by the reply
# size anyway, but a pathological list of thousands of wrong-typed items must
# not turn a diagnostic event into a payload of its own. The walk itself
# continues past the cap (normalisation must cover the whole reply); only
# the report is truncated.
SHAPE_DEVIATIONS_MAX = 32

# Sentinel: "remove this key / item" — a JSON null the hint did not advertise.
_DROP = object()

_INT_STRING_RE = re.compile(r"-?(?:0|[1-9]\d*)\Z")
_NUMBER_STRING_RE = re.compile(
    r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z"
)


def _join_path(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _normalise_shape(
    value: Any,
    example: Any,
    *,
    path: str,
    out: list[ShapeDeviation],
    state: dict[str, int],
    field_name: str = "",
) -> Any:
    """Walk a reply against its example-shaped hint: absorb the deviations
    every consumer would resolve the same way, report the rest, reject
    nothing. Returns the (possibly normalised) value, or ``_DROP``.

    Harness principle (2026-09-14, user decision): the prompt must be precise,
    the shared boundary tolerates the deviations a model plausibly makes, and
    only the domain parsers — which narrow every enum and coerce every scalar
    they read — stay strict where being wrong would write bad data. Before
    this change the same walk raised ``ModelJsonRepairError`` on the first
    mismatch and the whole reply became a ``malformed_response``: one
    hallucinated edge type discarded a whole KG extraction window, a
    ``"year": null`` failed paper metadata for every non-paper source, and
    every parser-side tolerance was dead code because the boundary rejected
    the reply first.

    What is absorbed here, and why here rather than in each parser: a null
    where the hint advertised a value (``"markdown": null``) is the model
    saying "nothing", and a bare ``str()`` downstream would turn it into the
    literal text ``"None"`` — a non-empty string that slips past every
    emptiness check and gets persisted as prose. ``"true"``/``"false"`` for a
    boolean hint and ``"2017"`` for a number hint have one reading. One
    scalar where a list of scalars was advertised (``"sub_queries": "如何降低
    噪声"``) would otherwise be iterated character by character. Each fix is
    still reported, so a drifting prompt stays visible in ``events.jsonl``.
    Anything with more than one plausible reading — a list where a string
    was advertised, an off-enum value, a scalar where an object was
    advertised — is delivered as written and reported; the consumer decides.

    Enum rule (unchanged in spirit): a hint string containing ``|`` means
    "IF filled, one of these values"; the empty string always passes because
    prompts use it for "this run does not use the field" (the 2026-09-07
    reflect ``enumerate.kind`` incident).
    """

    def note(reason: str, fix: str = "") -> None:
        # ``state["fixes"]`` counts every absorption, capped or not: whether
        # the delivered content was rewritten must never depend on how many
        # diagnostics fit in the report (codex #720 R1).
        if fix:
            state["fixes"] += 1
        if len(out) < SHAPE_DEVIATIONS_MAX:
            out.append(ShapeDeviation(path=path or "$", reason=reason, fix=fix))

    # A schema hint is prompt prose encoded as a JSON example, not JSON Schema.
    # Two current workload contracts deliberately use values the example cannot
    # express: conflict review's payload is null or an object, and its winner
    # placeholder explicitly permits JSON null.
    if field_name == "resolved_payload" and (
        value is None or isinstance(value, dict)
    ):
        return value
    if (
        value is None
        and isinstance(example, str)
        and example.startswith("<")
        and "null" in example.lower()
    ):
        return value
    if example is None:
        # Hints use null for optional scalar fields whose concrete value may be
        # null or a string (for example an optional edge type).
        if value is not None and not isinstance(value, str):
            note("invalid_type")
        return value
    if value is None:
        if _is_open_object(example):
            # An open object (``{}`` in the hint) explicitly accepts null.
            return value
        # Every remaining example advertises a value: null and absent mean the
        # same thing to the consumer, so make it absent.
        note("invalid_type" if not isinstance(example, bool) else "invalid_boolean", "dropped")
        return _DROP
    if isinstance(example, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            note("invalid_boolean", "coerced")
            return value.strip().lower() == "true"
        note("invalid_boolean")
        return value
    if isinstance(example, str):
        if not isinstance(value, str):
            note("invalid_type")
        elif "|" in example and value and value not in example.split("|"):
            note("invalid_enum")
        return value
    if isinstance(example, int):
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str) and _INT_STRING_RE.fullmatch(value.strip()):
            note("invalid_type", "coerced")
            return int(value.strip())
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            note("invalid_type", "coerced")
            return int(value)
        note("invalid_type")
        return value
    if isinstance(example, float):
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        ):
            return value
        if isinstance(value, str) and _NUMBER_STRING_RE.fullmatch(value.strip()):
            number = float(value.strip())
            if math.isfinite(number):
                note("invalid_type", "coerced")
                return number
        note("invalid_type")
        return value
    if isinstance(example, list):
        item_example = example[0] if example else None
        if not isinstance(value, list):
            if isinstance(value, (str, int, float)) and not isinstance(
                item_example, (dict, list)
            ):
                # One scalar where a list of scalars was advertised.
                note("invalid_type", "wrapped")
                value = [value]
            else:
                note("invalid_type")
                return value
        if not example:
            return value
        items: list[Any] = []
        for index, item in enumerate(value):
            fixed = _normalise_shape(
                item, item_example,
                path=f"{path}[{index}]", out=out, state=state,
                field_name=field_name,
            )
            if fixed is not _DROP:
                items.append(fixed)
        return items
    if isinstance(example, dict):
        if _is_open_object(example):
            # An open object (``{}`` in the hint) describes no keys and also
            # accepts JSON null; only a scalar or list is off-shape.
            if not isinstance(value, dict):
                note("invalid_type")
            return value
        if not isinstance(value, dict):
            note("invalid_type")
            return value
        if field_name == "frame_assignments":
            # The hint's ``facet-id`` is a placeholder. Actual keys come from
            # the report frame and are checked against that frame downstream;
            # this shared walk owns only the advertised value type.
            item_example = next(iter(example.values()), "")
            rebuilt: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                # The keys here are model-authored text, not hint vocabulary:
                # the diagnostic path names the entry by position so the
                # event log stays content-free.
                fixed = _normalise_shape(
                    item, item_example, path=f"{path}[{index}]",
                    out=out, state=state,
                )
                if fixed is not _DROP:
                    rebuilt[key] = fixed
            return rebuilt
        shared_keys = set(value).intersection(example)
        # A described nested object that shares no key with its example is
        # unusable to the parser that reads it (an empty plan item, say);
        # ``frame`` / ``validity_scope`` have entirely optional children and
        # may legitimately be ``{}``. Extra keys are never a deviation: hints
        # are examples, and providers add fields of their own.
        if example and not shared_keys and not (
            field_name in {"frame", "validity_scope"} and not value
        ):
            note("missing_expected_key")
            return value
        rebuilt = {}
        for key, item in value.items():
            if key not in shared_keys:
                rebuilt[key] = item
                continue
            fixed = _normalise_shape(
                item, example[key],
                path=_join_path(path, key), out=out, state=state,
                field_name=key,
            )
            if fixed is not _DROP:
                rebuilt[key] = fixed
        return rebuilt
    note("invalid_type")
    return value


def validate_model_json_shape(content: str, schema_hint: str) -> ModelJsonShape:
    """Gate a parseable object on usability; absorb or report field drift.

    Raises ``ModelJsonRepairError("missing_expected_key")`` only when the
    reply contains NONE of the hint's top-level fields (``{}`` included, and a
    reply whose advertised fields were all JSON null): no consumer can use
    such a reply, and delivering it would turn a model failure into a silent
    empty result. Every other mismatch is handled by ``_normalise_shape`` —
    absorbed when it has a single reading, delivered as written otherwise —
    and reported in ``deviations`` for the caller to log. ``content`` is the
    reply to deliver: byte-for-byte the input when nothing was absorbed, a
    canonical re-serialisation when something was. Provider-added or
    model-added extra fields are never deviations.
    """
    value = _strict_object(content)
    # ``json.loads`` accepts NaN / Infinity / overflowing exponents that JSON
    # itself cannot carry; a confidence of +inf would clamp to 1.0 in every
    # numeric consumer. Reject them here, once, on the strict path exactly
    # as the repair path always has (codex #720 R7).
    _assert_json_domain(value)
    example = _schema_example(schema_hint)
    if not example:
        return ModelJsonShape(content=content)
    if not set(value).intersection(example):
        raise ModelJsonRepairError("missing_expected_key")
    deviations: list[ShapeDeviation] = []
    state = {"fixes": 0}
    fixed = _normalise_shape(
        value, example, path="", out=deviations, state=state,
    )
    if not state["fixes"]:
        return ModelJsonShape(content=content, deviations=tuple(deviations))
    if not isinstance(fixed, dict) or not set(fixed).intersection(example):
        # Every advertised field was null: nothing usable survived.
        raise ModelJsonRepairError("missing_expected_key")
    try:
        canonical = json.dumps(
            fixed, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        # ``json.loads`` accepts NaN/Infinity literals that JSON cannot carry;
        # a rewritten reply must fail through the malformed-response path,
        # not as an uncaught error past the scheduler.
        raise ModelJsonRepairError("non_finite_number") from exc
    return ModelJsonShape(content=canonical, deviations=tuple(deviations))


def _validate_repaired_shape(
    raw: str,
    value: dict[str, Any],
    schema_hint: str,
) -> None:
    # The repaired object goes through ``validate_model_json_shape`` on its
    # canonical serialisation right after this returns, so field-level shape
    # is judged ONCE, by the same lenient walk as strict JSON. What stays here
    # is what only the repair path can violate: the reply must still name at
    # least one expected top-level field, and repair may restore delimiters
    # but never author or alter semantic text.
    example = _schema_example(schema_hint)
    if example and not set(value).intersection(example):
        raise ModelJsonRepairError("missing_expected_key")

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
