# Plan: knowhow KG-node retrieval (default-off, real-machine-eval gated)

**Branch:** `claude/knowhow-kg-node-retrieval` (stacked on the #272 branch tip `4395933`; rebase onto master once #272 merges).
**Goal:** let knowhow cell KOs (`object_type = 列名`, ids `ko-kh-{sha1(table_id|column_name|value_key)[:32]}`) surface in the `reasoning`/`graph` **KG-node** retrieval path, so ask citations land on KG-node anchors that jump to the row drawer. Chunk mode already works (#272). **Everything behind one default-off flag**; turning it on / final tuning waits for real-machine recall+latency eval per the efficiency-first mandate.

## Verified ground truth (recon done 2026-07-16; a prior Explore agent misread master versions — these are re-verified against the live branch)

- `_retrieve_scored` — `backend/app/services/retrieval_candidates.py:690`. `_KG_TYPES=("claim","formula","procedure","concept")` :42. `type_list` filter at **:700** (`if t in _KG_TYPES` drops column-name types). `cand_sims` built by `_kg_object_candidates` :715 (ANN) / `{h:0.0}` FTS fallback :734. `id_filter = set(cand_sims.keys()) if cand_sims is not None else None` at **:738**, feeding per-type `_knowledge_objects` loop :739-740. **:778-779** `if cand_sims is not None: knowledge_sims = cand_sims` (an injected `ko_id→sim` becomes that KO's semantic score). Full-scan path (cand_sims None) builds `knowledge_sims` from the `knowledge_embeddings` matrix at **:786** (knowhow KOs absent → keyword-only there). Scorer loop :793-804 `score_knowledge(query, objs, t, ...)`; `_TYPE_WEIGHT.get(type, 0.5)` defaults gracefully for dynamic types.
- Cell KO id: `projection.py:111` `_cell_ko_id(table_id, column_name, val_key) -> f"ko-kh-{_h(table_id, column_name, val_key)[:32]}"`; `val_key = textops.value_key(text)` (:616); `object_type = column["name"]` (:625); KO **payload = {name, text, table_id, rows, column_id, column_name, [steps]}** (`_ko_object_row` :666-673) — carries `table_id`+`rows`.
- Knowhow **element** metadata: `projection.py:428` `metadata.knowhow = {table_id, row_id, column_id, role, column_name, row_title, content_hash}`; element id `element_id(row_id, column_id)` (:122); element `text` = the cell net text. Chunks are per-cell parts `chunk-kh-{_h(row_id)[:16]}-{part}` (:137/:448) with `element_ids=(eid,)` (:497). Hidden knowhow source: `source_type='knowhow'`, one per table (`ensure_hidden_source`).
- Anchor side ALREADY wired: `_knowhow_ref_from_payload(payload)` `evidence_context.py:43` (len(rows)==1 rule); `knowledge_context` populates `knowhow` :200-201; **`parse_anchors` reads `knowhow=context.get("knowhow")` at :33** → `AnswerAnchor.knowhow` (`schemas.py:561`). So the graph path only needs the id_map dict to carry a `knowhow` key.
- Gate ii render: `render_subgraph_context` `kg/graph_reason.py:239`, builds `id_map[key] = {object_id, object_type, name, ...}` at **:292**. Node meta comes from `graph_retrieval.py` `_load` where `payload = json.loads(r["payload"])` is available but only `{type,name,tier}` kept → enrich there to carry `knowhow` from payload.
- Cache: `knowledge_counts_cache.py` — `type_counts(db, nb, statuses)` → `{object_type:count}`, memoized on `kg_mutation_seq` via `_mutation_seq`; knowhow projection bumps it (`mark_unified_dirty`, projection.py:278) so it self-invalidates.
- Flag pattern: `config.py` pydantic-settings v2 — `Field(False, validation_alias="...")` (e.g. `kg_community_summary_enabled` :185). Add `knowhow_kg_node_retrieval_enabled: bool = Field(False, validation_alias="KNOWHOW_KG_NODE_RETRIEVAL_ENABLED")`.

## Invariants
- **Flag OFF ⇒ byte-identical behavior.** No new query, no type widening, no injection when off. Guard every new path on `settings.knowhow_kg_node_retrieval_enabled AND notebook-has-knowhow-types`.
- **No new embed, no new index write.** Gate 0 reuses existing knowhow chunk vectors; never emits knowledge_embeddings/kg_objects_fts for KOs (structural-only design holds).
- **[0,1]/tau score contract** unchanged; injected sims are chunk cosine sims already in [0,1].
- **Turning the flag ON by default is out of scope** — needs real-machine recall/latency. Ship default-off + tests proving the wiring.

## Tasks

### T1 — flag + gate i (type widening) — `retrieval_candidates.py`, `config.py`
- Add the flag to `config.py`.
- Add helper `_knowhow_object_types(self, notebook_id) -> tuple[str,...]`: read distinct object_types via `knowledge_counts_cache.type_counts` (usable statuses), subtract `_KG_TYPES`, return the knowhow (column-name) types; empty tuple ⇒ no knowhow ⇒ callers no-op. Result is cache-cheap (memoized on kg_mutation_seq).
- In `_retrieve_scored` :700, when flag on: `allowed = set(_KG_TYPES) | set(self._knowhow_object_types(notebook_id))` and filter `type_list` against `allowed` (default-types case unions the knowhow types too). Flag off ⇒ unchanged.
- Tests: unit — flag off leaves type_list == today; flag on + a knowhow table ⇒ column-name types present; non-knowhow notebook ⇒ unchanged even flag on.

### T2 — gate 0 (chunk-reverse-lookup sidecar) — `retrieval_candidates.py` (+ small bridge helper)
- New helper `_knowhow_ko_candidates(self, db, notebook_id, query, query_vector) -> dict[str,float]`:
  1. similarity over the hidden knowhow source's chunk vectors only (brute-force over that small set — scoped query; knowhow tables are <100 rows so cost is bounded; do NOT run a notebook-wide chunk retrieval). Reuse existing chunk-vector plumbing scoped by `source_id IN (SELECT id FROM sources WHERE notebook_id=? AND source_type='knowhow')`.
  2. for each hit chunk (top-k = chunk_recall): `element_ids[0]` → element metadata.knowhow `{table_id, column_name}` + element `text` → `ko_id = _cell_ko_id(table_id, column_name, textops.value_key(text))`.
  3. accumulate `{ko_id: max(sim)}`.
- Inject in `_retrieve_scored`: when flag on and helper returns non-empty, **union into `cand_sims` before :738** on the bounded path; on the full-scan path (cand_sims None) merge the sims into `knowledge_sims` after :786 (so knowhow KOs get a semantic score there too) — do NOT flip cand_sims to non-None on the full-scan path (would wrongly bound doc-KO retrieval). Pair with T1 (types must be in type_list or `_knowledge_objects` won't fetch them).
- Efficiency: helper only runs when `_knowhow_object_types` non-empty. One scoped small vector query per retrieval.
- Tests: unit the bridge (seed a knowhow table via real projection with FakeEmbedder, assert a query returns the expected `ko_id`s with sims); integration forcing the bounded path (build a scale index or the FTS fallback) asserting a knowhow KO appears in `_retrieve_scored` results with object_type=列名 when flag on, absent when off.

### T3 — gate ii (graph node knowhow wiring) — `graph_retrieval.py`, `kg/graph_reason.py`
- In `graph_retrieval.py` `_load` (where payload is parsed), when the KO payload has `table_id`+`rows`, carry `knowhow: _knowhow_ref_from_payload(payload)` (or the raw `{table_id, rows}` and compute at render) into the node meta. Reuse `evidence_context._knowhow_ref_from_payload` (len(rows)==1 rule).
- Thread it through `build_rx_graph` node payload → `render_subgraph_context` id_map dict at :292 (`"knowhow": node.get("knowhow")`). `parse_anchors` already surfaces it.
- Tests: `render_subgraph_context` emits `knowhow` for a single-row knowhow KO node; graph-mode anchor carries `AnswerAnchor.knowhow`. Flag-gate consistent with T1/T2 (graph node face only shows knowhow KOs once they're retrievable — but the render wiring itself is null-safe and can be unconditional; keep it behind the flag for a clean single switch).

### T4 — grounded e2e — `test_knowhow_citation.py` (+ maybe `test_reasoning_retrieval.py`)
- Reuse `test_knowhow_retrieval.py`'s `imported` fixture (real projection → real column-name KOs) + `test_reasoning_retrieval.py`'s pattern of driving `_retrieve_scored`/reasoning and asserting anchors.
- Assert: flag ON ⇒ a reasoning/graph answer citing a knowhow cell yields an `AnswerAnchor` with `object_type=列名` and a non-None `knowhow` ref (single-row row); flag OFF ⇒ no such node anchor (chunk anchor still works — unchanged). Force the bounded retrieval path so gate 0's semantic injection is exercised.

## Orchestration
- T1+T2 share `_retrieve_scored` ⇒ ONE agent owns them serially (core retrieval).
- T3 is separate files ⇒ parallel agent.
- T4 (e2e) after T1-T3 land ⇒ controller (me) writes/runs it + full review + verify.
- Global: flag-off byte-identity test is mandatory; run full backend suite; frontend untouched. PR stacked on #272, default-off, with a "requires real-machine eval before default-on" note + the retrieval limitation this closes (only when enabled).
