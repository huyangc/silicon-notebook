# Final whole-branch Memory contract fix report

## Scope

Fixed only the three final review findings on branch `codex/agent-memory-system`
from base `1700291`:

1. fail-closed standard-JSON handling for Memory task context and evidence;
2. MCP proposal input reuse of the Core Memory contract before live auth/service;
3. atomic supersession when a proposed confirmed Memory is deprecated.

A follow-up final review additionally required pointer-free superseded
provenance, neutral Core ownership of JSON safety, and exact (not narrower)
MCP/Core acceptance limits.

The final narrow review required tag-count/blank semantics to live exclusively
in the same shared normalizer.

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
- Follow-up RED proved superseded current provenance retained `proposal_id`,
  `MemoryStore` depended on `app.services.memory_inputs`, and a valid proposal
  above the former MCP-only 8,000-byte/20-item/12,000-byte sub-budgets was
  rejected despite fitting the Core contract.
- Final tag RED proved 21 duplicate tags bypassed the shared post-dedup count,
  the real API persisted them, and MCP enforced count/blank rules locally.

### GREEN

- Core non-finite/null focused regression: `7 passed`.
- MCP Core-limit/non-finite/null focused regression: `6 passed`.
- Promotion terminal/illegal-reject/race focused regression: `3 passed`.
- Follow-up pointer/architecture/exact-MCP RED tests: `1 + 1 + 1` failed first,
  then passed after the narrow fixes.
- Final tag Core/service, real API, and MCP-delegation regressions failed first,
  then passed (`1 + 1 + 3` focused GREEN).

## Implementation

### Standard JSON boundary

Neutral `app/core/json_safety.py` now recursively rejects actual non-finite
Python floats and uses `allow_nan=False` for both canonical size validation and
the shared persistence serializer. `memory_inputs.py` converts neutral safety
errors to its public input error, while `MemoryStore` imports only the neutral
Core helper. `MemoryService.create_candidate` therefore rejects bad task/evidence
before notebook lookup or persistence. `MemoryStore` also uses the strict
serializer for Memory tags/provenance and promotion provenance updates, so an
internal caller cannot write non-standard JSON tokens. SQLite writes remain
transactional, leaving no Memory or provenance row on failure. Nested `None`
remains valid JSON null and round-trips through a real repository/service list
and detail read.

### MCP/Core contract

The MCP proposal envelope imports and calls the exact Core title, content, tag,
reason, task-context, evidence, and client-request-id normalizers instead of
maintaining looser scalar duplicates. Adapter-only tag/task/evidence serialized
sub-budgets were removed: MCP accepts the exact Core 8,192-byte task context,
50 evidence references, and 32,768-byte evidence payload contract. Validation
still occurs before `_selected_notebook`, so just-over-Core
input performs no live token refresh and no service call. The exact seven-tool
surface and every response budget are unchanged.

`normalize_tags` now caps the raw sequence at 20 before trim/dedup and rejects
every blank tag. Pydantic, service/internal callers, and MCP all delegate to
that one rule; MCP contains no duplicate raw-count or blank-tag policy.

### Promotion terminal transaction

The existing proposed-edit logic is now one
`MemoryStore._supersede_active_promotion_on` helper. Within the same Memory
write transaction it:

- rejects the active `proposed`/`under_review` queue row with deterministic
  `superseded_by_memory_terminal_status` for a terminal transition;
- records the owner reviewer and superseded timestamp/reason in provenance;
- retains immutable `kg_promotion_snapshots` and the pinned source revision;
- replaces current `kg_promotion` with a pointer-free superseded state, so its
  `proposal_id` remains only in snapshot and queue history;
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

- Final broader Memory/API/MCP/promotion/architecture suite: `125 passed`.
- Exact full backend: `2939 passed, 1 skipped` in `264.64s`.
- Focused frontend Memory model regression: `13 passed`.
- Frontend tests: `189 passed`.
- TypeScript: `npm run lint` passed.
- Next.js production build: `npm run build` passed.
- Repository gate: `scripts/check.sh` passed (including `2939 passed, 1 skipped`,
  `189` frontend tests, TypeScript, and production build).
- `git diff --check`: passed.

## Self-review

- Confirmed Core validation happens before access/idempotency/persistence and
  MCP validation before live principal lookup.
- Confirmed legitimate nested null is not conflated with a non-finite float.
- Confirmed no new lifecycle edge was introduced for rejection.
- Confirmed the terminal supersession, queue mutation, provenance update,
  promotion-state reset, and revision append share one SQLite write transaction.
- Confirmed historical pinned snapshots remain readable in the rejected audit
  queue while current provenance has no proposal pointer and the active queue
  contains no superseded proposal.
- Confirmed stores no longer import the Memory service input module.
- Confirmed MCP accepts values between the removed adapter sub-budgets and the
  exact Core limits, while just-over-Core input stops before live auth/service.
- Confirmed the exact MCP public tool set and response budget code were not
  changed.
