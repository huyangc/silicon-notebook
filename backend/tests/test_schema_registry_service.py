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
    # production-compatible seam: the llm_client setter writes the runtime
    # model provider the SchemaRegistryService consumes
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
