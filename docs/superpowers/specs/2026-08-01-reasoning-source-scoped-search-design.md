# Source-scoped evidence search for reasoning Ask

> **SUPERSEDED (2026-08-04) — this design was removed, not shipped as described.**
> Exact-equality resolution could not honor an abbreviation: asking about
> "pdagent" when the source is titled "PDAGENT-BENCH: …" resolved to zero
> matches, and because the design fails closed, an ordinary question died with
> a deterministic 422 that no retry could clear. Users already select sources
> themselves (the checkbox scope), so the model-side guess was removed whole:
> `source_refs`, `QueryIntentSourceScope`, the `source_scope_confirmation`
> gate, the signed preview capability, `evidence_scope.py`, and the
> `search_evidence` action are all gone. The retained contract is in
> `docs/product-and-api.md` § "Source scope is user-selected only". Everything
> below is kept as a historical record of the removed design.

- Date: 2026-08-01
- Status: superseded — removed 2026-08-04 (see banner above)
- Branch: `codex/reasoning-source-scope`
- Scope: Ask `reasoning` mode

## 1. Decision

Add one internal reasoning-Agent tool:

```json
{
  "tool": "search_evidence",
  "query": "What is the ICC2 equivalent of place_opt_design?",
  "source_refs": ["Innovus User Guide", "ICC2 Command Reference"]
}
```

Omitting `source_refs` searches every authorized participating source and preserves current behavior. A non-empty list is resolved by the server into an exact source set and creates a strict scope. An empty list is rejected rather than treated as an alias for all sources. Missing, ambiguous, unauthorized, deleted, or unmounted references fail closed and never fall back to the whole library.

This is an internal reasoning tool. It does not add a public MCP tool or change the External Agent tool set.

## 2. Product invariant

A selected scope is a retrieval contract, not a prompt hint. Once resolved, it is the maximum source set for the run. Later calls that omit a scope inherit it; no call may broaden it. Candidates gathered before the lock must be discarded or re-run within the scope. Synthesis, anchors, and citations perform a final scope check.

Authorization remains the outer boundary. The selected set is intersected with the current notebook and valid mounted-library participants.

### Architecture decision: scope is fixed before the run

The first selected scope may be created only by `/ask/intent`: preview resolves the user's explicit references, returns a display-safe snapshot, and requires confirmation before the durable run or any evidence access. A run therefore starts in exactly one state, `all` or `selected`. An `all` run cannot use a later `search_evidence` action to establish its first selected scope dynamically. Within a selected run, omitting `source_refs` means inherit; an explicit list may preserve or narrow the current set only. Resolution ambiguity or mismatch fails closed. Any channel that cannot prove source isolation is skipped with a visible reason while restricted.

## 3. Resolution semantics

The server resolves normalized exact display titles, original file names, and exposed stable source IDs. Case, Unicode, and surrounding whitespace may be normalized; fuzzy similarity is not authorization.

- zero matches: `not_found`;
- one match: resolved;
- multiple matches: `ambiguous` with a bounded, display-safe candidate list;
- deleted, revoked, or unmounted source: `unavailable`.

Every unresolved state stops scoped retrieval. It must not silently become an all-source search.

## 4. Internal contract

```text
search_evidence(
  query: string,
  source_refs?: non-empty list[string],
  channels?: "auto" | list["kg", "elements", "ppr", "exact"]
)
```

`channels="auto"` is the initial default. Existing model-independent limits still govern candidates, tokens, steps, and exact probes. The model supplies references; the server owns the canonical scope.

The run-local `ReasoningSourceScope` contains `mode`, resolution `status`, an allowed `(notebook_id, source_id)` set, and immutable display snapshots.

## 5. Deriving references from the question

The planner receives a bounded identity-only source catalog: display title, original file name, stable exposed ID/aliases. It receives no source text, summary, KG, or embedding. It emits `source_refs` only when the user explicitly refers to manuals, papers, files, or a demonstrative source set.

A later deterministic mention extractor may identify quoted/book-title spans, file extensions, and exact title fragments. The model decides semantic intent; the resolver decides uniqueness and authorization. Fuzzy candidates are clarification options only. Similarity between a domain/tool name and a document title must not create a restriction by itself.

Large catalogs must use bounded identity lookup. If the bounded result cannot prove uniqueness, the system reports ambiguity rather than treating a truncated prefix as complete.

## 6. Enforcement surface

One scope must constrain initial and added federated KG queries, element search, PPR seed/action, exact seed/action, neighbors, communities, follow-chain, collection enumeration, quota reranking, evidence hydration, synthesis, anchors, and citations.

Filtering should be pushed into bounded candidate generation so disallowed sources cannot occupy top-K. A graph channel that cannot safely honor the scope must be disabled in selected mode with a visible skip reason; running a whole-library graph and filtering only its output is not strict scoping. Multi-source KG objects retain only evidence from selected sources and are removed when none remains. SQLite and PostgreSQL must implement identical semantics. At repository boundaries, `source_ids=[]` always means the empty set.

## 7. API and UI

Persist an optional display-safe scope snapshot in `QueryIntentContract` / `AskResponse.intent`. Omission is backward-compatible and means all sources. The client does not establish authority by posting arbitrary IDs.

A resolved selected scope always enters intent confirmation, even when the question has no other ambiguity. The review shows “Only 2 sources” and their titles/file names. The live trace shows the confirmed scope, and the completed/reopened answer shows an expandable “Based on 2 specified sources” badge. Internal IDs and tokens are never displayed.

This protocol applies only to `reasoning`; `chunk` and experimental `graph` retain their current behavior.

## 8. Acceptance

Use three overlapping manuals A, B, and C with unique facts and citations. A scope of A+B must exclude C from every retrieval branch, reflection input, synthesis input, anchor, and citation. Duplicate or missing names fail closed. Revocation between confirmation and execution fails before evidence access. Omitting the scope preserves historical behavior byte-for-byte where applicable. Completed and reopened turns retain the same display snapshot, and SQLite/PostgreSQL agree.

## 9. Delivery tasks

1. Scope model, authorized identity resolver, and run guard.
2. `search_evidence` protocol plus initial and reflective execution.
3. Scope pushdown and final defense across retrieval paths.
4. Intent review, trace, and completed-answer UI.
5. Three-manual regression, adapter contracts, documentation, and full gates.

Each task receives a specification-conformance review and a code-quality review before the next task begins.
