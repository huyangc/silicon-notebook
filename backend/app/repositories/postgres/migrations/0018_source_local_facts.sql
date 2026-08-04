-- Mirror SQLite v40's immutable source-generation facts. These rows preserve
-- the pre-fusion payload/evidence used to construct a source-local retrieval
-- subgraph. global_object_id is intentionally not a foreign key: global fusion
-- and governance may replace that projection without deleting the source fact.

CREATE TABLE knowledge_source_facts (
  id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  source_id text COLLATE "C" NOT NULL,
  source_generation text COLLATE "C" NOT NULL,
  local_object_id text COLLATE "C" NOT NULL,
  global_object_id text COLLATE "C" NOT NULL DEFAULT '',
  object_type text COLLATE "C" NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  evidence jsonb NOT NULL DEFAULT '[]'::jsonb,
  projection_version bigint NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  CONSTRAINT pk_knowledge_source_facts PRIMARY KEY (id)
);

CREATE TABLE knowledge_source_fact_elements (
  fact_id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  source_id text COLLATE "C" NOT NULL,
  source_generation text COLLATE "C" NOT NULL,
  element_id text COLLATE "C" NOT NULL,
  created_at timestamptz NOT NULL,
  CONSTRAINT pk_knowledge_source_fact_elements PRIMARY KEY (fact_id, element_id)
);

ALTER TABLE knowledge_source_facts
  ADD CONSTRAINT fk_ksf_notebook FOREIGN KEY (notebook_id) REFERENCES notebooks(id)
  ON UPDATE NO ACTION ON DELETE CASCADE;
ALTER TABLE knowledge_source_facts
  ADD CONSTRAINT fk_ksf_source FOREIGN KEY (source_id) REFERENCES sources(id)
  ON UPDATE NO ACTION ON DELETE CASCADE;
ALTER TABLE knowledge_source_fact_elements
  ADD CONSTRAINT fk_ksfe_fact FOREIGN KEY (fact_id) REFERENCES knowledge_source_facts(id)
  ON UPDATE NO ACTION ON DELETE CASCADE;
ALTER TABLE knowledge_source_fact_elements
  ADD CONSTRAINT fk_ksfe_notebook FOREIGN KEY (notebook_id) REFERENCES notebooks(id)
  ON UPDATE NO ACTION ON DELETE CASCADE;
ALTER TABLE knowledge_source_fact_elements
  ADD CONSTRAINT fk_ksfe_source FOREIGN KEY (source_id) REFERENCES sources(id)
  ON UPDATE NO ACTION ON DELETE CASCADE;

CREATE INDEX idx_knowledge_source_facts_source_generation
  ON knowledge_source_facts(source_id, source_generation, id);
CREATE UNIQUE INDEX uq_knowledge_source_facts_generation_local
  ON knowledge_source_facts(source_id, source_generation, local_object_id);
CREATE INDEX idx_knowledge_source_facts_notebook_object
  ON knowledge_source_facts(notebook_id, global_object_id);
CREATE INDEX idx_knowledge_source_fact_elements_source
  ON knowledge_source_fact_elements(source_id, source_generation, element_id, fact_id);
