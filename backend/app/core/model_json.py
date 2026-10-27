"""Conservative normalization for JSON objects returned by chat models.

The transport asks for JSON, but some OpenAI-compatible endpoints accept that
request without enforcing it. Keep valid JSON byte-for-byte unchanged and use
the repair parser only for complete object-shaped replies. Repaired string
values are accepted only when they remain verbatim in the raw response, so
syntax recovery cannot silently rewrite an answer, query, or action argument.
Deviations with a single reading are not repairs and are absorbed for every
workload: a backslash that starts no JSON escape (LaTeX ``\\mathrm``,
``50\\%``) or a raw control character inside a string is read as the
characters written, stray closing punctuation or a repeat of the object after
a complete object is dropped, a restarted reply (``{"{"answer": ...}``,
``{"answer{"answer": ...}``) is read as the complete object the model
restarted, and an unescaped quote inside a value string that text follows
(``"所谓"恒流"模式"``) is read as a character of the text.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any

import json_repair

from app.core.model_schema import nullable_root_fields


@dataclass(frozen=True)
class ModelJsonObject:
    content: str
    repaired: bool = False
    #: A complete leading object was delivered and stray closing punctuation
    #: after it was dropped (see ``_leading_object``); ``content`` is then
    #: the object's own text, byte-for-byte apart from ``stray_backslashes``.
    trailing_data: bool = False
    #: A backslash that starts no JSON escape was read literally (see
    #: ``_escape_stray_backslashes``); ``content`` is the reply with each such
    #: backslash doubled, so it decodes to exactly the characters written.
    stray_backslashes: bool = False
    #: The reply restarted -- ``{"{``, ``{"answer{"answer":`` -- and the
    #: complete object after the fragments was taken (see
    #: ``_restarted_object``); ``content`` is that object's own text,
    #: byte-for-byte apart from the escapes the other flags name.
    duplicated_open: bool = False
    #: A raw control character (a newline or tab typed inside a string) was
    #: written as its JSON escape (see ``_read_literally``).
    control_chars: bool = False
    #: An unescaped ``"`` inside a value string, followed by text rather than
    #: JSON structure, was read as a character of the text and escaped (see
    #: ``_closes_value_string``).
    inner_quotes: bool = False


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


#: What may follow a backslash inside a JSON string (``u`` only with four hex
#: digits after it, see ``_starts_json_escape``).
_JSON_ESCAPE_CHARS = frozenset('"\\/bfnrt')
_HEX4_RE = re.compile(r"[0-9a-fA-F]{4}")


def _starts_json_escape(text: str, index: int) -> bool:
    char = text[index:index + 1]
    if char == "u":
        return bool(_HEX4_RE.fullmatch(text[index + 1:index + 5]))
    return bool(char) and char in _JSON_ESCAPE_CHARS


def _escape_stray_backslashes(raw: str) -> str:
    r"""Read a backslash that starts no JSON escape as a literal backslash.

    Models writing LaTeX or Markdown into a JSON string emit ``\mathrm{V}``,
    ``50\%`` or ``a\_b`` although the prompt asks for ``\\``. JSON rejects
    those escapes, and their only reading is the characters as written, so
    each such backslash inside a double-quoted string is doubled: the decoded
    text is then exactly what the model wrote. Valid escapes (``\n``, ``\"``,
    ``\\``, ``\u`` with four hex digits) are left to the JSON decoder, and
    text outside double-quoted strings is untouched (deepseek-flash,
    2026-09-29: an answer mixing ``\n`` paragraph breaks with ``50\%`` was
    rejected as ``string_changed`` on both synthesis attempts).
    """
    if "\\" not in raw:
        return raw
    out: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(raw):
        char = raw[index]
        if quote is None:
            if char in {'"', "'"}:
                quote = char
        elif char == "\\":
            if quote == '"' and not _starts_json_escape(raw, index + 1):
                out.append("\\\\")
                index += 1
                continue
            out.append(raw[index:index + 2])
            index += 2
            continue
        elif char == quote:
            quote = None
        out.append(char)
        index += 1
    return "".join(out)


def _decoded_string_surface(raw: str) -> str:
    """``raw`` with each double-quoted string's body replaced by its decoding.

    The decoding is the one ``_escape_stray_backslashes`` defines: JSON
    escapes as JSON reads them, stray backslashes literally. A body JSON still
    cannot decode (a raw control character, say) stays as written, so a
    repair of that kind is judged exactly as before. Delimiters and text
    outside double-quoted strings are kept as written, so a match can span
    only text the model itself wrote, as with ``raw``.
    """
    text = _escape_stray_backslashes(raw)
    out: list[str] = []
    copied = 0
    quote: str | None = None
    escaped = False
    start = 0
    for index, char in enumerate(text):
        if quote is None:
            if char in {'"', "'"}:
                quote = char
                start = index
            continue
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            quote = None
            if char != '"':
                continue
            try:
                decoded = json.loads(text[start:index + 1])
            except ValueError:
                continue
            out.append(text[copied:start + 1])
            out.append(decoded)
            copied = index
    out.append(text[copied:])
    return "".join(out)


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


# Cap on deviations one reply may report: the walk is bounded by the reply
# size anyway, but a pathological list of thousands of wrong-typed items must
# not turn a diagnostic event into a payload of its own. The walk itself
# continues past the cap (normalisation must cover the whole reply); only
# the report is truncated.
SHAPE_DEVIATIONS_MAX = 32

# Sentinel: "remove this key / item" — a JSON null the hint did not advertise.
_DROP = object()

# Lists whose items other fields reference by 0-based position (KG extraction
# edges name their endpoints by index into ``nodes``): removing an item there
# would silently re-point every later reference.
_POSITIONAL_LISTS = frozenset({"nodes"})

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
    state: dict[str, Any],
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
        if not path.endswith("]") and (
            "." in path or "[" in path or path in state["nullable_root"]
        ):
            # A property the strict projection makes nullable: prompts spell
            # "unused" as null there, in either mode, so this is not drift.
            state["fixes"] += 1
            return _DROP
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
            elif isinstance(value, (str, int, float)):
                # A scalar where a list of containers was advertised has no
                # reading at all; consumers iterate these collections
                # (``for it in data.get("edges") or []``), so deliver it as
                # absent rather than as a TypeError (codex #720 R10).
                note("invalid_type", "dropped")
                return _DROP
            else:
                note("invalid_type")
                return value
        if not example:
            return value
        items: list[Any] = []
        for index, item in enumerate(value):
            if item is None and field_name in _POSITIONAL_LISTS:
                # Other fields address this list by position, so a dropped
                # item would shift every later one onto the wrong reference;
                # the null stays as a hole the consumer skips.
                if len(out) < SHAPE_DEVIATIONS_MAX:
                    out.append(ShapeDeviation(
                        path=f"{path}[{index}]", reason="invalid_type", fix="",
                    ))
                items.append(item)
                continue
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

    A null is removed for the consumer wherever the hint advertised a value;
    it is reported only where the strict projection of the same hint would
    reject it (a required root field, a list item), so the conforming nulls
    prompts ask for never bury real drift.
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
    state: dict[str, Any] = {"fixes": 0, "nullable_root": nullable_root_fields(example)}
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
    # A string whose raw spelling mixes JSON escapes with stray backslashes
    # (``\n`` next to ``50\%``) matches neither ``raw`` nor its canonical
    # escaping; it is still verbatim when it appears in the decoded surface.
    surface: str | None = None
    pending = list(value.values())
    while pending:
        item = pending.pop()
        if isinstance(item, str) and item:
            escaped = json.dumps(item, ensure_ascii=False)[1:-1]
            if item not in raw and escaped not in raw:
                if surface is None:
                    surface = _decoded_string_surface(raw)
                if item not in surface:
                    raise ModelJsonRepairError("string_changed")
        if isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, dict):
            pending.extend(item.values())
    _assert_json_domain(value)
    if _value_tokens(value) != _raw_tokens(raw):
        raise ModelJsonRepairError("string_changed")


# A bare token in the raw reply: anything up to whitespace, a JSON delimiter
# or a quote (``_BARE_TOKEN_RE`` also spans quotes; it judges masked text).
_RAW_BARE_TOKEN_RE = re.compile(r"""[^\s{}\[\]:,"']+""")


def _token_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _value_tokens(value: Any) -> list[str]:
    """Keys and scalar values of ``value`` in document order, as text."""
    tokens: list[str] = []
    pending: list[Any] = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in reversed(list(item.items())):
                pending.append(child)
                pending.append(key)
        elif isinstance(item, list):
            pending.extend(reversed(item))
        else:
            tokens.append(_token_text(item))
    return [token for token in tokens if token]


def _single_quoted_as_json(body: str) -> str:
    r"""A single-quoted body spelled as a double-quoted JSON body.

    Read as the repair parser reads it: ``\'`` is a quote, JSON escapes keep
    their meaning (``\"`` included), a backslash that starts none is literal
    (``50\%``), and a bare ``"`` is a character of the text.
    """
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\":
            if body[index + 1:index + 2] == "'":
                out.append("'")
                index += 2
                continue
            if _starts_json_escape(body, index + 1):
                out.append(body[index:index + 2])
                index += 2
                continue
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        else:
            out.append(char)
        index += 1
    return "".join(out)


def _raw_tokens(raw: str) -> list[str] | None:
    """Keys and scalar values the model wrote, in order, or ``None``.

    Repair may restore delimiters only. Every check before this one looks at
    strings one at a time, so text could still move: two adjacent strings
    (``"A" "B",``) came back as a value ``A`` and a key ``B","grounded`` --
    the second paragraph and the field after it gone -- and a repeated key
    kept only its last value. Comparing the whole sequence catches both: a
    repair that drops, merges, splits, reorders or authors a token is
    rejected. Strings decode as the delivered reply would (stray backslashes
    literal); a string that cannot be decoded has no reading to compare.
    Empty strings are skipped on both sides (``{"a":,"b":1}`` repairs to an
    empty value, which authors no text).
    """
    text = _escape_stray_backslashes(raw)
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or char in "{}[]:,":
            index += 1
            continue
        if char in {'"', "'"}:
            end = index + 1
            while end < len(text) and text[end] != char:
                end += 2 if text[end] == "\\" else 1
            body = text[index + 1:end]
            if char == "'":
                body = _single_quoted_as_json(body)
            try:
                tokens.append(json.loads(f'"{body}"', strict=False))
            except ValueError:
                return None
            index = end + 1
            continue
        match = _RAW_BARE_TOKEN_RE.match(text, index)
        token = match.group()
        if _JSON_NUMBER_RE.fullmatch(token):
            token = _token_text(json.loads(token))
        tokens.append(token)
        index = match.end()
    return [token for token in tokens if token]


_DECODER = json.JSONDecoder()
# What may follow a complete object and still be read as noise: closing
# delimiters, quotes, backticks and whitespace. A deepseek-flash report section
# came back as a whole valid object followed by an extra ``"}`` (2026-09-29).
# Anything else after the object -- a different second object, ``, "key":
# value}`` continuing fields the model meant to include, prose -- has more
# than one reading and stays a malformed response.
_TRAILING_NOISE_RE = re.compile(r"[\s}\]\"'`]+\Z")


@dataclass(frozen=True)
class _Reading:
    """One spelling of the reply and what reading it that way absorbed."""

    text: str
    stray_backslashes: bool = False
    control_chars: bool = False
    inner_quotes: bool = False


def _closes_value_string(
    text: str, index: int, *, in_array: bool = False,
) -> bool | None:
    """Whether the ``"`` at ``index`` inside a VALUE string closes it.

    ``True`` when JSON structure follows (``,`` ``}`` ``]`` or the end),
    ``False`` when text follows (``所谓"恒流"模式``: the quote is a character
    of the answer), ``None`` when what follows is itself ambiguous -- a
    ``:`` or another quote (single quotes included: ``["a" 'b' "c"]``)
    means a key or a missing comma may be starting,
    and so does the start of any JSON value -- a literal, a number, ``[]``,
    ``{"`` (``["first" null "second"]`` lost its commas; read as text, three
    values become one string). Either reading of such a quote could swallow
    or invent a value. Inside an array every quote that does not close the
    string is ambiguous: ``["first"second"third"]`` or ``["a" b "c"]`` may
    be items that lost their commas around a bare one, whatever the
    spacing. Only in an object value, where unkeyed items are not JSON at
    all, does ``"the "foo" mode"`` have one reading.
    """
    index += 1
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    following = text[index:index + 1]
    if not following or following in ",}]":
        return True
    if in_array:
        return None
    if following in ':"\'' or _starts_json_value(text, index):
        return None
    return False


def _starts_json_value(text: str, index: int) -> bool:
    """Whether a JSON value -- not just text -- may start at ``index``.

    A whole literal or number, or a container whose first character is how
    JSON would go on (``[]``, ``["``, ``[1``, ``{}``, ``{"``). A citation
    ``[k1]`` or LaTeX ``{x}`` after a quote is text: no JSON value starts
    with ``k`` or ``x``.
    """
    char = text[index:index + 1]
    if char in {"[", "{"}:
        index += 1
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        following = text[index:index + 1]
        if not following or following in '"]}' or (char == "[" and following in "[{"):
            return True
        return char == "[" and _starts_json_value(text, index)
    token = _RAW_BARE_TOKEN_RE.match(text, index)
    return token is not None and (
        token.group() in {"true", "false", "null"}
        or bool(_JSON_NUMBER_RE.fullmatch(token.group()))
    )


def _read_literally(text: str, *, inner_quotes: bool) -> _Reading | None:
    r"""Spell ``text`` so JSON decodes each string to the characters written.

    Inside a double-quoted string: a backslash that starts no JSON escape is
    doubled (see ``_escape_stray_backslashes``), and a raw control character
    (a newline or tab typed straight into an answer) is written as its JSON
    escape -- both have one reading, the character itself. With
    ``inner_quotes``, a ``"`` inside a VALUE string that is followed by text
    rather than JSON structure is escaped too (see ``_closes_value_string``),
    and such a string may only close once its quotes -- the ones already
    escaped and the ones read as text -- pair up; key strings never take
    that reading, because a key that swallowed a quote is a duplicated
    opening or a lost delimiter, not a key. Returns ``None`` when an inner
    quote has no single reading. Text outside strings
    is untouched.
    """
    out: list[str] = []
    stack: list[str] = []
    expect_key = False
    in_string = in_key = False
    stray = control = inner = False
    # Quotes in the current value string: escaped ones and those read as
    # text. A string that took the inner-quote reading may only close after
    # an even number of them (see below).
    quotes = 0
    string_inner = in_array = False
    index = 0
    while index < len(text):
        char = text[index]
        if not in_string:
            if char == '"':
                in_string, in_key, expect_key = True, expect_key, False
                quotes, string_inner = 0, False
                in_array = bool(stack) and stack[-1] == "["
            elif char in "{[":
                stack.append(char)
                expect_key = char == "{"
            elif char in "}]":
                if stack:
                    stack.pop()
                expect_key = False
            elif char == ",":
                expect_key = bool(stack) and stack[-1] == "{"
            elif char == ":":
                expect_key = False
            out.append(char)
            index += 1
            continue
        if char == "\\":
            if _starts_json_escape(text, index + 1):
                quotes += text[index + 1] == '"'
                out.append(text[index:index + 2])
                index += 2
                continue
            out.append("\\\\")
            stray = True
        elif char == '"':
            closes = True
            if inner_quotes and not in_key:
                closes = _closes_value_string(text, index, in_array=in_array)
                if closes is None:
                    return None
            if closes:
                if string_inner and quotes % 2:
                    # ``"这种模式叫"恒流"}`` or ``["a, "b"]``: an odd quote
                    # count means this quote may as well be the partner of
                    # the one read as text, and the string either ends here
                    # or ends after more text; two readings.
                    return None
                in_string = False
                out.append(char)
            else:
                out.append('\\"')
                quotes += 1
                inner = string_inner = True
        elif char < " ":
            out.append(json.dumps(char)[1:-1])
            control = True
        else:
            out.append(char)
        index += 1
    return _Reading(
        "".join(out),
        stray_backslashes=stray,
        control_chars=control,
        inner_quotes=inner,
    )


def _readings(text: str, *, inner_quotes: bool) -> list[_Reading]:
    """``text`` as written, then each literal spelling that differs from it."""
    readings = [_Reading(text)]
    for allow_inner in (False, True) if inner_quotes else (False,):
        reading = _read_literally(text, inner_quotes=allow_inner)
        if reading is not None and all(
            reading.text != seen.text for seen in readings
        ):
            readings.append(reading)
    return readings


def _repeats_prefix(
    text: str, start: int, end: int, target: str, at: int,
    budget: list[int] | None = None,
) -> bool:
    """Whether ``text[start:end]`` repeats the beginning of ``target[at:]``.

    Whitespace between tokens is skipped on both sides: a restart written as
    ``{\n{`` or a repeat with different indentation is still the same
    characters. Inside a string (tracked on ``target``, which starts outside
    one) every character must match, whitespace included: ``"a b"`` is not
    ``"a  b"``. Each compared character spends one unit of ``budget[0]``;
    an exhausted budget answers ``False``. Whitespace ending the fragment is
    never compared: it precedes the restart, whatever string state the
    fragment's stray quotes suggest (``{ " {``).
    """
    while end > start and text[end - 1].isspace():
        end -= 1
    index, other = start, at
    in_string = escaped = False
    while index < end:
        # Every character examined is charged, skipped whitespace included:
        # a long run of it ahead of many ``{`` would otherwise be rescanned
        # for free at each one.
        if budget is not None:
            budget[0] -= 1
            if budget[0] < 0:
                return False
        if not in_string and text[index].isspace():
            index += 1
            continue
        if not in_string:
            skipped = other
            while other < len(target) and target[other].isspace():
                other += 1
            if budget is not None:
                budget[0] -= other - skipped
        if other >= len(target) or target[other] != text[index]:
            return False
        char = target[other]
        if escaped:
            escaped = False
        elif char == "\\" and in_string:
            escaped = True
        elif char == '"':
            in_string = not in_string
        index += 1
        other += 1
    return True


def _is_repeat_tail(tail: str, obj: str) -> bool:
    """Whether ``tail`` is the object written again (whole or cut short).

    A model stuck in a loop writes the reply, then starts it again:
    ``{A}{A}`` or ``{A}{"answer":"一、``. Each copy repeats text already
    delivered, so dropping it loses nothing. A second object that DIFFERS
    from the first anywhere has two readings and is not a repeat, and a cut
    copy may not be followed by anything (closers after it would make it a
    different, complete object).
    """
    rest = tail.strip()
    if not rest.startswith("{"):
        return False
    index = 0
    while (end := _copy_end(rest, index, obj)) is not None:
        index = end
        while index < len(rest) and rest[index].isspace():
            index += 1
    if index == len(rest) or _TRAILING_NOISE_RE.fullmatch(rest, index):
        return True
    return _repeats_prefix(rest, index, len(rest), obj, 0)


def _copy_end(text: str, index: int, obj: str) -> int | None:
    """End offset of a whole copy of ``obj`` written at ``text[index:]``.

    Whitespace between tokens may differ (a repeat re-indented), as in
    ``_repeats_prefix``; inside strings every character must match.
    Returns ``None`` when ``text`` does not repeat all of ``obj`` there.
    """
    other = 0
    in_string = escaped = False
    while other < len(obj):
        char = obj[other]
        if not in_string and char.isspace():
            other += 1
            continue
        if not in_string:
            while index < len(text) and text[index].isspace():
                index += 1
        if index >= len(text) or text[index] != char:
            return None
        if escaped:
            escaped = False
        elif char == "\\" and in_string:
            escaped = True
        elif char == '"':
            in_string = not in_string
        index += 1
        other += 1
    return index


def _leading_object(
    readings: list[_Reading],
) -> tuple[_Reading, str, bool] | None:
    """Return ``(reading, object, trailing_data)`` for the first reading that
    decodes to a complete leading object followed by nothing, by stray
    closers, or by a repeat of itself."""
    for reading in readings:
        try:
            value, end = _DECODER.raw_decode(reading.text)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        obj, tail = reading.text[:end], reading.text[end:]
        if not tail:
            return reading, obj, False
        if _TRAILING_NOISE_RE.fullmatch(tail) or _is_repeat_tail(tail, obj):
            return reading, obj, True
    return None


_RESTART_POINTS_MAX = 8


def _restart_points(text: str) -> list[int]:
    """Offsets where the model restarted the reply it had begun.

    ``{"{"answer":…``, ``{"answer{"answer":…``, ``{"answer":"{"answer":…``
    and ``{"answer":"一、先澄{"answer":"一、先澄清…`` (DeepSeek-V4-Flash
    answer syntheses, 2026-10-09 and 2026-10-14) all open with a fragment
    that the text after it repeats from its first character. A ``{`` is a
    restart point when the text since the reply's opening or since an
    earlier restart point is repeated, character for character, by what
    follows it. A nested object never qualifies by accident: the fragment
    before it (``{"sections":[``) is not how the nested object begins, and a
    ``{`` right after ``:``, ``[`` or ``,`` is where a nested value may
    start (``{"answer":{"answer":…}}``), which is a second reading.

    Returns ``(fragments dropped, offset)`` pairs, earliest first. The scan
    stays linear on a reply stuck repeating ``{`` or one short fragment: each
    fragment is compared only from the latest restart point, so its cost is
    bounded by its own length, and only the ``_RESTART_POINTS_MAX`` latest
    points are kept for decoding (only a late one can decode when the model
    restarted many times). The comparison budget, which charges skipped
    whitespace too, is what bounds a reply whose ``{`` are separated by long
    whitespace runs (codex #875 R1): without it that scan is quadratic.
    """
    start = found = 0
    points: list[tuple[int, int]] = []
    budget = [4 * len(text) + 4096]
    index = text.find("{", 1)
    while index != -1 and budget[0] > 0:
        before = index - 1
        while before > 0 and text[before].isspace():
            before -= 1
        if text[before] not in ":[," and _repeats_prefix(
            text, start, index, text, index, budget,
        ):
            start = index
            found += 1
            points.append((found, index))
            del points[:-_RESTART_POINTS_MAX]
        index = text.find("{", index + 1)
    return points


def _restarted_object(
    text: str, example: dict[str, Any],
) -> tuple[_Reading, str, bool] | None:
    """Return ``(reading, object, trailing_data)`` for a reply that restarted.

    The fragments before the restart are dropped before strings are read:
    the stray quote of ``{"{`` flips every quote after it, so reading
    backslashes or inner quotes over the whole reply would misplace string
    boundaries. The object must share a top-level field with the hint;
    without a hint nothing corroborates that it is the reply rather than a
    nested value. After the object only whitespace, quotes and one ``}`` per
    dropped fragment (the model closing what it opened) may follow: a ``]``
    would mean the lost fragment opened an array -- ``{"sections":[{...}]}``
    missing ``"sections":[`` -- and the object is a nested item.
    """
    for dropped, point in _restart_points(text):
        for reading in _readings(text[point:], inner_quotes=True):
            try:
                value, end = _DECODER.raw_decode(reading.text)
            except ValueError:
                continue
            if not isinstance(value, dict) or not set(value).intersection(example):
                break
            tail = reading.text[end:]
            if tail.strip(' \t\r\n"}') or tail.count("}") > dropped:
                break
            return reading, reading.text[:end], bool(tail.strip())
    return None


def _single_reading_object(
    content: str, schema_hint: str,
) -> ModelJsonObject | None:
    """The reply as the one object it can be read as, or ``None``.

    Tried in order, each only when the previous found nothing: the reply
    with stray backslashes and raw control characters read literally (and
    stray closers or a repeat after it dropped); a restarted reply; then
    unescaped quotes inside value strings read as characters of the text.
    The inner-quote reading comes last because it is the only one that moves
    a string boundary the model wrote.
    """
    text = content.strip()
    if not text.startswith("{"):
        return None
    found = _leading_object(_readings(text, inner_quotes=False))
    duplicated = False
    if found is None:
        example = _schema_example(schema_hint)
        if example:
            found = _restarted_object(text, example)
            duplicated = found is not None
    if found is None:
        found = _leading_object([
            reading for reading in _readings(text, inner_quotes=True)
            if reading.inner_quotes
        ])
    if found is None:
        return None
    reading, obj, trailing = found
    return ModelJsonObject(
        content=obj,
        trailing_data=trailing,
        stray_backslashes=reading.stray_backslashes,
        duplicated_open=duplicated,
        control_chars=reading.control_chars,
        inner_quotes=reading.inner_quotes,
    )


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
        # Not repairs: what the model wrote is delivered with its one reading
        # (see ``_single_reading_object``), so this applies to every workload
        # regardless of repair mode.
        absorbed = _single_reading_object(content, schema_hint)
        if absorbed is not None:
            return absorbed
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
    # Stray backslashes get their one reading here rather than whatever the
    # repair library does with them (a reply whose only fault they were never
    # reaches this point; see above).
    try:
        repaired = json_repair.loads(
            _escape_stray_backslashes(stripped), skip_json_loads=True,
        )
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
