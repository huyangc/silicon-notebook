import json
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path):
    s = get_settings().model_copy(update={"database_url": f"sqlite:///{tmp_path}/t.db",
                                          "storage_dir": str(tmp_path / "st")})
    repo = SQLiteRepository(s)
    repo._migrate(); repo._seed()
    return repo


def test_model_settings_default_empty(tmp_path):
    repo = _repo(tmp_path)
    assert repo.get_user_model_settings("user-local") == {}


def test_model_settings_roundtrip(tmp_path):
    repo = _repo(tmp_path)
    cfg = {"llm": {"base_url": "https://u.example/v1", "api_key": "sk-u", "model": "m-u"}}
    repo.set_user_model_settings("user-local", cfg)
    assert repo.get_user_model_settings("user-local") == cfg
    assert repo.get_user_model_settings("user-local")["llm"]["model"] == "m-u"


def test_policy_default_is_fallback():
    assert get_settings().user_model_config_policy == "fallback"
