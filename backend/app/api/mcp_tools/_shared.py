"""Scoped Streamable HTTP MCP adapter for notebook-bound Agent Memory.

The adapter deliberately contains no product SQL.  It authenticates an Agent
token, keeps only the selected notebook on the MCP session object, and calls
the already-composed Memory/retrieval/Ask services.  Every data tool rechecks
the live token row, scope, allowlist, and notebook membership.
"""
from __future__ import annotations

import contextvars
import json
import logging
import math
import time
from contextlib import contextmanager
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import anyio
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp import Context
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.request_context import reset_request_user, set_request_user
from app.models.identity import AgentPrincipal, UserProfile
from app.services.reasoning_retrieval import profile_wiring_active


logger = logging.getLogger(__name__)


RESULT_LIMIT = 20
TEXT_LIMIT = 2_000
TOTAL_TEXT_LIMIT = 12_000
OUTPUT_SCALAR_LIMIT = 6_000
OUTPUT_KEY_LIMIT = 120
OUTPUT_MAPPING_LIMIT = 20
OUTPUT_DEPTH_LIMIT = 5
OUTPUT_INTEGER_LIMIT = 9_999_999_999_999_999
# Heartbeat interval for the MCP progress notifications emitted while a tool's
# blocking body runs. See `_run_with_progress` for why they exist at all; the
# value only has to be comfortably under the SHORTEST idle timeout any client
# applies, and every beat is one small SSE frame, so it is cheap to be
# generous. It is not a timeout of ours: nothing here gives up on the work.
PROGRESS_HEARTBEAT_SECONDS = 5.0
_SELECTED_ATTR = "_silicon_notebook_selected_notebook"
_MCP_PRINCIPAL: contextvars.ContextVar[AgentPrincipal | None] = (
    contextvars.ContextVar("mcp_agent_principal", default=None)
)


def _is_loopback(host: str) -> bool:
    return host.lower().strip("[]") in {"127.0.0.1", "localhost", "::1"}


def validate_mcp_deployment(
    bind_host: str, public_url: str, *, require_https: bool = True
) -> None:
    """Guard a remotely reachable MCP endpoint against silent posture loss.

    Fail closed by default. When ``require_https`` is False (the product's
    intranet default, wired in ``app.main.create_app``) keep serving but log a
    prominent warning naming every protection relaxed for remote clients:
    ``create_memory_mcp`` disables Host/Origin (DNS-rebinding) validation, and
    over plain HTTP the Agent Bearer token additionally crosses the network in
    cleartext. Only safe on a trusted private network.
    """
    # MCP_PUBLIC_URL used to be only transport metadata. It is now also shown
    # verbatim in the anonymous Agent-onboarding Markdown, so it is an
    # instruction-plane trust boundary: fail before startup rather than letting
    # credentials or Markdown control text become public Agent instructions.
    if (
        not public_url
        or public_url != public_url.strip()
        or any(
            char.isspace() or ord(char) < 32 or ord(char) == 127
            for char in public_url
        )
        or "`" in public_url
    ):
        raise RuntimeError("invalid MCP_PUBLIC_URL")
    try:
        parsed = urlparse(public_url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("invalid MCP_PUBLIC_URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/mcp"
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise RuntimeError("invalid MCP_PUBLIC_URL")
    public_host = parsed.hostname or ""
    remotely_reachable = not _is_loopback(bind_host) or (
        public_host and not _is_loopback(public_host)
    )
    if not remotely_reachable:
        return
    is_plain_http = parsed.scheme.lower() != "https"
    if require_https:
        if is_plain_http:
            raise RuntimeError("remote MCP deployment requires HTTPS")
        return
    # require_https is False: create_memory_mcp disables Host/Origin
    # (DNS-rebinding) validation for these remote clients. Warn either way, and
    # name the extra cleartext-token exposure when the transport is plain HTTP.
    if is_plain_http:
        logger.warning(
            "MCP is serving remote clients over plain HTTP with Host/Origin "
            "(DNS-rebinding) validation disabled (bind_host=%s public_url=%s): "
            "the Agent Bearer token crosses the network in cleartext. Only do "
            "this on a trusted private network; set MCP_REQUIRE_HTTPS=1 to "
            "enforce HTTPS and restore Host/Origin validation.",
            bind_host,
            public_url,
        )
    else:
        logger.warning(
            "MCP has Host/Origin (DNS-rebinding) validation disabled for remote "
            "clients (bind_host=%s public_url=%s) because MCP_REQUIRE_HTTPS is "
            "not set. Transport is HTTPS; set MCP_REQUIRE_HTTPS=1 to also "
            "restore Host/Origin validation.",
            bind_host,
            public_url,
        )


def _serialized_size(value: Any) -> int:
    # Use the roomier standard separators so the bound also covers callers that
    # do not use a compact JSON encoder. Sorting makes the calculation stable.
    return len(json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=str
    ).encode("utf-8"))


def _serialized_chars(value: Any) -> int:
    return len(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    ))


def _mark_truncated(
    stats: dict[str, Any], *, items: int = 0, map_entries: int = 0,
    characters: int = 0, fields: int = 0,
) -> None:
    stats["truncated"] = True
    stats["omitted_items"] += max(0, int(items))
    stats["omitted_map_entries"] += max(0, int(map_entries))
    stats["omitted_characters"] += max(0, int(characters))
    stats["omitted_fields"] += max(0, int(fields))


def _sanitize_output(
    value: Any,
    stats: dict[str, Any],
    depth: int = 0,
    *,
    field: str = "",
    field_limits: Mapping[str, int] | None = None,
) -> Any:
    """Deterministically bound arbitrary adapter data before final packing."""
    if isinstance(value, str):
        limit = max(1, int((field_limits or {}).get(field, OUTPUT_SCALAR_LIMIT)))
        if len(value) <= limit:
            return value
        _mark_truncated(stats, characters=len(value) - limit + 1)
        return value[: limit - 1] + "…"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) <= OUTPUT_INTEGER_LIMIT:
            return value
        _mark_truncated(stats, characters=1)
        return OUTPUT_INTEGER_LIMIT if value > 0 else -OUTPUT_INTEGER_LIMIT
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        _mark_truncated(stats, fields=1)
        return None
    if depth >= OUTPUT_DEPTH_LIMIT:
        _mark_truncated(stats, fields=1)
        return "…"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        priorities = {
            "counts": (
                "sources", "memories", "rules", "cases", "checklist_items",
                "methods", "risks", "glossary",
            ),
            "kg_status": (
                "dirty", "last_rebuild_at", "objects", "relations", "clusters",
                "viz_building",
            ),
            # get_build_status's scale_index sub-object: the QUEUED shape
            # alone (state + the base 8 + 5 queue fields + a conditional
            # last_built_at/last_build_ms pair) plus the shared setdefault
            # tail (stale/n_nodes/n_chunks/n_ann/n_chunk_ann/has_chunk_ann)
            # totals 22 keys -- over OUTPUT_MAPPING_LIMIT (20). Alphabetical
            # fallback ordering silently dropped `total_chunks` and
            # `unindexed_sources`, the two counters an Agent polling a queued
            # build actually needs, while marking a 719-byte response
            # `truncated: true`. Front-load the progress-relevant keys so any
            # overflow eats the low-value tail (n_* internals, `stale`,
            # `offpeak_in_window`) instead.
            "scale_index": (
                "state", "exists", "building", "queue_position",
                "queue_length", "queued_at", "offpeak_next_start_at",
                "total_chunks", "unindexed_sources", "delta_chunks",
                "last_built_at", "last_build_ms",
            ),
        }.get(field, ())
        priority_index = {key: index for index, key in enumerate(priorities)}
        entries = sorted(
            value.items(),
            key=lambda item: (
                0 if str(item[0]) in priority_index else 1,
                priority_index.get(str(item[0]), 0),
                str(item[0]),
            ),
        )
        if len(entries) > OUTPUT_MAPPING_LIMIT:
            _mark_truncated(stats, map_entries=len(entries) - OUTPUT_MAPPING_LIMIT)
        for raw_key, child in entries[:OUTPUT_MAPPING_LIMIT]:
            key = str(raw_key)
            if len(key) > OUTPUT_KEY_LIMIT:
                _mark_truncated(stats, characters=len(key) - OUTPUT_KEY_LIMIT + 1)
                key = key[: OUTPUT_KEY_LIMIT - 1] + "…"
            # Avoid a clipped-key collision silently replacing earlier data.
            if key in result:
                _mark_truncated(stats, map_entries=1)
                continue
            result[key] = _sanitize_output(
                child,
                stats,
                depth + 1,
                field=key,
                field_limits=field_limits,
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        entries = list(value)
        if len(entries) > RESULT_LIMIT:
            _mark_truncated(stats, items=len(entries) - RESULT_LIMIT)
        return [
            _sanitize_output(
                child,
                stats,
                depth + 1,
                field=field,
                field_limits=field_limits,
            )
            for child in entries[:RESULT_LIMIT]
        ]
    text = str(value)
    return _sanitize_output(text, stats, depth)


def _iter_output_strings(value: Any, *, parent_key: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "truncation":
                continue
            if isinstance(child, str):
                yield value, key, child, str(key)
            else:
                yield from _iter_output_strings(child, parent_key=str(key))
    elif isinstance(value, list):
        for child in value:
            yield from _iter_output_strings(child, parent_key=parent_key)


def _shrink_longest_string(
    value: dict[str, Any], stats: dict[str, Any], *, identifiers: bool = False
) -> bool:
    candidates = list(_iter_output_strings(value))
    if not candidates:
        return False
    identifier_fields = {
        "notebook_id", "selected_notebook_id", "memory_id", "answer_id",
        "object_id", "source_id", "element_id", "key",
    }
    # Preserve ordinary identifiers exactly. They become shrinkable only if a
    # compromised downstream producer supplied an identifier large enough to
    # make the response otherwise impossible to serialize within the budget.
    pool = [
        item for item in candidates
        if (
            (item[3] in identifier_fields and len(item[2]) > 8)
            if identifiers
            else (item[3] not in identifier_fields and len(item[2]) > 32)
        )
    ]
    if not pool:
        return False
    parent, key, current, _field = max(pool, key=lambda item: len(item[2]))
    minimum = 8 if identifiers else 32
    if len(current) <= minimum:
        return False
    keep = max(minimum, len(current) // 2)
    parent[key] = current[: max(1, keep - 1)] + "…"
    _mark_truncated(stats, characters=len(current) - len(parent[key]))
    return True


def _iter_reducible_maps(value: Any, *, field: str = ""):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "truncation":
                continue
            if isinstance(child, dict):
                if key in {"counts", "kg_status", "provenance"} and child:
                    yield child
                yield from _iter_reducible_maps(child, field=str(key))
            elif isinstance(child, list):
                yield from _iter_reducible_maps(child, field=str(key))
    elif isinstance(value, list):
        for child in value:
            yield from _iter_reducible_maps(child, field=field)


def _drop_map_entry(value: dict[str, Any], stats: dict[str, Any]) -> bool:
    candidates = list(_iter_reducible_maps(value))
    if not candidates:
        return False
    target = max(candidates, key=len)
    target.pop(next(reversed(target)))
    _mark_truncated(stats, map_entries=1)
    return True


def _iter_reducible_lists(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "truncation":
                continue
            if isinstance(child, list):
                if len(child) > 1:
                    yield child
                yield from _iter_reducible_lists(child)
            elif isinstance(child, dict):
                yield from _iter_reducible_lists(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_reducible_lists(child)


def _drop_list_item(value: dict[str, Any], stats: dict[str, Any]) -> bool:
    candidates = list(_iter_reducible_lists(value))
    if not candidates:
        return False
    max(candidates, key=len).pop()
    _mark_truncated(stats, items=1)
    return True


def _fit_value_to_chars(
    value: Any, *, field: str, char_budget: int, stats: dict[str, Any]
) -> Any:
    """Fit one already-sanitized field without copying private data to metadata."""
    wrapper = {field: value}
    while _serialized_chars(wrapper[field]) > char_budget:
        if _shrink_longest_string(wrapper, stats):
            continue
        if _drop_map_entry(wrapper, stats):
            continue
        if _drop_list_item(wrapper, stats):
            continue
        if _shrink_longest_string(wrapper, stats, identifiers=True):
            continue
        raise ValueError(f"MCP {field} metadata exceeds sub-budget")
    return wrapper[field]


def _field_parents(value: Any, field: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if field in value:
            result.append(value)
        for key, child in value.items():
            if key != "truncation":
                result.extend(_field_parents(child, field))
    elif isinstance(value, list):
        for child in value:
            result.extend(_field_parents(child, field))
    return result


def _fit_aggregate_field_to_chars(
    value: dict[str, Any], *, field: str, char_budget: int,
    stats: dict[str, Any],
) -> None:
    parents = _field_parents(value, field)
    while sum(_serialized_chars(parent[field]) for parent in parents) > char_budget:
        parent = max(parents, key=lambda item: _serialized_chars(item[field]))
        current_size = _serialized_chars(parent[field])
        total_size = sum(_serialized_chars(item[field]) for item in parents)
        target = max(2, current_size - (total_size - char_budget))
        fitted = _fit_value_to_chars(
            parent[field], field=field, char_budget=target, stats=stats
        )
        if _serialized_chars(fitted) >= current_size:
            if fitted == {}:
                raise ValueError(f"MCP aggregate {field} exceeds sub-budget")
            fitted = {}
            _mark_truncated(stats, fields=1)
        parent[field] = fitted


def _budget_response(
    value: Mapping[str, Any], *, initial_omitted_items: int = 0,
    field_limits: Mapping[str, int] | None = None,
    provenance_budget_chars: int | None = None,
    tags_budget_chars: int | None = None,
    anchors_budget_chars: int | None = None,
    anchor_provenance_budget_chars: int | None = None,
    citations_budget_chars: int | None = None,
) -> dict[str, Any]:
    """Return a useful response that strictly fits the public MCP JSON budget."""
    stats: dict[str, Any] = {
        "budget_chars": TOTAL_TEXT_LIMIT,
        "truncated": False,
        "omitted_items": 0,
        "omitted_map_entries": 0,
        "omitted_characters": 0,
        "omitted_fields": 0,
    }
    if initial_omitted_items:
        _mark_truncated(stats, items=initial_omitted_items)
    result = _sanitize_output(dict(value), stats, field_limits=field_limits)
    anchors = result.get("anchors")
    if anchor_provenance_budget_chars is not None and isinstance(anchors, list):
        for anchor in anchors:
            if isinstance(anchor, dict) and "provenance" in anchor:
                anchor["provenance"] = _fit_value_to_chars(
                    anchor["provenance"],
                    field="provenance",
                    char_budget=anchor_provenance_budget_chars,
                    stats=stats,
                )
    if provenance_budget_chars is not None:
        _fit_aggregate_field_to_chars(
            result,
            field="provenance",
            char_budget=provenance_budget_chars,
            stats=stats,
        )
    if tags_budget_chars is not None and "tags" in result:
        result["tags"] = _fit_value_to_chars(
            result["tags"], field="tags", char_budget=tags_budget_chars, stats=stats
        )
    if anchors_budget_chars is not None and isinstance(anchors, list):
        result["anchors"] = _fit_value_to_chars(
            anchors,
            field="anchors",
            char_budget=anchors_budget_chars,
            stats=stats,
        )
    citations = result.get("citations")
    if citations_budget_chars is not None and isinstance(citations, list):
        # Pre-fit citations to its own budget, mirroring anchors above, so the
        # shared convergence loop below never needs to shrink the answer text
        # just to make room for an unbounded number of chunk-mode citations.
        result["citations"] = _fit_value_to_chars(
            citations,
            field="citations",
            char_budget=citations_budget_chars,
            stats=stats,
        )
    result["truncation"] = stats
    while _serialized_size(result) > TOTAL_TEXT_LIMIT:
        # Prefer retaining the first useful record: shrink evidence text/maps,
        # then extra records. Identifiers are the last scalar class reduced.
        if _shrink_longest_string(result, stats):
            continue
        if _drop_map_entry(result, stats):
            continue
        if _drop_list_item(result, stats):
            continue
        if _shrink_longest_string(result, stats, identifiers=True):
            continue
        raise ValueError("MCP response metadata exceeds output budget")
    return result


def _principal() -> AgentPrincipal:
    principal = _MCP_PRINCIPAL.get()
    if principal is None:
        raise PermissionError("Agent authentication required")
    return principal


def _live_principal(repo: Any) -> AgentPrincipal:
    # The ContextVar is inherited when the stateful SDK session task starts;
    # use it only for the bound token id, never as an authorization snapshot.
    principal = repo.refresh_agent_principal(_principal().token_id)
    if principal is None:
        raise PermissionError("Agent token or profile is no longer active")
    return principal


@contextmanager
def _owner_request_context(principal: AgentPrincipal):
    # Repository components that retain the legacy current-user boundary need
    # only a request identity.  MCP authentication has already established the
    # stable owner id; no browser session is synthesized.
    marker = set_request_user(
        UserProfile(
            id=principal.owner_id,
            email="",
            display_name=principal.profile_name,
            role="user",
        )
    )
    try:
        yield
    finally:
        reset_request_user(marker)


async def _run_with_progress(
    ctx: Context, work: Callable[[], Any], *, label: str
) -> Any:
    """Run one tool's blocking body in a worker thread, heart-beating meanwhile.

    MCP clients do not wait indefinitely for a tool. Claude Code applies an
    *idle* timeout -- it aborts a call that has produced neither a response nor
    a progress notification for N seconds -- and other clients apply a flat
    per-call ceiling. `ask_notebook` in `reasoning` mode routinely runs for
    minutes (plan, federated retrieval, reflect loop, synthesis), so without a
    heartbeat the client gives up on a call the server is still successfully
    executing, and the Agent sees a transport error where the answer was about
    to arrive. Trigger `build_kg`, and a whole notebook's extraction was
    already under way when the client walked out.

    Two properties make this free where it is not needed:

    * `Context.report_progress` is a no-op unless the client asked for progress
      by putting a `progressToken` in the request's `_meta`. A client that does
      not want notifications is charged nothing but this task group.
    * The first beat is one whole interval away, so a tool that answers in
      milliseconds -- which is nearly all of them -- never sends one.

    The heartbeat NEVER fails the call: if the notification cannot be written
    (the client hung up, the stream is closed) beating stops and the work runs
    to completion, because the work is what the caller asked for and it is
    already in a thread that cannot be cancelled anyway.

    ⚠ The task group must not be allowed to raise the work's exception itself.
    anyio 4 wraps a body exception in an `ExceptionGroup`, and FastMCP turns
    whatever comes out of a tool into the error text the Agent reads -- so a
    plain `raise ValueError("question too long: ...")` would reach the caller
    as `unhandled errors in a TaskGroup`. Capturing the exception in the child
    task and re-raising it here keeps the exception identity and message
    exactly what the tool wrote.
    """
    result: list[Any] = []
    failure: list[Exception] = []

    async def heartbeat() -> None:
        started = time.monotonic()
        while True:
            await anyio.sleep(PROGRESS_HEARTBEAT_SECONDS)
            elapsed = time.monotonic() - started
            try:
                # Deliberately only the tool name and a wall-clock count: this
                # string is written to a client we do not control, so it may
                # not carry the question, a notebook or source name, or any
                # other notebook content -- the same rule the observability
                # events follow.
                await ctx.report_progress(
                    elapsed, None, f"{label}: {elapsed:.0f}s elapsed"
                )
            except Exception:  # pragma: no cover - client-side stream failure
                logger.debug("progress heartbeat stopped for %s", label)
                return

    async def runner() -> None:
        try:
            result.append(await anyio.to_thread.run_sync(work))
        except Exception as exc:
            failure.append(exc)
        finally:
            tg.cancel_scope.cancel()

    async with anyio.create_task_group() as tg:
        tg.start_soon(heartbeat)
        tg.start_soon(runner)

    if failure:
        raise failure[0]
    return result[0]


class AgentBearerMiddleware:
    """Authenticate opaque Agent Bearer tokens without retaining raw values."""

    def __init__(
        self, app, repository_provider: Callable[[], Any], *,
        require_https: bool = True,
    ) -> None:
        self.app = app
        self.repository_provider = repository_provider
        self.require_https = require_https

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        client_host = str(client[0]) if client else ""
        if (
            self.require_https
            and str(scope.get("scheme", "http")).lower() != "https"
            and not _is_loopback(client_host)
        ):
            await JSONResponse(
                {"detail": "remote MCP transport requires HTTPS"}, status_code=403
            )(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        authorization = headers.get("authorization", "")
        raw_token = (
            authorization[7:].strip()
            if authorization.lower().startswith("bearer ")
            else ""
        )
        service = self.repository_provider()
        principal = await anyio.to_thread.run_sync(
            service.resolve_agent_token, raw_token
        )
        if principal is None:
            await JSONResponse(
                {"detail": "invalid or expired Agent token"}, status_code=401
            )(scope, receive, send)
            return

        # Let the SDK bind a stateful MCP session to the authenticated
        # credential.  Store only the non-secret token id in the ASGI user.
        authenticated_scope = dict(scope)
        authenticated_scope["user"] = AuthenticatedUser(
            AccessToken(
                token=principal.token_id,
                # Stateful owner comparison ignores AccessToken.token. Bind
                # the session with a field the SDK actually compares.
                client_id=principal.token_id,
                subject=principal.owner_id,
                scopes=list(principal.scopes),
            )
        )
        marker = _MCP_PRINCIPAL.set(principal)
        try:
            await self.app(authenticated_scope, receive, send)
        finally:
            _MCP_PRINCIPAL.reset(marker)


def _record_agent_call(
    repo: Any, principal: AgentPrincipal, notebook_id: str, scope: str
) -> None:
    """Book one admitted tool call into this member's own call ledger.

    Placed at the ONE choke point every notebook-scoped tool already passes
    through, and keyed on the capability ``scope`` that choke point already
    receives — so a tool added tomorrow is recorded without its author doing
    anything, and cannot silently opt out by forgetting an argument. The
    registered cost of keying on the scope rather than the tool name: two
    tools admitted under the same scope read back identically (see
    ``AgentObservationStorePort.append_call``).

    Four properties, each deliberate:

    * **After EVERY authorization gate, never before.** A call that was
      rejected did not reach this notebook, and recording it would turn the
      ledger into a log of attempts by principals who were denied — a
      different feature, with different privacy consequences, that nobody
      asked for. ⚠ ``require_agent_access`` is not always the LAST gate:
      ``_writable_notebook`` adds an owner-only check after it, so that
      helper suppresses recording here (``record=False``) and calls this
      itself once its own gate has cleared. Recording unconditionally inside
      ``_selected_notebook`` would book every write a read-only member
      attempted (codex #616 R1 P2).
    * **Never fatal, and never chatty.** Bookkeeping about a call is not part
      of the call: a failing ledger write (disk full, a migration mid-flight)
      must not turn a tool call that already passed authorization into an
      error the Agent sees. The failure is logged as a **content-free**
      event — no ``exc_info``, no exception text — because a database
      exception carries file paths and SQL fragments that the repository's
      logging rule keeps out of logs (``AGENTS.md``: "keep … exception text
      out of logs"; codex #616 R1 P2).
    * **Off means zero writes.** Both gates are checked before anything else,
      so a deployment that turns either off pays nothing — not a
      transaction, not a row.
    * **Compensates the member-removal race.** Authorization and this write
      are two steps; a member removed in between can have their rows cleared
      by the removal path first, leaving this row an orphan that resurfaces
      if they rejoin — against the "cleared on member removal" lifecycle the
      ledger documents. A post-write NOTEBOOK-read recheck (not the full
      ``require_agent_access``, for the reason ``add_observation``'s twin
      block spells out) clears the member's scope when access is genuinely
      gone, and fails open otherwise (codex #616 R2 P2). Unlike
      ``add_observation`` this never raises: the tool call itself was
      legitimate, and book-keeping must not turn it into an error.
    * **Layered under the feature's own master gate.** Recording requires
      ``profile_wiring_active`` as well as ``agent_call_log_enabled``. The
      two are NOT independent, and the reason is the entry point: with
      ``AGENT_PROFILE_ENABLED`` off the panel's entry button does not render
      at all, so rows written then are rows no member can ever open — the
      exact "accumulating records nobody can inspect" state codex #616 R1 P1
      flagged. Reading and clearing an EXISTING ledger stay ungated (see
      ``agent_profile_routes``): a switch flipped today must never make
      yesterday's rows unreachable.
    """
    settings = get_settings()
    if not getattr(settings, "agent_call_log_enabled", True):
        return
    if not profile_wiring_active(settings, getattr(repo, "agent_profile", None)):
        return
    try:
        repo.agent_observations.append_call(
            notebook_id,
            principal.owner_id,
            principal.profile_id,
            capability=scope,
        )
    except Exception:  # noqa: BLE001 — see "Never fatal, and never chatty".
        logger.warning("agent call ledger write failed")
        return
    # 成员移除竞态的补偿,与 ``add_observation`` 逐条同款(codex #616 R2 P2):
    # 鉴权与这次写入是两步,中间被移出共享笔记本的成员,其清理可能先跑——我们
    # 这一行就成了孤儿,等他重新加入时又冒出来,与「移除即清空」的生命周期契约
    # 相反。复查只看**笔记本读权**,不复查整个 ``require_agent_access``:令牌级
    # 的失效(吊销/过期/scope 变更)不会让这一行变得不该存在(owner 仍是成员,
    # 这仍是他自己的记录),为它补偿反而会误删该成员在这个库里的全部记录。
    # 复查本身**出错则保留**(fail-open):它不是「访问已被撤销」的证据。
    try:
        still_member = repo.user_can_read_notebook(notebook_id, principal.owner_id)
    except Exception:  # noqa: BLE001 — fail-open,同上
        still_member = True
    if not still_member:
        try:
            repo.agent_observations.clear_observations(
                notebook_id, principal.owner_id
            )
        except Exception:  # noqa: BLE001 — 补偿同样不该炸掉这次工具调用
            logger.warning("agent call ledger compensation failed")


def _selected_notebook(
    ctx: Context, repo: Any, scope: str, record: bool = True
) -> tuple[AgentPrincipal, str]:
    principal = _live_principal(repo)
    notebook_id = getattr(ctx.session, _SELECTED_ATTR, "")
    if not notebook_id:
        raise ValueError("select_notebook must be called before this tool")
    # Re-reads token state, current scopes/allowlist, and notebook membership.
    repo.require_agent_access(
        principal, scope, notebook_id
    )
    # ``record=False`` is passed by exactly one caller — ``_writable_notebook``,
    # which still has a gate to apply. See ``_record_agent_call``'s first
    # property for why the ledger must not run ahead of it.
    if record:
        _record_agent_call(repo, principal, notebook_id, scope)
    return principal, notebook_id
def _writable_notebook(
    ctx: Context, repo: Any, scope: str, record: bool = True
) -> tuple[AgentPrincipal, str]:
    """``_selected_notebook`` plus the OWNER-ONLY write gate.

    This is not belt-and-braces. ``require_agent_access`` clears on
    ``user_can_read_notebook`` — owner ∪ read-only member ∪ an effective grant
    edge (user/group/group_admins/everyone, since P1 group sharing) — because
    every tool that existed before the first caller of this helper only read.
    A token's allowlist can perfectly well name a notebook its owner merely
    joined as a read-only member or reached through a group grant, and that
    user cannot write to it through the browser
    (add/delete a source, or start a background knowledge-graph/index build).
    Landing an Agent write there would hand the sharing boundary's read side a
    write it was explicitly denied, through a door the notebook's real owner
    never opened. Shared by both the source-management and the build/
    maintenance tools below — every write operation on this surface, not just
    document uploads.

    ``user_can_access_notebook`` is the product's owner-only write predicate
    (its own docstring: 「写权:仅 owner(安全边界,勿放宽)」).

    ⚠ It is NO LONGER the same predicate the HTTP write surface resolves to.
    P2-T2 flipped six browser-facing capabilities (sources/kg/knowhow/knowledge/
    catalog writes and ``notebook:manage``) from owner-only to
    ``user_can_admin_notebook`` — owner ∪ an effective grant edge with
    ``role='admin'`` — so a group admin CAN now add/delete sources and start
    builds through the browser on a notebook shared into a group they
    administer. This surface deliberately did NOT follow: an Agent token's
    allowlist is a separate, longer-lived credential whose owner may have been
    granted admin on a notebook long after the token was issued, and the MCP
    write tools' blast radius (deleting documents out from under every member's
    retrieval) is the one this gate was created for. The divergence is a
    recorded decision (docs/product-and-api*.md「MCP 工具面与来源管理」), NOT drift;
    ``deps._CAPABILITY_LEVELS``'s own comment records the same split from the
    HTTP side. ``notebook:delete`` stayed owner-only on BOTH surfaces.

    There are now TWO Agent writes this gate deliberately does NOT cover:

    1. ``put_knowhow_cell_code`` (and its HTTP twins under
       ``/api/agent/knowhow/...``): design doc §⑥-4 keeps ``knowhow:code``
       entirely scope-driven — a cell code attachment is inert (never
       executed, indexed, embedded, or projected into retrieval/KG), so the
       blast-radius argument above does not carry over.

    2. ``add_observation`` (Agentic Memory P3, scope
       ``agent_observation:write``): also entirely scope-driven, via
       ``_selected_notebook`` directly rather than through this gate. Four
       reasons, not one:

       a. **Blast radius.** The owner-only line above exists because a write
          it guards reaches every member's retrieval (a document added or
          removed, a build kicked off). ``add_observation`` appends one row
          to ``agent_observations``, keyed by ``(notebook_id, owner_id,
          agent_profile_id)`` with ``owner_id`` always the CALLING member's
          own id (never taken from the request — see the tool's own
          implementation). Its blast radius is structurally capped at that
          one member's own row, not the notebook.

       b. **What it can become.** Those rows are read back by exactly one
          consumer: T4's overlay-consolidation job, and only into the SAME
          member's OWN overlay chain (``agent_notebook_profile`` rows with
          ``owner_id = <that member>``) — never the shared base chain a
          read-only member cannot otherwise touch. There is no path from an
          observation row to anything a different member's retrieval reads.

       c. **Design intent.** §5.1 gives every notebook member — not only the
          owner — their own overlay, precisely because a read-only member's
          Agent still uses this notebook and still accumulates its own usage
          impressions. Requiring notebook OWNERSHIP to write an observation
          would mean a read-only member's own Agent could never contribute to
          that member's own private overlay — the opposite of what §5.1
          describes.

       d. **Precedent.** Matches ``knowhow:code`` above: a second scope this
          gate does not cover is a recorded decision, not drift.
          Pinned on both sides by backend/tests/test_memory_mcp.py's
          ``test_add_observation_is_allowed_for_a_read_only_shared_notebook``
          (paired with ``test_source_writes_are_refused_in_a_read_only_
          shared_notebook``, the write this gate DOES refuse under the same
          share).
    """
    # ``record=False``: the call ledger only books calls that cleared EVERY
    # gate, and this helper's owner-only check below is the second one. A
    # read-only member reaching for a write tool passes ``require_agent_access``
    # and is refused here — recording before that would fill the ledger with
    # operations that never happened (codex #616 R1 P2).
    principal, notebook_id = _selected_notebook(ctx, repo, scope, record=False)
    if not repo.user_can_access_notebook(notebook_id, principal.owner_id):
        raise PermissionError(
            "this write operation requires owning the notebook; the token "
            "owner only has read access here"
        )
    # ``record=False`` 再往下传一层:``delete_source`` 在这道闸之后**还有**一道
    # 鉴权(只许删 Agent 自己添加的资料),由它自己在那道闸之后记(codex #616 R6 P2)。
    if record:
        _record_agent_call(repo, principal, notebook_id, scope)
    return principal, notebook_id
