"""Host-level contract for ``source.element_enricher`` (T1).

Everything here exercises ``SourceElementEnricherHost`` through the real
registry and the real runtime composition; the only fakes are the contributor
implementations, the clock, the connection probe and the cancellation token.
Deadline behaviour is driven by a tripped fake clock rather than by sleeping,
so no test in this file waits longer than one 50ms join slice.
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
import json
import re
import threading
import time

import pytest

from app.domain.cancellation import CoreCancellation
from app.domain.element_enrichment import (
    ELEMENT_ENRICHMENT_METADATA_MAX_DEPTH,
    persisted_element_enrichment_size,
    valid_element_enrichment_owner,
)
from app.domain.extensions import (
    ElementAssetLocation,
    ElementEnrichmentCallContext,
    ElementEnrichmentPatch,
    ParsedElementEnvelope,
)
from app.extension_sdk import (
    EXTENSION_API_VERSION,
    SOURCE_ELEMENT_ENRICHER_POINT,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ContributorResult,
    ElementEnrichmentCandidate,
    ElementRef,
    ExtensionBundle,
    ExtensionContribution,
    ExtensionManifest,
    ExtensionRegistrar,
    ExtensionResultStatus,
)
from app.extensions.bootstrap import build_extension_runtime
from app.extensions.element_enrichment import _AssetReader
from app.extensions.registry import ExtensionRegistryError


EVENT_KEYS = {
    "kind",
    "plugin_id",
    "contribution_id",
    "status",
    "reason_code",
    "duration_ms",
    "count",
}


class _ConnectionProbe:
    def __init__(self, held: bool = False) -> None:
        self.held = held
        self.calls = 0

    def is_connection_held(self) -> bool:
        self.calls += 1
        return self.held


class _Cancellation:
    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled

    def set(self) -> None:
        self._cancelled = True

    def is_set(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise CoreCancellation()


def _bundle(
    plugin_id: str,
    implementation: object,
    *,
    contribution_id: str | None = None,
    kind: ContributionKind = ContributionKind.CONTRIBUTOR,
    version: str = "1.0.0",
    availability=None,
    add: str = "add_contributor",
) -> ExtensionBundle:
    declaration = ContributionDeclaration(
        contribution_id or f"{plugin_id}.main",
        SOURCE_ELEMENT_ENRICHER_POINT,
        kind,
    )

    @dataclass(frozen=True)
    class _Typed:
        manifest: ExtensionManifest
        contribution: ExtensionContribution

        def register(self, registrar: ExtensionRegistrar) -> None:
            getattr(registrar, add)(self.contribution)

    return _Typed(
        ExtensionManifest(
            id=plugin_id,
            version=version,
            api_version=EXTENSION_API_VERSION,
            display_name=plugin_id,
            trust="deployment",
            contributions=(declaration,),
        ),
        ExtensionContribution(declaration, implementation, availability),
    )


class _Plugin:
    """Records what it was handed and answers a canned result."""

    def __init__(self, result: object) -> None:
        self.result = result
        self.contexts: list[object] = []

    def enrich(self, context):
        self.contexts.append(context)
        return self.result


class _Raising:
    def __init__(self) -> None:
        self.calls = 0

    def enrich(self, context):
        self.calls += 1
        raise RuntimeError("plugin blew up")


class _TrippableClock:
    """Fake monotonic clock that jumps past ``DEADLINE`` once tripped."""

    DEADLINE = 1000.5

    def __init__(self) -> None:
        self.tripped = False

    def trip(self) -> None:
        self.tripped = True

    def __call__(self) -> float:
        return 1002.0 if self.tripped else 1000.0


def _envelope(
    ordinal: int,
    *,
    element_type: str = "paragraph",
    text: str = "body",
    caption: str = "",
    description: str = "",
    asset_id: str = "",
    asset_mime: str = "",
) -> ParsedElementEnvelope:
    return ParsedElementEnvelope(
        ordinal,
        element_type,
        f"p{ordinal}",
        text,
        caption,
        description,
        asset_id,
        asset_mime,
    )


def _call(
    *,
    elements: tuple[ParsedElementEnvelope, ...] | None = None,
    asset_locations: dict[str, ElementAssetLocation] | None = None,
    probe: object | None = None,
    cancellation: object | None = None,
    max_proposals: int = 8,
    max_metadata_bytes: int = 4096,
    max_description_chars: int = 256,
    max_asset_bytes: int = 1024,
    deadline: float | None = None,
) -> ElementEnrichmentCallContext:
    return ElementEnrichmentCallContext(
        elements if elements is not None else (_envelope(1), _envelope(2)),
        asset_locations if asset_locations is not None else {},
        cancellation if cancellation is not None else _Cancellation(),
        probe if probe is not None else _ConnectionProbe(),
        max_proposals,
        max_metadata_bytes,
        max_description_chars,
        max_asset_bytes,
        deadline if deadline is not None else time.monotonic() + 60.0,
    )


def _host(*bundles, event_sink=None, clock=None):
    runtime = build_extension_runtime(bundles, event_sink=event_sink)
    host = runtime.element_enrichers
    if clock is not None:
        host._clock = clock
    return host


def _result(*items, status=ExtensionResultStatus.AVAILABLE, failure=None):
    return ContributorResult(items, status, failure)


# A sentinel rather than ``None``: ``None`` is itself a metadata value worth
# rejecting, so it must be passable through this helper.
_UNSET = object()


def _candidate(context, index: int, *, metadata=_UNSET, description: str = ""):
    return ElementEnrichmentCandidate(
        context.elements[index].ref,
        {"ok": True} if metadata is _UNSET else metadata,
        description,
    )


# --------------------------------------------------------------------------
# Topology, startup freeze
# --------------------------------------------------------------------------


def test_empty_topology_is_a_strict_no_op():
    def poison(*_args, **_kwargs):
        raise AssertionError("strict empty topology touched a collaborator")

    events: list[dict[str, object]] = []
    host = _host(event_sink=events.append, clock=poison)
    probe = _ConnectionProbe()

    # Not even a shaped call context: the short circuit precedes validation.
    assert host.enrich_application(object()) == ()
    assert host.enrich_application(_call(probe=probe)) == ()
    assert probe.calls == 0
    assert events == []
    assert host.has_contributions() is False


def test_registered_topology_is_visible_without_a_call():
    host = _host(_bundle("corp.enricher", _Plugin(_result())))
    assert host.has_contributions() is True


def test_a_non_contributor_kind_refuses_to_start():
    with pytest.raises(ExtensionRegistryError):
        _host(
            _bundle(
                "corp.enricher",
                _Plugin(_result()),
                kind=ContributionKind.OBSERVER,
                add="add_observer",
            )
        )


def test_an_implementation_without_enrich_refuses_to_start():
    class NoEnrich:
        pass

    with pytest.raises(ExtensionRegistryError):
        _host(_bundle("corp.enricher", NoEnrich()))

    class NotCallable:
        enrich = "nope"

    with pytest.raises(ExtensionRegistryError):
        _host(_bundle("corp.enricher", NotCallable()))


def test_unpersistable_provenance_refuses_to_start():
    # A manifest version that could not be written into the persisted owner
    # envelope is a deployment mistake, caught at composition rather than
    # silently dropping every batch at runtime.
    for version in ("1.0.0dev", "v" * 65, ""):
        assert not valid_element_enrichment_owner(
            "corp.enricher", version, "corp.enricher.main"
        )
    for version in ("1.0.0dev", "v" * 65):
        with pytest.raises(ExtensionRegistryError):
            _host(_bundle("corp.enricher", _Plugin(_result()), version=version))


def test_a_decorative_version_is_not_a_reason_to_refuse_to_start():
    # The registry itself asks only that a version be non-empty, and a version
    # is a persisted *value*, not a key: holding it to the stable-id shape
    # would take a whole deployment's backend down over cosmetics.
    assert valid_element_enrichment_owner(
        "corp.enricher", "1.0.0-rc1 (build 7)", "corp.enricher.main"
    )
    host = _host(
        _bundle(
            "corp.enricher", _Plugin(_result()), version="1.0.0-rc1 (build 7)"
        )
    )
    assert host.has_contributions() is True


def test_provenance_and_contract_failures_read_differently():
    with pytest.raises(ExtensionRegistryError) as missing:
        _host(_bundle("corp.enricher", object()))
    with pytest.raises(ExtensionRegistryError) as provenance:
        _host(_bundle("corp.enricher", _Plugin(_result()), version="v" * 65))

    assert "implement the contributor contract" in str(missing.value)
    assert "persisted safely" in str(provenance.value)


# --------------------------------------------------------------------------
# Happy path and the projection a contributor sees
# --------------------------------------------------------------------------


def test_a_clean_batch_is_admitted_and_returned_as_patches():
    events: list[dict[str, object]] = []
    plugin = _Plugin(None)

    def enrich(context):
        plugin.contexts.append(context)
        return _result(
            _candidate(context, 0, metadata={"netlist": "R1 1 0 1k"}),
            _candidate(context, 1, description="second", metadata={"n": 2}),
        )

    plugin.enrich = enrich
    host = _host(_bundle("corp.enricher", plugin), event_sink=events.append)

    patches = host.enrich_application(_call())

    assert patches == (
        ElementEnrichmentPatch(
            1, "corp.enricher", "1.0.0", "corp.enricher.main",
            patches[0].metadata, "",
        ),
        ElementEnrichmentPatch(
            2, "corp.enricher", "1.0.0", "corp.enricher.main",
            patches[1].metadata, "second",
        ),
    )
    assert patches[0].metadata == {"netlist": "R1 1 0 1k"}
    assert [event["status"] for event in events] == ["available"]
    assert events[0]["count"] == 2


def test_admitted_metadata_is_plain_json_the_caller_can_encode():
    plugin = _Plugin(None)
    plugin.enrich = lambda context: _result(
        _candidate(
            context,
            0,
            metadata={
                "is_circuit": True,
                "pins": [1, 2, {"net": "gnd"}],
                "ratio": 0.5,
                "note": None,
            },
        )
    )
    host = _host(_bundle("corp.enricher", plugin))

    patch = host.enrich_application(_call())[0]

    # Plain dict/list all the way down, so the ingestion adapter can persist it
    # without knowing anything about the host's admission internals.
    assert type(patch.metadata) is dict
    assert type(patch.metadata["pins"]) is list
    assert type(patch.metadata["pins"][2]) is dict
    assert json.loads(json.dumps(patch.metadata)) == patch.metadata


def test_the_view_projects_core_fields_and_never_the_parser_mapping():
    plugin = _Plugin(_result())
    host = _host(_bundle("corp.enricher", plugin))
    element = _envelope(
        1,
        element_type="image",
        text="Figure 1",
        caption="cap",
        description="desc",
        asset_id="asset-1",
        asset_mime="image/png",
    )

    host.enrich_application(
        _call(
            elements=(element,),
            asset_locations={
                "asset-1": ElementAssetLocation("/nonexistent.png", "image/png")
            },
        )
    )

    view = plugin.contexts[0].elements[0]
    assert (view.element_type, view.location_label, view.text) == (
        "image", "p1", "Figure 1",
    )
    assert (view.caption, view.description) == ("cap", "desc")
    assert (view.asset_id, view.asset_mime) == ("asset-1", "image/png")
    assert plugin.contexts[0].budget.max_proposals == 8
    assert plugin.contexts[0].budget.max_asset_bytes == 1024


def test_an_unresolved_asset_is_not_advertised_to_the_plugin():
    # ``asset_id`` non-empty is the SDK's promise that there IS an image to
    # read; an asset core could not locate must not make that promise.
    plugin = _Plugin(_result())
    host = _host(_bundle("corp.enricher", plugin))

    host.enrich_application(
        _call(
            elements=(
                _envelope(1, element_type="image", asset_id="asset-1",
                          asset_mime="image/png"),
            ),
            asset_locations={},
        )
    )

    view = plugin.contexts[0].elements[0]
    assert (view.asset_id, view.asset_mime) == ("", "")


# --------------------------------------------------------------------------
# Element identity and proposal count
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flavour,reason_code",
    [
        ("forged", "invalid_element_ref"),
        ("duplicate", "duplicate_element_ref"),
        ("over_budget", "enrichment_budget_exceeded"),
        ("wrong_type", "invalid_element_enrichment_result"),
    ],
)
def test_bad_element_references_discard_the_whole_batch(flavour, reason_code):
    events: list[dict[str, object]] = []

    def enrich(context):
        if flavour == "forged":
            return _result(
                ElementEnrichmentCandidate(ElementRef(object()), {"ok": True})
            )
        if flavour == "duplicate":
            return _result(_candidate(context, 0), _candidate(context, 0))
        if flavour == "wrong_type":
            return _result(_candidate(context, 0), "not a candidate")
        return _result(_candidate(context, 0), _candidate(context, 1))

    plugin = _Plugin(None)
    plugin.enrich = enrich
    host = _host(_bundle("corp.enricher", plugin), event_sink=events.append)

    patches = host.enrich_application(
        _call(max_proposals=1 if flavour == "over_budget" else 8)
    )

    assert patches == ()
    assert events[0]["status"] == "invalid"
    # Distinct codes: "addressed an element it was not shown" and "sent
    # something that is not a candidate" are different operator problems.
    assert events[0]["reason_code"] == reason_code
    assert events[0]["count"] == 0


def test_a_ref_rebuilt_around_the_same_token_is_still_not_the_ref_core_issued():
    def enrich(context):
        stolen = context.elements[0].ref
        return _result(
            ElementEnrichmentCandidate(ElementRef(stolen.token), {"ok": True})
        )

    plugin = _Plugin(None)
    plugin.enrich = enrich
    host = _host(_bundle("corp.enricher", plugin))

    assert host.enrich_application(_call()) == ()


# --------------------------------------------------------------------------
# Metadata and description admission
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "metadata",
    [
        {"Bad": 1},
        {"1leading": 1},
        {"way_too_long" + "x" * 64: 1},
        {"nested": {"Bad": 1}},
        {"v": float("inf")},
        {"v": float("nan")},
        {"v": [1, [float("-inf")]]},
        {"v": object()},
        {"v": {"a": {"b": object()}}},
        "not a mapping",
        42,
        None,
    ],
)
def test_malformed_metadata_discards_the_whole_batch(metadata):
    events: list[dict[str, object]] = []

    def enrich(context):
        return _result(
            _candidate(context, 0, metadata=metadata),
            _candidate(context, 1, metadata={"fine": 1}),
        )

    plugin = _Plugin(None)
    plugin.enrich = enrich
    host = _host(_bundle("corp.enricher", plugin), event_sink=events.append)

    assert host.enrich_application(_call()) == ()
    assert events[0]["status"] == "invalid"
    assert events[0]["reason_code"] == "invalid_enrichment_metadata"


def _nested(levels: int) -> object:
    value: object = "leaf"
    for _ in range(levels):
        value = {"a": value}
    return value


def test_metadata_depth_is_bounded_at_the_domain_constant():
    accepted = _nested(ELEMENT_ENRICHMENT_METADATA_MAX_DEPTH)
    rejected = _nested(ELEMENT_ENRICHMENT_METADATA_MAX_DEPTH + 1)

    for metadata, expected in ((accepted, 1), (rejected, 0)):
        plugin = _Plugin(None)
        plugin.enrich = (
            lambda context, payload=metadata: _result(
                _candidate(context, 0, metadata=payload)
            )
        )
        host = _host(_bundle("corp.enricher", plugin))
        assert len(host.enrich_application(_call())) == expected


def test_the_persisted_byte_budget_is_per_contribution_and_cumulative():
    payload = {"blob": "x" * 200}
    one = persisted_element_enrichment_size(
        plugin_id="corp.enricher",
        plugin_version="1.0.0",
        contribution_id="corp.enricher.main",
        metadata=payload,
        description="",
    )

    def enrich(context):
        return _result(
            _candidate(context, 0, metadata=payload),
            _candidate(context, 1, metadata=payload),
        )

    plugin = _Plugin(None)
    plugin.enrich = enrich

    # Room for one candidate but not for two: the pair is over budget, and the
    # whole contribution is discarded rather than half-admitted.
    events: list[dict[str, object]] = []
    host = _host(_bundle("corp.enricher", plugin), event_sink=events.append)
    assert host.enrich_application(_call(max_metadata_bytes=one + 10)) == ()
    assert events[0]["reason_code"] == "enrichment_budget_exceeded"
    assert len(host.enrich_application(_call(max_metadata_bytes=2 * one))) == 2


def test_a_description_is_charged_twice_because_it_is_persisted_twice():
    # The ingestion adapter writes it into the element's own description AND
    # appends a flattened copy to the element's text, so a budget that counted
    # it once would under-bound what is actually written.
    body = "x" * 100
    without = persisted_element_enrichment_size(
        plugin_id="corp.enricher",
        plugin_version="1.0.0",
        contribution_id="corp.enricher.main",
        metadata={},
        description="",
    )
    with_body = persisted_element_enrichment_size(
        plugin_id="corp.enricher",
        plugin_version="1.0.0",
        contribution_id="corp.enricher.main",
        metadata={},
        description=body,
    )

    assert with_body - without >= 2 * len(body)


@pytest.mark.parametrize(
    "description",
    ["bell\x07here", "null\x00here", "esc\x1bhere"],
)
def test_control_characters_in_a_description_discard_the_batch(description):
    events: list[dict[str, object]] = []
    plugin = _Plugin(None)
    plugin.enrich = lambda context: _result(
        _candidate(context, 0, description=description)
    )
    host = _host(_bundle("corp.enricher", plugin), event_sink=events.append)

    assert host.enrich_application(_call()) == ()
    assert events[0]["reason_code"] == "invalid_enrichment_description"


def test_carriage_returns_are_normalised_rather_than_rejected():
    plugin = _Plugin(None)
    plugin.enrich = lambda context: _result(
        _candidate(context, 0, description="line one\r\nline two\rline three")
    )
    host = _host(_bundle("corp.enricher", plugin))

    patch = host.enrich_application(_call())[0]

    # A plugin that built its text on Windows proposes the same text as one
    # that did not, and the persisted value must not record which.
    assert patch.description == "line one\nline twoline three"
    assert "\r" not in patch.description


def test_newlines_and_tabs_survive_because_fenced_code_needs_them():
    plugin = _Plugin(None)
    body = "Function: divider\n\n```spice\nR1\t1 0 1k\n```"
    plugin.enrich = lambda context: _result(
        _candidate(context, 0, description=body)
    )
    host = _host(_bundle("corp.enricher", plugin))

    patches = host.enrich_application(_call())
    assert patches[0].description == body


def test_an_over_long_description_discards_the_batch():
    plugin = _Plugin(None)
    plugin.enrich = lambda context: _result(
        _candidate(context, 0, description="x" * 9)
    )
    host = _host(_bundle("corp.enricher", plugin))

    assert host.enrich_application(_call(max_description_chars=9)) != ()
    assert host.enrich_application(_call(max_description_chars=8)) == ()


# --------------------------------------------------------------------------
# Result status
# --------------------------------------------------------------------------


def test_an_unavailable_result_is_discarded_whole_and_partial_is_admitted():
    for status, expected in (
        (ExtensionResultStatus.UNAVAILABLE, 0),
        (ExtensionResultStatus.PARTIAL, 1),
        (ExtensionResultStatus.AVAILABLE, 1),
    ):
        events: list[dict[str, object]] = []
        plugin = _Plugin(None)
        plugin.enrich = lambda context, value=status: _result(
            _candidate(context, 0), status=value
        )
        host = _host(_bundle("corp.enricher", plugin), event_sink=events.append)

        assert len(host.enrich_application(_call())) == expected
        assert events[0]["status"] == status.value
        assert events[0]["count"] == expected


@pytest.mark.parametrize(
    "result",
    [
        None,
        "not a result",
        ContributorResult([], ExtensionResultStatus.AVAILABLE),
        ContributorResult((), "available"),
    ],
)
def test_a_malformed_result_object_is_invalid(result):
    plugin = _Plugin(result)
    events: list[dict[str, object]] = []
    host = _host(_bundle("corp.enricher", plugin), event_sink=events.append)

    assert host.enrich_application(_call()) == ()
    assert events[0]["status"] == "invalid"


# --------------------------------------------------------------------------
# Deadline, failure, isolation
# --------------------------------------------------------------------------


def test_a_hung_contributor_times_out_and_ends_the_point():
    events: list[dict[str, object]] = []
    clock = _TrippableClock()
    released = threading.Event()
    second = _Plugin(_result())

    def hang(context):
        clock.trip()
        released.wait(5.0)
        return _result()

    first = _Plugin(None)
    first.enrich = hang
    # The registry freezes contributions in contribution-id order, so the
    # names here decide which one gets its turn first.
    host = _host(
        _bundle("corp.first", first),
        _bundle("corp.second", second),
        event_sink=events.append,
        clock=clock,
    )

    try:
        patches = host.enrich_application(_call(deadline=clock.DEADLINE))
    finally:
        released.set()

    assert patches == ()
    assert [event["reason_code"] for event in events] == [
        "element_enricher_timeout"
    ]
    # The point ended: the second contributor was never started, so it never
    # saw a context at all.
    assert second.contexts == []


def test_a_result_that_arrives_after_the_deadline_is_still_a_timeout():
    # "Past the deadline" is a fact about the clock, not about which join
    # slice happened to notice: a contributor whose worker returns a perfectly
    # valid batch, but only after its budget was spent, must be abandoned just
    # as surely as one that never returned.
    events: list[dict[str, object]] = []
    clock = _TrippableClock()
    second = _Plugin(_result())

    def late(context):
        clock.trip()
        return _result(_candidate(context, 0))

    first = _Plugin(None)
    first.enrich = late
    host = _host(
        _bundle("corp.first", first),
        _bundle("corp.second", second),
        event_sink=events.append,
        clock=clock,
    )

    patches = host.enrich_application(_call(deadline=clock.DEADLINE))

    assert patches == ()
    assert [event["reason_code"] for event in events] == [
        "element_enricher_timeout"
    ]
    assert second.contexts == []


def test_a_reader_dies_with_its_own_turn_not_with_the_whole_point(tmp_path):
    # Contributor A leaves a background thread behind and returns.  While B
    # has the floor, A's leftover thread must not still be reading this
    # notebook's images — even though the point as a whole is still running.
    proceed = threading.Event()
    during_turn: list[object] = []
    after_turn: list[object] = []

    def _linger(context):
        reader = context.assets
        ref = context.elements[0].ref
        during_turn.append(reader.read(ref))

        def _background() -> None:
            proceed.wait(5.0)
            after_turn.append(reader.read(ref))

        threading.Thread(target=_background, daemon=True).start()
        return _result()

    def _second(_context):
        proceed.set()
        for _ in range(400):
            if after_turn:
                break
            time.sleep(0.005)
        return _result()

    first = _Plugin(None)
    first.enrich = _linger
    second = _Plugin(None)
    second.enrich = _second
    host = _host(_bundle("corp.first", first), _bundle("corp.second", second))

    host.enrich_application(_image_call(tmp_path, b"\x89PNGdata"))

    # The first assertion is what keeps the second one honest: the image IS
    # readable during A's own turn, so the ``None`` below is the closed reader
    # and not a missing file.
    assert during_turn == [b"\x89PNGdata"]
    assert after_turn == [None]


def test_an_abandoned_contributors_reader_is_closed_too(tmp_path):
    clock = _TrippableClock()
    released = threading.Event()
    handles: list[tuple[object, object]] = []

    def hang(context):
        handles.append((context.assets, context.elements[0].ref))
        clock.trip()
        released.wait(5.0)
        return _result()

    plugin = _Plugin(None)
    plugin.enrich = hang
    host = _host(_bundle("corp.enricher", plugin), clock=clock)

    try:
        host.enrich_application(
            _image_call(tmp_path, b"\x89PNGdata", deadline=clock.DEADLINE)
        )
    finally:
        released.set()

    reader, ref = handles[0]
    # Abandoned, not merely finished: the host stopped waiting and the reader
    # went with it, even though the worker is still running.
    assert reader.read(ref) is None


def test_a_raising_contributor_fails_open_and_the_next_one_still_runs():
    events: list[dict[str, object]] = []
    first = _Raising()
    second = _Plugin(None)
    second.enrich = lambda context: _result(_candidate(context, 1))
    host = _host(
        _bundle("corp.broken", first),
        _bundle("corp.good", second),
        event_sink=events.append,
    )

    patches = host.enrich_application(_call())

    assert first.calls == 1
    assert [patch.plugin_id for patch in patches] == ["corp.good"]
    assert patches[0].ordinal == 2
    assert [event["reason_code"] for event in events] == [
        "element_enricher_failed",
        "",
    ]


def test_one_invalid_contribution_does_not_touch_another():
    bad = _Plugin(None)
    bad.enrich = lambda context: _result(
        ElementEnrichmentCandidate(ElementRef(object()), {"ok": True})
    )
    good = _Plugin(None)
    good.enrich = lambda context: _result(_candidate(context, 0))
    host = _host(_bundle("corp.bad", bad), _bundle("corp.good", good))

    patches = host.enrich_application(_call())

    assert [patch.plugin_id for patch in patches] == ["corp.good"]


def test_an_unavailable_probe_skips_only_that_contribution():
    events: list[dict[str, object]] = []
    skipped = _Plugin(_result())
    good = _Plugin(None)
    good.enrich = lambda context: _result(_candidate(context, 0))
    host = _host(
        _bundle(
            "corp.first",
            skipped,
            availability=lambda _context: Availability(
                AvailabilityStatus.DISABLED, "api_key_missing"
            ),
        ),
        _bundle("corp.second", good),
        event_sink=events.append,
    )

    patches = host.enrich_application(_call())

    assert skipped.contexts == []
    assert [patch.plugin_id for patch in patches] == ["corp.second"]
    assert events[0]["reason_code"] == "api_key_missing"


def test_the_availability_context_carries_counts_but_no_content():
    seen: list[object] = []
    host = _host(
        _bundle(
            "corp.enricher",
            _Plugin(_result()),
            availability=lambda context: (
                seen.append(context) or Availability.available()
            ),
        )
    )

    host.enrich_application(
        _call(
            elements=(
                _envelope(1),
                _envelope(2, element_type="image", asset_id="asset-1"),
            ),
            asset_locations={
                "asset-1": ElementAssetLocation("/nonexistent.png", "image/png")
            },
        )
    )

    assert seen[0].plugin_id == "corp.enricher"
    assert seen[0].contribution_id == "corp.enricher.main"
    assert (seen[0].element_count, seen[0].image_count) == (2, 1)


def test_cancellation_propagates_rather_than_failing_open():
    cancellation = _Cancellation(cancelled=True)
    plugin = _Plugin(_result())
    host = _host(_bundle("corp.enricher", plugin))

    with pytest.raises(CoreCancellation):
        host.enrich_application(_call(cancellation=cancellation))
    assert plugin.contexts == []


# --------------------------------------------------------------------------
# Asset reader
# --------------------------------------------------------------------------


def _image_call(
    tmp_path,
    payload: bytes,
    *,
    max_asset_bytes: int = 1024,
    deadline: float | None = None,
):
    path = tmp_path / "figure.png"
    path.write_bytes(payload)
    return _call(
        elements=(
            _envelope(
                1, element_type="image", asset_id="asset-1",
                asset_mime="image/png",
            ),
        ),
        asset_locations={
            "asset-1": ElementAssetLocation(str(path), "image/png")
        },
        max_asset_bytes=max_asset_bytes,
        deadline=deadline,
    )


def test_the_reader_returns_bytes_for_an_element_with_an_image(tmp_path):
    captured: list[bytes | None] = []
    plugin = _Plugin(None)
    plugin.enrich = lambda context: (
        captured.append(context.assets.read(context.elements[0].ref))
        or _result()
    )
    host = _host(_bundle("corp.enricher", plugin))

    host.enrich_application(_image_call(tmp_path, b"\x89PNGdata"))

    assert captured == [b"\x89PNGdata"]


def test_the_reader_refuses_an_image_over_the_byte_cap(tmp_path):
    captured: list[bytes | None] = []
    plugin = _Plugin(None)
    plugin.enrich = lambda context: (
        captured.append(context.assets.read(context.elements[0].ref))
        or _result()
    )
    host = _host(_bundle("corp.enricher", plugin))

    host.enrich_application(
        _image_call(tmp_path, b"0123456789", max_asset_bytes=4)
    )

    assert captured == [None]


def test_the_reader_answers_none_for_unknown_refs_and_missing_files(tmp_path):
    captured: list[bytes | None] = []

    def enrich(context):
        captured.append(context.assets.read(ElementRef(object())))
        captured.append(context.assets.read(None))
        captured.append(context.assets.read(context.elements[0].ref))
        return _result()

    plugin = _Plugin(None)
    plugin.enrich = enrich
    host = _host(_bundle("corp.enricher", plugin))

    call = _call(
        elements=(
            _envelope(1, element_type="image", asset_id="asset-1"),
        ),
        asset_locations={
            "asset-1": ElementAssetLocation(
                str(tmp_path / "absent.png"), "image/png"
            )
        },
    )
    host.enrich_application(call)

    assert captured == [None, None, None]


def test_the_reader_is_dead_once_the_call_returns(tmp_path):
    handles: list[tuple[object, object]] = []
    plugin = _Plugin(None)
    plugin.enrich = lambda context: (
        handles.append((context.assets, context.elements[0].ref)) or _result()
    )
    host = _host(_bundle("corp.enricher", plugin))

    host.enrich_application(_image_call(tmp_path, b"\x89PNGdata"))

    reader, ref = handles[0]
    assert reader.read(ref) is None


def test_the_reader_rechecks_closure_on_the_far_side_of_the_read(tmp_path, monkeypatch):
    # Directly, because the race this guards cannot be staged through the
    # host: the turn can end while a read is already in flight, and the bytes
    # that read produced must not be handed back afterwards.
    path = tmp_path / "figure.png"
    path.write_bytes(b"\x89PNGdata")
    ref = ElementRef(object())
    reader = _AssetReader({id(ref): (ref, str(path))}, 1024)
    real_open = open

    class _ClosingHandle:
        def __init__(self, handle):
            self._handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self._handle.close()
            return False

        def read(self, size):
            payload = self._handle.read(size)
            reader.close()
            return payload

    monkeypatch.setattr(
        "builtins.open",
        lambda *args, **kwargs: _ClosingHandle(real_open(*args, **kwargs)),
    )

    assert reader.read(ref) is None


# --------------------------------------------------------------------------
# Caller lease, isolation and event shape
# --------------------------------------------------------------------------


def test_no_contextvars_leak_into_the_plugin_thread():
    scope = contextvars.ContextVar("frozen_ingestion_scope", default="unset")
    scope.set("notebook-42-ingestion-scope")
    observed: list[str] = []

    class Peeking:
        def enrich(self, _context):
            observed.append(scope.get())
            return _result()

    _host(_bundle("corp.peek", Peeking())).enrich_application(_call())

    # A fresh empty Context is what keeps a plugin from inheriting this job's
    # ambient state. copy_context() would break this.
    assert observed == ["unset"]
    assert scope.get() == "notebook-42-ingestion-scope"


@pytest.mark.parametrize("probe", ["held", "raising", "missing"])
def test_a_caller_held_lease_skips_the_whole_point(probe):
    class _Raising:
        def is_connection_held(self):
            raise RuntimeError("probe blew up")

    class _Missing:
        pass

    probes = {
        "held": _ConnectionProbe(held=True),
        "raising": _Raising(),
        "missing": _Missing(),
    }
    events: list[dict[str, object]] = []
    plugin = _Plugin(_result())
    host = _host(_bundle("corp.enricher", plugin), event_sink=events.append)

    assert host.enrich_application(_call(probe=probes[probe])) == ()
    assert plugin.contexts == []
    assert len(events) == 1
    assert events[0]["reason_code"] == "connection_lease_held"
    assert events[0]["status"] == "unavailable"


def test_every_event_carries_exactly_the_stable_receipt_fields():
    stable = re.compile(r"^[a-z][a-z0-9_]*$")
    events: list[dict[str, object]] = []
    good = _Plugin(None)
    good.enrich = lambda context: _result(_candidate(context, 0))
    broken = _Raising()
    forging = _Plugin(None)
    forging.enrich = lambda context: _result(
        ElementEnrichmentCandidate(ElementRef(object()), {"ok": True})
    )
    host = _host(
        _bundle("corp.good", good),
        _bundle("corp.broken", broken),
        _bundle("corp.forging", forging),
        event_sink=events.append,
    )

    host.enrich_application(_call())

    assert len(events) == 3
    for event in events:
        assert set(event) == EVENT_KEYS
        assert event["kind"] == "source_element_enricher_attempt"
        assert stable.fullmatch(str(event["status"]))
        assert event["reason_code"] == "" or stable.fullmatch(
            str(event["reason_code"])
        )
        assert type(event["duration_ms"]) is int
        assert event["duration_ms"] >= 0
        assert type(event["count"]) is int


def test_a_plugin_supplied_reason_code_that_is_not_stable_is_dropped():
    events: list[dict[str, object]] = []
    host = _host(
        _bundle(
            "corp.enricher",
            _Plugin(_result()),
            availability=lambda _context: Availability(
                AvailabilityStatus.DISABLED, "Key missing: /etc/secret.pem"
            ),
        ),
        event_sink=events.append,
    )

    host.enrich_application(_call())

    # The registry already rejects an unstable probe reason; whatever survives
    # that, the receipt still refuses to forward a non-code string.
    assert events[0]["reason_code"] in ("", "invalid_availability_reason")
    assert "secret" not in str(events[0])


def test_an_out_of_contract_call_context_is_refused_before_any_plugin_runs():
    plugin = _Plugin(_result())
    host = _host(_bundle("corp.enricher", plugin))

    assert host.enrich_application(_call(elements=())) == ()
    assert host.enrich_application(_call(max_proposals=0)) == ()
    assert host.enrich_application(_call(max_metadata_bytes=0)) == ()
    assert host.enrich_application(_call(max_description_chars=0)) == ()
    assert host.enrich_application(_call(max_asset_bytes=0)) == ()
    assert host.enrich_application(_call(deadline=float("inf"))) == ()
    assert host.enrich_application(
        _call(elements=(_envelope(2),))
    ) == ()
    assert plugin.contexts == []
