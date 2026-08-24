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
mapped onto the transport's keyword arguments.  It belongs to the *adapter*
half of this package (settings / routes / consult / bundle), not to the
replaceable arXiv half: :mod:`.client` deliberately knows nothing about this
model, so something above it has to name the transport's parameters, and that
something must be exactly one function.  Two call sites spelling the same
mapping by hand is how a plugin ends up sending its default user agent from one
route and its configured one from another.

:func:`egress_allowed` at the bottom is the same shape of argument applied to
a different value: both ``.routes`` and ``.consult`` receive a URL parsed out
of an untrusted upstream Atom feed (see :mod:`.atom`) and both have to decide
whether that URL may reach a person before it does — one as a search result's
``pdf_url``, the other as an unbidden gap-consult suggestion.  It lives here
rather than in :mod:`.atom` for the same reason ``search_kwargs`` does: the
parser is the layer an in-house variant replaces wholesale, so teaching it a
hard-coded arxiv.org policy would mean the replacement inherits a policy that
is wrong for it.  Policy belongs to the policy layer.
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
        return value


def search_kwargs(
    settings: ArxivSearchSettings,
    *,
    limit: int,
    budget_seconds: float,
    start: int = 0,
) -> dict[str, object]:
    """Map deployment settings onto :func:`.client.search`'s keyword arguments.

    Both callers — the interactive search route and the gap-consult contributor
    — go through here.  The three per-call values (``limit``, ``budget_seconds``
    and ``start``) are arguments rather than settings because they are the two
    callers' *only* legitimate difference: how many records this call wants,
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

    Shared by :mod:`.routes` (a search result's ``pdf_url``) and
    :mod:`.consult` (a gap-consult suggestion's ``url``): both values come
    from the same untrusted upstream feed parser (:mod:`.atom`), and both
    call sites need the same answer to "may this reach a reader", even
    though what they do with a ``False`` differs — one falls back to an
    id-derived link, the other drops the suggestion outright.
    """

    try:
        parsed = urlsplit(url)
        base = urlsplit(base_url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname or ""
    if not host:
        return False
    return host in _EGRESS_HOSTS or host == (base.hostname or "")
