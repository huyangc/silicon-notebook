"""Config-driven discovery of deployment-owned, out-of-repo plugin bundles.

Three properties define this module and none of them may be softened:

1. **Named list only.**  ``EXTENSIONS_CONFIG`` points at one TOML file whose
   ``[extensions.<plugin id>]`` tables are the *complete* set of loadable
   plugins.  Nothing here scans a directory, reads entry points, or consults a
   second environment variable.  An entry with ``enabled = false`` is not even
   imported.

2. **Fail closed, at startup.**  Every rejection raises
   :class:`ExtensionDiscoveryError`, which the composition root re-raises so
   ``create_app()`` — and therefore the process — refuses to start.  A
   deployment plugin is never silently skipped: a half-loaded topology is far
   worse than a service that will not boot.

3. **Secret hygiene.**  ``[extensions.<id>.settings]`` routinely carries API
   keys and internal hostnames.  ``ExtensionDiscoveryError`` therefore carries
   only a plugin id, a stable reason code, an exception *class* name, and (for
   settings/config-shape failures) offending **key** names.  Underlying
   exceptions are raised ``from None`` and are never stringified: pydantic's
   ``ValidationError`` echoes the rejected input value, so ``str(exc)`` on a
   settings failure would print the secret into logs and tracebacks.

Capability naming: a plugin-provided capability name is a stable metadata id
(``_STABLE_ID`` below) — lowercase, dot/underscore/hyphen separated, e.g.
``corp.ieee.available``.  A colon is **not** accepted here even though core's
own nine capability names read ``point:name``: that spelling is reserved for
core-owned decisions, so a plugin can never mint a name that looks like one of
them.  (The conflict checks in ``capability_decisions_from_bundles`` are the
enforcement; the reserved separator is what keeps the two namespaces legible.)

Import boundary: this module imports only ``app.extension_sdk``, third-party
packages, and the standard library.  It must never reach into services,
repositories, the API layer, or ``app.core`` — deployment plugins are wired by
``app.extensions.bootstrap``, which owns the (lazy) Settings read.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import importlib
import logging
import re
import tomllib

from pydantic import BaseModel

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    AvailabilityProbe,
    ExtensionManifest,
)


# Every deployment-plugin rejection is logged through this one logger name —
# here and in the composition root — so an operator filters exactly one thing.
# The record carries a plugin id, a stable reason code, and an exception *class*
# name and nothing else: never ``exc.names``, ``str(exc)``, a module path, a
# file path, or any settings value.
DISCOVERY_LOGGER = logging.getLogger("silicon_notebook.extensions")

# Same stable-metadata-id shape the registry enforces on manifest ids: all
# accepted values are URL-path-segment safe.  Note it excludes ``:`` — see the
# capability-naming paragraph in the module docstring.
_STABLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_TOP_LEVEL_KEYS = frozenset({"extensions"})
_ENTRY_KEYS = frozenset({"bundle", "enabled", "settings"})


class ExtensionDiscoveryError(RuntimeError):
    """A deployment plugin was rejected; the process must not start.

    The rendered message is deliberately narrow — plugin id, stable reason
    code, offending key *names*, and an exception class name.  It carries no
    settings value, module path, file path, or upstream exception text.
    """

    def __init__(
        self,
        plugin_id: str,
        reason: str,
        *,
        exception_type: str = "",
        names: tuple[str, ...] = (),
    ) -> None:
        self.plugin_id = plugin_id
        self.reason = reason
        self.exception_type = exception_type
        self.names = names
        subject = (
            f"extension {plugin_id!r} rejected"
            if plugin_id
            else "extension configuration rejected"
        )
        message = f"{subject}: {reason}"
        if names:
            message = f"{message} keys={list(names)!r}"
        if exception_type:
            message = f"{message} ({exception_type})"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DiscoveredExtension:
    """One loaded, structurally validated, already-configured plugin bundle."""

    plugin_id: str
    bundle: object
    settings: object | None


def _plugin_attr(obj: object, name: str, plugin_id: str, reason: str) -> object:
    """``getattr(obj, name, None)`` with the plugin's own exceptions sanitized.

    Every "read this attribute off an object this module does not yet trust"
    call below goes through here rather than a bare ``getattr``. A bundle
    author can implement any attribute checked in this module — ``manifest``,
    ``register``, ``settings_model``, ``configure``, ``capability_decisions``
    — as a ``property`` or via ``__getattribute__`` that raises. A bare
    ``getattr(obj, name, None)`` only substitutes the default for
    ``AttributeError``; every *other* exception — carrying whatever
    secret-bearing message the plugin wrote into it — propagates straight
    through this module's careful sanitization and into the startup
    traceback untouched. ``KeyboardInterrupt``/``SystemExit`` still propagate
    unchanged, matching every other rejection path in this module.
    """

    try:
        return getattr(obj, name, None)
    except Exception as exc:
        raise ExtensionDiscoveryError(
            plugin_id, reason, exception_type=type(exc).__name__
        ) from None


def discover_deployment_extensions(
    config_path: str,
) -> tuple[DiscoveredExtension, ...]:
    """Load every named, enabled plugin from ``config_path`` in plugin-id order.

    An empty path means "no deployment plugins" and the frozen topology stays
    byte-identical to the built-in tuple.  The checks below run in the order
    written, and that order *is* the failure priority: a malformed file is
    reported before a malformed entry, an entry's shape before its import, its
    import before its identity, and its identity before its settings — so the
    first thing an operator sees is the outermost thing that is wrong.
    """

    path = config_path.strip()
    if not path:
        return ()

    try:
        with open(path, "rb") as handle:
            document = tomllib.load(handle)
    except OSError as exc:
        raise ExtensionDiscoveryError(
            "", "config_unreadable", exception_type=type(exc).__name__
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise ExtensionDiscoveryError(
            "", "config_invalid_toml", exception_type=type(exc).__name__
        ) from None

    unknown_top_level = tuple(sorted(set(document) - _TOP_LEVEL_KEYS))
    if unknown_top_level:
        raise ExtensionDiscoveryError(
            "", "config_unknown_top_level_key", names=unknown_top_level
        ) from None
    entries = document.get("extensions", {})
    if not isinstance(entries, dict):
        raise ExtensionDiscoveryError(
            "", "config_extensions_not_a_table"
        ) from None

    discovered: list[DiscoveredExtension] = []
    for plugin_id in sorted(entries):
        entry = entries[plugin_id]
        if type(plugin_id) is not str or not _STABLE_ID.fullmatch(plugin_id):
            raise ExtensionDiscoveryError(
                str(plugin_id), "plugin_id_invalid"
            ) from None
        if not isinstance(entry, dict):
            raise ExtensionDiscoveryError(
                plugin_id, "plugin_entry_not_a_table"
            ) from None
        unknown_keys = tuple(sorted(set(entry) - _ENTRY_KEYS))
        if unknown_keys:
            raise ExtensionDiscoveryError(
                plugin_id, "plugin_unknown_key", names=unknown_keys
            ) from None
        # Naming a plugin is what enables it; `enabled = false` is the explicit
        # off switch and must skip the import entirely, not merely drop the
        # bundle afterwards.
        enabled = entry.get("enabled", True)
        if type(enabled) is not bool:
            raise ExtensionDiscoveryError(
                plugin_id, "plugin_enabled_not_bool"
            ) from None
        if not enabled:
            continue
        bundle = _load_bundle(plugin_id, entry)
        settings = _bind_settings(plugin_id, bundle, entry.get("settings", {}))
        discovered.append(DiscoveredExtension(plugin_id, bundle, settings))
    return tuple(discovered)


def _load_bundle(plugin_id: str, entry: Mapping[str, object]) -> object:
    """Import ``"<module>:<attribute>"`` and structurally validate the object."""

    spec = entry.get("bundle")
    if spec is None:
        raise ExtensionDiscoveryError(plugin_id, "plugin_bundle_missing") from None
    if type(spec) is not str or spec.count(":") != 1:
        raise ExtensionDiscoveryError(
            plugin_id, "plugin_bundle_spec_invalid"
        ) from None
    module_path, _, attribute = spec.partition(":")
    if not module_path or not attribute.isidentifier():
        raise ExtensionDiscoveryError(
            plugin_id, "plugin_bundle_spec_invalid"
        ) from None

    try:
        module = importlib.import_module(module_path)
    except BaseException as exc:  # plugin import may raise anything at all
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            # These two propagate untouched — an operator's Ctrl-C or a
            # ``sys.exit()`` inside plugin import code must still stop the
            # process.  But they would otherwise leave *no* trace naming which
            # plugin's import the interpreter was inside when it happened, so
            # record that one fact (id + stable reason, no exception text)
            # before getting out of the way.
            DISCOVERY_LOGGER.error(
                "extension import interrupted: plugin=%s reason=%s",
                plugin_id,
                "plugin_module_import_interrupted",
            )
            raise
        raise ExtensionDiscoveryError(
            plugin_id,
            "plugin_module_import_failed",
            exception_type=type(exc).__name__,
        ) from None
    try:
        bundle = getattr(module, attribute)
    except Exception as exc:
        # A module-level ``__getattr__`` may raise anything, not only
        # ``AttributeError``; either way the named object is unusable.
        raise ExtensionDiscoveryError(
            plugin_id,
            "plugin_attribute_missing",
            exception_type=type(exc).__name__,
        ) from None

    manifest = _plugin_attr(bundle, "manifest", plugin_id, "plugin_not_a_bundle")
    if type(manifest) is not ExtensionManifest or not callable(
        _plugin_attr(bundle, "register", plugin_id, "plugin_not_a_bundle")
    ):
        raise ExtensionDiscoveryError(plugin_id, "plugin_not_a_bundle") from None
    if manifest.id != plugin_id:
        raise ExtensionDiscoveryError(plugin_id, "plugin_id_mismatch") from None
    if manifest.trust != "deployment":
        raise ExtensionDiscoveryError(
            plugin_id, "plugin_trust_not_deployment"
        ) from None
    if manifest.api_version != EXTENSION_API_VERSION:
        raise ExtensionDiscoveryError(
            plugin_id, "plugin_api_version_unsupported"
        ) from None
    return bundle


def _bind_settings(
    plugin_id: str, bundle: object, table: object
) -> object | None:
    """Validate the ``[settings]`` table and hand it to the bundle.

    Core computes the accepted key set itself from ``model_fields`` rather than
    trusting the plugin to declare ``extra="forbid"``: fail-closed behavior must
    not rest on a plugin author's diligence, and a silently ignored key in a
    deployment config is exactly the class of mistake that surfaces months later
    as "the feature was never actually on".

    ``configure`` runs here — inside discovery, before ``register()`` — so a
    configuration error is always reported before any topology error.
    """

    if not isinstance(table, dict):
        # Guard before the key-set difference below: ``set("secret")`` would
        # turn a scalar settings value into a set of its own characters and
        # report them as "unknown keys".
        raise ExtensionDiscoveryError(
            plugin_id, "plugin_settings_not_a_table"
        ) from None
    model = _plugin_attr(
        bundle, "settings_model", plugin_id, "plugin_settings_binding_missing"
    )
    configure = _plugin_attr(
        bundle, "configure", plugin_id, "plugin_settings_binding_missing"
    )
    if model is None and configure is None:
        if table:
            raise ExtensionDiscoveryError(
                plugin_id, "plugin_settings_not_accepted"
            ) from None
        return None
    if model is None or configure is None or not callable(configure):
        raise ExtensionDiscoveryError(
            plugin_id, "plugin_settings_binding_missing"
        ) from None
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise ExtensionDiscoveryError(
            plugin_id, "plugin_settings_model_invalid"
        ) from None

    accepted: set[str] = set()
    for name, field in model.model_fields.items():
        accepted.add(name)
        for alias in (
            getattr(field, "validation_alias", None),
            getattr(field, "alias", None),
        ):
            # Only plain string aliases widen the accepted set.  Registered
            # limitation: pydantic also allows ``AliasChoices``/``AliasPath``
            # here, and those names are *not* collected — a settings key that
            # only matches such an alias is reported as
            # ``plugin_settings_unknown_key`` instead of being accepted.  That
            # is the fail-closed direction (a rejected startup, not a silently
            # ignored key), and enumerating those shapes would mean this module
            # reimplementing pydantic's alias resolution.
            if type(alias) is str:
                accepted.add(alias)
    unknown_keys = tuple(sorted(set(table) - accepted))
    if unknown_keys:
        raise ExtensionDiscoveryError(
            plugin_id, "plugin_settings_unknown_key", names=unknown_keys
        ) from None

    try:
        instance = model.model_validate(dict(table))
    except Exception as exc:
        # `from None` and the class-name-only rendering are load-bearing:
        # pydantic's ValidationError repeats the rejected input value.
        raise ExtensionDiscoveryError(
            plugin_id,
            "plugin_settings_invalid",
            exception_type=type(exc).__name__,
        ) from None
    try:
        configure(instance)
    except Exception as exc:
        raise ExtensionDiscoveryError(
            plugin_id,
            "plugin_settings_binding_failed",
            exception_type=type(exc).__name__,
        ) from None
    return instance


def capability_decisions_from_bundles(
    bundles: Sequence[object],
    core_decisions: Mapping[str, AvailabilityProbe],
) -> dict[str, AvailabilityProbe]:
    """Merge plugin-provided capability probes into the core decision catalog.

    This is what lets a deployment plugin's own ``requires``/``ui_contributions``
    freeze at all: the registry validates each named capability against the
    catalog, so a plugin that both declares and supplies a capability must have
    its probe present *before* ``freeze()``.

    Every bundle is checked — built-in ones too — so the invariants below hold
    for the whole topology rather than only for the out-of-repo half.  A name
    collision with core, or between two plugins, is a startup failure: silently
    letting one probe shadow another would make availability depend on
    registration order.
    """

    merged: dict[str, AvailabilityProbe] = dict(core_decisions)
    claimed: dict[str, str] = {}
    for bundle in bundles:
        # ``plugin_id`` is not known yet -- deriving it is exactly what this
        # access is for -- so a raising ``manifest`` property is reported
        # under an empty plugin id, same as every other pre-identification
        # rejection in this module (e.g. ``config_unreadable``).
        manifest = _plugin_attr(
            bundle, "manifest", "", "plugin_attribute_access_failed"
        )
        plugin_id = getattr(manifest, "id", "") if manifest is not None else ""
        provides = getattr(manifest, "provides", ()) if manifest is not None else ()
        decisions = _plugin_attr(
            bundle,
            "capability_decisions",
            plugin_id,
            "plugin_capability_declaration_invalid",
        )
        if decisions is None:
            if provides:
                raise ExtensionDiscoveryError(
                    plugin_id, "plugin_capability_declaration_invalid"
                ) from None
            decisions = {}
        elif not isinstance(decisions, Mapping):
            raise ExtensionDiscoveryError(
                plugin_id, "plugin_capability_declaration_invalid"
            ) from None
        else:
            # ``isinstance(..., Mapping)`` only proves the shape is registered,
            # not that iterating it is safe: a plugin-supplied Mapping may raise
            # from ``__iter__``/``__getitem__``, and every read below (``set``,
            # ``tuple``, ``decisions[name]``) would then let that exception —
            # whose message the plugin wrote — escape unsanitized.  Materialize
            # once, here, behind the same conversion as every other rejection.
            try:
                decisions = dict(decisions)
            except Exception as exc:
                raise ExtensionDiscoveryError(
                    plugin_id,
                    "plugin_capability_declaration_invalid",
                    exception_type=type(exc).__name__,
                ) from None

        for name in tuple(provides) + tuple(decisions):
            if type(name) is not str or not _STABLE_ID.fullmatch(name):
                raise ExtensionDiscoveryError(
                    plugin_id, "plugin_capability_name_invalid"
                ) from None
        undeclared = tuple(sorted(set(decisions) - set(provides)))
        if undeclared:
            raise ExtensionDiscoveryError(
                plugin_id, "plugin_capability_not_declared", names=undeclared
            ) from None
        missing = tuple(sorted(set(provides) - set(decisions)))
        if missing:
            raise ExtensionDiscoveryError(
                plugin_id, "plugin_capability_missing_decision", names=missing
            ) from None

        for name in sorted(provides):
            probe = decisions[name]
            if not callable(probe):
                raise ExtensionDiscoveryError(
                    plugin_id, "plugin_capability_declaration_invalid", names=(name,)
                ) from None
            if name in core_decisions:
                raise ExtensionDiscoveryError(
                    plugin_id, "plugin_capability_conflicts_core", names=(name,)
                ) from None
            if name in claimed:
                raise ExtensionDiscoveryError(
                    plugin_id, "plugin_capability_conflicts_plugin", names=(name,)
                ) from None
            claimed[name] = plugin_id
            merged[name] = probe
    return merged


__all__ = [
    "DISCOVERY_LOGGER",
    "DiscoveredExtension",
    "ExtensionDiscoveryError",
    "capability_decisions_from_bundles",
    "discover_deployment_extensions",
]
