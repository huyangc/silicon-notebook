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
    RetrievalContributionBudget,
    RetrievalExtensionContext,
    RetrievalRunRef,
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
        ),
        implementation,
        availability,
    )


class _Reader:
    def __init__(self, allowed=()) -> None:
        self.allowed = set(allowed)
        self.calls = []

    def allows_many(self, candidates):
        self.calls.append(candidates)
        return tuple(candidate.identity in self.allowed for candidate in candidates)


class _Connection:
    def __init__(self, held=False) -> None:
        self.held = held

    def is_connection_held(self) -> bool:
        return self.held


def _context(
    reader: _Reader,
    *,
    cancel=None,
    connection=None,
    max_items=20,
    max_tokens=200,
    deadline=None,
) -> RetrievalExtensionContext:
    return RetrievalExtensionContext(
        invocation="selected_evidence",
        actor=ActorRef("actor"),
        notebook=NotebookRef("notebook"),
        scope=FrozenRetrievalScopeRef("scope", narrowed=True),
        run=RetrievalRunRef("run", "ask"),
        cancellation=cancel or threading.Event(),
        budget=RetrievalContributionBudget(max_items, max_tokens, deadline),
        reader=reader,
        models=None,
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
        "connection",
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
    assert [item.identity for item in reader.calls[0]] == [
        "base",
        "early",
        "outside",
    ]


def test_budget_is_shared_across_contributors_and_never_consumes_baseline():
    first = _Contributor(_result(_candidate("one", token_cost=3)))
    second = _Contributor(_result(_candidate("two", token_cost=3)))
    reader = _Reader({"one", "two"})
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
    assert len(reader.calls[0]) == 20


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
