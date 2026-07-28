-- Generation-scoped, resumable keyset state for bounded relation completion.
CREATE INDEX idx_knowledge_objects_source_id
  ON knowledge_objects(source_id, id);

CREATE TABLE kg_relation_completion_state (
  notebook_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_generation TEXT NOT NULL,
  mode TEXT NOT NULL,
  next_object_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  schema_version INTEGER NOT NULL,
  updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
  CONSTRAINT pk_kg_relation_completion_state
    PRIMARY KEY(source_id, source_generation, mode),
  CONSTRAINT fk_kg_relation_completion_state_source
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);

CREATE INDEX idx_kg_relation_completion_state_nb_status
  ON kg_relation_completion_state(notebook_id, status, updated_at);
