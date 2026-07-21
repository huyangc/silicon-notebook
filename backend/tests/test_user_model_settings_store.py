import json
import threading
from concurrent.futures import ThreadPoolExecutor

from app.core.config import get_settings
from app.repositories.sqlite import identity_store as identity_store_module
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


def test_paused_old_read_cannot_repopulate_subsequent_effective_resolution(
    tmp_path, monkeypatch
):
    reader = _repo(tmp_path)
    writer = _repo(tmp_path)
    old = {
        "llm": {
            "base_url": "https://old.example/v1",
            "api_key": "old-secret",
            "model": "old-model",
        }
    }
    new = {
        "llm": {
            "base_url": "https://new.example/v1",
            "api_key": "new-secret",
            "model": "new-model",
        }
    }
    writer.set_user_model_settings("user-local", old)

    selected_old = threading.Event()
    release_old = threading.Event()
    real_decode = identity_store_module._decode_model_settings

    def pause_reader_after_select(value):
        if threading.current_thread().name.startswith("stale-settings-reader"):
            selected_old.set()
            assert release_old.wait(5)
        return real_decode(value)

    monkeypatch.setattr(
        identity_store_module, "_decode_model_settings", pause_reader_after_select
    )
    with ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="stale-settings-reader"
    ) as executor:
        stale_read = executor.submit(
            reader.get_user_model_settings, "user-local"
        )
        assert selected_old.wait(5)
        writer._runtime.identity.patch_user_model_settings_atomic(
            "user-local", new
        )
        release_old.set()
        assert stale_read.result(timeout=5) == old

    assert reader.resolve_model_config(
        reader.current_user(), "llm"
    ).model == "new-model"


def test_preloaded_reader_observes_another_repository_settings_commit(tmp_path):
    reader = _repo(tmp_path)
    writer = _repo(tmp_path)
    writer.set_user_model_settings("user-local", {
        "llm": {
            "base_url": "https://old.example/v1",
            "api_key": "old-secret",
            "model": "old-model",
        }
    })
    assert reader.resolve_model_config(
        reader.current_user(), "llm"
    ).model == "old-model"

    writer._runtime.identity.patch_user_model_settings_atomic(
        "user-local",
        {"llm": {"model": "new-model"}},
    )

    assert reader.get_user_model_settings("user-local")["llm"]["model"] == "new-model"
    assert reader.resolve_model_config(
        reader.current_user(), "llm"
    ).model == "new-model"


def test_policy_default_is_fallback():
    assert get_settings().user_model_config_policy == "fallback"
