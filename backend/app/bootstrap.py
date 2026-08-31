"""Application composition root joining core adapters and extension hosts."""
from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.extensions import ExtensionRuntime, default_extension_runtime
from app.domain.extension_http import PluginRouterSpec
from app.extensions.admin_projection import (
    LoadedExtensionProjection,
    project_loaded_extensions,
)
from app.extensions.http_router import collect_plugin_router_specs
from app.extensions.ui_projection import (
    UiContributionProjection,
    project_ui_contributions,
)
from app.repositories.factory import create_repository
from app.repositories.ports import NotebookRepository
from app.services.extension_toggles import refresh_extension_admission


def application_extension_runtime() -> ExtensionRuntime:
    return default_extension_runtime()


def application_extension_ui_projection(
    runtime: ExtensionRuntime,
) -> Callable[[object | None], tuple[UiContributionProjection, ...]]:
    """Bind the frozen registry behind the API's narrow sanitized projection seam."""

    return lambda context: project_ui_contributions(runtime.registry, context)


def application_extension_admin_projection(
    runtime: ExtensionRuntime,
) -> Callable[[], tuple[LoadedExtensionProjection, ...]]:
    """Bind the frozen registry behind the admin-only topology projection seam.

    Unlike the UI projection above, this has no per-request context: the
    projection is a pure function of the startup-frozen registry, so the
    returned callable takes no arguments.
    """

    return lambda: project_loaded_extensions(runtime.registry)


def application_plugin_router_specs(
    runtime: ExtensionRuntime,
) -> tuple[PluginRouterSpec, ...]:
    """Freeze the deployment plugins' HTTP router contributions for mounting.

    Unlike the two projections above this returns the value itself rather than
    a callable: route topology must be decided while the application object is
    being built, never lazily on a request. ``app.api.extension_routes`` is the
    only consumer, and it cannot import ``app.extensions`` — this composition
    root is the seam that joins them.
    """

    return collect_plugin_router_specs(runtime.registry, runtime.plugin_settings)


def application_repository_hosts(
    runtime: ExtensionRuntime,
) -> dict[str, object]:
    """The extension host seats every application repository is composed with.

    Extracted so a process that must build its repository itself — the offline
    scale-build CLI needs the PostgreSQL adapter's schema-ownership seam, which
    the backend-neutral ``create_repository`` selector deliberately does not
    expose — still gets the SAME seats as the server. A host list that drifts
    between the server and an offline builder is exactly how an artifact gets
    built by a different pipeline than the one serving it.
    """

    return {
        "retrieval_contributor_host": runtime.retrieval_contributors,
        "parser_provider_chain_host": runtime.parser_chain,
        "ask_completed_observer_host": runtime.ask_completed_observers,
        "report_completed_observer_host": runtime.report_completed_observers,
        "ask_engine_host": runtime.ask_engines,
        "indexing_pipeline_host": runtime.indexing_pipelines,
        "gap_consult_host": runtime.gap_consult,
    }


def create_application_repository(settings: Settings) -> NotebookRepository:
    runtime = application_extension_runtime()
    repository = create_repository(
        settings, **application_repository_hosts(runtime)  # type: ignore[arg-type]
    )
    prime_extension_admission(repository)
    return repository


def prime_extension_admission(repository: NotebookRepository) -> None:
    # Prime the admission snapshot the extension registry reads. This is the
    # one place it can happen: the registry is frozen and the repository handed
    # in is fully composed, so the toggle table exists by now and this read is
    # safe — and every process that composes an application repository passes
    # through here, so servers, maintenance CLIs, batch jobs and the offline
    # scale-build CLI all start with the admin's switches already in effect
    # rather than with the empty default. (A process that did not run the
    # migrations itself reads the schema the running service already applied;
    # the CLI verifies that ledger before it ever composes.)
    #
    # The failure is NOT softened: a repository that cannot answer which
    # plugins an admin disabled has no business being handed to a caller that
    # is about to route requests through them, so the exception propagates and
    # composition fails. But the half-built repository must not be abandoned on
    # the way out — it already owns a connection pool (PostgreSQL) or open
    # handles (SQLite), and nothing else holds a reference to close them once
    # this frame unwinds. Close, then re-raise the original.
    try:
        refresh_extension_admission(
            repository._runtime.extension_toggles  # type: ignore[attr-defined]
        )
    except BaseException:
        try:
            repository.close()
        except Exception:  # never replace the prime's diagnostic
            pass
        raise
