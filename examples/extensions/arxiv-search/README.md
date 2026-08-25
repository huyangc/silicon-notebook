# arXiv Search — Sample Deployment Extension

[中文](./README_zh.md)

This is the repository's first **shipped-off** sample deployment plugin: a
runnable proof that "clean checkout + configuration only = enabled, zero
patches" actually works, and a companion to
[`docs/deployment-extensions-sop.md`](../../../docs/deployment-extensions-sop.md)
(the deployment-extensions SOP). It is off in every default checkout. Turning
it on is entirely a deployment-side decision: point `EXTENSIONS_CONFIG` at a
TOML file that names it, and point `SILICON_NOTEBOOK_UI_PLUGINS` at its UI
package.

This document is written for the operator who enables it, not for someone
extending the sample's own code — for that, read the source docstrings and
the SOP.

## 0. UI sample shape

The sample panel now uses the shared UI kit exported from
`frontend/features/extension-sdk/ui.tsx` for its content as well as its modal
shell. Concretely, the search row, result list, action row, alerts and empty
state all come from the SDK layer; the package still ships **no CSS of its
own** and still uses **no inline colour styling**. That split is deliberate:
deployment plugins keep passing structure and copy, while the repository-owned
SDK keeps the visual contract aligned with core modal surfaces.

## 1. Three steps to enable it

1. **Install the Python package into the backend's interpreter.** Either
   `pip install -e examples/extensions/arxiv-search` into the environment
   `PYTHON_BIN` points at, or put its `src/` directory on `PYTHONPATH`. The
   package name is `silicon-notebook-arxiv-search`; the importable module is
   `silicon_notebook_arxiv_search`.
2. **Copy and edit [`extensions.example.toml`](./extensions.example.toml) outside the checkout**, then point
   `EXTENSIONS_CONFIG` at your copy (e.g.
   `EXTENSIONS_CONFIG=/etc/silicon-notebook/extensions.toml`). Nothing loads
   this file automatically — an unmodified checkout ships no default that
   enables the plugin.
3. **Point `SILICON_NOTEBOOK_UI_PLUGINS` at `ui/arxiv-search`** (an absolute
   path, e.g. `/path/to/examples/extensions/arxiv-search/ui/arxiv-search`),
   then rebuild the frontend (`npm run build`, or let `npm run start`'s
   `prebuild` sync it for you).

Both variables must be set **before** the process starts — plugin topology is
frozen at startup composition time. There is no hot reload: after changing
either the TOML or `SILICON_NOTEBOOK_UI_PLUGINS`, restart the backend and
rebuild/restart the frontend.

## 2. Settings table

All keys are optional; core computes the accepted key set from the settings
model itself, so a misspelled key in the deployment TOML is a startup
failure, not a silently ignored line.

| Key | Default | Range / shape |
| --- | --- | --- |
| `base_url` | `https://export.arxiv.org/api/query` | Absolute `http(s)` URL, no query string, no fragment. Fail-fast at startup otherwise. |
| `max_results` | `10` | Integer, `1`–`20` |
| `timeout_seconds` | `10.0` | `0 < x ≤ 60` |
| `politeness_interval_seconds` | `3.0` | `0 ≤ x ≤ 30` |
| `user_agent` | `silicon-notebook-arxiv-sample/0.1 (+https://arxiv.org/help/api)` | Non-empty, no control characters. Fail-fast at startup otherwise. |
| `consult_enabled` | `false` | Boolean |
| `consult_max_suggestions` | `3` | Integer, `1`–`5` |

`base_url` and `user_agent` are validated at startup (absolute `http(s)`
with no query/fragment; non-blank with no control characters) — both values
cross a trust boundary into `urllib`, so a typo in the TOML fails loudly
rather than becoming a silent runtime shape.

The `politeness_interval_seconds` default of 3.0 seconds comes from arXiv's
own API terms of use, which ask callers to leave at least three seconds
between requests. Lowering it is a deployment's own decision against its own
agreement with arXiv; `0` is accepted so tests and mirrors are not forced to
sleep.

## 3. Plugin-private numeric limits

These numbers are **deliberately not** registered in
`docs/product-and-api.md`/`_zh.md` (that pair only registers core numeric
limits) — they belong to this plugin, and this README is where they are
registered instead.

| Constant | Value | Module | Notes |
| --- | --- | --- | --- |
| `TITLE_MAX_CHARS` | 500 | `atom.py` | Display ceiling, not core's limit — see note below |
| `SUMMARY_MAX_CHARS` | 4000 | `atom.py` | Display ceiling, not core's limit — see note below |
| `AUTHOR_MAX_CHARS` | 80 | `atom.py` | |
| `PUBLISHED_MAX_CHARS` | 40 | `atom.py` | |
| `MAX_AUTHORS` | 20 | `atom.py` | An in-memory/display ceiling on `authors`, not a silent drop — see below |
| `ARXIV_ID_MAX_CHARS` | 64 | `atom.py` | |
| `MAX_RESPONSE_BYTES` | 1 MiB (1024×1024) | `client.py` | Bounds network cost, not memory (see §5) |
| `MAX_QUERY_TERMS` | 8 | `client.py` | See "silent term drop" below |
| `QUERY_MAX_CHARS` | 200 | `routes.py` | Interactive `/search` query length |
| `MAX_IMPORT_URLS` | 20 | `routes.py` | Per `/import` request |
| `MAX_URL_CHARS` | 2048 | `routes.py` | Per URL in an import batch |
| `START_MAX` | 10,000 | `routes.py` | Paging ceiling |
| `CONSULT_RETURN_MARGIN_SECONDS` | 0.25 | `consult.py` | See §4.1 |

`TITLE_MAX_CHARS`/`SUMMARY_MAX_CHARS` used to be pinned to the same values as
core's own `GAP_SUGGESTION_TITLE_MAX_CHARS`/`GAP_SUGGESTION_SUMMARY_MAX_CHARS`
(200/400) — **that alignment is retired.** These two constants are a
display-oriented in-memory ceiling for the interactive `/search` results
page (a person reading an abstract), not the gap-suggestion limit (an
unbidden suggestion core hands to a model); sharing one number between them
meant every search result's abstract was silently cut at 400 characters —
unremarkable-looking, but real data loss on a page nobody asked to have
shortened, with no ellipsis to say so (see `atom.py::_collapse`'s
docstring). They are now sized generously enough that an ordinary arXiv
title or abstract is never touched. The gap-suggestion-specific cut still
happens, but it moved to where it is actually needed: `consult.py`'s
`_suggestion` now truncates explicitly to core's own
`GAP_SUGGESTION_TITLE_MAX_CHARS`/`GAP_SUGGESTION_SUMMARY_MAX_CHARS` (imported
from `app.extension_sdk`, never hand-copied) on the way into a
`GapSuggestion` — core's own admission host would cut an over-long value
anyway, but this plugin hands over an already-compliant value rather than
leaning on that as its only line of defence.

**`MAX_QUERY_TERMS` is user-visible behaviour, and it must be understood as
such.** `routes.py::search` rejects a search-box query of more than 8
whitespace-split words with an explicit 400 (`检索词最多 8 个，请精简后重试`),
before the throttle or arXiv are touched, and the panel disables its submit
button and shows the same message once the box holds more than 8 words. The
ninth word onward is **not** silently dropped: `build_query_url`'s own
`query.split()[:MAX_QUERY_TERMS]` slice still exists, but only as defence in
depth for a caller that reaches it directly — the route already refused any
input the slice would have had to truncate, and gap-consult's own term
extractor (`consult.py::_query_terms`) already bounds itself to
`MAX_QUERY_TERMS` terms before calling in, because its query is a handful of
terms this plugin derived from the question and gap phrases, not user-edited
text passed straight through.

**`QUERY_MAX_CHARS` is the same story, one layer earlier (P2-3).**
`routes.py::search` checks a query's *length* — in Unicode code points —
before it ever checks word count, rejecting anything past 200 characters
with `检索关键词过长，请精简后再试`. The panel mirrors this bound too
(`search-panel-model.ts::queryExceedsCharLimit`), disabling its submit
button and showing that same sentence, separately from the word-count
message, once the box holds more than 200 characters — counted the same way
Python's `len(str)` counts (Unicode code points), not `string.length`
(UTF-16 code units, which double-count anything outside the Basic
Multilingual Plane).

**`MAX_AUTHORS` used to be a silent drop; it no longer is (codex #596 R4).**
A large-collaboration paper (an ATLAS or CMS result, say) can carry several
thousand authors, and `atom.py::ArxivPaper.authors` still caps at 20 names —
that ceiling exists for the same reason `TITLE_MAX_CHARS`/`SUMMARY_MAX_CHARS`
do, to bound one upstream record's memory footprint and keep the results
page readable. What changed is that the entries past the cap are no longer
simply gone: `ArxivPaper.authors_total` carries the entry's true author
count (always `>= len(authors)`), the `/search` route's wire shape includes
it alongside the capped list, and the panel's
`search-panel-model.ts::formatAuthors` appends a disclosure — "甲、乙等21人" —
whenever the two diverge, rather than quietly showing a twenty-author list
for a paper that has more.

## 4. Behaviour disclosures

### 4.1 Two settings, not one, for gap consultation

Setting `consult_enabled = true` by itself is **not enough** to make the
plugin actually reach out to arXiv. Core places a single hard deadline on
the entire `ask.gap_consult` extension point,
`ASK_GAP_CONSULT_TIMEOUT_SECONDS` (default **4.0** seconds, accepted range
`0 < x ≤ 30`). With this plugin's own defaults, the worst case it needs to
finish inside that deadline is:

```
politeness_interval_seconds + timeout_seconds + CONSULT_RETURN_MARGIN_SECONDS
= 3.0 + 10.0 + 0.25 = 13.25 seconds
```

That is longer than the 4-second deadline, so the plugin refuses to even
start a request — it never fires, however many times you flip
`consult_enabled`. To actually enable outbound consultation you must **also**
raise `ASK_GAP_CONSULT_TIMEOUT_SECONDS` past 13.25 seconds (e.g. `15.0`; the
core-side ceiling is 30), or lower `timeout_seconds` far enough that the
worst case fits under the deployment's existing deadline. This is
deliberate: an answer that arrives after core's deadline is read by nobody,
so starting a request that cannot possibly finish in time is pure waste.

### 4.2 Gap consultation goes silent on a Chinese-only question

The term-extraction pass scans the **question wording plus every gap
phrase** together for Latin-alphabet search terms
(`consult.py::_query_terms`). If none can be found at all, consultation
returns a stable code (`arxiv_no_latin_terms`) with **zero network calls and
zero politeness-slot usage** — never even attempting a request. The
reasoning is that arXiv is a Latin-keyword index, so a question written
entirely in Chinese is guaranteed to return nothing; sending it would only
spend a politeness slot and a round trip to learn what is already knowable
here. The visible consequence: **on a mostly-Chinese notebook, gap
consultation will rarely if ever produce a suggestion.** This is the
plugin's designed behaviour, not a bug to work around.

### 4.3 The politeness throttle has no per-call wall-clock ceiling

`timeout_seconds` bounds one socket operation
(`urllib.request.urlopen(..., timeout=timeout_seconds)`), not the call as a
whole — the clock resets on every connect and every partial read. An
upstream that trickles a few bytes at a time without ever going silent
longer than `timeout_seconds` can therefore hold the process-wide politeness
lock (production pins the backend to `--workers 1`, so this throttle is a
true process-wide lock) for far longer than `timeout_seconds` implies, and
every other concurrent `/search` request sharing FastAPI's threadpool waits
on it too. The `ask.gap_consult` route is protected from this by its
**caller** — core's `GapConsultHost` bounds the whole probe-and-consult call
on its own wall-clock deadline regardless of what the transport underneath
it does. The interactive `/search` route has **no equivalent outer
deadline**: its budget (`timeout_seconds + politeness_interval_seconds`) is
a request handed to the throttle, not an upper bound this module enforces
on the call itself. This sample does not add one. A multi-worker or
multi-replica deployment needs external coordination (e.g. Redis-backed);
this sample deliberately ships none.

### 4.4 "Already imported" is the panel's session memory, not backend deduplication

Core's URL import path does **not** deduplicate by content — every URL that
passes the PDF probe gets an unconditional new source row, followed by a
full parse. (Content-hash deduplication exists in this product only on the
browser **upload** path, keyed by `(notebook_id, file_hash)`; the URL
importer never reaches it.) Importing the same PDF link twice therefore
genuinely creates a second source and parses it again. The panel's
"本次已导入过，可能已产生重复来源" ("already imported this session, a
duplicate source has probably just been created") message reads from its
own in-memory record of URLs it has already sent this session — it is a
**warning**, not an assurance that the server reused anything.

This is a different thing from the *within-one-request* de-duplication
`routes.py::_import_urls` does (P2-2): a URL's second and later occurrences
in the *same* `/import` payload are dropped, silently, before
`url_sources.import_urls` is ever called — a feed that lists the same paper
twice, or a client that double-submits a click, is an ordinary shape, not a
caller mistake worth refusing the whole batch over. `MAX_IMPORT_URLS` is
checked against this deduplicated count. It does not change the paragraph
above: two *separate* `/import` requests naming the same URL still each
reach core's non-content-addressed importer and still each create a source.

### 4.5 The sample's tests deliberately break with SOP §5.3

The sample's own tests live at `backend/tests/test_arxiv_sample_plugin.py`
(the plugin's own decisions, against hand-built seams) and
`backend/tests/test_arxiv_sample_plugin_e2e.py` (real discovery, a real
application, the mounted wire) — inside this repository's own test tree —
instead of in a `tests/` directory inside the plugin package, which is what
[SOP §5.3](../../../docs/deployment-extensions-sop.md#53-checks-to-run-inside-the-plugin-repository)
tells a real out-of-tree plugin to do. The reason: this repository's backend
verification lane only collects `backend/tests`. If the sample shipped its
tests inside its own package tree the way SOP §5.3 describes, those tests
would never actually run in this repository's own CI — they would ship
unrun. **A real out-of-tree plugin should follow SOP §5.3 as written** and
keep its tests in its own repository's `tests/`; do not copy this sample's
arrangement. The cost of this deviation: "copy the whole package out and
its tests run standalone" does not hold for this sample the way it would
for a real out-of-tree plugin.

Zero-patch acceptance is about the **runtime**, and that half is covered:
`test_arxiv_sample_plugin_e2e.py::test_the_package_runs_from_outside_the_repository`
copies the entire package to a temporary directory outside the checkout,
puts only the copy on `sys.path`, boots an application against a TOML that
names it, runs a search through the mounted route, and asserts that every
imported module's `__file__` is under the copy. The other half — a second,
clean checkout, three environment variables, a green `npm run build`, and a
working panel — needs a second checkout and is transcribed in the pull
request that introduced this sample.

### 4.6 One silent filter that matters for mirrored deployments

Gap-suggestion URLs are checked against `settings.py::egress_allowed`, which
only allows `arxiv.org`, `export.arxiv.org`, and **the host of this
deployment's own configured `base_url`**. So if `base_url` points at an
internal mirror, and that mirror's Atom feed returns PDF links on a
**third**, different host, those suggestions are **silently dropped**
(fail-safe by design). Note that the import route's own allow-list is
wider (any `*.arxiv.org` subdomain, **plus an exact match of the
deployment's own configured mirror host** — a mirror's own PDF links can be
imported directly, not just quoted in a gap-consult suggestion) — the two
are deliberately different: a result the user clicked on themselves gets
the more permissive check; an unbidden gap-consult suggestion nobody asked
for gets the narrower one. The mirror addition on the import side is
strictly an **exact** host match, never a second suffix rule — widening it
to `*.<mirror>` would let a caller import `<mirror>.evil.example` on the
strength of a deployment's own configuration, so `sub.<mirror>` is refused
the same as any other foreign host.

## 5. Other registered limitations

- **XML entity expansion.** `xml.etree` parses through libexpat, which has
  shipped a built-in amplification-factor guard against entity-expansion
  ("billion laughs") payloads since libexpat 2.4 (bundled in CPython
  3.9.6+/3.8.11+/3.10.0b4+ and later). That guard, not anything in this plugin, is the
  actual defence on those runtimes. `MAX_RESPONSE_BYTES` (§3) is **not** a
  mitigation for this attack class — a payload of a few hundred bytes can
  still declare an expansion factor in the millions — it only bounds network
  cost. The half of the mitigation this sample actually owns is that
  `base_url` is deployment-configured, not user input: nobody can point this
  module at an untrusted upstream URL without first editing a TOML file. A
  plugin meant to face an untrusted upstream should depend on `defusedxml`
  instead; this sample keeps a zero-third-party-dependency footprint on
  purpose — `pyproject.toml` declares only `pydantic`, and the extension SDK
  and FastAPI both come from the backend environment the plugin is installed
  into rather than being vendored.
- **The politeness throttle is per process** (§4.3).
- **No `api_key_env`.** arXiv's API takes no credential, so this plugin does
  not invent an unused settings key for one. See the credential convention
  in [SOP §3.2](../../../docs/deployment-extensions-sop.md#32-settings-optional)
  for how a plugin that does need one should reference a secret by
  environment-variable name.

## 6. What the two entry points each demonstrate

1. **Human-driven search and import.** Side-panel entry → search dialog →
   select results → the plugin's **own** `/import` route → core's URL
   import port (that port itself authorizes the current request user for
   `sources:write` on the target notebook — the plugin route does not do
   this itself).
2. **Agent-triggered gap consultation.** At the end of a step-by-step
   reasoning answer, core's `ask.gap_consult` extension point asks
   installed plugins for pointers outside the notebook. The import button
   on the resulting suggestion card is **core's own UI, calling core's own
   endpoint** — it does not go through this plugin at all.

**Two separate capability gates, not one.** `manifest.provides`'s
capability only gates the side-panel entry ("is this plugin configured at
all"). Outbound gap consultation is gated **separately**, per-contribution,
by `ArxivGapConsultContributor`'s own availability probe ("has this
deployment agreed to let it reach out to arXiv on its own"). Turning
consultation off leaves the search panel and the import route exactly as
they were.

## 7. G2 lane and recovery

This section is about the **UI package** half only — see §4.5 for the
backend-side tests, which run in **G1** on every PR like any other test in
the tree. This sample's UI package is **not** part of the default
`npm run test` tree — `extension-ui-host.component.test.tsx` pins "the
merged registry equals the built-in catalog, length 1, with zero plugins
configured", and that must not be relaxed to accommodate this sample. Its
own verification lane is
[`bash scripts/check_sample_plugin.sh`](../../../scripts/check_sample_plugin.sh)
(G2), which runs as part of `bash scripts/check_extended.sh`.

If that script is run and interrupted (or the sync is otherwise left
in place) and `frontend/features/ext-arxiv-search/` is still on disk
afterward, restore your tree with:

```bash
cd frontend && SILICON_NOTEBOOK_UI_PLUGINS="<your original value, or empty>" \
  node scripts/sync-ui-plugins.mjs
```

The script's own `trap` **restores** the caller's original
`SILICON_NOTEBOOK_UI_PLUGINS` value on exit rather than clearing it — a
developer or deployment box with its own private plugins configured should
not lose them to an interrupted run of this sample's checks.
