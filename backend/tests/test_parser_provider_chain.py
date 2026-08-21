from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.domain.cancellation import CoreCancellation
from app.extension_sdk import (
    EXTENSION_API_VERSION,
    PARSER_PROVIDER_CHAIN_POINT,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionFailure,
    ExtensionFailureKind,
    ExtensionManifest,
    ParserAdmissionDecision,
    ParserHostContext,
    ParserProposal,
    ParserRouteDecision,
    ParserSourceRef,
    ProviderAcceptance,
    ProviderChainAttempt,
    ProviderChainResult,
)
from app.extensions import build_extension_registry, default_extension_runtime
from app.extensions.builtin import (
    PARSER_BUILTIN_CONTRIBUTION_ID,
    PARSER_CLOUD_CONTRIBUTION_ID,
    PARSER_SELF_HOSTED_CONTRIBUTION_ID,
)
from app.extensions.parser_chain import (
    ParserChainCancelled,
    ParserProviderChainHost,
)


class NativeCancelled(RuntimeError):
    pass


class NativeCoreCancelled(CoreCancellation):
    pass


class _Token:
    def __init__(self, state=False, native_error=NativeCancelled):
        self.state = state
        self.native_error = native_error
        self.reads = 0

    def is_set(self):
        self.reads += 1
        return self.state() if callable(self.state) else self.state

    def raise_if_cancelled(self):
        if self.is_set():
            raise self.native_error()


class _Connection:
    def __init__(self, held=False):
        self.held = held
        self.calls = 0

    def is_connection_held(self):
        self.calls += 1
        return self.held


class _Access:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def probe(self):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result() if callable(self.result) else self.result


class _Link:
    def __init__(self):
        self.calls = 0

    def probe(self, context):
        self.calls += 1
        return context.access.probe()


@dataclass
class _Bundle:
    manifest: ExtensionManifest
    link: _Link
    availability: object | None = None

    def register(self, registrar):
        declaration = self.manifest.contributions[0]
        registrar.add_provider_chain_link(ExtensionContribution(
            declaration, self.link, self.availability
        ))


def _bundle(
    contribution_id: str,
    *,
    after: tuple[str, ...] = (),
    before: tuple[str, ...] = (),
    availability=None,
):
    declaration = ContributionDeclaration(
        contribution_id,
        PARSER_PROVIDER_CHAIN_POINT,
        ContributionKind.PROVIDER_CHAIN,
        after=after,
        before=before,
    )
    link = _Link()
    return _Bundle(
        ExtensionManifest(
            id=f"plugin.{contribution_id}",
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name=contribution_id,
            trust="builtin",
            contributions=(declaration,),
        ),
        link,
        availability,
    )


def _accept(contribution_id: str, value: object):
    return ProviderChainResult(
        ParserProposal(contribution_id, value),
        ProviderChainAttempt(ProviderAcceptance.ACCEPT, "accepted"),
    )


def _reject(reason="not_applicable"):
    return ProviderChainResult(
        None,
        ProviderChainAttempt(ProviderAcceptance.REJECT, reason),
    )


def _host(*bundles, events=None, cancellation_exceptions=(NativeCancelled,)):
    registry = build_extension_registry(bundles)
    return ParserProviderChainHost(
        registry,
        event_sink=(events.append if events is not None else None),
        cancellation_exceptions=cancellation_exceptions,
    )


def _run(
    host,
    accesses,
    *,
    baseline="legacy",
    source=None,
    token=None,
    route_policy=None,
    admit=None,
    materialize=None,
    contexts=None,
    warnings=None,
    events=None,
):
    source = source or ParserSourceRef("file", ".pdf")
    token = token or _Token()
    connections = {}

    def context_factory(contribution_id):
        if contexts is not None:
            contexts.append(contribution_id)
        connection = _Connection()
        connections[contribution_id] = connection
        return ParserHostContext(
            contribution_id,
            source,
            token,
            accesses[contribution_id],
            connection,
        )

    result = host.run(
        baseline,
        source=source,
        route_policy=route_policy or (
            lambda _link, _source: ParserRouteDecision(True, "local")
        ),
        context_factory=context_factory,
        admit=admit or (
            lambda _link, _proposal: ParserAdmissionDecision(True)
        ),
        materialize=materialize or (
            lambda _link, proposal: proposal.value
        ),
        cancellation=token,
        warning_sink=(warnings.append if warnings is not None else None),
        event_sink=(events.append if events is not None else None),
    )
    return result, connections


def test_empty_chain_returns_exact_baseline_without_touching_callbacks():
    host = _host()
    baseline = object()
    calls = []

    result = host.run(
        baseline,
        source=ParserSourceRef("file", ".pdf"),
        route_policy=lambda *_: calls.append("route"),
        context_factory=lambda *_: calls.append("context"),
        admit=lambda *_: calls.append("admit"),
        materialize=lambda *_: calls.append("materialize"),
    )

    assert result.value is baseline
    assert result.attempt.reason_code == "no_parser_links"
    assert calls == []


def test_nonempty_chain_requires_one_authoritative_cancellation_token():
    bundle = _bundle("candidate")
    calls = []

    result = _host(bundle).run(
        "legacy",
        source=ParserSourceRef("file", ".pdf"),
        route_policy=lambda *_: calls.append("route"),
        context_factory=lambda *_: calls.append("context"),
        admit=lambda *_: calls.append("admit"),
        materialize=lambda *_: calls.append("materialize"),
        cancellation=None,
    )

    assert result.value == "legacy"
    assert result.attempt.reason_code == "invalid_cancellation_token"
    assert calls == []


def test_reject_failure_and_invalid_result_continue_to_first_accept():
    bundles = tuple(_bundle(item) for item in ("a", "b", "c", "d"))
    accesses = {
        "a": _Access(_reject()),
        "b": _Access(RuntimeError("secret path /tmp/private.pdf")),
        "c": _Access(object()),
        "d": _Access(_accept("d", "parsed")),
    }
    events = []

    result, _ = _run(_host(*bundles), accesses, events=events)

    assert result.value == "parsed"
    assert [item["contribution_id"] for item in events] == ["a", "b", "c", "d"]
    assert all("secret" not in repr(item) and "/tmp" not in repr(item) for item in events)


def test_route_plan_freezes_before_io_but_accept_stops_later_availability():
    availability_calls = []
    first = _bundle("a")
    second = _bundle(
        "b",
        availability=lambda _context: (
            availability_calls.append("b") or Availability.available()
        ),
    )
    routes = []

    result, _ = _run(
        _host(first, second),
        {"a": _Access(_accept("a", "parsed")), "b": _Access(_reject())},
        route_policy=lambda link, _source: (
            routes.append(link) or ParserRouteDecision(True, "local")
        ),
    )

    assert result.value == "parsed"
    assert routes == ["a", "b"]
    assert availability_calls == []
    assert second.link.calls == 0


def test_prohibited_route_skips_availability_context_and_probe():
    availability_calls = []
    contexts = []
    bundle = _bundle(
        "cloud",
        availability=lambda _context: (
            availability_calls.append("availability") or Availability.available()
        ),
    )
    access = _Access(_accept("cloud", "external"))

    result, _ = _run(
        _host(bundle),
        {"cloud": access},
        route_policy=lambda *_: ParserRouteDecision(
            False, "public_cloud", "trust_boundary_denied"
        ),
        contexts=contexts,
    )

    assert result.value == "legacy"
    assert availability_calls == []
    assert contexts == []
    assert access.calls == 0


def test_live_availability_skips_execution_until_enabled():
    state = {"enabled": False}
    availability_contexts = []

    def availability(context):
        availability_contexts.append(context)
        return (
            Availability.available()
            if state["enabled"]
            else Availability(AvailabilityStatus.DISABLED, "feature_disabled")
        )

    bundle = _bundle(
        "optional",
        availability=availability,
    )
    access = _Access(_accept("optional", "parsed"))
    host = _host(bundle)

    first, _ = _run(host, {"optional": access})
    state["enabled"] = True
    second, _ = _run(host, {"optional": access})

    assert first.value == "legacy"
    assert second.value == "parsed"
    assert access.calls == 1
    assert len(availability_contexts) == 2
    assert all(
        context.cancellation is not None for context in availability_contexts
    )


def test_admission_rejection_has_zero_materialization_side_effects():
    persisted = []
    bundle = _bundle("candidate")

    result, _ = _run(
        _host(bundle),
        {"candidate": _Access(_accept("candidate", object()))},
        admit=lambda *_: ParserAdmissionDecision(False, "schema_rejected"),
        materialize=lambda *_: persisted.append("write"),
    )

    assert result.value == "legacy"
    assert persisted == []


def test_accepted_proposal_is_materialized_once_with_same_identity():
    proposal_value = object()
    seen = []
    bundle = _bundle("candidate")

    result, _ = _run(
        _host(bundle),
        {"candidate": _Access(_accept("candidate", proposal_value))},
        admit=lambda link, proposal: (
            seen.append(("admit", link, proposal.value is proposal_value))
            or ParserAdmissionDecision(True)
        ),
        materialize=lambda link, proposal: (
            seen.append(("materialize", link, proposal.value is proposal_value))
            or "committed"
        ),
    )

    assert result.value == "committed"
    assert seen == [
        ("admit", "candidate", True),
        ("materialize", "candidate", True),
    ]


def test_warning_is_core_route_policy_owned_and_only_emitted_on_accept():
    warnings = []
    bundle = _bundle("builtin")

    accepted, _ = _run(
        _host(bundle),
        {"builtin": _Access(_accept("builtin", "parsed"))},
        route_policy=lambda *_: ParserRouteDecision(
            True, "local", fallback_warning_code="high_fidelity_fallback"
        ),
        warnings=warnings,
    )

    assert accepted.attempt.warning_code == "high_fidelity_fallback"
    assert warnings == ["high_fidelity_fallback"]

    rejected_warnings = []
    rejected, _ = _run(
        _host(bundle),
        {"builtin": _Access(_reject())},
        route_policy=lambda *_: ParserRouteDecision(
            True, "local", fallback_warning_code="high_fidelity_fallback"
        ),
        warnings=rejected_warnings,
    )
    assert rejected.value == "legacy"
    assert rejected_warnings == []


def test_plugin_warning_and_subclassed_result_are_invalid_and_fail_open():
    class EvilResult(ProviderChainResult):
        pass

    bundle = _bundle("candidate")
    plugin_warning = ProviderChainResult(
        ParserProposal("candidate", "bad"),
        ProviderChainAttempt(
            ProviderAcceptance.ACCEPT, "accepted", "plugin_warning"
        ),
    )
    first, _ = _run(_host(bundle), {"candidate": _Access(plugin_warning)})
    second, _ = _run(
        _host(bundle),
        {"candidate": _Access(EvilResult(
            ParserProposal("candidate", "bad"),
            ProviderChainAttempt(ProviderAcceptance.ACCEPT, "accepted"),
        ))},
    )

    assert first.value == "legacy"
    assert second.value == "legacy"


def test_hostile_plugin_warning_never_reaches_core_callbacks():
    class HostileWarning:
        def __ne__(self, _other):
            return False

    admitted = []
    result = ProviderChainResult(
        ParserProposal("candidate", "bad"),
        ProviderChainAttempt(
            ProviderAcceptance.ACCEPT,
            "accepted",
            HostileWarning(),
        ),
    )

    visible, _ = _run(
        _host(_bundle("candidate")),
        {"candidate": _Access(result)},
        admit=lambda *_: admitted.append(True),
    )

    assert visible.value == "legacy"
    assert admitted == []


def test_hostile_proposal_contribution_id_never_reaches_core_callbacks():
    class EvilStr(str):
        def __eq__(self, _other):
            raise RuntimeError("hostile equality")

    admitted = []
    materialized = []
    bundle = _bundle("candidate")
    result = ProviderChainResult(
        ParserProposal(EvilStr("candidate"), "bad"),
        ProviderChainAttempt(ProviderAcceptance.ACCEPT, "accepted"),
    )

    visible, _ = _run(
        _host(bundle),
        {"candidate": _Access(result)},
        admit=lambda *_: admitted.append(True),
        materialize=lambda *_: materialized.append(True),
    )

    assert visible.value == "legacy"
    assert admitted == []
    assert materialized == []


def test_self_hosted_failure_never_enables_forbidden_cloud_fallback():
    self_hosted = _bundle("selfhost")
    cloud = _bundle("cloud", after=("selfhost",))
    builtin = _bundle("builtin", after=("cloud",))
    routes = []
    accesses = {
        "selfhost": _Access(RuntimeError("offline")),
        "cloud": _Access(_accept("cloud", "external")),
        "builtin": _Access(_accept("builtin", "local")),
    }

    def route(link, _source):
        routes.append(link)
        return ParserRouteDecision(
            link != "cloud",
            "public_cloud" if link == "cloud" else "local",
            "self_hosted_trust_boundary" if link == "cloud" else "",
        )

    result, _ = _run(
        _host(self_hosted, cloud, builtin), accesses, route_policy=route
    )

    assert result.value == "local"
    assert routes == ["selfhost", "cloud", "builtin"]
    assert accesses["cloud"].calls == 0


def test_route_plan_cannot_open_cloud_after_self_hosted_probe_failure():
    state = {"selfhost_failed": False}
    self_hosted = _bundle("selfhost")
    cloud = _bundle("cloud", after=("selfhost",))
    builtin = _bundle("builtin", after=("cloud",))

    def selfhost_failure():
        state["selfhost_failed"] = True
        raise RuntimeError("offline")

    accesses = {
        "selfhost": _Access(selfhost_failure),
        "cloud": _Access(_accept("cloud", "external")),
        "builtin": _Access(_accept("builtin", "local")),
    }

    def stateful_route(link, _source):
        if link == "cloud":
            return ParserRouteDecision(
                state["selfhost_failed"],
                "public_cloud",
                "self_hosted_trust_boundary",
            )
        return ParserRouteDecision(True, "local")

    result, _ = _run(
        _host(self_hosted, cloud, builtin),
        accesses,
        route_policy=stateful_route,
    )

    assert result.value == "local"
    assert accesses["selfhost"].calls == 1
    assert accesses["cloud"].calls == 0


def test_database_connection_guard_blocks_probe():
    bundle = _bundle("candidate")
    source = ParserSourceRef("file", ".pdf")
    token = _Token()
    access = _Access(_accept("candidate", "parsed"))
    host = _host(bundle)

    result = host.run(
        "legacy",
        source=source,
        route_policy=lambda *_: ParserRouteDecision(True, "local"),
        context_factory=lambda contribution_id: ParserHostContext(
            contribution_id, source, token, access, _Connection(True)
        ),
        admit=lambda *_: ParserAdmissionDecision(True),
        materialize=lambda *_: "committed",
        cancellation=token,
    )

    assert result.value == "legacy"
    assert access.calls == 0


@pytest.mark.parametrize("checkpoint", ["route", "probe", "admit"])
def test_native_core_cancellation_propagates_before_materialization(checkpoint):
    bundle = _bundle("candidate")
    token = _Token()
    persisted = []

    def set_cancel(value):
        token.state = True
        return value

    route = lambda *_: ParserRouteDecision(True, "local")
    access = _Access(_accept("candidate", "proposal"))
    admit = lambda *_: ParserAdmissionDecision(True)
    if checkpoint == "route":
        route = lambda *_: set_cancel(ParserRouteDecision(True, "local"))
    elif checkpoint == "probe":
        access = _Access(lambda: set_cancel(_accept("candidate", "proposal")))
    else:
        admit = lambda *_: set_cancel(ParserAdmissionDecision(True))

    with pytest.raises(NativeCancelled):
        _run(
            _host(bundle),
            {"candidate": access},
            token=token,
            route_policy=route,
            admit=admit,
            materialize=lambda *_: persisted.append("write"),
        )
    assert persisted == []


def test_materialization_finishes_before_late_native_cancellation_propagates():
    bundle = _bundle("candidate")
    token = _Token()
    persisted = []

    def materialize(*_args):
        persisted.append("write")
        token.state = True
        return "committed"

    with pytest.raises(NativeCancelled):
        _run(
            _host(bundle),
            {"candidate": _Access(_accept("candidate", "proposal"))},
            token=token,
            materialize=materialize,
        )
    assert persisted == ["write"]


def test_late_malformed_cancellation_cannot_relabel_completed_commit():
    bundle = _bundle("candidate")
    token = _Token()
    persisted = []

    def materialize(*_args):
        persisted.append("write")
        token.state = object()
        return "committed"

    result, _ = _run(
        _host(bundle),
        {"candidate": _Access(_accept("candidate", "proposal"))},
        token=token,
        materialize=materialize,
    )

    assert result.value == "committed"
    assert result.attempt.acceptance is ProviderAcceptance.ACCEPT
    assert persisted == ["write"]


def test_cancellation_from_final_warning_receipt_propagates_after_commit():
    bundle = _bundle("candidate")
    token = _Token()
    persisted = []
    warnings = []

    def materialize(*_args):
        persisted.append("write")
        return "committed"

    def warning_sink(code):
        warnings.append(code)
        token.state = True

    source = ParserSourceRef("file", ".pdf")
    access = _Access(_accept("candidate", "proposal"))
    connection = _Connection()

    with pytest.raises(NativeCancelled):
        _host(bundle).run(
            "legacy",
            source=source,
            route_policy=lambda *_: ParserRouteDecision(
                True,
                "local",
                fallback_warning_code="high_fidelity_fallback",
            ),
            context_factory=lambda contribution_id: ParserHostContext(
                contribution_id, source, token, access, connection
            ),
            admit=lambda *_: ParserAdmissionDecision(True),
            materialize=materialize,
            cancellation=token,
            warning_sink=warning_sink,
        )

    assert persisted == ["write"]
    assert warnings == ["high_fidelity_fallback"]


def test_malformed_or_hostile_cancellation_fails_open_before_commit():
    states = iter((False, False, object()))
    token = _Token(state=lambda: next(states, object()))
    persisted = []
    bundle = _bundle("candidate")

    result, _ = _run(
        _host(bundle),
        {"candidate": _Access(_accept("candidate", "proposal"))},
        token=token,
        materialize=lambda *_: persisted.append("write"),
    )

    assert result.value == "legacy"
    assert persisted == []


def test_true_then_hostile_native_reread_does_not_escape_as_cancellation():
    states = iter((True, object(), object()))
    token = _Token(state=lambda: next(states, object()), native_error=RuntimeError)
    bundle = _bundle("candidate")

    result, _ = _run(
        _host(bundle, cancellation_exceptions=(NativeCancelled,)),
        {"candidate": _Access(_accept("candidate", "proposal"))},
        token=token,
    )

    assert result.value == "legacy"
    assert result.attempt.reason_code == "invalid_cancellation_token"


def test_default_parser_topology_is_dag_ordered_but_not_wired_to_ingestion():
    runtime = default_extension_runtime()
    contributions = runtime.registry.contributions(PARSER_PROVIDER_CHAIN_POINT)

    assert [item.contribution.declaration.id for item in contributions] == [
        PARSER_SELF_HOSTED_CONTRIBUTION_ID,
        PARSER_CLOUD_CONTRIBUTION_ID,
        PARSER_BUILTIN_CONTRIBUTION_ID,
    ]

    app_root = Path(__file__).resolve().parents[1] / "app"
    for relative in ("services/parsers.py", "services/source_ingestion.py"):
        source = (app_root / relative).read_text(encoding="utf-8")
        assert "app.extension_sdk" not in source
        assert "app.extensions" not in source


def test_default_parser_link_order_matches_the_parser_registry_truth():
    from app.services.parser_registry import PARSER_ENGINES
    from app.services.parsers import (
        MINERU_CAPABLE_SUFFIXES,
        MINERU_FALLBACK_WARNING_SUFFIXES,
    )

    contribution_ids = [
        item.contribution.declaration.id
        for item in default_extension_runtime().registry.contributions(
            PARSER_PROVIDER_CHAIN_POINT
        )
    ]
    assert contribution_ids == [f"parser.{engine.id}" for engine in PARSER_ENGINES]
    mineru_suffixes = {
        f".{extension}"
        for engine in PARSER_ENGINES
        if engine.id.startswith("mineru_")
        for extension in engine.file_extensions
    }
    assert set(MINERU_CAPABLE_SUFFIXES) == mineru_suffixes
    assert set(MINERU_FALLBACK_WARNING_SUFFIXES) < mineru_suffixes


def test_default_parser_capabilities_are_live_and_point_specific():
    runtime = default_extension_runtime()
    source = ParserSourceRef("file", ".pdf")
    token = _Token()
    accesses = {
        PARSER_SELF_HOSTED_CONTRIBUTION_ID: _Access(_reject()),
        PARSER_CLOUD_CONTRIBUTION_ID: _Access(_reject()),
        PARSER_BUILTIN_CONTRIBUTION_ID: _Access(_accept(
            PARSER_BUILTIN_CONTRIBUTION_ID, "parsed"
        )),
    }

    result, _ = _run(
        runtime.parser_chain,
        accesses,
        source=source,
        token=token,
    )

    assert result.value == "parsed"
    assert [access.calls for access in accesses.values()] == [1, 1, 1]


def test_default_runtime_propagates_native_core_cancellation():
    token = _Token(state=True, native_error=NativeCoreCancelled)

    with pytest.raises(NativeCoreCancelled):
        _run(
            default_extension_runtime().parser_chain,
            {
                PARSER_SELF_HOSTED_CONTRIBUTION_ID: _Access(_reject()),
                PARSER_CLOUD_CONTRIBUTION_ID: _Access(_reject()),
                PARSER_BUILTIN_CONTRIBUTION_ID: _Access(_reject()),
            },
            token=token,
        )


def test_default_required_capability_is_live_before_access_projection():
    runtime = default_extension_runtime()
    source = ParserSourceRef("file", ".pdf")
    token = _Token()
    state = {"access": None}
    events = []
    probe = _Access(_accept(PARSER_SELF_HOSTED_CONTRIBUTION_ID, "parsed"))

    def context_factory(contribution_id):
        access = state["access"] if contribution_id == (
            PARSER_SELF_HOSTED_CONTRIBUTION_ID
        ) else _Access(_reject())
        return ParserHostContext(
            contribution_id, source, token, access, _Connection()
        )

    def run():
        return runtime.parser_chain.run(
            "legacy",
            source=source,
            route_policy=lambda link, _source: ParserRouteDecision(
                link == PARSER_SELF_HOSTED_CONTRIBUTION_ID,
                "private_service" if link == (
                    PARSER_SELF_HOSTED_CONTRIBUTION_ID
                ) else "local",
                "route_prohibited" if link != (
                    PARSER_SELF_HOSTED_CONTRIBUTION_ID
                ) else "",
            ),
            context_factory=context_factory,
            admit=lambda *_: ParserAdmissionDecision(True),
            materialize=lambda _link, proposal: proposal.value,
            cancellation=token,
            event_sink=events.append,
        )

    assert run().value == "legacy"
    assert probe.calls == 0
    assert events[0]["failure_code"] == "required_capability_unavailable"
    events.clear()
    state["access"] = probe
    assert run().value == "parsed"
    assert probe.calls == 1


def test_exact_true_without_registered_native_type_uses_host_cancellation():
    class PassiveToken(_Token):
        def raise_if_cancelled(self):
            return None

    bundle = _bundle("candidate")
    token = PassiveToken(state=True)
    with pytest.raises(ParserChainCancelled):
        _run(
            _host(bundle, cancellation_exceptions=()),
            {"candidate": _Access(_accept("candidate", "proposal"))},
            token=token,
        )
