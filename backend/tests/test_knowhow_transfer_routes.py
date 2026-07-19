# backend/tests/test_knowhow_transfer_routes.py
# 认证沿用 tests/test_notebook_share_readonly.py 的 _login 样板：真实注册+登录拿 Bearer，
# repo fixture 与 app 共享同一 tmp DB（autouse conftest 清 repository() lru_cache）。
import pytest
from fastapi.testclient import TestClient
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

COLUMNS = [{"name": "违例类型", "role": "anchor"}, {"name": "现象识别", "role": "procedure"}]

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

@pytest.fixture
def client(repo):
    from app.main import app
    return TestClient(app)

def _login(client, username, password="pw123456"):
    client.post("/api/auth/register", json={"username": username, "password": password})
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()["token"]
    return {"Authorization": f"Bearer {tok}"}

def _table(repo, nb):
    tid = repo.create_knowhow_table(nb, "T", "d", COLUMNS, created_by="")
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}
    repo.add_knowhow_row(tid, {cols["违例类型"]: "过冲"})
    return tid

def test_copy_endpoint_creates_table_in_target(client, repo):
    h = _login(client, "a00000001")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    tid = _table(repo, src)
    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": dst, "mode": "copy"},
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    new_tid = resp.json()["new_table_id"]
    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst

def test_transfer_to_same_notebook_rejected(client, repo):
    h = _login(client, "a00000002")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    tid = _table(repo, src)
    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": src, "mode": "copy"},
        headers=h,
    )
    assert resp.status_code == 400


def test_move_source_cleanup_failure_returns_409_with_new_table_id(client, repo, monkeypatch):
    """A3 评审附加需求：move 时复制已提交、但删源失败——不能让用户拿到裸 500
    去盲目重试(会在目标侧越堆越多重复副本)。故障注入删源这一步(不依赖真实
    投影/embedder：patch 掉真正执行删除的 KnowhowTransferStore.
    delete_table_if_unchanged，_table() 建的表从未投影，hidden_source_id 恒为
    None，走的正是"只删源"这条无条件必经路径——round 3 P1-1 把原来 repo.
    delete_knowhow_table 那次无条件删除换成了这个原子条件删除，故障注入点
    随之搬到这里，参见 backend/app/services/knowhow/transfer.py 的 move_table)。
    在类上 patch（而非 `repo._runtime.knowhow_transfer_store` 实例）：这条用例
    走 HTTP（client 触发 app 自己的请求处理路径），app 的依赖注入通过
    app/api/deps.py 的 `@lru_cache def repository()` 解析仓库实例，与这里
    `repo` 夹具直接构造的 SQLiteRepository(Settings()) 是两个不同的 Python
    对象（只是共享同一个 tmp DB 文件）——patch 实例只会打中 `repo` 自己，
    request 处理走的是另一个对象，必须 patch 类本身才能两边都命中。
    断言：状态码 409 + 结构化 code + new_table_id 在目标侧可解析 + 源表原封
    不动地还在——"重复不丢失"必须被诚实地捅给调用方，而不是悄悄吞掉。"""
    from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore

    def _boom_delete(self, table_id, expected_fingerprint):
        raise RuntimeError("simulated delete_table_if_unchanged failure")

    monkeypatch.setattr(KnowhowTransferStore, "delete_table_if_unchanged", _boom_delete)

    h = _login(client, "a00000003")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    tid = _table(repo, src)

    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": dst, "mode": "move"},
        headers=h,
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "source_cleanup_failed"
    # round 10 P1-A：这条用例是"清理操作本身抛异常"这一支（reason=
    # cleanup_error）——message 必须落在"手动删源安全"这条既有指引上,不能被
    # 下面新增的 source_changed 分支意外顶替。
    assert "手动删除源表" in detail["message"]
    new_tid = detail["new_table_id"]
    assert new_tid
    # 副本已在目标侧、可解析
    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst
    # 源表仍在——删源失败绝不能连带丢了源(duplicate-not-loss)
    assert repo.get_knowhow_table(tid)["notebook_id"] == src


# round 10 P1-A：上面那条用例的镜像——源清理失败的成因不是清理操作本身抛异常
# （delete_table_if_unchanged 直接 raise），而是源在复制完成后被并发编辑（指纹
# 复核未命中，delete_table_if_unchanged 正常返回 False）：源是被有意保留下来
# 保护这份编辑的，绝不能引导用户删除它。这条截然不同的成因必须映射到截然不同
# 的 code（source_changed_kept），且 message 绝不能出现"删除源表"字样——照做
# 会连带永久丢失这份被保留下来的并发编辑。
#
# 并发编辑通过 monkeypatch 模块级 `copy_table`（`app.services.knowhow.transfer`
# 模块属性，同 test_knowhow_transfer_service.py 里
# test_move_does_not_delete_source_row_added_after_copy_snapshot 的同款手法）
# 在真实 copy_table 返回之后、move_table 走到自己的清理段之前，往源表插入一
# 行——这条 monkeypatch 是模块级的，对 HTTP 路由触发的调用同样生效：move_table
# 内部对 copy_table 的调用是它自己模块全局命名空间里的名字查找，不是绑定引用，
# 不需要（也不能像 KnowhowTransferStore 那样）改成 patch 类。
def test_move_source_changed_returns_distinct_code_and_never_suggests_deleting_source(
    client, repo, monkeypatch
):
    from app.services.knowhow import transfer as kh_transfer

    h = _login(client, "a00000004")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=h).json()["id"]
    dst = client.post("/api/notebooks", json={"name": "dst"}, headers=h).json()["id"]
    tid = _table(repo, src)
    cols = {c["name"]: c["id"] for c in repo.get_knowhow_table(tid)["columns"]}

    real_copy_table = kh_transfer.copy_table

    def _copy_then_concurrent_row_add(repo_, source_table_id, target_notebook_id, actor_id):
        new_id = real_copy_table(repo_, source_table_id, target_notebook_id, actor_id)
        repo_.add_knowhow_row(source_table_id, {cols["违例类型"]: "并发新增的行"})
        return new_id

    monkeypatch.setattr(kh_transfer, "copy_table", _copy_then_concurrent_row_add)

    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": dst, "mode": "move"},
        headers=h,
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "source_changed_kept"
    # 不能只查"删除源表"不出现——message 里合法地用"请勿删除源表"这个否定
    # 祈使句提醒用户，这个短语本身就含着"删除源表"四个字。真正要挡的是
    # cleanup_error 消息那句会诱导操作的正面祈使句"手动删除源表"；同时正面
    # 钉住这条消息确实包含明确的"别删"警示，不是恰好没提。
    assert "手动删除源表" not in detail["message"], (
        "source_changed 绝不能沿用 cleanup_error 那句「手动删除源表」——照做"
        "会连带永久丢失被保留下来的并发编辑：" + detail["message"]
    )
    assert "请勿删除源表" in detail["message"] or "不要删除源表" in detail["message"], (
        "source_changed 的指引必须明确提醒用户别删源表：" + detail["message"]
    )
    new_tid = detail["new_table_id"]
    assert new_tid
    assert repo.get_knowhow_table(new_tid)["notebook_id"] == dst
    # 源表仍在，且带着并发新增的那一行——不是复制那一刻的旧快照
    src_detail = repo.get_knowhow_table(tid)
    assert src_detail["notebook_id"] == src
    assert len(src_detail["rows"]) == 2


# --------------------------------------------------------------------------
# 访问控制边界（A4 评审 Important）：上面三条用例全都以 notebook OWNER 身份跑，
# 而 owner 在 user_can_read_notebook / user_can_access_notebook 下都是 True，
# 同 notebook 那条又在触达任何守卫之前就 400 返回了——于是路由里
# 「copy 用读守卫 / move 用写守卫」这个三元表达式完全没有被覆盖：把它反过来
# 写，三条用例依旧全绿，而只读成员就此获得了删除别人表的能力（move = 复制
# + 删源）。下面四条专门钉这条接线。追加在文件尾：在 line 72 之上插入会顶掉
# test_repository_surface_manifest.py 里按行号钉死的 patch 站点。
# --------------------------------------------------------------------------

def _uid(client, headers):
    return client.get("/api/me", headers=headers).json()["id"]


def test_readonly_member_can_copy_but_never_move(client, repo):
    """只读成员：copy 放行(200)，move 必须 404 且源表分毫未动。

    这一条单独就能杀死「三元表达式写反」这个变异：反过来写的话 move 会改用
    读守卫，bob 通过 → 源表被复制走并删除 → 断言的 404 和「源表还在」双双失败。
    """
    owner_h = _login(client, "a00000010")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=owner_h).json()["id"]
    tid = _table(repo, src)

    bob_h = _login(client, "b00000011")
    bob_nb = client.post("/api/notebooks", json={"name": "bob"}, headers=bob_h).json()["id"]
    repo.add_member(src, _uid(client, bob_h))  # 只读成员：能读源，但不是 owner

    # copy：源侧只需读权限 → 放行
    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": bob_nb, "mode": "copy"},
        headers=bob_h,
    )
    assert resp.status_code == 200, resp.text
    assert repo.get_knowhow_table(resp.json()["new_table_id"])["notebook_id"] == bob_nb

    # move：源侧要写权限(move 会删源) → 只读成员必须 404，不泄露存在性
    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": bob_nb, "mode": "move"},
        headers=bob_h,
    )
    assert resp.status_code == 404, resp.text
    # 最要紧的一条：源表必须还在。只读成员绝不能删掉别人的表。
    assert repo.get_knowhow_table(tid)["notebook_id"] == src


def test_stranger_gets_404_for_source_notebook(client, repo):
    """非 owner 非成员：连 copy 都不行，且 404 而非 403（不泄露存在性）。"""
    owner_h = _login(client, "a00000020")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=owner_h).json()["id"]
    tid = _table(repo, src)

    stranger_h = _login(client, "c00000021")
    stranger_nb = client.post(
        "/api/notebooks", json={"name": "c"}, headers=stranger_h
    ).json()["id"]

    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": stranger_nb, "mode": "copy"},
        headers=stranger_h,
    )
    assert resp.status_code == 404, resp.text
    assert repo.get_knowhow_table(tid)["notebook_id"] == src


def test_target_notebook_not_owned_is_404(client, repo):
    """源侧是自己的，但目标 notebook 是别人的 → 404（目标恒需写权限）。
    否则任何人都能把表塞进别人的 notebook 里。"""
    owner_h = _login(client, "a00000030")
    src = client.post("/api/notebooks", json={"name": "src"}, headers=owner_h).json()["id"]
    tid = _table(repo, src)

    other_h = _login(client, "d00000031")
    other_nb = client.post("/api/notebooks", json={"name": "other"}, headers=other_h).json()["id"]

    resp = client.post(
        f"/api/notebooks/{src}/knowhow/{tid}/transfer",
        json={"target_notebook_id": other_nb, "mode": "copy"},
        headers=owner_h,
    )
    assert resp.status_code == 404, resp.text


def test_table_from_another_notebook_is_404(client, repo):
    """表 id 存在、两个 notebook 也都是自己的，但表不属于 URL 里那个源
    notebook → 404。守卫只证明「你能访问 URL 里这个 notebook」，不证明
    「这个 table_id 属于它」。"""
    h = _login(client, "a00000040")
    nb_a = client.post("/api/notebooks", json={"name": "a"}, headers=h).json()["id"]
    nb_b = client.post("/api/notebooks", json={"name": "b"}, headers=h).json()["id"]
    tid_in_b = _table(repo, nb_b)  # 表在 B，却从 A 的 URL 去搬它

    resp = client.post(
        f"/api/notebooks/{nb_a}/knowhow/{tid_in_b}/transfer",
        json={"target_notebook_id": nb_b, "mode": "copy"},
        headers=h,
    )
    assert resp.status_code == 404, resp.text
    assert repo.get_knowhow_table(tid_in_b)["notebook_id"] == nb_b


# ---------------------------------------------------------------------------
# 测试间调度器排水（同 test_knowhow_transfer_service.py 尾部同名 fixture 的
# 理由）：经路由 copy/move 同样会调度 0.5s 防抖重投影，不 settle 即结束的
# 测试会把投影线程溢出到同 worker 后续测试，间歇性打破
# test_get_scheduler_entry_does_not_pin_repo。收尾取消未点火 Timer 并等收敛。
import time as _drain_time

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
    deadline = _drain_time.time() + 8.0
    while _drain_time.time() < deadline:
        with scheduler._lock:
            if not scheduler._running and not scheduler._timers:
                return
        _drain_time.sleep(0.05)
