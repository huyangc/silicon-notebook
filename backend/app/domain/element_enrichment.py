"""Shared core admission rules for persisted element-enrichment provenance.

Both halves of the feature read these: the extension host, which decides what
a contribution may propose, and the ingestion adapter, which decides what is
written.  Keeping them here means the byte budget an operator configures and
the bytes actually persisted are computed by one function rather than two
drifting copies, and neither half has to import the Extension SDK to do it.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import math
import re


# Structural protocol bounds, not deployment-tunable product limits.  They keep
# manifest provenance from bypassing the point-wide persisted-byte budget: the
# owner envelope is written *inside* the subtree that budget measures, so an
# unbounded plugin id would otherwise buy unbounded persisted bytes.
EXTENSION_OWNER_ID_MAX_CHARS = 128
EXTENSION_OWNER_VERSION_MAX_CHARS = 64
# How deep a proposed ``metadata`` mapping may nest.  A bound rather than a
# preference: the admission walk, the JSON encoder used to size the subtree,
# and every later reader of that subtree all recurse over plugin-supplied
# structure.
ELEMENT_ENRICHMENT_METADATA_MAX_DEPTH = 12

_STABLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_METADATA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def valid_element_enrichment_owner(
    plugin_id: object,
    plugin_version: object,
    contribution_id: object,
) -> bool:
    """True when this provenance triple is safe to persist verbatim.

    ``plugin_id`` and ``contribution_id`` are held to the stable-id shape
    because they become *keys* — a contribution id is what a later reader
    indexes the ``extensions`` subtree by.  ``plugin_version`` is only a
    value, so it is held to what actually matters for persistence: present,
    bounded, and free of control characters.  The registry itself asks only
    that a version be non-empty, and a decorative one ("1.0.0-rc1 (build 7)")
    must not take a whole deployment's backend down at composition.
    """

    return (
        type(plugin_id) is str
        # Length precedes the regex so an arbitrarily long value costs O(1).
        and 0 < len(plugin_id) <= EXTENSION_OWNER_ID_MAX_CHARS
        and _STABLE_ID.fullmatch(plugin_id) is not None
        and type(contribution_id) is str
        and 0 < len(contribution_id) <= EXTENSION_OWNER_ID_MAX_CHARS
        and _STABLE_ID.fullmatch(contribution_id) is not None
        and valid_element_enrichment_version(plugin_version)
    )


def valid_element_enrichment_version(plugin_version: object) -> bool:
    """True when a manifest version is safe to persist as a plain value."""

    return (
        type(plugin_version) is str
        and 0 < len(plugin_version) <= EXTENSION_OWNER_VERSION_MAX_CHARS
        and all(char.isprintable() for char in plugin_version)
    )


def thaw_element_enrichment_metadata(
    value: object, *, max_nodes: int | None = None
) -> object:
    """Validate plugin metadata into a plain, JSON-encodable structure.

    The result is built from scratch out of ``dict``/``list`` and JSON scalars
    — never the plugin's own containers — so it is safe to persist, safe to
    ``json.dumps`` and immune to a mapping that changes what it returns on a
    second read.  Admission rules, all of them shared by the extension host
    and the ingestion adapter:

    * keys match ``^[a-z][a-z0-9_]{0,63}$``;
    * values are ``None``/``bool``/``int``/``str``, finite ``float``, a
      sequence, or a mapping;
    * nesting is at most ``ELEMENT_ENRICHMENT_METADATA_MAX_DEPTH`` deep.

    Raises ``TypeError`` for a value of a type that is not JSON at all and
    ``ValueError`` for a structural violation (depth, key shape, a non-finite
    float, or more than ``max_nodes`` nodes).  ``max_nodes`` bounds the walk
    itself: callers pass their persisted-byte budget, since every node costs
    at least one byte once encoded, so a structure with more nodes than that
    could never fit regardless of what the walk would find.
    """

    return _thawed(
        value,
        budget=[max_nodes if type(max_nodes) is int else None],
        depth=0,
    )


def _thawed(value: object, *, budget: list[int | None], depth: int) -> object:
    if depth > ELEMENT_ENRICHMENT_METADATA_MAX_DEPTH:
        raise ValueError("element enrichment metadata nests too deeply")
    remaining = budget[0]
    if remaining is not None:
        if remaining <= 0:
            raise ValueError("element enrichment metadata has too many nodes")
        budget[0] = remaining - 1
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        # NaN and the infinities are not JSON; every consumer of this subtree
        # would either fail or silently write a non-standard literal.
        if not math.isfinite(value):
            raise ValueError("element enrichment metadata float must be finite")
        return value
    if type(value) is list or type(value) is tuple:
        return [
            _thawed(entry, budget=budget, depth=depth + 1) for entry in value
        ]
    if isinstance(value, Mapping):
        thawed: dict[str, object] = {}
        for key, entry in value.items():
            if type(key) is not str or not _METADATA_KEY.fullmatch(key):
                raise ValueError("element enrichment metadata key is not stable")
            thawed[key] = _thawed(entry, budget=budget, depth=depth + 1)
        return thawed
    raise TypeError("element enrichment metadata must be strict JSON")


def persisted_element_enrichment_size(
    *,
    plugin_id: str,
    plugin_version: str,
    contribution_id: str,
    metadata: object,
    description: str,
) -> int:
    """UTF-8 bytes one candidate adds to the element it enriches.

    That is the complete ``extensions`` subtree keyed by ``contribution_id``
    — owner envelope included, because provenance is persisted beside the
    payload and a budget that ignored it would not bound what is written —
    plus the ``description``.

    The description is counted **twice**: the ingestion adapter writes it into
    the element's own ``description`` *and* appends a whitespace-flattened
    copy to the element's ``text`` so the chunk pipeline can retrieve it.  One
    copy is sized inside the envelope below (which also charges for JSON
    quoting and escaping) and the second is added as raw encoded bytes.

    ``metadata`` must already be plain JSON — the output of
    ``thaw_element_enrichment_metadata`` — since this encodes it.
    """

    payload: dict[str, object] = {
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "metadata": metadata,
    }
    if description:
        payload["description"] = description
    envelope = len(
        json.dumps(
            {contribution_id: payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return envelope + len(description.encode("utf-8"))


__all__ = [
    "ELEMENT_ENRICHMENT_METADATA_MAX_DEPTH",
    "EXTENSION_OWNER_ID_MAX_CHARS",
    "EXTENSION_OWNER_VERSION_MAX_CHARS",
    "persisted_element_enrichment_size",
    "thaw_element_enrichment_metadata",
    "valid_element_enrichment_owner",
    "valid_element_enrichment_version",
]
