from __future__ import annotations

from dataclasses import dataclass
from dataclasses import fields
import threading

import pytest

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    ActorRef,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ContributorResult,
    EvidenceCandidate,
    EvidenceProvenance,
    ExtensionContribution,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionManifest,
    ExtensionResultStatus,
    FrozenRetrievalScopeRef,
    NotebookRef,
    RETRIEVAL_CONTRIBUTOR_POINT,
    RETRIEVAL_SCOPE_READER_CAPABILITY,
    RetrievalContributionBudget,
    RetrievalExtensionContext,
    RetrievalHostContext,
    RetrievalRunRef,
    SCHEDULED_MODEL_ACCESS_CAPABILITY,
)
from app.extensions import build_extension_runtime
from app.extensions.retrieval import RetrievalHostCancelled
from app.extensions.registry import ExtensionRegistryError


@dataclass
class _Bundle:
    manifest: ExtensionManifest
    implementation: object
    availability: object | None = None

    def register(self, registrar) -> None:
        registrar.add_contributor(
            ExtensionContribution(
                self.manifest.contributions[0],
                self.implementation,
                self.availability,
            )
        )


def _bundle(
    contribution_id: str,
    implementation: object,
    *,
    availability=None,
    requires=(),
    optional_requires=(),
) -> _Bundle:
    declaration = ContributionDeclaration(
        contribution_id,
        RETRIEVAL_CONTRIBUTOR_POINT,
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
            requires=tuple(requires),
            optional_requires=tuple(optional_requires),
        ),
        implementation,
        availability,
    )


class _Reader:
    def __init__(self, allowed=(), authoritative=None) -> None:
        self.allowed = set(allowed)
        self.authoritative = authoritative or {}
        self.calls = []

    def read(self, request):
        self.calls.append(request)
        return tuple(
            self.authoritative.get(identity, _candidate(identity))
            for identity in request.identities
            if identity in self.allowed
        )


class _Connection:
    def __init__(self, held=False) -> None:
        self.held = held

    def is_connection_held(self) -> bool:
        return self.held


class _CoreCancelled(RuntimeError):
    pass


class _Cancellation:
    def __init__(self) -> None:
        self.cancelled = False

    def is_set(self) -> bool:
        return self.cancelled

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise _CoreCancelled("core cancellation")


def _context(
    reader: _Reader,
    *,
    cancel=None,
    connection=None,
    max_items=20,
    max_tokens=200,
    max_proposals=100,
    deadline=None,
) -> RetrievalHostContext:
    return RetrievalHostContext(
        invocation="selected_evidence",
        actor=ActorRef("actor"),
        notebook=NotebookRef("notebook"),
        scope=FrozenRetrievalScopeRef("scope", narrowed=True),
        run=RetrievalRunRef("run", "ask"),
        cancellation=cancel or threading.Event(),
        budget=RetrievalContributionBudget(
            max_items, max_tokens, max_proposals, deadline
        ),
        admission_reader=reader,
        model_access=None,
        connection=connection or _Connection(),
    )


def _candidate(identity: str, *, value=None, token_cost=1, source_id="source"):
    return EvidenceCandidate(
        identity=identity,
        notebook_id="notebook",
        source_id=source_id,
        provenance=EvidenceProvenance("chunk", f"element-{identity}"),
        value=value if value is not None else identity,
        token_cost=token_cost,
    )


class _Contributor:
    invocations = frozenset({"selected_evidence"})

    def __init__(self, result=None, error=None, on_call=None) -> None:
        self.result = result
        self.error = error
        self.on_call = on_call
        self.calls = 0

    def contribute(self, context):
        self.calls += 1
        if self.on_call is not None:
            self.on_call(context)
        if self.error is not None:
            raise self.error
        return self.result


def _result(*candidates, status=ExtensionResultStatus.AVAILABLE, failure=None):
    return ContributorResult(tuple(candidates), status, failure)


def test_empty_registry_returns_exact_baseline_before_any_other_work():
    from app.services.retrieval_run import retrieval_run

    baseline = [object()]
    events = []
    runtime = build_extension_runtime(
        event_sink=events.append,
    )
    runtime.retrieval_contributors._clock = lambda: (_ for _ in ()).throw(
        AssertionError("empty host must not read the clock")
    )

    with retrieval_run(run_kind="ask_chunk", fanout_limit=1) as run:
        result = runtime.retrieval_contributors.run(
            baseline,
            invocation="selected_evidence",
            context_factory=lambda: (_ for _ in ()).throw(
                AssertionError("empty host must not build context")
            ),
        )

    assert result is baseline
    assert events == []
    assert run.fanout_acquires == 0


def test_retrieval_context_exposes_only_point_specific_capabilities():
    assert {field.name for field in fields(RetrievalExtensionContext)} == {
        "invocation",
        "actor",
        "notebook",
        "scope",
        "run",
        "cancellation",
        "budget",
        "reader",
        "models",
    }


def test_other_invocation_does_not_construct_context_or_run_contributor():
    contributor = _Contributor(_result(_candidate("new")))
    runtime = build_extension_runtime((_bundle("selected", contributor),))
    baseline = ["base"]

    result = runtime.retrieval_contributors.run(
        baseline,
        invocation="chunk_candidates",
        context_factory=lambda: (_ for _ in ()).throw(
            AssertionError("non-applicable invocation must stay lazy")
        ),
    )

    assert result is baseline
    assert contributor.calls == 0


def test_malformed_retrieval_contributor_fails_during_composition():
    with pytest.raises(ExtensionRegistryError, match="contributor contract"):
        build_extension_runtime((_bundle("malformed", object()),))


def test_valid_candidates_append_in_id_order_with_one_batch_scope_check():
    late = _Contributor(_result(_candidate("late", token_cost=2)))
    early = _Contributor(
        _result(
            _candidate("base", value="rewrite"),
            _candidate("early", token_cost=2),
            _candidate("outside", token_cost=2),
        )
    )
    reader = _Reader({"base", "early", "late"})
    runtime = build_extension_runtime(
        (_bundle("late", late), _bundle("early", early))
    )
    baseline = ["base"]

    result = runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        context_factory=lambda: _context(reader),
        baseline_identity=str,
    )

    assert result == ("base", "early", "late")
    assert result[0] is baseline[0]
    assert len(reader.calls) == 2
    assert list(reader.calls[0].identities) == [
        "base",
        "early",
        "outside",
    ]


def test_budget_is_shared_across_contributors_and_never_consumes_baseline():
    first = _Contributor(_result(_candidate("one", token_cost=3)))
    second = _Contributor(_result(_candidate("two", token_cost=3)))
    reader = _Reader({"one", "two"})
    reader.authoritative = {
        "one": _candidate("one", token_cost=3),
        "two": _candidate("two", token_cost=3),
    }
    runtime = build_extension_runtime(
        (_bundle("a", first), _bundle("b", second))
    )

    result = runtime.retrieval_contributors.run(
        ["baseline"],
        invocation="selected_evidence",
        context_factory=lambda: _context(
            reader, max_items=20, max_tokens=3
        ),
        baseline_identity=str,
    )

    assert result == ("baseline", "one")


def test_unavailable_is_live_and_never_executes_contributor():
    state = {"available": False}
    contributor = _Contributor(_result(_candidate("new")))

    def probe(_context):
        if state["available"]:
            return Availability.available()
        return Availability(AvailabilityStatus.DISABLED, "feature_disabled")

    reader = _Reader({"new"})
    runtime = build_extension_runtime(
        (_bundle("live", contributor, availability=probe),)
    )
    baseline = ["base"]
    call = lambda: runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        context_factory=lambda: _context(reader),
        baseline_identity=str,
    )

    assert call() is baseline
    assert contributor.calls == 0
    state["available"] = True
    assert call() == ("base", "new")
    assert contributor.calls == 1


@pytest.mark.parametrize(
    "contributor",
    [
        _Contributor(error=RuntimeError("secret query")),
        _Contributor(error=TimeoutError()),
        _Contributor(result=object()),
        _Contributor(
            _result(
                _candidate("ignored"),
                failure=ExtensionFailure(
                    ExtensionFailureKind.FAILED, "stable_failure"
                ),
            )
        ),
    ],
)
def test_optional_failure_timeout_and_invalid_result_keep_same_baseline(contributor):
    runtime = build_extension_runtime((_bundle("failure", contributor),))
    baseline = ["base"]

    result = runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        context_factory=lambda: _context(_Reader({"ignored"})),
        baseline_identity=str,
    )

    assert result is baseline


def test_scope_and_provenance_validation_is_batch_and_fail_closed():
    invalid = _candidate("invalid")
    invalid = EvidenceCandidate(
        identity=invalid.identity,
        notebook_id=invalid.notebook_id,
        source_id="",
        provenance=invalid.provenance,
        value=invalid.value,
        token_cost=invalid.token_cost,
    )
    invalid_provenance = EvidenceCandidate(
        identity="invalid-provenance",
        notebook_id="notebook",
        source_id="source",
        provenance=EvidenceProvenance("raw_database_row", "secret"),
        value="invalid-provenance",
        token_cost=1,
    )
    candidates = tuple(_candidate(f"hit-{index}") for index in range(20))
    contributor = _Contributor(_result(invalid, invalid_provenance, *candidates))
    reader = _Reader({"hit-0"})
    runtime = build_extension_runtime((_bundle("batch", contributor),))

    result = runtime.retrieval_contributors.run(
        ["base"],
        invocation="selected_evidence",
        context_factory=lambda: _context(reader),
        baseline_identity=str,
    )

    assert result == ("base", "hit-0")
    assert len(reader.calls) == 1
    assert len(reader.calls[0].identities) == 20


def test_core_cancellation_propagates_and_stops_later_contributors():
    cancel = threading.Event()
    first = _Contributor(
        _result(_candidate("one")), on_call=lambda _context: cancel.set()
    )
    later = _Contributor(_result(_candidate("two")))
    runtime = build_extension_runtime(
        (_bundle("a", first), _bundle("b", later))
    )

    with pytest.raises(RetrievalHostCancelled):
        runtime.retrieval_contributors.run(
            ["base"],
            invocation="selected_evidence",
            context_factory=lambda: _context(
                _Reader({"one", "two"}), cancel=cancel
            ),
            baseline_identity=str,
        )

    assert first.calls == 1
    assert later.calls == 0


def test_core_cancellation_during_availability_propagates_its_native_error():
    cancellation = _Cancellation()
    contributor = _Contributor(_result(_candidate("new")))

    def probe(_context):
        cancellation.cancelled = True
        return Availability(AvailabilityStatus.UNAVAILABLE, "feature_disabled")

    runtime = build_extension_runtime(
        (_bundle("cancel", contributor, availability=probe),)
    )

    with pytest.raises(_CoreCancelled):
        runtime.retrieval_contributors.run(
            ["base"],
            invocation="selected_evidence",
            context_factory=lambda: _context(
                _Reader({"new"}), cancel=cancellation
            ),
            baseline_identity=str,
            cancellation=cancellation,
        )

    assert contributor.calls == 0


def test_pre_cancelled_request_skips_availability_and_context():
    cancellation = _Cancellation()
    cancellation.cancelled = True
    probes = []
    contributor = _Contributor(_result(_candidate("new")))
    runtime = build_extension_runtime((
        _bundle(
            "pre_cancelled",
            contributor,
            availability=lambda context: probes.append(context) or Availability.available(),
        ),
    ))

    with pytest.raises(_CoreCancelled):
        runtime.retrieval_contributors.run(
            ["base"],
            invocation="selected_evidence",
            context_factory=lambda: (_ for _ in ()).throw(AssertionError()),
            baseline_identity=str,
            cancellation=cancellation,
        )

    assert probes == []
    assert contributor.calls == 0


def test_connection_held_blocks_contributor_before_io():
    contributor = _Contributor(_result(_candidate("new")))
    runtime = build_extension_runtime((_bundle("blocked", contributor),))
    baseline = ["base"]

    result = runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        context_factory=lambda: _context(
            _Reader({"new"}), connection=_Connection(held=True)
        ),
        baseline_identity=str,
    )

    assert result is baseline
    assert contributor.calls == 0


def test_events_have_exact_content_free_shape_and_sink_failure_is_fail_open():
    events = []
    contributor = _Contributor(error=RuntimeError("question and source secret"))
    runtime = build_extension_runtime(
        (_bundle("failure", contributor),), event_sink=events.append
    )
    baseline = ["base"]

    assert runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        context_factory=lambda: _context(_Reader()),
        baseline_identity=str,
    ) is baseline
    assert set(events[0]) == {
        "kind",
        "contribution_id",
        "outcome",
        "accepted_count",
        "dropped_count",
        "elapsed_ms",
        "failure_code",
    }
    assert "secret" not in str(events[0])

    failing_runtime = build_extension_runtime(
        (_bundle("failure", contributor),),
        event_sink=lambda _event: (_ for _ in ()).throw(RuntimeError("sink")),
    )
    assert failing_runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        context_factory=lambda: _context(_Reader()),
        baseline_identity=str,
    ) is baseline


def test_invocation_routing_is_frozen_against_implementation_mutation():
    contributor = _Contributor(_result(_candidate("new")))
    runtime = build_extension_runtime((_bundle("frozen_route", contributor),))
    contributor.invocations = frozenset({"chunk_candidates"})

    assert runtime.retrieval_contributors.run(
        ["base"],
        invocation="selected_evidence",
        context_factory=lambda: _context(_Reader({"new"})),
        baseline_identity=str,
    ) == ("base", "new")
    baseline = ["base"]
    assert runtime.retrieval_contributors.run(
        baseline,
        invocation="chunk_candidates",
        context_factory=lambda: (_ for _ in ()).throw(AssertionError()),
    ) is baseline


def test_capability_ports_are_projected_per_manifest_and_live_decision():
    contexts = {}
    plain = _Contributor(
        _result(_candidate("plain")),
        on_call=lambda context: contexts.setdefault("plain", context),
    )
    scoped = _Contributor(
        _result(_candidate("scoped")),
        on_call=lambda context: contexts.setdefault("scoped", context),
    )
    modeled = _Contributor(
        _result(_candidate("modeled")),
        on_call=lambda context: contexts.setdefault("modeled", context),
    )
    model = object()
    state = {"model": True}
    runtime = build_extension_runtime(
        (
            _bundle("plain", plain),
            _bundle(
                "scoped",
                scoped,
                requires=(RETRIEVAL_SCOPE_READER_CAPABILITY,),
            ),
            _bundle(
                "modeled",
                modeled,
                optional_requires=(SCHEDULED_MODEL_ACCESS_CAPABILITY,),
            ),
        ),
        capability_decisions={
            RETRIEVAL_SCOPE_READER_CAPABILITY: lambda _context: Availability.available(),
            SCHEDULED_MODEL_ACCESS_CAPABILITY: lambda _context: (
                Availability.available()
                if state["model"]
                else Availability(AvailabilityStatus.DISABLED, "model_disabled")
            ),
        },
    )
    reader = _Reader({"plain", "scoped", "modeled"})
    context = _context(reader)
    object.__setattr__(context, "model_access", model)

    runtime.retrieval_contributors.run(
        ["base"],
        invocation="selected_evidence",
        context_factory=lambda: context,
        baseline_identity=str,
    )

    assert contexts["plain"].reader is None
    assert contexts["plain"].models is None
    assert contexts["scoped"].reader is reader
    assert contexts["scoped"].models is None
    assert contexts["modeled"].reader is None
    assert contexts["modeled"].models is model


@pytest.mark.parametrize(
    "candidate",
    [
        EvidenceCandidate(
            "bad_token", "notebook", "source",
            EvidenceProvenance("chunk", "element"), "value", "oops",
        ),
        EvidenceCandidate(
            "bad_bool", "notebook", "source",
            EvidenceProvenance("chunk", "element"), "value", True,
        ),
        EvidenceCandidate(
            "bad_provenance", "notebook", "source", object(), "value", 1,
        ),
        EvidenceCandidate(
            [], "notebook", "source",
            EvidenceProvenance("chunk", "element"), "value", 1,
        ),
    ],
)
def test_malformed_candidate_fields_are_total_and_fail_open(candidate):
    contributor = _Contributor(_result(candidate))
    runtime = build_extension_runtime((_bundle("malformed_fields", contributor),))
    baseline = ["base"]

    assert runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        context_factory=lambda: _context(_Reader()),
        baseline_identity=str,
    ) is baseline


def test_invalid_result_and_availability_enums_are_fail_open():
    bad_result = ContributorResult((), "evil")
    contributor = _Contributor(bad_result)
    runtime = build_extension_runtime((
        _bundle(
            "bad_enums",
            contributor,
            availability=lambda _context: Availability("evil"),
        ),
    ))
    baseline = ["base"]

    assert runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        context_factory=lambda: _context(_Reader()),
        baseline_identity=str,
    ) is baseline
    assert contributor.calls == 0


def test_proposal_limit_bounds_reader_work_before_hydration():
    candidates = tuple(_candidate(f"hit-{index}") for index in range(1000))
    contributor = _Contributor(_result(*candidates))
    reader = _Reader({candidate.identity for candidate in candidates})
    runtime = build_extension_runtime((_bundle("bounded", contributor),))

    runtime.retrieval_contributors.run(
        ["base"],
        invocation="selected_evidence",
        context_factory=lambda: _context(
            reader, max_items=1, max_tokens=1, max_proposals=7
        ),
        baseline_identity=str,
    )

    assert len(reader.calls) == 1
    assert len(reader.calls[0].identities) == 7


def test_core_hydrated_value_wins_and_metadata_mismatch_is_rejected():
    proposal = _candidate("safe", value="plugin-forged-body")
    authority = _candidate("safe", value="core-authoritative-body")
    mismatch = _candidate("mismatch", source_id="forged-source")
    reader = _Reader(
        {"safe", "mismatch"},
        authoritative={
            "safe": authority,
            "mismatch": _candidate("mismatch", source_id="real-source"),
        },
    )
    contributor = _Contributor(_result(proposal, mismatch))
    runtime = build_extension_runtime((_bundle("authority", contributor),))

    assert runtime.retrieval_contributors.run(
        ["base"],
        invocation="selected_evidence",
        context_factory=lambda: _context(reader),
        baseline_identity=str,
    ) == ("base", "core-authoritative-body")


def test_unavailable_probe_gets_io_free_context_and_skips_full_context():
    observed = []
    contributor = _Contributor(_result(_candidate("new")))

    def probe(context):
        observed.append(context)
        assert not hasattr(context, "reader")
        assert not hasattr(context, "models")
        assert not hasattr(context, "connection")
        return Availability(AvailabilityStatus.DISABLED, "feature_disabled")

    runtime = build_extension_runtime((
        _bundle("io_free_probe", contributor, availability=probe),
    ))
    baseline = ["base"]
    assert runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        context_factory=lambda: (_ for _ in ()).throw(AssertionError()),
        baseline_identity=str,
    ) is baseline
    assert len(observed) == 1
    assert contributor.calls == 0


def test_atomic_admission_discards_whole_contribution_on_one_rejection():
    contributor = _Contributor(_result(
        _candidate("accepted"),
        _candidate("outside"),
    ))
    runtime = build_extension_runtime(
        (_bundle("selected_graph", contributor),),
        retrieval_admission_policies={"selected_graph": "atomic"},
    )
    baseline = ["base"]

    assert runtime.retrieval_contributors.run(
        baseline,
        invocation="selected_evidence",
        context_factory=lambda: _context(_Reader({"accepted"})),
        baseline_identity=str,
    ) is baseline


def test_registry_rejects_content_shaped_metadata_ids():
    with pytest.raises(ExtensionRegistryError, match="identifiers"):
        build_extension_runtime((
            _bundle("user question source title", _Contributor(_result())),
        ))
