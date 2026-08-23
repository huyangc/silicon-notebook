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


def create_application_repository(settings: Settings) -> NotebookRepository:
    runtime = application_extension_runtime()
    return create_repository(
        settings,
        retrieval_contributor_host=runtime.retrieval_contributors,
        parser_provider_chain_host=runtime.parser_chain,
        ask_completed_observer_host=runtime.ask_completed_observers,
        report_completed_observer_host=runtime.report_completed_observers,
    )
