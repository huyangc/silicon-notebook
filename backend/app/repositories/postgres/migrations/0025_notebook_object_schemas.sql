-- Notebook-owned object-schema overrides and custom definitions.
-- object_schemas remains the administrator-owned global baseline. Historical
-- induced rows used its notebook_id compatibility column and are moved here.

CREATE TABLE notebook_object_schemas (
  notebook_id text COLLATE "C" NOT NULL,
  object_type text COLLATE "C" NOT NULL,
  plural text COLLATE "C" NOT NULL DEFAULT '',
  fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  primary_field text COLLATE "C" NOT NULL DEFAULT '',
  description text COLLATE "C" NOT NULL DEFAULT '',
  label text COLLATE "C" NOT NULL DEFAULT '',
  list_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  source text COLLATE "C" NOT NULL DEFAULT 'custom',
  status text COLLATE "C" NOT NULL DEFAULT 'active',
  rationale text COLLATE "C" NOT NULL DEFAULT '',
  created_by text COLLATE "C" NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL,
  updated_at timestamptz NOT NULL,
  CONSTRAINT pk_notebook_object_schemas PRIMARY KEY (notebook_id, object_type),
  CONSTRAINT fk_notebook_object_schemas_notebook FOREIGN KEY (notebook_id)
    REFERENCES notebooks(id) ON UPDATE NO ACTION ON DELETE CASCADE,
  CONSTRAINT ck_notebook_object_schemas_status CHECK (
    status IN ('active','proposed','disabled')
  )
);

CREATE INDEX idx_notebook_object_schemas_status
  ON notebook_object_schemas(notebook_id, status, object_type);

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM object_schemas os
    LEFT JOIN notebooks n ON n.id=os.notebook_id
    WHERE os.notebook_id<>'' AND n.id IS NULL
  ) THEN
    RAISE EXCEPTION
      'cannot migrate notebook-scoped object schema with missing notebook';
  END IF;
END $$;

INSERT INTO notebook_object_schemas
(notebook_id, object_type, plural, fields, primary_field, description, label,
 list_fields, source, status, rationale, created_by, created_at, updated_at)
SELECT os.notebook_id, os.object_type, os.plural, os.fields, os.primary_field,
       os.description, os.label, os.list_fields, os.source, os.status,
       os.rationale, COALESCE(n.created_by, ''), os.created_at, os.updated_at
FROM object_schemas os
JOIN notebooks n ON n.id = os.notebook_id
WHERE os.notebook_id <> '';

DELETE FROM object_schemas os
WHERE os.notebook_id <> ''
  AND EXISTS (SELECT 1 FROM notebooks n WHERE n.id=os.notebook_id);
