"""Deployment-configurable settings for the arXiv sample plugin.

Core computes the accepted key set from ``model_fields`` itself, so
``extra="forbid"`` here is a second lock on the same door rather than the door.
It is kept because this model is also usable standalone (a plugin author can
validate a candidate TOML table without the backend present).

Two absences are deliberate:

* **No ``api_key_env``.**  The arXiv API takes no credential.  Inventing an
  unused key field would make the sample teach a shape it never exercises; the
  credential convention lives in the deployment-extensions SOP instead, and an
  in-house variant is where it actually gets used.
* **No client/connection object.**  ``configure`` may only store values — it
  runs inside startup composition, before the registry freezes and before the
  service is ready, so it must not start a thread or open a connection.

``politeness_interval_seconds`` defaults to 3.0 because arXiv's API terms ask
callers to leave at least three seconds between requests.  Lowering it is a
deployment's own decision against its own agreement with arXiv; ``0`` is
accepted so tests and mirrors are not forced to sleep.

:func:`search_kwargs` at the bottom is the one place deployment settings are
mapped onto the transport's keyword arguments, for all three of its callers
(the interactive route, gap consultation and the reflect search action).  It belongs to the *adapter*
half of this package (settings / routes / consult / bundle), not to the
replaceable arXiv half: :mod:`.client` deliberately knows nothing about this
model, so something above it has to name the transport's parameters, and that
something must be exactly one function.  Two call sites spelling the same
mapping by hand is how a plugin ends up sending its default user agent from one
route and its configured one from another.

:func:`egress_allowed` at the bottom is the same shape of argument applied to
a different value: ``.routes``, ``.consult`` and ``.reflect_search`` each
receive a URL parsed out of an untrusted upstream Atom feed (see :mod:`.atom`)
and each has to decide whether that URL may reach a person before it does — as
a search result's ``pdf_url``, as an unbidden gap-consult suggestion, or as a
citable external-evidence link inside an answer.  It lives here
rather than in :mod:`.atom` for the same reason ``search_kwargs`` does: the
parser is the layer an in-house variant replaces wholesale, so teaching it a
hard-coded arxiv.org policy would mean the replacement inherits a policy that
is wrong for it.  Policy belongs to the policy layer.

:func:`deadline_budget` at the bottom is the third member of that family, and
the one with two contributors rather than two routes behind it: both
:mod:`.consult` and :mod:`.reflect_search` run under a core host's hard
wall-clock deadline, and both have to answer the same two questions before
dialling — "does this deployment's *worst* case still fit inside what is
left?" and "then how much of it may the throttle spend?".  The two answers are
one piece of arithmetic (see the function), and a second hand-written copy of
it is how one contributor ends up answering exactly *on* a deadline nobody is
still reading.

:func:`mirror_host` is the host-extraction primitive :func:`egress_allowed`
is built from, and ``.routes``'s import allow-list (``_is_arxiv_url``) shares
it too — the deployment's own configured mirror should be importable by URL,
not just quotable in a gap-consult suggestion.  Sharing the primitive is not
the same as sharing the policy: the import route still layers its own
arxiv.org-or-subdomain rule on top (a link a person clicked on themselves),
so the mirror host is one *exact*-match addition to a *wider* base rule
there, not the *whole* rule the way it is here.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Control characters a caller could use to smuggle a second header line into
# the outbound request if ``user_agent`` were sent verbatim — CR and LF are
# the classic request-splitting pair, but every C0 control and DEL are
# refused on the same footing since none of them belongs in a header value.
_CONTROL_CHARS = frozenset(chr(code) for code in range(0x20)) | {"\x7f"}


class ArxivSearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "https://export.arxiv.org/api/query"
    max_results: int = Field(10, ge=1, le=20)
    timeout_seconds: float = Field(10.0, gt=0, le=60)
    politeness_interval_seconds: float = Field(3.0, ge=0, le=30)
    user_agent: str = (
        "silicon-notebook-arxiv-sample/0.1 (+https://arxiv.org/help/api)"
    )
    # Installing the plugin is not the same as agreeing to send question-derived
    # keywords to arxiv.org on every thin answer.  Gap consultation stays off
    # until a deployment says otherwise.
    consult_enabled: bool = False
    consult_max_suggestions: int = Field(3, ge=1, le=5)
    # The same "installing is not consenting" rule, for the other outbound
    # contribution: the reflect action this plugin lends the retrieval agent
    # sends the model's own English keywords to arxiv.org from inside a live
    # answer.  A deployment that agreed to gap consultation has not thereby
    # agreed to this one — they leave on different triggers, at different
    # moments, and what this one brings back is quoted in the answer — so it
    # is a second, separate yes rather than a shared flag.
    reflect_search_enabled: bool = False
    reflect_search_max_items: int = Field(3, ge=1, le=5)

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        """Reject anything that is not a plain ``http(s)://host/path`` URL.

        ``base_url`` is a deployment configuration value, not user input, but
        it still crosses a trust boundary: it is handed straight to
        :func:`urllib.request.urlopen` by :mod:`.client`.  This mirrors the
        core project's fail-fast validation of ``MCP_PUBLIC_URL`` — an
        absolute ``http(s)`` URL with no query string and no fragment — rather
        than trusting a TOML author not to paste a ``file://`` path or a
        stray ``#fragment``. Query strings are rejected too:
        :func:`~silicon_notebook_arxiv_search.client.build_query_url` decides
        the separator (``?`` vs ``&``) from whether one is already present,
        so a ``base_url`` carrying its own query string would silently change
        that decision instead of failing loudly here.
        """
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("base_url must be an absolute http:// or https:// URL")
        if not parsed.netloc:
            raise ValueError("base_url must include a host")
        if parsed.query:
            raise ValueError("base_url must not include a query string")
        if parsed.fragment:
            raise ValueError("base_url must not include a fragment")
        return value

    @field_validator("user_agent")
    @classmethod
    def _validate_user_agent(cls, value: str) -> str:
        """Reject a blank value or one carrying a control character.

        ``user_agent`` is a deployment configuration value, not user input,
        but it crosses the same kind of trust boundary ``base_url`` does
        above: :mod:`.client` hands it straight to
        ``urllib.request.Request(..., headers={"User-Agent": user_agent})``.
        Fail-fast for the same reason — a TOML author's typo should not
        become a silent runtime shape — rather than trusting the value.  A
        bare CR or LF is the classic HTTP request-splitting pair (a second
        header line smuggled in after the first), so every C0 control
        character and DEL are refused on the same footing: none of them
        belongs in a header value, and ``urllib`` does not itself reject one.
        """
        if not value.strip():
            raise ValueError("user_agent must not be blank")
        if any(char in _CONTROL_CHARS for char in value):
            raise ValueError("user_agent must not contain control characters")
        # http.client encodes header values as Latin-1 at request time; a
        # value it cannot encode (Chinese text, emoji, …) would turn EVERY
        # search into the fixed 502 instead of failing here at startup —
        # the exact silent-runtime-shape this validator exists to prevent
        # (codex #596 R5).
        try:
            value.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "user_agent must be Latin-1 encodable (HTTP header values"
                " are sent as Latin-1)"
            ) from exc
        return value


def search_kwargs(
    settings: ArxivSearchSettings,
    *,
    limit: int,
    budget_seconds: float,
    start: int = 0,
) -> dict[str, object]:
    """Map deployment settings onto :func:`.client.search`'s keyword arguments.

    All three callers — the interactive search route, the gap-consult
    contributor and the reflect search action — go through here.  The three
    per-call values (``limit``, ``budget_seconds`` and ``start``) are arguments
    rather than settings because they are those callers' *only* legitimate
    difference: how many records this call wants,
    how long it may spend, and where in the result set it starts.  Everything
    else about how this deployment talks to arXiv is settings, and a call site
    that reached past this function to restate one of them would be declaring
    its own endpoint or its own user agent.

    ``fetch`` is deliberately absent: it is a test seam on the transport, not a
    deployment setting, so production callers never pass one.
    """
    return {
        "base_url": settings.base_url,
        "limit": limit,
        "budget_seconds": budget_seconds,
        "timeout_seconds": settings.timeout_seconds,
        "politeness_interval_seconds": settings.politeness_interval_seconds,
        "user_agent": settings.user_agent,
        "start": start,
    }


# Everything between "the response bytes arrived" and "the host read the
# return value": parsing the feed, mapping it, and the up-to-50 ms slice a
# core host's join loop is sleeping in when the worker finishes.  Shared by
# both outbound contributions because both are joined by the same shape of
# loop; it is not a deployment setting, so it is a constant here rather than a
# key in the model above.
RETURN_MARGIN_SECONDS = 0.25


def deadline_budget(
    settings: ArxivSearchSettings, deadline_monotonic: float, *, now: float
) -> float | None:
    """Seconds the throttle may spend, or ``None`` when the worst case cannot fit.

    **This is the step that is easy to get wrong**, which is why both
    contributors share one copy of it.  ``acquire_slot`` may sleep up to the
    budget it is given, and the HTTP call may then take up to
    ``timeout_seconds``.  A budget of "everything that is left" therefore
    returns an answer exactly *on* the deadline — and a core host's join loop
    re-reads that deadline on every 50 ms slice, so an answer landing on it is
    read by nobody.  So the gate refuses unless a *worst* case fits (a full
    politeness interval, a full timeout, and the return margin), and the
    budget then subtracts the timeout and the margin back out.  The two agree
    by construction: refuse when ``remaining < politeness + timeout + margin``,
    hand over ``remaining - timeout - margin``, which is therefore never less
    than one full politeness interval.

    ``now`` is passed in rather than read here so the caller reads its clock
    once, next to the deadline it was handed.
    """

    remaining = deadline_monotonic - now
    floor = (
        settings.politeness_interval_seconds
        + settings.timeout_seconds
        + RETURN_MARGIN_SECONDS
    )
    if remaining < floor:
        return None
    return remaining - settings.timeout_seconds - RETURN_MARGIN_SECONDS


def mirror_host(base_url: str) -> str:
    """The lowercase host ``base_url`` resolves to, or ``""`` if unparsable.

    Extracted so nobody hand-rolls a second ``urlsplit(base_url).hostname``.
    Both :func:`egress_allowed` below and :mod:`.routes`'s import allow-list
    (``_is_arxiv_url``) need "what host is this deployment's configured
    mirror" as a primitive — the former to widen its exact-match egress set
    by one host, the latter to widen its own arxiv.org-or-subdomain rule by
    the same one. They stay two different *policies* (an exact-match set
    versus a suffix rule with one exact addition) built from one shared
    host-extraction convention, not one policy reused twice — see
    ``routes.py::_is_arxiv_url`` for why the two must not collapse into the
    same acceptance surface.
    """
    try:
        return urlsplit(base_url).hostname or ""
    except ValueError:
        return ""


# Egress hosts a URL parsed out of an untrusted upstream feed may point at
# before this plugin shows it to a person — arXiv's own hosts, or the
# deployment's own configured mirror.  Deliberately *narrower* than the
# import route's subdomain rule in ``.routes`` (which accepts ``*.arxiv.org``
# for a link a person picked off a result page they themselves asked for):
# these two call sites both receive a value neither the caller nor the
# reader chose, so the host it may carry is spelled out rather than pattern
# matched.
_EGRESS_HOSTS = frozenset({"arxiv.org", "export.arxiv.org"})


def egress_allowed(url: str, base_url: str) -> bool:
    """True when ``url``'s host is arXiv's, or the deployment's own mirror.

    Shared by :mod:`.routes` (a search result's ``pdf_url``), :mod:`.consult`
    (a gap-consult suggestion's ``url``) and :mod:`.reflect_search` (an
    external-evidence item's ``url``): all three values come from the same
    untrusted upstream feed parser (:mod:`.atom`), and all three call sites
    need the same answer to "may this reach a reader", even though what they
    do with a ``False`` differs — the first falls back to an id-derived link,
    the other two drop the record outright.
    """

    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    if not host:
        return False
    return host in _EGRESS_HOSTS or host == mirror_host(base_url)
