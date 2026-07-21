from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository, set_request_user, reset_request_user
from app.models.schemas import AskRequest, NotebookCreate


def _repo(tmp_path, **over):
    s = get_settings().model_copy(update={"database_url": f"sqlite:///{tmp_path}/t.db",
                                          "storage_dir": str(tmp_path / "st"), **over})
    repo = SQLiteRepository(s); repo._migrate(); repo._seed()
    return repo


def test_required_unconfigured_ask_surfaces_model_error(tmp_path):
    repo = _repo(tmp_path, user_model_config_policy="required")
    tok = set_request_user(repo.current_user())
    try:
        nb = repo.create_notebook(NotebookCreate(name="t", purpose=""))
        resp = repo.ask(nb.id, AskRequest(question="hi", mode="chunk"))
        assert any(e.stage == "answer" for e in resp.model_errors)
        assert {e.message for e in resp.model_errors} == {"missing_config"}
        assert resp.answer == ""
    finally:
        reset_request_user(tok)


def test_required_unconfigured_reasoning_surfaces_model_error(tmp_path):
    repo = _repo(tmp_path, user_model_config_policy="required")
    tok = set_request_user(repo.current_user())
    try:
        nb = repo.create_notebook(NotebookCreate(name="t", purpose=""))
        resp = repo.ask(nb.id, AskRequest(question="hi", mode="reasoning"))
        assert any(e.stage == "answer" for e in resp.model_errors)
        assert {e.message for e in resp.model_errors} == {"missing_config"}
    finally:
        reset_request_user(tok)


def test_required_unconfigured_graph_surfaces_model_error(tmp_path):
    repo = _repo(tmp_path, user_model_config_policy="required")
    tok = set_request_user(repo.current_user())
    try:
        nb = repo.create_notebook(NotebookCreate(name="t", purpose=""))
        resp = repo.ask(nb.id, AskRequest(question="hi", mode="graph"))
        assert any(e.stage == "answer" for e in resp.model_errors)
        assert {e.message for e in resp.model_errors} == {"missing_config"}
    finally:
        reset_request_user(tok)
