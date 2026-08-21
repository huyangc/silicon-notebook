"""Extension composition and runtime registry."""

from app.extensions.bootstrap import build_extension_registry
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError

__all__ = [
    "ExtensionRegistry",
    "ExtensionRegistryError",
    "build_extension_registry",
]
