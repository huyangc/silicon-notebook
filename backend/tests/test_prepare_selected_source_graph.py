from __future__ import annotations

import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "prepare_selected_source_graph.py"


def _load_module():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(
            "prepare_selected_source_graph_test_module", SCRIPT
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(ROOT / "scripts"))


def test_render_enabled_env_replaces_duplicates_and_preserves_unrelated_lines():
    module = _load_module()
    original = (
        "DATABASE_URL=sqlite:///tmp/library.db\n"
        "export SOURCE_SUBGRAPH_PPR_ENABLED=false\n"
        "SOURCE_SUBGRAPH_PPR_ENABLED=0\n"
        "# SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=off\n"
    )

    rendered = module.render_enabled_env(original)

    assert rendered.count("SOURCE_SUBGRAPH_PPR_ENABLED=true") == 1
    assert "SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED=true" in rendered
    assert "SOURCE_PARTITIONED_PPR_ENABLED=true" in rendered
    assert "SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=shadow" in rendered
    assert "DATABASE_URL=sqlite:///tmp/library.db" in rendered
    assert "# SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=off" in rendered


def test_settings_target_is_authoritative_env_file(monkeypatch, tmp_path):
    module = _load_module()
    file_database = tmp_path / "from-file.db"
    process_database = tmp_path / "from-process.db"
    file_storage = tmp_path / "from-file-storage"
    process_storage = tmp_path / "from-process-storage"
    env_file = tmp_path / "deployment.env"
    env_file.write_text(
        f"DATABASE_URL=sqlite:///{file_database}\n"
        f"SILICON_NOTEBOOK_STORAGE_DIR={file_storage}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{process_database}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(process_storage))

    settings = module._settings(env_file)

    assert module.database_identity(settings.database_url).database == str(file_database)
    assert settings.storage_dir == str(file_storage)


class _Maintenance:
    def __init__(self, *, unavailable: int = 0) -> None:
        self.unavailable = unavailable
        self.artifact_ready = False

    def all_notebook_ids(self):
        return ["nb-1"]

    def user_source_ids_page(self, notebook_id, *, after_id="", limit=500):
        assert notebook_id == "nb-1"
        return ["src-1"] if not after_id else []

    def selected_source_graph_artifact_status(self, notebook_id):
        assert notebook_id == "nb-1"
        return {
            "ready": self.artifact_ready,
            "n_nodes": 7 if self.artifact_ready else 0,
            "published_sources": 1 - self.unavailable,
            "unavailable_sources": self.unavailable,
        }


class _Repository:
    def __init__(self, storage_dir: Path, *, unavailable: int = 0) -> None:
        self.storage_dir = storage_dir
        self.maintenance = _Maintenance(unavailable=unavailable)
        self.build_calls = 0

    def build_scale_index(self, notebook_id):
        assert notebook_id == "nb-1"
        self.build_calls += 1
        self.maintenance.artifact_ready = True
        return {"n_nodes": 7}


def _patch_workflow(monkeypatch, module, repository, lifecycle):
    @contextmanager
    def _open(*_args, **_kwargs):
        lifecycle.append("opened")
        try:
            yield repository
        finally:
            lifecycle.append("closed")

    monkeypatch.setattr(module, "open_maintenance_cli_repository", _open)
    monkeypatch.setattr(
        module,
        "run_backfill_source_index",
        lambda *_args, **_kwargs: {"objects": 3, "rows": 4},
    )
    monkeypatch.setattr(
        module,
        "run_backfill_source_facts",
        lambda *_args, **_kwargs: {
            "notebooks": [
                {
                    "sources": 1,
                    "complete": 1,
                    "incomplete": 0,
                    "busy": 0,
                    "no_generation": 0,
                    "failed": 0,
                    "facts_written": 3,
                }
            ]
        },
    )
    monkeypatch.setattr(
        module,
        "_audit_notebooks",
        lambda *_args, **_kwargs: {
            "sources": 1,
            "persisted_facts": 3,
            "expected_objects": 3,
        },
    )


def test_run_preparation_updates_env_only_after_database_work(monkeypatch, tmp_path):
    module = _load_module()
    lifecycle: list[str] = []
    repository = _Repository(tmp_path / "storage")
    _patch_workflow(monkeypatch, module, repository, lifecycle)
    original_enable = module._enable_env

    def _enable(path):
        assert lifecycle == ["opened", "closed"]
        lifecycle.append("env")
        original_enable(path)

    monkeypatch.setattr(module, "_enable_env", _enable)
    settings = SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'library.db'}",
        storage_dir=repository.storage_dir,
    )
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED=kept\n", encoding="utf-8")
    env_file.chmod(0o640)
    original_stat = env_file.stat()
    receipt_path = tmp_path / "receipt.json"

    result = module.run_preparation(
        settings=settings,
        env_file=env_file,
        receipt_path=receipt_path,
        confirm_service_stopped=True,
    )

    assert lifecycle == ["opened", "closed", "env"]
    assert result["status"] == "complete"
    assert result["env_updated"] is True
    assert result["receipt_updated"] is True
    assert result["notebooks"]["nb-1"]["phase"] == "audited"
    assert "SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=shadow" in env_file.read_text()
    updated_stat = env_file.stat()
    assert updated_stat.st_mode & 0o777 == 0o640
    assert (updated_stat.st_uid, updated_stat.st_gid) == (
        original_stat.st_uid,
        original_stat.st_gid,
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "complete"
    assert receipt["failure_code"] == ""
    assert repository.build_calls == 1


def test_completed_artifacts_are_revalidated_and_skipped_on_resume(
    monkeypatch, tmp_path
):
    module = _load_module()
    repository = _Repository(tmp_path / "storage")
    repository.maintenance.artifact_ready = True
    _patch_workflow(monkeypatch, module, repository, [])
    settings = SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'library.db'}",
        storage_dir=repository.storage_dir,
    )

    result = module.run_preparation(
        settings=settings,
        env_file=tmp_path / ".env",
        receipt_path=tmp_path / "receipt.json",
        confirm_service_stopped=True,
    )

    assert result["status"] == "complete"
    assert result["notebooks"]["nb-1"]["indexed_nodes"] == 7
    assert repository.build_calls == 0


def test_incomplete_partition_fails_without_touching_env(monkeypatch, tmp_path):
    module = _load_module()
    repository = _Repository(tmp_path / "storage", unavailable=1)
    _patch_workflow(monkeypatch, module, repository, [])
    settings = SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'library.db'}",
        storage_dir=repository.storage_dir,
    )
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED=kept\n", encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"

    with pytest.raises(module.PreparationError) as caught:
        module.run_preparation(
            settings=settings,
            env_file=env_file,
            receipt_path=receipt_path,
            confirm_service_stopped=True,
        )

    assert caught.value.code == "partition_artifact_incomplete"
    assert env_file.read_text(encoding="utf-8") == "UNRELATED=kept\n"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert receipt["failure_code"] == "partition_artifact_incomplete"
    assert "exception" not in json.dumps(receipt)


def test_service_stop_confirmation_is_required_before_receipt_write(tmp_path):
    module = _load_module()
    settings = SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'library.db'}",
        storage_dir=tmp_path / "storage",
    )
    receipt_path = tmp_path / "receipt.json"

    with pytest.raises(module.PreparationError) as caught:
        module.run_preparation(
            settings=settings,
            env_file=tmp_path / ".env",
            receipt_path=receipt_path,
            confirm_service_stopped=False,
        )

    assert caught.value.code == "service_stop_confirmation_required"
    assert not receipt_path.exists()


def test_invalid_receipt_identity_fails_closed(tmp_path):
    module = _load_module()
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(
        json.dumps(
            {
                "campaign_version": "not-an-integer",
                "database_fingerprint": "wrong",
                "notebooks": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(module.PreparationError) as caught:
        module._load_receipt(receipt_path, "expected")

    assert caught.value.code == "receipt_identity_mismatch"


def test_main_prepares_empty_sqlite_database_and_enables_env(monkeypatch, tmp_path):
    module = _load_module()
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_STORAGE_DIR", raising=False)
    env_file = tmp_path / "deployment.env"
    database = tmp_path / "library.db"
    storage = tmp_path / "storage"
    env_file.write_text(
        f"DATABASE_URL=sqlite:///{database}\nSILICON_NOTEBOOK_STORAGE_DIR={storage}\n",
        encoding="utf-8",
    )

    exit_code = module.main(
        ["--env-file", str(env_file), "--confirm-service-stopped"]
    )

    assert exit_code == 0
    assert database.exists()
    assert "SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=shadow" in env_file.read_text(
        encoding="utf-8"
    )
    receipt = json.loads(
        (storage / "maintenance" / "selected-source-graph-deployment.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["status"] == "complete"
    assert receipt["notebook_count"] == 0


def test_main_rejects_missing_env_file_without_creating_it(tmp_path):
    module = _load_module()
    env_file = tmp_path / "missing.env"

    exit_code = module.main(
        ["--env-file", str(env_file), "--confirm-service-stopped"]
    )

    assert exit_code == 1
    assert not env_file.exists()


def test_final_receipt_failure_does_not_reverse_committed_env(
    monkeypatch, tmp_path
):
    module = _load_module()
    repository = _Repository(tmp_path / "storage")
    _patch_workflow(monkeypatch, module, repository, [])
    settings = SimpleNamespace(
        database_url=f"sqlite:///{tmp_path / 'library.db'}",
        storage_dir=repository.storage_dir,
    )
    env_file = tmp_path / ".env"
    env_file.write_text("UNRELATED=kept\n", encoding="utf-8")
    receipt_path = tmp_path / "receipt.json"
    original_write = module._write_receipt

    def _write(path, receipt):
        if receipt.get("env_updated"):
            raise OSError("receipt unavailable")
        original_write(path, receipt)

    monkeypatch.setattr(module, "_write_receipt", _write)

    result = module.run_preparation(
        settings=settings,
        env_file=env_file,
        receipt_path=receipt_path,
        confirm_service_stopped=True,
    )

    assert result["status"] == "complete"
    assert result["receipt_updated"] is False
    assert "SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=shadow" in env_file.read_text(
        encoding="utf-8"
    )
