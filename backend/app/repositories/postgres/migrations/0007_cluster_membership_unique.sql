WITH ranked AS (
  SELECT
    id,
    ROW_NUMBER() OVER (
      PARTITION BY notebook_id, object_type, member_object_id
      ORDER BY created_at DESC, id COLLATE "C" DESC
    ) AS duplicate_rank
  FROM concept_clusters
)
DELETE FROM concept_clusters AS cluster
USING ranked
WHERE cluster.id = ranked.id
  AND ranked.duplicate_rank > 1;

CREATE UNIQUE INDEX uq_clusters_notebook_type_member
  ON concept_clusters(notebook_id, object_type, member_object_id);
