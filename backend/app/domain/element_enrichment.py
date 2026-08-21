"""Shared core admission rules for persisted element-enrichment provenance."""
from __future__ import annotations

import json
import re


# Structural protocol bounds, not deployment-tunable product limits.  They keep
# manifest provenance from bypassing the point-wide persisted-byte budget.
EXTENSION_OWNER_ID_MAX_CHARS = 128
EXTENSION_OWNER_VERSION_MAX_CHARS = 64

_STABLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_STABLE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")


def valid_element_enrichment_owner(
    plugin_id: object,
    plugin_version: object,
    contribution_id: object,
) -> bool:
    return (
        type(plugin_id) is str
        and 0 < len(plugin_id) <= EXTENSION_OWNER_ID_MAX_CHARS
        and _STABLE_ID.fullmatch(plugin_id) is not None
        and type(contribution_id) is str
        and 0 < len(contribution_id) <= EXTENSION_OWNER_ID_MAX_CHARS
        and _STABLE_ID.fullmatch(contribution_id) is not None
        and type(plugin_version) is str
        and 0 < len(plugin_version) <= EXTENSION_OWNER_VERSION_MAX_CHARS
        and _STABLE_VERSION.fullmatch(plugin_version) is not None
    )


def persisted_element_enrichment_size(
    *,
    plugin_id: str,
    plugin_version: str,
    contribution_id: str,
    metadata: object,
    caption: str,
) -> int:
    """Return UTF-8 bytes for the complete subtree added under ``extensions``."""

    payload: dict[str, object] = {
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "metadata": metadata,
    }
    if caption:
        payload["caption"] = caption
    return len(
        json.dumps(
            {contribution_id: payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
