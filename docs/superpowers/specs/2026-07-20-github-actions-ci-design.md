# GitHub Actions CI Design

## Goal

Add the repository's first GitHub Actions continuous-integration workflow
without creating a second definition of the test suite. Pull requests into
`master`, pushes to `master`, and explicit manual runs must install the
declared backend and frontend dependencies and execute the same complete
offline gate developers run locally: `scripts/check.sh`.

This change establishes an observable CI baseline only. It does not make the
new check mandatory in branch protection. Required-check enforcement will be
considered after the workflow has completed stable green runs on both a pull
request and `master`.

## Chosen Architecture

Use one `full-gate` job in `.github/workflows/ci.yml`.

The alternatives were three separate jobs matching the local lanes or a
Python/Node compatibility matrix. Separate jobs would duplicate environment
installation and runner allocation, while a version matrix would add cost
without serving the current local-beta compatibility target. A single job
keeps one authoritative verdict and delegates test parallelism to
`scripts/check.sh`, which already owns the `contracts`, `backend`, and
`frontend` lanes and their process cleanup.

The workflow must never enumerate individual test roots or repeat frontend
test/build commands. Changes to test coverage continue to happen only in the
repository gate scripts.

## Events and Concurrency

The workflow runs for:

- `pull_request` events targeting `master`;
- pushes to `master`;
- `workflow_dispatch` for a deliberate manual rerun.

There are no path filters. Documentation and policy files participate in
semantic contract tests, so a documentation-only change can still break the
complete gate.

Concurrency is scoped by workflow plus pull-request head branch or Git ref.
`cancel-in-progress: true` cancels a superseded run for the same pull request
or branch while allowing unrelated pull requests to run independently. This
limits wasted Actions time without serializing the repository.

## Security Boundary

The workflow uses the `pull_request` event, never `pull_request_target`.
Workflow permissions are explicitly limited to:

```yaml
permissions:
  contents: read
```

Checkout must not persist credentials because no later step performs a Git
write. The job does not read repository secrets, model credentials, or
deployment configuration. `scripts/check.sh` already clears model-provider
configuration and forces MinerU off, preserving its deterministic offline
boundary.

Only GitHub-maintained setup actions are used:

- `actions/checkout@v6`;
- `actions/setup-python@v6`;
- `actions/setup-node@v6`.

## Runner and Dependency Setup

The job uses the pinned `ubuntu-24.04` hosted image with:

- Python `3.13`, matching the current exercised backend baseline;
- Node.js `22`, matching the frontend type/development baseline;
- pip download caching keyed from `backend/requirements.txt`;
- npm download caching keyed from `frontend/package-lock.json`.

The caches contain package-manager downloads only. The workflow does not
cache `node_modules`, virtual environments, generated application state,
SQLite databases, or `.local` runtime artifacts.

Dependencies are recreated with:

```bash
python -m pip install -r backend/requirements.txt
npm ci --prefix frontend
```

The npm lockfile is authoritative for frontend installation. Backend
requirements remain governed by the repository's existing
`backend/requirements.txt` policy.

## Gate Execution and Resource Bounds

The final workflow step is:

```bash
bash scripts/check.sh
```

with:

```yaml
env:
  PYTHON_BIN: python
  BACKEND_PYTEST_WORKERS: "4"
```

The explicit worker count prevents the local Apple Silicon default of twelve
pytest workers from oversubscribing a GitHub-hosted runner. It changes only
parallelism, not test selection.

The job timeout is 20 minutes. The local warm-gate target remains under
60 seconds on the project's Apple Silicon development machine, but that
measurement is not a portable CI timeout: hosted runners must also install
dependencies and may have different CPU and filesystem performance.

Any installation, test, type-check, or production-build failure fails the
single `full-gate` job. The existing controller prints all three lane logs
before returning its aggregate exit status.

## Contract Tests

Add a YAML-structure contract test under `backend/tests` using the already
declared PyYAML dependency. Parse with a loader that preserves the literal
`on` key and assert semantic workflow invariants rather than line positions or
source slices:

- all three intended events exist and PR/push targets are `master`;
- `pull_request_target` is absent;
- permissions are exactly read-only repository contents;
- concurrency cancels superseded runs;
- the job uses the pinned runner and a finite 20-minute timeout;
- setup actions configure Python 3.13, Node 22, and dependency caches;
- dependency installation uses the committed requirement and lock files;
- the only repository gate invoked by the job is `scripts/check.sh`;
- `PYTHON_BIN` and the bounded pytest worker count reach the gate.

The test must not assert YAML line numbers, key order, formatting, or source
text offsets. It protects operational meaning while allowing ordinary
workflow refactoring.

## Documentation

Update `README.md`, `README_zh.md`, and `AGENTS.md` together:

- identify `CI / full-gate` as the GitHub-hosted form of `scripts/check.sh`;
- document its triggers, pinned runtime versions, and offline/no-secret
  boundary;
- distinguish the local 60-second warm target from the CI timeout;
- record that branch protection is intentionally deferred until stable green
  runs are observed.

## Rollout and Success Criteria

The implementation is successful when:

1. the new semantic workflow contract test first fails without the workflow
   and passes after it is added;
2. the complete local `scripts/check.sh` gate remains green;
3. the branch is pushed and the pull request's `CI / full-gate` run completes
   successfully on GitHub;
4. an independent review subagent finds no correctness, security, or
   maintainability blockers;
5. the pull request remains unmerged and branch protection remains unchanged.

After this PR is merged, observe at least one successful `master` run. Only
then should a separate, explicit repository-governance action make
`CI / full-gate` a required check.

## Non-Goals

- no branch-protection or ruleset mutation;
- no deployment, release, container, or artifact-publishing workflow;
- no operating-system or runtime-version matrix;
- no test sharding separate from the existing gate lanes;
- no network model calls or secret-backed integration tests;
- no hard 60-second timeout on GitHub-hosted runners.
