"""来源的 Agent 出处（SQLite v48 / PostgreSQL v26 的 `sources.agent_profile_id`）。

三段合同，对应三组用例：

1. **迁移**——全新库带上这一列、已部署 v47 库开库时补建（不改旧迁移）；
2. **写路径穿透**——上传 / 加链接带上 `agent_profile_id` 就落该值，
   不带就落 NULL；投影只出一个布尔 `agent_created`，原始 id 不上 wire；
3. **去重复用不改写出处**——内容去重命中既有行时，那一行的出处保持首写时的样子。
   这是**安全语义**不是巧合：用户先传的内容被 Agent 再传一次，那一行必须仍然算
   「人添加的」，否则后续 delete_source 工具的权限判据（「该来源由 Agent 添加」）
   就被一次重复上传绕开了。反方向同样钉住——Agent 先传、用户再传，行不会因为
   一次普通上传就变成「人添加的」（那会静默剥夺 Agent 清理自己产物的能力）。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.ports import UploadedSourceFile
from app.repositories.sqlite.migrations import SCHEMA_VERSION
from app.services import remote_sources
from app.services.remote_sources import PdfProbe
from app.services.sqlite_repository import SQLiteRepository


AGENT = "ap-agent-1"


def _settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return Settings(_env_file=None)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = SQLiteRepository(_settings(tmp_path, monkeypatch))
    # 不传 scheduler 时 upload_sources 会同步跑完整 parse→extract 流水线；
    # 这些用例只关心出处列，把它打桩成「只记录调用」（同 test_upload_dedup）。
    monkeypatch.setattr(
        r._runtime.source_ingestion, "process_source", lambda sid, hooks: None
    )
    return r


@pytest.fixture
def notebook_id(repo):
    return repo.create_notebook(NotebookCreate(name="nb")).id


def _upload(repo, notebook_id, *, content=b"hello world", name="a.txt", agent=""):
    return repo.upload_sources(
        notebook_id,
        [UploadedSourceFile(
            file_name=name, content_type="text/plain", content=content,
            doc_type="", doc_type_explicit=False,
        )],
        None,
        agent,
    )


def _stored_provenance(repo, source_id: str):
    with repo._connect() as db:
        return db.execute(
            "SELECT agent_profile_id FROM sources WHERE id=?", (source_id,)
        ).fetchone()["agent_profile_id"]


# --------------------------------------------------------------- 1. migration


def test_fresh_database_carries_the_provenance_column(repo):
    with repo._connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(sources)")}
        assert "agent_profile_id" in columns
        # 可空、无默认：NULL 就是「人添加的」，迁移不回填任何存量行。
        assert columns["agent_profile_id"][3] == 0          # notnull
        assert columns["agent_profile_id"][4] is None       # dflt_value
        # 刻意不建索引（判据是主键单行读），也不加唯一约束。
        indexed = {
            row["name"] for row in db.execute("PRAGMA index_list(sources)").fetchall()
        }
        for index in indexed:
            names = {
                row["name"]
                for row in db.execute(f"PRAGMA index_info({index})").fetchall()
            }
            assert "agent_profile_id" not in names


def test_deployed_v47_database_gains_the_column_on_open(repo, tmp_path):
    """已部署库补建：回滚到 v47 再开一次，v48 必须补上这一列。"""
    database = tmp_path / "t.db"
    repo.close_local()
    with sqlite3.connect(database) as rollback:
        rollback.execute("ALTER TABLE sources DROP COLUMN agent_profile_id")
        rollback.execute("PRAGMA user_version = 47")

    reopened = SQLiteRepository(Settings(_env_file=None))
    try:
        with reopened._connect() as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            columns = {row[1] for row in db.execute("PRAGMA table_info(sources)")}
            assert "agent_profile_id" in columns
    finally:
        reopened.close_local()


# ------------------------------------------------------- 2. write-path passthrough


def test_upload_without_an_agent_stores_null_and_projects_false(repo, notebook_id):
    uploaded = _upload(repo, notebook_id)
    assert uploaded[0].agent_created is False
    assert _stored_provenance(repo, uploaded[0].id) is None
    assert repo.get_source(uploaded[0].id).agent_created is False
    assert repo.list_sources(notebook_id)[0].agent_created is False


def test_agent_upload_stores_the_profile_and_projects_true(repo, notebook_id):
    uploaded = _upload(repo, notebook_id, agent=AGENT)
    assert uploaded[0].agent_created is True
    assert _stored_provenance(repo, uploaded[0].id) == AGENT
    assert repo.get_source(uploaded[0].id).agent_created is True
    assert repo.list_sources(notebook_id)[0].agent_created is True


def test_the_raw_profile_id_never_reaches_the_wire(repo, notebook_id):
    """出处只上一个布尔：原始 agent id 是服务端的删除判据，不是响应字段。"""
    uploaded = _upload(repo, notebook_id, agent=AGENT)
    payload = repo.get_source(uploaded[0].id).model_dump()
    assert "agent_profile_id" not in payload
    assert AGENT not in str(payload)


def test_add_url_sources_stamps_the_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("MINERU_API_TOKEN", "tok-test")
    monkeypatch.setenv("MINERU_MODE", "off")
    repo = SQLiteRepository(_settings(tmp_path, monkeypatch))
    monkeypatch.setattr(
        remote_sources, "probe_pdf",
        lambda url, **kw: PdfProbe(True, "", 123, "doc.pdf"),
    )
    notebook_id = repo.create_notebook(NotebookCreate(name="n")).id

    created = repo.add_url_sources(
        notebook_id, ["https://a/doc.pdf"], lambda sid: None, None, AGENT
    ).created
    assert [s.agent_created for s in created] == [True]
    assert _stored_provenance(repo, created[0].id) == AGENT

    plain = repo.add_url_sources(
        notebook_id, ["https://b/doc.pdf"], lambda sid: None
    ).created
    assert [s.agent_created for s in plain] == [False]
    assert _stored_provenance(repo, plain[0].id) is None


# ------------------------------------------------- 3. dedup reuse keeps provenance


def test_agent_reupload_of_a_user_source_stays_user_added(repo, notebook_id):
    """安全语义：Agent 重传用户已经传过的内容，那一行仍然是「人添加的」。

    否则「该来源由 Agent 添加」这个删除判据只要一次重复上传就能被翻过来。
    """
    first = _upload(repo, notebook_id)
    again = _upload(repo, notebook_id, agent=AGENT)

    assert again[0].id == first[0].id and again[0].reused is True
    assert _stored_provenance(repo, first[0].id) is None
    assert again[0].agent_created is False
    assert len(repo.list_sources(notebook_id)) == 1


def test_user_reupload_of_an_agent_source_stays_agent_added(repo, notebook_id):
    """反方向同样不改写：复用路径一个字都不碰出处列。"""
    first = _upload(repo, notebook_id, agent=AGENT)
    again = _upload(repo, notebook_id)

    assert again[0].id == first[0].id and again[0].reused is True
    assert _stored_provenance(repo, first[0].id) == AGENT
    assert again[0].agent_created is True


def test_a_second_agent_does_not_take_over_an_existing_row(repo, notebook_id):
    first = _upload(repo, notebook_id, agent=AGENT)
    again = _upload(repo, notebook_id, agent="ap-agent-2")

    assert again[0].id == first[0].id
    assert _stored_provenance(repo, first[0].id) == AGENT


@pytest.mark.parametrize(
    "first_agent,second_agent",
    [("", AGENT), (AGENT, ""), (AGENT, "ap-agent-2"), ("   ", AGENT)],
)
def test_the_atomic_insert_never_restamps_the_row_it_hands_back(
    repo, notebook_id, first_agent, second_agent
):
    """同一条规则在 store 层单独钉一遍——`insert_source_if_absent` 自己的复用分支。

    上传服务有一条**快路径**（`source_id_by_hash` 命中就直接 `reuse_uploaded_source`），
    所以上面那几个用例根本走不到这里：`insert_source_if_absent` 的「查到既有行就返回」
    分支只在并发首传输掉的那一次才被执行。变异验证证实了这一点——只钉服务层的话，
    在这个分支里加一条 UPDATE 不会让任何用例变红。
    """
    assert repo._runtime.source_store.insert_source_if_absent(
        source_id="src-first",
        notebook_id=notebook_id,
        digest="digest-shared",
        title="shared bytes",
        source_type="markdown",
        status="queued",
        parse_status="queued",
        file_name="shared.md",
        file_path="/tmp/shared.md",
        file_size=1,
        summary="",
        doc_type="",
        agent_profile_id=first_agent,
    ) is None
    assert repo._runtime.source_store.insert_source_if_absent(
        source_id="src-second",
        notebook_id=notebook_id,
        digest="digest-shared",
        title="shared bytes",
        source_type="markdown",
        status="queued",
        parse_status="queued",
        file_name="shared.md",
        file_path="/tmp/shared.md",
        file_size=1,
        summary="",
        doc_type="",
        agent_profile_id=second_agent,
    ) == "src-first"
    # 空白串与 "" 是同一句话（「没有 Agent」），一起折成 NULL。
    assert _stored_provenance(repo, "src-first") == (first_agent.strip() or None)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n", None])
def test_whitespace_only_provenance_folds_to_null(repo, notebook_id, blank):
    """`"   "` 不是一个 Agent 身份。

    不 strip 的话它会落成非 NULL，于是 `agent_created` 报 True、来源被判成
    「Agent 添加的」——却指不出任何一个 Agent，删除判据凭空多了一类可删行。
    两个后端用逐字相同的表达式归一。
    """
    uploaded = _upload(repo, notebook_id, agent=blank or "")
    assert _stored_provenance(repo, uploaded[0].id) is None
    assert uploaded[0].agent_created is False


# ------------------------------------------------- 4. deep copy drops provenance


def test_deep_copy_makes_every_row_user_added(repo, notebook_id):
    """整本深拷贝把出处清成 NULL——副本行是**用户的拷贝动作**建的。

    与同一段里的 `memory_id` 同一个理由：那不是一个跟着副本走的身份。方向是
    保守的——清空只会把副本行放进「Agent 不得删除」的保护类，而带过去会让某个
    Agent 在一个它从没碰过的笔记本里凭空获得删除权限。

    必须落 NULL 而不是 `""`：投影按 `IS NOT NULL` 派生，空串会读成「Agent 加的」。
    """
    agent_source = _upload(repo, notebook_id, agent=AGENT, name="agent.txt")[0]
    user_source = _upload(
        repo, notebook_id, content=b"human bytes", name="human.txt"
    )[0]
    assert _stored_provenance(repo, agent_source.id) == AGENT

    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT OR IGNORE INTO users "
            "(id,email,display_name,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("user-copier", "copier@t", "copier", "user",
             "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
    copied = repo.copy_notebook(notebook_id, new_owner_id="user-copier")

    copied_sources = repo.list_sources(copied.id)
    assert len(copied_sources) == 2
    assert [item.agent_created for item in copied_sources] == [False, False]
    with repo._connect() as db:
        provenance = [
            row["agent_profile_id"]
            for row in db.execute(
                "SELECT agent_profile_id FROM sources WHERE notebook_id=?",
                (copied.id,),
            ).fetchall()
        ]
    assert provenance == [None, None]
    # 原库一个字没动。
    assert _stored_provenance(repo, agent_source.id) == AGENT
    assert _stored_provenance(repo, user_source.id) is None
