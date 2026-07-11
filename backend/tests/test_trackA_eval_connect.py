"""T1: eval _connect leaks — public method coverage + grep gate."""
import inspect
import app.eval.speed as speed_mod


def test_insert_source_does_not_use_connect():
    """_insert_source must not call repo._connect() at all."""
    src = inspect.getsource(speed_mod._insert_source)
    assert "_connect" not in src, "_insert_source still calls _connect()"


def test_cleanup_does_not_use_connect():
    """_cleanup must not call repo._connect() at all."""
    src = inspect.getsource(speed_mod._cleanup)
    assert "_connect" not in src, "_cleanup still calls _connect()"


def test_repo_has_eval_insert_source_for_test():
    from app.services.sqlite_repository import SQLiteRepository
    assert hasattr(SQLiteRepository, "eval_insert_source_for_test"), \
        "SQLiteRepository missing eval_insert_source_for_test"


def test_production_protocol_excludes_eval_insert_source_for_test():
    from app.services.repository import NotebookRepository
    assert not hasattr(NotebookRepository, "eval_insert_source_for_test"), \
        "test-only eval helper leaked into production Protocol"


def test_cleanup_delegates_to_delete_notebook(tmp_path, monkeypatch):
    """_cleanup(repo, nb_id) must call repo.delete_notebook(nb_id), not raw SQL."""
    import app.eval.speed as speed_mod
    deleted = []

    class FakeRepo:
        def delete_notebook(self, nb_id):
            deleted.append(nb_id)

    speed_mod._cleanup(FakeRepo(), "nb-test")
    assert deleted == ["nb-test"]
