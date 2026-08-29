import pytest

from app.core.config import Settings
from app.models.schemas import AskRequest, NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.services.sqlite_repository import (
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)


def _settings(tmp_path, *, model_services_config: str = "") -> Settings:
    return Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path}/t.db",
        storage_dir=str(tmp_path / "st"),
        model_services_config=model_services_config,
        mineru_mode="off",
        event_log_enabled=False,
        llm_log_enabled=False,
        query_rewrite_enabled=False,
        graph_ppr_enabled=False,
        kg_query_refine_enabled=False,
        reasoning_ppr_prefetch=False,
        community_layer_enabled=False,
        mention_bridge_enabled=False,
    )


def _repo(tmp_path, *, model_services_config: str = "") -> SQLiteRepository:
    repo = SQLiteRepository(
        _settings(tmp_path, model_services_config=model_services_config)
    )
    repo._migrate()
    repo._seed()
    return repo


def _missing_ask_answer_config(tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("TEST_SYSTEM_MODEL_API_KEY", "fixture-secret")
    path = tmp_path / "model-services.toml"
    path.write_text(
        """[services.background_chat]
display_name = "BackgroundChat"
kind = "chat"
protocol = "openai"
base_url = "https://fixture.invalid/v1"
model = "fixture-chat"
api_key_env = "TEST_SYSTEM_MODEL_API_KEY"
max_concurrency = 2

[bindings]
kg_extract = "background_chat"
""",
        encoding="utf-8",
    )
    return str(path)


def _seed_grounded_kg(repo: SQLiteRepository, notebook_id: str) -> None:
    source = repo.upload_sources(
        notebook_id,
        [
            UploadedSourceFile(
                file_name="engram.md",
                content_type="text/markdown",
                content=(
                    b"# Engram\n\n"
                    b"Engram is a conditional memory module for Transformer backbones."
                ),
            )
        ],
        scheduler=lambda _source_id: None,
    )[0]
    repo.process_source(source.id)
    element = next(
        item
        for item in repo.source_elements(source.id)
        if "conditional memory module" in item.text
    )
    evidence = [
        {
            "source_id": source.id,
            "source_title": source.title,
            "element_id": element.id,
            "element_type": element.element_type,
            "location_label": element.location_label,
            "quoted_span": element.text,
            "confidence": 1.0,
        }
    ]
    repo.store_kg(
        notebook_id,
        source.id,
        [
            {
                "local_id": "claim-engram",
                "object_type": "claim",
                "payload": {
                    "name": "Engram is a conditional memory module for Transformer backbones.",
                    "section_path": "Engram",
                },
                "evidence": evidence,
            }
        ],
        [],
    )


def test_empty_system_registry_reasoning_keeps_offline_kg_evidence(tmp_path):
    repo = _repo(tmp_path)
    token = set_request_user(repo.current_user())
    try:
        notebook = repo.create_notebook(NotebookCreate(name="t", purpose=""))
        _seed_grounded_kg(repo, notebook.id)

        response = repo.ask(
            notebook.id,
            AskRequest(question="What is Engram?", mode="reasoning"),
        )

        assert response.related_knowledge
        assert response.citations
        assert "missing_config" not in {
            error.message for error in response.model_errors
        }
    finally:
        reset_request_user(token)
        repo.close()


@pytest.mark.parametrize("mode", ["chunk", "reasoning"])
def test_nonempty_registry_missing_ask_answer_surfaces_system_error(
    tmp_path, monkeypatch, mode
):
    config = _missing_ask_answer_config(tmp_path, monkeypatch)
    repo = _repo(tmp_path, model_services_config=config)
    token = set_request_user(repo.current_user())
    try:
        notebook = repo.create_notebook(NotebookCreate(name="t", purpose=""))

        response = repo.ask(notebook.id, AskRequest(question="hi", mode=mode))

        assert any(error.stage == "answer" for error in response.model_errors)
        assert {error.message for error in response.model_errors} == {"missing_config"}
        if mode != "chunk":
            assert response.conclusion == "系统未配置当前问答所需的模型服务，请联系维护人员"
    finally:
        reset_request_user(token)
        repo.close()
