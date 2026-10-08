-- PR-E8 (ledger B-12): a knowledge object promoted into a public library
-- carries the public library's OWN provenance. The approval paths write it
-- from now on (app/domain/promotion_provenance.py -- read its docstring
-- first: the rule, the ids, the titles); this migration rewrites what was
-- approved before. Mirrors SQLite v88 (_migration_88: a FROZEN copy of the
-- same rule, pinned equal to the live planner by
-- tests/test_promotion_provenance_migration.py), object by object in the same
-- order; that test and its PostgreSQL twin seed one world and assert the same
-- rows on both backends.
--
-- Per public library (notebooks.tier = 'base'), every evidence entry whose
-- source_id is NOT a source of that library (a promoter's source in another
-- notebook, a source that no longer exists, or no source id at all) -- not
-- only on objects with a source_candidate_id: a promotion merged into an
-- existing object keeps that object's id -- is rewritten:
--   * an entry whose original is a member's MEMORY source is DROPPED (fail
--     closed, ruling M1: pre-guard data only; nothing of a Memory is
--     published by this migration);
--   * an object created by an approved MEMORY promotion (its
--     source_candidate_id names a promotion_candidates row of object_type
--     'memory') is rewritten as the approval does now: one source per Memory
--     (src-promo-md5(<library>|memory:<memory id>)), titled
--     '晋升自个人记忆：' || the Memory's title (empty when the Memory is gone),
--     and its cards keep ONLY their stored quoted_span (the excerpt the member
--     approved); a Memory promotion merged into an existing object cannot be
--     told apart from a generic one and takes the generic rule below;
--   * any other entry: one source per original
--     (src-promo-md5(<library>|<original source id>)), titled '晋升自：' ||
--     the entry's stored source_title (status 'active', parse_status
--     'extracted': a terminal state, it is never parsed or extracted); text =
--     the original element's current
--     text when the element still exists and belongs to the named source,
--     otherwise the stored quoted_span;
--   * an entry with no text is dropped (an object left without evidence is
--     then dropped by every source ceiling at read time: fail closed);
--   * one element per distinct (source, original element, text)
--     (el-promo-md5(<source>|<original element>|<text>)), its metadata naming
--     the original; the first entry in object-id / position order names a
--     source and an element;
--   * the entry points at them; the original source id and its notebook
--     (the source's, else the approved candidate's, else '') stay as the
--     display keys origin_source_id / origin_notebook_id; an entry without a
--     stored quoted_span quotes the first 500 characters of the text;
--   * the object's reverse-index rows are replaced from its new evidence, and
--     every library touched is marked dirty with its kg_mutation_seq bumped
--     (what an approval does), so the derived layers rebuild from the new
--     evidence.
-- A non-object array item (legacy string evidence) and an entry naming one of
-- the library's own sources are kept as they are, in place.
--
-- Idempotent: after one run no entry of a public library names another
-- notebook's source, so a re-run selects nothing (and marks nothing dirty);
-- the inserts skip an existing id.
--
-- Deterministic data operations only: no model call, no randomness; the
-- timestamps of the rows it creates are now().
--
-- Cost. Candidates come from the evidence reverse index where it is ATTESTED
-- complete (unified_kg_state.source_index_backfilled = 1 and no unfinished
-- source_index_backfills row -- the same rule 0067 and the readers apply):
-- the library's knowledge_object_sources rows (idx_kos_notebook) with an
-- anti-join on the library's own sources; the reverse index has no row for an
-- entry WITHOUT a source id, so those libraries also take one scan of their
-- objects' evidence for such entries. A library whose reverse index is not
-- attested takes every object's evidence (idx_knowledge_objects_nb_* on
-- notebook_id). Only objects that hold a foreign entry are expanded into the
-- working table; the original elements and sources are primary-key probes.
-- Measured on a 1.04M-object unattested library with 2.04M evidence items
-- and 10k foreign entries: about 7 s in all, the slowest statement 4.3 s.
--
-- Locks (the migrator runs this file in ONE transaction): ROW EXCLUSIVE on
-- sources, source_elements, knowledge_objects, knowledge_object_sources and
-- unified_kg_state, and row locks on the rows it rewrites -- only objects that
-- still name another notebook's source, and the touched libraries' state rows.

-- 1. Public libraries and whether their reverse index is attested.
CREATE TEMP TABLE pp_lib ON COMMIT DROP AS
SELECT n.id AS notebook_id,
       (EXISTS (SELECT 1 FROM unified_kg_state u
                WHERE u.notebook_id = n.id AND u.source_index_backfilled = 1)
        AND NOT EXISTS (SELECT 1 FROM source_index_backfills b
                        WHERE b.notebook_id = n.id AND b.status <> 'complete')
       ) AS attested
FROM notebooks n
WHERE n.tier = 'base';
ANALYZE pp_lib;

-- 2. Candidate objects: the attested reverse index, plus (every library) a
--    scan for entries with no source id, which the reverse index never holds;
--    an unattested library takes every object.
CREATE TEMP TABLE pp_cand ON COMMIT DROP AS
SELECT kos.object_id AS id
FROM pp_lib l
JOIN knowledge_object_sources kos ON kos.notebook_id = l.notebook_id
WHERE l.attested
  AND NOT EXISTS (SELECT 1 FROM sources s
                  WHERE s.id = kos.source_id AND s.notebook_id = l.notebook_id)
UNION
SELECT ko.id
FROM pp_lib l
-- OFFSET 0 keeps the subquery a parameterised index scan per library
CROSS JOIN LATERAL (
  SELECT k.id, k.evidence FROM knowledge_objects k
  WHERE k.notebook_id = l.notebook_id OFFSET 0
) ko
WHERE NOT l.attested
   OR (jsonb_typeof(ko.evidence) = 'array'
       AND EXISTS (SELECT 1 FROM jsonb_array_elements(ko.evidence) AS ev(item)
                   WHERE jsonb_typeof(ev.item) = 'object'
                     AND COALESCE(ev.item ->> 'source_id', '') = ''));
ANALYZE pp_cand;

-- 3. The candidates that really hold a foreign entry (only these are expanded).
CREATE TEMP TABLE pp_obj ON COMMIT DROP AS
SELECT ko.id
FROM pp_cand c
JOIN knowledge_objects ko ON ko.id = c.id
WHERE jsonb_typeof(ko.evidence) = 'array'
  AND EXISTS (
    SELECT 1 FROM jsonb_array_elements(ko.evidence) AS ev(item)
    WHERE jsonb_typeof(ev.item) = 'object'
      AND NOT EXISTS (SELECT 1 FROM sources own
                      WHERE own.id = COALESCE(ev.item ->> 'source_id', '')
                        AND own.notebook_id = ko.notebook_id));
ANALYZE pp_obj;

-- 4. Every evidence item of those objects, in position order, whether it is
--    foreign, and the approved Memory promotion that created the object.
CREATE TEMP TABLE pp_item ON COMMIT DROP AS
SELECT ko.notebook_id AS base_id, ko.id AS object_id, ev.ord, ev.item,
       pc.notebook_id AS candidate_notebook_id,
       CASE WHEN pc.object_type = 'memory' THEN pc.object_id END AS memory_id,
       COALESCE(mi.title, '') AS memory_title,
       (jsonb_typeof(ev.item) = 'object'
        AND NOT EXISTS (SELECT 1 FROM sources own
                        WHERE own.id = COALESCE(ev.item ->> 'source_id', '')
                          AND own.notebook_id = ko.notebook_id)) AS foreign_item
FROM pp_obj o
JOIN knowledge_objects ko ON ko.id = o.id
LEFT JOIN promotion_candidates pc ON pc.id = NULLIF(ko.source_candidate_id, '')
LEFT JOIN memory_items mi ON pc.object_type = 'memory' AND mi.id = pc.object_id
CROSS JOIN LATERAL jsonb_array_elements(ko.evidence) WITH ORDINALITY AS ev(item, ord);
ANALYZE pp_item;

-- 5. The foreign items resolved against the original (primary-key probes).
CREATE TEMP TABLE pp_foreign ON COMMIT DROP AS
SELECT i.base_id, i.object_id, i.ord, i.item, i.memory_id, i.memory_title,
       COALESCE(i.item ->> 'source_id', '') AS origin_source_id,
       COALESCE(i.item ->> 'element_id', '') AS origin_element_id,
       CASE WHEN jsonb_typeof(i.item -> 'quoted_span') = 'string'
            THEN i.item ->> 'quoted_span' ELSE '' END AS stored_span,
       CASE WHEN jsonb_typeof(i.item -> 'source_title') = 'string'
            THEN i.item ->> 'source_title' ELSE '' END AS origin_title,
       CASE WHEN jsonb_typeof(i.item -> 'element_type') = 'string'
                 AND i.item ->> 'element_type' <> ''
            THEN i.item ->> 'element_type' ELSE 'paragraph' END AS element_type,
       CASE WHEN jsonb_typeof(i.item -> 'location_label') = 'string'
            THEN i.item ->> 'location_label' ELSE '' END AS location_label,
       COALESCE(os.notebook_id, i.candidate_notebook_id, '') AS origin_notebook_id,
       COALESCE(os.source_type = 'memory', false) AS memory_origin,
       COALESCE(oe.text, '') AS live_text
FROM pp_item i
LEFT JOIN sources os ON os.id = COALESCE(i.item ->> 'source_id', '')
LEFT JOIN source_elements oe
  ON i.memory_id IS NULL
 AND oe.id = NULLIF(COALESCE(i.item ->> 'element_id', ''), '')
 AND oe.source_id = os.id
 AND os.source_type <> 'memory'
WHERE i.foreign_item;

-- 6. The rewrite: text, promotion source and its title, promotion element.
CREATE TEMP TABLE pp_rewrite ON COMMIT DROP AS
SELECT x.*,
       'el-promo-' || md5(x.promo_source_id || '|' || x.origin_element_id || '|' || x.body)
         AS promo_element_id
FROM (
  SELECT f.*,
         CASE WHEN f.memory_origin THEN ''
              WHEN f.live_text <> '' THEN f.live_text
              ELSE f.stored_span END AS body,
         CASE WHEN f.memory_id IS NOT NULL
              THEN 'src-promo-' || md5(f.base_id || '|' || 'memory:' || f.memory_id)
              ELSE 'src-promo-' || md5(f.base_id || '|' || f.origin_source_id)
         END AS promo_source_id,
         CASE WHEN f.memory_id IS NOT NULL
              THEN '晋升自个人记忆：' || f.memory_title
              ELSE '晋升自：' || f.origin_title
         END AS promo_title
  FROM pp_foreign f
) x;
ANALYZE pp_rewrite;

-- 7. Promotion sources and elements (first entry in object-id / position
--    order names them; an existing id is kept).
INSERT INTO sources (id, notebook_id, title, source_type, status, parse_status,
                     file_name, file_path, source_url, file_size, file_hash,
                     summary, doc_type, created_at, updated_at)
SELECT d.promo_source_id, d.base_id, d.promo_title, 'promotion',
       'active', 'extracted', '', '', '', 0, '', '', '', now(), now()
FROM (
  SELECT DISTINCT ON (r.promo_source_id) r.promo_source_id, r.base_id,
         r.promo_title, r.object_id, r.ord
  FROM pp_rewrite r
  WHERE r.body <> ''
  ORDER BY r.promo_source_id, r.object_id COLLATE "C", r.ord
) d
ORDER BY d.object_id COLLATE "C", d.ord
ON CONFLICT (id) DO NOTHING;

INSERT INTO source_elements (id, source_id, element_type, location_label, text,
                             metadata, created_at)
SELECT d.promo_element_id, d.promo_source_id, d.element_type, d.location_label,
       d.body,
       jsonb_build_object('promotion', jsonb_build_object(
         'origin_source_id', d.origin_source_id,
         'origin_element_id', d.origin_element_id,
         'origin_notebook_id', d.origin_notebook_id)),
       now()
FROM (
  SELECT DISTINCT ON (r.promo_element_id) r.*
  FROM pp_rewrite r
  WHERE r.body <> ''
  ORDER BY r.promo_element_id, r.object_id COLLATE "C", r.ord
) d
ORDER BY d.object_id COLLATE "C", d.ord
ON CONFLICT (id) DO NOTHING;

-- 8. The rewritten evidence (dropped entries left out, order kept).
CREATE TEMP TABLE pp_new_evidence ON COMMIT DROP AS
SELECT i.object_id AS id, i.base_id,
       COALESCE(jsonb_agg(
         CASE WHEN r.object_id IS NULL THEN i.item
              ELSE i.item || jsonb_build_object(
                'source_id', r.promo_source_id,
                'element_id', r.promo_element_id,
                'quoted_span', CASE WHEN r.stored_span <> '' THEN r.stored_span
                                    ELSE left(r.body, 500) END,
                'origin_source_id', r.origin_source_id,
                'origin_notebook_id', r.origin_notebook_id)
         END ORDER BY i.ord
       ) FILTER (WHERE NOT i.foreign_item OR r.body <> ''), '[]'::jsonb) AS evidence
FROM pp_item i
LEFT JOIN pp_rewrite r ON r.object_id = i.object_id AND r.ord = i.ord
GROUP BY i.object_id, i.base_id;

UPDATE knowledge_objects ko
SET evidence = ne.evidence
FROM pp_new_evidence ne
WHERE ne.id = ko.id;

-- 9. Reverse index of the rewritten objects (KnowledgeStore.replace_object_sources).
DELETE FROM knowledge_object_sources kos
USING pp_new_evidence ne
WHERE kos.object_id = ne.id;

INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id)
SELECT DISTINCT ne.id, ev.item ->> 'source_id', ne.base_id
FROM pp_new_evidence ne
CROSS JOIN LATERAL jsonb_array_elements(ne.evidence) AS ev(item)
WHERE jsonb_typeof(ev.item) = 'object'
  AND COALESCE(ev.item ->> 'source_id', '') <> ''
ON CONFLICT (object_id, source_id) DO NOTHING;

-- 10. Every touched library: dirty, kg_mutation_seq + 1 (what an approval's
--     mark_dirty does), so derived layers and seq-keyed caches move on.
INSERT INTO unified_kg_state (notebook_id, dirty, kg_mutation_seq, updated_at)
SELECT DISTINCT ne.base_id, 1, 1, now() FROM pp_new_evidence ne
ON CONFLICT (notebook_id) DO UPDATE SET
  dirty = 1,
  kg_mutation_seq = unified_kg_state.kg_mutation_seq + 1,
  updated_at = EXCLUDED.updated_at;

-- 11. One content-free summary line in the server log (the SQLite twin logs
--     the same counts).
DO $pp$
DECLARE
  libraries bigint;
  objects_rewritten bigint;
  entries_rewritten bigint;
  entries_dropped bigint;
  objects_without_evidence bigint;
BEGIN
  SELECT count(DISTINCT base_id) INTO libraries FROM pp_new_evidence;
  SELECT count(*) INTO objects_rewritten FROM pp_new_evidence;
  SELECT count(*) FILTER (WHERE body <> ''), count(*) FILTER (WHERE body = '')
    INTO entries_rewritten, entries_dropped FROM pp_rewrite;
  SELECT count(*) INTO objects_without_evidence FROM pp_new_evidence
    WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements(evidence) AS ev(item)
                      WHERE jsonb_typeof(ev.item) = 'object');
  RAISE LOG 'promotion-provenance migration: libraries=% objects_rewritten=% entries_rewritten=% entries_dropped=% objects_without_evidence=%',
    libraries, objects_rewritten, entries_rewritten, entries_dropped,
    objects_without_evidence;
END
$pp$;
