-- Stable, index-satisfied keyset order for bounded lexical relation recall.
CREATE INDEX idx_knowledge_relations_nb_source_id
  ON knowledge_relations(notebook_id, source_object_id, id);
CREATE INDEX idx_knowledge_relations_nb_target_id
  ON knowledge_relations(notebook_id, target_object_id, id);
