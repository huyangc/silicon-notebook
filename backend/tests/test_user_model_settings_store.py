import json
import threading
from concurrent.futures import ThreadPoolExecutor

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


def test_atomic_patch_preserves_none_clears_empty_and_sets_nonempty(tmp_path):
    repo = _repo(tmp_path)
    repo.set_user_model_settings("user-local", {
        "llm": {
            "base_url": "https://old.example/v1",
            "api_key": "old-secret",
            "model": "old-model",
        }
    })

    updated = repo._runtime.identity.patch_user_model_settings_atomic(
        "user-local",
        {
            "llm": {
                "base_url": None,
                "api_key": "",
                "model": "new-model",
            },
            "rerank": None,
        },
    )

    assert updated == {
        "llm": {
            "base_url": "https://old.example/v1",
            "model": "new-model",
        }
    }
    assert repo.get_user_model_settings("user-local") == updated


def test_concurrent_atomic_patches_do_not_lose_updates_or_retain_stale_caches(
    tmp_path, monkeypatch
):
    repo_a = _repo(tmp_path)
    repo_b = _repo(tmp_path)
    store_a = repo_a._runtime.identity
    store_b = repo_b._runtime.identity
    assert store_a.get_user_model_settings("user-local") == {}
    assert store_b.get_user_model_settings("user-local") == {}

    start = threading.Barrier(2, timeout=5)
    begin_a = store_a.database.begin_immediate
    begin_b = store_b.database.begin_immediate

    def synchronized_begin_a(connection):
        start.wait()
        begin_a(connection)

    def synchronized_begin_b(connection):
        start.wait()
        begin_b(connection)

    monkeypatch.setattr(store_a.database, "begin_immediate", synchronized_begin_a)
    monkeypatch.setattr(store_b.database, "begin_immediate", synchronized_begin_b)

    with ThreadPoolExecutor(max_workers=2) as executor:
        llm = executor.submit(
            store_a.patch_user_model_settings_atomic,
            "user-local",
            {"llm": {"base_url": "https://llm.example/v1", "model": "llm-model"}},
        )
        rerank = executor.submit(
            store_b.patch_user_model_settings_atomic,
            "user-local",
            {"rerank": {"base_url": "https://rerank.example/v1", "model": "rerank-model"}},
        )
        llm.result(timeout=10)
        rerank.result(timeout=10)

    expected = {
        "llm": {"base_url": "https://llm.example/v1", "model": "llm-model"},
        "rerank": {"base_url": "https://rerank.example/v1", "model": "rerank-model"},
    }
    assert store_a.get_user_model_settings("user-local") == expected
    assert store_b.get_user_model_settings("user-local") == expected


def test_policy_default_is_fallback():
    assert get_settings().user_model_config_policy == "fallback"
