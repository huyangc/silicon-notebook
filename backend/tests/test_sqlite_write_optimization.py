import threading
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_connect_sets_performance_pragmas(repo):
    with repo._connect() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1        # NORMAL
        assert db.execute("PRAGMA temp_store").fetchone()[0] == 2         # MEMORY
        assert db.execute("PRAGMA mmap_size").fetchone()[0] == 268435456  # 256MB
        assert db.execute("PRAGMA cache_size").fetchone()[0] == -65536    # 64MB


def test_concurrent_store_kg_no_database_locked(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    errors = []

    def worker(k):
        try:
            objs = [{"local_id": f"{k}-{i}", "object_type": "concept",
                     "payload": {"name": f"c{k}-{i}"}, "evidence": []} for i in range(60)]
            repo.store_kg(nb.id, None, objs, [])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    ts = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    with repo._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
    assert n == 8 * 60


def test_all_writes_go_through_write_lock():
    import pathlib
    import re
    src = pathlib.Path(__file__).resolve().parents[1] / "app" / "services" / "sqlite_repository.py"
    lines = src.read_text(encoding="utf-8").splitlines()
    WRITE = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
    ALLOW = {"_migrate", "_seed"}            # 起步单线程, 不并发, 豁免
    cur = None
    in_block = False
    block_indent = 0
    offenders = []
    for i, ln in enumerate(lines, 1):
        m = re.match(r"\s*def (\w+)\(", ln)
        if m:
            cur = m.group(1)
        if "with self._connect() as db:" in ln:
            in_block = True
            block_indent = len(ln) - len(ln.lstrip())
            continue
        if in_block:
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= block_indent:
                in_block = False
            elif WRITE.search(ln) and cur not in ALLOW:
                offenders.append((i, cur, ln.strip()[:70]))
    assert not offenders, f"这些写仍走 _connect()(应改 _write()): {offenders}"
