from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
import time

import pytest

from app.domain.extensions import (
    ElementEnrichmentCallContext,
    ElementEnrichmentPatch,
    ParsedElementEnvelope,
)
from app.extension_sdk import (
    EXTENSION_API_VERSION,
    SOURCE_ELEMENT_CONTENT_ACCESS_CAPABILITY,
    SOURCE_ELEMENT_ENRICHER_POINT,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ContributorResult,
    ElementEnrichmentCandidate,
    ExtensionContribution,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionManifest,
    ExtensionResultStatus,
)
from app.extensions.bootstrap import build_extension_runtime, default_extension_runtime
from app.extensions.registry import ExtensionRegistryError
from app.models.sources import SourceElement
from app.services.source_element_enrichment import enrich_source_elements


class _Cancellation:
    def is_set(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class _Probe:
    def __init__(self, held: bool = False) -> None:
        self.held = held
        self.calls = 0

    def is_connection_held(self) -> bool:
        self.calls += 1
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
    requires=(SOURCE_ELEMENT_CONTENT_ACCESS_CAPABILITY,),
    optional_requires=(),
    version="1.0.0",
) -> _Bundle:
    declaration = ContributionDeclaration(
        contribution_id,
        SOURCE_ELEMENT_ENRICHER_POINT,
        ContributionKind.CONTRIBUTOR,
    )
    return _Bundle(
        ExtensionManifest(
            id=f"plugin-{contribution_id}",
            version=version,
            api_version=EXTENSION_API_VERSION,
            display_name=contribution_id,
            trust="builtin",
            contributions=(declaration,),
            requires=requires,
            optional_requires=optional_requires,
        ),
        ExtensionContribution(declaration, implementation, availability),
    )


def _runtime(*bundles: _Bundle):
    return build_extension_runtime(
        bundles,
        capability_decisions={
            SOURCE_ELEMENT_CONTENT_ACCESS_CAPABILITY: (
                lambda _context: Availability.available()
            )
        },
    )


def _call(
    *,
    probe: object | None = None,
    cancellation: object | None = None,
    max_proposals: int = 8,
    max_metadata_bytes: int = 4096,
):
    return ElementEnrichmentCallContext(
        elements=(
            ParsedElementEnvelope(1, "paragraph", "p1", "trusted caption", ""),
            ParsedElementEnvelope(2, "table", "p2", "row", "existing"),
        ),
        cancellation=cancellation or _Cancellation(),
        connection_probe=probe or _Probe(),
        max_proposals=max_proposals,
        max_metadata_bytes=max_metadata_bytes,
        max_caption_chars=128,
        deadline_monotonic=time.monotonic() + 60.0,
    )


def _available(*items) -> ContributorResult:
    return ContributorResult(tuple(items), ExtensionResultStatus.AVAILABLE)


def test_default_topology_keeps_element_enrichment_dormant():
    assert default_extension_runtime().element_enrichers.has_contributors is False


@pytest.mark.parametrize(
    "requires,optional_requires",
    [
        ((), ()),
        ((), (SOURCE_ELEMENT_CONTENT_ACCESS_CAPABILITY,)),
    ],
)
def test_element_enricher_requires_declared_content_capability(
    requires, optional_requires
):
    class Enricher:
        def enrich(self, _context):
            raise AssertionError("undeclared content capability reached plugin")

    with pytest.raises(ExtensionRegistryError):
        _runtime(
            _bundle(
                "enrich.undeclared",
                Enricher(),
                requires=requires,
                optional_requires=optional_requires,
            )
        )


def test_declared_content_capability_remains_live_and_can_deny_text_access():
    calls = []

    class Enricher:
        def enrich(self, _context):
            calls.append("called")
            return _available()

    runtime = build_extension_runtime(
        (_bundle("enrich.denied", Enricher()),),
        capability_decisions={
            SOURCE_ELEMENT_CONTENT_ACCESS_CAPABILITY: (
                lambda _context: Availability(
                    AvailabilityStatus.UNAVAILABLE,
                    "content_access_unavailable",
                )
            )
        },
    )
    assert runtime.element_enrichers.enrich_application(_call()) == ()
    assert calls == []


@pytest.mark.parametrize("version", ["v" * 65, "\ud800"])
def test_element_enricher_rejects_unsafe_or_unbounded_owner_version(version):
    class Enricher:
        def enrich(self, _context):
            return _available()

    with pytest.raises(ExtensionRegistryError):
        _runtime(_bundle("enrich.version", Enricher(), version=version))


def test_empty_registry_returns_before_context_clock_probe_or_event():
    runtime = _runtime()

    def poison(*_args, **_kwargs):
        raise AssertionError("empty registry touched a collaborator")

    runtime.element_enrichers._clock = poison
    assert runtime.element_enrichers.enrich_application(
        object(), event_sink=poison
    ) == ()


def test_hostile_cancellation_and_connection_getters_fail_open():
    class Enricher:
        def enrich(self, _context):
            raise AssertionError("hostile boundary reached plugin")

    class HostileCancellation:
        @property
        def is_set(self):
            raise RuntimeError("private cancellation failure")

    class HostileProbe:
        @property
        def is_connection_held(self):
            raise RuntimeError("private probe failure")

    host = _runtime(_bundle("enrich.boundary", Enricher())).element_enrichers
    assert host.enrich_application(
        _call(cancellation=HostileCancellation())
    ) == ()
    assert host.enrich_application(_call(probe=HostileProbe())) == ()


def test_contributor_gets_one_immutable_batch_and_returns_namespaced_patch():
    seen = []

    class Enricher:
        def enrich(self, context):
            seen.append(context)
            assert not hasattr(context.elements[0], "metadata")
            assert not hasattr(context.elements[0], "source_id")
            with pytest.raises(Exception):
                context.elements[0].text = "mutated"
            return _available(
                ElementEnrichmentCandidate(
                    context.elements[0].ref,
                    {"tags": ["grounded", {"score": 1.0}]},
                    "trusted caption",
                )
            )

    host = _runtime(_bundle("enrich.tags", Enricher())).element_enrichers
    patches = host.enrich_application(_call())
    assert len(seen) == 1
    assert len(seen[0].elements) == 2
    assert patches == (
        ElementEnrichmentPatch(
            1,
            "plugin-enrich.tags",
            "1.0.0",
            "enrich.tags",
            patches[0].metadata,
            "trusted caption",
        ),
    )
    assert type(patches[0].metadata) is MappingProxyType


def test_invalid_contribution_fails_open_without_blocking_later_contributor():
    calls = []

    class Invalid:
        def enrich(self, context):
            calls.append("invalid")
            return _available(
                ElementEnrichmentCandidate(context.elements[0].ref, {"bad": object()})
            )

    class Valid:
        def enrich(self, context):
            calls.append("valid")
            return _available(
                ElementEnrichmentCandidate(context.elements[1].ref, {"ok": True})
            )

    host = _runtime(
        _bundle("a.invalid", Invalid()), _bundle("b.valid", Valid())
    ).element_enrichers
    patches = host.enrich_application(_call())
    assert calls == ["invalid", "valid"]
    assert [patch.contribution_id for patch in patches] == ["b.valid"]


def test_ungrounded_caption_only_rejects_its_contribution():
    class Valid:
        def enrich(self, context):
            return _available(
                ElementEnrichmentCandidate(context.elements[0].ref, {"ok": True})
            )

    class Poison:
        def enrich(self, context):
            return _available(
                ElementEnrichmentCandidate(
                    context.elements[1].ref,
                    {"poison": True},
                    "invented caption",
                )
            )

    host = _runtime(
        _bundle("a.valid", Valid()), _bundle("b.poison", Poison())
    ).element_enrichers
    patches = host.enrich_application(_call())
    assert [patch.contribution_id for patch in patches] == ["a.valid"]


def test_full_persisted_namespace_counts_toward_byte_budget():
    class Enricher:
        def enrich(self, context):
            return _available(
                ElementEnrichmentCandidate(context.elements[0].ref, {})
            )

    host = _runtime(_bundle("enrich.bytes", Enricher())).element_enrichers
    # The old metadata+caption-only calculation admitted this empty payload.
    # The complete persisted owner/namespace envelope is necessarily larger.
    assert host.enrich_application(_call(max_metadata_bytes=10)) == ()


@pytest.mark.parametrize(
    "result_factory",
    [
        lambda context: ContributorResult(
            (
                ElementEnrichmentCandidate(context.elements[0].ref, {}),
                ElementEnrichmentCandidate(context.elements[0].ref, {}),
            ),
            ExtensionResultStatus.AVAILABLE,
        ),
        lambda context: ContributorResult(
            (ElementEnrichmentCandidate(type(context.elements[0].ref)(object()), {}),),
            ExtensionResultStatus.AVAILABLE,
        ),
        lambda context: ContributorResult(
            (ElementEnrichmentCandidate(context.elements[0].ref, {"Bad-Key": 1}),),
            ExtensionResultStatus.AVAILABLE,
        ),
        lambda context: ContributorResult(
            (ElementEnrichmentCandidate(context.elements[0].ref, {}),),
            ExtensionResultStatus.AVAILABLE,
            ExtensionFailure(ExtensionFailureKind.FAILED, "failed"),
        ),
        lambda context: ContributorResult(
            (ElementEnrichmentCandidate(context.elements[0].ref, {}),),
            ExtensionResultStatus.PARTIAL,
            ExtensionFailure(ExtensionFailureKind.FAILED, "partial"),
        ),
    ],
)
def test_atomic_result_validation_rejects_hostile_shapes(result_factory):
    class Enricher:
        def enrich(self, context):
            return result_factory(context)

    host = _runtime(_bundle("enrich.hostile", Enricher())).element_enrichers
    assert host.enrich_application(_call()) == ()


def test_unavailable_and_held_connection_do_not_enter_contributor():
    calls = []

    class Enricher:
        def enrich(self, _context):
            calls.append("called")
            return _available()

    unavailable = lambda _context: Availability(
        AvailabilityStatus.UNAVAILABLE, "not_ready"
    )
    host = _runtime(
        _bundle("enrich.unavailable", Enricher(), availability=unavailable)
    ).element_enrichers
    assert host.enrich_application(_call()) == ()
    assert calls == []

    host = _runtime(_bundle("enrich.held", Enricher())).element_enrichers
    assert host.enrich_application(_call(probe=_Probe(True))) == ()
    assert calls == []


def test_availability_cannot_leave_a_connection_for_later_contributors():
    probe = _Probe()
    calls = []

    def hostile_availability(_context):
        probe.held = True
        return Availability(AvailabilityStatus.UNAVAILABLE, "not_ready")

    class Enricher:
        def enrich(self, _context):
            calls.append("called")
            return _available()

    host = _runtime(
        _bundle(
            "a.unavailable",
            Enricher(),
            availability=hostile_availability,
        ),
        _bundle("b.later", Enricher()),
    ).element_enrichers
    assert host.enrich_application(_call(probe=probe)) == ()
    assert calls == []


def test_availability_that_consumes_deadline_does_not_enter_contributor():
    calls = []

    class Enricher:
        def enrich(self, _context):
            calls.append("called")
            return _available()

    host = _runtime(_bundle("enrich.deadline", Enricher())).element_enrichers
    moments = iter((0.0, 2.0))
    host._clock = lambda: next(moments)
    assert host.enrich_application(
        replace(_call(), deadline_monotonic=1.0)
    ) == ()
    assert calls == []


def test_budget_overflow_rejects_whole_contribution_without_truncation():
    class Enricher:
        def enrich(self, context):
            return _available(
                ElementEnrichmentCandidate(context.elements[0].ref, {}),
                ElementEnrichmentCandidate(context.elements[1].ref, {}),
            )

    host = _runtime(_bundle("enrich.too_many", Enricher())).element_enrichers
    assert host.enrich_application(_call(max_proposals=1)) == ()


def test_connection_left_held_by_one_contributor_blocks_core_commit_and_later_work():
    probe = _Probe()
    calls = []

    class First:
        def enrich(self, context):
            calls.append("first")
            probe.held = True
            return _available(
                ElementEnrichmentCandidate(context.elements[0].ref, {"ok": True})
            )

    class Later:
        def enrich(self, _context):
            calls.append("later")
            return _available()

    host = _runtime(
        _bundle("a.first", First()), _bundle("b.later", Later())
    ).element_enrichers
    assert host.enrich_application(_call(probe=probe)) == ()
    assert calls == ["first"]


def test_success_event_is_content_free_and_names_the_frozen_owner():
    class Enricher:
        def enrich(self, context):
            return _available(
                ElementEnrichmentCandidate(context.elements[0].ref, {"ok": True})
            )

    events = []
    host = _runtime(_bundle("enrich.event", Enricher())).element_enrichers
    assert len(host.enrich_application(_call(), event_sink=events.append)) == 1
    assert len(events) == 1
    assert events[0]["plugin_id"] == "plugin-enrich.event"
    assert events[0]["contribution_id"] == "enrich.event"
    assert set(events[0]) == {
        "kind",
        "point",
        "plugin_id",
        "contribution_id",
        "status",
        "count",
        "duration_ms",
    }
    assert not ({"text", "location", "source_id", "path", "error"} & set(events[0]))


def _element() -> SourceElement:
    return SourceElement(
        id="",
        source_id="source-1",
        element_type="paragraph",
        location_label="p1",
        text="trusted caption and body",
        metadata={"parser": "markdown", "asset_id": "asset-1"},
    )


class _FakeHost:
    has_contributors = True

    def __init__(self, patches):
        self.patches = patches
        self.calls = 0

    def enrich_application(self, context, *, event_sink=None):
        self.calls += 1
        assert len(context.elements) == 1
        return self.patches


def test_core_adapter_preserves_element_and_provenance_fields():
    baseline = _element()
    patch = ElementEnrichmentPatch(
        1,
        "plugin-tags",
        "1.0.0",
        "enrich.tags",
        MappingProxyType({"topics": ("one", "two")}),
        "trusted caption",
    )
    host = _FakeHost((patch,))
    result = enrich_source_elements(
        [baseline],
        host=host,
        connection_probe=_Probe(),
        max_proposals=8,
        max_metadata_bytes=4096,
        max_caption_chars=128,
        timeout_seconds=10.0,
        event_sink=None,
    )
    assert host.calls == 1
    assert result[0] is not baseline
    assert result[0].model_dump(exclude={"metadata"}) == baseline.model_dump(
        exclude={"metadata"}
    )
    assert result[0].metadata["parser"] == "markdown"
    assert result[0].metadata["asset_id"] == "asset-1"
    assert result[0].metadata["extensions"]["enrich.tags"] == {
        "plugin_id": "plugin-tags",
        "plugin_version": "1.0.0",
        "metadata": {"topics": ["one", "two"]},
        "caption": "trusted caption",
    }


def test_core_adapter_rejects_ungrounded_caption_and_forged_patch_atomically():
    baseline = _element()
    hostile = _FakeHost(
        (
            ElementEnrichmentPatch(
                1,
                "plugin-tags",
                "1.0.0",
                "enrich.tags",
                MappingProxyType({"ok": True}),
                "invented caption",
            ),
        )
    )
    result = enrich_source_elements(
        [baseline],
        host=hostile,
        connection_probe=_Probe(),
        max_proposals=8,
        max_metadata_bytes=4096,
        max_caption_chars=128,
        timeout_seconds=10.0,
        event_sink=None,
    )
    assert result[0] is baseline


def test_empty_host_adapter_returns_same_list_before_clock_or_snapshot():
    baseline = [_element()]
    host = _runtime().element_enrichers

    def poison():
        raise AssertionError("empty host touched clock")

    assert enrich_source_elements(
        baseline,
        host=host,
        connection_probe=object(),
        max_proposals=1,
        max_metadata_bytes=1,
        max_caption_chars=1,
        timeout_seconds=1.0,
        event_sink=None,
        clock=poison,
    ) is baseline


def test_core_adapter_treats_ordinary_host_failure_as_exact_baseline():
    baseline = [_element()]

    class FailingHost:
        has_contributors = True

        def enrich_application(self, _context, *, event_sink=None):
            raise RuntimeError("private failure")

    assert enrich_source_elements(
        baseline,
        host=FailingHost(),
        connection_probe=_Probe(),
        max_proposals=8,
        max_metadata_bytes=4096,
        max_caption_chars=128,
        timeout_seconds=10.0,
        event_sink=None,
    ) is baseline


def test_core_adapter_hostile_baseline_metadata_fails_open():
    class HostileMetadata(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("private metadata failure")

    baseline = SourceElement.model_construct(
        id="",
        source_id="source-1",
        element_type="paragraph",
        location_label="p1",
        text="trusted body",
        metadata=HostileMetadata(),
    )
    elements = [baseline]
    host = _FakeHost(())
    assert enrich_source_elements(
        elements,
        host=host,
        connection_probe=_Probe(),
        max_proposals=8,
        max_metadata_bytes=4096,
        max_caption_chars=128,
        timeout_seconds=10.0,
        event_sink=None,
    ) is elements


@pytest.mark.parametrize("version", ["v" * 65, "\ud800"])
def test_core_adapter_rejects_unsafe_owner_version(version):
    baseline = [_element()]
    host = _FakeHost(
        (
            ElementEnrichmentPatch(
                1,
                "plugin-tags",
                version,
                "enrich.tags",
                MappingProxyType({}),
            ),
        )
    )
    assert enrich_source_elements(
        baseline,
        host=host,
        connection_probe=_Probe(),
        max_proposals=8,
        max_metadata_bytes=4096,
        max_caption_chars=128,
        timeout_seconds=10.0,
        event_sink=None,
    ) is baseline


def test_core_adapter_counts_full_persisted_namespace_toward_budget():
    baseline = [_element()]
    host = _FakeHost(
        (
            ElementEnrichmentPatch(
                1,
                "plugin-tags",
                "1.0.0",
                "enrich.tags",
                MappingProxyType({}),
            ),
        )
    )
    assert enrich_source_elements(
        baseline,
        host=host,
        connection_probe=_Probe(),
        max_proposals=8,
        max_metadata_bytes=10,
        max_caption_chars=128,
        timeout_seconds=10.0,
        event_sink=None,
    ) is baseline
