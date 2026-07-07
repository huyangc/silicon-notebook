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
        # 干扰项:非 outline_ready、他人的报告 —— 都不该出现
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r2", nb, "x", "generating", "user-a", "2026-07-07T01:00:00", "2026-07-07T01:00:00"),
        )
    out = repo.pending_actions("user-a")
    items = [it for it in out["items"] if it["type"] == "report_outline"]
    assert len(items) == 1
    assert items[0]["report_id"] == "r1"
    assert items[0]["notebook_id"] == nb
    assert items[0]["title"]  # question 截断非空
    assert out["count"] == 1


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


def test_pending_actions_index_state(repo, monkeypatch):
    """索引态走 scale_index_status().state;用 monkeypatch 覆盖真实态,
    避免真造 stale/building 场景(需真实磁盘 manifest/后台线程)。"""
    nb = _seed_user_nb(repo, "user-a")

    def _fake_status(notebook_id):
        assert notebook_id == nb
        return {"state": "stale", "total_chunks": 100, "delta_chunks": 40}

    monkeypatch.setattr(repo, "scale_index_status", _fake_status)
    out = repo.pending_actions("user-a")
    idx_items = [it for it in out["items"] if it["type"] == "index"]
    assert len(idx_items) == 1
    assert idx_items[0]["state"] == "stale"
    assert idx_items[0]["notebook_id"] == nb
    assert out["count"] == 1


def test_pending_actions_index_building_not_counted(repo, monkeypatch):
    """building/queued 不计入 count(不是"待用户确认"的动作项)。"""
    nb = _seed_user_nb(repo, "user-a")
    monkeypatch.setattr(repo, "scale_index_status",
                         lambda notebook_id: {"state": "building", "total_chunks": 100, "delta_chunks": 40})
    out = repo.pending_actions("user-a")
    idx_items = [it for it in out["items"] if it["type"] == "index"]
    assert len(idx_items) == 1
    assert idx_items[0]["state"] == "building"
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
