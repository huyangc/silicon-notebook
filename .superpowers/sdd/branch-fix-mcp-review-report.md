# Whole-Branch MCP Review Fix Report

## Scope

This branch fixes only the Memory MCP adapter review findings:

1. strict serialized output budgets for the exact seven public MCP tools;
2. fresh-record hydration and terminal-state race handling in
   `search_agent_memory`;
3. an MCP-local bounded input envelope for `propose_memory` before live
   repository/service work.

No Memory UI, core models, service/store lifecycle, promotion behavior, token
policy, session semantics, Origin/HTTPS enforcement, or tool count was changed.

## TDD Evidence

The focused suite started from an 11-test green baseline. New official-client
tests then produced four intended RED failures:

- a `list_notebooks` response with many custom count types serialized to 67,658
  characters, above the 12,000 budget;
- a deleted record between retrieval and hydration raised `KeyError` and failed
  the entire `search_agent_memory` call;
- all 14 invalid proposal envelopes reached live-principal refresh before being
  rejected;
- `_bounded` accepted an oversized first item.

The first GREEN pass reached 14/14 focused tests. A self-review added UTF-8 byte
budget cases and multibyte nested proposal payloads; those tests were observed
RED before byte-aware accounting was implemented, then returned GREEN.

A follow-up review restored the earlier field sub-budgets. Official-client probes
were observed RED with 4,061 serialized tag characters against the 1,500 cap,
and four nested non-finite proposals (`NaN`, positive/negative infinity, and
`1e9999`) reached live-principal refresh. The focused tests returned GREEN only
after sub-budget packing and strict non-finite validation were added.

## Implementation

### Output budget

- Every successful result from each of the exact seven tools now passes through
  one deterministic finalizer with a 12,000-byte serialized UTF-8 JSON budget.
- Scalars, mapping keys/entries, list sizes, nesting depth, per-tool text fields,
  anchors, and provenance are bounded before exact final packing.
- Known notebook count and KG status keys are retained ahead of arbitrary custom
  keys. Normal identifiers are preserved and are reduced only as a final
  impossible-to-fit fallback.
- Each response includes fixed-shape `truncation` metadata containing only the
  budget and aggregate omitted item/map/character/field counts. It never copies
  private keys, paths, or values into metadata.
- Search/get provenance has a 2,000-character aggregate budget, get tags have a
  1,500-character aggregate budget, Ask anchor provenance is limited to 500
  characters per anchor, and the full Ask anchor collection is limited to 3,500
  characters. The real-tool probes verify these limits and retained identifiers.
- The superseded private `_bounded` helper and its helper-only unit test were
  removed; public behavior remains covered through the official MCP client.

### Hydration race

- Retrieval hits are treated only as candidate IDs plus relevance signals.
- After hydration, notebook binding and current status are checked again.
- Rejected, deprecated, deleted, inaccessible, or newly unauthorized candidate
  records are skipped without failing the remaining search.
- `status`, `title`, `content`, provenance, and status-derived booleans come only
  from the fresh record.

### Proposal envelope

- Validation occurs at the start of the tool, before repository lookup, live
  principal refresh, allowlist recheck, or Memory creation.
- Title, content, reason, client request ID, tags, task context, and evidence are
  normalized and checked for nonblank values where applicable.
- Deterministic character/item caps cover scalar fields and tags; serialized
  UTF-8 byte caps cover tags, nested task context, and total nested evidence.
- Evidence count is independently capped. Validated normalized values alone are
  passed to the existing service, keeping the guard compatible with later shared
  service-level limits.
- Nested task context and evidence use `allow_nan=False` plus recursive finite
  validation. Null is rejected in these strict nested envelopes because the
  official client serializes non-finite floats as null.

## Self-Review

- Exact public tool set remains seven; no tool was added or removed.
- Live token/session/scope/allowlist/notebook checks remain on every valid data
  operation; invalid proposal payloads fail earlier without weakening transport
  authentication.
- Candidate/formal retrieval-plane isolation and HTTPS/Origin behavior remain
  covered by the existing official-client suite and smoke.
- Product SQL and core Memory lifecycle behavior remain outside the MCP adapter.
- Architecture consumer allowlist changes only refresh the line-specific call
  sites shifted by the MCP-local helper block.
- Diff contains only the MCP adapter, its official-client tests, the line-specific
  architecture guard, and this report.

## Verification

- Focused official MCP client after follow-up cleanup: `13 passed`.
- Offline MCP smoke: `memory MCP smoke: OK (7 tools, session isolation, candidate plane isolation)`.
- Final MCP/Auth/architecture regression slice: `100 passed`.
- Architecture manifest failure found during the first full run was corrected and
  its focused guard reran `56 passed`.
- Final exact backend suite after follow-up cleanup: `2912 passed, 1 skipped in 273.10s`.
