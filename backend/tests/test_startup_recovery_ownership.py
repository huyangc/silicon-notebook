"""崩溃兜底(``_recover_interrupted_jobs``)的归属与覆盖面。

设计见 ``docs/superpowers/specs/2026-07-22-pipeline-damage-recovery-design.md`` A3。
三条性质：

1. **只有服务端有资格清算**：``SQLiteRepository.__init__`` → ``initialize()`` 不再
   跑恢复。离线 CLI／脚本每次运行都会直接构造一个仓储(约 20 处构造点)，旧行为下
   跑一次脚本就会把服务端正在处理的行判成上次崩溃的残骸。
2. **服务端仍然清算**：``startup_warmup.run_startup`` 显式调用一次。
3. **清算发生在就绪门之前**：``mark_ready()`` 之后业务路由才放行；残骸没翻正就放
   用户进来，等于让新任务和旧残骸同时在场。

外加本期新增的一条覆盖面：``parse_status IN ('queued','parsing')`` 的搁浅源落到
可重试终态 ``failed``(两列同写)，而不是永久假装「排队中／解析中」。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path}/recover.db",
        storage_dir=str(tmp_path / "storage"),
    )


def _seed_in_progress_rows(repo: SQLiteRepository) -> None:
    """造一批「上次进程遗留的进行中」行:两个搁浅源(queued/parsing) + 一个正常
    的已解析源(对照组) + 两个 pending/syncing 的 knowhow 行。"""
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb1', 'KB', ?, ?)",
            (now, now),
        )
        for sid, parse_status in (
            ("s-queued", "queued"),
            ("s-parsing", "parsing"),
            ("s-parsed", "parsed"),
        ):
            db.execute(
                """INSERT INTO sources
                   (id, notebook_id, title, source_type, status, parse_status,
                    file_name, file_path, file_size, file_hash, summary, doc_type,
                    error_message, created_at, updated_at)
                   VALUES (?, 'nb1', 'T', 'markdown', ?, ?, 'a.md', '', 0, '', '', '',
                           '', ?, ?)""",
                (sid, parse_status, parse_status, now, now),
            )
        db.execute(
            "INSERT INTO knowhow_tables (id, notebook_id, title, created_at, updated_at) "
            "VALUES ('kt1', 'nb1', 'T', ?, ?)",
            (now, now),
        )
        db.execute(
            "INSERT INTO knowhow_columns (id, table_id, name, position) "
            "VALUES ('kc1', 'kt1', '概念', 0)"
        )
        for rid, status in (("r-pend", "pending"), ("r-sync", "syncing")):
            db.execute(
                "INSERT INTO knowhow_rows "
                "(id, table_id, position, projection_status, created_at, updated_at) "
                "VALUES (?, 'kt1', 0, ?, ?, ?)",
                (rid, status, now, now),
            )


def _sources(repo: SQLiteRepository) -> dict[str, dict]:
    with repo._connect() as db:
        return {
            r["id"]: {
                "status": r["status"],
                "parse_status": r["parse_status"],
                "error_message": r["error_message"],
            }
            for r in db.execute(
                "SELECT id, status, parse_status, error_message FROM sources"
            ).fetchall()
        }


def _knowhow_row_statuses(repo: SQLiteRepository) -> dict[str, str]:
    with repo._connect() as db:
        return {
            r["id"]: r["projection_status"]
            for r in db.execute(
                "SELECT id, projection_status FROM knowhow_rows"
            ).fetchall()
        }


# ---------------------------------------------------------------------------
# 性质 1:离线 CLI 的仓储构造不再误伤
# ---------------------------------------------------------------------------
def test_plain_construction_does_not_settle_in_progress_rows(settings):
    """模拟离线 CLI:直接 SQLiteRepository(settings) 构造(约 20 处这么干)。

    构造 = migrate + seed，**不含**恢复。搁浅源仍是 'queued'/'parsing'，
    knowhow 的 pending/syncing 行也不得被刷成 'failed'(这条是顺带修掉的已知
    老坑:「离线 CLI 启动会把 pending 行刷成 failed」)。"""
    repo = SQLiteRepository(settings)
    _seed_in_progress_rows(repo)

    # 第二次构造 = CLI 再跑一次(同一个磁盘库)。
    SQLiteRepository(settings)

    sources = _sources(SQLiteRepository(settings))
    assert sources["s-queued"]["parse_status"] == "queued"
    assert sources["s-parsing"]["parse_status"] == "parsing"
    assert sources["s-queued"]["error_message"] == ""

    rows = _knowhow_row_statuses(SQLiteRepository(settings))
    assert rows["r-pend"] == "pending"
    assert rows["r-sync"] == "syncing"


# ---------------------------------------------------------------------------
# 性质 2 + 覆盖面:服务端显式清算,搁浅源落到可重试终态
# ---------------------------------------------------------------------------
def test_explicit_recovery_settles_stranded_sources_to_failed(settings):
    """服务端启动路径显式调用后,搁浅源必须落到 'failed':status 与 parse_status
    两列同写(SourceStore.set_status 的既有语义),并带上可读的中文说明。"""
    repo = SQLiteRepository(settings)
    _seed_in_progress_rows(repo)

    repo._recover_interrupted_jobs()

    sources = _sources(repo)
    for sid in ("s-queued", "s-parsing"):
        assert sources[sid]["status"] == "failed", sid
        assert sources[sid]["parse_status"] == "failed", sid
        assert sources[sid]["error_message"], sid
    # 已解析的源是对照组:不在清算范围内,原样不动。
    assert sources["s-parsed"]["parse_status"] == "parsed"
    assert sources["s-parsed"]["error_message"] == ""
    # knowhow 行同一次清算里落终态。
    assert _knowhow_row_statuses(repo) == {"r-pend": "failed", "r-sync": "failed"}


def test_stranded_sources_are_not_relabelled_parsed(settings):
    """搁浅源可能连 source_elements 都没有——标 'parsed' 是谎报,还会让它从
    「待处理」视图里消失。终态只能是可重试的 'failed'。"""
    repo = SQLiteRepository(settings)
    _seed_in_progress_rows(repo)

    repo._recover_interrupted_jobs()

    sources = _sources(repo)
    assert sources["s-queued"]["parse_status"] != "parsed"
    assert sources["s-parsing"]["parse_status"] != "parsed"


def test_recovery_message_is_user_facing_chinese_without_jargon(settings):
    """面向用户的文案不出现内部状态名(与同函数里 kg_build_jobs 那条同风格)。"""
    repo = SQLiteRepository(settings)
    _seed_in_progress_rows(repo)

    repo._recover_interrupted_jobs()

    message = _sources(repo)["s-queued"]["error_message"]
    assert "重新解析" in message
    for jargon in ("queued", "parsing", "parse_status", "status", "failed"):
        assert jargon not in message


def test_recovery_is_idempotent(settings):
    """幂等——safe every boot:第二次跑不改变任何已落终态的行。"""
    repo = SQLiteRepository(settings)
    _seed_in_progress_rows(repo)

    repo._recover_interrupted_jobs()
    first = _sources(repo)
    repo._recover_interrupted_jobs()

    assert _sources(repo) == first


# ---------------------------------------------------------------------------
# 性质 3:清算严格早于 mark_ready()
# ---------------------------------------------------------------------------
def test_run_startup_settles_stranded_sources_before_marking_ready(monkeypatch):
    """直接测那条不变量本身,而不是「调用顺序」的代理:在 ``mark_ready()`` 触发的
    那一刻去读库,搁浅源必须**已经**是终态。

    做法是给 ``readiness.mark_ready`` 套一层探针,在放行业务路由的瞬间抓一张
    快照。就绪门一开用户就能发起新任务,所以「那一刻库里还有搁浅行」和「清算跑
    在就绪之后」是同一件坏事——这条断言两者都盖到。

    变异验证:把 ``repo._recover_interrupted_jobs()`` 移到 ``mark_ready()``
    之后,快照读到的仍是 'queued' → 红;整条删掉同样红。"""
    from app.api.deps import repository
    from app.core import readiness
    from app.services import startup_warmup

    repo = repository()  # 与 run_startup 里拿到的是同一个 lru_cache 实例
    _seed_in_progress_rows(repo)

    at_the_gate: dict[str, dict] = {}
    real_mark_ready = readiness.mark_ready

    def _snapshot_then_open_the_gate():
        at_the_gate["sources"] = _sources(repo)
        at_the_gate["knowhow_rows"] = _knowhow_row_statuses(repo)
        return real_mark_ready()

    monkeypatch.setattr(
        startup_warmup.readiness, "mark_ready", _snapshot_then_open_the_gate
    )

    # begin_lifecycle 里已含 readiness.reset();run_startup 现在必须拿着它发的租约,
    # 收尾用 close_repository 归还,免得 _active_lifecycle 残留卡住后一个用例的 begin。
    lease = startup_warmup.begin_lifecycle()
    repo_ready = None
    try:
        repo_ready = startup_warmup.run_startup(lease)
    finally:
        startup_warmup.close_repository(lease, repo_ready)
        real_mark_ready()  # 还原 conftest 的 autouse 前置条件

    assert readiness.snapshot()["error"] is None  # 启动没有走进异常分支
    assert at_the_gate, "mark_ready() 没被调用——run_startup 根本没走到就绪"
    assert at_the_gate["sources"]["s-queued"]["parse_status"] == "failed"
    assert at_the_gate["sources"]["s-parsing"]["parse_status"] == "failed"
    assert at_the_gate["sources"]["s-queued"]["error_message"]
    assert at_the_gate["knowhow_rows"] == {"r-pend": "failed", "r-sync": "failed"}


# ---------------------------------------------------------------------------
# P1.5 Task 4:内存活跃租约不参与启动清算——就绪门开启那一刻它本就空
# ---------------------------------------------------------------------------
def test_active_lease_is_empty_when_mark_ready_fires(monkeypatch):
    """源级活跃租约(``SourceIngestionService._active_sources``,在途误报抑制的进程内
    dict)**不参与**启动清算,却仍满足「清算这一刻内存租约为空」的不变量——不是因为
    清算清了它(清算压根不碰它),而是因为它重启后天然为空,且业务路由(唯一会跑
    ``process_source`` 填租约的入口)要到 ``mark_ready()`` 之后才放行。单进程 + 清算
    早于就绪门 ⇒ 就绪门开启那一刻不可能有源正被本进程处理 ⇒ 租约此刻本就空。

    沿用上面同款的「mark_ready 探针抓快照」直接钉这条不变量:在放行业务路由的瞬间
    读 runtime-owned 租约本体(**runtime 探针,非 facade 打桩**——租约在
    ``SourceIngestionService`` 内部,读它不进冻结的 facade_surface 契约)。

    变异验证的对偶:这条守的是「到达就绪门时租约本就空」。若未来有人往启动路径里
    误插一步会跑 ``process_source``(或在 ``mark_ready`` 前 stamp 了租约),快照就非空
    → 红,把回归挡在就绪门前。"""
    from app.api.deps import repository
    from app.core import readiness
    from app.services import startup_warmup

    repo = repository()  # 与 run_startup 里拿到的是同一个 lru_cache 实例
    _seed_in_progress_rows(repo)

    at_the_gate: dict[str, dict] = {}
    real_mark_ready = readiness.mark_ready

    def _snapshot_then_open_the_gate():
        # 抓租约本体的浅拷贝(它是可变 dict,快照必须定格在这一刻)。
        at_the_gate["active"] = dict(repo._runtime.source_ingestion._active_sources)
        return real_mark_ready()

    monkeypatch.setattr(
        startup_warmup.readiness, "mark_ready", _snapshot_then_open_the_gate
    )

    # begin_lifecycle 里已含 readiness.reset();run_startup 现在必须拿着它发的租约,
    # 收尾用 close_repository 归还,免得 _active_lifecycle 残留卡住后一个用例的 begin。
    lease = startup_warmup.begin_lifecycle()
    repo_ready = None
    try:
        repo_ready = startup_warmup.run_startup(lease)
    finally:
        startup_warmup.close_repository(lease, repo_ready)
        real_mark_ready()  # 还原 conftest 的 autouse 前置条件

    assert readiness.snapshot()["error"] is None  # 启动没有走进异常分支
    assert "active" in at_the_gate, "mark_ready() 没被调用——run_startup 没走到就绪"
    assert at_the_gate["active"] == {}
