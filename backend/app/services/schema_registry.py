"""Editable extraction-schema registry service (Task 13).

Owns ``effective_schemas``, schema CRUD and the LLM-backed ``propose_schemas``
induction. The KnowledgeStore primitives own the object_schemas rows; this
service owns notebook validation, bounded content sampling, prompt/response
validation, duplicate suppression and fail-open behavior — moved verbatim
from the facade.
"""
from __future__ import annotations

import json
from typing import Dict, List

from app.core.config import Settings
from app.models.schemas import (
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
)
from app.repositories.sqlite.knowledge_store import KnowledgeStore
from app.repositories.sqlite.notebook_store import NotebookStore
from app.repositories.sqlite.source_store import SourceStore
from app.services.extraction_profiles import OBJECT_SCHEMAS, ObjectSchema
from app.services.prompts import SCHEMA_INDUCTION_HINT, schema_induction_prompt


def object_schema_from_row(row) -> ObjectSchemaModel:
    return ObjectSchemaModel(
        object_type=row["object_type"],
        plural=row["plural"] or f"{row['object_type']}s",
        fields=json.loads(row["fields"] or "[]"),
        primary=row["primary_field"] or "",
        description=row["description"] or "",
        label=row["label"] or row["object_type"],
        list_fields=json.loads(row["list_fields"] or "[]"),
        source=row["source"] or "builtin",
        status=row["status"] or "active",
        rationale=row["rationale"] or "",
        notebook_id=row["notebook_id"] if "notebook_id" in row.keys() else "",
    )


class SchemaRegistryService:
    def __init__(
        self,
        notebooks: NotebookStore,
        knowledge_store: KnowledgeStore,
        source_store: SourceStore,
        model_clients,
        settings: Settings,
    ) -> None:
        self.notebooks = notebooks
        self.knowledge = knowledge_store
        self.sources = source_store
        self.models = model_clients
        self.settings = settings
        self.database = knowledge_store.database

    @staticmethod
    def from_row(row) -> ObjectSchemaModel:
        return object_schema_from_row(row)

    def effective_schemas(self) -> Dict[str, ObjectSchema]:
        """Active object schemas as an ObjectSchema registry for extraction —
        DB rows overlaid on the code defaults."""
        with self.database.connect() as db:
            rows = self.knowledge.active_schema_rows(db)
        registry: Dict[str, ObjectSchema] = {}
        for row in rows:
            registry[row["object_type"]] = ObjectSchema(
                type=row["object_type"],
                plural=row["plural"] or f"{row['object_type']}s",
                fields=json.loads(row["fields"] or "[]"),
                primary=row["primary_field"] or "",
                description=row["description"] or "",
                list_fields=json.loads(row["list_fields"] or "[]"),
            )
        for object_type, schema in OBJECT_SCHEMAS.items():
            registry.setdefault(object_type, schema)
        return registry

    def list_object_schemas(self) -> List[ObjectSchemaModel]:
        with self.database.connect() as db:
            rows = self.knowledge.schema_rows(db)
        models = [object_schema_from_row(row) for row in rows]
        order = {"active": 0, "disabled": 1, "proposed": 2}
        models.sort(key=lambda m: (order.get(m.status, 3), m.object_type))
        return models

    def create_object_schema(self, payload: ObjectSchemaCreate) -> ObjectSchemaModel:
        object_type = payload.object_type.strip().lower().replace(" ", "_")
        if not object_type:
            raise ValueError("object_type is required")
        now = self.knowledge.seams.now()
        with self.database.write() as db:
            if self.knowledge.schema_exists(db, object_type):
                raise ValueError(f"object type '{object_type}' already exists")
            self.knowledge.insert_custom_schema(
                db,
                object_type,
                payload.plural.strip() or f"{object_type}s",
                json.dumps(payload.fields, ensure_ascii=False),
                payload.primary.strip() or (payload.fields[0] if payload.fields else ""),
                payload.description.strip(),
                payload.label.strip() or object_type,
                json.dumps(payload.list_fields, ensure_ascii=False),
                now,
            )
            row = self.knowledge.schema_row(db, object_type)
        return object_schema_from_row(row)

    def update_object_schema(
        self, object_type: str, payload: ObjectSchemaUpdate
    ) -> ObjectSchemaModel:
        updates: List[str] = []
        values: List[object] = []
        if payload.plural is not None:
            updates.append("plural = ?")
            values.append(payload.plural.strip())
        if payload.fields is not None:
            updates.append("fields = ?")
            values.append(json.dumps(payload.fields, ensure_ascii=False))
        if payload.primary is not None:
            updates.append("primary_field = ?")
            values.append(payload.primary.strip())
        if payload.description is not None:
            updates.append("description = ?")
            values.append(payload.description.strip())
        if payload.label is not None:
            updates.append("label = ?")
            values.append(payload.label.strip())
        if payload.list_fields is not None:
            updates.append("list_fields = ?")
            values.append(json.dumps(payload.list_fields, ensure_ascii=False))
        if payload.status is not None:
            status = payload.status.strip()
            if status not in {"active", "disabled", "proposed"}:
                raise ValueError(f"invalid schema status: {status}")
            updates.append("status = ?")
            values.append(status)
        with self.database.write() as db:
            row = self.knowledge.schema_row(db, object_type)
            if row is None:
                raise KeyError(object_type)
            if updates:
                updates.append("updated_at = ?")
                values.append(self.knowledge.seams.now())
                values.append(object_type)
                self.knowledge.update_schema_columns(db, object_type, updates, values)
            row = self.knowledge.schema_row(db, object_type)
        return object_schema_from_row(row)

    def delete_object_schema(self, object_type: str) -> None:
        with self.database.write() as db:
            row = self.knowledge.schema_row(db, object_type)
            if row is None:
                raise KeyError(object_type)
            if row["source"] == "builtin":
                raise ValueError("builtin schemas can be disabled but not deleted")
            self.knowledge.delete_schema_row(db, object_type)

    def propose_schemas(self, notebook_id: str) -> List[ObjectSchemaModel]:
        """Schema induction (suggestion mode): inspect the notebook's content and
        propose NEW object types the current schema does not cover. Proposals are
        stored with status='proposed' for curator approval; never auto-activated.
        Requires the LLM; offline this is a no-op that returns existing proposals."""
        self.notebooks.get_row(notebook_id)  # KeyError if missing
        with self.database.connect() as db:
            existing = self.knowledge.existing_schema_types(db)
        llm_client = self.models.llm_client
        if not llm_client.configured:
            return [m for m in self.list_object_schemas() if m.status == "proposed"]
        elements = self.sources.notebook_element_sample(notebook_id)
        if llm_client.configured and elements:
            sample = "\n".join(
                f"[{e['location_label']}] {e['text']}" for e in elements
            )[:8000]
            data: dict = {}
            try:
                raw = llm_client.chat_json(
                    [
                        {
                            "role": "user",
                            "content": schema_induction_prompt(
                                sorted(existing), sample
                            ),
                        }
                    ],
                    SCHEMA_INDUCTION_HINT,
                )
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                data = {}
            now = self.knowledge.seams.now()
            with self.database.write() as db:
                for item in data.get("new_types") or []:
                    if not isinstance(item, dict):
                        continue
                    object_type = (
                        str(item.get("object_type", "")).strip().lower().replace(" ", "_")
                    )
                    fields = [
                        str(f).strip()
                        for f in (item.get("fields") or [])
                        if str(f).strip()
                    ]
                    if not object_type or object_type in existing or not fields:
                        continue
                    primary = str(item.get("primary", "")).strip() or fields[0]
                    self.knowledge.insert_induced_schema(
                        db,
                        object_type,
                        str(item.get("plural", "")).strip() or f"{object_type}s",
                        json.dumps(fields, ensure_ascii=False),
                        primary,
                        str(item.get("description", "")).strip(),
                        str(item.get("label", "")).strip() or object_type,
                        str(item.get("rationale", "")).strip(),
                        notebook_id,
                        now,
                    )
                    existing.add(object_type)
        return [m for m in self.list_object_schemas() if m.status == "proposed"]
