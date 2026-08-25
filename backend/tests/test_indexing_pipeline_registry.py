from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    INDEXING_PIPELINE_POINT,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
    IndexingChunkProposal,
    IndexingPipelineDescriptor,
)
from app.extensions import build_extension_registry
from app.extensions.indexing import IndexingPipelineHost
from app.extensions.registry import ExtensionRegistryError


class _Pipeline:
    def __init__(self, pipeline_id: str, *, version: str = "1.0") -> None:
        self.descriptor = IndexingPipelineDescriptor(
            pipeline_id=pipeline_id,
            label=pipeline_id,
            description="test pipeline",
            version=version,
            overrides_chunking=True,
            overrides_kg_extraction=False,
        )

    def build_chunks(self, elements, _context):
        return tuple(
            IndexingChunkProposal(
                text=f"plugin:{item.text}", element_ids=(item.element_id,)
            )
            for item in elements
            if item.text
        )


@dataclass
class _Bundle:
    manifest: ExtensionManifest
    contributions: tuple[ExtensionContribution, ...]

    def register(self, registrar) -> None:
        for contribution in self.contributions:
            registrar.add_contributor(contribution)


def _bundle(
    plugin_id: str,
    *implementations: _Pipeline,
    availability=None,
) -> _Bundle:
    declarations = tuple(
        ContributionDeclaration(
            f"{plugin_id}-pipeline-{index}",
            INDEXING_PIPELINE_POINT,
            ContributionKind.CONTRIBUTOR,
        )
        for index, _implementation in enumerate(implementations)
    )
    manifest = ExtensionManifest(
        id=plugin_id,
        version="1.0",
        api_version=EXTENSION_API_VERSION,
        display_name=plugin_id,
        trust="builtin",
        contributions=declarations,
    )
    return _Bundle(
        manifest,
        tuple(
            ExtensionContribution(declaration, implementation, availability)
            for declaration, implementation in zip(declarations, implementations)
        ),
    )


def test_multiple_pipeline_contributors_are_startup_frozen_and_ordered():
    registry = build_extension_registry(
        (
            _bundle("zeta", _Pipeline("zeta.semantic")),
            _bundle("alpha", _Pipeline("alpha.section")),
        )
    )

    host = IndexingPipelineHost(registry)

    assert [option.pipeline_id for option in host.options()] == [
        "alpha.section",
        "zeta.semantic",
    ]
    assert registry.frozen is True


@pytest.mark.parametrize(
    "pipeline_id",
    ["", "builtin", "other.chunker", "alpha", "alpha.UPPER"],
)
def test_pipeline_id_must_be_namespaced_to_its_plugin(pipeline_id):
    registry = build_extension_registry((_bundle("alpha", _Pipeline(pipeline_id)),))

    with pytest.raises(ExtensionRegistryError, match="namespace"):
        IndexingPipelineHost(registry)


def test_duplicate_pipeline_id_fails_closed_at_startup():
    registry = build_extension_registry(
        (_bundle("alpha", _Pipeline("alpha.same"), _Pipeline("alpha.same")),)
    )

    with pytest.raises(ExtensionRegistryError, match="duplicate indexing pipeline"):
        IndexingPipelineHost(registry)


def test_availability_is_live_but_internal_reason_is_not_projected():
    state = {"available": False}

    def availability(_context):
        if state["available"]:
            return Availability.available()
        return Availability(AvailabilityStatus.UNAVAILABLE, "secret_internal_reason")

    host = IndexingPipelineHost(
        build_extension_registry(
            (_bundle("alpha", _Pipeline("alpha.live"), availability=availability),)
        )
    )

    assert host.option("alpha.live").available is False
    state["available"] = True
    assert host.option("alpha.live").available is True
    assert "secret_internal_reason" not in repr(host.options())
