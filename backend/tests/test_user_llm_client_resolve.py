import pytest
from app.core.config import get_settings
from app.services.sqlite_repository import SQLiteRepository, set_request_user, reset_request_user


def _repo(tmp_path, **over):
    s = get_settings().model_copy(update={"database_url": f"sqlite:///{tmp_path}/t.db",
                                          "storage_dir": str(tmp_path / "st"), **over})
    repo = SQLiteRepository(s); repo._migrate(); repo._seed()
    return repo


def test_user_config_drives_llm_client(tmp_path):
    repo = _repo(tmp_path)
    repo.set_user_model_settings("user-local",
        {"llm": {"base_url": "https://u/v1", "api_key": "sk-u", "model": "m-u"}})
    user = repo.current_user()
    tok = set_request_user(user)
    try:
        c = repo.llm_client
        assert c.base_url == "https://u/v1" and c.model == "m-u"
    finally:
        reset_request_user(tok)


def test_no_user_config_fallback_to_system_and_setter(tmp_path):
    repo = _repo(tmp_path)
    sentinel = object()
    repo.llm_client = sentinel   # setter 设系统默认(测试替身)
    assert repo.llm_client is sentinel   # 无用户配置 → 系统默认


def test_variant_uses_user_primary(tmp_path):
    repo = _repo(tmp_path)
    repo.set_user_model_settings("user-local",
        {"llm": {"base_url": "https://u/v1", "api_key": "sk-u", "model": "m-u"}})
    tok = set_request_user(repo.current_user())
    try:
        assert repo.kg_llm_client.model == "m-u"   # kg 未配 → 用户主 LLM
    finally:
        reset_request_user(tok)


def test_required_policy_unconfigured_is_sentinel(tmp_path):
    repo = _repo(tmp_path, user_model_config_policy="required")
    tok = set_request_user(repo.current_user())
    try:
        assert repo.llm_client.configured is False   # 未配 + required → 哨兵
    finally:
        reset_request_user(tok)
