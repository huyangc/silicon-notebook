import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    settings = Settings()
    return SQLiteRepository(settings)


def _mk_user(repo, uid):
    """建一个真实 users 行(notebooks.created_by 有 FK→users.id,见 test_notebook_share_copy.py 同款)。"""
    now = _now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", (uid, f"{uid}@e.test", uid, "user", now, now))


def _seed_user_nb(repo, uid, name="NB"):
    """先建 uid 的 users 行(FK 前提),再以 uid 为 created_by 建一个 notebook,返回其 id。"""
    _mk_user(repo, uid)
    nb_id = f"nb-{uid}-{name}"
    with repo._connect() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, purpose, primary_domain, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (nb_id, name, "", "", "ready", uid, "2026-07-07T00:00:00", "2026-07-07T00:00:00"),
        )
    return nb_id


def test_pending_actions_empty(repo):
    out = repo.pending_actions("user-x")
    assert out == {"count": 0, "items": []}


def test_pending_actions_report_outline(repo):
    nb = _seed_user_nb(repo, "user-a")
    with repo._connect() as db:
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r1", nb, "带隙基准的温漂机理?", "outline_ready", "user-a", "2026-07-07T01:00:00", "2026-07-07T01:00:00"),
        )
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r-intent", nb, "分析一下这个问题", "intent_ready", "user-a", "2026-07-07T02:00:00", "2026-07-07T02:00:00"),
        )
        # 干扰项:非待确认态、他人的报告 —— 都不该出现
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r2", nb, "x", "generating", "user-a", "2026-07-07T01:00:00", "2026-07-07T01:00:00"),
        )
    out = repo.pending_actions("user-a")
    items = [it for it in out["items"] if it["type"] == "report_outline"]
    assert {item["report_id"] for item in items} == {"r1", "r-intent"}
    assert all(item["notebook_id"] == nb for item in items)
    assert all(item["title"] for item in items)  # question 截断非空
    assert out["count"] == 2


def test_pending_actions_governance_counts(repo):
    nb = _seed_user_nb(repo, "user-a")
    with repo._connect() as db:
        db.execute("INSERT INTO concept_merge_candidates (id, notebook_id, canonical_a, canonical_b, score, status, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("m1", nb, "K-A", "K-B", 0.9, "pending", "2026-07-07T01:00:00", "2026-07-07T01:00:00"))
        db.execute("INSERT INTO concept_merge_candidates (id, notebook_id, canonical_a, canonical_b, score, status, created_at, updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("m2", nb, "K-C", "K-D", 0.9, "confirmed", "2026-07-07T01:00:00", "2026-07-07T01:00:00"))  # 非 pending 不计
    out = repo.pending_actions("user-a")
    gov = [it for it in out["items"] if it["type"] == "governance" and it["subtype"] == "merge"]
    assert len(gov) == 1
    assert gov[0]["count"] == 1
    assert gov[0]["notebook_id"] == nb


def test_pending_actions_isolation(repo):
    """他人创建的 notebook 的待办不出现在我的中心。"""
    nb_other = _seed_user_nb(repo, "user-b")
    with repo._connect() as db:
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r1", nb_other, "x", "outline_ready", "user-b", "2026-07-07T01:00:00", "2026-07-07T01:00:00"),
        )
    out = repo.pending_actions("user-a")
    assert out == {"count": 0, "items": []}


def test_member_without_own_notebooks_still_sees_their_shared_notebook_report(repo):
    """共享库里成员自建的待确认报告必须进铃铛,哪怕他一个自有库都没有(P1-T3b)。

    报告那一半的谓词只有 `created_by`,一个 notebook id 都不消费——它此前却待在
    `if notebook_ids:` 闸内,于是「没建过库的成员」铃铛恒为 0,而报告卡在
    intent_ready 等他确认,是一条走不通的路。
    库名由 P1-T4 补上:它不再取自「我自有的库」映射(共享库不在其中,条目会显示成
    一条没有出处的报告),而是随报告行 LEFT JOIN 出来。"""
    owner_nb = _seed_user_nb(repo, "user-owner")
    _mk_user(repo, "user-member")          # 成员自己一个 notebook 都没有
    # 建报告的前提本来就是对该库有读权(require_notebook_read);铃铛现在叠加了
    # 同一读谓词(codex #517 R1 P2),夹具如实给出成员行。
    repo.add_member(owner_nb, "user-member")
    with repo._connect() as db:
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r-shared", owner_nb, "成员自己的问题", "intent_ready", "user-member",
             "2026-07-07T01:00:00", "2026-07-07T01:00:00"),
        )

    out = repo.pending_actions("user-member")
    items = [it for it in out["items"] if it["type"] == "report_outline"]
    assert [it["report_id"] for it in items] == ["r-shared"]
    assert items[0]["notebook_id"] == owner_nb
    # 库名来自共享库那一行本身,不是「我自有的库」映射(P1-T4)。
    assert items[0]["notebook_name"] == "NB"
    assert out["count"] == 1

    # 反向:owner 的铃铛里不出现成员的报告(行级隔离在这一层同样成立)。
    owner_items = [
        it for it in repo.pending_actions("user-owner")["items"]
        if it["type"] == "report_outline"
    ]
    assert owner_items == []


def test_pending_report_drops_out_when_the_creator_loses_read_access(repo):
    """创建者失去读权后,待确认报告不进铃铛;恢复读权自动回来(codex #517 R1 P2)。

    没有这道谓词,被撤权的成员会永远挂着一条点不开的待确认项——每个报告端点都
    对他 404(require_notebook_read),铃铛却还在催他确认。修法是把 access_sql 的
    规范读谓词叠进铃铛查询,与「授权即时生效」同口径;条目从未被删,恢复即回。"""
    owner_nb = _seed_user_nb(repo, "user-owner")
    _mk_user(repo, "user-member")
    repo.add_member(owner_nb, "user-member")
    with repo._connect() as db:
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r-revoked", owner_nb, "撤权场景", "intent_ready", "user-member",
             "2026-07-07T02:00:00", "2026-07-07T02:00:00"),
        )

    def _member_report_ids():
        return [
            it["report_id"] for it in repo.pending_actions("user-member")["items"]
            if it["type"] == "report_outline"
        ]

    assert _member_report_ids() == ["r-revoked"]
    repo.remove_member(owner_nb, "user-member")
    assert _member_report_ids() == []
    repo.add_member(owner_nb, "user-member")
    assert _member_report_ids() == ["r-revoked"]


def test_pending_actions_index_state(repo, monkeypatch):
    """索引态走 scale_index_status().state;用 monkeypatch 覆盖真实态,
    避免真造 stale/building 场景(需真实磁盘 manifest/后台线程)。"""
    nb = _seed_user_nb(repo, "user-a")

    def _fake_status(notebook_id):
        assert notebook_id == nb
        return {"state": "stale", "total_chunks": 100, "delta_chunks": 40}

    monkeypatch.setattr(repo.__dict__["_runtime"].scale_artifacts, "status", _fake_status)
    out = repo.pending_actions("user-a")
    idx_items = [it for it in out["items"] if it["type"] == "index"]
    assert len(idx_items) == 1
    assert idx_items[0]["state"] == "stale"
    assert idx_items[0]["notebook_id"] == nb
    assert out["count"] == 1


def test_pending_actions_index_building_not_counted(repo, monkeypatch):
    """building/queued 不计入 count(不是"待用户确认"的动作项)。"""
    nb = _seed_user_nb(repo, "user-a")
    monkeypatch.setattr(repo.__dict__["_runtime"].scale_artifacts, "status",
                         lambda notebook_id: {"state": "building", "total_chunks": 100, "delta_chunks": 40})
    out = repo.pending_actions("user-a")
    idx_items = [it for it in out["items"] if it["type"] == "index"]
    assert len(idx_items) == 1
    assert idx_items[0]["state"] == "building"
    assert out["count"] == 0


def test_pending_actions_index_queued_state_passthrough_no_progress(repo, monkeypatch):
    """queued 不再被伪装成 building —— 排队态没有构建进度这种误导字段
    (total-delta)/total 是构建进度,不是排队进度)。"""
    nb = _seed_user_nb(repo, "user-a")
    monkeypatch.setattr(
        repo.__dict__["_runtime"].scale_artifacts,
        "status",
        lambda notebook_id: {"state": "queued", "total_chunks": 100, "delta_chunks": 40},
    )
    out = repo.pending_actions("user-a")
    idx_items = [it for it in out["items"] if it["type"] == "index"]
    assert len(idx_items) == 1
    assert idx_items[0]["state"] == "queued"
    assert "progress" not in idx_items[0]
    assert out["count"] == 0  # queued 一直不计入 count(既有口径不变)


def test_pending_actions_index_building_still_has_progress(repo, monkeypatch):
    """building 仍要有构建进度(只有 queued 被排除在进度计算之外)。"""
    nb = _seed_user_nb(repo, "user-a")
    monkeypatch.setattr(
        repo.__dict__["_runtime"].scale_artifacts,
        "status",
        lambda notebook_id: {"state": "building", "total_chunks": 100, "delta_chunks": 40},
    )
    out = repo.pending_actions("user-a")
    idx_items = [it for it in out["items"] if it["type"] == "index"]
    assert len(idx_items) == 1
    assert idx_items[0]["state"] == "building"
    assert idx_items[0]["progress"] == 60
    assert out["count"] == 0


def test_pending_actions_index_unindexed_not_surfaced(repo):
    """真实未建索引的全新 notebook(state=unindexed)不应出现在待办里
    (unindexed 不是"待确认",只是"从未建过";suggested/stale 才是)。"""
    _seed_user_nb(repo, "user-a")
    out = repo.pending_actions("user-a")
    assert out == {"count": 0, "items": []}


def _seed_promotion_candidate(repo, nb_id, cand_id="promo-1"):
    """在 promotion_candidates 插一条 status='proposed' 的行(propose_promotion
    对非 admin 也放行,见 require_notebook_access 守卫;/promotion-queue 才是
    admin-only,故此表本身不隐含 admin 身份)。"""
    with repo._connect() as db:
        db.execute(
            "INSERT INTO promotion_candidates "
            "(id, notebook_id, object_id, object_type, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (cand_id, nb_id, "obj-1", "concept", "proposed",
             "2026-07-07T01:00:00", "2026-07-07T01:00:00"),
        )


def test_pending_actions_promotion_hidden_for_non_admin(repo):
    """非 admin 创建的晋升候选不应出现在其待办中心 —— /promotion-queue 深链是
    admin-only(403),铃铛不该指向一个必 403 的动作(见本方法 docstring)。"""
    nb = _seed_user_nb(repo, "user-a")  # _mk_user 建的 role 固定为 'user'
    _seed_promotion_candidate(repo, nb)
    out = repo.pending_actions("user-a")
    gov = [it for it in out["items"] if it["type"] == "governance" and it["subtype"] == "promotion"]
    assert gov == []
    assert out["count"] == 0


def test_pending_actions_promotion_visible_for_admin(repo):
    """admin 创建的晋升候选应正常出现(admin 可访问 /promotion-queue)。"""
    now = _now()
    uid = "user-admin"
    with repo._write() as db:
        db.execute(
            "INSERT INTO users (id,email,display_name,role,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?)", (uid, f"{uid}@e.test", uid, "admin", now, now))
    nb_id = f"nb-{uid}-NB"
    with repo._connect() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, purpose, primary_domain, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (nb_id, "NB", "", "", "ready", uid, "2026-07-07T00:00:00", "2026-07-07T00:00:00"),
        )
    _seed_promotion_candidate(repo, nb_id)
    out = repo.pending_actions(uid)
    gov = [it for it in out["items"] if it["type"] == "governance" and it["subtype"] == "promotion"]
    assert len(gov) == 1
    assert gov[0]["count"] == 1
    assert gov[0]["notebook_id"] == nb_id
    assert out["count"] == 1


def test_pending_actions_includes_paper_meta_building(repo):
    """补抽进行中，list_for_user 含 type=paper_meta 项，progress 反映内存 dict；
    building 不计入 count(跟 index building 一致——只显示，不响铃)。"""
    nb = _seed_user_nb(repo, "user-a")
    svc = repo.__dict__["_runtime"].source_ingestion
    pending = repo.__dict__["_runtime"].pending_actions_service

    # 未在跑 → 无 paper_meta 项
    items0 = pending.list_for_user("user-a")["items"]
    assert all(i["type"] != "paper_meta" for i in items0)

    # 手动注入进行中(模拟 backfill_paper_metadata 运行期间的内存态)
    with svc._paper_meta_backfilling_lock:
        svc._paper_meta_backfilling[nb] = {"total": 5, "done": 2}
    try:
        out = pending.list_for_user("user-a")
        pm = [i for i in out["items"] if i["type"] == "paper_meta"]
        assert len(pm) == 1
        assert pm[0]["state"] == "building"
        assert pm[0]["notebook_id"] == nb
        assert pm[0]["notebook_name"] == "NB"
        assert pm[0]["progress"] == {"done": 2, "total": 5}
        assert out["count"] == 0
    finally:
        with svc._paper_meta_backfilling_lock:
            svc._paper_meta_backfilling.pop(nb, None)


def test_pending_actions_paper_meta_per_user_filter(repo):
    """非 owner 看不到该 notebook 的 paper_meta 项(per-user 隔离由
    pending_actions_projection_rows 的 created_by 过滤保证)。"""
    nb = _seed_user_nb(repo, "user-a")
    svc = repo.__dict__["_runtime"].source_ingestion
    with svc._paper_meta_backfilling_lock:
        svc._paper_meta_backfilling[nb] = {"total": 1, "done": 0}
    try:
        pending = repo.__dict__["_runtime"].pending_actions_service
        # 陌生 uid(非 notebook owner，甚至没有 users 行)看不到该项
        items = pending.list_for_user("user-stranger-999")["items"]
        assert all(
            i.get("notebook_id") != nb for i in items if i["type"] == "paper_meta"
        )
    finally:
        with svc._paper_meta_backfilling_lock:
            svc._paper_meta_backfilling.pop(nb, None)


def test_pending_actions_paper_meta_survives_index_status_failure(repo, monkeypatch):
    """索引态查询抛异常时,paper_meta 项仍应出现(scope-widening 回归守卫:
    确保一个 notebook 的 scale_runtime.status() 失败不会顺带吞掉同一个 notebook
    的 paper_meta 项——旧实现里 `except: continue` 会把两者一起跳过)。"""
    nb = _seed_user_nb(repo, "user-a")
    runtime = repo.__dict__["_runtime"]

    def _boom(notebook_id):
        raise RuntimeError("scale status unavailable")

    monkeypatch.setattr(runtime.scale_artifacts, "status", _boom)

    svc = runtime.source_ingestion
    with svc._paper_meta_backfilling_lock:
        svc._paper_meta_backfilling[nb] = {"total": 3, "done": 1}
    try:
        out = runtime.pending_actions_service.list_for_user("user-a")
        pm = [i for i in out["items"] if i["type"] == "paper_meta"]
        assert len(pm) == 1
        assert pm[0]["state"] == "building"
        assert pm[0]["notebook_id"] == nb
        assert pm[0]["progress"] == {"done": 1, "total": 3}
        # index 分支因 status 抛异常而不产出该 nb 的 index 项
        assert not any(
            i["type"] == "index" and i["notebook_id"] == nb for i in out["items"]
        )
    finally:
        with svc._paper_meta_backfilling_lock:
            svc._paper_meta_backfilling.pop(nb, None)
