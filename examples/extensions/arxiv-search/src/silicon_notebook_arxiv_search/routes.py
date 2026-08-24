"""The plugin's three HTTP routes, mounted under ``/api/extensions/{plugin_id}``.

Everything this module can reach is the :class:`PluginRouteContext` it is
handed: no repository, no global settings, no model client, no bearer token.
Three properties are load-bearing and each is pinned by a test:

**Every handler is a plain ``def``, never ``async def``.**  A sync handler runs
in FastAPI's threadpool, so it may call the blocking
``context.url_sources.import_urls`` directly — and so may it block on an arXiv
round trip.  An ``async def`` handler runs on the event loop thread, where both
of those would stall every other in-flight request in the process; core refuses
the first one at runtime rather than letting it happen quietly.

**The import allow-list runs before the port is touched.**  Core's own URL
import accepts any public URL, so restricting this route to ``arxiv.org`` is
not a privilege boundary — the port would authorize the caller either way.  It
is a *shape*: a plugin route that forwards arbitrary URLs to core's importer is
a general-purpose import proxy wearing an arXiv label, and a sample should
demonstrate the narrowest thing that does the job.

**No exception text ever leaves this module.**  Upstream failures become one
fixed Chinese sentence plus a stable event code.  The originating exception's
*class name* is logged — that is the whole of what a plugin may say about a
failure it did not write — and even that cannot ride along on the event:
``emit_event``'s whitelist accepts ``outcome`` only as a lowercase stable code,
so a class name would not merely be stripped, it would cause core to drop the
entire record.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends

from app.extension_sdk.http import PluginRouteContext

from . import client as arxiv_client
from .atom import ArxivPaper
from .settings import ArxivSearchSettings, egress_allowed, search_kwargs

LOGGER = logging.getLogger("silicon_notebook_arxiv_search")

# Plugin-private bounds; the package README is where they are registered for
# operators.  None of these is a core rail.
QUERY_MAX_CHARS = 200
MAX_IMPORT_URLS = 20
MAX_URL_CHARS = 2048
# arXiv's own API documents paging in the tens of thousands; this is a
# conservative order-of-magnitude ceiling on how far a caller may page rather
# than an attempt to mirror their exact limit.  Without it a caller could ask
# for an arbitrarily deep page — zero results, one throttle slot and one
# round trip spent proving that — with no cost to the caller and only cost to
# the shared politeness budget.
START_MAX = 10_000

# Hosts this route will hand to core's importer.  ``export.arxiv.org`` serves
# the API; PDFs live on ``arxiv.org`` itself, and the subdomain rule below is
# what keeps mirrors such as ``xxx.arxiv.org`` usable without opening the route
# to ``arxiv.org.example.com``.
_IMPORT_HOST_SUFFIX = ".arxiv.org"
_IMPORT_HOST = "arxiv.org"


def build_router(context: PluginRouteContext) -> APIRouter:
    """Build this plugin's router.  Called once, at startup, by core."""

    router = APIRouter()

    @router.get("/health")
    def health(_actor: Any = Depends(context.current_actor)) -> dict[str, object]:
        """Liveness for the plugin itself — deliberately zero remote calls.

        "Is arXiv reachable?" is a different question, and answering it here
        would give every dashboard poll a politeness slot and a round trip.
        """

        return {
            "plugin_id": context.plugin_id,
            "configured": _settings(context) is not None,
        }

    @router.get(
        "/notebooks/{notebook_id}/search",
        dependencies=[Depends(context.require_notebook_read)],
    )
    def search(notebook_id: str, q: str = "", start: int = 0) -> dict[str, object]:
        """One page of arXiv results for a notebook the caller may read.

        ``notebook_id`` is unused in the body on purpose: it is in the path so
        core's structural gate can see it, and the read gate above is what
        actually decides whether this caller gets an answer.
        """

        settings = _require_settings(context)
        query = (q or "").strip()
        if not query:
            raise context.user_error(400, "请输入检索关键词")
        if len(query) > QUERY_MAX_CHARS:
            raise context.user_error(400, "检索关键词过长，请精简后再试")
        if start < 0:
            raise context.user_error(400, "翻页位置不能为负数")
        if start > START_MAX:
            raise context.user_error(400, "翻页位置超出上限，请缩小检索范围后重试")

        # One extra record decides `has_more` without a second round trip.
        # Registered approximation: a malformed final entry is dropped by the
        # parser, so this under-reports rather than over-reports "there is
        # more" — the safe direction for a paging control.
        page = _search(
            context,
            settings,
            query,
            limit=settings.max_results + 1,
            start=start,
        )
        items = page[: settings.max_results]
        rows = [
            row
            for row in (_paper_row(paper, settings) for paper in items)
            if row is not None
        ]
        return {
            "items": rows,
            "start": start,
            "has_more": len(page) > settings.max_results,
        }

    @router.post(
        "/notebooks/{notebook_id}/import",
        dependencies=[
            Depends(context.require_notebook_capability("sources:write"))
        ],
    )
    def import_papers(
        notebook_id: str, payload: dict[str, Any]
    ) -> dict[str, object]:
        """Import selected arXiv PDF links through core's own URL importer."""

        urls = _import_urls(context, payload)
        result = context.url_sources.import_urls(notebook_id, urls)
        context.emit_event(
            {"event": "arxiv_urls_imported", "count": len(result.created)}
        )
        return {
            "created": [
                {"source_id": row.source_id, "title": row.title, "url": row.url}
                for row in result.created
            ],
            "rejected": [
                {"url": row.url, "reason": row.reason} for row in result.rejected
            ],
        }

    return router


def _settings(context: PluginRouteContext) -> ArxivSearchSettings | None:
    """The bound settings instance, read from the *context* rather than the bundle.

    The bundle holds the same object, but reading it from there would make this
    router depend on module state instead of on what core handed it — and the
    seam core hands it is the only thing a second instance of this plugin (or a
    test) can control.
    """

    settings = context.settings
    return settings if isinstance(settings, ArxivSearchSettings) else None


def _require_settings(context: PluginRouteContext) -> ArxivSearchSettings:
    settings = _settings(context)
    if settings is None:
        raise context.user_error(503, "arXiv 文献检索尚未配置，请联系管理员")
    return settings


def _search(
    context: PluginRouteContext,
    settings: ArxivSearchSettings,
    query: str,
    *,
    limit: int,
    start: int,
) -> tuple[ArxivPaper, ...]:
    """Run one query, mapping both failure modes onto fixed user-facing copy.

    The route's budget is generous — a politeness wait plus one timeout — since
    a person is waiting on a request they made deliberately.  That is the whole
    difference from the gap-consult caller, which has someone else's answer
    latency to protect and hands the same throttle a far smaller number.
    """

    budget = settings.timeout_seconds + settings.politeness_interval_seconds
    try:
        return arxiv_client.search(
            query,
            **search_kwargs(
                settings, limit=limit, budget_seconds=budget, start=start
            ),
        )
    except arxiv_client.ArxivThrottled:
        # Not a failure: another request holds the politeness slot.  Saying so
        # separately from 502 lets a caller retry immediately and lets an
        # operator tell "arXiv is down" from "we are busy being polite".
        raise context.user_error(503, "arXiv 检索排队中，请稍后再试") from None
    except arxiv_client.ArxivUpstreamError as exc:
        # The class name is the most this module may say about someone else's
        # failure.  It goes to the log and nowhere else: `exc.args` carries the
        # upstream text for a developer reading a traceback, the response gets
        # a fixed sentence, and the event gets a stable code (see the module
        # docstring for why the class name cannot ride on the event either).
        LOGGER.warning(
            "arxiv search failed: cause=%s", type(exc.__cause__ or exc).__name__
        )
        context.emit_event(
            {"event": "arxiv_search_failed", "outcome": "upstream_failed"}
        )
        raise context.user_error(502, "arXiv 检索暂时不可用，请稍后再试") from None


def _paper_row(
    paper: ArxivPaper, settings: ArxivSearchSettings
) -> dict[str, object] | None:
    """Map one parsed record onto the wire shape, with the same egress policy
    gap-consult already applies to a suggestion's ``url``.

    ``paper.pdf_url`` comes straight out of an upstream feed entry's ``href``
    attribute (see :mod:`.atom`): a compromised or merely buggy upstream can
    put anything there, ``javascript:`` included, and this route used to hand
    that value to the browser unchecked.  ``egress_allowed`` is the same
    check :mod:`.consult` uses to decide whether a suggestion may reach a
    reader; unlike consult — which drops a suggestion outright on a foreign
    host, because nobody asked for it — this is a page of results the caller
    *did* ask for, so a link that fails the check is replaced with the
    id-derived arXiv PDF url rather than silently shortening the page.  Only
    when ``arxiv_id`` is itself missing (which the parser already refuses to
    produce a paper without) does the row disappear instead.
    """

    pdf_url = paper.pdf_url
    if not egress_allowed(pdf_url, settings.base_url):
        if not paper.arxiv_id:
            return None
        pdf_url = f"https://arxiv.org/pdf/{paper.arxiv_id}"
    return {
        "arxiv_id": paper.arxiv_id,
        "title": paper.title,
        "authors": list(paper.authors),
        "published": paper.published,
        "summary": paper.summary,
        "pdf_url": pdf_url,
        "abs_url": paper.abs_url,
    }


def _import_urls(context: PluginRouteContext, payload: object) -> list[str]:
    """Validate the request body, or refuse before core's port is called.

    Every check here happens *before* ``url_sources`` is touched — that
    ordering is the point of the allow-list, not an optimisation, and it is
    asserted with a spy that must record zero calls.
    """

    if not isinstance(payload, dict):
        raise context.user_error(400, "请求格式不正确，请重新选择要导入的文献")
    raw = payload.get("urls")
    if not isinstance(raw, list) or not raw:
        raise context.user_error(400, "请先选择要导入的文献")
    if len(raw) > MAX_IMPORT_URLS:
        raise context.user_error(
            400, f"一次最多导入 {MAX_IMPORT_URLS} 篇，请分批选择"
        )
    urls: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise context.user_error(400, "请先选择要导入的文献")
        url = item.strip()
        # Host checked first, independent of length: a foreign host is
        # refused for what it is, not for how long it happens to be.  Only a
        # link that already cleared the host check gets the length message —
        # a legitimate arXiv link that is merely too long is a different
        # problem from one that was never going to arXiv at all, and sharing
        # one sentence between them would tell a caller the wrong thing to
        # fix.
        if not _is_arxiv_url(url):
            raise context.user_error(400, "只能导入 arXiv 的 PDF 链接")
        if len(url) > MAX_URL_CHARS:
            raise context.user_error(
                400, f"链接过长，最多支持 {MAX_URL_CHARS} 个字符"
            )
        urls.append(url)
    return urls


def _is_arxiv_url(url: str) -> bool:
    """True for an ``http(s)`` URL whose host is arxiv.org or a subdomain.

    ``urlsplit(...).hostname`` is what makes the suffix test safe: it lowercases
    the host, drops any port, and drops any ``user@`` prefix, so neither
    ``https://arxiv.org@evil.example/x`` nor ``https://ARXIV.ORG.evil.example/x``
    can be read as an arXiv host.
    """

    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    return host == _IMPORT_HOST or host.endswith(_IMPORT_HOST_SUFFIX)
