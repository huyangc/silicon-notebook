# Whole-branch promotion blocker fix report

## Scope

Fixed only the three requested promotion review blockers on
`codex/agent-memory-system` from base `95ded95`:

1. Memory→KG proposals now pin an immutable, proposal-specific source snapshot.
2. The admin review UI renders the complete sanitized typed candidate/evidence contract.
3. Approve/reject routes persist the authenticated admin reviewer id.

## TDD evidence

Initial REDs were observed before production changes:

- backend pinned-snapshot tests failed for missing `source_revision`, proposed edits
  remaining `proposed`, and the missing reviewer-aware facade path;
- frontend review projection failed because `promotion-review.ts` did not exist;
- the multi-admin route regression failed with both reviewers incorrectly attributed to
  the built-in first admin;
- the knowledge-object variant failed with `reviewed_by='curator'`.

## Implementation

- Each Memory proposal stores a private snapshot keyed by `proposal_id` in
  `memory_provenance`. It pins `source_revision`, proposal revision, title/content/tags,
  sanitized Concept/Claim/Formula/Procedure candidates, and server-validated evidence.
- Queue hydration and approval both read the same proposal-keyed snapshot. Queue output
  exposes only the sanitized projection plus `source_revision`; raw content/provenance and
  Agent task context remain private.
- Approval validates the candidate↔notebook binding, current creator access, confirmed and
  proposed states, current proposal id, latest revision, source revision row, and current
  Memory content against the pinned snapshot inside the write transaction before Base KG
  mutations.
- Editing a proposed Memory uses the existing Memory write transaction to reject the active
  queue item as `superseded_by_memory_edit`, attribute the supersession to the creator,
  reset `promotion_state='none'`, retain the immutable historical snapshot for audit, and
  append the edit revision. A new proposal receives a new id and snapshot.
- Existing pre-fix Memory proposals without a pinned snapshot remain listable as a safe empty
  projection, but approval fails closed; editing them supersedes them and allows a pinned
  re-proposal.
- Frozen facade signatures remain intact. New explicit
  `approve_promotion_as_reviewer` / `reject_promotion_as_reviewer` adapters carry the
  authenticated route user id through the facade and governance service. Both Memory and
  ordinary knowledge promotions record the real reviewer.
- The admin queue displays the pinned revision, every candidate, all type-specific fields
  (including explicit `未提供` values), and every server-validated evidence card.

## Regression coverage

- pinned queue/approval revision snapshot;
- proposed edit supersession and re-proposal;
- approval tamper mismatch with zero Base/candidate/revision side effects;
- concurrent edit/approval race permits only a fully approved or fully superseded outcome;
- approval idempotency plus stale/rejected candidate no-side-effect handling;
- creator access and notebook mismatch fail closed;
- authenticated attribution across two distinct admin users for Memory approve, Memory reject,
  and ordinary KG approve;
- frontend typed candidate, all-evidence, pinned-revision, and private-field omission projection.

## Verification

- Focused promotion/governance/mutation: `107 passed`.
- Repository surface/ports/module contracts: `20 passed`.
- Repository API/facade contracts: `23 passed`.
- Final promotion + surface focused run: `29 passed`.
- Frontend: `185 passed`; `npm run lint` passed; `npm run build` passed.
- Final exact backend suite: `2910 passed, 1 skipped in 255.70s`.
- `git diff --check`: clean.

## Self-review

- Confirmed no raw Memory provenance/task context enters the promotion response or UI review
  projection.
- Confirmed the stale-snapshot validation and Base writes share one SQLite write transaction.
- Confirmed the edit invalidation and Memory revision append share one SQLite write transaction.
- Confirmed legacy facade signatures and frozen OpenAPI/repository contracts remain green.
- Confirmed no progress ledger changes and no unrelated branch findings were modified.

## Follow-up: production-shaped review projection

An independent whole-branch review found that the first frontend fixture did not match
`MemoryService._promotion_candidates`: it omitted `name` from Claim/Formula/Procedure and
treated `variables` / `goal` as guaranteed review fields. The follow-up was again completed
with a failing test first.

- Claim now projects `name` + `statement`.
- Formula now projects `name` + `expression`.
- Procedure now projects `name` + `steps`.
- Concept projects the payload's `name` and optional `definition`.
- Optional future `variables` / `goal` are rendered only when actually present in the pinned
  payload; they are not shown as production guarantees.
- A regression invariant compares every production-shaped payload key with the projected
  review labels, preventing a field that will enter Base KG from being omitted in admin review.

Follow-up verification: focused review projection `3 passed`; complete frontend `185 passed`;
`npm run lint` and `npm run build` passed.
