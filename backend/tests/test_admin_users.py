from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    return SQLiteRepository(Settings())


def _seed(repo):
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        # 两个用户(notebooks.created_by 是 FK→users(id),必须先建用户)
        for uid, uname in (("u1", "a00000001"), ("u2", "b00000002")):
            db.execute(
                "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@x", uid.upper(), "user", "active", uname, now, now),
            )
        # u1: 2 个正常 notebook + 1 个 copying(应被排除);u2: 0
        for nid, status in (("n1", "ready"), ("n2", "ready"), ("n3", "copying")):
            db.execute(
                "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (nid, nid, "u1", status, now, now),
            )
        # u1 在 n1 下:2 个 source、1 个 report、1 个 conversation，提交 2 次提问；
        # u2 作为共享成员在同一 notebook 另有 1 个 conversation/提问。
        # 提问次数必须数 ask_jobs，失败/取消也属于一次已提交问题。
        for sid in ("s1", "s2"):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,created_at,updated_at,uploaded_by) "
                "VALUES (?,?,?,?,?,?,?)", (sid, "n1", sid, "md", now, now, "u1"),
            )
        # 两份报告都建在 u1 的 notebook n1 里,但创建者不同:r1 是 owner 自己建的,
        # r2 是共享成员 u2 在同一本库里建的**他自己的**报告(群组知识共享 P1)。
        # 用量必须按 created_by 归集——按 notebook owner 归集会把 r2 记到 u1 头上。
        for report_id, creator in (("r1", "u1"), ("r2", "u2")):
            db.execute(
                "INSERT INTO reports (id,notebook_id,question,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (report_id, "n1", "q?", creator, now, now),
            )
        db.execute(
            "INSERT INTO conversations (id,notebook_id,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", ("c1", "n1", "u1", "2026-07-06T10:00:00", "2026-07-06T12:00:00"),
        )
        db.execute(
            "INSERT INTO conversations (id,notebook_id,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", ("c2", "n1", "u2", "2026-07-05T10:00:00", "2026-07-05T12:00:00"),
        )
        for job_id, conversation_id, creator, question, status in (
            ("j1", "c1", "u1", "first?", "completed"),
            ("j2", "c1", "u1", "second?", "failed"),
            ("j3", "c2", "u2", "shared?", "cancelled"),
        ):
            db.execute(
                "INSERT INTO ask_jobs "
                "(id,notebook_id,conversation_id,created_by,mode,question,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (job_id, "n1", conversation_id, creator, "chunk", question, status, now, now),
            )


def test_list_user_usage_counts(repo):
    _seed(repo)
    rows = {r["username"]: r for r in repo.list_user_usage()}
    # Should include u1, u2 and the auto-created admin user; check only the two we seeded
    assert "a00000001" in rows and "b00000002" in rows
    a = rows["a00000001"]
    assert a["id"] == "u1"
    assert a["role"] == "user"
    assert a["notebooks"] == 2          # copying 被排除
    assert a["sources"] == 2
    assert a["conversations"] == 1      # 兼容旧 API 字段
    assert a["questions"] == 2
    # 只数 u1 自己建的那一份;u2 在同一本库里建的 r2 不算 u1 的用量。
    assert a["reports"] == 1
    assert a["last_active"] == "2026-07-07T00:00:00"
    b = rows["b00000002"]
    assert b["notebooks"] == 0 and b["sources"] == 0
    assert b["conversations"] == 1 and b["questions"] == 1
    # u2 一本自己的库都没有,但他在别人的共享库里建了一份报告——按创建者归集,
    # 这一份必须记在他头上(与 `questions` 含共享库提交是同一条口径)。
    assert b["reports"] == 1
    assert b["last_active"] == "2026-07-07T00:00:00"


def test_list_user_usage_sources_matches_last_active_predicate(repo):
    """Phase A 来源口径修正:用户级「来源」总数与 last_active 的上传候选同一
    谓词——只算 live 笔记本 + 可见来源,归因
    COALESCE(NULLIF(uploaded_by,''), nb.created_by)。见规格
    docs/superpowers/specs/2026-09-07-admin-usage-overview-usage-signals-design_zh.md
    §3 Phase A。retained 行按 actor_id 计的覆盖见
    test_admin_user_activity.py 的
    test_deleted_shared_upload_keeps_actor_and_owner_accounting_separate /
    test_deleted_notebook_keeps_only_expiring_activity_metadata(那两个测试
    在本次修正里已经按新口径更新)。
    """
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        for uid, uname in (("u1", "a00000001"), ("u2", "b00000002")):
            db.execute(
                "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@x", uid.upper(), "user", "active", uname, now, now),
            )
        # u1 拥有一本 live 库 n1、一本 copying 库 n-copy(其中的来源不计)。
        for nid, status in (("n1", "ready"), ("n-copy", "copying")):
            db.execute(
                "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (nid, nid, "u1", status, now, now),
            )
        # n1 下 5 个来源:
        #  s-self   u1 自己上传(uploaded_by='u1')——计入 u1。
        #  s-shared u2 上传到 u1 的共享库(uploaded_by='u2')——计入 u2,不计
        #           u1;来源数是**资产**口径,按实际上传者归因,即使库属于
        #           u1(规格 §7 决策 2,与 last_active 同一谓词)。
        #  s-null   uploaded_by 为 NULL——深拷贝副本或极早期未回填行,
        #           COALESCE 落到 nb.created_by=u1。
        #  s-blank  uploaded_by 为空串——插入路径把空白折成 NULL,今天不可达,
        #           但 NULLIF(uploaded_by,'') 就是为它存在的防御:少了它这行
        #           会归到 '' 这个不存在的用户名下、从 u1 的计数里消失。
        #  s-memory / s-knowhow 合成来源(source_type 落在 memory/knowhow),
        #           VISIBLE_SOURCE_TYPES_PREDICATE 排除,不计入任何人。
        for sid, source_type, uploaded_by in (
            ("s-self", "pdf", "u1"),
            ("s-shared", "pdf", "u2"),
            ("s-null", "pdf", None),
            ("s-blank", "pdf", ""),
            ("s-memory", "memory", "u1"),
            ("s-knowhow", "knowhow", "u1"),
        ):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,created_at,updated_at,uploaded_by) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, "n1", sid, source_type, now, now, uploaded_by),
            )
        # copying 库里的来源即使显式给了 uploaded_by 也不计。
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,created_at,updated_at,uploaded_by) "
            "VALUES (?,?,?,?,?,?,?)",
            ("s-copying", "n-copy", "s-copying", "pdf", now, now, "u1"),
        )
    usage = {row["id"]: row for row in repo.list_user_usage()}
    # u1: s-self + s-null(NULL 归 owner)+ s-blank(空串归 owner)= 3;
    # s-shared 归 u2;s-copying、s-memory、s-knowhow 都不计入任何人。
    assert usage["u1"]["sources"] == 3
    assert usage["u2"]["sources"] == 1


def test_last_active_tracks_user_actions_not_conversation_updates(repo):
    _seed(repo)

    def last_active(user_id="u1"):
        return next(
            row["last_active"]
            for row in repo.list_user_usage()
            if row["id"] == user_id
        )

    # 上传可见来源、提交提问、发起深度报告都会立即刷新。
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET created_at=? WHERE id='s1'",
            ("2026-07-08T01:00:00+00:00",),
        )
    assert last_active() == "2026-07-08T01:00:00+00:00"

    with repo._write() as db:
        db.execute(
            "UPDATE ask_jobs SET created_at=? WHERE id='j1'",
            ("2026-07-08T02:00:00+00:00",),
        )
    assert last_active() == "2026-07-08T02:00:00+00:00"

    with repo._write() as db:
        db.execute(
            "UPDATE reports SET created_at=? WHERE id='r1'",
            ("2026-07-08T03:00:00+00:00",),
        )
    assert last_active() == "2026-07-08T03:00:00+00:00"

    # 后台回答落库会推进 conversation.updated_at，但那不是新的用户动作。
    with repo._write() as db:
        db.execute(
            "UPDATE conversations SET updated_at=? WHERE id='c1'",
            ("2026-07-08T04:00:00+00:00",),
        )
        db.execute(
            "UPDATE sources SET source_type='memory',created_at=? WHERE id='s2'",
            ("2026-07-08T05:00:00+00:00",),
        )
    assert last_active() == "2026-07-08T03:00:00+00:00"


def test_last_active_attributes_shared_upload_to_the_actual_uploader(repo):
    _seed(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,created_at,updated_at,uploaded_by) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                "shared-upload", "n1", "Shared", "pdf",
                "2026-07-09T00:00:00+00:00", "2026-07-09T00:00:00+00:00", "u2",
            ),
        )
    rows = {row["id"]: row for row in repo.list_user_usage()}
    assert rows["u2"]["last_active"] == "2026-07-09T00:00:00+00:00"
    assert rows["u1"]["last_active"] == "2026-07-07T00:00:00"


def test_last_active_source_group_uses_absolute_time_not_text_order(repo):
    _seed(repo)
    with repo._write() as db:
        db.execute(
            "UPDATE sources SET created_at=? WHERE id='s1'",
            ("2026-07-08T02:00:00+00:00",),
        )
        # 文本更大但绝对时刻是 01:30Z，不能盖过 s1 的 02:00Z。
        db.execute(
            "UPDATE sources SET created_at=? WHERE id='s2'",
            ("2026-07-08T10:30:00+09:00",),
        )
    usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert usage["last_active"] == "2026-07-08T02:00:00+00:00"


def test_last_active_does_not_expose_unresolved_time_sentinel(repo):
    with repo._write() as db:
        db.execute(
            "INSERT INTO users "
            "(id,email,display_name,role,status,username,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "legacy", "legacy@x", "Legacy", "user", "active",
                "l00000001", "", "",
            ),
        )
        db.execute(
            "INSERT INTO notebooks "
            "(id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("legacy-nb", "Legacy", "legacy", "ready", "", ""),
        )
        db.execute(
            "INSERT INTO ask_jobs "
            "(id,notebook_id,created_by,mode,question,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                "legacy-ask", "legacy-nb", "legacy", "chunk", "q", "done", "", "",
            ),
        )
    usage = next(row for row in repo.list_user_usage() if row["id"] == "legacy")
    assert usage["last_active"] is None


def test_source_store_stamps_visible_upload_actor_but_not_hidden_projection(repo):
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("actor-nb", "Actor", "user-local", "ready", "t0", "t0"),
        )
    store = repo._runtime.source_store
    common = {
        "notebook_id": "actor-nb", "status": "ready", "parse_status": "ready",
        "file_name": "", "file_path": "", "file_size": 0, "file_hash": "",
        "summary": "", "doc_type": "",
    }
    store.insert_source(
        source_id="visible-actor", title="Visible", source_type="pdf", **common
    )
    store.insert_source(
        source_id="hidden-actor", title="Hidden", source_type="memory", **common
    )
    with repo._connect() as db:
        rows = {
            row["id"]: row["uploaded_by"]
            for row in db.execute(
                "SELECT id,uploaded_by FROM sources WHERE notebook_id='actor-nb'"
            )
        }
    assert rows == {"visible-actor": "user-local", "hidden-actor": None}


# ---------------------------------------------------------------------------
# Phase B/C 使用强度信号,含 B1 last_seen(users.last_seen_at 迁移 + 会话
# touch 双写,见 identity 侧测试:backend/tests/test_architecture_hardening.py
# 的 test_last_seen_touch_follows_session_throttle):见规格
# docs/superpowers/specs/2026-09-07-admin-usage-overview-usage-signals-design_zh.md
# §3 Phase B/C。
# ---------------------------------------------------------------------------


def test_list_user_usage_last_seen_reads_users_column(repo):
    """B1:`list_user_usage()["last_seen"]` 直接读 `users.last_seen_at`——
    未上线过为 None,上线过原样返回字符串(与 SQLite 侧其它时间列一致,不做
    额外解析)。写路径(登录/节流 touch)由 identity 测试覆盖。"""
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO users "
            "(id,email,display_name,role,status,username,created_at,updated_at,last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("u1", "u1@x", "U1", "user", "active", "a00000001", now, now, "2026-07-08T00:00:00"),
        )
        db.execute(
            "INSERT INTO users "
            "(id,email,display_name,role,status,username,created_at,updated_at,last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("u2", "u2@x", "U2", "user", "active", "b00000002", now, now, None),
        )
    usage = {row["id"]: row for row in repo.list_user_usage()}
    assert usage["u1"]["last_seen"] == "2026-07-08T00:00:00"
    assert usage["u2"]["last_seen"] is None


def test_list_user_usage_storage_bytes_matches_sources_attribution(repo):
    """B2:与 `sources` 完全同一归因(uploaded_by 优先,NULL 回落 nb.created_by)、
    同一 live+可见过滤,只是把 COUNT(*) 换成 SUM(file_size)。"""
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        for uid, uname in (("u1", "a00000001"), ("u2", "b00000002")):
            db.execute(
                "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@x", uid.upper(), "user", "active", uname, now, now),
            )
        for nid, status in (("n1", "ready"), ("n-copy", "copying")):
            db.execute(
                "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (nid, nid, "u1", status, now, now),
            )
        for sid, notebook_id, source_type, uploaded_by, file_size in (
            ("s-self", "n1", "pdf", "u1", 1000),
            ("s-shared", "n1", "pdf", "u2", 2000),
            ("s-null", "n1", "pdf", None, 300),        # NULL 回落 owner u1
            ("s-memory", "n1", "memory", "u1", 5000),  # 排除:合成来源
            ("s-copying", "n-copy", "pdf", "u1", 9000),  # 排除:copying 库
        ):
            db.execute(
                "INSERT INTO sources "
                "(id,notebook_id,title,source_type,created_at,updated_at,uploaded_by,file_size) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (sid, notebook_id, sid, source_type, now, now, uploaded_by, file_size),
            )
    usage = {row["id"]: row for row in repo.list_user_usage()}
    assert usage["u1"]["storage_bytes"] == 1300  # s-self + s-null
    assert usage["u2"]["storage_bytes"] == 2000


def test_list_user_usage_questions_30d_window(repo):
    """B3:近 30 天提问数——窗口内/窗口外/贴近边界各一条,裸 UTC 与带 offset 的
    ISO 串各覆盖一次,证明 SQLite 侧走 `_absolute_instant`(与 last_active 同一
    判据)而不是裸文本比较。真实墙钟相对时间(而非固定字面量),因为 SQL 侧
    用的是 `julianday('now','-30 days')`,没有可注入的时钟种子——`_absolute_instant`
    的 COALESCE 兜底值是公元 1 年,不会误入窗口。"""
    now = datetime.now(timezone.utc)

    def bare(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S")

    created_now = bare(now)
    in_window_bare = bare(now - timedelta(days=10))
    # 带本机 +08:00 offset 的 ISO 串,与 _absolute_instant 文档里举的例子同一格式。
    in_window_offset = (
        (now - timedelta(days=5)).astimezone(timezone(timedelta(hours=8))).isoformat()
    )
    # 贴近 30 天边界但留出安全余量(测试执行耗时远小于分钟级),验证 `>=` 判据
    # 会把「刚好还在窗口里」的行计入,而不是掐着微秒做不稳定的边界断言。
    near_boundary_included = bare(now - timedelta(days=29, hours=23, minutes=50))
    out_window = bare(now - timedelta(days=40))

    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("u1", "u1@x", "U1", "user", "active", "a00000001", created_now, created_now),
        )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", ("n1", "n1", "u1", "ready", created_now, created_now),
        )
        for jid, created_at in (
            ("j-in-bare", in_window_bare),
            ("j-in-offset", in_window_offset),
            ("j-near-boundary", near_boundary_included),
            ("j-out", out_window),
        ):
            db.execute(
                "INSERT INTO ask_jobs "
                "(id,notebook_id,created_by,mode,question,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (jid, "n1", "u1", "chunk", "q?", "completed", created_at, created_at),
            )
    usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert usage["questions_30d"] == 3


def test_list_user_usage_questions_30d_retained_branch_same_window(repo):
    """B3 retained 分支:笔记本删除后的留存快照仍按同一 30 天窗口计入/排除,
    与 live 分支同一比较语义(created_at 在留存快照里原样保留)。"""
    now = datetime.now(timezone.utc)
    created_now = now.isoformat()
    recent = (now - timedelta(days=3)).isoformat()
    old = (now - timedelta(days=40)).isoformat()
    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("u1", "u1@x", "U1", "user", "active", "a00000001", created_now, created_now),
        )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", ("n1", "n1", "u1", "ready", created_now, created_now),
        )
        db.execute(
            "INSERT INTO ask_jobs "
            "(id,notebook_id,created_by,mode,question,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("j-recent", "n1", "u1", "chunk", "q1", "completed", recent, recent),
        )
        db.execute(
            "INSERT INTO ask_jobs "
            "(id,notebook_id,created_by,mode,question,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("j-old", "n1", "u1", "chunk", "q2", "completed", old, old),
        )
    repo._runtime.notebook_store.delete_row_and_orphan_embeddings("n1")
    usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert usage["questions_30d"] == 1


def test_list_user_usage_questions_failed_and_reports_failed(repo):
    """B5:status='failed' 计入失败数;cancelled 不算失败。"""
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("u1", "u1@x", "U1", "user", "active", "a00000001", now, now),
        )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", ("n1", "n1", "u1", "ready", now, now),
        )
        for jid, status in (
            ("j-failed", "failed"), ("j-cancelled", "cancelled"), ("j-done", "completed"),
        ):
            db.execute(
                "INSERT INTO ask_jobs "
                "(id,notebook_id,created_by,mode,question,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (jid, "n1", "u1", "chunk", "q?", status, now, now),
            )
        for rid, status in (
            ("r-failed", "failed"), ("r-cancelled", "cancelled"), ("r-done", "done"),
        ):
            db.execute(
                "INSERT INTO reports (id,notebook_id,question,status,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (rid, "n1", "q?", status, "u1", now, now),
            )
    usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert usage["questions_failed"] == 1
    assert usage["reports_failed"] == 1


def test_list_user_usage_failed_counts_retained_branch(repo):
    """B5 retained 分支:笔记本删除后,留存快照里 status='failed' 的提问/报告
    仍计入失败数(与 live 分支同一口径)。"""
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("u1", "u1@x", "U1", "user", "active", "a00000001", now, now),
        )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", ("n1", "n1", "u1", "ready", now, now),
        )
        db.execute(
            "INSERT INTO ask_jobs "
            "(id,notebook_id,created_by,mode,question,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("j-failed", "n1", "u1", "chunk", "q?", "failed", now, now),
        )
        db.execute(
            "INSERT INTO reports (id,notebook_id,question,status,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r-failed", "n1", "q?", "failed", "u1", now, now),
        )
    repo._runtime.notebook_store.delete_row_and_orphan_embeddings("n1")
    usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert usage["questions_failed"] == 1
    assert usage["reports_failed"] == 1


def test_list_user_usage_kg_builds_counts_all_statuses_excludes_empty_creator(repo):
    """B4:所有状态都算(与 questions 含失败/取消同口径);created_by 空串
    (早期/异常写入)不算有效用户键。"""
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("u1", "u1@x", "U1", "user", "active", "a00000001", now, now),
        )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", ("n1", "n1", "u1", "ready", now, now),
        )
        for kid, created_by, status in (
            ("kg-done", "u1", "completed"),
            ("kg-failed", "u1", "failed"),
            ("kg-noowner", "", "completed"),
        ):
            db.execute(
                "INSERT INTO kg_build_jobs "
                "(id,notebook_id,created_by,mode,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (kid, "n1", created_by, "full", status, now, now),
            )
    usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert usage["kg_builds"] == 2


def test_list_user_usage_memory_count_excludes_rejected(repo):
    """Phase C:memory_count 排除 status='rejected'——被拒绝的候选不代表用户
    采纳了 Memory 机制。"""
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("u1", "u1@x", "U1", "user", "active", "a00000001", now, now),
        )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", ("n1", "n1", "u1", "ready", now, now),
        )
        for mid, status in (
            ("m-confirmed", "confirmed"), ("m-candidate", "candidate"), ("m-rejected", "rejected"),
        ):
            db.execute(
                "INSERT INTO memory_items "
                "(id,notebook_id,created_by,origin,status,title,content_md,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, "n1", "u1", "ask_answer", status, "T", "C", now, now),
            )
    usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert usage["memory_count"] == 2


def test_list_user_usage_knowhow_tables_excludes_empty_creator(repo):
    """Phase C:knowhow_tables 按 created_by,空串(早期/异常写入)不算有效
    用户键,同 kg_builds 口径。"""
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            ("u1", "u1@x", "U1", "user", "active", "a00000001", now, now),
        )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", ("n1", "n1", "u1", "ready", now, now),
        )
        for kid, created_by in (("k1", "u1"), ("k2", "")):
            db.execute(
                "INSERT INTO knowhow_tables "
                "(id,notebook_id,title,created_by,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (kid, "n1", kid, created_by, now, now),
            )
    usage = next(row for row in repo.list_user_usage() if row["id"] == "u1")
    assert usage["knowhow_tables"] == 1


def test_list_user_usage_joined_notebooks_and_groups(repo):
    """Phase C:joined_notebooks 按 notebook_members.user_id(他人库的成员身份),
    groups 按 group_members.user_id;自有库/群组创建者不经这两张表。"""
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        for uid, uname in (("u1", "a00000001"), ("u2", "b00000002")):
            db.execute(
                "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@x", uid.upper(), "user", "active", uname, now, now),
            )
        db.execute(
            "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", ("n1", "n1", "u1", "ready", now, now),
        )
        db.execute(
            "INSERT INTO notebook_members (notebook_id,user_id,role,added_at) "
            "VALUES (?,?,?,?)", ("n1", "u2", "reader", now),
        )
        db.execute(
            "INSERT INTO groups (id,name,kind,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", ("g1", "G1", "project", "u1", now, now),
        )
        db.execute(
            "INSERT INTO group_members (group_id,user_id,role,added_at) "
            "VALUES (?,?,?,?)", ("g1", "u2", "member", now),
        )
    usage = {row["id"]: row for row in repo.list_user_usage()}
    assert usage["u2"]["joined_notebooks"] == 1
    assert usage["u2"]["groups"] == 1
    assert usage["u1"]["joined_notebooks"] == 0
    assert usage["u1"]["groups"] == 0


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _auth(client, username):
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _auth_admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_admin_users_forbidden_for_regular_user(client):
    b = _auth(client, "z00123456")
    assert client.get("/api/admin/users", headers=b).status_code == 403


def test_admin_users_lists_username_and_counts(client):
    admin = _auth_admin(client)
    a = _auth(client, "z00123456")
    client.post("/api/notebooks", json={"name": "A1"}, headers=a)
    client.post("/api/notebooks", json={"name": "A2"}, headers=a)
    resp = client.get("/api/admin/users", headers=admin)
    assert resp.status_code == 200
    rows = {r["username"]: r for r in resp.json()}
    assert "admin" in rows and "z00123456" in rows
    assert rows["z00123456"]["notebooks"] == 2
    assert rows["z00123456"]["role"] == "user"
    assert rows["z00123456"]["role_mutable"] is True
    assert rows["admin"]["role_mutable"] is False
    # 展示用户名,但内部 id 仍是 user-<hex>(未统一)
    assert rows["z00123456"]["id"].startswith("user-")


def test_admin_users_is_online_reflects_pending_bus(client):
    from app.services.pending_bus import pending_bus
    admin = _auth_admin(client)
    _auth(client, "z00123456")
    rows = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}
    uid = rows["z00123456"]["id"]
    assert rows["z00123456"]["is_online"] is False        # 未连接 → 离线
    q = pending_bus.register(uid)
    try:
        rows2 = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}
        assert rows2["z00123456"]["is_online"] is True     # 有连接 → 在线
    finally:
        pending_bus.unregister(uid, q)
    rows3 = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}
    assert rows3["z00123456"]["is_online"] is False        # 断开 → 离线


def test_admin_online_endpoint_lists_connected(client):
    from app.services.pending_bus import pending_bus
    admin = _auth_admin(client)
    _auth(client, "z00123456")
    uid = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}["z00123456"]["id"]
    q = pending_bus.register(uid)
    try:
        data = client.get("/api/admin/online", headers=admin).json()
        assert uid in data["online_ids"]
        assert data["online_ids"] == sorted(data["online_ids"])  # 端点保证已排序
    finally:
        pending_bus.unregister(uid, q)


def test_admin_online_forbidden_for_regular_user(client):
    b = _auth(client, "z00123456")
    assert client.get("/api/admin/online", headers=b).status_code == 403


def test_admin_can_grant_and_revoke_role_for_existing_session(client):
    admin = _auth_admin(client)
    user_headers = _auth(client, "z00123456")
    user_id = {
        row["username"]: row
        for row in client.get("/api/admin/users", headers=admin).json()
    }["z00123456"]["id"]

    granted = client.patch(
        f"/api/admin/users/{user_id}/role",
        headers=admin,
        json={"role": "admin"},
    )
    assert granted.status_code == 200
    assert granted.json() == {
        "id": user_id,
        "username": "z00123456",
        "role": "admin",
    }
    # resolve_session 每次重新读取 users.role：已有 token 无需重新登录。
    assert client.get("/api/admin/users", headers=user_headers).status_code == 200

    revoked = client.patch(
        f"/api/admin/users/{user_id}/role",
        headers=admin,
        json={"role": "user"},
    )
    assert revoked.status_code == 200
    assert revoked.json()["role"] == "user"
    assert client.get("/api/admin/users", headers=user_headers).status_code == 403


def test_regular_user_cannot_assign_admin_role(client):
    admin = _auth_admin(client)
    user_headers = _auth(client, "z00123456")
    user_id = {
        row["username"]: row
        for row in client.get("/api/admin/users", headers=admin).json()
    }["z00123456"]["id"]
    response = client.patch(
        f"/api/admin/users/{user_id}/role",
        headers=user_headers,
        json={"role": "admin"},
    )
    assert response.status_code == 403
    assert response.headers["X-User-Message"] == "1"


def test_builtin_admin_and_active_admin_cannot_demote_themselves(client):
    admin = _auth_admin(client)
    builtin = client.patch(
        "/api/admin/users/user-local/role",
        headers=admin,
        json={"role": "user"},
    )
    assert builtin.status_code == 409
    assert builtin.json()["detail"] == "内置管理员权限不可撤销"

    promoted_headers = _auth(client, "z00123456")
    user_id = {
        row["username"]: row
        for row in client.get("/api/admin/users", headers=admin).json()
    }["z00123456"]["id"]
    assert client.patch(
        f"/api/admin/users/{user_id}/role",
        headers=admin,
        json={"role": "admin"},
    ).status_code == 200
    self_demote = client.patch(
        f"/api/admin/users/{user_id}/role",
        headers=promoted_headers,
        json={"role": "user"},
    )
    assert self_demote.status_code == 409
    assert self_demote.json()["detail"] == "不能撤销当前账户的管理员权限"


def test_role_update_validates_target_and_role(client):
    admin = _auth_admin(client)
    assert client.patch(
        "/api/admin/users/missing/role",
        headers=admin,
        json={"role": "admin"},
    ).status_code == 404
    assert client.patch(
        "/api/admin/users/user-local/role",
        headers=admin,
        json={"role": "owner"},
    ).status_code == 422
