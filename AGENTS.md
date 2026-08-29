# Agent Development Instructions

This file is the short operating entry point for coding agents in this repository. It
contains repository-wide working rules and routes agents to the current source of truth.
It must stay concise: product narratives, endpoint contracts, component state machines,
environment-variable catalogs, numeric rail tables, migration procedures, and incident
history belong in the owning documents below, not here.

## Start Every Task

1. Read the user request and inspect `git status --short` before editing. Existing changes
   belong to the user unless proven otherwise; preserve them and avoid unrelated rewrites.
2. Confirm that write work is happening in an isolated linked worktree and branch. If the
   current directory is already such a worktree, continue there. Read-only investigation
   does not require a new worktree.
3. Identify the owning document from the routing table and read the relevant section before
   changing behavior. Historical files under `docs/superpowers/` explain past decisions but
   are not the current contract unless a live document explicitly points to them.
4. Use `rg` / `rg --files` for discovery. Prefer focused edits and focused tests before the
   full verification gate.
5. Before running `npm install` in a worktree, inspect `frontend/node_modules` with
   `ls -l frontend/node_modules`. If it is a symlink into the main checkout, do not write
   through the shared dependency tree; copy it or install in the correct checkout when an
   independent dependency tree is required.

## Current Sources of Truth

| Change area | Read before editing | Authority |
| --- | --- | --- |
| Product behavior, UI behavior, HTTP/MCP contracts, exact limits | `docs/product-and-api.md` and `docs/product-and-api_zh.md` | Paired product/API reference |
| Runtime architecture, dependency direction, data flow, active debt | `architecture.md` | Runtime architecture |
| Installation, environment, model services, deployment settings | `docs/deployment-and-configuration.md` and `_zh.md` | Paired deployment reference |
| Operations, diagnostics, ingestion, migration execution, backfills | `docs/operations.md` and `_zh.md` | Paired operations reference |
| Development workflow, architecture guardrails, schema/migration authoring, tests, CI, PR policy | `docs/development.md` and `_zh.md` | Paired development reference |
| User-visible Chinese terminology | `docs/ui-vocabulary.md` | UI-copy vocabulary |
| Deployment extension authoring and operation | `docs/deployment-extensions-sop.md` and `_zh.md` | Paired extension SOP |
| External-Agent onboarding and token scopes | `docs/agent-mcp-memory-sop.md` and `_zh.md` | Paired Agent MCP SOP |
| Product specification and implemented status | `silicon_notebook_fangan.md` and `fangan_done.md` | Specification and completion ledger |
| Script usage | `scripts/README.md` | Command reference |

The root `README.md` / `README_zh.md` are concise entry points, not detailed contracts.
`CLAUDE.md` contains Claude Code-specific resident instructions; it is not a second product
or architecture source of truth. For agent-workflow constraints stated in both files,
`AGENTS.md` follows `CLAUDE.md`: if their wording or required behavior differs, `CLAUDE.md`
controls and `AGENTS.md` must be corrected. Carrier-specific commands apply only to the
carrier they name; shared product, architecture, and development contracts remain owned by
the canonical documents below.

## Repository-Wide Working Rules

### Scope and safety

- Make the smallest coherent change that fully satisfies the request. Do not modify unrelated
  files, discard user changes, or use destructive Git/filesystem commands to get a clean tree.
- Run GitHub-facing operations (for example, pushing branches and creating, inspecting, or
  merging pull requests and checking their CI) outside the sandbox; keep all other commands
  and file edits inside the sandbox.
- Do not infer authority for deployment, external messages, PR creation, merging, or other
  remote mutations from a local code-edit request.
- Use the agent's structured edit/write capability for file changes; do not replace whole
  files through shell redirection.
- Keep secrets, raw tokens, credentials, private paths, source content, prompts, evidence, and
  exception text out of logs and content-free telemetry.

### Production code

- Do not introduce result-changing literal slices or limits in production paths. Reuse a named
  protocol constant for invariants and a validated `Settings` field for deployment-tunable
  quality/cost budgets. Exact numeric rails live only in the paired product/API reference.
- User-authored data must never be silently truncated. Reject over-limit input through the
  shared backend/frontend rail, or disclose bounded projection explicitly when that is the
  documented public contract.
- Keep model-input truncation on its existing single configuration source so online, batch,
  and backfill paths cannot drift.
- Preserve backend/frontend parity for user-visible capabilities. A feature is not complete
  when only one side of its observable contract exists.
- Follow the dependency, ownership, extension, authorization, and state-owner boundaries in
  `architecture.md` and `docs/development*.md`; do not restate or fork those contracts here.

### Tests

- Ordinary unit and standard-gate tests must be hermetic: no host port binding and no ambient
  service dependency. Use pure functions, fakes, in-process clients, or self-contained
  subprocesses when the contract is intrinsically process-level.
- Test observable behavior and semantic identities, not source line numbers, textual offsets,
  copied implementations, or refactor-only golden snapshots.
- Do not disable committed tests with skip/xfail/todo/only. Add focused regression coverage for
  a bug fix and keep existing coverage reachable through the documented verification lanes.

### User-facing copy

- Apply `docs/ui-vocabulary.md` to JSX text, labels, titles, placeholders, accessibility text,
  toasts, and displayable backend `user_error(...)` messages. Internal identifiers remain
  unchanged in code and protocols.
- User-visible errors are Chinese and actionable. Diagnostic details that are not explicitly
  marked displayable remain internal.

### Interactive feedback

- Every control that performs an action must change visibly the moment it is pressed and return
  to its resting state on release. The baseline is one element-level `button:…:active` rule in
  `frontend/app/globals.css`; keep it on the element rather than on individual button classes so
  newly written buttons inherit it, and exclude disabled controls.
- Press feedback only answers "did the click register". Report the action's *outcome* on the
  control itself or immediately beside it, and clear that state on its own timer. A banner at the
  top of the page is not sufficient on its own: it scrolls out of view, sits far from the pointer,
  and repeats identical text on a second click, so a real effect reads as "nothing happened".
- Actions that keep running after the click must additionally disable or replace the control
  while in flight, so a returned POST cannot be resubmitted by repeated clicking.
- The selector, the property choices, and the gates that pin them live in `docs/development*.md`.

## Documentation Ownership

- Update every canonical document whose owned surface actually changed; one change may affect
  more than one surface. If a document has an English and Chinese pair, update both in the same
  change and keep them semantically aligned.
- Update the root READMEs only when their entry-point material changes: quick start, high-level
  capabilities/current boundaries, or documentation navigation.
- Update `AGENTS.md` only when repository-wide agent workflow, routing, safety, verification, or
  completion rules change. Update `CLAUDE.md` only when Claude Code-specific resident rules or
  its routing change. Do not copy ordinary product or architecture changes into either file.
- Keep detailed behavior in the canonical references; links and short summaries are preferred
  to duplicated prose. Tests must validate the owning document, not force details back into
  entry-point files.
- When a feature from `silicon_notebook_fangan.md` is genuinely complete, update the matching
  entry in `fangan_done.md`, remove it from the unfinished list, cite the spec section, and state
  only verified behavior. Do this only after the applicable standard gate passes.

## Verification and Handoff

Run focused tests while editing, then run the standard gate before claiming completion:

```bash
bash scripts/check.sh
```

The project commonly uses:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

Use `scripts/check_extended.sh` for the documented extended lane and
`scripts/check_postgres.sh` only with the explicit PostgreSQL test environment described in
`docs/development*.md`. Missing frontend dependencies are a failure, not permission to skip the
frontend lane. A read-only review inspects the diff and the submitter's verification evidence;
it does not mutate the tree or rerun the full gate unless explicitly requested. In the handoff,
report what changed, which checks ran, and any check that could
not run. Follow the PR/review/merge policy in `docs/development*.md` when the user has asked for
remote delivery.
