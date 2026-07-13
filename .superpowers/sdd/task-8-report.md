# Task 8 Report: Governed Memory-to-KG Promotion

## Result

Implemented creator-owned, confirmed-only Memory promotion through the
existing personal-to-base governance queue.

- Global and notebook Memory cards expose `提升到 KG` only while a Memory is
  `confirmed` with `promotion_state=none`.
- `POST /api/memories/{memory_id}/promote` verifies the current user is the
  Memory creator and still has notebook read access, then adds a
  `source_kind=memory` entry to the existing admin promotion queue.
- Memory proposals never create a staging object in a personal
  `knowledge_objects` corpus. The queue points to the private Memory id and
  persists only sanitized Concept / Claim / Formula / Procedure extraction
  candidates in the Memory's promotion audit record.
- The existing admin queue displays Memory-backed proposals, extracted object
  types, and server-validated source evidence. Its existing approve/reject
  actions handle both Knowledge and Memory sources.

## Lifecycle and Privacy

Proposal, approval, and rejection atomically update `promotion_state`, append
a Memory revision, and update `provenance.kg_promotion`. They do not change the
Memory's notebook, creator, lifecycle status, or retrieval visibility.

Only title, Memory content, tags, and deterministic structured candidates can
reach the review payload. Agent task context, proposal reason, client request
details, Agent profile/token data, and raw provenance are never copied into a
Base KG payload. Agent-provided evidence references are not trusted for
promotion. Ask citations are accepted only after the source and element are
resolved against the Memory's notebook; the published quote comes from the
stored SourceElement rather than the citation payload.

Candidate, rejected, and deprecated Memory cannot be proposed. Approval also
fails closed if the owner deprecates a proposed Memory before admin review.
Rejection leaves the Memory private and confirmed while recording the curator
reason. Losing notebook access between the service preflight and the proposal
transaction also fails closed through a transaction-local access recheck.

## Base KG Approval and Idempotency

Admin approval creates or merges approved Base KG objects for the supported
extraction types. Each object uses the existing `find_base_dedup_match`,
`merge_evidence_lists`, and object-source replacement path. Existing Base
objects win dedupe and receive only approved evidence.

The complete resulting Base object id list is stored on the private Memory.
Approval retries return that persisted list without creating additional
objects, revisions, or post-commit mutations. The original confirmed Memory
continues to participate at confirmed-Memory authority; only the resulting
Base objects receive Base authority.

## Frontend

- Added promotion-state helpers and owner-authenticated endpoint construction.
- Added Memory-card action/status rendering for proposed, approved, and
  rejected states, including abort-safe mutation behavior.
- Extended the existing admin queue type and UI to distinguish
  `Memory 提取候选`, show extracted object types, display validated evidence,
  and keep the same approve/reject controls.
- Updated the intentional OpenAPI fixture for the new endpoint and the
  additive `source_kind`, `memory_id`, and `base_object_ids` response fields.

## TDD Evidence

The initial backend RED run produced 9 failures because
`propose_memory_promotion` and the REST endpoint did not exist. It covered
creator/confirmed-only authorization, queue reuse, four supported object
types, private-field redaction, approval/rejection state, Base dedupe,
idempotent retries, and API owner/admin boundaries. The frontend RED failed
because the promotion helpers and UI were absent.

A later RED proved an admin could approve after the owner deprecated a pending
Memory; approval now revalidates `confirmed` inside the write transaction.
Another RED proved the queue did not yet show safe evidence; the queue now
hydrates only SourceElement-validated evidence before review.

## Verification

- Focused Memory promotion: `11 passed`.
- Promotion/governance/phase/failure/surface/API-contract regression:
  `106 passed in 9.50s`.
- Frontend all tests: `182 passed`.
- Frontend TypeScript: `npx tsc --noEmit` passed.
- Frontend production build: `npm run build` passed.
- `git diff --check`: clean.
- Exact backend suite: `2901 passed, 1 skipped in 250.23s`.

The first broader regression run had 5 failures: four frozen facade/surface
guards and the intentional OpenAPI delta. Root cause was the temporary change
to the old facade review signature plus unregistered Task 8 consumers. The
facade signature was restored, the Memory-specific surface allowlist was
added, and only the intentional OpenAPI schema/path fixture was regenerated.

## Self-Review

- Confirmed proposal is creator-private and notebook-bound at service and
  transaction boundaries.
- Confirmed no unapproved Memory or temporary Knowledge object enters normal
  notebook KG retrieval.
- Confirmed Memory remains `confirmed`, private, and notebook-bound after both
  approval and rejection.
- Confirmed admin approval creates actual `approved` Base KG objects and
  reuses the existing dedupe/evidence merge path.
- Confirmed repeated approval returns the same full Base object id set and
  cannot duplicate either new or merged objects.
- Confirmed Agent private context and unverified evidence do not appear in
  queue/base payloads; Ask evidence is resolved from stored SourceElements.
- Confirmed synchronous SQLite promotion work remains off the event loop in
  the new Memory REST endpoint.
- Confirmed the existing Knowledge promotion behavior and frozen facade
  signatures remain compatible.

## Remaining Concerns

No known correctness blocker. The first version uses deterministic extraction
from tags, Memory text, inline formulas, and ordered/bulleted steps; future
work may add an LLM-assisted extraction preview without weakening the same
privacy, evidence, and admin-review boundary.
