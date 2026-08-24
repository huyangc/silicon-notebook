"""Unit tests for the arXiv sample deployment plugin (X9 PR-B).

The sample plugin is not part of the backend package: it lives at
``examples/extensions/arxiv-search`` and is meant to be installed into a
deployment's interpreter.  Its ``src`` directory therefore goes on ``sys.path``
here, the same shape the discovery tests use for the throwaway plugins they
write, so these tests exercise the module a deployment would import rather than
a copy maintained beside them.

Why these tests live under the backend test root at all, when the SOP tells an
out-of-tree plugin to keep its tests in its own repository: the backend
verification lane only collects ``backend/tests``.  A sample that shipped its
tests in its own tree would ship them unrun.  The plugin README records the
difference so nobody copies this arrangement into a real out-of-tree plugin.

This file covers the whole plugin: the layers that have no Silicon Notebook
dependency at all — settings validation, Atom parsing, and the politeness
throttle — and then the adapter half, where the bundle's two capability gates,
the three HTTP routes and the gap-consult contributor are exercised against
hand-built seams.  Real discovery, a real ``create_app()`` and the mounted
wire are deliberately left to the end-to-end batch: what is under test here is
what the plugin decides, not what core does with it.
"""
from __future__ import annotations

import ast
import inspect
import logging
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.extension_routes import _event_emitter
from app.core.config import Settings as CoreSettings
from app.domain.extension_http import (
    PluginActor,
    PluginImportedSource,
    PluginRejectedUrl,
    PluginRouteContext,
    PluginUrlImportResult,
)
from app.extension_sdk import (
    AvailabilityStatus,
    ExtensionResultStatus,
    GAP_SUGGESTION_SUMMARY_MAX_CHARS,
    GAP_SUGGESTION_TITLE_MAX_CHARS,
    GapConsultExtensionContext,
    GapConsultQuery,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _REPO_ROOT / "examples" / "extensions" / "arxiv-search"
_PLUGIN_SRC = _PLUGIN_ROOT / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

import silicon_notebook_arxiv_search as arxiv_package  # noqa: E402
from silicon_notebook_arxiv_search import atom as arxiv_atom  # noqa: E402
from silicon_notebook_arxiv_search import bundle as arxiv_bundle  # noqa: E402
from silicon_notebook_arxiv_search import client as arxiv_client  # noqa: E402
from silicon_notebook_arxiv_search import consult as arxiv_consult  # noqa: E402
from silicon_notebook_arxiv_search import routes as arxiv_routes  # noqa: E402
from silicon_notebook_arxiv_search.bundle import (  # noqa: E402
    AVAILABLE_CAPABILITY,
    PLUGIN_ID,
    ArxivSearchBundle,
    BUNDLE,
)
from silicon_notebook_arxiv_search.settings import (  # noqa: E402
    ArxivSearchSettings,
    egress_allowed,
    mirror_host,
    search_kwargs,
)

_SAMPLE_FEED = Path(__file__).parent / "fixtures" / "arxiv_atom_sample.xml"

# Every module `atom` is allowed to reach for.  A whitelist rather than a
# blacklist so that *adding* an I/O import — moving a fetch into the parser
# instead of merely calling one — fails here too.
_ATOM_ALLOWED_IMPORTS = {"__future__", "re", "dataclasses", "xml.etree"}


@pytest.fixture(autouse=True)
def _reset_throttle():
    """The throttle is module state; give every test a clean, unheld one."""

    arxiv_client._LAST_REQUEST_AT = None
    yield
    if arxiv_client._THROTTLE.locked():
        arxiv_client._THROTTLE.release()
    arxiv_client._LAST_REQUEST_AT = None


@pytest.fixture
def sample_feed() -> bytes:
    return _SAMPLE_FEED.read_bytes()


def _feed_with(title: str, summary: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<id>http://arxiv.org/abs/2401.00003v1</id>"
        f"<title>{title}</title>"
        f"<summary>{summary}</summary>"
        "</entry></feed>"
    ).encode("utf-8")


def _feed_with_authors(count: int) -> bytes:
    """One entry whose ``<author>`` elements are exactly ``count`` real names
    (``Author 0`` .. ``Author {count-1}``), for exercising ``MAX_AUTHORS`` /
    ``authors_total`` without dragging in the fixture's unrelated fields.
    """
    authors = "".join(
        f"<author><name>Author {i}</name></author>" for i in range(count)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<id>http://arxiv.org/abs/2401.00099v1</id>"
        "<title>A Very Collaborative Paper</title>"
        f"{authors}"
        "</entry></feed>"
    ).encode("utf-8")


def _search_kwargs(**overrides):
    defaults = {
        "base_url": "https://export.arxiv.example/api/query",
        "limit": 5,
        "budget_seconds": 5.0,
        "timeout_seconds": 1.0,
        "politeness_interval_seconds": 0.0,
        "user_agent": "silicon-notebook-arxiv-sample/test",
    }
    defaults.update(overrides)
    return defaults


# --------------------------------------------------------------------------
# Atom parsing
# --------------------------------------------------------------------------


def test_atom_entries_parse_into_papers(sample_feed):
    papers = arxiv_atom.parse_atom(sample_feed, limit=10)

    # Three entries in, two out: the middle one has no <id> and is dropped
    # without raising, which is the whole degradation contract.
    assert len(papers) == 2
    assert [paper.arxiv_id for paper in papers] == [
        "2401.00001v1",
        "2401.00002v2",
    ]
    first = papers[0]
    assert first.title == "Retrieval-Augmented Generation for Long Documents"
    assert first.authors == ("Ada Lovelace", "Alan Turing")
    # Under `MAX_AUTHORS`, the total matches the kept list exactly — the
    # over-cap case (where they diverge) is covered separately below.
    assert first.authors_total == 2
    assert first.published == "2024-01-02T03:04:05Z"
    assert first.summary.startswith("We study retrieval-augmented generation")
    assert first.abs_url == "https://arxiv.org/abs/2401.00001v1"
    assert "An Entry With No Identifier" not in {
        paper.title for paper in papers
    }


def test_pdf_url_is_constructed_when_the_link_is_missing(sample_feed):
    papers = arxiv_atom.parse_atom(sample_feed, limit=10)

    # The second surviving entry advertises only an `alternate` landing-page
    # link.  Import probes exactly the URL it is handed, so the parser has to
    # produce a PDF direct link itself rather than pass the landing page on.
    assert b'title="pdf" href="http://arxiv.org/pdf/2401.00002' not in sample_feed
    assert papers[1].pdf_url == "https://arxiv.org/pdf/2401.00002v2"


def test_pdf_url_http_is_upgraded_to_https(sample_feed):
    papers = arxiv_atom.parse_atom(sample_feed, limit=10)

    # Guard against a vacuous assertion: arXiv really does advertise these
    # links over plain http, and the fixture must still say so.
    assert b'title="pdf" href="http://arxiv.org/pdf/2401.00001v1"' in sample_feed
    assert papers[0].pdf_url == "https://arxiv.org/pdf/2401.00001v1"


def test_title_and_summary_whitespace_is_collapsed_and_truncated(sample_feed):
    from_fixture = arxiv_atom.parse_atom(sample_feed, limit=10)[0]

    assert "\n" not in from_fixture.title
    assert "  " not in from_fixture.title
    assert "\n" not in from_fixture.summary
    assert "  " not in from_fixture.summary

    # 1000 repeats comfortably clears both of `.atom`'s now-generous ceilings
    # (500/4000 — a display-oriented in-memory bound, not core's own
    # gap-suggestion limit; see the module docstring and P2-1), so this still
    # proves the parser's own hard cut fires, independent of that value.
    long_title = "title " * 1000
    long_summary = "summary " * 1000
    truncated = arxiv_atom.parse_atom(
        _feed_with(long_title, long_summary), limit=1
    )[0]

    assert len(truncated.title) == arxiv_atom.TITLE_MAX_CHARS
    assert len(truncated.summary) == arxiv_atom.SUMMARY_MAX_CHARS
    assert truncated.title == " ".join(long_title.split())[
        : arxiv_atom.TITLE_MAX_CHARS
    ]


def test_authors_beyond_the_display_cap_are_disclosed_not_dropped():
    """`MAX_AUTHORS` still caps ``authors`` for memory/display, but
    ``authors_total`` must report the true count.  Before this, an author
    list past the cap was shortened with no way to tell — silently
    truncating user-visible data is exactly the red line
    ``authors_total`` closes (repo rule: "数值上限与截断").
    """

    papers = arxiv_atom.parse_atom(_feed_with_authors(21), limit=10)

    assert len(papers) == 1
    paper = papers[0]
    assert len(paper.authors) == arxiv_atom.MAX_AUTHORS
    assert paper.authors_total == 21
    # The kept names are the *first* `MAX_AUTHORS`, in document order — a
    # display cap should not reorder or sample the list it shortens.
    assert paper.authors[0] == "Author 0"
    assert paper.authors[-1] == f"Author {arxiv_atom.MAX_AUTHORS - 1}"


def test_authors_total_matches_the_kept_count_at_exactly_the_cap():
    """The boundary case: `count == MAX_AUTHORS` must not be treated as
    "over" — `authors_total` and `len(authors)` agree, and no caller should
    read this paper as having "and N more" authors it does not have.
    """

    papers = arxiv_atom.parse_atom(
        _feed_with_authors(arxiv_atom.MAX_AUTHORS), limit=10
    )

    assert len(papers) == 1
    assert papers[0].authors_total == arxiv_atom.MAX_AUTHORS
    assert len(papers[0].authors) == arxiv_atom.MAX_AUTHORS


def test_authors_total_ignores_author_elements_with_no_name():
    """An `<author>` element with a blank or missing `<name>` is the same
    "no name, no record" rule `_entry_to_paper` applies to the entry as a
    whole (see `_arxiv_id`/`title` gating parse_atom's own drop decision) —
    it must not inflate the total either.
    """

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<id>http://arxiv.org/abs/2401.00098v1</id>"
        "<title>Blank Author Elements</title>"
        "<author><name>Real Name</name></author>"
        "<author><name></name></author>"
        "<author></author>"
        "</entry></feed>"
    ).encode("utf-8")

    papers = arxiv_atom.parse_atom(feed, limit=10)

    assert len(papers) == 1
    assert papers[0].authors_total == 1
    assert papers[0].authors == ("Real Name",)


def test_search_route_reports_the_true_author_count_alongside_the_capped_list(
    monkeypatch,
):
    """The wire shape must carry ``authors_total`` — the panel's disclosure
    ("等 N 人", see `search-panel-model.ts::formatAuthors`) reads it, and a
    response that only ever sends the capped list has no way to tell a
    twenty-author paper from a two-thousand-author one.
    """

    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: _feed_with_authors(21)
    )

    context, _ = _route_context(_test_settings(max_results=5))
    search = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/search", "GET"
    ).endpoint
    body = search(notebook_id="nb-1", q="collaboration")

    assert len(body["items"]) == 1
    item = body["items"][0]
    assert len(item["authors"]) == arxiv_atom.MAX_AUTHORS
    assert item["authors_total"] == 21


def test_search_route_returns_a_long_summary_without_truncating_it_to_cores_limit(
    monkeypatch,
):
    """`.atom`'s display bound is no longer pinned to core's gap-suggestion one.

    A summary comfortably past core's own ``GAP_SUGGESTION_SUMMARY_MAX_CHARS``
    (400) but still well under ``.atom.SUMMARY_MAX_CHARS`` (4000) must reach
    the interactive `/search` response whole — that page is what a person
    reads results from, and cutting an abstract off mid-sentence with no
    ellipsis is silent data loss on a page nobody asked to have shortened.
    See P2-1: the gap-consult-specific cut moved to `.consult`, precisely so
    this path would stop sharing it.
    """

    long_summary = "term " * 600
    assert len(" ".join(long_summary.split())) > GAP_SUGGESTION_SUMMARY_MAX_CHARS
    assert len(" ".join(long_summary.split())) < arxiv_atom.SUMMARY_MAX_CHARS

    feed = _feed_with("A perfectly ordinary title", long_summary)
    monkeypatch.setattr(arxiv_client, "_fetch", lambda *args: feed)

    context, _ = _route_context(_test_settings(max_results=5))
    search = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/search", "GET"
    ).endpoint
    body = search(notebook_id="nb-1", q="term")

    assert len(body["items"]) == 1
    returned_summary = body["items"][0]["summary"]
    assert returned_summary == " ".join(long_summary.split())
    assert len(returned_summary) > GAP_SUGGESTION_SUMMARY_MAX_CHARS


def test_entry_count_is_capped_by_limit(sample_feed):
    assert len(arxiv_atom.parse_atom(sample_feed, limit=1)) == 1
    assert arxiv_atom.parse_atom(sample_feed, limit=0) == ()
    assert arxiv_atom.parse_atom(sample_feed, limit=-3) == ()


def test_parse_atom_short_circuits_on_nonpositive_limit_even_for_malformed_input():
    # The ``limit <= 0`` guard must fire before ``ElementTree.fromstring`` ever
    # sees the payload — proven here with input that would raise if parsed.
    assert arxiv_atom.parse_atom(b"<truncated", limit=0) == ()
    assert arxiv_atom.parse_atom(b"<truncated", limit=-1) == ()


def test_arxiv_id_over_the_length_ceiling_is_dropped():
    # A pathological <id> must not sail through unbounded: every other field
    # on ArxivPaper is already capped, and the id was the one exception.
    huge_id = "2401." + "0" * 5000 + "v1"
    payload = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry>"
        f"<id>http://arxiv.org/abs/{huge_id}</id>"
        "<title>Oversized identifier</title>"
        "</entry>"
        "<entry>"
        "<id>http://arxiv.org/abs/2401.00099v1</id>"
        "<title>Ordinary identifier</title>"
        "</entry>"
        "</feed>"
    ).encode("utf-8")

    papers = arxiv_atom.parse_atom(payload, limit=10)

    assert len(papers) == 1
    assert papers[0].arxiv_id == "2401.00099v1"


def test_pure_layer_cannot_reach_the_backend():
    """Neither ``atom`` nor ``client`` may import anything under ``app.``.

    ``test_parser_performs_no_io`` above already whitelists ``atom``'s exact
    import set and is left untouched; this is a separate, narrower assertion
    that covers both pure-layer modules against the one thing that would
    actually breach the layering the package docstring promises — a plugin
    author reaching for ``app.*`` for its side effects without ever calling
    into it, which a behavioural "no I/O happens" test cannot see at all.
    """
    for module in (arxiv_atom, arxiv_client):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        backend_imports = {
            name for name in imported if name == "app" or name.startswith("app.")
        }
        assert not backend_imports, (module.__name__, sorted(backend_imports))


def test_parser_performs_no_io(sample_feed, monkeypatch):
    calls: list[tuple] = []

    def spy(*args):
        calls.append(args)
        raise AssertionError("the parser must not dial anything")

    monkeypatch.setattr(arxiv_client, "_fetch", spy)

    assert len(arxiv_atom.parse_atom(sample_feed, limit=10)) == 2
    assert calls == []

    # Behavioural absence is only half of it — prove the parser cannot reach a
    # transport at all, so moving one into it is a failure rather than a habit.
    tree = ast.parse(Path(arxiv_atom.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= _ATOM_ALLOWED_IMPORTS, sorted(
        imported - _ATOM_ALLOWED_IMPORTS
    )


# --------------------------------------------------------------------------
# Politeness throttle
# --------------------------------------------------------------------------


def test_throttle_serializes_and_spaces_requests(sample_feed):
    interval = 0.05
    windows: list[tuple[float, float]] = []
    results: list[object] = []
    failures: list[BaseException] = []
    bookkeeping = threading.Lock()

    def stub(url, timeout, user_agent):
        entered = time.monotonic()
        time.sleep(0.02)
        left = time.monotonic()
        with bookkeeping:
            windows.append((entered, left))
        return sample_feed

    def run():
        try:
            papers = arxiv_client.search(
                "retrieval augmented generation",
                fetch=stub,
                **_search_kwargs(politeness_interval_seconds=interval),
            )
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            with bookkeeping:
                failures.append(exc)
            return
        with bookkeeping:
            results.append(papers)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10.0)

    assert not failures, failures
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == 2
    assert all(len(papers) == 2 for papers in results)

    windows.sort()
    assert len(windows) == 2
    # Serialised: the lock spans the whole call, so the windows cannot overlap.
    assert windows[1][0] >= windows[0][1]
    # Spaced: measured end-to-start, matching release_slot's own documented
    # contract ("the interval is measured from the end of one request to the
    # start of the next").  A start-to-start comparison here would still pass
    # if the stamp were taken on acquire instead of release, because the first
    # request's own 0.02s of work time gets silently credited toward the
    # interval; end-to-start does not give that credit.
    assert windows[1][0] - windows[0][1] >= interval


def test_throttle_refuses_rather_than_sleeping_past_the_budget():
    # Part one — the politeness wait itself must not outlast the budget.  Take
    # and release a slot first so the next caller has a fresh stamp to wait on.
    assert arxiv_client.acquire_slot(0.0, 1.0) is True
    arxiv_client.release_slot()

    started = time.monotonic()
    assert arxiv_client.acquire_slot(1.0, 0.05) is False
    assert time.monotonic() - started < 0.5
    assert arxiv_client._THROTTLE.locked() is False

    # Part two — so must the wait for the lock.  Interval zero here on purpose:
    # the only thing that can refuse this caller is the bounded acquire, so an
    # unbounded one is caught instead of being masked by the politeness check.
    held = threading.Event()
    may_release = threading.Event()
    outcome: dict[str, object] = {}

    def holder():
        if arxiv_client.acquire_slot(0.0, 5.0):
            held.set()
            may_release.wait(5.0)
            arxiv_client.release_slot()

    def waiter():
        began = time.monotonic()
        outcome["result"] = arxiv_client.acquire_slot(0.0, 0.05)
        outcome["elapsed"] = time.monotonic() - began

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert held.wait(5.0)

    waiter_thread = threading.Thread(target=waiter)
    waiter_thread.start()
    waiter_thread.join(0.5)
    still_waiting = waiter_thread.is_alive()

    # Unblock before asserting, so a failure leaves no thread parked on a lock.
    may_release.set()
    holder_thread.join(5.0)
    waiter_thread.join(5.0)

    assert still_waiting is False
    assert outcome["result"] is False
    assert outcome["elapsed"] < 0.5


def test_throttle_lock_is_released_on_exception():
    def boom(url, timeout, user_agent):
        raise TimeoutError("upstream took too long")

    with pytest.raises(arxiv_client.ArxivUpstreamError):
        arxiv_client.search("graph neural networks", fetch=boom, **_search_kwargs())

    assert arxiv_client._THROTTLE.locked() is False
    assert arxiv_client.acquire_slot(0.0, 0.5) is True
    arxiv_client.release_slot()


def test_search_distinguishes_throttled_from_upstream_failure():
    # Hold the throttle from another thread so this call's own acquire_slot
    # has no way to succeed inside its tiny budget.
    held = threading.Event()
    may_release = threading.Event()
    calls: list[tuple] = []

    def spy(url, timeout, user_agent):
        calls.append((url, timeout, user_agent))
        return b""

    def holder():
        if arxiv_client.acquire_slot(0.0, 5.0):
            held.set()
            may_release.wait(5.0)
            arxiv_client.release_slot()

    holder_thread = threading.Thread(target=holder)
    holder_thread.start()
    assert held.wait(5.0)

    try:
        with pytest.raises(arxiv_client.ArxivThrottled):
            arxiv_client.search(
                "graph neural networks",
                fetch=spy,
                **_search_kwargs(budget_seconds=0.05, politeness_interval_seconds=0.0),
            )
    finally:
        may_release.set()
        holder_thread.join(5.0)

    # The two outcomes must stay distinguishable at the ``search()`` boundary:
    # a throttle refusal is diagnosed before ``fetch`` is ever called, unlike
    # an ``ArxivUpstreamError`` raised by a failing ``fetch``.
    assert calls == []


# --------------------------------------------------------------------------
# search() with a non-positive limit
# --------------------------------------------------------------------------


def test_search_returns_empty_without_fetching_when_limit_is_not_positive():
    calls: list[tuple] = []

    def spy(url, timeout, user_agent):
        calls.append((url, timeout, user_agent))
        return b""

    assert (
        arxiv_client.search("graph neural networks", fetch=spy, **_search_kwargs(limit=0))
        == ()
    )
    assert arxiv_client.search(
        "graph neural networks", fetch=spy, **_search_kwargs(limit=-3)
    ) == ()
    assert calls == []


# --------------------------------------------------------------------------
# _fetch()
# --------------------------------------------------------------------------


def test_fetch_reads_at_most_the_response_byte_ceiling(monkeypatch):
    read_calls: list[int] = []

    class _FakeResponse:
        def read(self, n=-1):
            read_calls.append(n)
            return b"<feed></feed>"

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def fake_urlopen(request, timeout=None):
        return _FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    arxiv_client._fetch("https://export.arxiv.example/api/query", 1.0, "test-agent")

    assert read_calls == [arxiv_client.MAX_RESPONSE_BYTES]


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_settings_defaults():
    settings = ArxivSearchSettings()

    # Installing the plugin does not consent to outbound gap consultation.
    assert settings.consult_enabled is False
    # arXiv asks for three seconds between requests; that is the default, not a
    # value a deployment has to remember to set.
    assert settings.politeness_interval_seconds == 3.0
    assert settings.base_url == "https://export.arxiv.org/api/query"
    assert settings.max_results == 10
    assert settings.timeout_seconds == 10.0
    assert settings.consult_max_suggestions == 3
    assert "arxiv" in settings.user_agent.lower()


@pytest.mark.parametrize(
    "field, value",
    [
        ("max_results", 0),
        ("max_results", 21),
        ("timeout_seconds", 0),
        ("timeout_seconds", 61),
        ("politeness_interval_seconds", -1),
        ("politeness_interval_seconds", 31),
        ("consult_max_suggestions", 0),
        ("consult_max_suggestions", 6),
    ],
)
def test_settings_reject_out_of_range(field, value):
    with pytest.raises(ValidationError):
        ArxivSearchSettings(**{field: value})


def test_settings_reject_an_unknown_key():
    # Core computes the accepted key set itself, so this is belt and braces —
    # but a sample that silently swallowed a misspelled key would teach the
    # wrong shape to anyone copying it.
    with pytest.raises(ValidationError):
        ArxivSearchSettings(politeness_interval=1.0)


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///etc/passwd",
        "",
        "not a url",
        "https://export.arxiv.org/api/query#frag",
        "https://export.arxiv.org/api/query?x=1",
    ],
)
def test_settings_reject_invalid_base_url(base_url):
    with pytest.raises(ValidationError):
        ArxivSearchSettings(base_url=base_url)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://export.arxiv.org/api/query",
        "https://export.arxiv.org/api/query",
        "https://mirror.example.com:8443/api/query",
    ],
)
def test_settings_accept_valid_base_url(base_url):
    assert ArxivSearchSettings(base_url=base_url).base_url == base_url


@pytest.mark.parametrize(
    "user_agent",
    [
        "",
        "   ",
        # The classic request-splitting pair: a bare CR/LF sent verbatim in a
        # header value smuggles a second header line into the request.
        "silicon-notebook-arxiv-sample/0.1\r\nX-Injected: 1",
        "silicon-notebook-arxiv-sample/0.1\x00",
    ],
)
def test_settings_reject_blank_or_control_character_user_agent(user_agent):
    with pytest.raises(ValidationError):
        ArxivSearchSettings(user_agent=user_agent)


def test_settings_accept_an_ordinary_user_agent():
    value = "some-deployment/2.0 (+https://example.com/bot)"
    assert ArxivSearchSettings(user_agent=value).user_agent == value


def test_mirror_host_extracts_a_lowercase_host_and_is_blank_on_failure():
    """The primitive :func:`egress_allowed` and ``routes.py::_is_arxiv_url``
    now both build their mirror-host check from.

    ``hostname`` lowercases and drops the port — the same normalisation the
    import route's own arxiv.org suffix check already relies on (see
    ``test_import_route_accepts_host_shapes_that_still_resolve_to_arxiv``) —
    and an unparsable value degrades to ``""`` rather than raising, so a
    caller comparing against it (never equal to a real hostname) fails safe.
    """

    assert mirror_host("https://export.arxiv.example/api/query") == "export.arxiv.example"
    assert mirror_host("https://EXPORT.ARXIV.EXAMPLE:8443/x") == "export.arxiv.example"
    assert mirror_host("") == ""


def test_egress_allowed_still_accepts_only_arxivs_own_hosts_and_the_configured_mirror():
    """The refactor that extracted :func:`mirror_host` must not widen or
    narrow :func:`egress_allowed`'s own exact-match set — it is intentionally
    narrower than the import route's suffix rule (see the module docstring).
    """

    base_url = "https://export.arxiv.example/api/query"
    assert egress_allowed("https://arxiv.org/pdf/x", base_url) is True
    assert egress_allowed("https://export.arxiv.org/pdf/x", base_url) is True
    assert egress_allowed("https://export.arxiv.example/pdf/x", base_url) is True
    # No suffix widening: a subdomain of the mirror is not the mirror.
    assert egress_allowed("https://sub.export.arxiv.example/pdf/x", base_url) is False
    assert egress_allowed("https://evil.example/pdf/x", base_url) is False
    # An unparsable configured mirror must not make everything compare equal.
    assert egress_allowed("https://evil.example/pdf/x", "not a url") is False


def test_example_toml_worst_case_arithmetic_matches_the_constants():
    """The sample TOML's ⚠ block spells out arithmetic, not prose.

    ``politeness_interval_seconds`` + ``timeout_seconds`` (both settings
    defaults) plus :data:`arxiv_consult.CONSULT_RETURN_MARGIN_SECONDS` is the
    plugin's own worst case, and core's default
    ``ask_gap_consult_timeout_seconds`` is the deadline it is compared
    against — together they are why gap consultation with every shipped
    default never fires.  None of the three numbers has a test tying it to
    this sentence, so a change to any of them would go stale in prose nobody
    re-derives; this regenerates the figure from the real constants and greps
    the file for the same digits, so drift fails here instead of only in a
    future re-reading.
    """

    settings = ArxivSearchSettings()
    worst_case = (
        settings.politeness_interval_seconds
        + settings.timeout_seconds
        + arxiv_consult.CONSULT_RETURN_MARGIN_SECONDS
    )
    default_deadline = CoreSettings.model_fields[
        "ask_gap_consult_timeout_seconds"
    ].default

    text = (_PLUGIN_ROOT / "extensions.example.toml").read_text(encoding="utf-8")
    match = re.search(
        r"that is ([\d.]+) seconds against a ([\d.]+)-second deadline", text
    )
    assert match is not None, "worst-case sentence not found in the sample TOML"
    assert float(match.group(1)) == pytest.approx(worst_case)
    assert float(match.group(2)) == pytest.approx(default_deadline)
    # And it is genuinely bigger than the deadline it is compared against —
    # that gap is the whole point of the paragraph.
    assert worst_case > default_deadline


def test_search_kwargs_covers_every_transport_parameter():
    """One mapper, and it must name *all* of ``search``'s keyword arguments.

    Two call sites spelling this mapping by hand is how a plugin ends up
    sending its configured user agent from one route and the transport's
    default from another.  Comparing against the real signature also catches
    the other direction: a transport that grows a parameter the mapper does not
    supply would silently run on that parameter's default everywhere.
    """

    signature = inspect.signature(arxiv_client.search)
    transport = {
        name
        for name, parameter in signature.parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        # A test seam on the transport, not a deployment setting: production
        # callers never pass one, so the mapper deliberately omits it.
        and name != "fetch"
    }
    settings = ArxivSearchSettings()
    mapped = search_kwargs(settings, limit=7, budget_seconds=1.5, start=25)

    assert set(mapped) == transport
    assert mapped == {
        "base_url": settings.base_url,
        "limit": 7,
        "budget_seconds": 1.5,
        "timeout_seconds": settings.timeout_seconds,
        "politeness_interval_seconds": settings.politeness_interval_seconds,
        "user_agent": settings.user_agent,
        "start": 25,
    }


def test_every_search_call_site_goes_through_the_shared_mapper():
    """The mapper is only shared if nobody spells the mapping out beside it.

    A move mutation — inlining the same seven keyword arguments at one call
    site — leaves the parity test above perfectly green while reintroducing
    exactly the drift it exists to prevent: one route sending the configured
    user agent and the other sending the transport's default.  So the call
    shape itself is asserted: every ``search(...)`` in the adapter passes the
    query and ``**search_kwargs(...)``, and nothing else.
    """

    call_sites = 0
    for module in (arxiv_routes, arxiv_consult):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "search"
                and isinstance(func.value, ast.Name)
                and func.value.id == "arxiv_client"
            ):
                continue
            call_sites += 1
            # The diagnostic names the call itself, not a file:line — the
            # repository's test-contract policy refuses source-position
            # identities in this tree (`architecture/policy.py`), and it is
            # right to: a line number in an assertion message rots on the next
            # edit above it, while the unparsed call expression stays true.
            where = f"{Path(module.__file__).name}: {ast.unparse(node)}"
            # No hand-written transport keyword may appear here…
            assert [kw.arg for kw in node.keywords] == [None], where
            # …and the one ``**`` expansion must be the shared mapper.
            expansion = node.keywords[0].value
            assert isinstance(expansion, ast.Call), where
            assert isinstance(expansion.func, ast.Name), where
            assert expansion.func.id == "search_kwargs", where

    # Vacuity guard: both call sites really were found.
    assert call_sites == 2


def test_the_adapter_half_only_imports_the_extension_sdk():
    """SOP §3.6: a plugin imports ``app.extension_sdk`` and ``app.domain`` only.

    Never a concrete repository, facade, runtime, service or ``app.api``
    module.  ``test_pure_layer_cannot_reach_the_backend`` above covers the arXiv
    half by forbidding ``app.*`` outright; this covers the half that legitimately
    imports *some* of it, where the boundary is which subpackage rather than
    whether.  The failure this catches is quiet — a plugin that reached into
    ``app.services`` would work perfectly until the day core moved the module.
    """

    for module in (arxiv_routes, arxiv_consult, arxiv_bundle):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        backend = {
            name for name in imported if name == "app" or name.startswith("app.")
        }
        # Vacuity guard: each of these really does import from core.
        assert backend, module.__name__
        for name in backend:
            assert name.split(".")[1] in {"extension_sdk", "domain"}, (
                module.__name__,
                name,
            )


# --------------------------------------------------------------------------
# Shared seams for the adapter half
# --------------------------------------------------------------------------


class _UserError(Exception):
    """Stand-in for what ``context.user_error`` builds.

    Core returns an ``HTTPException`` whose detail carries the
    ``X-User-Message`` header; what the plugin contract actually says is only
    "call it, raise what it returns", so the fake records the two things a
    route decides — the status and the sentence.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class _UrlSourcesSpy:
    """Records every call. The import allow-list is asserted against ``calls``."""

    def __init__(self, created=(), rejected=()) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._created = tuple(created)
        self._rejected = tuple(rejected)

    def import_urls(self, notebook_id, urls):
        self.calls.append((notebook_id, list(urls)))
        return PluginUrlImportResult(
            created=self._created, rejected=self._rejected
        )

    async def import_urls_async(self, notebook_id, urls):  # pragma: no cover
        raise AssertionError("sync handlers must use import_urls")


@dataclass
class _EventLogSpy:
    """Duck-typed ``EventLogger`` — only ``emit`` is ever called."""

    records: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.records = []

    def emit(self, record) -> None:
        self.records.append(record)


def _route_context(
    settings: ArxivSearchSettings | None = None,
    *,
    url_sources: _UrlSourcesSpy | None = None,
    event_log: _EventLogSpy | None = None,
) -> tuple[PluginRouteContext, dict[str, Any]]:
    """A hand-built ``PluginRouteContext`` plus the seams it was wired from.

    Deliberately not a real app: what is under test is what the plugin's own
    handlers decide.  Whether core's gates actually run is core's own contract
    and is covered where the router is really mounted.  The gate *objects* are
    still checked here — that a route declares them at all is the plugin's
    responsibility, and it is asserted structurally below.
    """

    # Gate stand-ins must be callables with distinct identity: FastAPI asserts
    # a parameter-less dependency is callable when the route is declared, and
    # the assertions below compare by identity.
    def _gate(label: str):
        def gate() -> str:  # pragma: no cover - never invoked without an app
            return label

        return gate

    capability_gates: dict[str, object] = {}

    def require_capability(name: str) -> object:
        return capability_gates.setdefault(name, _gate(f"capability:{name}"))

    read_gate = _gate("read")
    log = event_log if event_log is not None else _EventLogSpy()
    sources = url_sources if url_sources is not None else _UrlSourcesSpy()
    context = PluginRouteContext(
        plugin_id=PLUGIN_ID,
        settings=settings,
        require_notebook_capability=require_capability,
        require_notebook_read=read_gate,
        current_actor=lambda: PluginActor(id="user-1", is_admin=False),
        user_error=_UserError,
        url_sources=sources,
        # The real sanitizer, not a passthrough: a payload this plugin builds
        # has to survive core's whitelist, and a stub would prove nothing.
        emit_event=_event_emitter(PLUGIN_ID, log),
    )
    return context, {
        "capability_gates": capability_gates,
        "read_gate": read_gate,
        "event_log": log,
        "url_sources": sources,
    }


def _route(router, path: str, method: str):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route
    raise AssertionError(f"no {method} {path} in {[r.path for r in router.routes]}")


def _test_settings(**overrides) -> ArxivSearchSettings:
    values = {
        "base_url": "https://export.arxiv.example/api/query",
        "politeness_interval_seconds": 0.0,
        "timeout_seconds": 1.0,
        "max_results": 2,
    }
    values.update(overrides)
    return ArxivSearchSettings(**values)


def _configured_bundle(**overrides) -> ArxivSearchBundle:
    """A fresh bundle, so no test mutates the module-level ``BUNDLE``."""

    bundle = ArxivSearchBundle(BUNDLE.manifest)
    bundle.configure(_test_settings(**overrides))
    return bundle


# --------------------------------------------------------------------------
# Bundle: manifest, registration, and the two capability gates
# --------------------------------------------------------------------------


def test_bundle_is_not_reexported_from_the_package_init():
    """``__init__`` must not drag FastAPI and the SDK in behind a bare import.

    Asserted on the source rather than on the imported module, because this
    test file has already imported ``bundle`` itself: at that point
    ``sys.modules`` proves nothing about what ``import
    silicon_notebook_arxiv_search`` alone would have pulled in.
    """

    assert not hasattr(arxiv_package, "BUNDLE")
    assert arxiv_package.__all__ == ["__version__"]

    tree = ast.parse(Path(arxiv_package.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= {"__future__"}, sorted(imported)

    # The other half of the same fact: the shipped config sample must name the
    # submodule, since the package no longer offers the shorter spelling.
    sample = (_PLUGIN_ROOT / "extensions.example.toml").read_text(encoding="utf-8")
    assert 'bundle = "silicon_notebook_arxiv_search.bundle:BUNDLE"' in sample


def test_manifest_registrations_match_declarations():
    registered: list = []

    class _Registrar:
        def add_contributor(self, contribution) -> None:
            registered.append(contribution)

        def __getattr__(self, name):  # pragma: no cover - defensive
            raise AssertionError(f"unexpected registrar call: {name}")

    ArxivSearchBundle(BUNDLE.manifest).register(_Registrar())

    declared = {declaration.id for declaration in BUNDLE.manifest.contributions}
    assert {c.declaration.id for c in registered} == declared
    # UI declarations are metadata and travel on ``ui_contributions``; a panel
    # id leaking into the registered set would stop the process at freeze.
    assert BUNDLE.manifest.ui_contributions[0].id not in declared


def test_provides_and_capability_decisions_agree():
    assert set(BUNDLE.capability_decisions) == set(BUNDLE.manifest.provides)
    assert BUNDLE.manifest.provides == (AVAILABLE_CAPABILITY,)
    assert ":" not in AVAILABLE_CAPABILITY


def test_manifest_requires_is_empty_so_the_router_survives_a_disabled_consult():
    """``requires`` only gates whatever looks itself up via
    ``registry.availability(contribution_id, ...)``.

    ``ExtensionRegistry.availability`` does iterate ``manifest.requires``
    before it reaches a contribution's own probe — but that accessor is not
    on the path HTTP route mounting takes (routers are mounted
    unconditionally at startup from the registered contribution set) nor on
    the one the workspace panel takes (its own ``capability`` is evaluated
    directly via ``registry.capability_availability()``).  So naming the
    consult gate in ``manifest.requires`` would *not* have taken the HTTP
    routes or the workspace entry down with ``consult_enabled = false``,
    contrary to what an earlier version of this docstring claimed.  It is
    still left empty for precision (a manifest-wide gate would be silently
    inherited by any contribution this plugin adds later that a future
    consumer *does* look up that way) and because ``requires`` reads as an
    overall precondition for the plugin instance, not a per-feature toggle.
    The per-contribution probe below is where consultation is actually
    gated.
    """

    assert BUNDLE.manifest.requires == ()

    consult = _contribution(BUNDLE, f"{PLUGIN_ID}.gap_consult")
    router = _contribution(BUNDLE, f"{PLUGIN_ID}.router")
    assert consult.availability is not None
    # And the router has none, so nothing about consultation can reach it.
    assert router.availability is None


def _contribution(bundle: ArxivSearchBundle, contribution_id: str):
    collected: list = []

    class _Registrar:
        def add_contributor(self, contribution) -> None:
            collected.append(contribution)

    bundle.register(_Registrar())
    for contribution in collected:
        if contribution.declaration.id == contribution_id:
            return contribution
    raise AssertionError(f"{contribution_id} was not registered")


def test_ui_capability_probe_is_disabled_when_unconfigured():
    unconfigured = ArxivSearchBundle(BUNDLE.manifest)
    decision = unconfigured.capability_decisions[AVAILABLE_CAPABILITY](None)
    assert decision.status is AvailabilityStatus.DISABLED
    assert decision.reason_code == "not_configured"

    configured = _configured_bundle()
    assert (
        configured.capability_decisions[AVAILABLE_CAPABILITY](None).status
        is AvailabilityStatus.AVAILABLE
    )


def test_consult_probe_is_disabled_by_default():
    # Configured, but ``consult_enabled`` defaults to false: installing the
    # plugin is not consent to send question-derived keywords outward.
    bundle = _configured_bundle()
    decision = bundle._consult_available(None)
    assert decision.status is AvailabilityStatus.DISABLED
    assert decision.reason_code == "consult_disabled"

    # The workspace entry stays up, which is the point of the split.
    assert (
        bundle.capability_decisions[AVAILABLE_CAPABILITY](None).status
        is AvailabilityStatus.AVAILABLE
    )


def test_consult_probe_is_available_when_enabled():
    bundle = _configured_bundle(consult_enabled=True)
    assert bundle._consult_available(None).status is AvailabilityStatus.AVAILABLE


def test_both_probes_perform_no_io(monkeypatch):
    """Neither probe may dial anything.

    The consult probe has a second reason beyond the SDK's general rule: core
    runs it on the same deadline-bound worker thread as ``consult`` itself, so
    a probe that called arXiv would spend the reader's own latency budget
    deciding whether it was allowed to spend the reader's latency budget.
    """

    calls: list = []

    def spy(*args):
        calls.append(args)
        raise AssertionError("an availability probe must not dial anything")

    monkeypatch.setattr(arxiv_client, "_fetch", spy)

    bundle = _configured_bundle(consult_enabled=True)
    assert (
        bundle.capability_decisions[AVAILABLE_CAPABILITY](None).status
        is AvailabilityStatus.AVAILABLE
    )
    assert bundle._consult_available(None).status is AvailabilityStatus.AVAILABLE

    assert calls == []
    # Not even a politeness slot: a probe that took the throttle would serialise
    # itself behind whatever real request happens to hold it.
    assert arxiv_client._THROTTLE.locked() is False
    assert arxiv_client._LAST_REQUEST_AT is None


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


def test_notebook_routes_declare_core_gates():
    context, seams = _route_context(_test_settings())
    router = arxiv_routes.build_router(context)

    search = _route(router, "/notebooks/{notebook_id}/search", "GET")
    assert seams["read_gate"] in {d.dependency for d in search.dependencies}

    imports = _route(router, "/notebooks/{notebook_id}/import", "POST")
    write_gate = seams["capability_gates"]["sources:write"]
    assert write_gate in {d.dependency for d in imports.dependencies}

    # The literal parameter name is what core's structural check looks for.
    for route in (search, imports):
        assert "{notebook_id}" in route.path

    # Every handler is a plain ``def``: an ``async def`` one would call the
    # blocking import port (and block on arXiv) from the event loop thread.
    for route in router.routes:
        assert not inspect.iscoroutinefunction(route.endpoint), route.path


def test_health_route_makes_no_remote_call(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )

    context, _ = _route_context(_test_settings())
    health = _route(arxiv_routes.build_router(context), "/health", "GET").endpoint
    body = health(_actor=PluginActor(id="user-1", is_admin=False))

    assert body == {"plugin_id": PLUGIN_ID, "configured": True}
    assert calls == []

    unconfigured, _ = _route_context(None)
    other = _route(arxiv_routes.build_router(unconfigured), "/health", "GET")
    assert other.endpoint(_actor=PluginActor(id="u", is_admin=False))[
        "configured"
    ] is False


def test_search_route_rejects_blank_and_overlong_query(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )

    context, _ = _route_context(_test_settings())
    search = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/search", "GET"
    ).endpoint

    for blank in ("", "   "):
        with pytest.raises(_UserError) as blank_error:
            search(notebook_id="nb-1", q=blank)
        assert blank_error.value.status_code == 400

    with pytest.raises(_UserError) as long_error:
        search(notebook_id="nb-1", q="x" * (arxiv_routes.QUERY_MAX_CHARS + 1))
    assert long_error.value.status_code == 400

    with pytest.raises(_UserError) as start_error:
        search(notebook_id="nb-1", q="graph", start=-1)
    assert start_error.value.status_code == 400

    # A refusal is a refusal: none of these may cost a round trip.
    assert calls == []


def test_search_route_rejects_a_start_beyond_the_plugin_bound(monkeypatch):
    """``start`` has an upper bound too, not just a lower one.

    Nothing stopped a caller from asking for an arbitrarily deep page before
    this: the request would still cost a full politeness slot and round trip
    to prove there was nothing there.  ``START_MAX`` is a plugin-private,
    order-of-magnitude ceiling — not an attempt to mirror arXiv's own paging
    limit — and the refusal must be its own sentence rather than reusing the
    one ``start < 0`` uses, so a caller (or an operator reading a log) can
    tell which of the two bounds was crossed.
    """

    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )

    context, _ = _route_context(_test_settings())
    search = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/search", "GET"
    ).endpoint

    with pytest.raises(_UserError) as negative_error:
        search(notebook_id="nb-1", q="graph", start=-1)
    with pytest.raises(_UserError) as overflow_error:
        search(notebook_id="nb-1", q="graph", start=arxiv_routes.START_MAX + 1)

    assert overflow_error.value.status_code == 400
    assert overflow_error.value.detail != negative_error.value.detail

    # A refusal is a refusal: zero network cost, zero throttle slot.
    assert calls == []


def test_search_route_rejects_over_limit_query_terms_instead_of_silently_dropping_them(
    monkeypatch, sample_feed
):
    """Silent truncation of user-edited data is a red line this closes.

    ``client.py::build_query_url`` slices to ``MAX_QUERY_TERMS`` internally —
    that used to be the *only* enforcement, so the ninth word onward of a
    query vanished with no warning by the time it reached arXiv. The route
    must now refuse explicitly, before the throttle or the network are
    touched, using the exact same whitespace-split counting rule the slice
    uses; a query at the limit must still reach arXiv with every word intact.
    """

    calls: list[str] = []

    def stub(url, timeout, user_agent):
        calls.append(url)
        return sample_feed

    monkeypatch.setattr(arxiv_client, "_fetch", stub)

    context, _ = _route_context(_test_settings(max_results=1))
    search = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/search", "GET"
    ).endpoint

    over_limit = " ".join(f"term{i}" for i in range(arxiv_client.MAX_QUERY_TERMS + 1))
    with pytest.raises(_UserError) as error:
        search(notebook_id="nb-1", q=over_limit)
    assert error.value.status_code == 400
    # A refusal is a refusal: zero network cost, zero throttle slot.
    assert calls == []

    at_limit = " ".join(f"term{i}" for i in range(arxiv_client.MAX_QUERY_TERMS))
    search(notebook_id="nb-1", q=at_limit)

    assert len(calls) == 1
    query = parse_qs(urlsplit(calls[0]).query)
    # All eight words reached arXiv — none silently dropped.
    assert query["search_query"] == [
        " AND ".join(f"all:{term}" for term in at_limit.split())
    ]


def test_search_route_returns_a_page_and_reports_has_more(monkeypatch, sample_feed):
    seen: list[str] = []

    def stub(url, timeout, user_agent):
        seen.append(url)
        return sample_feed

    monkeypatch.setattr(arxiv_client, "_fetch", stub)

    context, _ = _route_context(_test_settings(max_results=1))
    search = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/search", "GET"
    ).endpoint
    body = search(notebook_id="nb-1", q="retrieval augmented", start=0)

    assert len(body["items"]) == 1
    assert body["start"] == 0
    # Two usable entries came back for a page of one, so there is more.  The
    # extra record is asked for in the same round trip rather than in a second.
    assert body["has_more"] is True
    assert body["items"][0]["pdf_url"].startswith("https://arxiv.org/pdf/")

    query = parse_qs(urlsplit(seen[0]).query)
    assert query["max_results"] == ["2"]
    assert query["start"] == ["0"]


def test_search_route_pdf_url_never_carries_a_feed_supplied_non_arxiv_link(
    monkeypatch,
):
    """``/search``'s ``pdf_url`` gets the same egress policy gap-consult has.

    ``pdf_url`` is parsed straight out of an upstream feed entry's ``href``
    attribute; a compromised or merely misbehaving upstream can put anything
    there — this feed puts a ``javascript:`` URL on it.  Unlike gap-consult,
    which drops a suggestion outright on a foreign host because nobody asked
    for it, ``/search`` is a page of results the caller *did* ask for, so the
    row survives with the id-derived arXiv PDF url instead of the raw value.
    """

    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<id>http://arxiv.org/abs/2401.99999v1</id>"
        "<title>Malicious link</title>"
        '<link title="pdf" href="javascript:alert(1)"/>'
        "</entry></feed>"
    ).encode("utf-8")
    monkeypatch.setattr(arxiv_client, "_fetch", lambda *args: feed)

    context, _ = _route_context(_test_settings(max_results=5))
    search = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/search", "GET"
    ).endpoint
    body = search(notebook_id="nb-1", q="graph")

    assert len(body["items"]) == 1
    assert body["items"][0]["pdf_url"] == "https://arxiv.org/pdf/2401.99999v1"


def test_search_route_maps_a_throttle_refusal_to_503(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )
    monkeypatch.setattr(arxiv_client, "acquire_slot", lambda *args: False)

    context, _ = _route_context(_test_settings())
    search = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/search", "GET"
    ).endpoint

    with pytest.raises(_UserError) as error:
        search(notebook_id="nb-1", q="graph neural networks")

    # 503, not 502: nothing failed, someone else holds the politeness slot.
    # Collapsing the two would tell an operator "arXiv is down" when the truth
    # is "we are busy being polite", and would tell the caller not to retry.
    assert error.value.status_code == 503
    assert calls == []


def test_search_route_maps_upstream_failure_to_502_without_leaking_the_exception_text(
    monkeypatch, caplog
):
    secret = "SECRET-UPSTREAM-DETAIL"
    settings = _test_settings()
    host = urlsplit(settings.base_url).hostname

    def boom(url, timeout, user_agent):
        raise TimeoutError(f"{secret} while reading {url}")

    monkeypatch.setattr(arxiv_client, "_fetch", boom)

    event_log = _EventLogSpy()
    context, _ = _route_context(settings, event_log=event_log)
    search = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/search", "GET"
    ).endpoint

    caplog.set_level(logging.WARNING, logger="silicon_notebook_arxiv_search")
    with pytest.raises(_UserError) as error:
        search(notebook_id="nb-1", q="graph neural networks")

    assert error.value.status_code == 502
    assert secret not in error.value.detail
    assert host not in error.value.detail

    # The class name is the whole of what this plugin may say about someone
    # else's failure, and it goes to the log — never to the reader, and never
    # to the event, whose ``outcome`` whitelist is lowercase-only and would
    # drop the entire record rather than strip the name.
    assert "TimeoutError" in caplog.text
    assert secret not in caplog.text
    assert host not in caplog.text
    assert event_log.records == [
        {
            "event": "arxiv_search_failed",
            "outcome": "upstream_failed",
            "kind": "extension_plugin",
            "plugin_id": PLUGIN_ID,
        }
    ]


def test_import_route_rejects_a_non_arxiv_host_before_touching_the_port():
    """The allow-list runs *before* core's port, not after its answer.

    Ordering is the whole assertion: core's importer would authorize the caller
    either way, so this is about the shape a plugin route publishes, and a
    check that ran after the call would have already forwarded the URL.
    """

    sources = _UrlSourcesSpy()
    context, _ = _route_context(_test_settings(), url_sources=sources)
    handler = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/import", "POST"
    ).endpoint

    foreign = [
        "https://evil.example/paper.pdf",
        # A bare substring or prefix check on the raw URL would accept this
        # one — ``arxiv.org`` is a *prefix* of ``arxiv.org.evil.example``, not
        # a suffix of it.  What actually tells them apart is the suffix check
        # in ``_is_arxiv_url`` running against ``urlsplit(...).hostname``
        # (which strips everything down to the host itself) rather than
        # against the raw string.
        "https://arxiv.org.evil.example/paper.pdf",
        "ftp://arxiv.org/pdf/2401.00001v1",
        "https://arxiv.org@evil.example/paper.pdf",
    ]
    for url in foreign:
        with pytest.raises(_UserError) as error:
            handler(notebook_id="nb-1", payload={"urls": [url]})
        assert error.value.status_code == 400, url

    # One good URL beside a foreign one still refuses the whole request: a
    # partial import would leave the caller guessing which half landed.
    with pytest.raises(_UserError):
        handler(
            notebook_id="nb-1",
            payload={
                "urls": [
                    "https://arxiv.org/pdf/2401.00001v1",
                    "https://evil.example/paper.pdf",
                ]
            },
        )

    assert sources.calls == []

    # Vacuity guard: the accepted shapes really do reach the port.
    handler(
        notebook_id="nb-1",
        payload={
            "urls": [
                "https://arxiv.org/pdf/2401.00001v1",
                "https://export.arxiv.org/pdf/2401.00002v2",
            ]
        },
    )
    assert len(sources.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        # ``hostname`` lowercases the host.
        "https://ARXIV.ORG/pdf/2401.00001v1",
        # ``hostname`` drops the port.
        "https://arxiv.org:8443/pdf/2401.00001v1",
        # ``hostname`` drops a ``user@`` prefix — the mirror image of the
        # foreign-host test above, where the *host itself* was
        # ``evil.example`` and ``arxiv.org@`` was only ever the userinfo.
        # Here the host really is arxiv.org.
        "https://user@arxiv.org/pdf/2401.00001v1",
    ],
)
def test_import_route_accepts_host_shapes_that_still_resolve_to_arxiv(url):
    """The acceptance direction of hostname normalisation, not just refusal.

    The test above exercises that a *foreign* host cannot be read as arXiv's
    despite looking similar in the raw string; this is the other half — none
    of these three shapes is itself a foreign host, and normalising them (case,
    port, userinfo) must not turn a legitimate arXiv link into a refusal.
    """

    sources = _UrlSourcesSpy()
    context, _ = _route_context(_test_settings(), url_sources=sources)
    handler = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/import", "POST"
    ).endpoint

    handler(notebook_id="nb-1", payload={"urls": [url]})
    assert len(sources.calls) == 1


def test_import_route_accepts_the_configured_mirror_host():
    """The import allow-list now also accepts this deployment's own mirror.

    ``_test_settings()``'s ``base_url`` host is ``export.arxiv.example`` —
    deliberately not a ``*.arxiv.org`` subdomain — so this exercises a
    genuinely new acceptance, not one the pre-existing suffix rule already
    covered.
    """

    sources = _UrlSourcesSpy()
    context, _ = _route_context(_test_settings(), url_sources=sources)
    handler = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/import", "POST"
    ).endpoint

    handler(
        notebook_id="nb-1",
        payload={"urls": ["https://export.arxiv.example/pdf/2401.00001v1"]},
    )
    assert len(sources.calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        # A subdomain of the mirror is not the mirror: widening the addition
        # to a suffix rule would let a caller import
        # ``<mirror>.evil.example`` on the strength of a deployment's own
        # configuration — the mirror addition must stay an *exact* match.
        "https://sub.export.arxiv.example/pdf/x",
        "https://export.arxiv.example.evil.example/pdf/x",
        # The userinfo trick, mirrored against the configured host instead of
        # arxiv.org (see ``test_import_route_rejects_a_non_arxiv_host_before_touching_the_port``):
        # the *host* here is ``evil.example``, and ``export.arxiv.example@``
        # is only ever the userinfo.
        "https://export.arxiv.example@evil.example/pdf/x",
    ],
)
def test_import_route_rejects_lookalikes_of_the_configured_mirror_host(url):
    sources = _UrlSourcesSpy()
    context, _ = _route_context(_test_settings(), url_sources=sources)
    handler = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/import", "POST"
    ).endpoint

    with pytest.raises(_UserError) as error:
        handler(notebook_id="nb-1", payload={"urls": [url]})
    assert error.value.status_code == 400
    assert sources.calls == []


def test_import_route_without_configured_settings_still_accepts_plain_arxiv_links():
    """The mirror addition must not disturb the settings-less baseline.

    This route has never required the plugin to be "configured" in order to
    import a plain arxiv.org link — only ``/search`` requires settings (see
    ``test_search_route_refuses_when_the_plugin_is_unconfigured``) — so a
    ``None`` settings context (nothing to widen the allow-list with) must
    fall back to exactly the pre-existing arxiv.org-or-subdomain behaviour
    rather than erroring or rejecting.
    """

    sources = _UrlSourcesSpy()
    context, _ = _route_context(None, url_sources=sources)
    handler = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/import", "POST"
    ).endpoint

    handler(
        notebook_id="nb-1",
        payload={"urls": ["https://arxiv.org/pdf/2401.00001v1"]},
    )
    assert len(sources.calls) == 1


def test_import_route_gives_a_distinct_message_for_an_overlong_legitimate_link():
    """A legitimate-host link that is merely too long is not a foreign host.

    Both used to share one sentence ("only arXiv PDF links may be
    imported"), which tells a caller with a too-long *arxiv.org* link the
    wrong thing to fix.  The host check runs first and independently of
    length, so a foreign host is refused for what it is regardless of how
    long it happens to be.
    """

    context, _ = _route_context(_test_settings())
    handler = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/import", "POST"
    ).endpoint

    overlong = "https://arxiv.org/pdf/" + "x" * arxiv_routes.MAX_URL_CHARS
    with pytest.raises(_UserError) as overlong_error:
        handler(notebook_id="nb-1", payload={"urls": [overlong]})
    with pytest.raises(_UserError) as foreign_error:
        handler(
            notebook_id="nb-1", payload={"urls": ["https://evil.example/paper.pdf"]}
        )

    assert overlong_error.value.status_code == 400
    assert foreign_error.value.status_code == 400
    assert overlong_error.value.detail != foreign_error.value.detail
    assert foreign_error.value.detail == "只能导入 arXiv 的 PDF 链接"


def test_import_route_rejects_an_empty_or_malformed_body():
    """Two layers refuse a malformed body, and they do different jobs.

    ``[]`` and ``None`` here exercise the handler's own
    ``isinstance(payload, dict)`` guard directly, by calling ``import_papers``
    exactly the way every other test in this file does — bypassing FastAPI's
    request parsing entirely.  Over the real wire those two shapes never
    reach that guard at all: the route parameter is typed ``payload:
    dict[str, Any]``, so a JSON body that is not an object is refused by
    FastAPI's own validation (422) before the handler runs.  The `isinstance`
    check is defense in depth for a caller that reaches the handler some
    other way (a future non-HTTP entry point, a test double); it is not what
    a real HTTP client sees for these two shapes.  See
    ``test_import_route_via_http_rejects_non_dict_bodies_with_422`` below for
    the wire-accurate version of the same two shapes.
    """

    sources = _UrlSourcesSpy()
    context, _ = _route_context(_test_settings(), url_sources=sources)
    handler = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/import", "POST"
    ).endpoint

    bodies: list[Any] = [
        {},
        {"urls": []},
        {"urls": "https://arxiv.org/pdf/2401.00001v1"},
        {"urls": [""]},
        {"urls": ["   "]},
        {"urls": [None]},
        {"urls": [123]},
        # Distinct URLs, not `MAX_IMPORT_URLS + 1` repeats of one: the batch
        # ceiling below is now checked against the *deduplicated* count
        # (P2-2), so `MAX_IMPORT_URLS + 1` copies of the same URL would fold
        # to one and no longer trip it — see
        # `test_import_route_checks_the_batch_ceiling_against_the_deduplicated_count`
        # for that case on its own.
        {
            "urls": [
                f"https://arxiv.org/pdf/x{i}"
                for i in range(arxiv_routes.MAX_IMPORT_URLS + 1)
            ]
        },
        {"urls": ["https://arxiv.org/pdf/" + "x" * arxiv_routes.MAX_URL_CHARS]},
        [],
        None,
    ]
    for body in bodies:
        with pytest.raises(_UserError) as error:
            handler(notebook_id="nb-1", payload=body)
        assert error.value.status_code == 400, body

    assert sources.calls == []


def test_import_route_deduplicates_repeated_urls_before_calling_the_port():
    """A URL's second and later occurrences are dropped, silently (P2-2).

    Core's own `add_url_sources` is not content-addressed (see the module
    docstring): sending the same URL twice would create two duplicate
    sources for nothing.  Two identical entries in the same batch — a feed
    that lists the same paper twice, or a client that double-submits — is an
    ordinary shape, not a caller mistake, so the repeat is dropped rather
    than answered with a 400, and the port is only ever handed one copy.
    """

    created = (
        PluginImportedSource(
            source_id="src-1", title="Paper A", url="https://arxiv.org/pdf/2401.00001v1"
        ),
    )
    sources = _UrlSourcesSpy(created=created)
    context, _ = _route_context(_test_settings(), url_sources=sources)
    handler = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/import", "POST"
    ).endpoint

    body = handler(
        notebook_id="nb-1",
        payload={
            "urls": [
                "https://arxiv.org/pdf/2401.00001v1",
                "https://arxiv.org/pdf/2401.00002v2",
                # A third, later repeat of the first URL — proves dedup keeps
                # scanning past the first repeat rather than only catching an
                # immediately-adjacent one.
                "https://arxiv.org/pdf/2401.00001v1",
            ]
        },
    )

    assert len(sources.calls) == 1
    _, forwarded = sources.calls[0]
    # Order-preserving: the survivors keep the order they were first seen in.
    assert forwarded == [
        "https://arxiv.org/pdf/2401.00001v1",
        "https://arxiv.org/pdf/2401.00002v2",
    ]
    # A repeat is dropped, not refused — the request still succeeds.
    assert body["created"] == [
        {
            "source_id": "src-1",
            "title": "Paper A",
            "url": "https://arxiv.org/pdf/2401.00001v1",
        }
    ]


def test_import_route_checks_the_batch_ceiling_against_the_deduplicated_count():
    """`MAX_IMPORT_URLS` counts unique URLs, not raw list length (P2-2).

    A caller who names the same handful of papers more times than the cap
    must not be refused for a repetition that will not cost a second source
    — the ceiling exists to bound how many *new* sources one request can
    create, not how many times a URL may appear in the request body.
    """

    sources = _UrlSourcesSpy()
    context, _ = _route_context(_test_settings(), url_sources=sources)
    handler = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/import", "POST"
    ).endpoint

    repeated = ["https://arxiv.org/pdf/2401.00001v1"] * (
        arxiv_routes.MAX_IMPORT_URLS + 5
    )
    handler(notebook_id="nb-1", payload={"urls": repeated})

    assert len(sources.calls) == 1
    _, forwarded = sources.calls[0]
    assert forwarded == ["https://arxiv.org/pdf/2401.00001v1"]

    # Vacuity guard: the ceiling still fires once the *unique* count exceeds
    # it, so the test above is not passing because the check was removed.
    distinct_over_cap = [
        f"https://arxiv.org/pdf/x{i}" for i in range(arxiv_routes.MAX_IMPORT_URLS + 1)
    ]
    with pytest.raises(_UserError) as error:
        handler(notebook_id="nb-1", payload={"urls": distinct_over_cap})
    assert error.value.status_code == 400
    assert len(sources.calls) == 1


def test_import_route_via_http_rejects_non_dict_bodies_with_422():
    """The real over-the-wire shape of "malformed body" for a non-dict payload.

    ``payload: dict[str, Any]`` makes FastAPI treat the request body as a
    JSON object; a JSON array, ``null``, a string or a bare number never
    reaches ``import_papers`` at all — FastAPI's own request validation
    refuses it first with 422, not this plugin's ``user_error(400, ...)``.
    Only a real ``TestClient`` round trip proves that, so this is the one
    test in this file that mounts the router into an actual ``FastAPI`` app
    instead of calling the handler directly; the gates (``Depends(...)``)
    are the same no-op stand-ins ``_route_context`` builds everywhere else,
    so this is still not the real mounted-app wire (core's session guard,
    ``mount_extension_routers``) — only the shape FastAPI itself accepts onto
    a route with this parameter type.
    """

    sources = _UrlSourcesSpy()
    context, _ = _route_context(_test_settings(), url_sources=sources)
    app = FastAPI()
    app.include_router(arxiv_routes.build_router(context))
    client = TestClient(app)

    for body_bytes in (b"[]", b"null", b'"a string"', b"123"):
        response = client.post(
            "/notebooks/nb-1/import",
            content=body_bytes,
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422, body_bytes

    assert sources.calls == []


def test_the_ui_package_import_cap_matches_the_route_cap():
    """The batch ceiling is spelled twice, once per language; keep them equal.

    ``routes.py::MAX_IMPORT_URLS`` is the authoritative bound — it answers an
    over-long batch with a 400 — and the TypeScript constant only exists so
    the panel can disable its button and say why instead of letting someone
    tick thirty papers and learn the limit from an error banner.

    Drift is safe in only one direction (a panel stricter than the server is
    merely conservative; a panel looser than the server surfaces the 400
    verbatim), which is exactly why it would go unnoticed.  Five lines of
    regex is cheap enough that "grep for the other one" does not have to be
    the whole mechanism.  This lives in the G1 file rather than the
    end-to-end one on purpose: it needs no application, and a number that
    can silently drift should be checked on every pull request rather than
    once a night.
    """

    source = (
        _PLUGIN_ROOT / "ui" / "arxiv-search" / "search-panel-model.ts"
    ).read_text(encoding="utf-8")
    match = re.search(r"export const MAX_IMPORT_URLS = (\d+);", source)
    assert match is not None, (
        "the UI package's source no longer matches the "
        "`export const MAX_IMPORT_URLS = <n>;` shape — the export may still "
        "be there, just not in the exact form this regex expects"
    )
    assert int(match.group(1)) == arxiv_routes.MAX_IMPORT_URLS


def test_the_ui_package_query_term_cap_matches_the_route_cap():
    """The query-term ceiling is spelled twice, once per language — same
    shape as :func:`test_the_ui_package_import_cap_matches_the_route_cap`
    above, for the same reason: ``client.py::MAX_QUERY_TERMS`` (the route
    now rejects a query over this bound explicitly, before ever calling
    ``build_query_url``) is the authoritative number, and the TypeScript
    constant only exists so the panel can disable its submit button and say
    why instead of letting someone submit a ten-word query and learn the
    limit from an error banner.
    """

    source = (
        _PLUGIN_ROOT / "ui" / "arxiv-search" / "search-panel-model.ts"
    ).read_text(encoding="utf-8")
    match = re.search(r"export const MAX_QUERY_TERMS = (\d+);", source)
    assert match is not None, (
        "the UI package's source no longer matches the "
        "`export const MAX_QUERY_TERMS = <n>;` shape — the export may still "
        "be there, just not in the exact form this regex expects"
    )
    assert int(match.group(1)) == arxiv_client.MAX_QUERY_TERMS


def test_the_ui_package_query_char_cap_matches_the_route_cap():
    """The query-length ceiling is spelled twice, once per language (P2-3).

    Same shape as :func:`test_the_ui_package_query_term_cap_matches_the_route_cap`
    above, for the same reason: ``routes.py::QUERY_MAX_CHARS`` is the
    authoritative bound — the route answers an over-length query with a 400,
    checked before the whitespace-split word count even runs — and the
    TypeScript constant only exists so the panel can disable its submit
    button and say why.
    """

    source = (
        _PLUGIN_ROOT / "ui" / "arxiv-search" / "search-panel-model.ts"
    ).read_text(encoding="utf-8")
    match = re.search(r"export const QUERY_MAX_CHARS = (\d+);", source)
    assert match is not None, (
        "the UI package's source no longer matches the "
        "`export const QUERY_MAX_CHARS = <n>;` shape — the export may still "
        "be there, just not in the exact form this regex expects"
    )
    assert int(match.group(1)) == arxiv_routes.QUERY_MAX_CHARS


def test_import_route_emits_only_whitelisted_event_fields():
    created = (
        PluginImportedSource(
            source_id="src-1", title="2401.00001v1", url="https://arxiv.org/pdf/a"
        ),
    )
    rejected = (PluginRejectedUrl(url="https://arxiv.org/pdf/b", reason="重复的链接"),)
    sources = _UrlSourcesSpy(created=created, rejected=rejected)
    event_log = _EventLogSpy()
    context, _ = _route_context(
        _test_settings(), url_sources=sources, event_log=event_log
    )
    handler = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/import", "POST"
    ).endpoint

    body = handler(
        notebook_id="nb-1",
        payload={
            "urls": [
                "https://arxiv.org/pdf/2401.00001v1",
                "https://arxiv.org/pdf/2401.00002v2",
            ]
        },
    )

    assert body["created"] == [
        {"source_id": "src-1", "title": "2401.00001v1", "url": "https://arxiv.org/pdf/a"}
    ]
    assert body["rejected"] == [
        {"url": "https://arxiv.org/pdf/b", "reason": "重复的链接"}
    ]
    # The payload passed through core's real sanitizer, so this is "core kept
    # it", not "our stub echoed it".
    assert event_log.records == [
        {
            "event": "arxiv_urls_imported",
            "count": 1,
            "kind": "extension_plugin",
            "plugin_id": PLUGIN_ID,
        }
    ]

    # Vacuity guard: the same emitter really does drop a payload with an extra
    # key, so the assertion above is about the payload rather than the seam.
    context.emit_event({"event": "arxiv_urls_imported", "notebook_id": "nb-1"})
    assert len(event_log.records) == 1


def test_search_route_refuses_when_the_plugin_is_unconfigured(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )

    context, _ = _route_context(None)
    search = _route(
        arxiv_routes.build_router(context), "/notebooks/{notebook_id}/search", "GET"
    ).endpoint

    with pytest.raises(_UserError) as error:
        search(notebook_id="nb-1", q="graph")
    assert error.value.status_code == 503
    assert calls == []


# --------------------------------------------------------------------------
# Gap consultation
# --------------------------------------------------------------------------


class _Cancelled:
    def is_set(self) -> bool:
        return True

    def raise_if_cancelled(self) -> None:
        raise RuntimeError("cancelled")


def _consult_context(
    question: str = "how does retrieval augmented generation ground answers",
    gaps: tuple[str, ...] = (),
    *,
    max_suggestions: int = 3,
    cancellation: object | None = None,
    deadline_offset: float = 30.0,
) -> GapConsultExtensionContext:
    return GapConsultExtensionContext(
        query=GapConsultQuery(
            question=question, gaps=gaps, max_suggestions=max_suggestions
        ),
        cancellation=cancellation,
        max_suggestions=max_suggestions,
        deadline_monotonic=time.monotonic() + deadline_offset,
    )


def _contributor(**overrides):
    bundle = _configured_bundle(consult_enabled=True, **overrides)
    return bundle.contributor


def test_consult_is_unavailable_when_disabled_even_if_the_probe_is_bypassed():
    # Defence in depth: the probe already refused, so reaching ``consult`` at
    # all means a host stopped consulting it.  That must not become an
    # outbound request.
    for contributor in (
        ArxivSearchBundle(BUNDLE.manifest).contributor,
        _configured_bundle().contributor,
    ):
        result = contributor.consult(_consult_context())
        assert result.status is ExtensionResultStatus.UNAVAILABLE
        assert result.items == ()
        assert result.failure.code == "consult_disabled"


def test_consult_returns_empty_when_cancelled_rather_than_raising(monkeypatch):
    """Cancellation returns; it never raises.

    Core wraps the whole call in ``except BaseException`` and files anything
    thrown as ``gap_consult_failed`` — so ``raise_if_cancelled()`` here would
    relabel "the user pressed stop" as "the plugin broke".  Core re-reads
    cancellation on every 50 ms join slice, so returning is sufficient.
    """

    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )

    result = _contributor().consult(_consult_context(cancellation=_Cancelled()))

    assert result.items == ()
    assert result.failure.code == "arxiv_cancelled"
    assert calls == []


def test_consult_makes_no_request_when_no_suggestion_slots_remain(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )

    result = _contributor().consult(_consult_context(max_suggestions=0))

    assert result.failure.code == "arxiv_no_suggestion_budget"
    assert calls == []
    assert arxiv_client._LAST_REQUEST_AT is None


def test_consult_returns_unavailable_without_fetching_when_no_latin_terms(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )

    result = _contributor().consult(
        _consult_context(question="这份资料里没有提到的部分是什么？", gaps=("检索增强",))
    )

    assert result.failure.code == "arxiv_no_latin_terms"
    assert calls == []


def test_consult_terms_come_from_the_question_and_every_gap_phrase(monkeypatch):
    seen: list[str] = []

    def stub(url, timeout, user_agent):
        seen.append(url)
        return b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    monkeypatch.setattr(arxiv_client, "_fetch", stub)

    # The question is entirely Chinese; the gap phrase carries the term of art.
    # Scanning only the question would throw this case away, which is the
    # commonest shape in a Chinese-language notebook.
    result = _contributor().consult(
        _consult_context(question="这个方法的边界在哪里？", gaps=("shaping loss bounds",))
    )

    assert result.status is ExtensionResultStatus.AVAILABLE
    query = parse_qs(urlsplit(seen[0]).query)["search_query"][0]
    assert "all:shaping" in query
    assert "all:bounds" in query
    # ``loss`` is not a stop word; ``the`` in a Latin question would be.
    assert "all:loss" in query


def test_consult_honours_the_smaller_of_context_and_settings_caps(monkeypatch):
    seen: list[str] = []

    def stub(url, timeout, user_agent):
        seen.append(url)
        return b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'

    monkeypatch.setattr(arxiv_client, "_fetch", stub)

    _contributor(consult_max_suggestions=5).consult(
        _consult_context(max_suggestions=2)
    )
    _contributor(consult_max_suggestions=1).consult(
        _consult_context(max_suggestions=5)
    )

    assert [parse_qs(urlsplit(url).query)["max_results"][0] for url in seen] == [
        "2",
        "1",
    ]


def test_consult_maps_papers_to_pdf_direct_links(monkeypatch, sample_feed):
    monkeypatch.setattr(arxiv_client, "_fetch", lambda *args: sample_feed)

    result = _contributor().consult(_consult_context())

    assert result.status is ExtensionResultStatus.AVAILABLE
    assert len(result.items) == 2
    for suggestion in result.items:
        # SOP §3.6: only PDF direct links.  Core does not fetch the URL to find
        # out what it is, and the import endpoint probes exactly what it is
        # given rather than hunting for a link on a landing page.
        assert suggestion.url.startswith("https://arxiv.org/pdf/")
        assert suggestion.source_label == "arXiv"
        assert suggestion.title


def test_consult_truncates_title_and_summary_to_cores_gap_suggestion_limits(
    monkeypatch,
):
    """`.consult` owns the gap-suggestion-specific cut now, not `.atom` (P2-1).

    `.atom`'s own ``TITLE_MAX_CHARS``/``SUMMARY_MAX_CHARS`` are now a
    generous, display-oriented in-memory ceiling (500/4000) and are no
    longer pinned to core's own ``GAP_SUGGESTION_TITLE_MAX_CHARS``/
    ``GAP_SUGGESTION_SUMMARY_MAX_CHARS`` (200/400). A record whose title and
    summary sail past core's limits while staying comfortably under
    `.atom`'s own proves the cut a suggestion actually gets happens in
    `.consult`, not merely because the parser already trimmed it upstream.
    """

    long_title = " ".join(["term"] * 60)  # 299 chars: > core's 200, < atom's 500
    long_summary = " ".join(["term"] * 600)  # 2999 chars: > core's 400, < atom's 4000
    assert GAP_SUGGESTION_TITLE_MAX_CHARS < len(long_title) < arxiv_atom.TITLE_MAX_CHARS
    assert (
        GAP_SUGGESTION_SUMMARY_MAX_CHARS
        < len(long_summary)
        < arxiv_atom.SUMMARY_MAX_CHARS
    )

    feed = _feed_with(long_title, long_summary)
    monkeypatch.setattr(arxiv_client, "_fetch", lambda *args: feed)

    # The parser itself must not already have cut these — otherwise the
    # assertions below would prove nothing about where the truncation
    # actually happens.
    parsed = arxiv_atom.parse_atom(feed, limit=1)[0]
    assert parsed.title == long_title
    assert parsed.summary == long_summary

    result = _contributor().consult(_consult_context())

    assert result.status is ExtensionResultStatus.AVAILABLE
    assert len(result.items) == 1
    suggestion = result.items[0]
    assert len(suggestion.title) == GAP_SUGGESTION_TITLE_MAX_CHARS
    assert suggestion.title == long_title[:GAP_SUGGESTION_TITLE_MAX_CHARS]
    assert len(suggestion.summary) == GAP_SUGGESTION_SUMMARY_MAX_CHARS
    assert suggestion.summary == long_summary[:GAP_SUGGESTION_SUMMARY_MAX_CHARS]


def test_consult_drops_a_suggestion_from_a_foreign_host(monkeypatch):
    feed = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom">'
        "<entry><id>http://arxiv.org/abs/2401.10001v1</id>"
        "<title>Redirected elsewhere</title>"
        '<link title="pdf" href="https://evil.example/paper.pdf"/>'
        "</entry>"
        "<entry><id>http://arxiv.org/abs/2401.10002v1</id>"
        "<title>Ordinary paper</title></entry>"
        "</feed>"
    ).encode("utf-8")
    monkeypatch.setattr(arxiv_client, "_fetch", lambda *args: feed)

    result = _contributor().consult(_consult_context())

    # Policy lives in the policy layer: the parser reports what the feed said,
    # and this is where the deployment decides which hosts may reach a reader.
    assert [suggestion.url for suggestion in result.items] == [
        "https://arxiv.org/pdf/2401.10002v1"
    ]


def test_consult_returns_unavailable_when_the_deadline_has_already_passed(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )

    result = _contributor().consult(_consult_context(deadline_offset=-1.0))

    assert result.failure.code == "arxiv_budget_too_small"
    assert calls == []


def test_consult_refuses_a_deadline_that_cannot_fit_the_worst_case(monkeypatch):
    """The gate is the *worst* case, not "is there any time left".

    ``acquire_slot`` may sleep up to the budget it is handed and the HTTP call
    may then take a full ``timeout_seconds``, so a budget of "everything that
    is left" answers exactly on the deadline — and core's join loop re-reads
    the deadline on every 50 ms slice, so an answer that lands on it is read by
    nobody.  Refusing early costs nothing; answering late costs a politeness
    slot and a round trip for a result nobody sees.
    """

    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )

    contributor = _contributor(
        politeness_interval_seconds=3.0, timeout_seconds=10.0
    )
    floor = 3.0 + 10.0 + arxiv_consult.CONSULT_RETURN_MARGIN_SECONDS

    # Core's own default gap-consult deadline is 4 seconds: with arXiv's
    # politeness terms that is structurally too small, so this contributor says
    # so instead of starting a request it cannot finish.
    result = contributor.consult(_consult_context(deadline_offset=4.0))
    assert result.failure.code == "arxiv_budget_too_small"
    assert calls == []

    # Vacuity guard: past the floor it really does go.
    monkeypatch.setattr(
        arxiv_client,
        "_fetch",
        lambda *args: b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>',
    )
    monkeypatch.setattr(arxiv_client, "acquire_slot", lambda *args: True)
    monkeypatch.setattr(arxiv_client, "release_slot", lambda: None)
    opened = contributor.consult(_consult_context(deadline_offset=floor + 1.0))
    assert opened.status is ExtensionResultStatus.AVAILABLE


def test_consult_leaves_the_transport_a_budget_that_ends_before_the_deadline(
    monkeypatch,
):
    budgets: list[float] = []

    def spy(interval, budget):
        budgets.append(budget)
        return True

    monkeypatch.setattr(arxiv_client, "acquire_slot", spy)
    monkeypatch.setattr(arxiv_client, "release_slot", lambda: None)
    monkeypatch.setattr(
        arxiv_client,
        "_fetch",
        lambda *args: b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>',
    )

    contributor = _contributor(politeness_interval_seconds=2.0, timeout_seconds=3.0)
    contributor.consult(_consult_context(deadline_offset=20.0))

    # budget + timeout + margin must still land inside the deadline, so the
    # budget handed to the throttle is the remainder after both are reserved.
    assert budgets
    assert budgets[0] <= 20.0 - 3.0 - arxiv_consult.CONSULT_RETURN_MARGIN_SECONDS
    # …and it is still large enough for a full politeness wait, which is what
    # the floor check above guarantees.
    assert budgets[0] >= 2.0


def test_consult_swallows_upstream_errors_into_a_stable_code(monkeypatch):
    secret = "SECRET-CONSULT-DETAIL"

    def boom(url, timeout, user_agent):
        raise TimeoutError(f"{secret} at {url}")

    monkeypatch.setattr(arxiv_client, "_fetch", boom)

    result = _contributor().consult(_consult_context())

    assert result.status is ExtensionResultStatus.UNAVAILABLE
    assert result.items == ()
    assert result.failure.code == "arxiv_upstream_failed"
    assert secret not in repr(result)
    assert "arxiv.example" not in repr(result)


def test_consult_maps_a_throttle_refusal_to_its_own_code(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        arxiv_client, "_fetch", lambda *args: calls.append(args) or b""
    )
    monkeypatch.setattr(arxiv_client, "acquire_slot", lambda *args: False)

    result = _contributor().consult(_consult_context())

    # Distinct from ``arxiv_upstream_failed``: nothing failed, and an operator
    # reading the event should see "we were being polite", not "arXiv broke".
    assert result.failure.code == "arxiv_throttled"
    assert calls == []
