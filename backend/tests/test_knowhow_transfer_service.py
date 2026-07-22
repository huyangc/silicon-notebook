import time
import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.sqlite_repository import SQLiteRepository
from app.services.knowhow import transfer as kh_transfer
from tests.model_testkit import bind_embedding_client

COLUMNS = [
    {"name": "违例类型", "role": "anchor"},
    {"name": "现象识别", "role": "procedure"},
]

class _FakeEmbedder:
    dim = 3
    def __init__(self):
        self.call_count = 0
    def embed_texts(self, texts):
        self.call_count += len(texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, _FakeEmbedder())
    return r

def _nb(repo, name="KH"):
    return repo.create_notebook(NotebookCreate(name=name, purpose="p", primary_domain="d")).id

def _table_with_row(repo, nb):
    tid = repo.create_knowhow_table(nb, "时序修复", "desc", COLUMNS)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲", cols["现象识别"]: "示波器观察"})
    return tid

def _settle(repo, tid, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        detail = repo.get_knowhow_table(tid)
        rows = detail.get("rows", [])
        if rows and all(r["projection_status"] in ("synced", "failed") for r in rows):
            return detail
        time.sleep(0.05)
    return repo.get_knowhow_table(tid)

def _project(repo, tid):
    # store 层的 add_knowhow_row 不自动调度投影（那是路由/api 层的事）——测试里显式调度。
    from app.services.knowhow.api import get_scheduler
    get_scheduler(repo).schedule(tid)
    return _settle(repo, tid)

def test_copy_creates_independent_table_in_target(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)  # 不投影：业务表拷贝与投影无关

    new_tid = kh_transfer.copy_table(repo, src_tid, dst_nb, actor_id="user-x")

    assert new_tid != src_tid
    dst = repo.get_knowhow_table(new_tid)
    assert dst["notebook_id"] == dst_nb
    assert dst["created_by"] == "user-x"
    assert {c["name"] for c in dst["columns"]} == {"违例类型", "现象识别"}
    assert len(dst["rows"]) == 1
    # 源不受影响
    assert repo.get_knowhow_table(src_tid)["notebook_id"] == src_nb

def test_copy_reprojection_reuses_vectors_zero_reembed(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    _project(repo, src_tid)  # 先把源投影好，产出 chunks + chunk_embeddings
    repo.embedder.call_count = 0  # 归零，之后只观察 copy 引发的 embed

    new_tid = kh_transfer.copy_table(repo, src_tid, dst_nb, actor_id="user-x")
    _settle(repo, new_tid)  # 等重投影落地（copy_table 自己已调度）

    # K-1：chunk_embeddings 已随拷贝以稳定 id 落库 → 重投影零重嵌入
    assert repo.embedder.call_count == 0


def test_copy_is_retrievable_in_target_via_lexical_and_vector(repo):
    """拷贝出来的表必须在目标 notebook 里「搜得到」——两条检索通道都要有。

    词法通道尤其脆弱：chunks_fts 是无触发器的手工维护 FTS5 虚表，只有
    ChunkStore.insert_rows/delete_by_ids 会写它；而对拷贝出来的 chunk，
    重投影必然走 `old_specs == new_specs -> continue`（projection.py），
    两个写 FTS 的路径一个都不会碰 → 事务里不显式补 chunks_fts 的话，副本
    永久只能被向量召回、词法搜索彻底搜不到，且没有任何自愈路径（不像向量
    有 self-heal probe）。这条断言就是那个缺口的回归闸。"""
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    _project(repo, src_tid)

    new_tid = kh_transfer.copy_table(repo, src_tid, dst_nb, actor_id="user-x")
    _settle(repo, new_tid)

    with repo._connect() as db:
        # 词法/FTS 通道（production 检索用的同一个原语）
        fts_hits = repo._runtime.knowledge.chunk_fts_search(db, dst_nb, "示波器观察", k=10)
        # 向量通道：拷贝进来的 chunk 必须都带着自己的向量
        vec_rows = db.execute(
            "SELECT COUNT(*) FROM chunk_embeddings WHERE notebook_id=?", (dst_nb,)
        ).fetchone()[0]
        chunk_rows = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE notebook_id=?", (dst_nb,)
        ).fetchone()[0]

    assert fts_hits, "副本的 chunk 未进 chunks_fts —— 目标库词法检索永久搜不到"
    assert chunk_rows > 0
    assert vec_rows == chunk_rows, "副本的 chunk 与向量数量不匹配"


def test_move_deletes_source_table_and_projection(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    # 源表先真投影（_project 会先 schedule() 再等 settle），否则
    # hidden_source_id 全程是 None——下面的 objects 清空断言会因 SQL
    # `source_id=NULL` 永不匹配而空转通过，测不出 delete_table_projection
    # 有没有真的被调用。
    src_hidden = _project(repo, src_tid)["hidden_source_id"]
    assert src_hidden, "源表未真正投影，测试前置条件不成立"
    # 第二个空转口子：_settle 也接受 "failed"，而 ensure_hidden_source 跑在
    # 行处理之前、KO 重建跑在之后——中途结构性失败会给出「truthy hidden +
    # 零 objects」，让末尾 remaining == 0 换个路子重新空转。先钉住前置有料。
    with repo._connect() as db:
        before = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (src_hidden,)
        ).fetchone()[0]
    assert before > 0, "源表投影未产出 objects，删投影断言将空转"

    new_tid = kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")

    # 目标有，新表可读
    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst_nb
    # 源表已删
    import pytest as _pytest
    with _pytest.raises(KeyError):
        repo.get_knowhow_table(src_tid)
    # 源隐藏源的投影已拆（objects 清空）
    with repo._connect() as db:
        remaining = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (src_hidden,)
        ).fetchone()[0]
    assert remaining == 0

def test_move_keeps_source_intact_when_projection_teardown_fails(repo, monkeypatch):
    """拆投影失败必须留下「可恢复的重复」，而不是不可回收的幽灵。

    这条钉的是 move_table 里三步的**顺序**：copy → 拆源投影 → 删源表。
    反过来（先删表再拆投影）时，拆投影一旦失败，源表行没了、
    hidden_source_id 只存在于那条被删掉的行里，源库的 chunks/chunks_fts/
    chunk_embeddings/KO 就永久且不可回收地活着——源 notebook 会继续检索到
    已经搬走的内容，而 delete_table_projection 的生产调用方需要一条不复存在
    的 knowhow_tables 行才能重试；copy_table→snapshot_table 也会因表已删而
    抛 KeyError，重试同样无门。这不是纸面风险：copy_table 紧邻着调度了后台
    重投影，此刻必然存在并发 SQLite 写者，delete_table_projection 完全可能
    在 busy timeout 上吃到 OperationalError。

    A4（REST 端点）评审附加需求：move_table 现在把这一段(读 hidden_source_id
    + 拆投影 + 删源表)包进 SourceCleanupFailed——裸 RuntimeError 不再直接
    冒泡，而是被收进一个携带 new_table_id 的类型化错误，好让路由层能返回
    结构化 409 而不是通用 500。故这里断言的异常类型也从 RuntimeError 改为
    SourceCleanupFailed，并额外钉住 new_table_id/cause 两个字段。"""
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    src_hidden = _project(repo, src_tid)["hidden_source_id"]
    assert src_hidden, "源表未真正投影，测试前置条件不成立"

    class _BoomProjector:
        def delete_table_projection(self, hidden_source_id):
            raise RuntimeError("simulated delete_table_projection failure")

    monkeypatch.setattr(kh_transfer, "build_projector", lambda _repo: _BoomProjector())

    import pytest as _pytest
    with _pytest.raises(kh_transfer.SourceCleanupFailed) as exc_info:
        kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")
    assert isinstance(exc_info.value.cause, RuntimeError)
    assert exc_info.value.new_table_id
    # round 10 P1-A：teardown 本身抛出异常——源原封未动，reason 必须是
    # "cleanup_error"（手动删除源表安全），不能被误判成 "source_changed"。
    assert exc_info.value.reason == "cleanup_error"

    # 源表必须原封不动地还在：可以重试搬迁，也可以照常删除——两边都留，不丢。
    detail = repo.get_knowhow_table(src_tid)
    assert detail["notebook_id"] == src_nb
    assert len(detail["rows"]) == 1
    assert detail["hidden_source_id"] == src_hidden
    # 「两边都留」的另一半：副本已提交在目标侧。move_table 是在拆投影处抛的，
    # 此时 copy_table 早已返回，所以目标必须真有这张表（拿不到返回值，从目标
    # 侧查）。只钉源表还在的话，「复制根本没发生」也会让这条用例通过。
    dst_tables = repo.list_knowhow_tables(dst_nb)
    assert len(dst_tables) == 1
    # new_table_id 就是目标侧那张副本表的 id——路由层拿它回填给调用方靠的
    # 正是这条勾连，光钉「有一张表」测不出 id 对不对。
    assert exc_info.value.new_table_id == dst_tables[0]["id"]
    assert dst_tables[0]["title"] == detail["title"]

def test_transfer_table_dispatches_copy_keeping_source(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)

    new_tid = kh_transfer.transfer_table(repo, src_tid, dst_nb, "user-x", mode="copy")

    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst_nb
    assert repo.get_knowhow_table(src_tid)["notebook_id"] == src_nb  # copy 不删源

def test_transfer_table_dispatches_move_removing_source(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)

    new_tid = kh_transfer.transfer_table(repo, src_tid, dst_nb, "user-x", mode="move")

    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst_nb
    import pytest as _pytest
    with _pytest.raises(KeyError):  # move 必须删源
        repo.get_knowhow_table(src_tid)

def test_transfer_table_rejects_bad_mode(repo):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    import pytest as _pytest
    with _pytest.raises(ValueError):
        kh_transfer.transfer_table(repo, src_tid, dst_nb, "user-x", mode="teleport")


# --- Final-fix-wave Important 1: copy_table's post-commit schedule() must not
# let an exception escape. DB insert_transfer already COMMITted by this point
# (assert repo.get_knowhow_table(new_tid) below proves it); ProjectionScheduler
# .schedule -> threading.Timer.start() can raise RuntimeError under thread
# exhaustion. Before the fix this propagated straight out of copy_table as a
# bare exception — the caller (a route with no handler for it) would see a
# 500 despite the copy having already landed, inviting a retry that piles up
# duplicate tables. Appended at EOF (see file header pin note in
# test_repository_callers_static.py / test_repository_surface_manifest.py —
# transfer.py:29/89/185/204 are line-pinned; this test only calls existing
# public functions, it doesn't touch those lines).
def test_copy_survives_scheduler_failure_after_commit(repo, monkeypatch):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)

    class _BoomScheduler:
        def schedule(self, table_id):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(kh_transfer, "get_scheduler", lambda _repo: _BoomScheduler())

    # Must not raise: the DB side already committed, so a scheduling failure
    # is best-effort-lost KG rebuild, not a failed copy.
    new_tid = kh_transfer.copy_table(repo, src_tid, dst_nb, actor_id="user-x")

    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst_nb


def test_move_survives_scheduler_failure_and_still_cleans_source(repo, monkeypatch):
    """The same fault, exercised through move_table: before the fix, copy_table's
    unguarded schedule() would raise and propagate straight out of move_table
    WITHOUT ever entering its own try/except — bypassing the
    SourceCleanupFailed -> 409 path entirely and leaving the source table
    completely untouched while a copy silently exists in the target. After the
    fix, the scheduler failure is swallowed inside copy_table and move_table
    proceeds normally: copy lands, source gets cleaned up, the call returns
    the new table id like any other successful move."""
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)

    class _BoomScheduler:
        def schedule(self, table_id):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(kh_transfer, "get_scheduler", lambda _repo: _BoomScheduler())

    new_tid = kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")

    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst_nb
    import pytest as _pytest
    with _pytest.raises(KeyError):  # move 必须删源 —— schedule() 失败不能拦下这一步
        repo.get_knowhow_table(src_tid)


# --- Final-fix-wave Minor: copied asset rows must not keep a foreign source_id.
# notebook_assets.source_id points at a `sources` row (non-NULL only for
# MinerU-derived assets; paste-uploads are NULL, which is why this needs an
# explicit non-NULL fixture to actually exercise the bug — a NULL source_id
# would make the assertion vacuous). knowhow_store.delete_source_asset_rows
# does `DELETE FROM notebook_assets WHERE source_id=?` with no notebook_id
# scoping, so leaving the copy's row pointed at the SOURCE notebook's source
# means deleting/reparsing that source in the source notebook silently deletes
# the copy's asset row in the target notebook too — violating spec §4.3's
# "独立副本" (independent copy) promise. Local import (not module-level) to
# avoid shifting the line-pinned `from app.services.sqlite_repository import
# SQLiteRepository` at line 5 (KNOWHOW_TRANSFER_SERVICE_ALLOWED_IMPORTS).
def test_copy_gives_asset_row_independent_source_id(repo):
    from app.services.knowhow.assets import AssetService

    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    asset = AssetService(repo).save(
        src_nb, "a.png", "image/png", b"\x89PNG" + b"x" * 40, "user-x"
    )
    # 伪造一个非空 source_id，模拟 MinerU 派生资产（粘贴上传恒为 NULL，测不出
    # 这条回归——必须显式构造非空值才能钉住这个坑）。
    with repo._connect() as db:
        db.execute(
            "UPDATE notebook_assets SET source_id=? WHERE id=?",
            ("src-fake-source", asset["id"]),
        )
        db.commit()

    tid = repo.create_knowhow_table(src_nb, "T", "", [{"name": "备注", "role": "attribute"}])
    column_id = repo.get_knowhow_table(tid)["columns"][0]["id"]
    repo.add_knowhow_row(tid, {column_id: f"![img](asset://{asset['id']})"})

    new_tid = kh_transfer.copy_table(repo, tid, dst_nb, actor_id="user-x")

    with repo._connect() as db:
        new_assets = [
            dict(r)
            for r in db.execute(
                "SELECT id, source_id FROM notebook_assets WHERE notebook_id=?", (dst_nb,)
            ).fetchall()
        ]
    assert len(new_assets) == 1
    assert new_assets[0]["id"] != asset["id"]
    assert new_assets[0]["source_id"] is None, (
        "副本资产行的 source_id 不该继续指向源 notebook 的 source —— "
        "否则源那边删/重解析该 source 会连带删掉这条已经在别的 notebook 的副本行"
    )


# --- PR review round 2 P1-2 (data loss): copy_table snapshots the source: if
# a cell/row/column edit commits between that snapshot and move_table's
# delete of the source, the target keeps the stale snapshot and the newly-
# edited source is destroyed forever — the concurrent edit is gone for good.
# The interleaving here injects the edit via copy_table itself (monkeypatched
# to add a row to the source right after the real copy_table returns, i.e.
# right after the copy has committed and BEFORE move_table reaches its
# cleanup section) — this is the earliest a concurrent writer could
# realistically land relative to move_table's own call sequence, and it
# deliberately uses add_knowhow_row (not a cell edit): add/delete-row is
# exactly the case knowhow_tables.mutation_seq does NOT bump (only
# update_knowhow_cell/update_knowhow_cells do — see KnowhowStore's own
# docstring), so a fingerprint that compared mutation_seq alone would sail
# straight past this race and still lose the row. Appended at EOF — this
# file only calls existing public functions, it doesn't touch the line-
# pinned `store = repo._runtime.knowhow_transfer_store` (transfer.py:193) or
# any other pinned site (see test_repository_callers_static.py /
# test_repository_surface_manifest.py).
def test_move_does_not_delete_source_row_added_after_copy_snapshot(repo, monkeypatch):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(src_tid)["columns"]}

    real_copy_table = kh_transfer.copy_table

    def _copy_then_concurrent_row_add(repo_, source_table_id, target_notebook_id, actor_id):
        new_id = real_copy_table(repo_, source_table_id, target_notebook_id, actor_id)
        # 模拟并发写者：就在 copy_table 的事务提交之后、move_table 走到自己的
        # 清理段之前，另一个请求往源表加了一行。
        repo_.add_knowhow_row(source_table_id, {cols["违例类型"]: "并发新增的行"})
        return new_id

    monkeypatch.setattr(kh_transfer, "copy_table", _copy_then_concurrent_row_add)

    with pytest.raises(kh_transfer.SourceCleanupFailed) as exc_info:
        kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")
    assert exc_info.value.new_table_id
    # round 10 P1-A：早退的指纹预检未命中——源是被有意保留以保护这份并发
    # 编辑的，reason 必须是 "source_changed"（绝不能引导用户删除源）。
    assert exc_info.value.reason == "source_changed"

    # 源没被删，且带着并发新增的那一行——不是复制那一刻的旧快照
    detail = repo.get_knowhow_table(src_tid)
    assert detail["notebook_id"] == src_nb
    assert len(detail["rows"]) == 2, "并发新增的行必须还在源表里，不能被误删的源表带走"

    # 目标侧副本仍然存在（拷贝已提交，不因收尾中止而消失），但停在拷贝那一刻
    # 的旧快照（只有 1 行）——fingerprint 只挡「删」，不让复制本身变成实时
    # 同步，这是已知、可接受的局限（见报告）。
    dst_tables = repo.list_knowhow_tables(dst_nb)
    assert len(dst_tables) == 1
    assert dst_tables[0]["id"] == exc_info.value.new_table_id
    assert repo.get_knowhow_table(exc_info.value.new_table_id)["rows"].__len__() == 1


def test_move_deletes_source_when_untouched_after_copy_snapshot(repo):
    """Companion happy-path guard: the new fingerprint re-check must not turn
    every ordinary, uncontended move into a false-positive SourceCleanupFailed
    — ``test_move_deletes_source_table_and_projection`` already covers the
    full projection-teardown shape; this one pins the narrower fingerprint
    match itself with a minimal table."""
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)

    new_tid = kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")

    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst_nb
    with pytest.raises(KeyError):
        repo.get_knowhow_table(src_tid)


# --- PR review round 3 P1-1 (still a race, even with round 2's re-check):
# round 2 added a fingerprint recheck right before the cleanup section, but
# the recheck and the eventual `repo.delete_knowhow_table` were two
# INDEPENDENT statements with the entire projection-teardown call sitting in
# between them — check-then-act, not atomic. An edit landing in THAT specific
# gap (after the recheck reads "unchanged", before the row is actually
# deleted) sails straight past round 2's guard: the recheck already passed
# using the pre-edit fingerprint, and the unconditional delete then removes
# the row (and the concurrent edit riding along with it) with no further
# check. This is a DIFFERENT bug from round 2's P1-2 (which was about the gap
# between copy_table's snapshot and the FIRST fingerprint read) — this one is
# specifically about the SECOND check not being atomic with the delete that
# follows it, and round 2's own fingerprint content (mutation_seq + 4 counts)
# is already enough to detect an added row, so this test deliberately reuses
# a plain add_knowhow_row (not a cell-code edit — that gap is P1-3's, tested
# separately) to isolate the atomicity bug from the fingerprint-content bug.
# The interleaving is injected via a stub `build_projector` whose
# `delete_table_projection` — called between the recheck and the delete —
# performs the concurrent write before returning normally (as if teardown
# succeeded): the earliest realistic point in move_table's own call sequence
# after the recheck has already passed. Appended at EOF, same zero-line-shift
# reason as every other block in this file (see the file-header pin note).
def test_move_does_not_delete_source_row_added_during_projection_teardown(
    repo, monkeypatch
):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(src_tid)["columns"]}
    # 表必须真投影过，hidden_source_id 才非空——move_table 只在 hidden 为真
    # 时才调用 delete_table_projection，这正是本用例注入并发编辑的钩子。
    src_hidden = _project(repo, src_tid)["hidden_source_id"]
    assert src_hidden, "源表未真正投影，测试前置条件不成立"

    class _ConcurrentEditDuringTeardown:
        def delete_table_projection(self, hidden_source_id):
            # 模拟并发写者恰好卡在「指纹复核通过」和「原子删除」之间的窗口
            # ——拆投影本身允许失败/耗时，是这条竞态在生产中最现实的落点。
            # 故意不调用真实投影拆卸（不是这条用例要测的东西，_BoomProjector
            # 同款先例见 test_move_keeps_source_intact_when_projection_
            # teardown_fails）：只做并发编辑这一件事，正常返回（模拟"拆投影
            # 本身成功"，把变量收窄到"拆投影和删表行之间有编辑插入"这一件
            # 事本身）。
            repo.add_knowhow_row(src_tid, {cols["违例类型"]: "并发新增的行"})

    monkeypatch.setattr(
        kh_transfer, "build_projector", lambda _repo: _ConcurrentEditDuringTeardown()
    )

    with pytest.raises(kh_transfer.SourceCleanupFailed) as exc_info:
        kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")
    assert exc_info.value.new_table_id
    # round 10 P1-A：原子条件删除返回 False（指纹复核未命中）——源是被有意
    # 保留以保护这份并发编辑的，reason 必须是 "source_changed"。
    assert exc_info.value.reason == "source_changed"

    # 源没被删，且带着并发新增的那一行——不是复制/复核那一刻的旧快照
    detail = repo.get_knowhow_table(src_tid)
    assert detail["notebook_id"] == src_nb
    assert len(detail["rows"]) == 2, "并发新增的行必须还在源表里，不能被误删的源表带走"

    # 目标侧副本仍然存在（拷贝已提交，不因收尾中止而消失）
    dst_tables = repo.list_knowhow_tables(dst_nb)
    assert len(dst_tables) == 1
    assert dst_tables[0]["id"] == exc_info.value.new_table_id


# --- PR review round 4 P2-2: teardown-before-atomic-delete leaves the
# RETAINED table's projection torn down with nothing queued to rebuild it.
#
# The scenario round 3's own test above (test_move_does_not_delete_source_
# row_added_during_projection_teardown) already proves the atomic delete
# correctly RETAINS the source when a concurrent edit lands during teardown
# — this test reuses the exact same injection point (a stub build_projector
# whose delete_table_projection performs the concurrent edit instead of
# tearing anything down for real) and asks the next question: teardown
# already ran (in production, delete_table_projection really does tear the
# projection down before the atomic delete's re-check even runs) — if the
# concurrent edit's OWN reprojection had ALSO already completed by that
# point (rows marked synced, no job left pending), NOTHING is left to
# rebuild the now-torn-down projection once the table is retained. The
# concurrently-added row here starts life "pending" (KnowhowStore.
# add_knowhow_row's own contract) and nothing in this test's injection path
# schedules it (the stub replaces build_projector wholesale — no route-level
# schedule() call fires either) — so absent a fix, it stays "pending"
# forever: exactly the "no chunks/KG until a manual reproject" defect the
# fix must close. Appended at EOF, same zero-line-shift reason as every
# other block in this file (see the file-header pin note).
def test_move_reschedules_projection_for_retained_source_when_edited_during_teardown(
    repo, monkeypatch
):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(src_tid)["columns"]}
    src_hidden = _project(repo, src_tid)["hidden_source_id"]
    assert src_hidden, "源表未真正投影，测试前置条件不成立"

    class _ConcurrentEditDuringTeardown:
        def delete_table_projection(self, hidden_source_id):
            # 同 round 3 P1-1 的注入点：正常返回（模拟"拆投影本身成功"），
            # 只做并发编辑这一件事。新行落库即 projection_status='pending'，
            # 且这条注入路径不经过路由层，没有任何人会替它调度重投影。
            repo.add_knowhow_row(src_tid, {cols["违例类型"]: "并发新增的行"})

    monkeypatch.setattr(
        kh_transfer, "build_projector", lambda _repo: _ConcurrentEditDuringTeardown()
    )

    with pytest.raises(kh_transfer.SourceCleanupFailed) as exc_info:
        kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")
    # round 10 P1-A：同上一条用例，原子条件删除返回 False——reason 必须是
    # "source_changed"。
    assert exc_info.value.reason == "source_changed"

    # 保留分支必须自己把重投影重新排上队——不能指望并发编辑那次“顺便”
    # 排过了（它没有：注入路径根本没走路由层的 schedule() 调用）。
    detail = _settle(repo, src_tid)
    assert len(detail["rows"]) == 2, "并发新增的行必须还在源表里"
    assert all(r["projection_status"] == "synced" for r in detail["rows"]), (
        "保留分支必须重新排队投影——否则并发新增的那一行永远停在 pending，"
        "没有 chunk/KG，直到有人手工点「重建投影」: "
        f"{[(r['id'], r['projection_status']) for r in detail['rows']]}"
    )
    with repo._connect() as db:
        objects = db.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE source_id=?", (src_hidden,)
        ).fetchone()[0]
    assert objects > 0, "重投影必须真的重建出 KG objects，不只是把 projection_status 标记成 synced"


# --- PR review round 5 P1-1 (ROOT FIX, fingerprint gap #3, data loss): the
# four tests below are the move_table-level companions to the direct
# table_fingerprint unit tests in test_knowhow_transfer_store.py (test_
# fingerprint_changes_when_table_title_is_edited and its four siblings) —
# same interleaving SHAPE as test_move_does_not_delete_source_row_added_
# after_copy_snapshot above (round 2 P1-2): monkeypatch kh_transfer.
# copy_table to call the real implementation and then perform a concurrent
# edit on the SOURCE table right after the copy commits (the earliest a
# concurrent writer could realistically land relative to move_table's own
# call sequence), before move_table reaches its cleanup section. What
# differs here is WHICH edit is injected: none of these four touch row/
# cell/code cardinality or content, or bump mutation_seq — exactly the class
# of edit the pre-round-5 fingerprint (mutation_seq + four counts + a
# cell_code content signal) structurally could not see, per its own
# docstring's "Known, accepted scope boundary" paragraph. Before the fix,
# each of these four would have sailed straight past the recheck inside
# move_table and the source table — now carrying newer metadata than the
# target's frozen copy — would have been silently deleted. Appended at EOF,
# same zero-line-shift reason as every other block in this file (see the
# file-header pin note).
def test_move_does_not_delete_source_table_title_edited_after_copy_snapshot(
    repo, monkeypatch
):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)

    real_copy_table = kh_transfer.copy_table

    def _copy_then_concurrent_title_edit(repo_, source_table_id, target_notebook_id, actor_id):
        new_id = real_copy_table(repo_, source_table_id, target_notebook_id, actor_id)
        # 模拟并发写者：就在 copy_table 的事务提交之后、move_table 走到自己的
        # 清理段之前，另一个请求改了源表的标题。
        repo_.update_knowhow_table_meta(source_table_id, title="并发改名后的标题")
        return new_id

    monkeypatch.setattr(kh_transfer, "copy_table", _copy_then_concurrent_title_edit)

    with pytest.raises(kh_transfer.SourceCleanupFailed) as exc_info:
        kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")
    assert exc_info.value.new_table_id
    # round 10 P1-A：早退的指纹预检未命中——reason 必须是 "source_changed"。
    assert exc_info.value.reason == "source_changed"

    # 源没被删，且带着并发改名后的新标题——不是复制那一刻的旧快照
    detail = repo.get_knowhow_table(src_tid)
    assert detail["notebook_id"] == src_nb
    assert detail["title"] == "并发改名后的标题"

    # 目标侧副本仍然存在（拷贝已提交，不因收尾中止而消失），但停在拷贝那一刻
    # 的旧标题——这个 guard 只挡「删」，不让复制本身变成实时同步
    dst_tables = repo.list_knowhow_tables(dst_nb)
    assert len(dst_tables) == 1
    assert dst_tables[0]["id"] == exc_info.value.new_table_id
    assert repo.get_knowhow_table(exc_info.value.new_table_id)["title"] == "时序修复"


def test_move_does_not_delete_source_column_renamed_after_copy_snapshot(
    repo, monkeypatch
):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    column_id = repo.get_knowhow_table(src_tid)["columns"][0]["id"]

    real_copy_table = kh_transfer.copy_table

    def _copy_then_concurrent_rename(repo_, source_table_id, target_notebook_id, actor_id):
        new_id = real_copy_table(repo_, source_table_id, target_notebook_id, actor_id)
        repo_.rename_knowhow_column(column_id, "并发改名后的列")
        return new_id

    monkeypatch.setattr(kh_transfer, "copy_table", _copy_then_concurrent_rename)

    with pytest.raises(kh_transfer.SourceCleanupFailed) as exc_info:
        kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")
    assert exc_info.value.new_table_id
    assert exc_info.value.reason == "source_changed"  # round 10 P1-A

    detail = repo.get_knowhow_table(src_tid)
    assert detail["notebook_id"] == src_nb
    assert {c["name"] for c in detail["columns"]} == {"并发改名后的列", "现象识别"}

    dst_tables = repo.list_knowhow_tables(dst_nb)
    assert len(dst_tables) == 1
    assert dst_tables[0]["id"] == exc_info.value.new_table_id


def test_move_does_not_delete_source_anchor_moved_after_copy_snapshot(
    repo, monkeypatch
):
    """role 编码的另一半语义（anchor 指定，非纯内容 kind）：并发把 anchor 从
    「违例类型」移到「现象识别」——这一变化直接改变「这张表的 KG 主题列是
    谁」，round 5 之前的指纹（计数 + cell_code 内容信号）对它完全失明。"""
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    cols = repo.get_knowhow_table(src_tid)["columns"]
    new_anchor_id = next(c["id"] for c in cols if c["name"] == "现象识别")

    real_copy_table = kh_transfer.copy_table

    def _copy_then_concurrent_anchor_move(repo_, source_table_id, target_notebook_id, actor_id):
        new_id = real_copy_table(repo_, source_table_id, target_notebook_id, actor_id)
        repo_.set_knowhow_anchor_column(source_table_id, new_anchor_id)
        return new_id

    monkeypatch.setattr(kh_transfer, "copy_table", _copy_then_concurrent_anchor_move)

    with pytest.raises(kh_transfer.SourceCleanupFailed) as exc_info:
        kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")
    assert exc_info.value.new_table_id
    assert exc_info.value.reason == "source_changed"  # round 10 P1-A

    detail = repo.get_knowhow_table(src_tid)
    assert detail["notebook_id"] == src_nb
    roles = {c["name"]: c["role"] for c in detail["columns"]}
    assert roles["现象识别"] == "anchor"
    assert roles["违例类型"] == "attribute"

    dst_tables = repo.list_knowhow_tables(dst_nb)
    assert len(dst_tables) == 1
    assert dst_tables[0]["id"] == exc_info.value.new_table_id


def test_move_does_not_delete_source_column_swapped_after_copy_snapshot(
    repo, monkeypatch
):
    """净列数不变的换列（删一列、加一列不同的）：旧指纹的 col_count 在这种
    编辑下不动（3 列换成 3 列），是四个计数字段类设计从根本上抓不住的一类
    编辑，只有把列身份（id）本身哈希进去才行——见 test_knowhow_transfer_
    store.py 里同名单元测试的详细论证。"""
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    extra_col_id = repo.add_knowhow_column(src_tid, "占位列", "attribute")

    real_copy_table = kh_transfer.copy_table

    def _copy_then_concurrent_swap(repo_, source_table_id, target_notebook_id, actor_id):
        new_id = real_copy_table(repo_, source_table_id, target_notebook_id, actor_id)
        repo_.add_knowhow_column(source_table_id, "替换列", "entity")
        repo_.delete_knowhow_column(extra_col_id)
        return new_id

    monkeypatch.setattr(kh_transfer, "copy_table", _copy_then_concurrent_swap)

    with pytest.raises(kh_transfer.SourceCleanupFailed) as exc_info:
        kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")
    assert exc_info.value.new_table_id
    assert exc_info.value.reason == "source_changed"  # round 10 P1-A

    detail = repo.get_knowhow_table(src_tid)
    assert detail["notebook_id"] == src_nb
    names = {c["name"] for c in detail["columns"]}
    assert "替换列" in names and "占位列" not in names, "并发换列后的列集合必须还在源表里"

    dst_tables = repo.list_knowhow_tables(dst_nb)
    assert len(dst_tables) == 1
    assert dst_tables[0]["id"] == exc_info.value.new_table_id


# --- PR review round 6 P1-A (MOST IMPORTANT — plain sequential bug, not a
# concurrency race): project a table, then delete a business ROW. project_
# table's per-row loop (_write_elements/_write_chunks in projection.py) only
# ever rewrites elements/chunks for rows still present in table["rows"] —
# _write_elements's own delete-then-reinsert (`delete_elements_by_knowhow_
# row(db, source_id, row_id)`) only fires for a row that gets REVISITED by a
# later projection pass. A deleted row is never revisited by anything (it is
# simply absent from the next pass's row loop), so its old source_elements
# (and the chunks/chunk_embeddings hanging off them) stay attached to the
# table's hidden source FOREVER — nothing in this codebase ever sweeps them.
# snapshot_table selects every element/chunk by source_id (no row/column
# filter), so the snapshot silently includes these stale rows. _remap then
# did `khrow_map[kh_meta["row_id"]]` unconditionally — khrow_map only holds
# LIVE row ids (built from snapshot["rows"], which IS filtered by the live
# `knowhow_rows` table) — so the stale element's row_id raises KeyError. The
# route's `except KeyError: raise HTTPException(404, "Table not found")`
# turns this into a permanent, ordinary-usage brick: the table can never be
# copied or moved again by anyone, no concurrency required to trigger it.
# Local `import json` (not module-level) to avoid shifting this file's own
# line-pinned `from app.services.sqlite_repository import SQLiteRepository`
# at line 5 (KNOWHOW_TRANSFER_SERVICE_ALLOWED_IMPORTS in
# test_repository_surface_manifest.py) — same zero-line-shift convention
# every other block in this file already follows.
def test_copy_skips_stale_elements_from_a_deleted_row(repo):
    import json

    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    tid = repo.create_knowhow_table(src_nb, "时序修复", "desc", COLUMNS)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    row1 = repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲", cols["现象识别"]: "示波器观察"})
    repo.add_knowhow_row(tid, {cols["违例类型"]: "欠冲", cols["现象识别"]: "眼图分析"})
    _project(repo, tid)  # 两行都投影，各自在隐藏源下产出 element/chunk

    repo.delete_knowhow_row(row1)  # 只删业务行本身——不触发任何重投影

    # 修复前：这里直接 KeyError（khrow_map 缺 row1），路由层会转成 404。
    new_tid = kh_transfer.copy_table(repo, tid, dst_nb, actor_id="user-x")

    dst = repo.get_knowhow_table(new_tid)
    assert len(dst["rows"]) == 1, "只有存活的第 2 行应该被复制（业务行快照本就只含活行）"
    surviving_row_ids = {r["id"] for r in dst["rows"]}
    with repo._connect() as db:
        elements = [
            dict(r) for r in db.execute(
                "SELECT se.metadata FROM source_elements se "
                "JOIN sources s ON s.id = se.source_id WHERE s.notebook_id=?",
                (dst_nb,),
            ).fetchall()
        ]
    assert elements, "目标表投影后应至少产出一个 element（不能因为过度跳过而变成零产出）"
    for el in elements:
        meta = json.loads(el["metadata"])["knowhow"]
        assert meta["row_id"] in surviving_row_ids, (
            f"目标侧 element 引用了非存活业务行 {meta['row_id']}——陈旧派生物"
            "不该跟着复制搬过去"
        )


def test_copy_skips_stale_elements_from_a_deleted_column(repo):
    """镜像用例（同一崩溃机制，触发源是删列而非删行）：_write_elements 按行
    整体删后重插，只有该行被重投影时才会清理已经不在当前列集合里的陈旧
    element；列被删除但没有任何后续重投影发生时，旧列的 element 原样留在
    隐藏源下，_remap 必须同样跳过、不崩溃。"""
    import json

    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    tid = repo.create_knowhow_table(src_nb, "时序修复", "desc", COLUMNS)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲", cols["现象识别"]: "示波器观察"})
    _project(repo, tid)

    repo.delete_knowhow_column(cols["现象识别"])  # 只删列本身——不触发任何重投影

    new_tid = kh_transfer.copy_table(repo, tid, dst_nb, actor_id="user-x")

    dst = repo.get_knowhow_table(new_tid)
    assert {c["name"] for c in dst["columns"]} == {"违例类型"}
    surviving_col_ids = {c["id"] for c in dst["columns"]}
    with repo._connect() as db:
        elements = [
            dict(r) for r in db.execute(
                "SELECT se.metadata FROM source_elements se "
                "JOIN sources s ON s.id = se.source_id WHERE s.notebook_id=?",
                (dst_nb,),
            ).fetchall()
        ]
    assert elements, "目标表投影后应至少产出一个 element"
    for el in elements:
        meta = json.loads(el["metadata"])["knowhow"]
        assert meta["column_id"] in surviving_col_ids, (
            f"目标侧 element 引用了非存活列 {meta['column_id']}——陈旧派生物"
            "不该跟着复制搬过去"
        )


# --- PR review round 6 P1-B: a hidden source recreated mid-move (retain-path/
# stale-debounce reprojection racing move_table's own teardown) must be swept
# after a successful delete. Window: delete_table_projection(hidden) deletes
# the SOURCE row but does NOT clear knowhow_tables.hidden_source_id on the
# table row (that column is only ever touched by set_knowhow_hidden_source,
# never by delete_table_projection) — so between that call returning and
# delete_table_if_unchanged's atomic delete, the table row still exists with
# hidden_source_id pointing at the now-gone source. If a concurrent
# ensure_hidden_source (KnowhowProjector's own idempotent "recreate if the
# recorded id no longer resolves" branch — projection.py) runs for this exact
# table_id in that window, it reads the stale hidden_source_id, gets KeyError
# resolving it, and mints a REPLACEMENT source — updating the table row's
# hidden_source_id to the new id. table_fingerprint (structural business data
# only, correctly — hidden_source_id is derived, not business content) never
# sees this, so the atomic delete still succeeds normally; the table row
# (with the replacement id) is gone, and the replacement source is now
# unreferenced by ANY knowhow_tables row — a permanent orphan, still
# searchable, sitting in the SOURCE notebook.
#
# Real concurrency is unreliable to trigger deterministically in a test, so
# this injects the race directly: monkeypatch KnowhowTransferStore.
# delete_table_if_unchanged (the method called immediately after
# delete_table_projection returns, mirroring this file's own established
# teardown-injection idiom, e.g. test_move_does_not_delete_source_row_added_
# during_projection_teardown above) to call the REAL production
# KnowhowProjector.ensure_hidden_source for src_tid — the exact method a
# concurrent reprojection job would call — before delegating to the real
# delete. This reproduces production's actual code path, not a hand-rolled
# approximation of its output.
def test_move_sweeps_hidden_source_recreated_between_teardown_and_delete(
    repo, monkeypatch
):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    src_hidden = _project(repo, src_tid)["hidden_source_id"]
    assert src_hidden, "源表未真正投影，测试前置条件不成立"

    store = repo._runtime.knowhow_transfer_store
    real_delete = store.delete_table_if_unchanged
    replacement_ids: list[str] = []

    def _recreate_hidden_source_then_delete(table_id, expected_fingerprint):
        # delete_table_projection(src_hidden) 已经跑完（在 move_table 自己的
        # try 块里，紧邻在这次调用之前）——这里调真实的 ensure_hidden_source，
        # 与生产中恰好卡在这个窗口的并发重投影走的是同一条代码路径：读到的
        # hidden_source_id 仍是刚被拆掉的旧 src_hidden，resolve 失败后铸出一
        # 个全新替身源，并把它写回 table_id 的 hidden_source_id 列。
        replacement_ids.append(
            kh_transfer.build_projector(repo).ensure_hidden_source(table_id)
        )
        return real_delete(table_id, expected_fingerprint)

    monkeypatch.setattr(
        store, "delete_table_if_unchanged", _recreate_hidden_source_then_delete
    )

    new_tid = kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")

    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst_nb
    with pytest.raises(KeyError):
        repo.get_knowhow_table(src_tid)
    assert replacement_ids and replacement_ids[0] != src_hidden, (
        "前置条件：注入确实铸出了一个新的替身隐藏源（不是复用旧的）"
    )

    with repo._connect() as db:
        remaining = [
            dict(r) for r in db.execute(
                "SELECT id FROM sources WHERE notebook_id=? AND source_type='knowhow'",
                (src_nb,),
            ).fetchall()
        ]
    assert not remaining, (
        f"替身隐藏源 {replacement_ids} 必须被收尾扫掉，不能作为孤儿留在源 "
        f"notebook 里继续可被检索到：{remaining}"
    )


def test_move_sweep_is_idempotent_when_no_orphan_exists(repo):
    """Companion happy-path guard: the ordinary, uncontended move (no
    concurrent reprojection racing teardown) must not be affected by the new
    sweep — no orphan to find, sweep is a no-op, move behaves exactly as
    before."""
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    _project(repo, src_tid)

    new_tid = kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")

    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst_nb
    with pytest.raises(KeyError):
        repo.get_knowhow_table(src_tid)
    with repo._connect() as db:
        remaining = db.execute(
            "SELECT id FROM sources WHERE notebook_id=? AND source_type='knowhow'",
            (src_nb,),
        ).fetchall()
    assert not remaining


# --- PR review round 7 P2-B: move-level companion to test_knowhow_transfer_
# store.py's own test_fingerprint_changes_when_cell_code_updated_by_changes.
# cell_code_signal hashed row_id/column_id/code_text/language/
# cell_content_hash but not updated_by — an agent rewriting a cell's code
# attachment with the IDENTICAL code/language/content-hash right after
# copy_table commits, changing only updated_by, used to leave every one of
# those four fields unchanged, so the fingerprint recheck inside move_table
# would see "unchanged" and delete the source — freezing the target on the
# STALE attribution forever. Same injection shape as the sibling round-5
# tests above (monkeypatch kh_transfer.copy_table to call the real
# implementation, then perform the concurrent edit on the source before
# move_table reaches its cleanup section). Appended near EOF (ahead of the
# scheduler-drain fixture below, which must stay the file's literal last
# block per its own comment) for the same zero-line-shift reason as every
# other block in this file.
def test_move_does_not_delete_source_cell_code_updated_by_changed_after_copy_snapshot(
    repo, monkeypatch
):
    src_nb, dst_nb = _nb(repo, "src"), _nb(repo, "dst")
    src_tid = _table_with_row(repo, src_nb)
    detail = repo.get_knowhow_table(src_tid)
    row_id = detail["rows"][0]["id"]
    column_id = detail["columns"][0]["id"]
    repo.upsert_knowhow_cell_code(row_id, column_id, "print(1)", "python", "user-x", "hash-v1")

    real_copy_table = kh_transfer.copy_table

    def _copy_then_concurrent_updated_by_change(repo_, source_table_id, target_notebook_id, actor_id):
        new_id = real_copy_table(repo_, source_table_id, target_notebook_id, actor_id)
        # 模拟并发写者：就在 copy_table 的事务提交之后、move_table 走到自己的
        # 清理段之前，另一个 agent 用完全相同的代码/语言/hash 重写了这个
        # cell 的代码附件，只换了 updated_by。
        repo_.upsert_knowhow_cell_code(row_id, column_id, "print(1)", "python", "user-y", "hash-v1")
        return new_id

    monkeypatch.setattr(kh_transfer, "copy_table", _copy_then_concurrent_updated_by_change)

    with pytest.raises(kh_transfer.SourceCleanupFailed) as exc_info:
        kh_transfer.move_table(repo, src_tid, dst_nb, actor_id="user-x")
    assert exc_info.value.new_table_id
    assert exc_info.value.reason == "source_changed"  # round 10 P1-A

    # 源没被删，且带着并发重写后的新 updated_by——不是复制那一刻的旧属主
    code = repo._runtime.knowhow_store.get_knowhow_cell_code(row_id, column_id)
    assert code["updated_by"] == "user-y"

    dst_tables = repo.list_knowhow_tables(dst_nb)
    assert len(dst_tables) == 1
    assert dst_tables[0]["id"] == exc_info.value.new_table_id


# ---------------------------------------------------------------------------
# 测试间调度器排水（追加于 EOF，避免移动上方守卫行号 pin）。
# copy_table/move_table 会给目标（保留路径还有源）表调度 0.5s 防抖重投影；
# 不 settle 就结束的测试会让 Timer 在测试结束后才点火，把一条持有 repo 强引用
# （_project 里的 target = repo_ref()）的投影线程溢出到同 worker 的后续测试，
# 间歇性打破 test_knowhow_projection.py::test_get_scheduler_entry_does_not_pin_repo
# 的全局 len(_SCHEDULERS)==0 / repo 可回收断言。此 fixture 在每个测试收尾时
# 取消未点火的 Timer 并等在跑的投影收敛，保证不向后续测试泄漏线程。
import pytest as _pytest_drain


@_pytest_drain.fixture(autouse=True)
def _drain_projection_scheduler(repo):
    yield
    from app.services.knowhow import api as _kh_api

    scheduler = _kh_api._SCHEDULERS.get(repo)
    if scheduler is None:
        return
    with scheduler._lock:
        pending = list(scheduler._timers.values())
        scheduler._timers.clear()
        scheduler._rerun.clear()
    for timer in pending:
        timer.cancel()
    deadline = time.time() + 8.0
    while time.time() < deadline:
        with scheduler._lock:
            if not scheduler._running and not scheduler._timers:
                return
        time.sleep(0.05)
