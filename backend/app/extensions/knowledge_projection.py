"""Governed runner for ``knowledge.candidate_projector`` contributions."""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from app.domain.cancellation import CoreCancellation
from app.domain.knowledge_projection import (
    KnowledgeProjectionCallContext,
    KnowledgeProjectionElement,
    KnowledgeProjectionEvidencePatch,
    KnowledgeProjectionObjectPatch,
    KnowledgeProjectionPatch,
    KnowledgeProjectionRelationPatch,
    KnowledgeProjectionSchema,
)
from app.extension_sdk import (
    KNOWLEDGE_CANDIDATE_PROJECTOR_POINT,
    KNOWLEDGE_EVIDENCE_QUOTE_MAX_CHARS,
    SCOPED_SOURCE_ELEMENTS_READ_CAPABILITY,
    AvailabilityStatus,
    ContributionKind,
    ContributorResult,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionResultStatus,
    KnowledgeCandidateBatch,
    KnowledgeCandidateRef,
    KnowledgeElementRef,
    KnowledgeElementView,
    KnowledgeEvidenceCandidate,
    KnowledgeObjectCandidate,
    KnowledgeProjectionAvailabilityContext,
    KnowledgeProjectionBudget,
    KnowledgeProjectionContext,
    KnowledgeRelationCandidate,
    KnowledgeSchemaView,
)
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError


_STABLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_STABLE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
_JSON_KEY = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_OWNER_ID_MAX = 128
_OWNER_VERSION_MAX = 64


@dataclass(frozen=True)
class _Frozen:
    plugin_id: str
    plugin_version: str
    contribution_id: str
    project: Callable[[KnowledgeProjectionContext], object]
    requires: tuple[str, ...]


class _MalformedCancellation(RuntimeError):
    pass


def _valid_owner(plugin_id: object, version: object, contribution_id: object) -> bool:
    return (
        type(plugin_id) is str
        and 0 < len(plugin_id) <= _OWNER_ID_MAX
        and _STABLE_ID.fullmatch(plugin_id) is not None
        and type(contribution_id) is str
        and 0 < len(contribution_id) <= _OWNER_ID_MAX
        and _STABLE_ID.fullmatch(contribution_id) is not None
        and type(version) is str
        and 0 < len(version) <= _OWNER_VERSION_MAX
        and _SAFE_VERSION.fullmatch(version) is not None
    )


def _cancelled(token: object) -> bool:
    try:
        check = getattr(token, "is_set", None)
    except Exception as exc:
        raise _MalformedCancellation from exc
    if not callable(check):
        raise _MalformedCancellation
    try:
        value = check()
    except Exception as exc:
        raise _MalformedCancellation from exc
    if type(value) is not bool:
        raise _MalformedCancellation
    return value


def _raise_if_cancelled(token: object) -> None:
    if not _cancelled(token):
        return
    try:
        raiser = getattr(token, "raise_if_cancelled", None)
    except Exception as exc:
        raise _MalformedCancellation from exc
    if not callable(raiser):
        raise _MalformedCancellation
    try:
        raiser()
    except CoreCancellation:
        raise
    except Exception as exc:
        raise _MalformedCancellation from exc
    raise _MalformedCancellation


def _freeze_json(value: object, *, depth: int = 0) -> object:
    if depth > 12:
        raise ValueError("candidate payload too deep")
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("candidate payload float must be finite")
        return value
    if type(value) is list or type(value) is tuple:
        return tuple(_freeze_json(item, depth=depth + 1) for item in value)
    if type(value) is dict:
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str or _JSON_KEY.fullmatch(key) is None:
                raise ValueError("invalid candidate payload key")
            frozen[key] = _freeze_json(item, depth=depth + 1)
        return MappingProxyType(frozen)
    raise ValueError("candidate payload must be strict JSON")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


class KnowledgeCandidateProjectorHost:
    def __init__(
        self,
        registry: ExtensionRegistry,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        manifests = {manifest.id: manifest for manifest in registry.manifests()}
        frozen: list[_Frozen] = []
        for registered in registry.contributions(KNOWLEDGE_CANDIDATE_PROJECTOR_POINT):
            declaration = registered.contribution.declaration
            manifest = manifests[registered.plugin_id]
            implementation = registered.contribution.implementation
            try:
                project = getattr(implementation, "project", None)
            except Exception as exc:
                raise ExtensionRegistryError(
                    "invalid knowledge candidate projector"
                ) from exc
            if (
                declaration.kind is not ContributionKind.CONTRIBUTOR
                or type(manifest.requires) is not tuple
                or not any(
                    type(capability) is str
                    and capability == SCOPED_SOURCE_ELEMENTS_READ_CAPABILITY
                    for capability in manifest.requires
                )
                or not _valid_owner(
                    registered.plugin_id, manifest.version, declaration.id
                )
                or not callable(project)
            ):
                raise ExtensionRegistryError("invalid knowledge candidate projector")
            frozen.append(
                _Frozen(
                    registered.plugin_id,
                    manifest.version,
                    declaration.id,
                    project,
                    manifest.requires,
                )
            )
        self._registry = registry
        self._registrations = tuple(frozen)
        self._event_sink = event_sink
        self._clock = clock

    @property
    def has_contributors(self) -> bool:
        return bool(self._registrations)

    def project_application(
        self,
        call_context: KnowledgeProjectionCallContext,
        *,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> tuple[KnowledgeProjectionPatch, ...]:
        if not self._registrations:
            return ()
        try:
            return self._run(
                call_context,
                event_sink if event_sink is not None else self._event_sink,
            )
        except _MalformedCancellation:
            return ()

    def _run(
        self,
        call: KnowledgeProjectionCallContext,
        sink: Callable[[dict[str, object]], None] | None,
    ) -> tuple[KnowledgeProjectionPatch, ...]:
        if not self._valid_call(call):
            return ()
        _raise_if_cancelled(call.cancellation)
        if not self._connection_clear_for(call):
            return ()
        refs: list[KnowledgeElementRef] = []
        views: list[KnowledgeElementView] = []
        for expected, element in enumerate(call.elements, start=1):
            if (
                type(element) is not KnowledgeProjectionElement
                or type(element.ordinal) is not int
                or element.ordinal != expected
                or any(
                    type(value) is not str
                    for value in (
                        element.element_id,
                        element.element_type,
                        element.location_label,
                        element.text,
                    )
                )
                or not element.element_id
            ):
                return ()
            ref = KnowledgeElementRef(object())
            refs.append(ref)
            views.append(
                KnowledgeElementView(
                    ref, element.element_type, element.location_label, element.text
                )
            )
        schemas: list[KnowledgeSchemaView] = []
        schema_by_type: dict[str, KnowledgeSchemaView] = {}
        for schema in call.schemas:
            if (
                type(schema) is not KnowledgeProjectionSchema
                or type(schema.object_type) is not str
                or not schema.object_type
                or type(schema.fields) is not tuple
                or type(schema.primary) is not str
                or type(schema.list_fields) is not tuple
                or any(type(value) is not str for value in schema.fields)
                or any(type(value) is not str for value in schema.list_fields)
                or schema.primary not in schema.fields
                or not set(schema.list_fields).issubset(schema.fields)
                or schema.object_type in schema_by_type
            ):
                return ()
            view = KnowledgeSchemaView(
                schema.object_type,
                schema.fields,
                schema.primary,
                schema.list_fields,
            )
            schemas.append(view)
            schema_by_type[schema.object_type] = view
        budget = KnowledgeProjectionBudget(
            call.max_objects,
            call.max_relations,
            call.max_candidate_bytes,
            call.deadline_monotonic,
        )
        context = KnowledgeProjectionContext(
            tuple(views), tuple(schemas), call.cancellation, budget
        )
        accepted: list[KnowledgeProjectionPatch] = []
        remaining_objects = call.max_objects
        remaining_relations = call.max_relations
        remaining_bytes = call.max_candidate_bytes
        for registration in self._registrations:
            _raise_if_cancelled(call.cancellation)
            if not self._connection_clear_for(call):
                return ()
            expired = self._expired_for(call)
            if expired is None:
                return ()
            if expired:
                break
            availability_context = KnowledgeProjectionAvailabilityContext(
                registration.contribution_id,
                len(views),
                len(schemas),
                budget,
            )
            available = True
            for capability in registration.requires:
                try:
                    decision = self._registry.capability_availability(
                        capability, availability_context
                    )
                except Exception:
                    decision = None
                _raise_if_cancelled(call.cancellation)
                if not self._connection_clear_for(call):
                    return ()
                expired = self._expired_for(call)
                if expired is None:
                    return ()
                if expired:
                    return tuple(accepted)
                if (
                    decision is None
                    or type(decision.status) is not AvailabilityStatus
                    or decision.status is not AvailabilityStatus.AVAILABLE
                ):
                    available = False
                    break
            if not available:
                continue
            try:
                decision = self._registry.contribution_availability(
                    registration.contribution_id, availability_context
                )
            except Exception:
                decision = None
            _raise_if_cancelled(call.cancellation)
            if not self._connection_clear_for(call):
                return ()
            expired = self._expired_for(call)
            if expired is None:
                return ()
            if expired:
                return tuple(accepted)
            if (
                decision is None
                or type(decision.status) is not AvailabilityStatus
                or decision.status is not AvailabilityStatus.AVAILABLE
            ):
                continue
            started = self._safe_clock()
            if not self._connection_clear_for(call):
                return ()
            if started is None or started >= call.deadline_monotonic:
                return tuple(accepted)
            try:
                result = registration.project(context)
            except Exception:
                if not self._connection_clear_for(call):
                    return ()
                expired = self._expired_for(call)
                if expired is None:
                    return ()
                if expired:
                    return tuple(accepted)
                self._emit(sink, registration, "failed", 0, 0, started, call)
                _raise_if_cancelled(call.cancellation)
                continue
            _raise_if_cancelled(call.cancellation)
            if not self._connection_clear_for(call):
                return ()
            expired = self._expired_for(call)
            if expired is None:
                return ()
            if expired:
                return tuple(accepted)
            if self._valid_unavailable_result(result):
                self._emit(
                    sink,
                    registration,
                    "unavailable",
                    0,
                    0,
                    started,
                    call,
                )
                _raise_if_cancelled(call.cancellation)
                continue
            validated = self._validate_result(
                result,
                refs,
                views,
                schema_by_type,
                registration,
                remaining_objects,
                remaining_relations,
                remaining_bytes,
            )
            _raise_if_cancelled(call.cancellation)
            expired = self._expired_for(call)
            if expired is None:
                return ()
            if expired:
                return tuple(accepted)
            if validated is None:
                self._emit(sink, registration, "invalid", 0, 0, started, call)
                _raise_if_cancelled(call.cancellation)
                continue
            patch, used_bytes = validated
            accepted.append(patch)
            remaining_objects -= len(patch.objects)
            remaining_relations -= len(patch.relations)
            remaining_bytes -= used_bytes
            self._emit(
                sink,
                registration,
                "available",
                len(patch.objects),
                len(patch.relations),
                started,
                call,
            )
            _raise_if_cancelled(call.cancellation)
            if remaining_objects <= 0 and remaining_relations <= 0:
                break
        _raise_if_cancelled(call.cancellation)
        if not self._connection_clear_for(call):
            return ()
        return tuple(accepted)

    @staticmethod
    def _valid_call(call: object) -> bool:
        return (
            type(call) is KnowledgeProjectionCallContext
            and type(call.elements) is tuple
            and bool(call.elements)
            and type(call.schemas) is tuple
            and bool(call.schemas)
            and type(call.max_objects) is int
            and call.max_objects >= 1
            and type(call.max_relations) is int
            and call.max_relations >= 1
            and type(call.max_candidate_bytes) is int
            and call.max_candidate_bytes >= 1
            and type(call.deadline_monotonic) is float
            and math.isfinite(call.deadline_monotonic)
        )

    def _validate_result(
        self,
        result: object,
        refs: list[KnowledgeElementRef],
        views: list[KnowledgeElementView],
        schemas: dict[str, KnowledgeSchemaView],
        registration: _Frozen,
        max_objects: int,
        max_relations: int,
        max_bytes: int,
    ) -> tuple[KnowledgeProjectionPatch, int] | None:
        if type(result) is not ContributorResult or type(result.items) is not tuple:
            return None
        if type(result.status) is not ExtensionResultStatus:
            return None
        if result.failure is not None and type(result.failure) is not ExtensionFailure:
            return None
        if result.failure is not None and (
            type(result.failure.kind) is not ExtensionFailureKind
            or type(result.failure.code) is not str
            or _STABLE_CODE.fullmatch(result.failure.code) is None
        ):
            return None
        if result.status is not ExtensionResultStatus.AVAILABLE:
            return None
        if result.failure is not None or len(result.items) != 1:
            return None
        batch = result.items[0]
        if (
            type(batch) is not KnowledgeCandidateBatch
            or type(batch.objects) is not tuple
            or type(batch.relations) is not tuple
            or len(batch.objects) > max_objects
            or len(batch.relations) > max_relations
            or not batch.objects
        ):
            return None
        element_ordinals = {id(ref): index for index, ref in enumerate(refs, 1)}
        candidate_refs: dict[int, str] = {}
        candidate_types: dict[str, str] = {}
        object_patches: list[KnowledgeProjectionObjectPatch] = []
        materialized_for_size: list[object] = []
        for candidate in batch.objects:
            if (
                type(candidate) is not KnowledgeObjectCandidate
                or type(candidate.ref) is not KnowledgeCandidateRef
                or id(candidate.ref) in candidate_refs
                or type(candidate.key) is not str
                or not (0 < len(candidate.key) <= _OWNER_ID_MAX)
                or _STABLE_ID.fullmatch(candidate.key) is None
                or type(candidate.object_type) is not str
                or candidate.object_type not in schemas
                or type(candidate.payload) is not dict
                or type(candidate.evidence) is not tuple
                or not candidate.evidence
                or candidate.key in candidate_types
            ):
                return None
            schema = schemas[candidate.object_type]
            try:
                payload = _freeze_json(candidate.payload)
            except Exception:
                return None
            if not self._payload_matches_schema(payload, schema):
                return None
            evidence = self._evidence_ordinals(
                candidate.evidence, element_ordinals, refs, views
            )
            if evidence is None:
                return None
            candidate_refs[id(candidate.ref)] = candidate.key
            candidate_types[candidate.key] = candidate.object_type
            object_patches.append(
                KnowledgeProjectionObjectPatch(
                    candidate.key,
                    candidate.object_type,
                    payload,
                    evidence,
                )
            )
            materialized_for_size.append(
                {
                    "key": candidate.key,
                    "object_type": candidate.object_type,
                    "payload": _thaw_json(payload),
                    "evidence": [item.quote for item in evidence],
                }
            )
        relation_patches: list[KnowledgeProjectionRelationPatch] = []
        relation_keys: set[tuple[str, str, str]] = set()
        for relation in batch.relations:
            if (
                type(relation) is not KnowledgeRelationCandidate
                or type(relation.source) is not KnowledgeCandidateRef
                or type(relation.target) is not KnowledgeCandidateRef
                or type(relation.edge_type) is not str
                or _STABLE_ID.fullmatch(relation.edge_type) is None
                or type(relation.evidence) is not tuple
                or not relation.evidence
            ):
                return None
            source_key = candidate_refs.get(id(relation.source))
            target_key = candidate_refs.get(id(relation.target))
            if (
                source_key is None
                or target_key is None
                or relation.source is relation.target
            ):
                return None
            identity = (source_key, relation.edge_type, target_key)
            if identity in relation_keys:
                return None
            relation_keys.add(identity)
            evidence = self._evidence_ordinals(
                relation.evidence, element_ordinals, refs, views
            )
            if evidence is None:
                return None
            relation_patches.append(
                KnowledgeProjectionRelationPatch(
                    source_key,
                    target_key,
                    relation.edge_type,
                    evidence,
                )
            )
            materialized_for_size.append(
                {
                    "source": source_key,
                    "target": target_key,
                    "edge_type": relation.edge_type,
                    "evidence": [item.quote for item in evidence],
                }
            )
        try:
            size = len(
                json.dumps(
                    {
                        "plugin_id": registration.plugin_id,
                        "plugin_version": registration.plugin_version,
                        "contribution_id": registration.contribution_id,
                        "candidates": materialized_for_size,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except Exception:
            return None
        if size > max_bytes:
            return None
        return (
            KnowledgeProjectionPatch(
                registration.plugin_id,
                registration.plugin_version,
                registration.contribution_id,
                tuple(object_patches),
                tuple(relation_patches),
            ),
            size,
        )

    @staticmethod
    def _valid_unavailable_result(result: object) -> bool:
        return (
            type(result) is ContributorResult
            and type(result.items) is tuple
            and not result.items
            and type(result.status) is ExtensionResultStatus
            and result.status is not ExtensionResultStatus.AVAILABLE
            and type(result.failure) is ExtensionFailure
            and type(result.failure.kind) is ExtensionFailureKind
            and type(result.failure.code) is str
            and _STABLE_CODE.fullmatch(result.failure.code) is not None
        )

    @staticmethod
    def _payload_matches_schema(
        payload: object, schema: KnowledgeSchemaView
    ) -> bool:
        if not isinstance(payload, Mapping) or set(payload) != set(schema.fields):
            return False
        for field in schema.fields:
            value = payload[field]
            if field in schema.list_fields:
                if type(value) is not tuple or any(
                    type(item) is not str for item in value
                ):
                    return False
            elif type(value) is not str:
                return False
        primary = payload.get(schema.primary)
        return type(primary) is str and bool(primary.strip())

    @staticmethod
    def _evidence_ordinals(
        evidence: tuple[KnowledgeEvidenceCandidate, ...],
        ordinals: dict[int, int],
        refs: list[KnowledgeElementRef],
        views: list[KnowledgeElementView],
    ) -> tuple[KnowledgeProjectionEvidencePatch, ...] | None:
        result: list[KnowledgeProjectionEvidencePatch] = []
        seen: set[int] = set()
        for item in evidence:
            if (
                type(item) is not KnowledgeEvidenceCandidate
                or type(item.element) is not KnowledgeElementRef
                or type(item.quote) is not str
                or not item.quote.strip()
                or len(item.quote) > KNOWLEDGE_EVIDENCE_QUOTE_MAX_CHARS
            ):
                return None
            ordinal = ordinals.get(id(item.element))
            if (
                ordinal is None
                or refs[ordinal - 1] is not item.element
                or ordinal in seen
                or item.quote not in views[ordinal - 1].text
            ):
                return None
            seen.add(ordinal)
            result.append(KnowledgeProjectionEvidencePatch(ordinal, item.quote))
        return tuple(result)

    @staticmethod
    def _connection_clear(probe: object) -> bool:
        try:
            check = getattr(probe, "is_connection_held", None)
            if not callable(check):
                return False
            held = check()
        except Exception:
            return False
        return type(held) is bool and held is False

    def _connection_clear_for(self, call: KnowledgeProjectionCallContext) -> bool:
        clear = self._connection_clear(call.connection_probe)
        if not clear:
            return False
        _raise_if_cancelled(call.cancellation)
        return True

    def _expired_for(self, call: KnowledgeProjectionCallContext) -> bool | None:
        now = self._safe_clock()
        if not self._connection_clear_for(call):
            return None
        return now is None or now >= call.deadline_monotonic

    def _safe_clock(self) -> float | None:
        try:
            value = self._clock()
        except Exception:
            return None
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            return None
        return float(value)

    def _emit(
        self,
        sink: Callable[[dict[str, object]], None] | None,
        registration: _Frozen,
        status: str,
        objects: int,
        relations: int,
        started: float | None,
        call: KnowledgeProjectionCallContext,
    ) -> None:
        if sink is None:
            return
        finished = self._safe_clock()
        if not self._connection_clear_for(call):
            return
        elapsed = 0
        if started is not None and finished is not None:
            delta = max(0.0, finished - started) * 1000
            if math.isfinite(delta):
                elapsed = int(delta)
        try:
            sink(
                {
                    "kind": "extension_contribution",
                    "point": KNOWLEDGE_CANDIDATE_PROJECTOR_POINT,
                    "plugin_id": registration.plugin_id,
                    "contribution_id": registration.contribution_id,
                    "status": status,
                    "object_count": objects,
                    "relation_count": relations,
                    "duration_ms": elapsed,
                }
            )
        except Exception:
            pass
        _raise_if_cancelled(call.cancellation)
