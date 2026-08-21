"""Core admission for optional source-local knowledge candidates."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

from app.domain.cancellation import CoreCancellation
from app.domain.knowledge_projection import (
    KNOWLEDGE_EVIDENCE_QUOTE_MAX_CHARS,
    KnowledgeCandidateProjectorHostPort,
    KnowledgeProjectionCallContext,
    KnowledgeProjectionElement,
    KnowledgeProjectionEvidencePatch,
    KnowledgeProjectionObjectPatch,
    KnowledgeProjectionPatch,
    KnowledgeProjectionRelationPatch,
    KnowledgeProjectionSchema,
)
from app.models.sources import SourceElement
from app.services.kg.edge_schema import is_valid_edge_pair


_STABLE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SAFE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*$")
_OWNER_ID_MAX = 128
_OWNER_VERSION_MAX = 64
_LOCAL_ID_DIGEST_HEX_CHARS = 32
_HIDDEN_SOURCE_TYPES = frozenset({"memory", "knowhow"})


class KnowledgeProjectionBoundaryError(BaseException):
    """Stop all ordinary failure plumbing after an invalid connection boundary."""


class _ProjectionCancelled(CoreCancellation):
    pass


class _ControlCancellation:
    __slots__ = ("__control",)

    def __init__(self, control: object | None) -> None:
        self.__control = control

    def is_set(self) -> bool:
        if self.__control is None:
            return False
        value = getattr(self.__control, "aborted", False)
        if type(value) is not bool:
            raise TypeError("malformed KG cancellation state")
        return value

    def raise_if_cancelled(self) -> None:
        raise _ProjectionCancelled()


def project_knowledge_candidates(
    objects: list[dict],
    relations: list[dict],
    *,
    source_id: str,
    source_title: str,
    source_type: str,
    elements: list[SourceElement],
    host: KnowledgeCandidateProjectorHostPort | None,
    effective_schemas: Callable[[str], Mapping[str, Any]],
    notebook_id: str,
    control: object | None,
    connection_probe: object,
    max_objects: int,
    max_relations: int,
    max_candidate_bytes: int,
    timeout_seconds: float,
    event_sink: Callable[[dict[str, object]], None] | None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[list[dict], list[dict]]:
    """Append independently admitted candidates without touching the baseline.

    Empty topology and hidden synthetic sources return the original list
    objects before schema resolution, clock, probe, event or DTO snapshot work.
    """

    if host is None or source_type in _HIDDEN_SOURCE_TYPES:
        return objects, relations
    try:
        has_contributors = host.has_contributors
    except CoreCancellation:
        _assert_connection_clear(connection_probe, control)
        return objects, relations
    except Exception:
        _assert_connection_clear(connection_probe, control)
        return objects, relations
    _assert_connection_clear(connection_probe, control)
    if has_contributors is not True:
        return objects, relations
    if (
        type(source_id) is not str
        or not source_id
        or type(source_title) is not str
        or type(notebook_id) is not str
        or not notebook_id
        or type(max_objects) is not int
        or max_objects < 1
        or type(max_relations) is not int
        or max_relations < 1
        or type(max_candidate_bytes) is not int
        or max_candidate_bytes < 1
        or type(timeout_seconds) not in {int, float}
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        return objects, relations
    try:
        schemas_by_type = effective_schemas(notebook_id)
        _assert_connection_clear(connection_probe, control)
        schemas = _schema_snapshot(schemas_by_type)
        _assert_connection_clear(connection_probe, control)
        if not schemas:
            return objects, relations
        now = clock()
        _assert_connection_clear(connection_probe, control)
        if type(now) not in {int, float} or not math.isfinite(float(now)):
            return objects, relations
        element_snapshot = _element_snapshot(elements, source_id)
        _assert_connection_clear(connection_probe, control)
        if not element_snapshot:
            return objects, relations
        cancellation = _ControlCancellation(control)
        patches = host.project_application(
            KnowledgeProjectionCallContext(
                element_snapshot,
                schemas,
                cancellation,
                connection_probe,
                max_objects,
                max_relations,
                max_candidate_bytes,
                float(now) + float(timeout_seconds),
            ),
            event_sink=event_sink,
        )
    except KnowledgeProjectionBoundaryError:
        raise
    except (_ProjectionCancelled, CoreCancellation):
        _assert_connection_clear(connection_probe, control)
        return objects, relations
    except Exception:
        _assert_connection_clear(connection_probe, control)
        return objects, relations
    _assert_connection_clear(connection_probe, control)
    if type(patches) is not tuple or not patches:
        return objects, relations
    baseline_ids: set[str] = set()
    for item in objects:
        if type(item) is not dict or type(item.get("local_id")) is not str:
            return objects, relations
        baseline_ids.add(item["local_id"])
    accepted_objects: list[dict] = []
    accepted_relations: list[dict] = []
    remaining_objects = max_objects
    remaining_relations = max_relations
    remaining_bytes = max_candidate_bytes
    owners: set[str] = set()
    for patch in patches:
        try:
            admitted = _admit_patch(
                patch,
                elements,
                schemas_by_type,
                source_id,
                source_title,
                baseline_ids | {
                    item["local_id"] for item in accepted_objects
                },
                remaining_objects,
                remaining_relations,
                remaining_bytes,
            )
        except Exception:
            admitted = None
        _raise_control_cancelled(control)
        if admitted is None or patch.contribution_id in owners:
            continue
        new_objects, new_relations, used_bytes = admitted
        owners.add(patch.contribution_id)
        accepted_objects.extend(new_objects)
        accepted_relations.extend(new_relations)
        remaining_objects -= len(new_objects)
        remaining_relations -= len(new_relations)
        remaining_bytes -= used_bytes
    _assert_connection_clear(connection_probe, control)
    if not accepted_objects and not accepted_relations:
        return objects, relations
    return objects + accepted_objects, relations + accepted_relations


def _raise_control_cancelled(control: object | None) -> None:
    if control is None:
        return
    raiser = getattr(control, "raise_if_aborted", None)
    if callable(raiser):
        raiser()


def _assert_connection_clear(probe: object, control: object | None) -> None:
    try:
        check = getattr(probe, "is_connection_held", None)
        if not callable(check):
            raise KnowledgeProjectionBoundaryError(
                "invalid knowledge projection connection probe"
            )
        held = check()
    except CoreCancellation as exc:
        raise KnowledgeProjectionBoundaryError(
            "invalid knowledge projection connection probe"
        ) from exc
    except KnowledgeProjectionBoundaryError:
        raise
    except Exception as exc:
        raise KnowledgeProjectionBoundaryError(
            "invalid knowledge projection connection probe"
        ) from exc
    if type(held) is not bool or held:
        raise KnowledgeProjectionBoundaryError(
            "knowledge projection retained a database connection"
        )
    _raise_control_cancelled(control)


def _element_snapshot(
    elements: list[SourceElement],
    source_id: str,
) -> tuple[KnowledgeProjectionElement, ...]:
    result: list[KnowledgeProjectionElement] = []
    for ordinal, element in enumerate(elements, start=1):
        values = (
            element.id,
            element.element_type,
            element.location_label,
            element.text,
        )
        if any(type(value) is not str for value in values) or not element.id:
            return ()
        if type(element.source_id) is not str or element.source_id != source_id:
            return ()
        result.append(KnowledgeProjectionElement(ordinal, *values))
    return tuple(result)


def _schema_snapshot(
    schemas: Mapping[str, Any],
) -> tuple[KnowledgeProjectionSchema, ...]:
    if not isinstance(schemas, Mapping):
        return ()
    result: list[KnowledgeProjectionSchema] = []
    for object_type in sorted(schemas):
        schema = schemas[object_type]
        fields = getattr(schema, "fields", None)
        primary = getattr(schema, "primary", None)
        list_fields = getattr(schema, "list_fields", None)
        if (
            type(object_type) is not str
            or type(fields) is not list
            or type(primary) is not str
            or type(list_fields) is not list
            or any(type(value) is not str for value in fields)
            or any(type(value) is not str for value in list_fields)
            or primary not in fields
        ):
            return ()
        result.append(
            KnowledgeProjectionSchema(
                object_type,
                tuple(fields),
                primary,
                tuple(list_fields),
            )
        )
    return tuple(result)


def _valid_owner(patch: KnowledgeProjectionPatch) -> bool:
    return (
        type(patch.plugin_id) is str
        and 0 < len(patch.plugin_id) <= _OWNER_ID_MAX
        and _STABLE_ID.fullmatch(patch.plugin_id) is not None
        and type(patch.contribution_id) is str
        and 0 < len(patch.contribution_id) <= _OWNER_ID_MAX
        and _STABLE_ID.fullmatch(patch.contribution_id) is not None
        and type(patch.plugin_version) is str
        and 0 < len(patch.plugin_version) <= _OWNER_VERSION_MAX
        and _SAFE_VERSION.fullmatch(patch.plugin_version) is not None
    )


def _admit_patch(
    patch: object,
    elements: list[SourceElement],
    schemas: Mapping[str, Any],
    source_id: str,
    source_title: str,
    reserved_ids: set[str],
    max_objects: int,
    max_relations: int,
    max_bytes: int,
) -> tuple[list[dict], list[dict], int] | None:
    if (
        type(patch) is not KnowledgeProjectionPatch
        or not _valid_owner(patch)
        or type(patch.objects) is not tuple
        or type(patch.relations) is not tuple
        or not patch.objects
        or len(patch.objects) > max_objects
        or len(patch.relations) > max_relations
    ):
        return None
    objects: list[dict] = []
    key_to_local: dict[str, str] = {}
    key_to_type: dict[str, str] = {}
    for candidate in patch.objects:
        if (
            type(candidate) is not KnowledgeProjectionObjectPatch
            or type(candidate.candidate_key) is not str
            or not (0 < len(candidate.candidate_key) <= _OWNER_ID_MAX)
            or _STABLE_ID.fullmatch(candidate.candidate_key) is None
            or candidate.candidate_key in key_to_local
            or type(candidate.object_type) is not str
            or candidate.object_type not in schemas
            or type(candidate.evidence) is not tuple
            or not candidate.evidence
        ):
            return None
        schema = schemas[candidate.object_type]
        if type(candidate.payload) is not MappingProxyType:
            return None
        payload = _thaw_payload(candidate.payload)
        if payload is None or not _payload_matches_schema(payload, schema):
            return None
        evidence = _materialize_evidence(
            candidate.evidence, elements, source_id, source_title
        )
        if evidence is None:
            return None
        local_id = "kp-" + hashlib.sha256(
            (
                patch.plugin_id
                + "\0"
                + patch.contribution_id
                + "\0"
                + candidate.candidate_key
            ).encode("utf-8")
        ).hexdigest()[:_LOCAL_ID_DIGEST_HEX_CHARS]
        if local_id in reserved_ids:
            return None
        key_to_local[candidate.candidate_key] = local_id
        key_to_type[candidate.candidate_key] = candidate.object_type
        objects.append(
            {
                "local_id": local_id,
                "object_type": candidate.object_type,
                "payload": payload,
                "evidence": evidence,
            }
        )
    relations: list[dict] = []
    relation_keys: set[tuple[str, str, str]] = set()
    for relation in patch.relations:
        if (
            type(relation) is not KnowledgeProjectionRelationPatch
            or type(relation.source_key) is not str
            or type(relation.target_key) is not str
            or relation.source_key == relation.target_key
            or relation.source_key not in key_to_local
            or relation.target_key not in key_to_local
            or type(relation.edge_type) is not str
            or not is_valid_edge_pair(
                relation.edge_type,
                key_to_type[relation.source_key],
                key_to_type[relation.target_key],
            )
            or type(relation.evidence) is not tuple
            or not relation.evidence
        ):
            return None
        identity = (
            relation.source_key,
            relation.edge_type,
            relation.target_key,
        )
        if identity in relation_keys:
            return None
        relation_keys.add(identity)
        evidence = _materialize_evidence(
            relation.evidence, elements, source_id, source_title
        )
        if evidence is None:
            return None
        relations.append(
            {
                "source_local_id": key_to_local[relation.source_key],
                "target_local_id": key_to_local[relation.target_key],
                "edge_type": relation.edge_type,
                "evidence": evidence,
            }
        )
    try:
        used_bytes = len(
            json.dumps(
                {
                    "plugin_id": patch.plugin_id,
                    "plugin_version": patch.plugin_version,
                    "contribution_id": patch.contribution_id,
                    "objects": objects,
                    "relations": relations,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
    except Exception:
        return None
    if used_bytes > max_bytes:
        return None
    return objects, relations, used_bytes


def _thaw_payload(value: object, *, depth: int = 0) -> dict[str, object] | None:
    if depth > 12 or type(value) is not MappingProxyType:
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            return None
        thawed = _thaw_value(item, depth=depth + 1)
        if thawed is _INVALID:
            return None
        result[key] = thawed
    return result


_INVALID = object()


def _thaw_value(value: object, *, depth: int) -> object:
    if depth > 12:
        return _INVALID
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        return value if math.isfinite(value) else _INVALID
    if type(value) is tuple:
        result = []
        for item in value:
            thawed = _thaw_value(item, depth=depth + 1)
            if thawed is _INVALID:
                return _INVALID
            result.append(thawed)
        return result
    if type(value) is MappingProxyType:
        result_dict: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                return _INVALID
            thawed = _thaw_value(item, depth=depth + 1)
            if thawed is _INVALID:
                return _INVALID
            result_dict[key] = thawed
        return result_dict
    return _INVALID


def _payload_matches_schema(payload: dict[str, object], schema: object) -> bool:
    fields = getattr(schema, "fields", None)
    primary = getattr(schema, "primary", None)
    list_fields = getattr(schema, "list_fields", None)
    if (
        type(fields) is not list
        or type(primary) is not str
        or type(list_fields) is not list
        or set(payload) != set(fields)
    ):
        return False
    for field in fields:
        value = payload[field]
        if field in list_fields:
            if type(value) is not list or any(
                type(item) is not str for item in value
            ):
                return False
        elif type(value) is not str:
            return False
    primary_value = payload.get(primary)
    return type(primary_value) is str and bool(primary_value.strip())


def _materialize_evidence(
    evidence: tuple[KnowledgeProjectionEvidencePatch, ...],
    elements: list[SourceElement],
    source_id: str,
    source_title: str,
) -> list[dict] | None:
    result: list[dict] = []
    seen: set[int] = set()
    for item in evidence:
        if (
            type(item) is not KnowledgeProjectionEvidencePatch
            or type(item.ordinal) is not int
            or not 1 <= item.ordinal <= len(elements)
            or item.ordinal in seen
            or type(item.quote) is not str
            or not item.quote.strip()
            or len(item.quote) > KNOWLEDGE_EVIDENCE_QUOTE_MAX_CHARS
        ):
            return None
        element = elements[item.ordinal - 1]
        if (
            type(element.id) is not str
            or not element.id
            or type(element.element_type) is not str
            or type(element.location_label) is not str
            or type(element.text) is not str
            or item.quote not in element.text
        ):
            return None
        seen.add(item.ordinal)
        result.append(
            {
                "source_id": source_id,
                "source_title": source_title,
                "element_id": element.id,
                "element_type": element.element_type,
                "location_label": element.location_label,
                "quoted_span": item.quote,
                "confidence": 1.0,
            }
        )
    return result
