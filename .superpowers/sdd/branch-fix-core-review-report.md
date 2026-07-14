# Branch fix report: Agent Memory core review

## Scope

This branch closes the requested core review findings without changing the MCP tool/output
contract, Memory→KG promotion behavior, or the database schema:

1. Candidate provenance and evidence validation.
2. Shared Memory input limits across API, service/internal, MCP, and frontend boundaries.
3. Owner-wide Memory counts plus notebook filtering without N+1 queries.
4. Offset-aware Agent token expiry handling.
5. Transactional live-access revalidation when saving an Ask answer to Memory.

## Red-to-green evidence

Tests were added before implementation. The first focused backend run produced 9 expected
failures and 35 passes; both focused frontend model files also failed because the new helper
contracts did not exist. After implementation:

- Focused Memory backend: `44 passed`.
- Focused frontend models: `22 passed`.
- Broader Memory/MCP/API/contract suite: `193 passed`.
- All recursively discovered frontend tests: `189 passed`.
- Exact backend suite: `2918 passed, 1 skipped`.
- TypeScript and the Next.js production build passed.

## Implemented behavior

### Candidate provenance and review

- Candidate creation now holds `BEGIN IMMEDIATE`, rechecks live notebook access and the
  active owner-scoped Agent profile, and snapshots only profile id/name—never the bearer
  token.
- Every submitted evidence reference is retained as a safe identifier record and receives a
  server-side `validated`/`invalid` status, bounded reason, and trusted flag.
- Source elements, sources, knowledge objects, and confirmed owner Memory are checked against
  the candidate notebook/owner. Unsupported, missing, cross-notebook, cross-owner, rejected,
  deprecated, or legacy-unverified evidence fails closed.
- The owner-only Memory UI renders Agent identity, client request id, reason/task context, and
  every evidence identity/status/reason. Existing promotion sanitization remains unchanged and
  consumes only validated trusted evidence.

### Shared input limits

- Added one normalization/limit module used by Pydantic models and `MemoryService`, so direct
  service and MCP calls cannot bypass HTTP validation.
- Title/content are trimmed and must remain nonblank. Enforced caps: title 80 chars, content
  40,000 chars, 20 tags × 80 chars, reason 1,000 chars, task context 8,192 serialized UTF-8
  bytes, 50 evidence refs / 32,768 serialized UTF-8 bytes, and client request id 200 chars.
- API boundary violations return 422; service/internal callers fail closed with
  `MemoryInputError`.
- Frontend editor constraints and validation messages match the server's user-editable limits.

### Global Memory page

- `PaginatedMemories` now carries owner-wide total/pending counts and bounded notebook options.
- The store uses four fixed queries: owner aggregates, a notebook aggregate capped at 200,
  filtered count, and the requested page. Counts/options remain independent of active filters.
- The global page shows owner-wide totals and adds a functional notebook filter while preserving
  the existing request-epoch/session-abort guards.

### Token expiry

- The browser converts `datetime-local` to an ISO UTC instant; a deterministic UTC+8 test covers
  the non-UTC case.
- The backend rejects naive timestamps, normalizes aware timestamps to UTC `Z`, and treats
  malformed/legacy-naive stored expiries as expired instead of attaching server timezone.

### Answer save race

- The Memory store now rechecks current notebook owner/member access in the same
  `BEGIN IMMEDIATE` transaction that snapshots the answer and writes the Memory, initial
  revision, and provenance.
- Owner and read-only-member saves retain their prior semantics; concurrent membership
  revocation creates no partial Memory/revision/provenance rows.

## Architecture and compatibility review

- Product SQL remains in `MemoryStore`; `MemoryService` owns orchestration and shared validation.
- API SQLite calls still run through the existing threadpool boundary.
- No migration, schema-version change, in-process model dependency, or new MCP tool was added.
- Frozen OpenAPI contract fixtures were updated for the additive pagination response fields.
- README.md, README_zh.md, and AGENTS.md document the same new behavior and constraints.
- This is remediation of the already-recorded Memory feature, not a new spec completion; the
  shared completion ledger was intentionally left unchanged per the branch-task instruction.

## Final verification

`PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh` passed:

- Backend smoke and official seven-tool MCP smoke passed.
- Backend: `2918 passed, 1 skipped`.
- Frontend: `189 passed, 0 failed`.
- TypeScript `tsc --noEmit` and Next.js production build passed.

The initial unqualified `bash scripts/check.sh` invocation stopped before tests because the
machine's default `python3` lacked `markdown_it`; rerunning with the repository's canonical
dependency-complete interpreter, as required by AGENTS.md, produced the green result above.
The commit SHA is included in the branch handoff.
