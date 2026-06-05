import threading
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    return SQLiteRepository(Settings())


def test_connect_enables_wal(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    with r._connect() as db:
        mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"


def test_concurrent_writers_do_not_lock(tmp_path, monkeypatch):
    r = _repo(tmp_path, monkeypatch)
    with r._connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS scratch (k INTEGER)")
    errors = []

    def writer(start):
        try:
            with r._connect() as db:
                for i in range(start, start + 200):
                    db.execute("INSERT INTO scratch (k) VALUES (?)", (i,))
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=writer, args=(s,)) for s in (0, 1000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [], errors
    with r._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM scratch").fetchone()[0]
    assert n == 400
