# Task 7 Report: Scoped Streamable HTTP MCP

## Result

Implemented the stateful Streamable HTTP MCP endpoint at `/mcp` with exactly
seven public tools:

- `list_notebooks`
- `select_notebook`
- `search_agent_memory`
- `search_notebook_context`
- `get_memory`
- `ask_notebook`
- `propose_memory`

Agent Bearer tokens are resolved through the existing Memory service. The MCP
SDK binds each transport session to the authenticated non-secret token id, and
the selected notebook is stored on that MCP session object rather than in
process-global state. Every tool refreshes the live token/profile state; data
tools require an explicit selection and then recheck scope, notebook allowlist,
and current notebook access.

The two retrieval planes remain separate:

- `search_agent_memory` returns confirmed Memory and, only with
  `memory:read_candidates`, same-owner/same-notebook candidates across Agent
  profiles. Candidate results are explicitly marked unconfirmed.
- `search_notebook_context` reuses notebook search and only exposes confirmed
  Memory. Candidate Memory cannot enter it or `ask_notebook`.

`propose_memory` reuses MemoryService idempotency and creates only `candidate`
rows with notebook, owner, Agent profile, and `client_request_id` provenance.
The candidate is immediately recallable in the Agent plane. `ask_notebook`
reuses the existing Ask implementation and exposes only `chunk` and
`reasoning`; experimental `graph` is rejected.

## Security and Transport

- Missing, expired, revoked, tampered, or profile-disabled Agent tokens fail
  authentication.
- Stateful sessions are credential-bound by placing the non-secret token id in
  the SDK-compared `client_id`; the SDK does not compare `AccessToken.token`.
- Origin and Host validation use the SDK transport-security middleware.
- Non-loopback deployment configuration fails unless `MCP_PUBLIC_URL` is
  HTTPS; local loopback HTTP remains available for local Agent clients.
- Runtime ASGI requests independently require `scope.scheme == "https"` for
  non-loopback clients. Forwarded headers are not trusted by the adapter.
- Raw Agent tokens are never stored in MCP session state, tool results, or
  logs. Only the token id is passed to the SDK's authenticated user identity.
- Result count, text, tags, nested provenance, complete `get_memory` responses,
  Ask answers, and Ask anchor output are bounded. Retrieved text is labelled
  untrusted evidence/data, not an instruction.
- FastAPI explicitly composes the MCP session-manager lifespan because mounted
  Starlette application lifespans do not start automatically.

## Architecture

Added the consumer-specific `McpMemoryRepository` Protocol. The MCP adapter
contains no SQL and does not reach private repository/runtime state. Existing
public facade delegates are reused, with two new one-hop delegates:
`agent_memory_hits()` calls the established two-plane MemoryRetriever, while
`refresh_agent_principal()` reloads live token/profile, scope, and notebook
allowlist state after transport authentication. The initialization ContextVar
contributes only the session-bound token id.

Synchronous repository/model work runs in AnyIO worker threads, with the
authenticated owner request context scoped and reset inside each worker call.

## TDD Evidence

The official-client RED run failed all five initial cases because `/mcp` and
the tools did not exist. It covered the exact public tool list, required and
session-scoped notebook selection, cross-profile candidate recall without
notebook-plane/cross-user leakage, live revoke/membership loss, and unsafe
remote plain HTTP rejection.

Further contract tests cover all seven tools, allowlist rejection, Agent
candidate `get_memory`, user-confirmed transition into notebook context,
formal Ask reuse, experimental graph rejection, least-privilege scope
behavior, Origin/auth rejection, and bounded output. A final budget RED failed
because `_bounded()` did not yet accept a per-response budget; the Ask anchor
projection now uses a smaller aggregate budget.

Independent review produced three additional protocol RED failures:

- a lower-scope token for the same Agent profile reused an initialized session
  because the SDK compares client/issuer/subject rather than token value;
- remote plain-HTTP ASGI traffic was accepted when startup configuration named
  an HTTPS public URL;
- `get_memory` returned unbounded individual tag strings.

The focused run showed `3 failed, 8 passed`; the live-principal service test
also failed first because no refresh boundary existed. The fixes bind sessions
to token id, refresh every tool from live storage, validate actual ASGI scheme
and client address, and enforce an exact serialized `get_memory` budget. A
same-`client_request_id` retry test also confirms `propose_memory` returns the
same Memory id without inserting a duplicate.

## Verification

- Official MCP + Agent-token focused tests: `20 passed in 3.77s`.
- Repository/architecture guards: `49 passed in 6.23s`.
- Official-client offline smoke:
  `memory MCP smoke: OK (7 tools, session isolation, candidate plane isolation)`.
- Related Auth/Agent-token/Memory/Ask regression: `199 passed in 17.30s`.
- `git diff --check`: clean.
- Exact backend suite: `2890 passed, 1 skipped in 251.81s`.

The first exact backend run produced `2883 passed, 1 skipped, 3 failed`; all
three failures identified the same new private `repository()._runtime` access
in the MCP dependency. That access was removed in favor of the public
consumer-specific Protocol. The architecture and caller guards then passed.

## Self-Review

- Confirmed the selected notebook lives on `Context.session` and two sessions
  using the same token do not share selection.
- Confirmed a different token for the same profile cannot reuse that session,
  and initialization-time scopes/allowlists are not used for authorization.
- Confirmed every data operation carries the selected notebook into existing
  services and rechecks live authorization.
- Confirmed remote HTTP is rejected from the ASGI scope while remote HTTPS and
  loopback HTTP remain usable.
- Confirmed candidates remain same-owner/same-notebook Agent evidence until
  user confirmation, and rejected/deprecated Memory cannot be read.
- Confirmed no MCP write tool can confirm, reject, deprecate, delete, or
  promote Memory.
- Confirmed the public tool list contains exactly seven entries.
- Confirmed no product SQL or private repository/runtime access was added to
  the MCP adapter.

## Remaining Concerns

No known correctness blocker. The mounted SDK endpoint redirects `/mcp` to
`/mcp/`; the official Streamable HTTP client follows the redirect and the
contract smoke exercises the documented `/mcp` URL. Task 9 will document local
and HTTPS deployment configuration for Claude Code and Codex.
