"""Task 13: SchemaRegistryService owns schema CRUD + LLM-backed induction.
Store methods own object_schemas rows; the service owns notebook validation,
bounded content sampling, prompt/validation, duplicate suppression and
fail-open behavior. Behavior is characterized through the frozen facade
surface so the extraction is provably behavior-preserving."""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.config import Settings
from app.models.schemas import (
    NotebookCreate,
    ObjectSchemaCreate,
    ObjectSchemaUpdate,
)
from app.services.sqlite_repository import SQLiteRepository, _now
from tests.model_testkit import RecordingModelProvider, bind_chat_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    provider = RecordingModelProvider()
    repository = SQLiteRepository(Settings(), model_provider=provider)
    repository.recording_model_provider = provider
    return repository


def _insert_element(repo, notebook_id: str, text: str) -> str:
    source_id = f"src-{uuid4().hex[:10]}"
    now = _now()
    with repo._write() as db:
        db.execute(
            """INSERT INTO sources
               (id, notebook_id, title, source_type, status, parse_status,
                file_name, file_path, file_size, file_hash, summary, doc_type,
                created_at, updated_at)
               VALUES (?, ?, 'Doc', 'markdown', 'extracted', 'parsed',
                       'doc.md', '', 0, '', '', '', ?, ?)""",
            (source_id, notebook_id, now, now),
        )
        db.execute(
            """INSERT INTO source_elements
               (id, source_id, element_type, location_label, text, metadata, created_at)
               VALUES (?, ?, 'paragraph', 'p1', ?, '{}', ?)""",
            (f"el-{uuid4().hex[:10]}", source_id, text, now),
        )
    return source_id


class _FakeLLM:
    def __init__(self, raw, configured=True, boom=False):
        self.raw = raw
        self.configured = configured
        self.boom = boom
        self.calls = 0

    def chat_json(self, messages, schema_hint):
        self.calls += 1
        if self.boom:
            raise RuntimeError("model down")
        return self.raw


def test_runtime_owns_schema_registry_service(repo):
    service = repo._runtime.schema_registry
    assert service is not None
    assert service.knowledge is repo._runtime.knowledge
    assert service.sources is repo._runtime.source_store
    assert service.notebooks is repo._runtime.notebook_store
    assert service.settings is repo.settings


def test_v47_migration_relocates_legacy_notebook_schema(repo):
    notebook = repo.create_notebook(NotebookCreate(name="legacy schema"))
    now = _now()
    with repo._write() as db:
        db.execute("DROP TABLE notebook_object_schemas")
        db.execute(
            "INSERT INTO object_schemas "
            "(object_type,notebook_id,plural,fields,primary_field,label,source,"
            "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy_recipe", notebook.id, "legacy_recipes", '["name"]',
                "name", "Legacy recipe", "induced", "proposed", now, now,
            ),
        )
        db.execute("PRAGMA user_version = 46")

    reopened = SQLiteRepository(repo.settings)
    with reopened._connect() as db:
        relocated = db.execute(
            "SELECT * FROM notebook_object_schemas "
            "WHERE notebook_id=? AND object_type='legacy_recipe'",
            (notebook.id,),
        ).fetchone()
        legacy = db.execute(
            "SELECT 1 FROM object_schemas WHERE object_type='legacy_recipe'"
        ).fetchone()
    assert relocated is not None
    assert relocated["created_by"] == "user-local"
    assert relocated["status"] == "proposed"
    assert legacy is None


def test_v47_migration_refuses_orphaned_legacy_schema(repo):
    now = _now()
    with repo._write() as db:
        db.execute("DROP TABLE notebook_object_schemas")
        db.execute(
            "INSERT INTO object_schemas "
            "(object_type,notebook_id,created_at,updated_at) VALUES (?,?,?,?)",
            ("orphan_recipe", "nb-missing", now, now),
        )
        db.execute("PRAGMA user_version = 46")

    with pytest.raises(RuntimeError, match="missing notebook"):
        SQLiteRepository(repo.settings)


def test_effective_schemas_overlays_db_rows_on_builtin(repo):
    from app.services.extraction_profiles import OBJECT_SCHEMAS

    registry = repo.effective_schemas()
    for object_type in OBJECT_SCHEMAS:
        assert object_type in registry
    repo.create_object_schema(ObjectSchemaCreate(
        object_type="lab_recipe", plural="lab_recipes",
        fields=["name", "steps"], primary="name",
        description="", label="Lab Recipe", list_fields=[],
    ))
    registry = repo.effective_schemas()
    assert registry["lab_recipe"].fields == ["name", "steps"]


def test_effective_schema_truth_table_global_and_notebook_overrides(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    other = repo.create_notebook(NotebookCreate(name="other"))

    # Code default exists only through the seeded active global row.
    assert "concept" in repo.effective_schemas(nb.id)
    repo.update_object_schema("concept", ObjectSchemaUpdate(status="disabled"))
    assert "concept" not in repo.effective_schemas(nb.id)
    disabled_view = {
        item.object_type: item for item in repo.list_notebook_object_schemas(nb.id)
    }
    assert disabled_view["concept"].status == "disabled"
    assert disabled_view["concept"].inherited is True
    assert disabled_view["concept"].can_edit is False

    local_enabled = repo.update_notebook_object_schema(
        nb.id,
        "concept",
        ObjectSchemaUpdate(status="active"),
        created_by="user-local",
    )
    assert local_enabled.overrides_global is True
    assert "concept" in repo.effective_schemas(nb.id)
    assert "concept" not in repo.effective_schemas(other.id)
    assert repo.delete_notebook_object_schema(nb.id, "concept") == "inherited"

    repo.update_object_schema("concept", ObjectSchemaUpdate(status="active"))
    local = repo.update_notebook_object_schema(
        nb.id,
        "concept",
        ObjectSchemaUpdate(label="Local concept", status="disabled"),
        created_by="user-local",
    )
    assert local.scope == "notebook"
    assert local.overrides_global is True
    assert "concept" not in repo.effective_schemas(nb.id)
    assert "concept" in repo.effective_schemas(other.id)

    assert repo.delete_notebook_object_schema(nb.id, "concept") == "inherited"
    assert "concept" in repo.effective_schemas(nb.id)


def test_notebook_schema_custom_type_is_isolated_and_delete_guards_objects(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    other = repo.create_notebook(NotebookCreate(name="other"))
    created = repo.create_notebook_object_schema(
        nb.id,
        ObjectSchemaCreate(
            object_type="lab_recipe", fields=["name", "steps"],
            primary="name", list_fields=["steps"], label="Notebook recipe",
        ),
        created_by="user-local",
    )
    assert created.scope == "notebook"
    assert created.inherited is False
    assert created.overrides_global is False
    assert created.can_edit is True
    assert "lab_recipe" in repo.effective_schemas(nb.id)
    assert "lab_recipe" not in repo.effective_schemas(other.id)

    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,source_id,payload,evidence,status,"
            "owner,last_reviewed,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ko-schema", nb.id, "lab_recipe", "", '{"name":"x"}',
                "[]", "approved", "", "", now, now,
            ),
        )
    with pytest.raises(ValueError, match="knowledge objects"):
        repo.delete_notebook_object_schema(nb.id, "lab_recipe")
    with repo._write() as db:
        db.execute("DELETE FROM knowledge_objects WHERE id='ko-schema'")
    assert repo.delete_notebook_object_schema(nb.id, "lab_recipe") == "deleted"


def test_schema_field_order_never_hides_existing_payload_fields():
    from app.services.ask_service import knowledge_record
    from app.services.extraction_profiles import ObjectSchema

    schema = ObjectSchema(
        type="lab_recipe",
        plural="lab_recipes",
        fields=["name"],
        primary="name",
        description="",
        list_fields=[],
    )
    record = knowledge_record(
        "lab_recipe",
        {
            "id": "ko-existing",
            "payload": {"name": "etch", "retired_field": "still valuable"},
            "evidence": [],
        },
        schema,
    )

    assert [field.key for field in record.fields] == ["name", "retired_field"]


@pytest.mark.parametrize(
    "payload,message",
    [
        (ObjectSchemaCreate(object_type="Bad-Type", fields=["name"]), "object_type"),
        (ObjectSchemaCreate(object_type="x", fields=["name", "name"]), "unique"),
        (ObjectSchemaCreate(object_type="x", fields=["name"], primary="missing"), "primary"),
        (ObjectSchemaCreate(object_type="x", fields=["name"], list_fields=["missing"]), "list_fields"),
    ],
)
def test_notebook_schema_input_rails(repo, payload, message):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    with pytest.raises(ValueError, match=message):
        repo.create_notebook_object_schema(
            nb.id, payload, created_by="user-local"
        )


def test_schema_crud_roundtrip_and_ordering(repo):
    created = repo.create_object_schema(ObjectSchemaCreate(
        object_type="Lab Recipe", plural="", fields=["name"], primary="",
        description=" d ", label="", list_fields=[],
    ))
    # normalization: lowercased, spaces -> underscore; defaults filled
    assert created.object_type == "lab_recipe"
    assert created.plural == "lab_recipes"
    assert created.primary == "name"
    assert created.label == "lab_recipe"
    with pytest.raises(ValueError):
        repo.create_object_schema(ObjectSchemaCreate(
            object_type="lab_recipe", plural="", fields=["name"], primary="",
            description="", label="", list_fields=[],
        ))
    updated = repo.update_object_schema(
        "lab_recipe", ObjectSchemaUpdate(status="disabled")
    )
    assert updated.status == "disabled"
    with pytest.raises(ValueError):
        repo.update_object_schema("lab_recipe", ObjectSchemaUpdate(status="bogus"))
    with pytest.raises(KeyError):
        repo.update_object_schema("missing_type", ObjectSchemaUpdate(label="x"))
    # list ordering: active < disabled < proposed, then object_type asc
    models = repo.list_object_schemas()
    order = {"active": 0, "disabled": 1, "proposed": 2}
    keys = [(order.get(m.status, 3), m.object_type) for m in models]
    assert keys == sorted(keys)
    repo.delete_object_schema("lab_recipe")
    with pytest.raises(KeyError):
        repo.delete_object_schema("lab_recipe")


def test_delete_builtin_schema_is_refused(repo):
    # builtin rows are seeded at migration time
    with pytest.raises(ValueError):
        repo.delete_object_schema("concept")


def test_propose_schemas_unconfigured_is_a_noop_returning_proposals(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _insert_element(repo, nb.id, "Flow doc with steps and owners.")
    assert not repo._runtime.models.configured("schema_induction")
    assert repo.propose_schemas(nb.id) == []


def test_propose_schemas_missing_notebook_raises(repo):
    with pytest.raises(KeyError):
        repo.propose_schemas("nb-missing")


def test_propose_schemas_persists_new_types_and_suppresses_existing(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _insert_element(repo, nb.id, "Checklists with owner and deadline fields.")
    fake = _FakeLLM(
        '{"new_types": ['
        '{"object_type": "Design Review", "fields": ["name", "owner"],'
        ' "primary": "name", "label": "Review", "plural": "reviews",'
        ' "description": "d", "rationale": "seen in doc"},'
        '{"object_type": "concept", "fields": ["name"]},'
        '{"object_type": "", "fields": ["name"]},'
        '{"object_type": "no_fields", "fields": []},'
        '"not-a-dict"'
        ']}'
    )
    # Bind exactly the runtime workload consumed by SchemaRegistryService.
    bind_chat_client(repo, "schema_induction", fake)
    proposals = repo.propose_schemas(nb.id)
    assert fake.calls == 1
    assert ("chat", "schema_induction") in repo.recording_model_provider.calls
    assert [m.object_type for m in proposals] == ["design_review"]
    model = proposals[0]
    assert model.status == "proposed"
    assert model.source == "induced"
    assert model.fields == ["name", "owner"]
    assert model.rationale == "seen in doc"
    assert model.notebook_id == nb.id
    assert model.scope == "notebook"
    # second run: the freshly-proposed type is now "existing" -> suppressed,
    # no duplicate row
    proposals = repo.propose_schemas(nb.id)
    assert [m.object_type for m in proposals] == ["design_review"]


def test_propose_schemas_fail_open_on_malformed_and_model_error(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    _insert_element(repo, nb.id, "text")
    malformed = _FakeLLM("{not json")
    bind_chat_client(repo, "schema_induction", malformed)
    assert repo.propose_schemas(nb.id) == []
    boom = _FakeLLM("{}", boom=True)
    bind_chat_client(repo, "schema_induction", boom)
    assert repo.propose_schemas(nb.id) == []


def test_propose_schemas_without_elements_skips_llm(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    fake = _FakeLLM('{"new_types": []}')
    bind_chat_client(repo, "schema_induction", fake)
    assert repo.propose_schemas(nb.id) == []
    assert fake.calls == 0


def test_propose_schemas_unconfigured_does_not_scan_notebook(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    monkeypatch.setattr(
        repo._runtime.source_store,
        "notebook_element_sample",
        lambda notebook_id: (_ for _ in ()).throw(
            AssertionError("unconfigured schema induction must not scan elements")
        ),
    )

    assert not repo._runtime.models.configured("schema_induction")
    assert repo.propose_schemas(nb.id) == []


def test_schema_induction_sample_is_bounded_before_prompt_build(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for index in range(100):
        _insert_element(repo, nb.id, f"element-{index}-" + ("x" * 500))

    elements = repo._runtime.source_store.notebook_element_sample(nb.id)
    rendered = "\n".join(
        f"[{element['location_label']}] {element['text']}" for element in elements
    )

    assert len(rendered) <= 8000
    assert len(elements) < 100


def test_unconfigured_induction_returns_only_current_notebook_proposals(repo):
    first = repo.create_notebook(NotebookCreate(name="first"))
    second = repo.create_notebook(NotebookCreate(name="second"))
    now = _now()
    with repo._write() as db:
        repo._runtime.knowledge.insert_notebook_schema(
            db,
            notebook_id=first.id,
            object_type="first_proposal",
            plural="first_proposals",
            fields_json='["name"]',
            primary="name",
            description="",
            label="First",
            list_fields_json="[]",
            source="induced",
            status="proposed",
            rationale="",
            created_by="user-local",
            now=now,
        )
        repo._runtime.knowledge.insert_notebook_schema(
            db,
            notebook_id=second.id,
            object_type="second_proposal",
            plural="second_proposals",
            fields_json='["name"]',
            primary="name",
            description="",
            label="Second",
            list_fields_json="[]",
            source="induced",
            status="proposed",
            rationale="",
            created_by="user-local",
            now=now,
        )

    assert [item.object_type for item in repo.propose_schemas(first.id)] == [
        "first_proposal"
    ]
