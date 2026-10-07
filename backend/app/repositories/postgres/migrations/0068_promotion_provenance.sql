-- PR-E8 (ledger B-12): a knowledge object promoted into a public library
-- carries the public library's OWN provenance. The approval paths write it
-- from now on (app/domain/promotion_provenance.py -- read its docstring
-- first: the rule, the ids, the titles); this migration rewrites what was
-- approved before. Mirrors SQLite v88 (_migration_88), which runs the very
-- planner the approval paths run, object by object, in the same order;
-- tests/test_promotion_provenance_migration.py and its PostgreSQL twin seed
-- one world and assert the same rows on both backends and equality with the
-- runtime rewrite.
--
-- Per public library (notebooks.tier = 'base'), every evidence entry whose
-- source_id is NOT a source of that library (a promoter's source in another
-- notebook, or a source that no longer exists) -- not only on objects with a
-- source_candidate_id: a promotion merged into an existing object keeps that
-- object's id -- is rewritten:
--   * text: the original element's current text when the element still
--     exists, belongs to the named source and that source is not a Memory
--     source; otherwise the entry's stored quoted_span; an entry with neither
--     is dropped (an object left without evidence is then dropped by every
--     source ceiling at read time: fail closed);
--   * one source per original (src-promo-md5(<library>|<original source id>)),
--     source_type 'promotion', titled '晋升自：' || the entry's stored
--     source_title (first entry in object-id / position order), visible,
--     status 'active', parse_status 'parsed';
--   * one element per distinct (source, original element, text)
--     (el-promo-md5(<source>|<original element>|<text>)), its metadata naming
--     the original;
--   * the entry points at them; the original source id and its notebook
--     (the source's, else the approved candidate's, else '') stay as the
--     display keys origin_source_id / origin_notebook_id; an entry without a
--     stored quoted_span quotes the first 500 characters of the text;
--   * the object's reverse-index rows are replaced from its new evidence.
-- A non-object array item (legacy string evidence) and an entry naming one of
-- the library's own sources are kept as they are, in place.
--
-- Idempotent: after one run no entry of a public library names another
-- notebook's source, so a re-run selects nothing; the inserts skip an
-- existing id.
--
-- Deterministic data operations only: no model call, no randomness; the
-- timestamps of the rows it creates are now().
--
-- Cost. Candidates come from the evidence reverse index where it is ATTESTED
-- complete (unified_kg_state.source_index_backfilled = 1 and no unfinished
-- source_index_backfills row -- the same rule 0067 and the readers apply):
-- the library's knowledge_object_sources rows (idx_kos_notebook) with an
-- anti-join on pk_sources. A library whose reverse index is not attested
-- takes every object's evidence (idx_knowledge_objects_nb_* on notebook_id).
-- Only the candidates' evidence is expanded; the original elements and
-- sources are primary-key probes; nothing reads a wide table without its
-- notebook or its key.
--
-- Locks (the migrator runs this file in ONE transaction): ROW EXCLUSIVE on
-- sources, source_elements, knowledge_objects and knowledge_object_sources,
-- and row locks on the rows it rewrites -- only objects that still name
-- another notebook's source.

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

-- 2. Candidate objects.
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
  SELECT k.id FROM knowledge_objects k WHERE k.notebook_id = l.notebook_id OFFSET 0
) ko
WHERE NOT l.attested;
ANALYZE pp_cand;

-- 3. Every evidence item of the candidates, in position order, and whether it
--    is foreign to the object's own library.
CREATE TEMP TABLE pp_item ON COMMIT DROP AS
SELECT ko.notebook_id AS base_id, ko.id AS object_id, ev.ord, ev.item,
       ko.source_candidate_id,
       (jsonb_typeof(ev.item) = 'object'
        AND NOT EXISTS (SELECT 1 FROM sources own
                        WHERE own.id = COALESCE(ev.item ->> 'source_id', '')
                          AND own.notebook_id = ko.notebook_id)) AS foreign_item
FROM pp_cand c
JOIN knowledge_objects ko ON ko.id = c.id
JOIN pp_lib l ON l.notebook_id = ko.notebook_id
CROSS JOIN LATERAL jsonb_array_elements(
  CASE WHEN jsonb_typeof(ko.evidence) = 'array' THEN ko.evidence ELSE '[]'::jsonb END
) WITH ORDINALITY AS ev(item, ord);
ANALYZE pp_item;

-- 4. The foreign items resolved against the original (primary-key probes).
CREATE TEMP TABLE pp_foreign ON COMMIT DROP AS
SELECT i.base_id, i.object_id, i.ord, i.item,
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
       COALESCE(os.notebook_id, pc.notebook_id, '') AS origin_notebook_id,
       COALESCE(oe.text, '') AS live_text
FROM pp_item i
LEFT JOIN sources os ON os.id = COALESCE(i.item ->> 'source_id', '')
LEFT JOIN promotion_candidates pc ON pc.id = NULLIF(i.source_candidate_id, '')
LEFT JOIN source_elements oe
  ON oe.id = NULLIF(COALESCE(i.item ->> 'element_id', ''), '')
 AND oe.source_id = os.id
 AND os.source_type <> 'memory'
WHERE i.foreign_item;

-- 5. The rewrite: text, promotion source, promotion element.
CREATE TEMP TABLE pp_rewrite ON COMMIT DROP AS
SELECT x.*,
       'el-promo-' || md5(x.promo_source_id || '|' || x.origin_element_id || '|' || x.body)
         AS promo_element_id
FROM (
  SELECT f.*,
         CASE WHEN f.live_text <> '' THEN f.live_text ELSE f.stored_span END AS body,
         'src-promo-' || md5(f.base_id || '|' || f.origin_source_id) AS promo_source_id
  FROM pp_foreign f
) x;
ANALYZE pp_rewrite;

-- 6. Promotion sources and elements (first entry in object-id / position
--    order names them; an existing id is kept).
INSERT INTO sources (id, notebook_id, title, source_type, status, parse_status,
                     file_name, file_path, source_url, file_size, file_hash,
                     summary, doc_type, created_at, updated_at)
SELECT d.promo_source_id, d.base_id, '晋升自：' || d.origin_title, 'promotion',
       'active', 'parsed', '', '', '', 0, '', '', '', now(), now()
FROM (
  SELECT DISTINCT ON (r.promo_source_id) r.promo_source_id, r.base_id,
         r.origin_title, r.object_id, r.ord
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

-- 7. The rewritten evidence (dropped entries left out, order kept).
CREATE TEMP TABLE pp_new_evidence ON COMMIT DROP AS
SELECT i.object_id AS id,
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
WHERE i.object_id IN (SELECT object_id FROM pp_rewrite)
GROUP BY i.object_id;

UPDATE knowledge_objects ko
SET evidence = ne.evidence
FROM pp_new_evidence ne
WHERE ne.id = ko.id;

-- 8. Reverse index of the rewritten objects (KnowledgeStore.replace_object_sources).
DELETE FROM knowledge_object_sources kos
USING pp_new_evidence ne
WHERE kos.object_id = ne.id;

INSERT INTO knowledge_object_sources (object_id, source_id, notebook_id)
SELECT DISTINCT ne.id, ev.item ->> 'source_id', ko.notebook_id
FROM pp_new_evidence ne
JOIN knowledge_objects ko ON ko.id = ne.id
CROSS JOIN LATERAL jsonb_array_elements(ne.evidence) AS ev(item)
WHERE jsonb_typeof(ev.item) = 'object'
  AND COALESCE(ev.item ->> 'source_id', '') <> ''
ON CONFLICT (object_id, source_id) DO NOTHING;

-- 9. One content-free summary line in the server log (the SQLite twin logs
--    the same counts).
DO $pp$
DECLARE
  libraries bigint;
  objects_rewritten bigint;
  entries_rewritten bigint;
  entries_dropped bigint;
  objects_without_evidence bigint;
BEGIN
  SELECT count(DISTINCT base_id) INTO libraries FROM pp_rewrite;
  SELECT count(*) INTO objects_rewritten FROM pp_new_evidence;
  SELECT count(*) FILTER (WHERE body <> ''), count(*) FILTER (WHERE body = '')
    INTO entries_rewritten, entries_dropped FROM pp_rewrite;
  SELECT count(*) INTO objects_without_evidence FROM pp_new_evidence
    WHERE evidence = '[]'::jsonb;
  RAISE LOG 'promotion-provenance migration: libraries=% objects_rewritten=% entries_rewritten=% entries_dropped=% objects_without_evidence=%',
    libraries, objects_rewritten, entries_rewritten, entries_dropped,
    objects_without_evidence;
END
$pp$;
