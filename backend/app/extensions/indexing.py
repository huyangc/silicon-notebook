"""Startup-frozen host for selectable deployment indexing pipelines."""
from __future__ import annotations

import re
from typing import Sequence

from app.domain.indexing_pipeline import (
    IndexingPipelineChunkResult,
    IndexingPipelineKgMapResult,
    IndexingPipelineKgPromptResult,
    IndexingPipelineOption,
)
from app.domain.kg.edge_schema import EDGE_SPECS, EDGE_TYPE_ORDER
from app.extension_sdk import (
    AvailabilityStatus,
    ContributionKind,
    INDEXING_PIPELINE_POINT,
    IndexingChunkContext,
    IndexingChunkProposal,
    IndexingKgEdgeType,
    IndexingKgElement,
    IndexingKgFragment,
    IndexingKgMessage,
    IndexingKgPrompt,
    IndexingKgPromptContext,
    IndexingPipelineAvailabilityContext,
    IndexingPipelineDescriptor,
    IndexingSourceElement,
)
from app.extensions.discovery import ExtensionDiscoveryError
from app.extensions.registry import ExtensionRegistry, ExtensionRegistryError


_PIPELINE_ID = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)+$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_PIPELINE_ID_MAX_CHARS = 128
_PIPELINE_LABEL_MAX_CHARS = 80
_PIPELINE_DESCRIPTION_MAX_CHARS = 300
_PIPELINE_VERSION_MAX_CHARS = 80


class IndexingPipelineHost:
    def __init__(self, registry: ExtensionRegistry) -> None:
        if not registry.frozen:
            raise ExtensionRegistryError(
                "Indexing pipeline host requires a frozen registry"
            )
        manifests = {manifest.id: manifest for manifest in registry.manifests()}
        registrations: dict[str, tuple[str, str, object, IndexingPipelineDescriptor]] = {}
        for item in registry.contributions(INDEXING_PIPELINE_POINT):
            try:
                descriptor = self._validate(item.plugin_id, item.contribution)
            except Exception as exc:
                manifest = manifests[item.plugin_id]
                if manifest.trust == "deployment":
                    raise ExtensionDiscoveryError(
                        item.plugin_id,
                        "invalid_indexing_pipeline",
                        exception_type=type(exc).__name__,
                    ) from None
                if isinstance(exc, ExtensionRegistryError):
                    raise
                raise ExtensionRegistryError("invalid indexing pipeline") from exc
            if descriptor.pipeline_id in registrations:
                raise ExtensionRegistryError(
                    f"duplicate indexing pipeline {descriptor.pipeline_id!r}"
                )
            registrations[descriptor.pipeline_id] = (
                item.plugin_id,
                item.contribution.declaration.id,
                item.contribution.implementation,
                descriptor,
            )
        self._registry = registry
        self._registrations = dict(sorted(registrations.items()))

    @staticmethod
    def _validate(plugin_id: str, contribution) -> IndexingPipelineDescriptor:
        declaration = contribution.declaration
        implementation = contribution.implementation
        if declaration.kind is not ContributionKind.CONTRIBUTOR:
            raise ExtensionRegistryError(
                f"indexing pipeline {declaration.id!r} must be a contributor"
            )
        descriptor = getattr(implementation, "descriptor", None)
        if type(descriptor) is not IndexingPipelineDescriptor:
            raise ExtensionRegistryError("indexing pipeline descriptor has invalid type")
        if (
            type(descriptor.pipeline_id) is not str
            or not descriptor.pipeline_id.startswith(f"{plugin_id}.")
            or len(descriptor.pipeline_id) > _PIPELINE_ID_MAX_CHARS
            or not _PIPELINE_ID.fullmatch(descriptor.pipeline_id)
            or descriptor.pipeline_id == "builtin"
        ):
            raise ExtensionRegistryError("indexing pipeline id has invalid namespace")
        if (
            type(descriptor.label) is not str
            or not descriptor.label.strip()
            or len(descriptor.label) > _PIPELINE_LABEL_MAX_CHARS
            or type(descriptor.description) is not str
            or len(descriptor.description) > _PIPELINE_DESCRIPTION_MAX_CHARS
            or type(descriptor.version) is not str
            or len(descriptor.version) > _PIPELINE_VERSION_MAX_CHARS
            or not _VERSION.fullmatch(descriptor.version)
            or type(descriptor.overrides_chunking) is not bool
            or type(descriptor.overrides_kg_extraction) is not bool
            or not (
                descriptor.overrides_chunking
                or descriptor.overrides_kg_extraction
            )
        ):
            raise ExtensionRegistryError("indexing pipeline descriptor is invalid")
        if descriptor.overrides_chunking and not callable(
            getattr(implementation, "build_chunks", None)
        ):
            raise ExtensionRegistryError("chunking pipeline has no build_chunks method")
        if descriptor.overrides_kg_extraction and not (
            callable(getattr(implementation, "build_kg_prompt", None))
            and callable(getattr(implementation, "map_kg_response", None))
        ):
            raise ExtensionRegistryError(
                "KG indexing pipeline has no prompt/mapper methods"
            )
        return descriptor

    def _available(self, contribution_id: str, pipeline_id: str) -> bool:
        decision = self._registry.availability(
            contribution_id,
            IndexingPipelineAvailabilityContext(contribution_id, pipeline_id),
        )
        return decision.status is AvailabilityStatus.AVAILABLE

    def options(self) -> tuple[IndexingPipelineOption, ...]:
        return tuple(
            IndexingPipelineOption(
                pipeline_id=descriptor.pipeline_id,
                label=descriptor.label,
                description=descriptor.description,
                version=descriptor.version,
                overrides_chunking=descriptor.overrides_chunking,
                overrides_kg_extraction=descriptor.overrides_kg_extraction,
                available=self._available(contribution_id, pipeline_id),
            )
            for pipeline_id, (
                _plugin_id,
                contribution_id,
                _implementation,
                descriptor,
            ) in self._registrations.items()
        )

    def option(self, pipeline_id: str) -> IndexingPipelineOption | None:
        registered = self._registrations.get(pipeline_id)
        if registered is None:
            return None
        _plugin_id, contribution_id, _implementation, descriptor = registered
        return IndexingPipelineOption(
            pipeline_id=descriptor.pipeline_id,
            label=descriptor.label,
            description=descriptor.description,
            version=descriptor.version,
            overrides_chunking=descriptor.overrides_chunking,
            overrides_kg_extraction=descriptor.overrides_kg_extraction,
            available=self._available(contribution_id, pipeline_id),
        )

    def build_chunks(
        self,
        pipeline_id: str,
        elements: Sequence[object],
        *,
        target_chars: int,
        overlap_chars: int,
    ) -> IndexingPipelineChunkResult:
        registered = self._registrations.get(pipeline_id)
        if registered is None:
            return IndexingPipelineChunkResult(None, "indexing_pipeline_missing")
        _plugin_id, contribution_id, implementation, descriptor = registered
        if not self._available(contribution_id, pipeline_id):
            return IndexingPipelineChunkResult(None, "indexing_pipeline_unavailable")
        if not descriptor.overrides_chunking:
            # None(而不是空 tuple):调用方对 None 走内建 chunker 回退,而空 tuple
            # 会被当成「合法地提议零个 chunk」直接采纳——这条分支当前在调用侧更早
            # 处已短路、不可达,但一旦可达,() 就是整来源静默零 chunk(评审 P2)。
            return IndexingPipelineChunkResult(
                None, "indexing_pipeline_not_chunking"
            )
        try:
            projected = tuple(
                IndexingSourceElement(
                    element_id=str(item["id"]),
                    element_type=str(item.get("element_type") or ""),
                    text=str(item.get("text") or ""),
                    caption=str(item.get("caption") or ""),
                    description=str(item.get("description") or ""),
                    section_path=str(item.get("section_path") or ""),
                )
                for item in elements
            )
            proposals = implementation.build_chunks(
                projected,
                IndexingChunkContext(target_chars, overlap_chars),
            )
            # Do not iterate here: validation owns the deployment bounds and
            # stops a malicious/unbounded iterator before materializing it.
            return IndexingPipelineChunkResult(proposals)
        except Exception:
            return IndexingPipelineChunkResult(
                None, "indexing_pipeline_chunk_failed"
            )

    @staticmethod
    def _project_kg_elements(elements: Sequence[object]) -> tuple[IndexingKgElement, ...]:
        projected = []
        for index, item in enumerate(elements):
            get = item.get if isinstance(item, dict) else None
            projected.append(
                IndexingKgElement(
                    handle=f"e{index}",
                    text=(
                        str(get("text") or "")
                        if get is not None
                        else str(getattr(item, "text", "") or "")
                    ),
                    element_type=(
                        str(get("element_type") or get("type") or "")
                        if get is not None
                        else str(
                            getattr(item, "element_type", None)
                            or getattr(item, "type", "")
                            or ""
                        )
                    ),
                    location_label=(
                        str(get("location_label") or "")
                        if get is not None
                        else str(getattr(item, "location_label", "") or "")
                    ),
                    section_path=(
                        str(get("section_path") or "")
                        if get is not None
                        else str(getattr(item, "section_path", "") or "")
                    ),
                )
            )
        return tuple(projected)

    @staticmethod
    def _kg_context(
        doc_type: str,
        section_path: str,
        object_types: Sequence[str] = (),
    ) -> IndexingKgPromptContext:
        def title(value: str) -> str:
            return value[:1].upper() + value[1:]

        core = ("Concept", "Claim", "Formula", "Procedure")
        visible_types = tuple(
            dict.fromkeys(
                core
                + tuple(
                    str(item).strip()
                    for item in object_types
                    if str(item).strip()
                )
            )
        )
        return IndexingKgPromptContext(
            doc_type=doc_type,
            section_path=section_path,
            object_types=visible_types,
            edge_types=tuple(
                IndexingKgEdgeType(
                    edge_type=edge_type,
                    allowed_pairs=tuple(
                        (title(source), title(target))
                        for source, target in sorted(
                            EDGE_SPECS[edge_type].allowed_pairs
                        )
                    ),
                )
                for edge_type in EDGE_TYPE_ORDER
            ),
        )

    @staticmethod
    def _valid_prompt(prompt: object) -> bool:
        if type(prompt) is not IndexingKgPrompt:
            return False
        if type(prompt.messages) is not tuple or not prompt.messages:
            return False
        if type(prompt.response_schema_hint) is not str:
            return False
        for message in prompt.messages:
            if (
                type(message) is not IndexingKgMessage
                or message.role not in {"system", "user", "assistant"}
                or type(message.content) is not str
                or not message.content.strip()
            ):
                return False
        return True

    def build_kg_prompt(
        self,
        pipeline_id: str,
        elements: Sequence[object],
        *,
        doc_type: str,
        section_path: str,
        object_types: Sequence[str] = (),
    ) -> IndexingPipelineKgPromptResult:
        registered = self._registrations.get(pipeline_id)
        if registered is None:
            return IndexingPipelineKgPromptResult(
                None, "indexing_pipeline_missing"
            )
        _plugin_id, contribution_id, implementation, descriptor = registered
        if not self._available(contribution_id, pipeline_id):
            return IndexingPipelineKgPromptResult(
                None, "indexing_pipeline_unavailable"
            )
        if not descriptor.overrides_kg_extraction:
            return IndexingPipelineKgPromptResult(None)
        try:
            projected = self._project_kg_elements(elements)
            context = self._kg_context(doc_type, section_path, object_types)
            prompt = implementation.build_kg_prompt(projected, context)
            if not self._valid_prompt(prompt):
                return IndexingPipelineKgPromptResult(
                    None, "indexing_pipeline_invalid_kg_prompt"
                )
            return IndexingPipelineKgPromptResult(prompt)
        except Exception:
            return IndexingPipelineKgPromptResult(
                None, "indexing_pipeline_kg_prompt_failed"
            )

    def map_kg_response(
        self,
        pipeline_id: str,
        response: str,
        elements: Sequence[object],
        *,
        doc_type: str,
        section_path: str,
        object_types: Sequence[str] = (),
    ) -> IndexingPipelineKgMapResult:
        registered = self._registrations.get(pipeline_id)
        if registered is None:
            return IndexingPipelineKgMapResult(None, "indexing_pipeline_missing")
        _plugin_id, contribution_id, implementation, descriptor = registered
        if not self._available(contribution_id, pipeline_id):
            return IndexingPipelineKgMapResult(
                None, "indexing_pipeline_unavailable"
            )
        if not descriptor.overrides_kg_extraction:
            return IndexingPipelineKgMapResult(None)
        try:
            projected = self._project_kg_elements(elements)
            context = self._kg_context(doc_type, section_path, object_types)
            fragment = implementation.map_kg_response(response, projected, context)
            if type(fragment) is not IndexingKgFragment:
                return IndexingPipelineKgMapResult(
                    None, "indexing_pipeline_invalid_kg_fragment"
                )
            return IndexingPipelineKgMapResult(fragment)
        except Exception:
            return IndexingPipelineKgMapResult(
                None, "indexing_pipeline_kg_mapper_failed"
            )


__all__ = ["IndexingPipelineHost"]
