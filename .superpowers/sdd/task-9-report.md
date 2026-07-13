# Task 9 Report: Agent Memory Documentation and Final Verification

## Result

Documented and verified the completed Agent Memory system across the canonical
setup, product, architecture, and implementation-ledger documents.

- Added the official `mcp` Python SDK Memory smoke to `scripts/check.sh` through
  the configured `PYTHON_BIN` and backend `PYTHONPATH`.
- Synchronized `README.md`, `README_zh.md`, and `AGENTS.md` for the four-tab
  workspace, global/notebook Memory surfaces, two retrieval planes, privacy,
  lifecycle, Agent access, MCP transport/tools, and KG promotion governance.
- Added the implemented Agent Memory contract as §19 of
  `silicon_notebook_fangan.md` and a factual completed ledger entry in
  `fangan_done.md` only after the first full gate passed.
- Updated the live `architecture.md` and its contract test so current documents
  describe four tabs while the dated 2026-07-10 plan/spec retain their historic
  three-tab wording.

## Documented Product Contract

The documents now cover:

- Manual Ask answer → preview → user edit → confirm, including the deterministic
  question-title/cleaned-answer fallback when the preview model is unavailable.
- Creator-private Memory bound to exactly one notebook, with a global owner
  aggregate plus notebook count/tab, and notebook-deletion cascade warning.
- `candidate | confirmed | rejected | deprecated`; same-user/same-notebook
  scoped cross-Agent candidate sharing; confirmed-only formal Ask, notebook
  search, Deep Report, and `search_notebook_context`.
- Relevance before authority and the conflict order
  `candidate < personal source < confirmed Memory < base KG/base source`.
- Stable Agent profiles and one-time plaintext opaque tokens with exact scopes,
  default notebook, allowlist, expiry, disable/revoke behavior, and revalidation.
- Exact MCP tools: `list_notebooks`, `select_notebook`, `search_agent_memory`,
  `search_notebook_context`, `get_memory`, `ask_notebook`, and
  `propose_memory`; every new session explicitly selects a notebook.
- Local loopback HTTP at `http://127.0.0.1:8000/mcp`, remote HTTPS with
  `MCP_PUBLIC_URL`, Codex's bearer-token environment-variable command, and the
  current Claude Code HTTP/header command with the raw-header persistence warning.
- Confirmed-only creator proposal → admin review of sanitized extraction
  candidates and server-validated evidence → approval-time current status/access
  revalidation → dedupe/merge into one or more Base KG objects. The API/audit
  retains the complete `base_object_ids` without changing or exposing the
  private Memory.
- Recall@5/MRR/nDCG, no-Memory/KB-only/KB+confirmed A/B, and the three zero-leak
  counters for formal candidate, cross-user, and cross-notebook leakage.

## Debugging Record

The first post-documentation final gate exposed two failures in
`test_architecture_documentation.py`. Systematic inspection showed both were
stale static expectations from before Memory: the test required a three-tab
workspace and the `2026-07-12` ledger date. The production UI and current docs
correctly use four tabs and `2026-07-13`.

The contract test was narrowed correctly: current live docs assert the four-tab
contract, while the dated 2026-07-10 architecture plan/spec continue to assert
their historical three-tab text. `architecture.md` was synchronized as a live
architecture document. The focused contract suite then passed 14/14 before the
full final gate was rerun.

The independent review then found three documentation-contract gaps: the live
docs were not all guarded against a return to the three-tab workspace, one
product-spec sentence incorrectly implied that admin browses raw Memory
revision/provenance, and the promotion result was described as a singular Base
object even though one Memory proposal may emit multiple object types. A new
whitespace-insensitive live-section contract first failed (`1 failed, 14
passed`), then passed (`15 passed`) after all current documents were corrected.
Dated 2026-07-10 plan/spec text remains intentionally historical.

## Verification

Final fresh command:

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python3 bash scripts/check.sh \
  && (cd frontend && npm run build) \
  && git diff --check
```

Results:

- Official SDK Memory MCP smoke: passed (7 tools, session isolation, candidate
  plane isolation and same-owner/same-notebook cross-Agent recall).
- Focused live-document contract: RED `1 failed, 14 passed`; GREEN `15 passed`.
- Backend: `2904 passed, 1 skipped in 259.79s`.
- Frontend tests: `182 passed`, `0 failed`.
- TypeScript: `tsc --noEmit` passed.
- Production build inside `scripts/check.sh`: passed.
- Explicit second `npm run build`: passed.
- `git diff --check`: clean.

## Self-Review

- Compared every parent brief requirement against the final docs, including the
  exact seven tools, explicit notebook selection, exact Codex/Claude CLI syntax,
  Claude raw-header warning, scopes, expiry/revoke, deterministic fallback,
  deletion cascade, promotion privacy, evaluation, and zero-leak counters.
- Confirmed `backend/requirements.txt` already contains `mcp>=1.26.0`; no package
  installation or network mutation was performed.
- Confirmed no deleted web overview, Studio, Article, or KG candidate queue was
  presented as a current feature.
- Confirmed the only non-document changes are the offline check hook and the
  documentation contract test required to keep current live docs truthful.
- Inspected `git status`, `git diff --stat`, the full focused diff, and
  `git diff --check`; no unrelated workspace edits were found.

## Remaining Concerns

No known correctness blocker. Claude Code's explicit Authorization header may
be retained in local configuration, so the runbook intentionally recommends
least-privilege scopes, short expiry, local-config protection, and revocation/
rotation rather than claiming unsupported environment interpolation.
