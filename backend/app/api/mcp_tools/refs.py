"""Opaque ``ref`` handles for ``read_reference``.

A ref is ``base64url(json)`` without padding. Three kinds exist:

* ``{"k":"el","n":<notebook_id>,"s":<source_id>,"e":<element_id>}`` -- a
  source element, read in the context of notebook ``n`` (its own sources plus
  the reference libraries it mounts);
* ``{"k":"gel","j":<job_id>,"e":<element_id>}`` -- an element cited by a
  global Ask job;
* ``{"k":"mem","n":<notebook_id>,"m":<memory_id>}`` -- one of the token
  owner's Memory items in notebook ``n``.

A ref is NOT signed and grants nothing: ``read_reference`` re-runs, on every
call, the complete authorization the original per-kind read tool ran. It only
spares the Agent from carrying three different id tuples around. A ref that
does not decode to exactly one of the shapes above is ``invalid_argument``.
"""
from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from ._shared import AgentToolError


#: Kind -> its id fields, in encoding order.
_REF_FIELDS: dict[str, tuple[str, ...]] = {
    "el": ("n", "s", "e"),
    "gel": ("j", "e"),
    "mem": ("n", "m"),
}
# Upper bound on an accepted ref; generous for three ids, small enough that a
# pasted document is rejected before any JSON parsing.
REF_MAX_CHARS = 2_000


def encode_ref(kind: str, **ids: str) -> str:
    fields = _REF_FIELDS[kind]
    payload = {"k": kind, **{field: str(ids[field]) for field in fields}}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def element_ref(notebook_id: str, source_id: str, element_id: str) -> str | None:
    if not (notebook_id and source_id and element_id):
        return None
    return encode_ref("el", n=notebook_id, s=source_id, e=element_id)


def memory_ref(notebook_id: str, memory_id: str) -> str | None:
    if not (notebook_id and memory_id):
        return None
    return encode_ref("mem", n=notebook_id, m=memory_id)


def global_element_ref(job_id: str, element_id: str) -> str | None:
    if not (job_id and element_id):
        return None
    return encode_ref("gel", j=job_id, e=element_id)


_INVALID_REF = "ref 无法解析：请原样传入 search、ask 或 get_ask 返回的 ref"


def decode_ref(ref: Any) -> tuple[str, dict[str, str]]:
    """``(kind, ids)`` for a well-formed ref, else ``invalid_argument``."""
    if not isinstance(ref, str) or not ref.strip() or len(ref) > REF_MAX_CHARS:
        raise AgentToolError("invalid_argument", _INVALID_REF)
    text = ref.strip()
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise AgentToolError("invalid_argument", _INVALID_REF) from None
    if not isinstance(payload, dict):
        raise AgentToolError("invalid_argument", _INVALID_REF)
    kind = payload.get("k")
    fields = _REF_FIELDS.get(kind) if isinstance(kind, str) else None
    if fields is None or set(payload) != {"k", *fields}:
        raise AgentToolError("invalid_argument", _INVALID_REF)
    ids = {field: payload[field] for field in fields}
    # An id that is blank after stripping would fall back to a default
    # (the token's notebook) downstream: refused here instead.
    if not all(isinstance(value, str) and value.strip() for value in ids.values()):
        raise AgentToolError("invalid_argument", _INVALID_REF)
    return kind, ids
