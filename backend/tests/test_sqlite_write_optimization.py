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
    """Task 27 expanded write audit: every primary SQLite adapter under
    backend/app/repositories/sqlite/ (plus the compatibility facade) must run
    its writes under SqliteDatabase.write() — a write keyword inside a plain
    connect() block is an offender unless the function is an explicit
    migration/startup allowlist entry. (The old single-file facade scan is
    subsumed: the facade file is scanned alongside every adapter.)"""
    import pathlib
    import re
    backend = pathlib.Path(__file__).resolve().parents[1]
    targets = sorted((backend / "app" / "repositories" / "sqlite").glob("*.py"))
    targets.append(backend / "app" / "services" / "sqlite_repository.py")
    # SQL 写关键字; (?<!\.) 排除 Python 的同名方法调用(集合 .update()/列表 .insert()/
    # 字符串 .replace() 等)。真正的 SQL 写在字符串字面量里, 关键字前是引号/空格而非 ".", 仍会命中。
    WRITE = re.compile(r"(?<!\.)\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)
    # 自检: 必须命中真写, 又要放过同名方法调用(否则像 neighbour_ids.update() 那样误报)
    assert WRITE.search('db.execute("INSERT INTO t VALUES(?)")')
    assert WRITE.search('db.execute("UPDATE t SET x=? WHERE id=?")')
    assert not WRITE.search("neighbour_ids.update(r['x'] for r in rows)")
    assert not WRITE.search("buf.insert(0, item)")
    assert not WRITE.search('name.replace("a", "b")')
    # 读事务块的开启形态:facade 的 `with self._connect() as db:` 与组件层的
    # `with self._database.connect() as db:` / `with X.connect() as db:`。
    CONNECT_BLOCK = re.compile(r"with\s+[\w.]*(?:_connect|\.connect)\(\)\s+as\s+\w+:")
    WRITE_BLOCK = re.compile(r"with\s+[\w.]*(?:_write|\.write)\(\)\s+as\s+\w+:")
    # 起步单线程, 不并发, 豁免。_migration_N = 版本化 schema 迁移步骤(基线=_migration_1);
    # _recover_interrupted_jobs = 启动崩溃兜底。前者由 __init__ 在对外服务前调用,
    # 后者由服务端 lifespan(startup_warmup.run_startup)在就绪门之前调用一次。
    # CREATE TABLE DDL 里 "ON DELETE CASCADE" 外键子句触发 DELETE 关键字误报(非真实
    # DML 写), 故版本化迁移步骤(_migration_*)整类豁免。
    ALLOW_EXACT = {"_migrate", "_recover_interrupted_jobs", "_seed"}
    offenders = []
    for src in targets:
        cur = None
        in_block = False
        block_indent = 0
        for i, ln in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"\s*def (\w+)\(", ln)
            if m:
                cur = m.group(1)
                in_block = False
            if CONNECT_BLOCK.search(ln):
                in_block = True
                block_indent = len(ln) - len(ln.lstrip())
                continue
            if WRITE_BLOCK.search(ln):
                in_block = False
                continue
            if in_block:
                if ln.strip() and (len(ln) - len(ln.lstrip())) <= block_indent:
                    in_block = False
                elif WRITE.search(ln) and cur not in ALLOW_EXACT and not (
                    cur or ""
                ).startswith("_migration_"):
                    offenders.append((src.name, i, cur, ln.strip()[:70]))
    assert not offenders, f"这些写仍走 connect()(应改 write()): {offenders}"


@pytest.fixture
def embed_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")     # embedder_configured == True
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_BATCH_SIZE", "10")
    r = SQLiteRepository(Settings())

    class _FakeEmbedder:
        def embed_texts(self, texts):
            return [[0.1, 0.2, 0.3] for _ in texts]

    r.embedder = _FakeEmbedder()
    return r


def test_embed_objects_batch_writes_in_one_transaction(embed_repo, monkeypatch):
    nb = embed_repo.create_notebook(NotebookCreate(name="nb"))
    writes = {"n": 0}
    import contextlib
    real_write = embed_repo._write

    @contextlib.contextmanager
    def counting_write():
        writes["n"] += 1
        with real_write() as db:
            yield db

    monkeypatch.setattr(embed_repo, "_write", counting_write)
    items = [{"_oid": f"ko-{i}", "payload": {"name": f"concept number {i}"}} for i in range(35)]
    embed_repo._embed_objects_batch(nb.id, items)

    assert writes["n"] == 1                       # 35项/批10 = 4批, 但只 1 次写事务
    with embed_repo._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
    assert n == 35


def test_store_kg_chunks_large_insert(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [{"local_id": f"L{i}", "object_type": "concept",
             "payload": {"name": f"c{i}"}, "evidence": []} for i in range(2500)]
    rels = [{"source_local_id": "L0", "target_local_id": f"L{i}",
             "edge_type": "about", "evidence": []} for i in range(1, 2500)]
    n_obj, n_rel = repo.store_kg(nb.id, None, objs, rels)
    assert n_obj == 2500 and n_rel == 2499
    with repo._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchone()["c"] == 2500
        assert db.execute("SELECT COUNT(*) c FROM knowledge_relations WHERE notebook_id=?", (nb.id,)).fetchone()["c"] == 2499
        r = db.execute("SELECT source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id=? LIMIT 1", (nb.id,)).fetchone()
        assert db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE id=?", (r["source_object_id"],)).fetchone()["c"] == 1
        assert db.execute("SELECT COUNT(*) c FROM knowledge_objects WHERE id=?", (r["target_object_id"],)).fetchone()["c"] == 1


