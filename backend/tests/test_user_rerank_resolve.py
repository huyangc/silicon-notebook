from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository, set_request_user, reset_request_user
from app.services.rerank_client import RerankClient


def _repo(tmp_path, **over):
    s = get_settings().model_copy(update={"database_url": f"sqlite:///{tmp_path}/t.db",
                                          "storage_dir": str(tmp_path / "st"), **over})
    repo = SQLiteRepository(s); repo._migrate(); repo._seed()
    return repo


def test_rerank_overrides():
    c = RerankClient(get_settings(), model="rm", base_url="https://r/v1/", api_key="sk-r")
    assert c.model == "rm" and c.base_url == "https://r/v1" and c.api_key == "sk-r"
    assert c.configured is True


def test_user_rerank_drives_client(tmp_path):
    repo = _repo(tmp_path)
    repo.set_user_model_settings("user-local",
        {"rerank": {"base_url": "https://r/v1", "api_key": "sk-r", "model": "rm"}})
    tok = set_request_user(repo.current_user())
    try:
        assert repo.rerank_client.base_url == "https://r/v1" and repo.rerank_client.model == "rm"
    finally:
        reset_request_user(tok)


def test_required_policy_rerank_disabled(tmp_path):
    repo = _repo(tmp_path, user_model_config_policy="required")
    tok = set_request_user(repo.current_user())
    try:
        assert repo.rerank_client.configured is False   # 未配 + required → 禁用(原序)
    finally:
        reset_request_user(tok)
