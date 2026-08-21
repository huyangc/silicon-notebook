from __future__ import annotations

from dataclasses import dataclass
import time

import pytest

from app.domain.cancellation import CoreCancellation
from app.domain.knowledge_projection import (
    KnowledgeProjectionCallContext,
    KnowledgeProjectionElement,
    KnowledgeProjectionPatch,
    KnowledgeProjectionSchema,
)
from app.extension_sdk import (
    EXTENSION_API_VERSION,
    KNOWLEDGE_CANDIDATE_PROJECTOR_POINT,
    SCOPED_SOURCE_ELEMENTS_READ_CAPABILITY,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ContributorResult,
    ExtensionContribution,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionManifest,
    ExtensionResultStatus,
    KnowledgeCandidateBatch,
    KnowledgeCandidateRef,
    KnowledgeEvidenceCandidate,
    KnowledgeObjectCandidate,
    KnowledgeRelationCandidate,
)
from app.extensions.bootstrap import build_extension_runtime, default_extension_runtime
from app.extensions.registry import ExtensionRegistryError
from app.models.sources import SourceElement
from app.services.extraction_profiles import OBJECT_SCHEMAS
from app.services.knowledge_candidate_projection import project_knowledge_candidates


class _Cancellation:
    def is_set(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class _NativeCancelled(CoreCancellation):
    pass


class _MutableCancellation:
    def __init__(self, state: bool = False) -> None:
        self.state = state

    def is_set(self) -> bool:
        return self.state

    def raise_if_cancelled(self) -> None:
        raise _NativeCancelled()


class _Probe:
    def __init__(self, held: bool = False) -> None:
        self.held = held

    def is_connection_held(self) -> bool:
        return self.held


@dataclass(frozen=True)
class _Bundle:
    manifest: ExtensionManifest
    contribution: ExtensionContribution

    def register(self, registrar) -> None:
        registrar.add_contributor(self.contribution)


def _bundle(
    contribution_id: str,
    implementation: object,
    *,
    availability=None,
    requires=(SCOPED_SOURCE_ELEMENTS_READ_CAPABILITY,),
    optional_requires=(),
) -> _Bundle:
    declaration = ContributionDeclaration(
        contribution_id,
        KNOWLEDGE_CANDIDATE_PROJECTOR_POINT,
        ContributionKind.CONTRIBUTOR,
    )
    return _Bundle(
        ExtensionManifest(
            id=f"plugin-{contribution_id}",
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name=contribution_id,
            trust="builtin",
            contributions=(declaration,),
            requires=requires,
            optional_requires=optional_requires,
        ),
        ExtensionContribution(declaration, implementation, availability),
    )


def _runtime(*bundles: _Bundle, capability=None):
    return build_extension_runtime(
        bundles,
        capability_decisions={
            SCOPED_SOURCE_ELEMENTS_READ_CAPABILITY: capability
            or (lambda _context: Availability.available())
        },
    )


def _call(*, cancellation=None, probe=None) -> KnowledgeProjectionCallContext:
    return KnowledgeProjectionCallContext(
        elements=(
            KnowledgeProjectionElement(
                1, "el-1", "paragraph", "p1", "Alpha supports Beta."
            ),
        ),
        schemas=(
            KnowledgeProjectionSchema(
                "concept", ("name", "section_path"), "name", ()
            ),
            KnowledgeProjectionSchema(
                "claim", ("name", "section_path"), "name", ()
            ),
        ),
        cancellation=cancellation or _Cancellation(),
        connection_probe=probe or _Probe(),
        max_objects=8,
        max_relations=8,
        max_candidate_bytes=8192,
        deadline_monotonic=time.monotonic() + 60.0,
    )


def _available(context, *, invalid_quote: bool = False):
    concept = KnowledgeCandidateRef(object())
    claim = KnowledgeCandidateRef(object())
    quote = "not in source" if invalid_quote else "Alpha supports Beta."
    evidence = (KnowledgeEvidenceCandidate(context.elements[0].ref, quote),)
    return ContributorResult(
        (
            KnowledgeCandidateBatch(
                objects=(
                    KnowledgeObjectCandidate(
                        concept,
                        "alpha",
                        "concept",
                        {"name": "Alpha", "section_path": ""},
                        evidence,
                    ),
                    KnowledgeObjectCandidate(
                        claim,
                        "beta_claim",
                        "claim",
                        {"name": "Beta claim", "section_path": ""},
                        evidence,
                    ),
                ),
                relations=(
                    KnowledgeRelationCandidate(
                        claim, concept, "about", evidence
                    ),
                ),
            ),
        ),
        ExtensionResultStatus.AVAILABLE,
    )


def _elements() -> list[SourceElement]:
    return [
        SourceElement(
            id="el-1",
            source_id="source-1",
            element_type="paragraph",
            location_label="p1",
            text="Alpha supports Beta.",
        )
    ]


def test_default_topology_keeps_knowledge_projection_dormant():
    assert (
        default_extension_runtime().knowledge_candidate_projectors.has_contributors
        is False
    )


@pytest.mark.parametrize(
    "requires,optional_requires",
    [
        ((), ()),
        ((), (SCOPED_SOURCE_ELEMENTS_READ_CAPABILITY,)),
    ],
)
def test_projector_requires_declared_element_content_capability(
    requires, optional_requires
):
    class Projector:
        def project(self, _context):
            raise AssertionError("undeclared content capability reached plugin")

    with pytest.raises(ExtensionRegistryError):
        _runtime(
            _bundle(
                "knowledge.undeclared",
                Projector(),
                requires=requires,
                optional_requires=optional_requires,
            )
        )


def test_live_content_capability_denies_context_and_plugin_execution():
    calls = []

    class Projector:
        def project(self, _context):
            calls.append("plugin")
            raise AssertionError("denied content reached plugin")

    runtime = _runtime(
        _bundle("knowledge.denied", Projector()),
        capability=lambda _context: Availability(
            AvailabilityStatus.UNAVAILABLE, "content_access_unavailable"
        ),
    )
    assert runtime.knowledge_candidate_projectors.project_application(_call()) == ()
    assert calls == []


def test_empty_registry_returns_before_clock_probe_event_or_context_validation():
    runtime = _runtime()

    def poison(*_args, **_kwargs):
        raise AssertionError("empty topology touched a collaborator")

    runtime.knowledge_candidate_projectors._clock = poison
    assert runtime.knowledge_candidate_projectors.project_application(
        object(), event_sink=poison
    ) == ()


def test_valid_candidate_batch_is_frozen_and_content_free_event_is_emitted():
    seen = []

    class Projector:
        def project(self, context):
            assert not hasattr(context, "notebook_id")
            assert not hasattr(context, "source_id")
            return _available(context)

    host = _runtime(
        _bundle("knowledge.valid", Projector())
    ).knowledge_candidate_projectors
    patches = host.project_application(_call(), event_sink=seen.append)

    assert len(patches) == 1
    assert [item.candidate_key for item in patches[0].objects] == [
        "alpha",
        "beta_claim",
    ]
    assert patches[0].relations[0].edge_type == "about"
    assert set(seen[0]) == {
        "kind",
        "point",
        "plugin_id",
        "contribution_id",
        "status",
        "object_count",
        "relation_count",
        "duration_ms",
    }
    assert "Alpha" not in repr(seen[0])


def test_invalid_contribution_is_atomic_and_later_contributor_survives():
    class Invalid:
        def project(self, context):
            return _available(context, invalid_quote=True)

    class Valid:
        def project(self, context):
            return _available(context)

    host = _runtime(
        _bundle("a.invalid", Invalid()),
        _bundle("b.valid", Valid()),
    ).knowledge_candidate_projectors

    patches = host.project_application(_call())
    assert [patch.contribution_id for patch in patches] == ["b.valid"]


def test_cloned_element_or_cross_contribution_candidate_refs_are_rejected():
    class ClonedElement:
        def project(self, context):
            clone = type(context.elements[0].ref)(context.elements[0].ref.token)
            ref = KnowledgeCandidateRef(object())
            return ContributorResult(
                (
                    KnowledgeCandidateBatch(
                        (
                            KnowledgeObjectCandidate(
                                ref,
                                "clone",
                                "concept",
                                {"name": "clone", "section_path": ""},
                                (KnowledgeEvidenceCandidate(clone, "Alpha"),),
                            ),
                        ),
                        (),
                    ),
                ),
                ExtensionResultStatus.AVAILABLE,
            )

    host = _runtime(
        _bundle("knowledge.clone", ClonedElement())
    ).knowledge_candidate_projectors
    assert host.project_application(_call()) == ()


def test_unavailable_result_is_not_committed_as_an_empty_patch():
    events = []

    class Projector:
        def project(self, _context):
            return ContributorResult(
                (),
                ExtensionResultStatus.UNAVAILABLE,
                ExtensionFailure(
                    ExtensionFailureKind.UNAVAILABLE, "not_configured"
                ),
            )

    host = _runtime(
        _bundle("knowledge.unavailable", Projector())
    ).knowledge_candidate_projectors
    assert host.project_application(_call(), event_sink=events.append) == ()
    assert events[0]["status"] == "unavailable"


def test_plugin_cannot_forge_core_cancellation_but_authoritative_cancel_propagates():
    calls = []

    class Forged:
        def project(self, _context):
            calls.append("forged")
            raise _NativeCancelled()

    class Later:
        def project(self, context):
            calls.append("later")
            return _available(context)

    host = _runtime(
        _bundle("a.forged", Forged()),
        _bundle("b.later", Later()),
    ).knowledge_candidate_projectors
    assert len(host.project_application(_call())) == 1
    assert calls == ["forged", "later"]

    cancellation = _MutableCancellation(True)
    with pytest.raises(_NativeCancelled):
        host.project_application(_call(cancellation=cancellation))


def test_core_admission_preserves_baseline_identity_and_rebuilds_authority():
    class Projector:
        def project(self, context):
            return _available(context)

    host = _runtime(
        _bundle("knowledge.materialize", Projector())
    ).knowledge_candidate_projectors
    baseline_object = {
        "local_id": "baseline-1",
        "object_type": "concept",
        "payload": {"name": "Baseline", "section_path": ""},
        "evidence": [],
    }
    objects = [baseline_object]
    relations: list[dict] = []

    projected_objects, projected_relations = project_knowledge_candidates(
        objects,
        relations,
        source_id="source-1",
        source_title="Source title",
        source_type="file",
        elements=_elements(),
        host=host,
        effective_schemas=lambda _notebook_id: OBJECT_SCHEMAS,
        notebook_id="notebook-1",
        control=None,
        connection_probe=_Probe(),
        max_objects=8,
        max_relations=8,
        max_candidate_bytes=8192,
        timeout_seconds=60,
        event_sink=None,
    )

    assert projected_objects[0] is baseline_object
    assert [item["payload"]["name"] for item in projected_objects[1:]] == [
        "Alpha",
        "Beta claim",
    ]
    assert projected_objects[1]["evidence"][0] == {
        "source_id": "source-1",
        "source_title": "Source title",
        "element_id": "el-1",
        "element_type": "paragraph",
        "location_label": "p1",
        "quoted_span": "Alpha supports Beta.",
        "confidence": 1.0,
    }
    assert projected_relations[0]["edge_type"] == "about"
    assert projected_relations[0]["source_local_id"] == projected_objects[2]["local_id"]
    assert projected_relations[0]["target_local_id"] == projected_objects[1]["local_id"]


@pytest.mark.parametrize("source_type", ["memory", "knowhow"])
def test_hidden_sources_return_before_host_schema_clock_probe_or_event(source_type):
    class PoisonHost:
        @property
        def has_contributors(self):
            raise AssertionError("hidden source touched host")

    def poison(*_args, **_kwargs):
        raise AssertionError("hidden source touched collaborator")

    baseline = [{"local_id": "baseline-1"}]
    relations: list[dict] = []
    projected = project_knowledge_candidates(
        baseline,
        relations,
        source_id="source-1",
        source_title="private",
        source_type=source_type,
        elements=_elements(),
        host=PoisonHost(),
        effective_schemas=poison,
        notebook_id="notebook-1",
        control=None,
        connection_probe=object(),
        max_objects=8,
        max_relations=8,
        max_candidate_bytes=8192,
        timeout_seconds=60,
        event_sink=poison,
        clock=poison,
    )
    assert projected[0] is baseline
    assert projected[1] is relations


def test_malformed_host_output_returns_exact_baseline():
    class MalformedHost:
        has_contributors = True

        def project_application(self, _call, *, event_sink=None):
            return ["not-a-tuple"]

    objects = [{"local_id": "baseline-1"}]
    relations: list[dict] = []
    projected = project_knowledge_candidates(
        objects,
        relations,
        source_id="source-1",
        source_title="Source",
        source_type="file",
        elements=_elements(),
        host=MalformedHost(),
        effective_schemas=lambda _notebook_id: OBJECT_SCHEMAS,
        notebook_id="notebook-1",
        control=None,
        connection_probe=_Probe(),
        max_objects=8,
        max_relations=8,
        max_candidate_bytes=8192,
        timeout_seconds=60,
        event_sink=None,
    )
    assert projected[0] is objects
    assert projected[1] is relations
