"""Editable extraction-schema registry service (Task 13).

Owns ``effective_schemas``, schema CRUD and the LLM-backed ``propose_schemas``
induction. The KnowledgeStore primitives own the object_schemas rows; this
service owns notebook validation, bounded content sampling, prompt/response
validation, duplicate suppression and fail-open behavior — moved verbatim
from the facade.
"""
from __future__ import annotations

import json
import re
from typing import Dict, List

from app.core.config import Settings
from app.models.knowledge import (
    ObjectSchemaCreate,
    ObjectSchemaModel,
    ObjectSchemaUpdate,
)
from app.repositories.ports import (
    KnowledgeStorePort,
    NotebookStorePort,
    RepositoryDatabasePort,
    SourceStorePort,
)
from app.services.extraction_profiles import OBJECT_SCHEMAS, ObjectSchema
from app.services.prompts import SCHEMA_INDUCTION_HINT, schema_induction_prompt


SCHEMA_NAME_MAX_CHARS = 80
SCHEMA_TEXT_MAX_CHARS = 2000
SCHEMA_FIELDS_MAX = 64
_SCHEMA_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


class SchemaConflictError(ValueError):
    """A valid schema definition conflicts with an existing registry key."""


def _normalized_schema_type(value: str) -> str:
    result = value.strip().lower().replace(" ", "_")
    if not result:
        raise ValueError("object_type is required")
    if len(result) > SCHEMA_NAME_MAX_CHARS or not _SCHEMA_IDENTIFIER.fullmatch(result):
        raise ValueError(
            "object_type must start with a lowercase letter and contain only "
            "lowercase letters, digits, and underscores"
        )
    return result


def _validated_schema_definition(
    *, fields: List[str], primary: str, list_fields: List[str]
) -> tuple[List[str], str, List[str]]:
    if len(fields) > SCHEMA_FIELDS_MAX or len(list_fields) > SCHEMA_FIELDS_MAX:
        raise ValueError("schema has too many fields")
    normalized_fields = [field.strip() for field in fields]
    normalized_list_fields = [field.strip() for field in list_fields]
    if any(
        not field
        or len(field) > SCHEMA_NAME_MAX_CHARS
        or not _SCHEMA_IDENTIFIER.fullmatch(field)
        for field in (*normalized_fields, *normalized_list_fields)
    ):
        raise ValueError("schema fields must use lowercase identifier names")
    if len(normalized_fields) != len(set(normalized_fields)):
        raise ValueError("schema fields must be unique")
    if len(normalized_list_fields) != len(set(normalized_list_fields)):
        raise ValueError("schema list_fields must be unique")
    if not set(normalized_list_fields).issubset(normalized_fields):
        raise ValueError("schema list_fields must be included in fields")
    normalized_primary = primary.strip()
    if normalized_primary and normalized_primary not in normalized_fields:
        raise ValueError("schema primary must be included in fields")
    if not normalized_primary and normalized_fields:
        normalized_primary = normalized_fields[0]
    return normalized_fields, normalized_primary, normalized_list_fields


def _validate_schema_texts(*values: str) -> None:
    if any(len(value.strip()) > SCHEMA_TEXT_MAX_CHARS for value in values):
        raise ValueError("schema text value is too long")


def object_schema_from_row(
    row,
    *,
    scope: str = "global",
    inherited: bool = False,
    overrides_global: bool = False,
    can_edit: bool = False,
) -> ObjectSchemaModel:
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
        scope=scope,
        inherited=inherited,
        overrides_global=overrides_global,
        can_edit=can_edit,
    )


class SchemaRegistryService:
    def __init__(
        self,
        database: RepositoryDatabasePort,
        notebooks: NotebookStorePort,
        knowledge_store: KnowledgeStorePort,
        source_store: SourceStorePort,
        model_clients,
        settings: Settings,
        current_user_id=lambda: "",
    ) -> None:
        self.database = database
        self.notebooks = notebooks
        self.knowledge = knowledge_store
        self.sources = source_store
        self.models = model_clients
        self.settings = settings
        self.current_user_id = current_user_id

    @staticmethod
    def from_row(row) -> ObjectSchemaModel:
        return object_schema_from_row(row)

    @staticmethod
    def _runtime_schema(row) -> ObjectSchema:
        return ObjectSchema(
            type=row["object_type"],
            plural=row["plural"] or f"{row['object_type']}s",
            fields=json.loads(row["fields"] or "[]"),
            primary=row["primary_field"] or "",
            description=row["description"] or "",
            list_fields=json.loads(row["list_fields"] or "[]"),
        )

    def effective_schemas(self, notebook_id: str = "") -> Dict[str, ObjectSchema]:
        """Resolve the active global baseline plus one notebook's overlay.

        A local non-active row is still an override: most importantly a
        ``disabled`` row removes the active global definition. The optional
        empty notebook id preserves the administrator/smoke compatibility
        surface; every notebook-semantic production caller passes its id.
        """
        with self.database.connect() as db:
            global_rows = self.knowledge.schema_rows(db)
            notebook_rows = (
                self.knowledge.notebook_schema_rows(db, notebook_id)
                if notebook_id
                else []
            )
        registry: Dict[str, ObjectSchema] = {}
        global_types = set()
        for row in global_rows:
            global_types.add(row["object_type"])
            if row["status"] == "active":
                registry[row["object_type"]] = self._runtime_schema(row)
        for object_type, schema in OBJECT_SCHEMAS.items():
            if object_type not in global_types:
                registry[object_type] = schema
        for row in notebook_rows:
            object_type = row["object_type"]
            if row["status"] == "active":
                registry[object_type] = self._runtime_schema(row)
            else:
                registry.pop(object_type, None)
        return registry

    def schema_labels(self, notebook_id: str) -> Dict[str, str]:
        return {
            item.object_type: (item.label or item.object_type)
            for item in self.list_notebook_object_schemas(notebook_id)
            if item.status == "active"
        }

    def list_object_schemas(self, *, can_edit: bool = False) -> List[ObjectSchemaModel]:
        with self.database.connect() as db:
            rows = self.knowledge.schema_rows(db)
        models = [
            object_schema_from_row(row, scope="global", can_edit=can_edit)
            for row in rows
        ]
        order = {"active": 0, "disabled": 1, "proposed": 2}
        models.sort(key=lambda m: (order.get(m.status, 3), m.object_type))
        return models

    def list_notebook_object_schemas(
        self, notebook_id: str, *, can_edit: bool = False
    ) -> List[ObjectSchemaModel]:
        self.notebooks.get_row(notebook_id)
        with self.database.connect() as db:
            global_rows = self.knowledge.schema_rows(db)
            notebook_rows = self.knowledge.notebook_schema_rows(db, notebook_id)
        global_by_type = {row["object_type"]: row for row in global_rows}
        local_by_type = {row["object_type"]: row for row in notebook_rows}
        models: List[ObjectSchemaModel] = []
        for object_type, row in global_by_type.items():
            local = local_by_type.pop(object_type, None)
            if local is not None:
                models.append(object_schema_from_row(
                    local,
                    scope="notebook",
                    overrides_global=True,
                    can_edit=can_edit,
                ))
            else:
                models.append(object_schema_from_row(
                    row,
                    scope="global",
                    inherited=True,
                    can_edit=can_edit,
                ))
        models.extend(
            object_schema_from_row(
                row,
                scope="notebook",
                overrides_global=False,
                can_edit=can_edit,
            )
            for row in local_by_type.values()
        )
        order = {"active": 0, "disabled": 1, "proposed": 2}
        models.sort(key=lambda m: (order.get(m.status, 3), m.object_type))
        return models

    def create_object_schema(self, payload: ObjectSchemaCreate) -> ObjectSchemaModel:
        object_type = _normalized_schema_type(payload.object_type)
        fields, primary, list_fields = _validated_schema_definition(
            fields=payload.fields,
            primary=payload.primary,
            list_fields=payload.list_fields,
        )
        _validate_schema_texts(payload.plural, payload.description, payload.label)
        now = self.knowledge.seams.now()
        with self.database.write() as db:
            if self.knowledge.schema_exists(db, object_type):
                raise ValueError(f"object type '{object_type}' already exists")
            self.knowledge.insert_custom_schema(
                db,
                object_type,
                payload.plural.strip() or f"{object_type}s",
                json.dumps(fields, ensure_ascii=False),
                primary,
                payload.description.strip(),
                payload.label.strip() or object_type,
                json.dumps(list_fields, ensure_ascii=False),
                now,
            )
            row = self.knowledge.schema_row(db, object_type)
        return object_schema_from_row(row)

    def update_object_schema(
        self, object_type: str, payload: ObjectSchemaUpdate
    ) -> ObjectSchemaModel:
        with self.database.connect() as db:
            current = self.knowledge.schema_row(db, object_type)
        if current is None:
            raise KeyError(object_type)
        self._validate_update(current, payload)
        updates: List[str] = []
        values: List[object] = []
        if payload.plural is not None:
            updates.append("plural = ?")
            values.append(payload.plural.strip())
        if payload.fields is not None:
            updates.append("fields = ?")
            values.append(json.dumps(
                [field.strip() for field in payload.fields], ensure_ascii=False
            ))
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
            values.append(json.dumps(
                [field.strip() for field in payload.list_fields], ensure_ascii=False
            ))
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

    def create_notebook_object_schema(
        self, notebook_id: str, payload: ObjectSchemaCreate, *, created_by: str
    ) -> ObjectSchemaModel:
        self.notebooks.get_row(notebook_id)
        object_type = _normalized_schema_type(payload.object_type)
        fields, primary, list_fields = _validated_schema_definition(
            fields=payload.fields,
            primary=payload.primary,
            list_fields=payload.list_fields,
        )
        _validate_schema_texts(payload.plural, payload.description, payload.label)
        now = self.knowledge.seams.now()
        with self.database.write() as db:
            if (
                self.knowledge.schema_row(db, object_type) is not None
                or self.knowledge.notebook_schema_row(db, notebook_id, object_type)
                is not None
            ):
                raise SchemaConflictError(
                    f"object type '{object_type}' already exists"
                )
            self.knowledge.insert_notebook_schema(
                db,
                notebook_id=notebook_id,
                object_type=object_type,
                plural=payload.plural.strip() or f"{object_type}s",
                fields_json=json.dumps(fields, ensure_ascii=False),
                primary=primary,
                description=payload.description.strip(),
                label=payload.label.strip() or object_type,
                list_fields_json=json.dumps(list_fields, ensure_ascii=False),
                source="custom",
                status="active",
                rationale="",
                created_by=created_by,
                now=now,
            )
            row = self.knowledge.notebook_schema_row(db, notebook_id, object_type)
        return object_schema_from_row(
            row, scope="notebook", can_edit=True
        )

    @staticmethod
    def _update_columns(payload: ObjectSchemaUpdate) -> tuple[List[str], List[object]]:
        updates: List[str] = []
        values: List[object] = []
        for attribute, column, json_field in (
            ("plural", "plural", False),
            ("fields", "fields", True),
            ("primary", "primary_field", False),
            ("description", "description", False),
            ("label", "label", False),
            ("list_fields", "list_fields", True),
        ):
            value = getattr(payload, attribute)
            if value is not None:
                updates.append(f"{column} = ?")
                values.append(
                    json.dumps(
                        [field.strip() for field in value], ensure_ascii=False
                    )
                    if json_field
                    else value.strip()
                )
        if payload.status is not None:
            status = payload.status.strip()
            if status not in {"active", "disabled", "proposed"}:
                raise ValueError(f"invalid schema status: {status}")
            updates.append("status = ?")
            values.append(status)
        return updates, values

    @staticmethod
    def _validate_update(row, payload: ObjectSchemaUpdate) -> None:
        fields = (
            payload.fields
            if payload.fields is not None
            else json.loads(row["fields"] or "[]")
        )
        primary = (
            payload.primary
            if payload.primary is not None
            else row["primary_field"] or ""
        )
        list_fields = (
            payload.list_fields
            if payload.list_fields is not None
            else json.loads(row["list_fields"] or "[]")
        )
        _validated_schema_definition(
            fields=fields, primary=primary, list_fields=list_fields
        )
        _validate_schema_texts(*(
            value
            for value in (payload.plural, payload.description, payload.label)
            if value is not None
        ))

    def update_notebook_object_schema(
        self,
        notebook_id: str,
        object_type: str,
        payload: ObjectSchemaUpdate,
        *,
        created_by: str,
    ) -> ObjectSchemaModel:
        self.notebooks.get_row(notebook_id)
        updates, values = self._update_columns(payload)
        with self.database.write() as db:
            row = self.knowledge.notebook_schema_row(db, notebook_id, object_type)
            global_row = self.knowledge.schema_row(db, object_type)
            if row is None:
                if global_row is None:
                    raise KeyError(object_type)
                now = self.knowledge.seams.now()
                self.knowledge.insert_notebook_schema(
                    db,
                    notebook_id=notebook_id,
                    object_type=object_type,
                    plural=global_row["plural"],
                    fields_json=global_row["fields"],
                    primary=global_row["primary_field"],
                    description=global_row["description"],
                    label=global_row["label"],
                    list_fields_json=global_row["list_fields"],
                    source=global_row["source"],
                    status=global_row["status"],
                    rationale=global_row["rationale"],
                    created_by=created_by,
                    now=now,
                )
                row = self.knowledge.notebook_schema_row(
                    db, notebook_id, object_type
                )
            self._validate_update(row, payload)
            if updates:
                updates.append("updated_at = ?")
                values.append(self.knowledge.seams.now())
                self.knowledge.update_notebook_schema_columns(
                    db, notebook_id, object_type, updates, values
                )
            row = self.knowledge.notebook_schema_row(db, notebook_id, object_type)
        return object_schema_from_row(
            row,
            scope="notebook",
            overrides_global=global_row is not None,
            can_edit=True,
        )

    def delete_notebook_object_schema(
        self, notebook_id: str, object_type: str
    ) -> str:
        self.notebooks.get_row(notebook_id)
        with self.database.write() as db:
            row = self.knowledge.notebook_schema_row(db, notebook_id, object_type)
            if row is None:
                raise KeyError(object_type)
            global_row = self.knowledge.schema_row(db, object_type)
            if (
                global_row is None
                and self.knowledge.notebook_schema_has_objects(
                    db, notebook_id, object_type
                )
            ):
                raise ValueError(
                    "cannot delete a notebook-only schema while knowledge objects use it"
                )
            self.knowledge.delete_notebook_schema_row(db, notebook_id, object_type)
        return "inherited" if global_row is not None else "deleted"

    def propose_schemas(self, notebook_id: str) -> List[ObjectSchemaModel]:
        """Schema induction (suggestion mode): inspect the notebook's content and
        propose NEW object types the current schema does not cover. Proposals are
        stored with status='proposed' for curator approval; never auto-activated.
        Requires the LLM; offline this is a no-op that returns existing proposals."""
        self.notebooks.get_row(notebook_id)  # KeyError if missing
        with self.database.connect() as db:
            existing = self.knowledge.existing_schema_types(db)
            existing.update(
                row["object_type"]
                for row in self.knowledge.notebook_schema_rows(db, notebook_id)
            )
        llm_client = self.models.chat("schema_induction")
        if not llm_client.configured:
            return [
                m for m in self.list_notebook_object_schemas(notebook_id)
                if m.scope == "notebook" and m.status == "proposed"
            ]
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
                    try:
                        object_type = _normalized_schema_type(
                            str(item.get("object_type", ""))
                        )
                    except ValueError:
                        continue
                    fields = [
                        str(f).strip()
                        for f in (item.get("fields") or [])
                        if str(f).strip()
                    ]
                    if not object_type or object_type in existing or not fields:
                        continue
                    try:
                        fields, primary, list_fields = _validated_schema_definition(
                            fields=fields,
                            primary=str(item.get("primary", "")),
                            list_fields=[],
                        )
                        _validate_schema_texts(
                            str(item.get("plural", "")),
                            str(item.get("description", "")),
                            str(item.get("label", "")),
                            str(item.get("rationale", "")),
                        )
                    except ValueError:
                        continue
                    self.knowledge.insert_notebook_schema(
                        db,
                        notebook_id=notebook_id,
                        object_type=object_type,
                        plural=str(item.get("plural", "")).strip()
                        or f"{object_type}s",
                        fields_json=json.dumps(fields, ensure_ascii=False),
                        primary=primary,
                        description=str(item.get("description", "")).strip(),
                        label=str(item.get("label", "")).strip() or object_type,
                        list_fields_json=json.dumps(list_fields),
                        source="induced",
                        status="proposed",
                        rationale=str(item.get("rationale", "")).strip(),
                        created_by=self.current_user_id(),
                        now=now,
                    )
                    existing.add(object_type)
        return [
            m for m in self.list_notebook_object_schemas(notebook_id)
            if m.scope == "notebook" and m.status == "proposed"
        ]
