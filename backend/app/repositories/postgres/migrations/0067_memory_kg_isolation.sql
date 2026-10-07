-- Ruling M1 (2026-09-29): knowledge-graph content derived from a member's
-- Memory belongs to that member only and is structurally isolated from every
-- shared derived product. This migration cleans the rows built before the
-- write-side guards existed and queues the affected notebooks for the
-- isolated rebuild. Mirrors SQLite v87 (_migration_87): same order, same
-- semantics; SQLite rewrites the evidence JSON in Python.
--
-- "Memory-derived" has ONE definition: app/repositories/postgres/memory_sql.py.
-- A migration is frozen SQL, so the fragments below are inlined verbatim
-- (memory_derived_object on alias ko, memory_derived_relation on kr, the
-- source-type predicate on s.source_type, cluster_seed_object_id on
-- c.canonical_a / c.canonical_b; the
-- promotion selection of step 3 is the predicate the write-side task handed
-- over, with memory_derived_object('ko') inside it);
-- backend/tests/test_memory_isolation_migration.py asserts that each inlined
-- fragment equals what memory_sql renders today for the same alias, so a
-- later change to the definition shows up as a failing test, not as a silent
-- divergence between this file and the live readers.
--
-- Deterministic data operations only: no model call, no clock-dependent
-- choice, no randomness. The cleanup is scoped to the notebooks that hold at
-- least one Memory source (the affected set F). A public library CAN be in
-- F: a notebook may have become a public library after a Memory source was
-- created in it (memory_service leaves such a source in place). The only
-- rows outside F the migration writes are the markers of G (step 1): every
-- other notebook with clusters, public libraries included, marked 2 for the
-- post-readiness dangling-seed check (a notebook whose last Memory was
-- deleted before the upgrade may still carry a cluster, a description or a
-- community text made while it was a member); nothing of G is cleaned.
--
-- Cost, stated as measured (PostgreSQL 16, a 2M-object public library,
-- 3.1M objects / 15.6M mention rows / 3.9M co-mention rows in all; the
-- report's worlds B, C and D and test_memory_isolation_migration_pg.py):
--   * every statement that reaches a WIDE table (knowledge_objects,
--     concept_clusters, the community-member lookup) is driven from the small
--     working tables below (the Memory sources, M, A) through the index named
--     next to it; the EXPLAIN pins hold that;
--   * the NARROW derived tables that have no index on the side a delete keys
--     on (mention_edges by concept, canonical_relations by target, the
--     co-mention pairs) are read ONCE in a hash join against A u M: those
--     statements cost in proportion to the whole table, not to F (mention
--     edges by concept: 3.0 s at 15.6M rows, the slowest statement);
--   * the evidence scan of step 9 reads every evidence item of each notebook
--     in mki_scan (F's notebooks that are not public libraries, and F's public
--     libraries whose reverse index is not attested complete): in proportion
--     to the largest such notebook (1M objects x 8 evidence items: 2.3 s;
--     reading all evidence of a 2M-object library: 13.5 s);
--   * G costs one index probe of concept_clusters per notebook.
-- The whole transaction measured 17.7-29.0 s on a loaded server; no single
-- statement came near the 30 s statement_timeout.
--
-- Idempotent: re-executing this file changes nothing. Every DELETE and the
-- evidence rewrite only select rows that still reference Memory, the
-- promotion rejection only selects proposals that are still open, and every
-- whole-notebook step (the precompute/checkpoint/scratch deletes of step 7
-- and the state change of step 12) runs only on the run that adds the marker
-- column (mki_run.first_run), so a re-run after the rebuild neither deletes
-- what the rebuild regenerated, nor re-queues a finished notebook, nor bumps
-- its sequences again.
--
-- Locks (the migrator runs this whole file in ONE transaction, so every lock
-- below is held until COMMIT):
--   * CREATE TEMP TABLE ... AS SELECT: ACCESS SHARE on the tables read;
--     readers and writers are never blocked by it.
--   * each DELETE / UPDATE: ROW EXCLUSIVE on its table plus row locks on the
--     rows it removes or rewrites -- only rows that reference Memory, so a
--     concurrent writer is blocked only if it touches exactly those rows.
--   * adding the marker column to unified_kg_state (constant DEFAULT 1):
--     ACCESS EXCLUSIVE on unified_kg_state (one row per notebook). A constant
--     default is metadata-only on PostgreSQL 11+ (no table rewrite). It is
--     deliberately the LAST group of statements (after the summary line), so
--     that lock is held only for the two small statements after it and the
--     commit; the pool's session lock_timeout (POSTGRES_LOCK_TIMEOUT_SECONDS,
--     default 5 s) makes a blocked ALTER fail loudly (startup reports "not
--     ready" and the operator retries) instead of queueing every later reader
--     of the table behind it. Every statement also runs under the pool's
--     statement_timeout (POSTGRES_STATEMENT_TIMEOUT_SECONDS, default 30 s).

-- 0. Is this the run that adds the marker column?
CREATE TEMP TABLE mki_run ON COMMIT DROP AS
SELECT NOT EXISTS (
  SELECT 1 FROM information_schema.columns
  WHERE table_schema = current_schema()
    AND table_name = 'unified_kg_state'
    AND column_name = 'memory_isolation_version'
) AS first_run;

-- 1. Memory sources (memory_sql.memory_source_type_predicate('s.source_type'))
--    with their owner (the memory item's created_by; NULL for an orphan
--    Memory source), and the affected notebooks F. sources is small (one row
--    per document or confirmed memory).
CREATE TEMP TABLE mki_ms ON COMMIT DROP AS
SELECT s.id, s.notebook_id, mi.created_by AS owner
FROM sources s LEFT JOIN memory_items mi ON mi.id = s.memory_id
WHERE s.source_type = 'memory';
ANALYZE mki_ms;

CREATE TEMP TABLE mki_f ON COMMIT DROP AS
SELECT DISTINCT notebook_id FROM mki_ms;
ANALYZE mki_f;

--    G: the notebooks OUTSIDE F that have any cluster row -- public
--    libraries included (a notebook may have held Memory before it became
--    a public library, and lost its last Memory since). Deleting or
--    re-extracting a Memory removes only its member rows, so a cluster whose
--    canonical id / name / description came from that Memory, and the
--    communities, mention rows and merge candidates made while it was a
--    member, can survive after the notebook's last Memory is gone. Telling
--    such a notebook apart reads every cluster row of it (a 1M-object
--    notebook measured 18 s on a loaded server), which must not run inside
--    this startup-blocking transaction; so the migration only marks G with
--    marker 2 ("isolation not yet checked", one index probe of
--    concept_clusters per notebook through idx_clusters_nb) -- inserting the
--    state row where there is none (a notebook copy has none by design) --
--    and the post-readiness worker checks each one in bounded pages
--    (MemoryIsolationStore.seed_check_signal): any signal -> queued for the
--    isolated rebuild (marker 0), none -> 1. Nothing of G is cleaned here.
CREATE TEMP TABLE mki_g ON COMMIT DROP AS
SELECT n.id AS notebook_id
FROM notebooks n
WHERE n.id NOT IN (SELECT notebook_id FROM mki_f)
  AND EXISTS (SELECT 1 FROM concept_clusters cc WHERE cc.notebook_id = n.id);

-- 2. M: Memory-derived objects in F (memory_sql.memory_derived_object('ko')),
--    reached from the Memory sources through idx_knowledge_objects_source.
CREATE TEMP TABLE mki_m ON COMMIT DROP AS
SELECT ko.notebook_id, ko.id
FROM mki_ms
JOIN knowledge_objects ko ON ko.source_id = mki_ms.id
WHERE ko.notebook_id IN (SELECT notebook_id FROM mki_f)
  AND EXISTS (SELECT 1 FROM sources ds WHERE ds.id = ko.source_id AND ds.source_type = 'memory');
ANALYZE mki_m;

--    A: every canonical id, in any generation (published or building, see
--    0051_derived_generation.sql) and any object type, whose cluster holds an
--    M member (reached from M through idx_clusters_member). Such an id may
--    have been minted from the Memory object's own seed (K-<seed>), and the
--    cluster's canonical_name / canonical_description are copied onto every
--    member row, so the WHOLE cluster goes, not only the Memory member row.
CREATE TEMP TABLE mki_a ON COMMIT DROP AS
SELECT DISTINCT cc.notebook_id, cc.canonical_id
FROM mki_m
JOIN concept_clusters cc
  ON cc.member_object_id = mki_m.id AND cc.notebook_id = mki_m.notebook_id;
ANALYZE mki_a;

--    A u M as one reference list (derived layers key on canonical ids; an
--    unclustered object's canonical id is its own object id).
CREATE TEMP TABLE mki_am ON COMMIT DROP AS
SELECT notebook_id, canonical_id AS ref FROM mki_a
UNION
SELECT notebook_id, id AS ref FROM mki_m;
ANALYZE mki_am;

-- 3. Promotion proposals still OPEN (the schema's statuses are proposed |
--    under_review | approved | rejected) that were made through the generic
--    knowledge-object path (propose_promotion: object_type is the object's KG
--    type, object_id a knowledge object id) for a Memory-derived object are
--    REJECTED, not deleted, so the curator queue keeps its audit trail: the
--    same terminal state the approval-time refusal writes (reason =
--    memory_kg_isolation.MEMORY_PROMOTION_REJECTED_REASON, pinned by
--    test_memory_isolation_migration.py; reviewed_by empty because no
--    reviewer acted). Proposals made through the creator-only Memory path
--    (propose_memory_promotion: object_type 'memory', object_id a memory item
--    id) are legitimate and stay. Approved ones are left alone (their public
--    copy is the deployment owner's decision; checkup H10 counts them). The
--    migration never deletes a knowledge object, so no proposal is left
--    pointing at a removed row. promotion_candidates is a queue (small);
--    idx_promotion_status.
CREATE TEMP TABLE mki_promo ON COMMIT DROP AS
SELECT pc.id FROM promotion_candidates pc
WHERE pc.object_type <> 'memory'
  AND pc.status IN ('proposed', 'under_review')
  AND EXISTS (
    SELECT 1 FROM knowledge_objects ko
    WHERE ko.id = pc.object_id AND ko.notebook_id = pc.notebook_id
      AND EXISTS (SELECT 1 FROM sources ds WHERE ds.id = ko.source_id AND ds.source_type = 'memory'));

UPDATE promotion_candidates pc
SET status = 'rejected',
    reason = 'memory_derived_object',
    reviewed_by = '',
    updated_at = now()
WHERE pc.id IN (SELECT id FROM mki_promo);

-- 4. Merge candidates and conflict candidates that name A u M. A node
--    conflict refers to object ids, an edge conflict to relation ids
--    (memory_sql.memory_derived_relation('kr')); resolved rows go too because
--    their rationale / resolved_payload quote the Memory side. Before they
--    go, the APPLIED node conflicts that changed a shared object on behalf of
--    a Memory one (resolution 'modify' merged a payload into the shared side,
--    or 'discard' set the shared side to status 'conflict'), and the applied
--    edge conflicts that rejected a shared relation, are counted for the
--    summary line: the migration does not guess the shared side's previous
--    payload/status. The rows are deleted below, so the read-only SQL in
--    docs/operations.md, run BEFORE upgrading, is the only way to list them.
--    Both candidate tables are review queues (small); idx_conflict_candidates_nb_status.
CREATE TEMP TABLE mki_conflict_shared ON COMMIT DROP AS
SELECT k.id
FROM kg_conflict_candidates k
WHERE k.notebook_id IN (SELECT notebook_id FROM mki_f)
  AND k.kind = 'node' AND k.status = 'applied'
  AND (
    (k.resolution = 'modify'
     AND (k.left_ref IN (SELECT id FROM mki_m) OR k.right_ref IN (SELECT id FROM mki_m))
     AND (CASE WHEN k.winner_ref IN (k.left_ref, k.right_ref) THEN k.winner_ref
               ELSE k.left_ref END) NOT IN (SELECT id FROM mki_m))
    OR (k.resolution = 'discard'
     AND k.winner_ref IN (k.left_ref, k.right_ref)
     AND k.winner_ref IN (SELECT id FROM mki_m)
     AND (CASE WHEN k.winner_ref = k.left_ref THEN k.right_ref
               ELSE k.left_ref END) NOT IN (SELECT id FROM mki_m))
  );

--    The edge arm: an applied 'discard' whose winner is a Memory relation
--    (memory_sql.memory_derived_relation('kr')) set the shared loser
--    relation's review_status to 'rejected'; edge 'modify' is a no-op.
CREATE TEMP TABLE mki_conflict_shared_edges ON COMMIT DROP AS
SELECT k.id
FROM kg_conflict_candidates k
WHERE k.notebook_id IN (SELECT notebook_id FROM mki_f)
  AND k.kind = 'edge' AND k.status = 'applied' AND k.resolution = 'discard'
  AND k.winner_ref IN (k.left_ref, k.right_ref)
  AND EXISTS (SELECT 1 FROM knowledge_relations kr
              WHERE kr.id = k.winner_ref
                AND EXISTS (SELECT 1 FROM sources ds WHERE ds.id = kr.source_id AND ds.source_type = 'memory'))
  AND EXISTS (SELECT 1 FROM knowledge_relations kr
              WHERE kr.id = (CASE WHEN k.winner_ref = k.left_ref THEN k.right_ref ELSE k.left_ref END)
                AND NOT EXISTS (SELECT 1 FROM sources ds WHERE ds.id = kr.source_id AND ds.source_type = 'memory'));

--    Merge candidates name A u M, or a unique-seed sentinel minted from an M
--    object (<prefix>~<object id>, prefixes K- / KL- / KF- / KP-: the same
--    decoding as memory_sql.cluster_seed_object_id) that no cluster row
--    carries once the Memory's clusters are gone. Bridge ids derived from a
--    Memory object's NAME need kg_merge's normaliser and are purged by the
--    rebuild worker right before each notebook's rebuild
--    (app/services/memory_isolation_rebuild.py).
DELETE FROM concept_merge_candidates c
WHERE c.notebook_id IN (SELECT notebook_id FROM mki_f)
  AND (EXISTS (SELECT 1 FROM mki_am WHERE mki_am.notebook_id = c.notebook_id AND mki_am.ref = c.canonical_a)
       OR EXISTS (SELECT 1 FROM mki_am WHERE mki_am.notebook_id = c.notebook_id AND mki_am.ref = c.canonical_b)
       OR (CASE WHEN substr(c.canonical_a, 1, 6) = 'K-~ko-' THEN substr(c.canonical_a, 4) WHEN substr(c.canonical_a, 1, 1) = 'K' AND substr(c.canonical_a, 3, 5) = '-~ko-' THEN substr(c.canonical_a, 5) END)
          IN (SELECT id FROM mki_m)
       OR (CASE WHEN substr(c.canonical_b, 1, 6) = 'K-~ko-' THEN substr(c.canonical_b, 4) WHEN substr(c.canonical_b, 1, 1) = 'K' AND substr(c.canonical_b, 3, 5) = '-~ko-' THEN substr(c.canonical_b, 5) END)
          IN (SELECT id FROM mki_m));

DELETE FROM kg_conflict_candidates k
WHERE k.notebook_id IN (SELECT notebook_id FROM mki_f)
  AND (
    (k.kind = 'node' AND (k.left_ref IN (SELECT id FROM mki_m) OR k.right_ref IN (SELECT id FROM mki_m)))
    OR (k.kind = 'edge' AND EXISTS (
      SELECT 1 FROM knowledge_relations kr
      WHERE kr.id IN (k.left_ref, k.right_ref)
        AND EXISTS (SELECT 1 FROM sources ds WHERE ds.id = kr.source_id AND ds.source_type = 'memory')))
  );

-- 5. Mention bridge. A co-mention pair between two shared concepts that was
--    bridged by a Memory claim carries that claim in its count, so pairs
--    bridged by an M claim go first (while the mention edges still exist),
--    then every pair and edge that names A u M. Each statement is driven from
--    a working table:
--      * pairs by canonical_a: pk_concept_comentions (notebook_id, canonical_a);
--      * pairs by canonical_b: idx_comentions_nb_b (notebook_id, canonical_b);
--      * pairs bridged by an M claim: the M claim's mention rows through
--        pk_mention_edges (notebook_id, claim_object_id), then the pair's PK;
--      * edges of an M claim: pk_mention_edges (notebook_id, claim_object_id);
--      * edges to an A u M concept: mention_edges has no concept index, so
--        this one reads the mention rows once and hash-probes A u M (cost in
--        proportion to mention_edges, measured with a 10M-row table below 30 s).
DELETE FROM concept_comentions cm
USING (
  SELECT DISTINCT e1.notebook_id, e1.concept_canonical_id AS a, e2.concept_canonical_id AS b
  FROM mki_m
  JOIN mention_edges e1
    ON e1.notebook_id = mki_m.notebook_id AND e1.claim_object_id = mki_m.id
  JOIN mention_edges e2
    ON e2.notebook_id = e1.notebook_id AND e2.claim_object_id = e1.claim_object_id
) br
WHERE cm.notebook_id = br.notebook_id AND cm.canonical_a = br.a AND cm.canonical_b = br.b;

DELETE FROM concept_comentions cm
USING mki_am
WHERE cm.notebook_id = mki_am.notebook_id AND cm.canonical_a = mki_am.ref;

DELETE FROM concept_comentions cm
USING mki_am
WHERE cm.notebook_id = mki_am.notebook_id AND cm.canonical_b = mki_am.ref;

DELETE FROM mention_edges me
USING mki_m
WHERE me.notebook_id = mki_m.notebook_id AND me.claim_object_id = mki_m.id;

DELETE FROM mention_edges me
USING mki_am
WHERE me.notebook_id = mki_am.notebook_id AND me.concept_canonical_id = mki_am.ref;

-- 6. Canonical relations touching A u M: by source through
--    pk_canonical_relations (notebook_id, canonical_src); by target (no
--    index) with one read of the table hash-probing A u M.
DELETE FROM canonical_relations cr
USING mki_am
WHERE cr.notebook_id = mki_am.notebook_id AND cr.canonical_src = mki_am.ref;

DELETE FROM canonical_relations cr
USING mki_am
WHERE cr.notebook_id = mki_am.notebook_id AND cr.canonical_tgt = mki_am.ref;

-- 7. Communities with an A u M member are removed whole (their member_ids
--    list, title, summary and findings may all carry the Memory side), with
--    all of their member rows (idx_commmem_nb_can to find them,
--    pk_community_members / pk_communities to delete); the other communities
--    stay until the rebuild. On the first run only, the quality-analysis
--    precompute of F (ledger plus the two detail tables, which the ledger
--    rule makes invisible anyway, and kg_source_profiles holds
--    per-Memory-source rows), the rebuild checkpoints of F (merge review
--    verdicts carry canonical names) and the clustering scratch rows of F
--    (per-run seeds and canonical names; a finished run clears its own, so
--    only an interrupted run leaves any, and no reader outside that run ever
--    reads them) go entirely -- each through its notebook_id index.
CREATE TEMP TABLE mki_comm ON COMMIT DROP AS
SELECT DISTINCT cm.community_id
FROM mki_am
JOIN community_members cm ON cm.notebook_id = mki_am.notebook_id AND cm.canonical_id = mki_am.ref;
ANALYZE mki_comm;

DELETE FROM community_members cm
USING mki_comm
WHERE cm.community_id = mki_comm.community_id;
DELETE FROM communities c
USING mki_comm
WHERE c.id = mki_comm.community_id;

DELETE FROM kg_analysis_artifacts
WHERE (SELECT first_run FROM mki_run) AND notebook_id IN (SELECT notebook_id FROM mki_f);
DELETE FROM kg_community_edges
WHERE (SELECT first_run FROM mki_run) AND notebook_id IN (SELECT notebook_id FROM mki_f);
DELETE FROM kg_source_profiles
WHERE (SELECT first_run FROM mki_run) AND notebook_id IN (SELECT notebook_id FROM mki_f);
DELETE FROM kg_rebuild_checkpoint
WHERE (SELECT first_run FROM mki_run) AND notebook_id IN (SELECT notebook_id FROM mki_f);
DELETE FROM kg_cluster_scratch
WHERE (SELECT first_run FROM mki_run) AND notebook_id IN (SELECT notebook_id FROM mki_f);
DELETE FROM kg_canonical_scratch
WHERE (SELECT first_run FROM mki_run) AND notebook_id IN (SELECT notebook_id FROM mki_f);

-- 8. The clusters themselves (whole clusters, every generation):
--    idx_clusters_nb_canonical (notebook_id, canonical_id).
DELETE FROM concept_clusters cc
USING mki_a
WHERE cc.notebook_id = mki_a.notebook_id AND cc.canonical_id = mki_a.canonical_id;

-- 9. Mixed evidence. A shared (non-Memory) object that a manual merge folded
--    Memory evidence into loses those evidence items and their reverse-index
--    rows. An object whose primary source IS Memory keeps whatever shared
--    evidence was merged into it: it stays private to the Memory's owner. It
--    loses, though, the items of ANOTHER member's Memory (a pre-upgrade
--    cross-owner merge) and of a Memory whose owner cannot be determined
--    (orphan Memory source): only its owner reads it, and that owner may not
--    read another member's Memory (same rule as E4-3's merge refusal).
--    An array item that is not an object (legacy string evidence) has no
--    source_id (->> answers NULL) and is always kept.
--    Only objects of F are touched: an object in another notebook (a public
--    library's approved promotion copy) keeps its evidence and its
--    reverse-index rows alike.
--    Shared candidates, two ways (UNION):
--      * the evidence reverse index knowledge_object_sources
--        (idx (source_id, object_id)) from each Memory source, within the
--        source's own notebook. The reverse index is authoritative only
--        where it is ATTESTED complete: unified_kg_state.source_index_backfilled
--        = 1 and no source_index_backfills row other than 'complete' (the
--        readers' own rule, knowledge_store's "authoritative" branch: a
--        historical notebook is lazily backfilled, and a forced backfill
--        empties the notebook's rows first and refills them in batches, so an
--        interrupted one leaves only a part);
--      * the evidence itself, for the notebooks of mki_scan: F's notebooks
--        that are NOT public libraries (where Memory is actually used), and
--        F's public libraries whose reverse index is NOT attested -- there an
--        evidence item whose reverse-index row is missing must still not
--        survive. Every evidence item of their objects
--        (idx_knowledge_objects_nb_* on notebook_id), hash-joined once
--        against the Memory sources. An attested public library keeps the
--        cheap reverse-index path only (reading all evidence of a 2M-object
--        library measured 13.5 s).
--    Memory candidates are read from M (primary key). The kept items are
--    rebuilt with one hash left join.
CREATE TEMP TABLE mki_scan ON COMMIT DROP AS
SELECT f.notebook_id
FROM mki_f f
JOIN notebooks n ON n.id = f.notebook_id
WHERE COALESCE(n.tier, '') <> 'base'
   OR NOT EXISTS (SELECT 1 FROM unified_kg_state u
                  WHERE u.notebook_id = f.notebook_id AND u.source_index_backfilled = 1)
   OR EXISTS (SELECT 1 FROM source_index_backfills b
              WHERE b.notebook_id = f.notebook_id AND b.status <> 'complete');
ANALYZE mki_scan;

CREATE TEMP TABLE mki_mixed ON COMMIT DROP AS
SELECT kos.object_id AS id
FROM mki_ms
JOIN knowledge_object_sources kos
  ON kos.source_id = mki_ms.id AND kos.notebook_id = mki_ms.notebook_id
WHERE NOT EXISTS (SELECT 1 FROM mki_m WHERE mki_m.id = kos.object_id)
UNION
SELECT ko.id
FROM mki_scan
-- per notebook of mki_scan (OFFSET 0 keeps the subquery a parameterised
-- index scan: the planner cannot know which notebooks mki_scan holds, and
-- would otherwise price the join with the average notebook size -- a public
-- library's)
CROSS JOIN LATERAL (
  SELECT k.id, k.source_id, k.evidence FROM knowledge_objects k
  WHERE k.notebook_id = mki_scan.notebook_id OFFSET 0
) ko
CROSS JOIN LATERAL jsonb_array_elements(
  CASE WHEN jsonb_typeof(ko.evidence) = 'array' THEN ko.evidence ELSE '[]'::jsonb END
) AS hit(item)
JOIN mki_ms ON mki_ms.id = hit.item ->> 'source_id'
WHERE NOT EXISTS (SELECT 1 FROM sources ds WHERE ds.id = ko.source_id AND ds.source_type = 'memory');
ANALYZE mki_mixed;

CREATE TEMP TABLE mki_cross ON COMMIT DROP AS
SELECT DISTINCT ko.id
FROM mki_m
JOIN knowledge_objects ko ON ko.id = mki_m.id
JOIN mki_ms own ON own.id = ko.source_id
CROSS JOIN LATERAL jsonb_array_elements(
  CASE WHEN jsonb_typeof(ko.evidence) = 'array' THEN ko.evidence ELSE '[]'::jsonb END
) AS hit(item)
JOIN mki_ms oth ON oth.id = hit.item ->> 'source_id'
WHERE oth.id <> own.id
  AND (own.owner IS NULL OR oth.owner IS NULL OR own.owner <> oth.owner);
ANALYZE mki_cross;

CREATE TEMP TABLE mki_new_evidence ON COMMIT DROP AS
SELECT ko.id,
       COALESCE(jsonb_agg(ev.item ORDER BY ev.ord) FILTER (
                  WHERE ms.id IS NULL
                     OR (own.id IS NOT NULL
                         AND (ms.id = own.id
                              OR (own.owner IS NOT NULL AND ms.owner IS NOT NULL
                                  AND ms.owner = own.owner)))),
                '[]'::jsonb) AS evidence
FROM (SELECT id FROM mki_mixed UNION SELECT id FROM mki_cross) mx
JOIN knowledge_objects ko ON ko.id = mx.id
LEFT JOIN mki_ms own ON own.id = ko.source_id
CROSS JOIN LATERAL jsonb_array_elements(
  CASE WHEN jsonb_typeof(ko.evidence) = 'array' THEN ko.evidence ELSE '[]'::jsonb END
) WITH ORDINALITY AS ev(item, ord)
LEFT JOIN mki_ms ms ON ms.id = ev.item ->> 'source_id'
WHERE ko.notebook_id IN (SELECT notebook_id FROM mki_f)
GROUP BY ko.id;

UPDATE knowledge_objects ko
SET evidence = ne.evidence
FROM mki_new_evidence ne
WHERE ne.id = ko.id;

--    Reverse-index rows of F (idx (source_id, object_id) from each Memory
--    source): a shared object's row of any Memory source, a Memory object's
--    row of another member's (or an orphan) Memory source.
DELETE FROM knowledge_object_sources kos
USING mki_ms
WHERE kos.source_id = mki_ms.id AND kos.notebook_id = mki_ms.notebook_id
  AND (NOT EXISTS (SELECT 1 FROM mki_m WHERE mki_m.id = kos.object_id)
       OR EXISTS (SELECT 1 FROM knowledge_objects ko JOIN mki_ms own ON own.id = ko.source_id
                  WHERE ko.id = kos.object_id AND own.id <> mki_ms.id
                    AND (own.owner IS NULL OR mki_ms.owner IS NULL OR own.owner <> mki_ms.owner)));

-- 10. Chunks under Memory sources and every row derived from them (normally
--    zero rows: Memory sources are never chunked, see the chunk write guards).
--    The derived tables cascade from chunks, but are named explicitly so the
--    cleanup does not depend on the cascade being present.
CREATE TEMP TABLE mki_chunks ON COMMIT DROP AS
SELECT c.id FROM chunks c WHERE c.source_id IN (SELECT id FROM mki_ms);
DELETE FROM chunk_embeddings WHERE chunk_id IN (SELECT id FROM mki_chunks);
DELETE FROM chunk_questions WHERE chunk_id IN (SELECT id FROM mki_chunks);
DELETE FROM chunk_elements WHERE chunk_id IN (SELECT id FROM mki_chunks);
DELETE FROM chunks WHERE id IN (SELECT id FROM mki_chunks);

-- 11. One content-free summary line in the PostgreSQL server log (LOG level
--     reaches the server log under the default log_min_messages): ids and
--     text never appear, only counts. It runs BEFORE the marker column is
--     added so the ACCESS EXCLUSIVE lock of step 12 is not held while it
--     counts. private_kept counts Memory-derived objects that keep shared
--     evidence a manual merge folded into them (they stay private to the
--     Memory's owner; docs/operations.md). promotions_rejected counts the
--     open generic proposals step 3 closed; conflicts_applied_on_shared_nodes
--     / _edges count the applied node and edge conflicts step 4 found that
--     had changed a shared object or relation for a Memory one;
--     cross_owner_stripped counts the Memory objects that lost another
--     member's Memory evidence (step 9); evidence_scan_notebooks counts
--     mki_scan (step 9); seed_check_notebooks counts G.
DO $mki$
DECLARE
  n_scan bigint;
  n_f bigint;
  n_m bigint;
  n_a bigint;
  n_comm bigint;
  n_chunks bigint;
  n_private bigint;
  n_promo bigint;
  n_conflict bigint;
  n_conflict_edges bigint;
  n_cross bigint;
  n_g bigint;
BEGIN
  SELECT count(*) INTO n_f FROM mki_f;
  SELECT count(*) INTO n_m FROM mki_m;
  SELECT count(*) INTO n_a FROM mki_a;
  SELECT count(*) INTO n_comm FROM mki_comm;
  SELECT count(*) INTO n_chunks FROM mki_chunks;
  SELECT count(*) INTO n_promo FROM mki_promo;
  SELECT count(*) INTO n_conflict FROM mki_conflict_shared;
  SELECT count(*) INTO n_conflict_edges FROM mki_conflict_shared_edges;
  SELECT count(*) INTO n_cross FROM mki_cross;
  SELECT count(*) INTO n_g FROM mki_g;
  SELECT count(*) INTO n_scan FROM mki_scan;
  SELECT count(*) INTO n_private
  FROM mki_m JOIN knowledge_objects ko ON ko.id = mki_m.id
  WHERE CASE WHEN jsonb_typeof(ko.evidence) = 'array' THEN EXISTS (
          SELECT 1 FROM jsonb_array_elements(ko.evidence) AS pv(item)
          WHERE (pv.item ->> 'source_id') NOT IN (SELECT id FROM mki_ms))
        ELSE false END;
  RAISE LOG 'memory-kg-isolation migration: affected_notebooks=% memory_objects=% clusters_removed=% communities_removed=% memory_chunks_removed=% private_kept=% promotions_rejected=% conflicts_applied_on_shared_nodes=% conflicts_applied_on_shared_edges=% cross_owner_stripped=% evidence_scan_notebooks=% seed_check_notebooks=%',
    n_f, n_m, n_a, n_comm, n_chunks, n_private, n_promo, n_conflict, n_conflict_edges, n_cross, n_scan, n_g;
END
$mki$;

-- 12. The marker and the derived-layer gates, first run only. New rows
--     default to 1: a graph built after this migration is isolated by the
--     write-side guards. G goes to 2 (awaiting the worker's dangling-seed
--     check; nothing else of an existing state row changes, and a notebook
--     without one -- a copy -- gets a row of table defaults, which every
--     reader already takes for a missing row: generation 0, clean,
--     uncertified reverse index). For F: the three
--     derived layers' sequences go to -1 and dirty to 1 so every layer's own sequence gate recomputes,
--     and both mutation counters move by one so every process cache keyed on
--     them misses (kg_mutation_seq is also part of the cluster input version,
--     so the rebuild of a queued notebook genuinely re-clusters). A notebook
--     of F without a state row (only possible after an offline merge) gets
--     one, so its rebuild is still queued.
ALTER TABLE unified_kg_state
  ADD COLUMN IF NOT EXISTS memory_isolation_version integer NOT NULL DEFAULT 1;

UPDATE unified_kg_state u
SET memory_isolation_version = 0,
    community_seq = -1,
    canonical_rel_seq = -1,
    mention_seq = -1,
    dirty = 1,
    kg_mutation_seq = u.kg_mutation_seq + 1,
    cluster_mutation_seq = u.cluster_mutation_seq + 1,
    updated_at = now()
WHERE (SELECT first_run FROM mki_run)
  AND u.notebook_id IN (SELECT notebook_id FROM mki_f);

INSERT INTO unified_kg_state (notebook_id, updated_at, memory_isolation_version)
SELECT g.notebook_id, now(), 2
FROM mki_g g
WHERE (SELECT first_run FROM mki_run)
ON CONFLICT (notebook_id) DO UPDATE SET memory_isolation_version = 2;

INSERT INTO unified_kg_state (notebook_id, dirty, updated_at, memory_isolation_version)
SELECT f.notebook_id, 1, now(), 0
FROM mki_f f
JOIN notebooks n ON n.id = f.notebook_id
WHERE (SELECT first_run FROM mki_run)
  AND NOT EXISTS (SELECT 1 FROM unified_kg_state u WHERE u.notebook_id = f.notebook_id);
