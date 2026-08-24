"""Pure-layer tests for the arXiv sample deployment plugin (X9 PR-B).

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

This file covers the layers that have no Silicon Notebook dependency at all —
settings validation, Atom parsing, and the politeness throttle.  The bundle,
routes and gap-consult contributor are covered by their own batch.
"""
from __future__ import annotations

import ast
import sys
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_SRC = _REPO_ROOT / "examples" / "extensions" / "arxiv-search" / "src"
if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))

from silicon_notebook_arxiv_search import atom as arxiv_atom  # noqa: E402
from silicon_notebook_arxiv_search import client as arxiv_client  # noqa: E402
from silicon_notebook_arxiv_search.settings import (  # noqa: E402
    ArxivSearchSettings,
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

    long_title = "title " * 200
    long_summary = "summary " * 200
    truncated = arxiv_atom.parse_atom(
        _feed_with(long_title, long_summary), limit=1
    )[0]

    assert len(truncated.title) == arxiv_atom.TITLE_MAX_CHARS
    assert len(truncated.summary) == arxiv_atom.SUMMARY_MAX_CHARS
    assert truncated.title == " ".join(long_title.split())[
        : arxiv_atom.TITLE_MAX_CHARS
    ]


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
