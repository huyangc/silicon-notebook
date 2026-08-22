from __future__ import annotations

import inspect
from dataclasses import dataclass

import pytest

from app.domain.report_export import (
    REPORT_EXPORT_FORMAT_MARKDOWN,
    ReportExportBoundaryError,
    ReportExportError,
    ReportExporterCall,
    ReportExportSource,
)
from app.extension_sdk import (
    EXTENSION_API_VERSION,
    REPORT_EXPORTER_POINT,
    Availability,
    AvailabilityStatus,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
    ExtensionResultStatus,
    ProviderResult,
    ReportExportedItem,
)
from app.extensions.bootstrap import build_extension_registry, default_extension_runtime
from app.extensions.registry import ExtensionRegistryError
from app.extensions.report_export import ReportExporterHost
from app.services.report_export import export_completed_reports


class _Probe:
    def __init__(self) -> None:
        self.held = False
        self.calls = 0

    def is_connection_held(self) -> bool:
        self.calls += 1
        return self.held


class _Provider:
    def __init__(self) -> None:
        self.calls = []

    @staticmethod
    def formats():
        return (REPORT_EXPORT_FORMAT_MARKDOWN,)

    def export(self, context):
        self.calls.append(context)
        return ProviderResult(
            tuple(ReportExportedItem(item.ref, item.content_md) for item in context.reports),
            ExtensionResultStatus.AVAILABLE,
        )


class _HardAbort(BaseException):
    pass


@dataclass(frozen=True)
class _Bundle:
    provider: object
    plugin_id: str = "builtin.test_report_exporter"
    contribution_id: str = "builtin.test_report_exporter"
    kind: ContributionKind = ContributionKind.PROVIDER
    availability: object | None = None

    @property
    def manifest(self):
        declaration = ContributionDeclaration(
            self.contribution_id, REPORT_EXPORTER_POINT, self.kind
        )
        return ExtensionManifest(
            id=self.plugin_id,
            version="1.0.0",
            api_version=EXTENSION_API_VERSION,
            display_name="test Report exporter",
            trust="builtin",
            contributions=(declaration,),
        )

    def register(self, registrar):
        declaration = self.manifest.contributions[0]
        contribution = ExtensionContribution(
            declaration, self.provider, self.availability
        )
        if self.kind is ContributionKind.PROVIDER:
            registrar.add_provider(contribution)
        else:
            registrar.add_contributor(contribution)


def _host(bundle: _Bundle) -> ReportExporterHost:
    return ReportExporterHost(
        build_extension_registry((bundle,)),
        trusted_plugin_ids=frozenset({bundle.plugin_id}),
    )


def _sources() -> tuple[ReportExportSource, ...]:
    return (
        ReportExportSource("rep-1", ' A/B:*?"<>|\n ', "第一份\r\n[k1]\n"),
        ReportExportSource("rep-1", ' A/B:*?"<>|\n ', "第一份\r\n[k1]\n"),
        ReportExportSource("rep-2", "文" * 41, "第二份🙂\n"),
    )


def test_default_provider_preserves_markdown_and_exact_legacy_filenames():
    probe = _Probe()
    artifacts = export_completed_reports(
        _sources(),
        host=default_extension_runtime().report_exporter,
        connection_probe=probe,
    )
    assert [artifact.filename for artifact in artifacts] == [
        "A_B________-rep-1.md",
        "A_B________-rep-1-1.md",
        f"{'文' * 40}-rep-2.md",
    ]
    assert [artifact.content for artifact in artifacts] == [
        "第一份\r\n[k1]\n",
        "第一份\r\n[k1]\n",
        "第二份🙂\n",
    ]
    assert probe.calls >= 2


def test_provider_gets_only_opaque_refs_and_markdown_after_clear_probe():
    provider = _Provider()
    probe = _Probe()
    sources = _sources()[:1]
    rendered = _host(_Bundle(provider)).export_application(
        ReportExporterCall(sources, REPORT_EXPORT_FORMAT_MARKDOWN, probe)
    )
    assert rendered[0].content is sources[0].content_md
    context = provider.calls[0]
    assert context.format_id == "markdown"
    assert context.reports[0].content_md is sources[0].content_md
    assert not hasattr(context.reports[0], "report_id")
    assert not hasattr(context.reports[0], "question")
    assert not hasattr(context.reports[0], "notebook_id")
    assert not hasattr(context.reports[0], "created_by")
    assert not hasattr(context, "repository")
    assert not hasattr(context, "connection_probe")


def test_empty_wrong_kind_untrusted_and_multiple_provider_topologies_fail_closed():
    empty = ReportExporterHost(build_extension_registry())
    with pytest.raises(ReportExportError, match="report_exporter_unavailable"):
        empty.export_application(
            ReportExporterCall(_sources()[:1], "markdown", _Probe())
        )

    wrong = _Bundle(_Provider(), kind=ContributionKind.CONTRIBUTOR)
    with pytest.raises(ExtensionRegistryError, match="must be a provider"):
        _host(wrong)

    bundle = _Bundle(_Provider())
    with pytest.raises(ExtensionRegistryError, match="explicitly trusted"):
        ReportExporterHost(build_extension_registry((bundle,)))

    second = _Bundle(
        _Provider(),
        plugin_id="builtin.test_report_exporter_two",
        contribution_id="builtin.test_report_exporter_two",
    )
    with pytest.raises(ExtensionRegistryError, match="multiple single providers"):
        build_extension_registry((bundle, second))


def test_discovery_magic_methods_are_normalized_before_shape_comparison():
    class PoisonCallable:
        def __call__(self, *_args):
            return (REPORT_EXPORT_FORMAT_MARKDOWN,)

        @property
        def __name__(self):
            raise _HardAbort("secret inspect getter")

    class PoisonInspectProvider:
        formats = PoisonCallable()

        def export(self, _context):
            raise AssertionError("not reached")

    with pytest.raises(ExtensionRegistryError) as inspect_error:
        _host(_Bundle(PoisonInspectProvider()))
    assert str(inspect_error.value) == "invalid report exporter"
    assert "secret" not in str(inspect_error.value)

    class PoisonString(str):
        def __eq__(self, _other):
            raise _HardAbort("secret format equality")

        def __ne__(self, _other):
            raise _HardAbort("secret format inequality")

    class PoisonFormatProvider(_Provider):
        @staticmethod
        def formats():
            return (PoisonString(REPORT_EXPORT_FORMAT_MARKDOWN),)

    with pytest.raises(ExtensionRegistryError) as format_error:
        _host(_Bundle(PoisonFormatProvider()))
    assert str(format_error.value) == "invalid report exporter formats"
    assert "secret" not in str(format_error.value)


def test_unavailable_provider_is_not_invoked():
    provider = _Provider()
    bundle = _Bundle(
        provider,
        availability=lambda _context: Availability(
            AvailabilityStatus.UNAVAILABLE, "disabled_for_test"
        ),
    )
    with pytest.raises(ReportExportError, match="report_exporter_unavailable"):
        _host(bundle).export_application(
            ReportExporterCall(_sources()[:1], "markdown", _Probe())
        )
    assert provider.calls == []


def test_availability_cannot_leave_a_connection_for_provider_or_core():
    provider = _Provider()
    probe = _Probe()

    def availability(_context):
        probe.held = True
        return Availability.available()

    bundle = _Bundle(provider, availability=availability)
    with pytest.raises(ReportExportBoundaryError, match="connection_held"):
        _host(bundle).export_application(
            ReportExporterCall(_sources()[:1], "markdown", probe)
        )
    assert provider.calls == []


def test_availability_baseexception_rechecks_connection_and_hides_text():
    provider = _Provider()
    probe = _Probe()

    def availability(_context):
        probe.held = True
        raise _HardAbort("secret availability failure")

    with pytest.raises(ReportExportBoundaryError, match="connection_held") as caught:
        _host(_Bundle(provider, availability=availability)).export_application(
            ReportExporterCall(_sources()[:1], "markdown", probe)
        )
    assert "secret" not in str(caught.value)
    assert provider.calls == []


def test_availability_cannot_rewrite_the_host_source_snapshot():
    provider = _Provider()
    source = _sources()[0]

    def availability(_context):
        object.__setattr__(source, "content_md", "AVAILABILITY-REWRITE")
        return Availability.available()

    rendered = _host(
        _Bundle(provider, availability=availability)
    ).export_application(
        ReportExporterCall((source,), "markdown", _Probe())
    )
    assert rendered[0].content == "第一份\r\n[k1]\n"
    assert provider.calls[0].reports[0].content_md == "第一份\r\n[k1]\n"


@pytest.mark.parametrize("mutation", ["missing", "reordered", "clone", "changed"])
def test_malformed_provider_result_rejects_the_whole_batch(mutation):
    class Bad(_Provider):
        def export(self, context):
            values = [
                ReportExportedItem(item.ref, item.content_md)
                for item in context.reports
            ]
            if mutation == "missing":
                values.pop()
            elif mutation == "reordered":
                values.reverse()
            elif mutation == "clone":
                values[0] = ReportExportedItem(type(values[0].ref)(), values[0].content)
            else:
                values[0] = ReportExportedItem(values[0].ref, "rewritten")
            return ProviderResult(tuple(values), ExtensionResultStatus.AVAILABLE)

    with pytest.raises(ReportExportError, match="invalid_report_export_result"):
        _host(_Bundle(Bad())).export_application(
            ReportExporterCall(_sources()[:2], "markdown", _Probe())
        )


def test_provider_failure_is_stable_and_never_leaks_exception_text():
    class Failing(_Provider):
        def export(self, _context):
            raise ValueError("secret provider credential")

    with pytest.raises(ReportExportError) as caught:
        _host(_Bundle(Failing())).export_application(
            ReportExporterCall(_sources()[:1], "markdown", _Probe())
        )
    assert str(caught.value) == "report_exporter_failed"
    assert "secret" not in str(caught.value)


def test_provider_baseexception_rechecks_connection_and_hides_text():
    probe = _Probe()

    class Failing(_Provider):
        def export(self, _context):
            probe.held = True
            raise _HardAbort("secret hard abort")

    with pytest.raises(ReportExportBoundaryError, match="connection_held") as caught:
        _host(_Bundle(Failing())).export_application(
            ReportExporterCall(_sources()[:1], "markdown", probe)
        )
    assert "secret" not in str(caught.value)


def test_clear_connection_baseexceptions_map_to_stable_host_and_service_errors():
    class Failing(_Provider):
        def export(self, _context):
            raise _HardAbort("secret hard abort")

    with pytest.raises(ReportExportError) as host_error:
        _host(_Bundle(Failing())).export_application(
            ReportExporterCall(_sources()[:1], "markdown", _Probe())
        )
    assert str(host_error.value) == "report_exporter_failed"

    class ForgedHost:
        def export_application(self, _call):
            raise _HardAbort("secret forged host abort")

    with pytest.raises(ReportExportError) as service_error:
        export_completed_reports(
            _sources()[:1], host=ForgedHost(), connection_probe=_Probe()
        )
    assert str(service_error.value) == "report_exporter_failed"


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit])
def test_system_control_exceptions_propagate_when_connection_is_clear(signal):
    class Stopping(_Provider):
        def export(self, _context):
            raise signal()

    with pytest.raises(signal):
        _host(_Bundle(Stopping())).export_application(
            ReportExporterCall(_sources()[:1], "markdown", _Probe())
        )


def test_provider_cannot_mutate_frozen_view_to_rewrite_markdown():
    class Mutating(_Provider):
        def export(self, context):
            object.__setattr__(context.reports[0], "content_md", "REWRITTEN")
            return ProviderResult(
                (ReportExportedItem(context.reports[0].ref, "REWRITTEN"),),
                ExtensionResultStatus.AVAILABLE,
            )

    with pytest.raises(ReportExportError, match="invalid_report_export_result"):
        _host(_Bundle(Mutating())).export_application(
            ReportExporterCall(_sources()[:1], "markdown", _Probe())
        )


def test_connection_must_be_clear_before_and_after_provider():
    probe = _Probe()
    probe.held = True
    provider = _Provider()
    with pytest.raises(ReportExportBoundaryError, match="connection_held"):
        _host(_Bundle(provider)).export_application(
            ReportExporterCall(_sources()[:1], "markdown", probe)
        )
    assert provider.calls == []

    class Leaking(_Provider):
        def export(self, context):
            probe.held = True
            return super().export(context)

    probe.held = False
    with pytest.raises(ReportExportBoundaryError, match="connection_held"):
        _host(_Bundle(Leaking())).export_application(
            ReportExporterCall(_sources()[:1], "markdown", probe)
        )


def test_service_rechecks_connection_after_a_forged_host():
    probe = _Probe()

    class ForgedHost:
        has_provider = True

        def export_application(self, call):
            probe.held = True
            from app.domain.report_export import ReportExportRendered

            return tuple(ReportExportRendered(item.content_md) for item in call.sources)

    with pytest.raises(ReportExportBoundaryError, match="connection_held"):
        export_completed_reports(
            _sources()[:1], host=ForgedHost(), connection_probe=probe
        )


def test_service_rechecks_connection_after_forged_host_baseexception():
    probe = _Probe()

    class ForgedHost:
        def export_application(self, _call):
            probe.held = True
            raise _HardAbort("secret forged host abort")

    with pytest.raises(ReportExportBoundaryError, match="connection_held") as caught:
        export_completed_reports(
            _sources()[:1], host=ForgedHost(), connection_probe=probe
        )
    assert "secret" not in str(caught.value)


def test_service_rejects_forged_host_markdown_rewrite_and_source_mutation():
    from app.domain.report_export import ReportExportRendered

    sources = _sources()[:1]

    class ForgedHost:
        def export_application(self, call):
            object.__setattr__(call.sources[0], "question", "forged-name")
            object.__setattr__(call.sources[0], "content_md", "FORGED")
            return (ReportExportRendered("FORGED"),)

    with pytest.raises(ReportExportError, match="invalid_report_export_result"):
        export_completed_reports(
            sources, host=ForgedHost(), connection_probe=_Probe()
        )


def test_batch_route_is_the_only_backend_composition_and_stores_do_not_format():
    from app.api import report_routes
    from app.repositories.postgres import report_store as postgres_store
    from app.repositories.sqlite import report_store as sqlite_store

    endpoint = inspect.getsource(report_routes.export_reports_endpoint)
    assert endpoint.count("export_completed_reports(") == 1
    assert "zipfile.ZipFile" in endpoint
    for module in (sqlite_store, postgres_store):
        source = inspect.getsource(module.ReportStore.export_reports)
        assert "ReportExportSource(" in source
        assert "re.sub" not in source
        assert '".md"' not in source
