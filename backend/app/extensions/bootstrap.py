"""Composition entry point for the startup-frozen extension topology."""
from __future__ import annotations

from collections.abc import Iterable

from app.extension_sdk import ExtensionBundle
from app.extensions.registry import ExtensionRegistry, frozen_registry


def build_extension_registry(
    bundles: Iterable[ExtensionBundle] = (),
) -> ExtensionRegistry:
    return frozen_registry(bundles)
