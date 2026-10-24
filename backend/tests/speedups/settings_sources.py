"""Avoid repeated scalar-shape introspection in test Settings sources.

For the verified Pydantic versions, the exact builtin ``str``, ``int``,
``float`` and ``bool`` annotations are always non-complex. Metadata cannot
turn one into a JSON container: even ``Json`` returns non-complex. Returning
that immutable classification avoids reflecting on 423 scalar fields twice
per Settings construction. Nothing caches settings, environment/file reads,
aliases, validators or parsed values.

Only the application's exact Settings class and the two plain env sources
qualify, with the original public classifier still installed. Every current
annotation is checked by identity; custom types, Annotated, aliases, subclasses
and changed classifier methods take the real implementation. Pin tests compare
all fields and exercise mutation/fallback boundaries. A dependency upgrade
disables the shim until those versions are reviewed again.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Iterator

import pydantic
import pydantic_settings
from annotated_types import Ge, Gt, Le
from pydantic import Json
from pydantic.fields import FieldInfo
from pydantic_settings import ForceDecode, NoDecode
from pydantic_settings.sources.base import PydanticBaseEnvSettingsSource
from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource
from pydantic_settings.sources.providers.env import EnvSettingsSource

from app.core.config import Settings

VERIFIED_PYDANTIC_VERSION = "2.12.4"
VERIFIED_SETTINGS_VERSION = "2.16.0"
ENV_SWITCH = "SILICON_NOTEBOOK_TEST_SETTINGS_SCALARS"

_real_field_is_complex = EnvSettingsSource._field_is_complex
_real_public_classifier = PydanticBaseEnvSettingsSource.field_is_complex
_disabled_depth = 0
_lock = threading.Lock()


def install(*, force: bool = False) -> bool:
    """Install once; force bypasses only the off switch, never version guards."""
    if not force and os.environ.get(ENV_SWITCH, "").strip() == "0":
        return False
    if (
        pydantic.__version__ != VERIFIED_PYDANTIC_VERSION
        or pydantic_settings.__version__ != VERIFIED_SETTINGS_VERSION
    ):
        return False
    if installed():
        return True
    if EnvSettingsSource._field_is_complex is not _real_field_is_complex:
        return False
    EnvSettingsSource._field_is_complex = _field_is_complex
    return True


def installed() -> bool:
    return EnvSettingsSource._field_is_complex is _field_is_complex


def uninstall() -> None:
    if installed():
        EnvSettingsSource._field_is_complex = _real_field_is_complex


@contextmanager
def disabled() -> Iterator[None]:
    """Use real classification while retaining fresh sources and validation."""
    global _disabled_depth
    with _lock:
        _disabled_depth += 1
    try:
        yield
    finally:
        with _lock:
            _disabled_depth -= 1


def _field_is_complex(self, field):
    if (
        _disabled_depth
        or self.settings_cls is not Settings
        or (type(self) is not EnvSettingsSource and type(self) is not DotEnvSettingsSource)
        or type(field) is not FieldInfo
    ):
        return _real_field_is_complex(self, field)
    annotation = field.annotation
    if (
        getattr(self.field_is_complex, "__func__", None) is _real_public_classifier
        and (
            annotation is str or annotation is int
            or annotation is float or annotation is bool
        )
        and _ordinary_metadata(field.metadata)
    ):
        return False, False
    return _real_field_is_complex(self, field)


def _ordinary_metadata(metadata) -> bool:
    """Do not bypass custom metadata's observable isinstance/iteration hooks."""
    if type(metadata) is not list:
        return False
    for item in metadata:
        kind = type(item)
        if not (
            kind is Ge or kind is Gt or kind is Le or kind is Json
            or item is NoDecode or item is ForceDecode
        ):
            return False
    return True
