# Final whole-branch Memory contract fix report

## Scope

Fixed only the three final review findings on branch `codex/agent-memory-system`
from base `1700291`:

1. fail-closed standard-JSON handling for Memory task context and evidence;
2. MCP proposal input reuse of the Core Memory contract before live auth/service;
3. atomic supersession when a proposed confirmed Memory is deprecated.

No MCP tool, lifecycle state, schema, or frontend capability was added.

## TDD evidence

### RED

- Six real `MemoryService`/`SQLiteRepository` cases showed nested Python
  `NaN`, positive infinity, and negative infinity in either `task_context` or
  `evidence_refs` were accepted and persisted instead of raising.
- The official MCP proposal test showed three just-over-Core scalar inputs
  reached live principal/service work because the adapter used looser duplicate
  limits.
- Deprecating a proposed Memory left `promotion_state='proposed'` and an active
  queue row. A concurrent deprecate/approve run could leave a terminal Memory
  with a still-proposed queue row. `confirmed -> rejected` was confirmed to be
  an existing illegal transition and was not expanded.

### GREEN

- Core non-finite/null focused regression: `7 passed`.
- MCP Core-limit/non-finite/null focused regression: `6 passed`.
- Promotion terminal/illegal-reject/race focused regression: `3 passed`.

## Implementation

### Standard JSON boundary

`memory_inputs.py` now recursively rejects actual non-finite Python floats and
uses `allow_nan=False` for both size validation and the shared persistence
serializer. `MemoryService.create_candidate` therefore rejects bad task/evidence
before notebook lookup or persistence. `MemoryStore` also uses the strict
serializer for Memory tags/provenance and promotion provenance updates, so an
internal caller cannot write non-standard JSON tokens. SQLite writes remain
transactional, leaving no Memory or provenance row on failure. Nested `None`
remains valid JSON null and round-trips through a real repository/service list
and detail read.

### MCP/Core contract

The MCP proposal envelope imports and calls the exact Core title, content, tag,
reason, task-context, evidence, and client-request-id normalizers instead of
maintaining looser scalar duplicates. The pre-existing tighter MCP serialized
sub-budgets (tags/task context/evidence) and raw collection caps remain in
place. Validation still occurs before `_selected_notebook`, so just-over-Core
input performs no live token refresh and no service call. The exact seven-tool
surface and every response budget are unchanged.

### Promotion terminal transaction

The existing proposed-edit logic is now one
`MemoryStore._supersede_active_promotion_on` helper. Within the same Memory
write transaction it:

- rejects the active `proposed`/`under_review` queue row with deterministic
  `superseded_by_memory_terminal_status` for a terminal transition;
- records the owner reviewer and superseded timestamp/reason in provenance;
- retains immutable `kg_promotion_snapshots` and the pinned source revision;
- clears the current `memory_items.promotion_state` to `none`;
- writes the terminal Memory revision.

Approval after supersession fails before any Base KG mutation. The concurrent
approve/deprecate test permits only two serialized outcomes: fully approved
before deprecation, or fully superseded with zero Base objects. The existing
`confirmed -> rejected` transition remains illegal and its regression verifies
zero state/queue/Base mutation.

## Documentation

Updated `README.md`, `README_zh.md`, and `AGENTS.md` together for strict
non-finite/null handling and proposed-Memory deprecation. Updated
`fangan_done.md` with the verified behavior and current gate counts.

## Verification

- Broader Memory/API/MCP/promotion/phase suite: `93 passed`.
- Architecture/contracts/static manifest suite: `77 passed`.
- Exact full backend: `2934 passed, 1 skipped` in `254.83s`.
- Frontend tests: `189 passed`.
- TypeScript: `npm run lint` passed.
- Next.js production build: `npm run build` passed.
- `git diff --check`: passed.

## Self-review

- Confirmed Core validation happens before access/idempotency/persistence and
  MCP validation before live principal lookup.
- Confirmed legitimate nested null is not conflated with a non-finite float.
- Confirmed no new lifecycle edge was introduced for rejection.
- Confirmed the terminal supersession, queue mutation, provenance update,
  promotion-state reset, and revision append share one SQLite write transaction.
- Confirmed historical pinned snapshots remain readable in the rejected audit
  queue while the active queue contains no superseded proposal.
- Confirmed the exact MCP public tool set and response budget code were not
  changed.
