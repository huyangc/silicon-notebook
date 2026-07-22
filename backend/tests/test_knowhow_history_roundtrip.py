"""往返不变量：任意混合变更序列，逐点回退都必须精确复原。

这是 delta 方案正确性的唯一有效证明。用固定种子的伪随机序列而不是
手写几个用例——手写的用例只会覆盖作者想得到的组合，而漏挂钩/payload
存不全的 bug 恰恰藏在想不到的组合里（例如"删了列又删了行再回退"）。

上一轮评审已经证明：随机序列若不含列改名/改内容类型/anchor 移动这三种
操作，旧实现 5/5 通过；含了这三种，4/5 失败——它们是这套逻辑最容易出
错的地方（``_revert_payload`` 的"两侧快照做差"对列定义字段最敏感）。
``_mutate`` 必须保留这三种分支，不能为了"看着简单"而精简掉。
"""
from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.sqlite import knowhow_fingerprint
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    return SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path}/knowhow.db",
            storage_dir=str(tmp_path / "storage"),
        )
    )


@pytest.fixture(autouse=True)
def _advancing_clock(monkeypatch):
    """把 ``_now()``（``app.services.sqlite_repository._now``，``KnowhowStore``/
    ``KnowhowHistoryStore`` 唯一的时间戳来源）换成每次调用严格递增一秒的假钟。

    不这么做的话，真实 ``_now()`` 会 ``.replace(microsecond=0)`` 截断到整秒
    （见该函数定义），而这条测试的"30 步演化 + 31 次回退"全程在同一个进程内
    跑完只需要几百毫秒——同一个 seed 的全部时间戳几乎必然落在同一个墙钟秒
    里，"真实创建时刻"和"回退重建时刻"两个本该不同的值会**凑巧撞在一起**，
    让任何基于 created_at 的比较失去区分力（变异验证过：把 ``_rebuild_row``
    改回 Task 8 P3 修复前的样子——重建时永远盖 ``now()``、不取 payload 里存的
    原始 created_at——在真实时钟下这条测试仍然 5/5 全绿，见
    ``.superpowers/sdd/task-9-report.md`` 的"MUTATION-C"一节）。换成严格递增
    的假钟后同一处改动会稳定地被抓到（红），本 fixture 因此是测试本身"确实
    在验证 created_at"这件事的必要前提，不是装饰。

    只换时钟来源，不改任何生产代码路径——``monkeypatch`` 在每个测试用例
    结束时自动复原，参数化的每个 seed 各有一份独立实例，符合"不用进程级
    共享状态"的并行安全要求（本文件其余部分同理全靠 ``tmp_path`` 隔离）。
    """
    import app.services.sqlite_repository as repo_mod

    start = datetime(2020, 1, 1)
    counter = {"n": 0}

    def fake_now() -> str:
        counter["n"] += 1
        return (start + timedelta(seconds=counter["n"])).isoformat()

    monkeypatch.setattr(repo_mod, "_now", fake_now)


def _fp(repo, table_id):
    with repo._runtime.database.connect() as db:
        return knowhow_fingerprint.fingerprint_on(db, table_id)


def _snapshot(repo, table_id):
    return repo._runtime.knowhow_transfer_store.snapshot_table(table_id)


def _mutate(rng, store, table_id, state) -> str:
    """随机施加一次变更。所有分支都必须是"用户真能做到"的操作。

    返回本次选中的 ``action`` 名字——纯粹为了让调用方（本文件末尾的
    coverage 断言、以及 task-9-report.md 的操作分布统计脚本）能不重新
    实现一遍选择逻辑就核对覆盖面；不影响任何分支的行为。
    """
    detail = store.get_knowhow_table(table_id)
    columns = [c for c in detail["columns"]]
    rows = [r for r in detail["rows"]]
    choices = ["add_row", "add_column", "meta", "rename_column"]
    if rows and columns:
        choices += ["cell", "cell", "cell", "code"]
    if len(rows) > 1:
        choices.append("delete_row")
    if len(columns) > 2:
        choices.append("delete_column")
    if len(columns) > 1:
        choices += ["kind", "anchor"]

    action = rng.choice(choices)
    if action == "add_row":
        store.add_knowhow_row(table_id, {columns[0]["id"]: f"值{rng.randint(0, 99)}"})
    elif action == "add_column":
        state["col_n"] += 1
        store.add_knowhow_column(table_id, f"列{state['col_n']}", "attribute")
    elif action == "meta":
        store.update_knowhow_table_meta(table_id, title=f"标题{rng.randint(0, 99)}")
    elif action == "rename_column":
        state["col_n"] += 1
        store.rename_knowhow_column(rng.choice(columns)["id"], f"改名{state['col_n']}")
    elif action == "cell":
        store.update_knowhow_cell(
            rng.choice(rows)["id"], rng.choice(columns)["id"], f"内容{rng.randint(0, 999)}"
        )
    elif action == "code":
        store.upsert_knowhow_cell_code(
            rng.choice(rows)["id"], rng.choice(columns)["id"],
            f"print({rng.randint(0, 9)})", "python", "u", f"h{rng.randint(0, 9)}",
        )
    elif action == "delete_row":
        store.delete_knowhow_row(rng.choice(rows)["id"])
    elif action == "delete_column":
        non_anchor = [c for c in columns if c["role"] != "anchor"]
        store.delete_knowhow_column(rng.choice(non_anchor)["id"])
    elif action == "kind":
        non_anchor = [c for c in columns if c["role"] != "anchor"]
        store.set_knowhow_column_kind(
            rng.choice(non_anchor)["id"], rng.choice(["procedure", "entity", "attribute"])
        )
    elif action == "anchor":
        store.set_knowhow_anchor_column(table_id, rng.choice(columns)["id"])
    return action


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_every_history_point_is_exactly_reachable_by_revert(repo, seed):
    rng = random.Random(seed)
    store = repo._runtime.knowhow_store
    hist = repo._runtime.knowhow_history_store
    notebook_id = repo.create_notebook(
        NotebookCreate(name="t", purpose="p", primary_domain="d")
    ).id
    table_id = store.create_knowhow_table(
        notebook_id, "表", "",
        [{"name": "概念", "role": "anchor"}, {"name": "做法", "role": "attribute"}],
    )

    # 阶段一：随机演化，记下每个点的指纹与快照
    checkpoints: list[tuple[int, str, dict]] = [
        (hist.head_seq(table_id), _fp(repo, table_id), _snapshot(repo, table_id))
    ]
    state = {"col_n": 0}
    actions: list[str] = []
    for _ in range(30):
        actions.append(_mutate(rng, store, table_id, state))
        checkpoints.append(
            (hist.head_seq(table_id), _fp(repo, table_id), _snapshot(repo, table_id))
        )

    # 覆盖面守卫：这条测试存在的全部意义就是压中"列改名/改内容类型/anchor
    # 移动"这三种最容易出 bug 的操作（见上一轮评审：不含它们旧实现 5/5
    # 通过，含了 4/5 失败）。30 步 × 固定种子理论上总会摇到，但如果种子
    # 分布不巧一次都没摇到,测试会在"没测到关键路径"的情况下假装通过——
    # 显式断言，摇不到就地报错而不是悄悄放过。
    hit = Counter(actions)
    for kind in ("rename_column", "kind", "anchor"):
        assert hit[kind] > 0, (
            f"seed={seed} 30 步随机演化里一次都没摇到 {kind}——"
            f"这条测试的核心覆盖面落空了（实际分布：{dict(hit)}）"
        )

    # 阶段二：从最新逐点回退，每一步都要精确落在当时的状态上
    for seq, want_fp, want_snapshot in reversed(checkpoints):
        hist.revert_to(table_id, seq, hist.head_seq(table_id))
        assert _fp(repo, table_id) == want_fp, f"seed={seed} seq={seq} 指纹不符"
        got = _snapshot(repo, table_id)
        for key in ("columns", "rows", "cells", "cell_code"):
            assert _normalize(got[key], key) == _normalize(want_snapshot[key], key), (
                f"seed={seed} seq={seq} 的 {key} 与当时不一致"
            )


def _normalize(rows, kind):
    """快照比较要忽略"回退必然产生新值/新代理键"的字段——但忽略集合按
    ``kind``（``columns``/``rows``/``cells``/``cell_code``）区分，不能对
    四类都用同一个忽略集合：

    - ``updated_at``：四类都忽略。回退是"逆序重放、每步都重新写值"，每次
      写入都会盖一次新时间戳；``knowhow_fingerprint.FINGERPRINT_SQL`` 本来
      就不覆盖它（见该模块顶部注释：「不含 updated_at 时间戳」），如果这里
      拿它去比较，回退功能本身永远过不了这条测试。

    - ``created_at``：
        * ``rows`` **不忽略**——上一个任务（Task 8 修复轮 P3）刚把
          ``row_add``/``row_delete`` 的 payload 补上行的真实 ``created_at``，
          ``_rebuild_row`` 现在原样恢复它（不再盖 ``now()``）。这条往返测试
          存在的目的之一就是守住这个修复不被回归，所以行快照必须真的比较
          它，不能像最初草稿那样一并忽略掉。
        * ``cell_code`` **忽略**——这是已登记、暂不修的已知缺口：一个代码
          附件所在的行/列被整个删除、又被回退重建时，宿主行/列的 DELETE
          带着 ``ON DELETE CASCADE`` 把旧的 ``knowhow_cell_code`` 行也删了，
          重建时 ``_write_cell_code``/``_rebuild_row``/``_rebuild_column``
          走的是全新 INSERT（不是 ``ON CONFLICT DO UPDATE`` 分支），
          ``created_at`` 必然盖成重建那一刻的 ``now()``，不是真实创建时间。
          这个不对称是**有意的**：不忽略就是拿一个已知且暂不修的缺口去断言
          "必须不变"，产出假失败而不是回退引擎的真 bug。
        * ``columns``/``cells`` 两张表压根没有 ``created_at`` 列（见
          ``migrations.py`` 里 ``knowhow_columns``/``knowhow_cells`` 的
          ``CREATE TABLE``），忽略与否都不影响比较结果，写在忽略集合里只是
          防御性的、不依赖这条实现细节。

    - ``id``（仅 ``cells``/``cell_code``）：这两张表的代理主键在设计上就是
      可再生的——``knowhow_fingerprint.py`` 的 ``FINGERPRINT_SQL`` 注释原话
      "the surrogate id columns on knowhow_cells/knowhow_cell_code ... their
      real identity is the (row_id, column_id) pair"。``_write_cell``/
      ``_write_cell_code``/``upsert_knowhow_cell_code`` 都是 ``INSERT ...
      ON CONFLICT(row_id, column_id) DO UPDATE``：同一 ``(row_id,
      column_id)`` 原地更新会保留旧 id，但那一行的宿主行/列被删再重建时，
      CASCADE 先删光旧格子/代码附件行，重建时走的是全新 INSERT，必然发一个
      新 id——30 步序列里只要出现一次 ``delete_row``/``delete_column``，回退
      经过那一步就会触发，不是偶发角落情形。比较它就是拿一个"设计上允许
      变"的字段断言"必须不变"，产出假失败。``columns``/``rows`` 的 ``id``
      不在此列——``_rebuild_row``/``_rebuild_column`` 原样复用 payload 里存
      的旧 id（这两个方法自己的文档字符串："id 绝不能换新"），这两类的 id
      理应精确不变，纳入比较才有意义（能抓住"重建时意外换了新 id"这类真实
      回归，而不是被规避掉）。
    """
    ignored = {"updated_at", "created_at"}
    if kind == "rows":
        ignored = {"updated_at"}
    if kind in ("cells", "cell_code"):
        ignored = ignored | {"id"}
    return sorted(
        (tuple(sorted((k, v) for k, v in dict(r).items() if k not in ignored))
         for r in rows),
        key=repr,
    )
