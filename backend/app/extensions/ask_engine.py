"""Frozen provider host for deployment-defined ``ask.engine`` modes.

The host owns topology, availability and exception sanitization.  Per-run
retrieval/model/trace ports are built by the Ask application service and are
the only authorities forwarded to a provider.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Callable

from app.domain.cancellation import AskCancelled
from app.domain.ask import AskMode
from app.domain.ask_engine import (
    AskPluginEngineError,
    safe_plugin_engine_error_code,
)
from app.extension_sdk import (
    ASK_ENGINE_POINT,
    AvailabilityStatus,
    AskEngineAvailabilityContext,
    AskEngineContext,
    AskEngineDescriptor,
    AskEnginePortError,
    AskEngineResult,
    ContributionKind,
    EngineModelPort,
    EngineTraceSink,
    RetrievalAccessPort,
)
from app.extensions.discovery import ExtensionDiscoveryError
# ASK_ENGINE_POINT is a ContributionKind.PROVIDER set (not a provider chain).
from app.extensions.registry import (
    ExtensionRegistry,
    ExtensionRegistryError,
    RegisteredContribution,
)


# Keep the point's registry kind next to the point-specific host so SDK docs
# and static contract reflection cannot mistake the generic registry's parser
# chain branch for this provider set.
ASK_ENGINE_CONTRIBUTION_KIND = ContributionKind.PROVIDER


ASK_ENGINE_MODE_ID_MAX_CHARS = 128
ASK_ENGINE_LABEL_MAX_CHARS = 80
ASK_ENGINE_DESCRIPTION_MAX_CHARS = 300
_MODE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")


@dataclass(frozen=True, slots=True)
class RegisteredAskEngine:
    plugin_id: str
    contribution_id: str
    descriptor: AskEngineDescriptor


class AskEngineHost:
    """Validate and execute the startup-frozen Ask engine provider set."""

    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError("Ask engine host requires a frozen registry")
        manifest_by_id = {manifest.id: manifest for manifest in registry.manifests()}
        registrations: list[RegisteredAskEngine] = []
        providers: dict[str, RegisteredContribution] = {}
        for item in registry.contributions(ASK_ENGINE_POINT):
            try:
                descriptor = _validated_descriptor(item)
            except BaseException as exc:  # startup boundary; sanitize deployment code
                manifest = manifest_by_id[item.plugin_id]
                if manifest.trust == "deployment":
                    raise ExtensionDiscoveryError(
                        item.plugin_id,
                        "invalid_ask_engine",
                        exception_type=type(exc).__name__,
                    ) from None
                if isinstance(exc, ExtensionRegistryError):
                    raise
                raise ExtensionRegistryError(
                    f"Ask engine {item.contribution.declaration.id!r} is invalid"
                ) from exc
            if descriptor.mode_id in providers:
                raise ExtensionRegistryError(
                    f"duplicate Ask engine mode {descriptor.mode_id!r}"
                )
            registrations.append(RegisteredAskEngine(
                item.plugin_id,
                item.contribution.declaration.id,
                descriptor,
            ))
            providers[descriptor.mode_id] = item
        registrations.sort(key=lambda item: item.descriptor.mode_id)
        self._registry = registry
        self._registrations = tuple(registrations)
        self._registration_by_mode = {
            item.descriptor.mode_id: item for item in registrations
        }
        self._providers = providers
        self._event_sink = event_sink
        self._clock = clock

    def has_engines(self) -> bool:
        return bool(self._registrations)

    def registrations(self) -> tuple[RegisteredAskEngine, ...]:
        return self._registrations

    def is_available(self, mode_id: str) -> bool:
        registration = self._registration_by_mode.get(mode_id)
        if registration is None:
            return False
        decision = self._registry.availability(
            registration.contribution_id,
            AskEngineAvailabilityContext(
                registration.contribution_id,
                mode_id,
            ),
        )
        return decision.status is AvailabilityStatus.AVAILABLE

    def modes(self) -> tuple[AskMode, ...]:
        return tuple(
            AskMode(
                item.descriptor.mode_id,
                "ask_plugin_engine",
                "extension",
                False,
                item.descriptor.requires_kg,
                True,
            )
            for item in self._registrations
        )

    def mode(self, mode_id: str) -> AskMode | None:
        registration = self._registration_by_mode.get(mode_id)
        if registration is None:
            return None
        descriptor = registration.descriptor
        return AskMode(
            descriptor.mode_id,
            "ask_plugin_engine",
            "extension",
            False,
            descriptor.requires_kg,
            True,
        )

    def answer(
        self,
        mode_id: str,
        context: AskEngineContext,
        retrieval: RetrievalAccessPort,
        model: EngineModelPort,
        trace: EngineTraceSink,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> AskEngineResult:
        item = self._providers.get(mode_id)
        if item is None:
            raise AskPluginEngineError("plugin_engine_unavailable")
        registration = self._registration_by_mode[mode_id]
        sink = event_sink if event_sink is not None else self._event_sink
        started = self._clock()
        status = "failed"
        reason = "plugin_engine_failed"
        citation_count = 0
        try:
            availability = self._registry.availability(
                registration.contribution_id,
                AskEngineAvailabilityContext(
                    registration.contribution_id,
                    mode_id,
                ),
            )
            if availability.status is not AvailabilityStatus.AVAILABLE:
                reason = "plugin_engine_unavailable"
                raise AskPluginEngineError(reason)
            implementation = item.contribution.implementation
            result = implementation.answer(context, retrieval, model, trace)
            if not _valid_result(result):
                reason = "invalid_plugin_engine_result"
                raise AskPluginEngineError(reason)
            status = "available"
            reason = ""
            citation_count = len(result.citations)
            return result
        except AskCancelled:
            status = "cancelled"
            reason = "plugin_engine_cancelled"
            raise
        except AskEnginePortError as exc:
            reason = safe_plugin_engine_error_code(exc.code)
            raise AskPluginEngineError(reason) from None
        except AskPluginEngineError:
            raise
        except BaseException:  # deployment code never controls persisted text
            raise AskPluginEngineError("plugin_engine_failed") from None
        finally:
            _emit(
                sink,
                plugin_id=registration.plugin_id,
                mode_id=mode_id,
                stage="answer",
                status=status,
                reason_code=reason,
                duration_ms=max(0, int((self._clock() - started) * 1000)),
                citation_count=citation_count,
            )


def _validated_descriptor(item: RegisteredContribution) -> AskEngineDescriptor:
    declaration = item.contribution.declaration
    implementation = item.contribution.implementation
    if declaration.kind is not ASK_ENGINE_CONTRIBUTION_KIND:
        raise ExtensionRegistryError(
            f"Ask engine {declaration.id!r} must be a provider"
        )
    if not callable(getattr(implementation, "answer", None)):
        raise ExtensionRegistryError(
            f"Ask engine {declaration.id!r} does not implement answer"
        )
    descriptor = getattr(implementation, "descriptor", None)
    if type(descriptor) is not AskEngineDescriptor:
        raise ExtensionRegistryError(
            f"Ask engine {declaration.id!r} has an invalid descriptor"
        )
    prefix = f"{item.plugin_id}."
    if (
        type(descriptor.mode_id) is not str
        or len(descriptor.mode_id) > ASK_ENGINE_MODE_ID_MAX_CHARS
        or not descriptor.mode_id.startswith(prefix)
        or not _MODE_ID.fullmatch(descriptor.mode_id)
        or type(descriptor.label) is not str
        or not descriptor.label.strip()
        or len(descriptor.label) > ASK_ENGINE_LABEL_MAX_CHARS
        or "\n" in descriptor.label
        or "\r" in descriptor.label
        or type(descriptor.description) is not str
        or not descriptor.description.strip()
        or len(descriptor.description) > ASK_ENGINE_DESCRIPTION_MAX_CHARS
        or "\n" in descriptor.description
        or "\r" in descriptor.description
        or type(descriptor.requires_kg) is not bool
    ):
        raise ExtensionRegistryError(
            f"Ask engine {declaration.id!r} has invalid descriptor fields"
        )
    return descriptor


def _valid_result(value: object) -> bool:
    return (
        type(value) is AskEngineResult
        and type(value.answer_markdown) is str
        and bool(value.answer_markdown.strip())
        and type(value.citations) is tuple
    )


def _emit(sink: object, **payload: object) -> None:
    if not callable(sink):
        return
    try:
        sink({"kind": "ask_plugin_engine", **payload})
    except Exception:
        return


__all__ = [
    "ASK_ENGINE_DESCRIPTION_MAX_CHARS",
    "ASK_ENGINE_LABEL_MAX_CHARS",
    "ASK_ENGINE_MODE_ID_MAX_CHARS",
    "AskEngineHost",
    "RegisteredAskEngine",
]
