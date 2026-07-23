"""Backend-neutral rendered knowhow asset-reference delta helpers."""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence


_ASSET_IMAGE_REF_RE = re.compile(r"!\[[^\]]*\]\(asset://([A-Za-z0-9_-]+)\)")


def rendered_asset_ids(content_md: str) -> frozenset[str]:
    """Return only live rendered-image refs, not prose/code literals."""
    return frozenset(_ASSET_IMAGE_REF_RE.findall(str(content_md or "")))


def required_asset_ids(
    explicit: Sequence[str], changes: Iterable[tuple[str, str]]
) -> tuple[str, ...]:
    """Canonical refs that a write newly introduces.

    ``changes`` contains ``(old_content, new_content)`` pairs for every actual
    target. A legacy dead ref retained by an unrelated edit is not revalidated;
    adding that same ref to another target still is. Caller-supplied ids remain
    authoritative and are unioned with the computed deltas.
    """
    required = {str(asset_id) for asset_id in explicit if str(asset_id)}
    for old_content, new_content in changes:
        required.update(
            rendered_asset_ids(new_content) - rendered_asset_ids(old_content)
        )
    return tuple(sorted(required))


__all__ = ["rendered_asset_ids", "required_asset_ids"]
