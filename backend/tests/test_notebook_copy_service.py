# backend/tests/test_notebook_copy_service.py
"""Task 9: NotebookCopyService — deep-copy orchestration (ID remap, chunked
transactions, filesystem copy, compensation) recomposed off the facade mixin.

Late-binding contract under test: the service must read the compatibility
seams (`sqlite_repository._new_id`, `sqlite_repository._COPY_CHUNK`, the
facade `_insert_row` seat) during EVERY operation, so patches applied AFTER
repository construction are observed.  Failure injection after the sentinel
insert and after the first table chunk must compensate ONLY the destination
rows/files — the source notebook and unrelated live sentinels stay untouched.
"""
import json
import uuid
from datetime import datetime

import pytest

from app.core.config import Settings
from app.services import sqlite_repository
from app.services.notebook_sharing import NotebookCopyService, NotebookSharingService


NOW = "2026-01-01T00:00:00"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return sqlite_repository.SQLiteRepository(Settings())


def _seed_user(repo, uid):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT OR IGNORE INTO users (id,email,display_name,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (uid, f"{uid}@t", uid, "user", NOW, NOW),
        )


def _seed_plain_nb(repo, owner="user-local", name="NB"):
    nb = f"nb-{uuid.uuid4().hex[:10]}"
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,purpose,primary_domain,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (nb, name, "", "Semiconductor", "draft", owner, NOW, NOW),
        )
    return nb


def _seed(repo, n=2, with_files=False, owner="user-local"):
    """Seed a notebook with n sources + n chunks + n knowledge objects."""
    nb = _seed_plain_nb(repo, owner=owner, name="Orig")
    with repo._runtime.database.write() as db:
        for i in range(n):
            s = f"src-{uuid.uuid4().hex[:8]}"
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,file_name,file_path,file_size,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (s, nb, f"S{i}", "document", "s.md", "", 1, NOW, NOW),
            )
            db.execute(
                "INSERT INTO chunks (id,notebook_id,source_id,text,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (f"ck-{uuid.uuid4().hex[:8]}", nb, s, f"chunk {i}", "[]", NOW),
            )
            db.execute(
                "INSERT INTO knowledge_objects (id,notebook_id,object_type,source_id,payload,evidence,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"ko-{uuid.uuid4().hex[:8]}", nb, "concept", s,
                 json.dumps({"name": f"x{i}"}), "[]", NOW, NOW),
            )
    if with_files:
        nb_dir = repo.storage_dir / "notebooks" / nb
        nb_dir.mkdir(parents=True, exist_ok=True)
        (nb_dir / "orig.md").write_text("payload", encoding="utf-8")
    return nb


def test_copy_observes_new_id_patched_after_construction(repo, monkeypatch):
    src = _seed(repo, n=2)
    _seed_user(repo, "user-bob")
    counter = {"n": 0}

    def fixed_new_id(prefix):
        counter["n"] += 1
        return f"{prefix}-t9fixed-{counter['n']:04d}"

    monkeypatch.setattr(sqlite_repository, "_new_id", fixed_new_id)
    copied = repo.copy_notebook(src, new_owner_id="user-bob")
    assert copied.id.startswith("nb-t9fixed-")
    with repo._runtime.database.connect() as db:
        source_ids = [
            r["id"] for r in db.execute(
                "SELECT id FROM sources WHERE notebook_id=?", (copied.id,)
            ).fetchall()
        ]
    assert source_ids and all(sid.startswith("src-t9fixed-") for sid in source_ids)


def test_notebook_copy_separates_owner_id_from_knowhow_genesis_actor(repo):
    src = _seed(repo, n=1)
    _seed_user(repo, "user-audit-copy")
    table_id = repo.create_knowhow_table(
        src, "T", "", [{"name": "C", "role": "attribute"}]
    )

    copied = repo.copy_notebook(
        src, new_owner_id="user-audit-copy", actor_label="a00123456"
    )

    copied_table = repo.list_knowhow_tables(copied.id)[0]
    assert repo.get_knowhow_table(copied_table["id"])["created_by"] == "user-audit-copy"
    assert repo.get_knowhow_change(copied_table["id"], 1)["actor"] == "a00123456"


def test_copy_observes_copy_chunk_patched_after_construction(repo, monkeypatch):
    src = _seed(repo, n=3)
    _seed_user(repo, "user-carol")
    transactions = {"n": 0}
    original_write = repo._runtime.database.write

    def counting_write():
        transactions["n"] += 1
        return original_write()

    monkeypatch.setattr(repo._runtime.database, "write", counting_write)
    repo.copy_notebook(src, new_owner_id="user-carol")
    baseline = transactions["n"]
    monkeypatch.setattr(sqlite_repository, "_COPY_CHUNK", 1)
    transactions["n"] = 0
    repo.copy_notebook(src, new_owner_id="user-carol")
    assert transactions["n"] > baseline, (
        "shrinking sqlite_repository._COPY_CHUNK after construction must force "
        "more (smaller) chunk transactions — the seam is read per operation"
    )


def test_failure_after_sentinel_compensates_only_destination(repo, monkeypatch):
    src = _seed(repo, n=2, with_files=True)
    _seed_user(repo, "user-dave")
    live = _seed_plain_nb(repo, owner="user-dave", name="active-copy")
    fresh = datetime.now().replace(microsecond=0).isoformat()
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE notebooks SET status='copying', created_at=? WHERE id=?",
            (fresh, live),
        )
    calls = {"n": 0}
    original_insert = repo._insert_row

    def boom_after_sentinel(db, table, data):
        calls["n"] += 1
        if calls["n"] == 2:  # call 1 = copying sentinel, call 2 = first sources row
            raise RuntimeError("boom after sentinel")
        return original_insert(db, table, data)

    monkeypatch.setattr(repo, "_insert_row", boom_after_sentinel)
    with pytest.raises(RuntimeError, match="boom after sentinel"):
        repo.copy_notebook(src, new_owner_id="user-dave")
    with repo._runtime.database.connect() as db:
        rows = db.execute(
            "SELECT id FROM notebooks WHERE created_by='user-dave' AND status='copying'"
        ).fetchall()
        assert [r["id"] for r in rows] == [live]  # live sentinel untouched
        assert db.execute(
            "SELECT COUNT(*) FROM sources WHERE notebook_id=?", (src,)
        ).fetchone()[0] == 2  # source rows untouched
    assert (repo.storage_dir / "notebooks" / src).exists()  # source files untouched
    leftover = [
        p.name for p in (repo.storage_dir / "notebooks").iterdir() if p.name != src
    ]
    assert leftover == []  # destination directory compensated


def test_failure_after_first_chunk_compensates_rows_and_files(repo, monkeypatch):
    src = _seed(repo, n=3, with_files=True)
    _seed_user(repo, "user-erin")
    monkeypatch.setattr(sqlite_repository, "_COPY_CHUNK", 1)
    calls = {"n": 0}
    original_insert = repo._insert_row

    def boom_mid_chunks(db, table, data):
        calls["n"] += 1
        if calls["n"] == 3:  # sentinel(1) + first sources chunk(2), boom in chunk 2
            raise RuntimeError("boom mid chunk")
        return original_insert(db, table, data)

    monkeypatch.setattr(repo, "_insert_row", boom_mid_chunks)
    with pytest.raises(RuntimeError, match="boom mid chunk"):
        repo.copy_notebook(src, new_owner_id="user-erin")
    with repo._runtime.database.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM notebooks WHERE created_by='user-erin'"
        ).fetchone()[0] == 0
        dangling = db.execute(
            "SELECT COUNT(*) FROM knowledge_embeddings WHERE notebook_id NOT IN "
            "(SELECT id FROM notebooks)"
        ).fetchone()[0]
        assert dangling == 0
    assert (repo.storage_dir / "notebooks" / src).exists()
    leftover = [
        p.name for p in (repo.storage_dir / "notebooks").iterdir() if p.name != src
    ]
    assert leftover == []


# ---------------------------------------------------------------------------
# Task 4 delegation test: NotebookSharingService.join_shared holds ZERO SQL —
# the on-snapshot notebook read is now SharingStore.notebook_row_on, reached on
# the same connection join_shared hydrates the summary from.  The spy wraps the
# real method (so from_row still gets a genuine row) and proves the delegation.
# Primary assertion tagged `# MUT`.
# ---------------------------------------------------------------------------


def test_t4deleg_notebook_row_on_delegate(repo, monkeypatch):
    # Seed via the file's raw-SQL helper and reach join_shared through the
    # service instance so this test adds no manifest-tracked facade-member
    # consumer sites (repo._runtime.sharing is not the `repo`/`*_repo` base the
    # surface scanner records).
    notebook_id = _seed_plain_nb(repo, owner="user-local", name="join")
    _seed_user(repo, "user-joiner")
    store = repo._runtime.sharing_store
    summaries = repo._runtime.sharing._summaries
    original_store = store.notebook_row_on  # staticmethod -> plain function
    original_summary = summaries.from_row
    store_dbs = []
    summary_dbs = []

    def spy_store(db, nb_id):
        store_dbs.append(db)
        return original_store(db, nb_id)

    def spy_summary(db, row):
        summary_dbs.append(db)
        return original_summary(db, row)

    monkeypatch.setattr(store, "notebook_row_on", spy_store)
    monkeypatch.setattr(summaries, "from_row", spy_summary)
    result = repo._runtime.sharing.join_shared(notebook_id, "user-joiner")
    assert len(store_dbs) == 1
    assert len(summary_dbs) == 1
    assert store_dbs[0] is summary_dbs[0]  # MUT
    assert result.id == notebook_id
    assert result.access == "reader"


def test_copy_resets_indexing_pipeline_columns_to_builtin(repo):
    """深拷贝复位索引管线四列(desired/version/generation/job authority)。

    published identity 住在 unified_kg_state 而它刻意不进深拷贝:照抄 desired 会让
    副本天生 desired≠published、每次写入 409 直到手动全库重建;照抄 job_id 会让副本
    的状态投影 join 到源库正在跑的 job 行。与 share_token/agent_profile_id 同款清洗。

    反向护栏(codex #602 R7 P2 驳回):复位到内建是**拍板取舍**,不是漏拷——副本里
    插件 chunk 与后续内建 chunk 的粒度异质已登记接受(所有身份消费方在副本上从零
    开始,详见 _reset_copied_notebook_row 的 docstring)。想改回「继承/种 published
    identity」的人必须先推翻那段论证,而不是把这里的断言当 bug 修掉。
    """
    from app.domain.indexing_pipeline import BUILTIN_INDEXING_PIPELINE_VERSION

    _seed_user(repo, "user-bob")
    src = _seed(repo, n=1)
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE notebooks SET indexing_pipeline='p.foo',"
            "indexing_pipeline_version='p.foo/v9',"
            "indexing_pipeline_generation='gen-1',"
            "indexing_pipeline_job_id='job-of-source-nb' WHERE id=?",
            (src,),
        )
    copied = repo.copy_notebook(src, new_owner_id="user-bob")
    with repo._runtime.database.connect() as db:
        copy_row = db.execute(
            "SELECT indexing_pipeline,indexing_pipeline_version,"
            "indexing_pipeline_generation,indexing_pipeline_job_id "
            "FROM notebooks WHERE id=?",
            (copied.id,),
        ).fetchone()
        source_row = db.execute(
            "SELECT indexing_pipeline,indexing_pipeline_job_id "
            "FROM notebooks WHERE id=?",
            (src,),
        ).fetchone()
    assert dict(copy_row) == {
        "indexing_pipeline": None,
        "indexing_pipeline_version": BUILTIN_INDEXING_PIPELINE_VERSION,
        "indexing_pipeline_generation": "",
        "indexing_pipeline_job_id": "",
    }
    # 源库自己的选择与在飞授权一个字不动。
    assert dict(source_row) == {
        "indexing_pipeline": "p.foo",
        "indexing_pipeline_job_id": "job-of-source-nb",
    }
