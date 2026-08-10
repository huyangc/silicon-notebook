-- Optional LLM-generated question shadow index. Generated text is only an
-- address into an original chunk and is never exposed as citation evidence.

ALTER TABLE chunks ADD COLUMN question_indexed_at timestamptz;

CREATE TABLE chunk_questions (
  id text COLLATE "C" NOT NULL,
  chunk_id text COLLATE "C" NOT NULL,
  notebook_id text COLLATE "C" NOT NULL,
  source_id text COLLATE "C" NOT NULL,
  question text COLLATE "C" NOT NULL,
  vector bytea NOT NULL,
  created_at timestamptz NOT NULL,
  CONSTRAINT pk_chunk_questions PRIMARY KEY (id),
  CONSTRAINT uq_chunk_questions_chunk_question UNIQUE (chunk_id, question)
);

CREATE INDEX idx_chunk_questions_nb ON chunk_questions(notebook_id, id);
CREATE INDEX idx_chunk_questions_source ON chunk_questions(source_id, id);

ALTER TABLE chunk_questions
  ADD CONSTRAINT fk_chunk_questions_chunk_id__chunks
  FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE;
ALTER TABLE chunk_questions
  ADD CONSTRAINT fk_chunk_questions_notebook_id__notebooks
  FOREIGN KEY (notebook_id) REFERENCES notebooks(id) ON DELETE CASCADE;
ALTER TABLE chunk_questions
  ADD CONSTRAINT fk_chunk_questions_source_id__sources
  FOREIGN KEY (source_id) REFERENCES sources(id) ON DELETE CASCADE;
