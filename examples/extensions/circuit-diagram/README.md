# Circuit Diagram — Sample Deployment Extension

[中文](./README_zh.md)

A sample deployment plugin for the `source.element_enricher` extension point:
when a source is parsed, every image it contains is shown to a vision model,
and the ones the model calls schematics get a SPICE-style netlist and a
one-line function summary written into the element core already persists.

It is off in every default checkout, and turning it on is entirely a
deployment-side decision. It is a companion to
[`docs/deployment-extensions-sop.md`](../../../docs/deployment-extensions-sop.md)
(the deployment-extensions SOP) and to the arXiv sample next door, which
demonstrates a different pair of points (HTTP routes and gap consultation).

This document is written for the operator who enables it, not for someone
extending the sample's own code — for that, read the source docstrings and
the SOP.

## 1. Two steps to enable it

1. **Install the Python package into the backend's interpreter.** Either
   `pip install -e examples/extensions/circuit-diagram` into the environment
   `PYTHON_BIN` points at, or put its `src/` directory on `PYTHONPATH`. The
   package name is `silicon-notebook-circuit-diagram`; the importable module
   is `silicon_notebook_circuit_diagram`. Then copy
   [`extensions.example.toml`](./extensions.example.toml) outside the
   checkout, edit it, and point `EXTENSIONS_CONFIG` at your copy (e.g.
   `EXTENSIONS_CONFIG=/etc/silicon-notebook/extensions.toml`). Nothing loads
   that file automatically — an unmodified checkout ships no default that
   enables the plugin.
2. **Export the API key** under the name `api_key_env` gives (default
   `DEEPSEEK_API_KEY`). Until that variable carries a non-blank value the
   plugin is *loaded but unavailable*: it appears in `/admin/extensions`, core
   asks it nothing, and every parse behaves exactly as if it were not
   installed. The event log records `api_key_missing` rather than a failure.

Both must be in place **before** the process starts — plugin topology is
frozen at startup composition time, and there is no hot reload. After changing
the TOML or the environment, restart the backend.

That is a separate layer from runtime enable/disable: once this plugin is
loaded, an admin can switch it off at `/admin/extensions` without a restart.
See [Deployment extensions SOP §8](../../../docs/deployment-extensions-sop.md#8-upgrade-rollback-disable).

There is no UI package and no frontend step: this plugin adds no panel, no
route and no slot. Everything it produces is rendered by core's existing
source-detail view.

## 2. Settings table

All keys are optional; core computes the accepted key set from the settings
model itself, so a misspelled key in the deployment TOML is a startup failure,
not a silently ignored line.

| Key | Default | Range / shape | What it does |
| --- | --- | --- | --- |
| `base_url` | `https://api.deepseek.com` | absolute `http(s)` URL, no query, no fragment | Endpoint root. The plugin appends `/chat/completions`. A trailing slash is stripped. |
| `model` | `deepseek-flash` | non-blank, no control characters | Sent in the request body and persisted into each enriched element's metadata. |
| `api_key_env` | `DEEPSEEK_API_KEY` | environment variable name | The **name** of the variable holding the key, never the key. |
| `timeout_seconds` | `30.0` | `0 < x ≤ 120` | Per-request socket timeout, and the worst case the budget check reserves. |
| `max_images_per_source` | `8` | `1..64` | How many images of one source may be sent, in parse order. |
| `max_image_bytes` | `4194304` | `1024..33554432` | Images larger than this are skipped rather than sent. |
| `prompt_language` | `zh` | `zh` or `en` | Language the model answers in, and of the description heading. |

## 3. What it writes, and where

For each image the model calls a schematic, the plugin proposes one candidate
and core persists it into that element:

* `metadata.extensions["examples.circuit_diagram.enricher"].metadata` —
  `{"is_circuit": true, "netlist": "...", "function": "...", "model": "..."}`,
  under the contribution's own name, alongside the plugin id and version.
* `metadata.description` — the human-readable text, appended to whatever
  description the parser already produced:

  ~~~
  电路功能：一个由 R1/R2 构成的分压网络……

  ```spice
  R1 in out 10k
  R2 out 0 10k
  ```
  ~~~

  The front end splits that on the fence and renders the netlist as a code
  block rather than reflowing it into a paragraph.
* `text` — the same description with its whitespace flattened, appended to the
  element's own text. This is what puts the element into the retrieval corpus:
  an image element with neither caption nor description is excluded from
  chunking, so before enrichment a bare schematic was unsearchable.

An image the model says is **not** a schematic gets nothing at all — no
"checked, not a circuit" marker. A marker would be persisted metadata whose
only content is this plugin's own opinion.

## 4. Accuracy is not claimed

This sample exists to prove the link from a parsed image to persisted,
retrievable metadata. It does not claim that `deepseek-flash` reads any
particular schematic correctly, and the netlists it produces should be treated
as a starting point for a human, not as a circuit description you can simulate.
Whether the accuracy is good enough is the deployment's own question; the
`model` field is persisted with every enrichment so that answer can be revised
later without guessing which model produced what.

## 5. Registered limitations

* **`timeout_seconds` bounds one socket operation, not the whole call.**
  `urllib.request.urlopen(..., timeout=...)` resets its clock on connect and
  on every partial read, so an upstream that trickles bytes can outlast it.
  What actually bounds this plugin's turn is core's own
  `SOURCE_ELEMENT_ENRICHER_TIMEOUT_SECONDS` deadline, enforced on a thread the
  host can abandon.
* **A source can be classified only in part.** The plugin refuses to start an
  image unless `timeout_seconds + 0.25s` is still left on core's deadline, and
  reports the rest as a partial result. See the note at the bottom of
  `extensions.example.toml` for how to size the two settings against each other.
* **Requests are serial, one image at a time.** There is no batching and no
  concurrency: the point runs on a single host-owned worker thread, and
  parallel outbound requests from a plugin would be latency the deployment did
  not budget for.
* **No outbound address policy.** `base_url` is deployment-configured rather
  than user input, so the plugin does not re-check that the host resolves to a
  public address the way core's URL ingestion does. A variant that let *users*
  choose the endpoint would have to add that check.
* **Response bodies are read to at most 256 KiB**, and a response past that
  ceiling becomes a skipped image rather than an unbounded read.
* **Per-image failures are counted, not reported per image.** A refused
  request, a malformed answer or an unreadable asset each skip that image and
  turn the result into `partial`; core's event carries the count, not which
  image.
* **Redirects are not followed.** A 3xx from `base_url` is treated as a failed
  request, not as a hop. The credential travels in an `Authorization` header
  and `urllib`'s default handler would replay it at whatever host the response
  named; a deployment that moved its endpoint changes `base_url` instead.
* **Identical images are classified once per source, but not pre-screened.**
  Images are de-duplicated by SHA-256 of their bytes within one source, so a
  header logo repeated on forty pages costs one request. It still consumes
  forty of the `max_images_per_source` slots, because the cap is applied to the
  element list before any bytes are read. There is deliberately **no** minimum
  size filter either: "too small to be a schematic" is a judgement this sample
  does not make, so a page full of icons can use up the budget. A deployment
  with that shape of document should lower `max_images_per_source` or raise
  core's deadline.
* **The model's own answer is reshaped before it is persisted.** Line endings
  are normalised; characters core would refuse (U+3000, NBSP, zero-width
  joiners, a BOM) are replaced with an ordinary space; a netlist the model
  fenced itself is unfenced so the fence this plugin writes is not nested; and
  both fields are cut to the ceilings below. An answer that claims a schematic
  but carries neither a netlist nor a summary is dropped.

### Plugin-private numeric limits

Not deployment-configurable — they live in the plugin's own source. Listed
because they explain what an operator sees.

| Limit | Value | Where | What it bounds |
| --- | --- | --- | --- |
| `NETLIST_MAX_CHARS` | 4000 | `enricher.py` | Longest netlist persisted, after normalisation. |
| `FUNCTION_MAX_CHARS` | 1000 | `enricher.py` | Longest function summary persisted. |
| `RETURN_MARGIN_SECONDS` | 0.25 | `enricher.py` | Reserved on top of `timeout_seconds` before starting another image, to cover parsing and core's 50 ms join slice. |
| `MAX_RESPONSE_BYTES` | 256 KiB | `client.py` | How much of one response body is read. |
| De-duplication | SHA-256 of the image bytes, per `enrich` call | `enricher.py` | Successful classifications only; a failed request is retried on the next identical image. |

Both `NETLIST_MAX_CHARS` and `FUNCTION_MAX_CHARS` are further reduced when
core's `SOURCE_ELEMENT_ENRICHER_MAX_DESCRIPTION_CHARS` is smaller, since the
heading and the fence are charged against that limit first.

## 6. Verifying it against the real model

The automated tests run with no network at all (see §7), so the one thing they
cannot tell you is whether your endpoint, key and model actually answer. That
check is manual and takes a minute:

1. Export the key and point `EXTENSIONS_CONFIG` at your TOML, then start the
   backend.
2. Open `/admin/extensions` and confirm the plugin is listed and enabled. If
   it is listed but contributes nothing, the key is missing — the event log
   says `api_key_missing`.
3. Upload a small Markdown file with one embedded schematic image (a
   `![](data:image/png;base64,...)` data URI is enough, and so is a `.zip`
   bundle or a PDF page) into a scratch notebook.
4. Wait for the source to reach `extracted`, then open it. The image should
   show a `电路功能：…` line followed by a fenced netlist block.
5. `GET /api/sources/{source_id}/elements` and check
   `metadata.extensions["examples.circuit_diagram.enricher"]` is present on the
   image element.
6. Ask a question in that notebook whose answer is in the function summary. The
   image element should now be retrievable — that is what the `text` append is
   for.

If step 4 shows nothing, the event log is the place to look: this point emits
`source_element_enricher_attempt` with a stable `reason_code` for every
attempt, and the plugin never puts a URL, a key or a setting value into one.

## 7. Tests

`backend/tests/test_circuit_diagram_sample_plugin.py` covers what the plugin
*decides* — settings validation, the availability probe's three states, the
three shapes of model answer, the request the transport builds, the budget
stop, and the filters that keep non-images out. `…_e2e.py` covers what core
does with it once a real TOML names it: a real `create_app()`, a real upload, a
real parse, and the persisted element read back over the API.

Both run with no network: the only seam faked is `client._post`, the
injectable transport this package ships for exactly that purpose, and the
end-to-end file additionally makes `socket.getaddrinfo` raise so a future edit
that reintroduced a real dial fails loudly.

Those tests live under the backend test root rather than in this package,
because the backend verification lane only collects `backend/tests` — a sample
shipping its tests in its own tree would ship them unrun. **Do not copy that
arrangement into a real out-of-tree plugin**: the SOP asks such a plugin to
keep its tests in its own repository and run them in its own CI.
