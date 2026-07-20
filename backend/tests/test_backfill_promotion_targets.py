# backend/tests/test_backfill_promotion_targets.py
"""scripts/backfill_promotion_targets.py 的离线补救 CLI 测试。

背景: _migration_20 给 promotion_candidates 加 target_base_id 列(默认空串, 不回填)。
Task 7 之后, propose 侧强制解析 target_base_id, 但 Task 7 之前创建、仍处
proposed/under_review 的存量候选行永远补不上 —— 没有任何接口能改已存在候选的
target_base_id。本工具就是那条补救通路: 按「该候选所属 notebook 已挂载的公共知识库」
解析目标, 与 propose 侧 _resolve_promotion_target 同一条规则、同一个判定函数
(GovernanceStore.mounted_public_base_ids), 唯一挂载有效性谓词见 mount_sql.py。
"""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "backfill_promotion_targets.py"
_spec = importlib.util.spec_from_file_location("backfill_promotion_targets", _SCRIPT)
bpt = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["backfill_promotion_targets"] = bpt
_spec.loader.exec_module(bpt)

NOW = "2026-01-01T00:00:00"


def _fresh_db(path):
    """Fresh current-schema+seed db via the app repository (same pattern as test_merge_dbs.py)."""
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    repo = SQLiteRepository(Settings(database_url=f"sqlite:///{path}"))
    repo.close_local()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _add_notebook(conn, nb_id, tier, name="nb", created_by="user-local"):
    conn.execute(
        "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
        "created_at,updated_at,tier) VALUES(?,?,?,?,?,?,?,?,?)",
        (nb_id, name, "", "", "active", created_by, NOW, NOW, tier),
    )


def _mount(conn, notebook_id, base_notebook_id, created_by="user-local"):
    conn.execute(
        "INSERT INTO notebook_bases(notebook_id,base_notebook_id,created_at,created_by) "
        "VALUES(?,?,?,?)",
        (notebook_id, base_notebook_id, NOW, created_by),
    )


def _add_candidate(conn, cand_id, nb_id, object_id, status="proposed", target_base_id="",
                   object_type="concept"):
    conn.execute(
        "INSERT INTO promotion_candidates"
        "(id,notebook_id,object_id,object_type,status,reason,reviewed_by,base_match_id,"
        "created_at,updated_at,target_base_id) VALUES(?,?,?,?,?,'','','',?,?,?)",
        (cand_id, nb_id, object_id, object_type, status, NOW, NOW, target_base_id),
    )


# ---------------------------------------------------------------------------
# pending_rows(): status/target_base_id 过滤 + 未迁移库守卫
# ---------------------------------------------------------------------------

def test_require_migrated_raises_when_target_base_id_column_missing(tmp_path):
    """老库(未跑 _migration_20)promotion_candidates 没有 target_base_id 列 —— 必须
    fail-loud, 不能悄悄当成"没有待处理行"。"""
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE promotion_candidates (id TEXT PRIMARY KEY, notebook_id TEXT, "
        "object_id TEXT, object_type TEXT, status TEXT)"
    )
    conn.commit()
    with pytest.raises(SystemExit):
        bpt.pending_rows(conn)


def test_pending_rows_only_proposed_and_under_review_with_empty_target(tmp_path):
    conn = _fresh_db(tmp_path / "a.db")
    _add_notebook(conn, "nb-personal1", "personal", "P1")
    _add_notebook(conn, "nb-base1", "base", "Base One")
    _mount(conn, "nb-personal1", "nb-base1")
    _add_candidate(conn, "cand-1", "nb-personal1", "obj-1", status="proposed", target_base_id="")
    _add_candidate(conn, "cand-2", "nb-personal1", "obj-2", status="under_review", target_base_id="")
    _add_candidate(conn, "cand-3", "nb-personal1", "obj-3", status="proposed", target_base_id="nb-base1")
    _add_candidate(conn, "cand-4", "nb-personal1", "obj-4", status="approved", target_base_id="")
    _add_candidate(conn, "cand-5", "nb-personal1", "obj-5", status="rejected", target_base_id="")
    conn.commit()

    rows = bpt.pending_rows(conn)
    ids = {r["id"] for r in rows}
    assert ids == {"cand-1", "cand-2"}


# ---------------------------------------------------------------------------
# plan(): 解析规则(与 propose 侧 _resolve_promotion_target 对齐)
# ---------------------------------------------------------------------------

def test_plan_auto_resolves_single_mounted_public_base(tmp_path):
    conn = _fresh_db(tmp_path / "a.db")
    _add_notebook(conn, "nb-p1", "personal", "P1")
    _add_notebook(conn, "nb-base1", "base", "Base One")
    _mount(conn, "nb-p1", "nb-base1")
    _add_candidate(conn, "cand-1", "nb-p1", "obj-1")
    conn.commit()

    rows = bpt.pending_rows(conn)
    entries = bpt.plan(conn, rows, {})
    assert len(entries) == 1
    assert entries[0]["resolution"] == "auto"
    assert entries[0]["target_base_id"] == "nb-base1"


def test_plan_blocked_when_notebook_mounts_nothing(tmp_path):
    conn = _fresh_db(tmp_path / "a.db")
    _add_notebook(conn, "nb-p1", "personal", "P1")
    _add_candidate(conn, "cand-1", "nb-p1", "obj-1")
    conn.commit()

    rows = bpt.pending_rows(conn)
    entries = bpt.plan(conn, rows, {})
    assert entries[0]["resolution"] == "blocked_no_mount"
    assert entries[0]["target_base_id"] == ""


def test_plan_ambiguous_when_multiple_mounted_public_bases_without_override(tmp_path):
    conn = _fresh_db(tmp_path / "a.db")
    _add_notebook(conn, "nb-p1", "personal", "P1")
    _add_notebook(conn, "nb-base1", "base", "Base One")
    _add_notebook(conn, "nb-base2", "base", "Base Two")
    _mount(conn, "nb-p1", "nb-base1")
    _mount(conn, "nb-p1", "nb-base2")
    _add_candidate(conn, "cand-1", "nb-p1", "obj-1")
    conn.commit()

    rows = bpt.pending_rows(conn)
    entries = bpt.plan(conn, rows, {})
    assert entries[0]["resolution"] == "ambiguous"
    assert entries[0]["target_base_id"] == ""
    assert set(entries[0]["mounted"]) == {"nb-base1", "nb-base2"}


def test_plan_uses_explicit_override_for_ambiguous_notebook(tmp_path):
    conn = _fresh_db(tmp_path / "a.db")
    _add_notebook(conn, "nb-p1", "personal", "P1")
    _add_notebook(conn, "nb-base1", "base", "Base One")
    _add_notebook(conn, "nb-base2", "base", "Base Two")
    _mount(conn, "nb-p1", "nb-base1")
    _mount(conn, "nb-p1", "nb-base2")
    _add_candidate(conn, "cand-1", "nb-p1", "obj-1")
    conn.commit()

    rows = bpt.pending_rows(conn)
    entries = bpt.plan(conn, rows, {"nb-p1": "nb-base2"})
    assert entries[0]["resolution"] == "explicit"
    assert entries[0]["target_base_id"] == "nb-base2"


def test_plan_rejects_override_not_in_mounted_set_without_prior_validate(tmp_path):
    """纵深防御回归测试: plan() 是模块级公开函数, 不能只靠调用方先跑
    validate_overrides() 这道前置校验(见 main() 里"先 validate 后 plan"的调用顺序)。
    这里故意跳过 validate_overrides(), 直接拿一个不在该 notebook 挂载集合内的 override
    调 plan() —— 必须自己就拒绝, 不能把非法目标当成 explicit 解析悄悄写进结果。"""
    conn = _fresh_db(tmp_path / "a.db")
    _add_notebook(conn, "nb-p1", "personal", "P1")
    _add_notebook(conn, "nb-base1", "base", "Base One")
    _add_notebook(conn, "nb-base-unmounted", "base", "Unrelated Base")
    _mount(conn, "nb-p1", "nb-base1")
    _add_candidate(conn, "cand-1", "nb-p1", "obj-1")
    conn.commit()

    rows = bpt.pending_rows(conn)
    with pytest.raises(SystemExit):
        bpt.plan(conn, rows, {"nb-p1": "nb-base-unmounted"})


def test_plan_ignores_mount_edge_invalidated_by_demotion_and_different_owner(tmp_path):
    """回归测试(核实事实#2 补测): plan() 必须复用挂载有效性谓词的真实语义 ——
    一条指向"已被降级为 personal 且不同 owner"的挂载边不算已挂载,不能被当成
    唯一挂载而自动解析。这是防止有人手写一份简化版判定(只查 notebook_bases 存在
    与否, 漏掉 tier='base' OR same-owner 的有效性条件)的回归锚点。"""
    conn = _fresh_db(tmp_path / "a.db")
    _add_notebook(conn, "nb-p1", "personal", "P1", created_by="user-local")
    _add_notebook(conn, "nb-other", "personal", "Other's NB", created_by="someone-else")
    _mount(conn, "nb-p1", "nb-other")  # 挂载边还在,但目标已不是 base 且不同 owner -> 无效
    _add_candidate(conn, "cand-1", "nb-p1", "obj-1")
    conn.commit()

    rows = bpt.pending_rows(conn)
    entries = bpt.plan(conn, rows, {})
    assert entries[0]["resolution"] == "blocked_no_mount"
    assert entries[0]["mounted"] == []


# ---------------------------------------------------------------------------
# validate_overrides(): --set 必须落在挂载集合内, 非法立即整体拒绝
# ---------------------------------------------------------------------------

def test_validate_overrides_rejects_base_not_in_mounted_set(tmp_path):
    conn = _fresh_db(tmp_path / "a.db")
    _add_notebook(conn, "nb-p1", "personal", "P1")
    _add_notebook(conn, "nb-base1", "base", "Base One")
    _add_notebook(conn, "nb-base-unmounted", "base", "Unrelated Base")
    _mount(conn, "nb-p1", "nb-base1")
    _add_candidate(conn, "cand-1", "nb-p1", "obj-1")
    conn.commit()

    rows = bpt.pending_rows(conn)
    with pytest.raises(SystemExit):
        bpt.validate_overrides(conn, rows, {"nb-p1": "nb-base-unmounted"})


def test_validate_overrides_warns_but_does_not_raise_on_unmatched_notebook(tmp_path, capsys):
    conn = _fresh_db(tmp_path / "a.db")
    _add_notebook(conn, "nb-p1", "personal", "P1")
    _add_notebook(conn, "nb-base1", "base", "Base One")
    _mount(conn, "nb-p1", "nb-base1")
    _add_candidate(conn, "cand-1", "nb-p1", "obj-1")
    conn.commit()

    rows = bpt.pending_rows(conn)
    bpt.validate_overrides(conn, rows, {"nb-typo-does-not-exist": "nb-base1"})
    captured = capsys.readouterr()
    assert "nb-typo-does-not-exist" in captured.err


# ---------------------------------------------------------------------------
# apply_plan(): 只写可解析行, 阻塞/歧义行原样不动
# ---------------------------------------------------------------------------

def test_apply_plan_writes_only_resolved_rows(tmp_path):
    conn = _fresh_db(tmp_path / "a.db")
    _add_notebook(conn, "nb-auto", "personal", "Auto")
    _add_notebook(conn, "nb-blocked", "personal", "Blocked")
    _add_notebook(conn, "nb-base1", "base", "Base One")
    _mount(conn, "nb-auto", "nb-base1")
    _add_candidate(conn, "cand-auto", "nb-auto", "obj-1")
    _add_candidate(conn, "cand-blocked", "nb-blocked", "obj-2")
    conn.commit()

    rows = bpt.pending_rows(conn)
    entries = bpt.plan(conn, rows, {})
    n = bpt.apply_plan(conn, entries, "2026-07-19T00:00:00+00:00")
    conn.commit()

    assert n == 1
    got = {r["id"]: r["target_base_id"] for r in conn.execute(
        "SELECT id, target_base_id FROM promotion_candidates")}
    assert got["cand-auto"] == "nb-base1"
    assert got["cand-blocked"] == ""


# ---------------------------------------------------------------------------
# CLI end-to-end (main())
# ---------------------------------------------------------------------------

def _seed_mixed(tmp_path, name="a.db"):
    conn = _fresh_db(tmp_path / name)
    _add_notebook(conn, "nb-auto", "personal", "Auto")
    _add_notebook(conn, "nb-ambig", "personal", "Ambiguous")
    _add_notebook(conn, "nb-blocked", "personal", "Blocked")
    _add_notebook(conn, "nb-base1", "base", "Base One")
    _add_notebook(conn, "nb-base2", "base", "Base Two")
    _mount(conn, "nb-auto", "nb-base1")
    _mount(conn, "nb-ambig", "nb-base1")
    _mount(conn, "nb-ambig", "nb-base2")
    _add_candidate(conn, "cand-auto", "nb-auto", "obj-1")
    _add_candidate(conn, "cand-ambig", "nb-ambig", "obj-2")
    _add_candidate(conn, "cand-blocked", "nb-blocked", "obj-3")
    conn.commit()
    conn.close()
    return tmp_path / name


def test_cli_list_does_not_write(tmp_path, capsys):
    db_path = _seed_mixed(tmp_path)
    rc = bpt.main(["--db", str(db_path), "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cand-auto" in out and "cand-ambig" in out and "cand-blocked" in out

    conn = sqlite3.connect(db_path)
    targets = {r[0] for r in conn.execute(
        "SELECT target_base_id FROM promotion_candidates WHERE target_base_id != ''")}
    conn.close()
    assert targets == set()  # list 不写库


def test_cli_apply_resolves_unambiguous_rows_only(tmp_path):
    db_path = _seed_mixed(tmp_path)
    rc = bpt.main(["--db", str(db_path), "apply"])
    assert rc == 0

    conn = sqlite3.connect(db_path)
    got = {r[0]: r[1] for r in conn.execute(
        "SELECT id, target_base_id FROM promotion_candidates")}
    conn.close()
    assert got["cand-auto"] == "nb-base1"
    assert got["cand-ambig"] == ""       # 歧义, 没给 --set, 不动
    assert got["cand-blocked"] == ""     # 未挂载, 不动


def test_cli_apply_dry_run_writes_nothing(tmp_path):
    db_path = _seed_mixed(tmp_path)
    rc = bpt.main(["--db", str(db_path), "apply", "--dry-run"])
    assert rc == 0

    conn = sqlite3.connect(db_path)
    targets = {r[0] for r in conn.execute(
        "SELECT target_base_id FROM promotion_candidates WHERE target_base_id != ''")}
    conn.close()
    assert targets == set()


def test_cli_apply_with_set_resolves_ambiguous_notebook(tmp_path):
    db_path = _seed_mixed(tmp_path)
    rc = bpt.main(["--db", str(db_path), "apply", "--set", "nb-ambig=nb-base2"])
    assert rc == 0

    conn = sqlite3.connect(db_path)
    got = {r[0]: r[1] for r in conn.execute(
        "SELECT id, target_base_id FROM promotion_candidates")}
    conn.close()
    assert got["cand-ambig"] == "nb-base2"
    assert got["cand-auto"] == "nb-base1"      # 仍照常自动解析
    assert got["cand-blocked"] == ""


def test_cli_apply_rejects_set_pointing_outside_mounted_set_and_writes_nothing(tmp_path):
    """--set 给了一个该 notebook 没挂载的 base -> 整次运行在任何写入之前拒绝,
    包括同一次运行里其它本可正常解析的行也不能被写(all-or-nothing 校验前置)。"""
    db_path = _seed_mixed(tmp_path)
    with pytest.raises(SystemExit):
        bpt.main(["--db", str(db_path), "apply", "--set", "nb-ambig=nb-base-does-not-exist"])

    conn = sqlite3.connect(db_path)
    targets = {r[0] for r in conn.execute(
        "SELECT target_base_id FROM promotion_candidates WHERE target_base_id != ''")}
    conn.close()
    assert targets == set()  # 连 cand-auto 这种本该能自动解析的行也没被写


def test_cli_missing_db_file_errors(tmp_path):
    with pytest.raises(SystemExit):
        bpt.main(["--db", str(tmp_path / "does-not-exist.db"), "list"])


def test_cli_unmigrated_db_errors(tmp_path):
    p = tmp_path / "old.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE promotion_candidates (id TEXT PRIMARY KEY, notebook_id TEXT, "
        "object_id TEXT, object_type TEXT, status TEXT)"
    )
    conn.commit()
    conn.close()
    with pytest.raises(SystemExit):
        bpt.main(["--db", str(p), "list"])


def test_cli_list_rejects_set_flag(tmp_path):
    """--set 只在 apply 子命令定义; argparse 应在 list 下直接拒绝(不是运行期静默忽略)。"""
    db_path = _seed_mixed(tmp_path)
    with pytest.raises(SystemExit):
        bpt.main(["--db", str(db_path), "list", "--set", "nb-ambig=nb-base2"])
