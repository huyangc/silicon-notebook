"""Application composition root joining core adapters and extension hosts."""
from __future__ import annotations

from collections.abc import Callable

from app.core.config import Settings
from app.extensions import ExtensionRuntime, default_extension_runtime
from app.domain.extension_http import PluginRouterSpec
from app.domain.auth_provider import AuthProviderHostPort
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


def application_auth_provider() -> AuthProviderHostPort:
    return application_extension_runtime().auth_provider


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
        "element_enricher_host": runtime.element_enrichers,
        "reflect_action_host": runtime.reflect_actions,
    }


def create_application_repository(settings: Settings) -> NotebookRepository:
    runtime = application_extension_runtime()
    repository = create_repository(
        settings, **application_repository_hosts(runtime)  # type: ignore[arg-type]
    )
    prime_extension_admission(repository)
    try:
        from app.services.auth_flow import AuthFlowService

        # Attach the provider to the identity store once, here: unified auth
        # follows its switch for session resolution and local-credential
        # refusals. Then refuse to start with an enabled, unusable provider.
        auth_store = repository._runtime.identity.auth
        auth_store.use_provider(runtime.auth_provider)
        flow = AuthFlowService(auth_store, runtime.auth_provider, settings)
        if flow.validate_configuration() is not None:
            refuse_unified_auth_lockout(auth_store)
    except BaseException:
        repository.close()
        raise
    return repository


NO_SSO_ADMIN_AT_STARTUP = (
    "不能以启用统一认证的状态启动：站内还没有内置管理员以外的在用管理员。内置管理员在"
    "统一认证下无法登录，请先停用统一认证插件、把一个能经统一认证进入的账号（用户名就是"
    "其工号）设为管理员，再启用。"
)


def refuse_unified_auth_lockout(auth_store) -> None:
    """Startup counterpart of the admin switch's lockout pre-check: an enabled
    provider with no active administrator besides the built-in one (who cannot
    sign in through unified authentication) would leave nobody able to
    administer the site, so composition is refused rather than started."""
    from app.domain.auth_provider import AuthProviderError

    if auth_store.has_sso_admin():
        return
    error = AuthProviderError("no_sso_admin")
    error.add_note(NO_SSO_ADMIN_AT_STARTUP)
    raise error


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
