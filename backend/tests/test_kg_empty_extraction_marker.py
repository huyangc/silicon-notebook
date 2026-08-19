"""「已分析、但这篇里没有可整理的知识」这一态的判据,以及它的四处消费点必须同口径。

背景(现场):上传一批几乎全是图片、正文只有几百字的 PDF,抽取合法地产出 0 个知识
对象。判据在这次改动之前只有一条 ``NOT EXISTS(knowledge_objects)``,于是这些来源
永远算「待分析」——来源徽标一直显示待分析、看板一直显示「1 项内容未完成,继续
分析」、每点一次「分析新增」就把它们重新分析一遍再得到零。用户读到的是「一直分析
不成功」,而其实每一轮都成功了。

判据的**权威表述**是 ``app.models.sources.kg_analyzed_without_objects``。它有四个
消费点,两个在 Python(来源投影、构建目标选择)、两个必须是 SQL(pending 计数是一条
COUNT,逐行拉回 Python 判就是 N+1)。本文件钉三件事:

1. 写者与读者不脱钩——``run_extraction`` 成功路径写的那条消息,n_obj=0 时确实满足
   判据,n_obj>0 时确实不满足;
2. 判据本身的分界(尤其 ``no-llm`` 与「有失败窗口」两个**不算**已分析的反例);
3. SQLite 的 SQL 镜像与 Python 判据**在真库上逐用例一致**——这是本文件的主证,
   因为那条 SQL 是手抄的方言镜像,漂了没有任何东西会报错,只会让计数悄悄错。
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.models.sources import (
    KG_EMPTY_RUN_MESSAGE_PREFIX,
    KG_RUN_MESSAGE_OBJECTS_PREFIX,
    kg_analyzed_without_objects,
)
from app.services.sqlite_repository import SQLiteRepository


def _run_message(n_obj: int, *, failed_windows: int = 0, total_windows: int = 3) -> str:
    """复刻 ``run_extraction`` 成功路径那条 f-string 的形状(见 source_ingestion.py)。

    刻意手写而不是 import 一个共享的 builder:本测试要证的正是「那条 f-string 的产物
    满足判据」。若两边共用同一个 builder,这条断言就退化成同义反复。改了那条消息的
    形状而没改这里,应当**在这里**红——那正是想要的行为。
    """
    return (
        f"{KG_RUN_MESSAGE_OBJECTS_PREFIX}{n_obj} relations=0 doc_type=academic "
        f"windows_failed={failed_windows}/{total_windows} windows_skipped=0 "
        "concepts_dropped=0 claims_dropped=0 completion_mode=off completion_inserted=0"
    )


# (标签, status, error_message, 期望判定)。SQL 镜像与 Python 判据吃同一张表。
CASES = (
    ("零产出的成功抽取", "completed", _run_message(0), True),
    ("抽出了对象", "completed", _run_message(4), False),
    # 零产出但有失败窗口:零可能只是因为那些窗口丢了,不能据此说「这篇没有知识」。
    ("零产出但有失败窗口", "completed", _run_message(0, failed_windows=2), False),
    ("有产出且有失败窗口", "completed", _run_message(3, failed_windows=1), False),
    # 模型没配 → 确实还没被分析过,配好之后「分析新增」必须把它捡回来。
    ("模型未配置", "completed", "no-llm", False),
    ("抽取失败", "failed", "RuntimeError: upstream timeout", False),
    ("还在跑", "running", "", False),
    ("没有任何抽取记录", "", "", False),
    (
        "partial 重试的半成品",
        "completed",
        "partial KG retry incomplete; existing KG preserved retry_incomplete=1 "
        "windows_failed=0/2 empty_result=1 candidate_objects=0 candidate_relations=0",
        False,
    ),
)


@pytest.mark.parametrize("label,status,message,expected", CASES, ids=[c[0] for c in CASES])
def test_predicate_separates_analyzed_empty_from_never_analyzed(
    label, status, message, expected
):
    assert kg_analyzed_without_objects(status, message) is expected, label


def test_zero_object_message_carries_the_shared_prefix():
    """写者与读者的唯一联系点。前缀常量改了、或那条消息不再以对象数起头,这里红。"""
    assert _run_message(0).startswith(KG_EMPTY_RUN_MESSAGE_PREFIX)
    assert not _run_message(1).startswith(KG_EMPTY_RUN_MESSAGE_PREFIX)
    # 位数不能糊弄前缀:objects=0 与 objects=01/objects=10 必须分得开。
    assert not _run_message(10).startswith(KG_EMPTY_RUN_MESSAGE_PREFIX)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def _seed_source(repo, notebook_id: str, source_id: str, status: str, message: str):
    """一条**已解析**(有 elements)、**没有任何知识对象**的来源 + 它最近一次抽取记录。

    没有知识对象是本测试的前提:有对象的来源本来就不算 pending,走不到新判据。
    """
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "parse_status,created_at,updated_at) "
            "VALUES (?,?,?,'pdf','extracted','extracted','2026-01-01','2026-01-01')",
            (source_id, notebook_id, source_id),
        )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,created_at) VALUES (?,?,'paragraph','p.1','x','2026-01-01')",
            (f"el-{source_id}", source_id),
        )
        if status:
            db.execute(
                "INSERT INTO extraction_runs (id,notebook_id,source_id,run_type,"
                "status,error_message,created_at,updated_at) "
                "VALUES (?,?,?,'kg',?,?,'2026-01-01','2026-01-01')",
                (f"run-{source_id}", notebook_id, source_id, status, message),
            )


def test_sqlite_pending_count_sql_agrees_with_the_python_predicate(repo):
    """主证:SQL 镜像 ⇔ Python 判据,在真库上逐用例对账。

    每个用例一条来源;SQL 数出来的 pending 数必须恰好等于「判据说**不是**已分析的
    那几条」。任一方向漂了(SQL 少排除 → 计数偏高、来源永远待分析;SQL 多排除 →
    真正没分析过的来源被静默跳过、再也不会被分析)都会在这里红。
    """
    from app.repositories.sqlite import knowledge_counts_cache as kcc

    notebook = repo.create_notebook(NotebookCreate(name="零产出"))
    for index, (_label, status, message, _expected) in enumerate(CASES):
        _seed_source(repo, notebook.id, f"src-{index}", status, message)

    expected_pending = sum(
        0 if kg_analyzed_without_objects(status, message) else 1
        for _label, status, message, _expected in CASES
    )
    with repo._runtime.database.connect() as db:
        counted = kcc._pending_source_count_query(db, notebook.id, visible_only=True)
    assert counted == expected_pending

    # 且这个数确实小于「全部来源」——否则上面的相等可能是两边一起没排除任何东西。
    assert 0 < counted < len(CASES)


def test_sqlite_build_targets_skip_analyzed_empty_sources(repo):
    """构建目标选择:零产出来源不再被每一轮「分析新增」重新选中(那是白付模型钱)。

    同时钉住反向——``no-llm``(模型没配)与失败窗口的来源仍然是目标:它们确实还没被
    成功分析过,把它们一起跳过就是让一批来源永远进不了图谱。
    """
    notebook = repo.create_notebook(NotebookCreate(name="目标选择"))
    for index, (_label, status, message, _expected) in enumerate(CASES):
        _seed_source(repo, notebook.id, f"src-{index}", status, message)

    lifecycle = repo._runtime.knowledge_lifecycle
    selected: set[str] = set()
    for targets, _skipped, _missing in lifecycle._kg_target_batches(
        notebook.id, mode="incremental"
    ):
        selected.update(source_id for source_id, _preserve in targets)

    for index, (label, status, message, _expected) in enumerate(CASES):
        analyzed_empty = kg_analyzed_without_objects(status, message)
        assert (f"src-{index}" in selected) is not analyzed_empty, label


def test_rebuild_mode_still_picks_up_analyzed_empty_sources(repo):
    """跳过只作用于增量模式。换了模型、或带 OCR 重新解析之后要把这批来源捡回来,
    走的就是整库重建这条路——它必须仍然选中它们,否则这个特性就变成了永久拉黑。"""
    notebook = repo.create_notebook(NotebookCreate(name="重建"))
    _seed_source(repo, notebook.id, "src-empty", "completed", _run_message(0))

    lifecycle = repo._runtime.knowledge_lifecycle
    selected = {
        source_id
        for targets, _skipped, _missing in lifecycle._kg_target_batches(
            notebook.id, mode="rebuild"
        )
        for source_id, _preserve in targets
    }
    assert selected == {"src-empty"}


def test_source_projection_reports_the_third_state(repo):
    """来源徽标的三态:两个布尔互斥,且零产出来源不再谎称「待分析」。"""
    notebook = repo.create_notebook(NotebookCreate(name="投影"))
    _seed_source(repo, notebook.id, "src-empty", "completed", _run_message(0))
    _seed_source(repo, notebook.id, "src-never", "completed", "no-llm")

    by_id = {item.id: item for item in repo.list_sources(notebook.id)}

    empty = by_id["src-empty"]
    assert (empty.kg_extracted, empty.kg_analyzed_empty) == (False, True)

    never = by_id["src-never"]
    assert (never.kg_extracted, never.kg_analyzed_empty) == (False, False)

    # 单条路径(get_source)与批量路径(list_sources)必须给出同一个答案:两处各有一份
    # 取数代码,漂了会让同一份来源在列表和详情里显示成两种状态。
    for source_id in ("src-empty", "src-never"):
        detail = repo.get_source(source_id)
        assert detail.kg_analyzed_empty == by_id[source_id].kg_analyzed_empty
        assert detail.kg_extracted == by_id[source_id].kg_extracted
