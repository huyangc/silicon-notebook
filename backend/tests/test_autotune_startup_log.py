"""create_app 启动日志打印已解析的核绑定旋钮值。"""
import logging
import pytest


def test_startup_logs_autotune_line(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("KG_CLUSTER_ANN_THREADS", "13")
    from app.main import create_app
    with caplog.at_level(logging.INFO, logger="silicon_notebook.startup"):
        create_app()
    assert "kg_cluster_ann_threads=13" in caplog.text
