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


# --- 进行中的提问(待确认中心「问答进行中」分组) -----------------------------


def _insert_ask_job(repo, job_id, notebook_id, created_by, **kw):
    """插一行 ask_jobs。默认 running、默认 created_at 递增(按 job_id 排序无关)。"""
    with repo._connect() as db:
        db.execute(
            "INSERT INTO ask_jobs (id,notebook_id,conversation_id,created_by,mode,"
            "question,asked_at,status,trace_json,answer_id,error,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,'','','',?,?)",
            (
                job_id, notebook_id, kw.get("conversation_id", f"conv-{job_id}"),
                created_by, kw.get("mode", "reasoning"),
                kw.get("question", "带隙基准的温漂机理是什么?"),
                kw.get("asked_at", "2026-07-07T03:00:00+00:00"),
                kw.get("status", "running"),
                kw.get("created_at", "2026-07-07T03:00:00+00:00"),
                kw.get("created_at", "2026-07-07T03:00:00+00:00"),
            ),
        )


def test_pending_actions_running_ask_item_fields(repo):
    """在途提问进铃铛,字段齐全;问题只带摘要、不带全文。"""
    from app.repositories.pending_action_rows import ASK_QUESTION_PREVIEW_CHARS

    nb = _seed_user_nb(repo, "user-a")
    long_question = "为" * (ASK_QUESTION_PREVIEW_CHARS + 40)
    _insert_ask_job(repo, "askjob-1", nb, "user-a", question=long_question,
                    conversation_id="conv-x")

    out = repo.pending_actions("user-a")
    asks = [it for it in out["items"] if it["type"] == "ask"]
    assert len(asks) == 1
    item = asks[0]
    assert item["state"] == "running"
    assert item["job_id"] == "askjob-1"
    assert item["notebook_id"] == nb
    assert item["notebook_name"] == "NB"
    assert item["conversation_id"] == "conv-x"
    assert item["asked_at"] == "2026-07-07T03:00:00+00:00"
    # 摘要恰好被截断,提示词全文不进铃铛快照。
    assert item["title"] == long_question[:ASK_QUESTION_PREVIEW_CHARS]
    assert len(item["title"]) < len(long_question)
    # 在途提问不响铃:它是状态展示,不是「待你确认」的动作。
    assert out["count"] == 0


def test_pending_actions_running_ask_owner_isolation(repo):
    """属主隔离:别人在我库里跑的提问不进我的铃铛,我在别人库里跑的进我的铃铛。"""
    owner_nb = _seed_user_nb(repo, "user-owner")
    _mk_user(repo, "user-member")
    repo.add_member(owner_nb, "user-member")
    _insert_ask_job(repo, "askjob-owner", owner_nb, "user-owner")
    _insert_ask_job(repo, "askjob-member", owner_nb, "user-member")

    def _ask_ids(uid):
        return [it["job_id"] for it in repo.pending_actions(uid)["items"]
                if it["type"] == "ask"]

    # 库主只看到自己那条 —— 成员在他库里跑的提问是成员的动作。
    assert _ask_ids("user-owner") == ["askjob-owner"]
    # 成员一个自有库都没有,但他在共享库里的在途提问照样进他的铃铛
    #(与「待确认报告」同一条 `if notebook_ids:` 闸外裁决),库名随行带回。
    member_items = [it for it in repo.pending_actions("user-member")["items"]
                    if it["type"] == "ask"]
    assert [it["job_id"] for it in member_items] == ["askjob-member"]
    assert member_items[0]["notebook_name"] == "NB"


def test_pending_actions_running_ask_terminal_states_absent(repo):
    """终态一律不出现——包括重启兜底写下的 interrupted。"""
    nb = _seed_user_nb(repo, "user-a")
    for index, status in enumerate(
        ("done", "failed", "cancelled", "interrupted", "running")
    ):
        _insert_ask_job(repo, f"askjob-{status}", nb, "user-a", status=status,
                        created_at=f"2026-07-07T0{index}:00:00+00:00")

    asks = [it for it in repo.pending_actions("user-a")["items"] if it["type"] == "ask"]
    assert [it["job_id"] for it in asks] == ["askjob-running"]


def test_pending_actions_running_ask_drops_out_when_read_access_is_lost(repo):
    """对该库失权 → 条目出铃铛;恢复读权自动回来(与待确认报告同口径)。"""
    owner_nb = _seed_user_nb(repo, "user-owner")
    _mk_user(repo, "user-member")
    repo.add_member(owner_nb, "user-member")
    _insert_ask_job(repo, "askjob-shared", owner_nb, "user-member")

    def _member_ask_ids():
        return [it["job_id"] for it in repo.pending_actions("user-member")["items"]
                if it["type"] == "ask"]

    assert _member_ask_ids() == ["askjob-shared"]
    repo.remove_member(owner_nb, "user-member")
    assert _member_ask_ids() == []
    repo.add_member(owner_nb, "user-member")
    assert _member_ask_ids() == ["askjob-shared"]


def test_pending_actions_running_asks_are_bounded_newest_first(repo):
    """条数封顶在 RUNNING_ASK_ROWS,且取的是**最新**的那一批。"""
    from app.repositories.pending_action_rows import RUNNING_ASK_ROWS

    nb = _seed_user_nb(repo, "user-a")
    total = RUNNING_ASK_ROWS + 5
    for index in range(total):
        # created_at 递增:index 越大越新。
        _insert_ask_job(
            repo, f"askjob-{index:03d}", nb, "user-a",
            created_at=f"2026-07-07T00:00:{index:02d}+00:00",
        )

    asks = [it for it in repo.pending_actions("user-a")["items"] if it["type"] == "ask"]
    assert len(asks) == RUNNING_ASK_ROWS
    newest = [f"askjob-{index:03d}" for index in range(total - 1, total - 1 - RUNNING_ASK_ROWS, -1)]
    assert [it["job_id"] for it in asks] == newest


def test_pending_actions_running_ask_asked_at_falls_back_to_created_at(repo):
    """asked_at 为空(旧行 / 不带该字段的客户端)时回落到服务端写入时刻——
    否则前端算不出「已进行多久」,只能显示一个从 1970 年算起的荒谬时长。"""
    nb = _seed_user_nb(repo, "user-a")
    _insert_ask_job(repo, "askjob-noaskedat", nb, "user-a", asked_at="",
                    created_at="2026-07-07T05:00:00+00:00")

    asks = [it for it in repo.pending_actions("user-a")["items"] if it["type"] == "ask"]
    assert asks[0]["asked_at"] == "2026-07-07T05:00:00+00:00"


def test_pending_actions_running_ask_in_a_notebook_being_deleted_is_absent(repo):
    """删除/拷贝中的库不给可点击的深链(NOTEBOOK_LIVE_SQL 同口径)。"""
    nb = _seed_user_nb(repo, "user-a")
    _insert_ask_job(repo, "askjob-live", nb, "user-a")
    assert any(it["type"] == "ask" for it in repo.pending_actions("user-a")["items"])

    with repo._connect() as db:
        db.execute("UPDATE notebooks SET status='deleting' WHERE id=?", (nb,))
    assert not any(it["type"] == "ask" for it in repo.pending_actions("user-a")["items"])


# --- 同步 Ask 路径的推送边界(流式那条由 test_ask_execution_coordinator.py 钉) ---


class _FakeSyncAsk:
    """`AskService.ask_current` 的最小协作者集合。

    以未绑定函数直接调用真实的 `ask_current`(`AskService.ask_current(fake, …)`),
    只替换它使用的那几个协作者:测试要钉的是**推送落在哪几个时刻**,不是引擎行为。
    """

    def __init__(self, calls, response=None, boom=None):
        self.calls = calls
        self._response = response
        self._boom = boom

    def current_user_id(self):
        return "user-sync"

    def _resolve_ask_mode(self, mode):
        from types import SimpleNamespace
        return SimpleNamespace(id="chunk")

    def validate_reasoning_submission(self, notebook_id, payload):
        return None

    def begin_job_current(self, notebook_id, payload, mode, cancel_event):
        self.calls.append(("begin", notebook_id, mode))
        return "askjob-sync", "conv-sync"

    def ask(self, notebook_id, payload, *, user_id, job_id, cancel_event):
        self.calls.append(("ask", job_id, user_id))
        if self._boom is not None:
            raise self._boom
        return self._response

    def finish_job(self, job_id, status, *, answer_id="", error=""):
        self.calls.append(("finish", status))


def _sync_ask_calls(monkeypatch, *, response=None, boom=None):
    from types import SimpleNamespace
    import app.services.ask_service as ask_service_module

    calls: list = []
    monkeypatch.setattr(
        ask_service_module, "publish_snapshot",
        lambda user_id: calls.append(("publish", user_id)),
    )
    fake = _FakeSyncAsk(calls, response=response, boom=boom)
    payload = SimpleNamespace(mode="chunk")
    return calls, lambda: ask_service_module.AskService.ask_current(fake, "nb-1", payload)


def test_sync_ask_publishes_pending_snapshot_at_start_and_at_the_terminal_row(monkeypatch):
    """同步路径同样是「起点一次、终态一次」。终态那次由 `finally` 覆盖全部出口,
    而不是在每个 `finish_job` 旁边各抄一遍——抄的那种写法迟早会漏掉后来新增的
    出口,而漏掉的形态是「铃铛上留着一条永远不消失的进行中提问」。"""
    from types import SimpleNamespace

    calls, run = _sync_ask_calls(
        monkeypatch, response=SimpleNamespace(answer_id="ans-sync")
    )
    run()
    assert calls == [
        ("begin", "nb-1", "chunk"),
        ("publish", "user-sync"),
        ("ask", "askjob-sync", "user-sync"),
        ("finish", "done"),
        ("publish", "user-sync"),
    ]


def test_sync_ask_publishes_a_terminal_snapshot_when_the_engine_raises(monkeypatch):
    """引擎抛出时同样要刷一次——否则失败的提问会永远挂在铃铛上。"""
    calls, run = _sync_ask_calls(monkeypatch, boom=RuntimeError("engine down"))
    with pytest.raises(RuntimeError, match="engine down"):
        run()
    assert calls[-2:] == [("finish", "failed"), ("publish", "user-sync")]
    assert [call for call in calls if call[0] == "publish"] == [
        ("publish", "user-sync"), ("publish", "user-sync")
    ]


def test_pending_actions_running_ask_preview_collapses_whitespace(repo):
    """多行提问(粘贴进来的那种)先归一空白再截断,摘要里全是内容字符。

    铃铛条目是**单行**呈现:换行、缩进、连续空格既在 60 码点的额度里白占位置,
    又会在那一行里折成一段可疑的空隙。归一必须在截断**之前**,否则前 60 个码点里
    有多少是空白全看用户怎么排版。
    """
    from app.repositories.pending_action_rows import ASK_QUESTION_PREVIEW_CHARS

    nb = _seed_user_nb(repo, "user-a")
    multiline = "第一行问题\n\n   第二行   接着问\t还有制表符\n" + "尾" * 80
    _insert_ask_job(repo, "askjob-ml", nb, "user-a", question=multiline)

    item = [it for it in repo.pending_actions("user-a")["items"]
            if it["type"] == "ask"][0]
    assert "\n" not in item["title"] and "\t" not in item["title"]
    assert "  " not in item["title"]
    assert item["title"].startswith("第一行问题 第二行 接着问 还有制表符 尾")
    # 归一后仍按码点截断,且额度全部用在内容上。
    assert len(item["title"]) == ASK_QUESTION_PREVIEW_CHARS
    assert item["title"] == " ".join(multiline.split())[:ASK_QUESTION_PREVIEW_CHARS]


def test_sync_ask_costs_nothing_when_nobody_is_watching_the_bell(monkeypatch):
    """同步路径的两次推送在**没人订阅时**一次库都不查。

    这里刻意先 `bind_loop`:`bind_loop` 是进程级、绑定后不解绑,所以生产里只要有一个
    人打开过铃铛,`mark_dirty` 原有的"loop is None 就返回"对所有其他用户就永久失效
    了——这条用例要钉的正是 loop 已绑定之后的那种形态,per-user 的订阅闸把重算挡在
    外面。用真的 `publish_snapshot` 与真的总线(不打桩),否则钉不到这条通路。
    """
    import asyncio
    from types import SimpleNamespace
    from app.services.pending_bus import pending_bus

    recomputed: list = []
    monkeypatch.setattr(
        pending_bus, "_recompute",
        lambda uid: recomputed.append(uid) or {"count": 0, "items": []},
    )
    async def _open_the_bell_once():
        pending_bus.bind_loop()

    asyncio.run(_open_the_bell_once())   # 有人打开过铃铛 → loop 已绑定且不再解绑
    assert pending_bus.has_subscribers("user-sync") is False

    calls: list = []
    fake = _FakeSyncAsk(calls, response=SimpleNamespace(answer_id="ans-sync"))
    import app.services.ask_service as ask_service_module
    ask_service_module.AskService.ask_current(fake, "nb-1", SimpleNamespace(mode="chunk"))

    assert recomputed == [], "铃铛没打开,同步提问不该为快照查一次库"
    assert [c[0] for c in calls] == ["begin", "ask", "finish"]


def test_sync_ask_publishes_on_the_request_thread_so_the_two_snapshots_cannot_invert(
    monkeypatch,
):
    """两次推送都在请求线程上,因此起点那帧一定先于终点那帧送达。

    快照是绝对值不是增量,谁最后到就是谁说了算;而重算跑在**调用线程**上,投递顺序
    等于重算完成顺序。把这两次各扔进一条新后台线程,快速失败的提问(校验不过/模型未
    配置)两次提交只隔毫秒,起点那帧(含「进行中」条目)就可能后到,在铃铛上留下一条
    永不消失的假进行中项。要挪出请求线程,得先有一条按 user 串行的发布通道——这条
    用例就是那个前提的守卫,不是在庆祝"在请求线程上跑"本身。
    """
    import threading
    from types import SimpleNamespace
    import app.services.ask_service as ask_service_module

    threads: list = []
    monkeypatch.setattr(
        ask_service_module, "publish_snapshot",
        lambda user_id: threads.append(threading.get_ident()),
    )
    fake = _FakeSyncAsk([], response=SimpleNamespace(answer_id="ans-sync"))
    ask_service_module.AskService.ask_current(fake, "nb-1", SimpleNamespace(mode="chunk"))

    assert threads == [threading.get_ident(), threading.get_ident()]
