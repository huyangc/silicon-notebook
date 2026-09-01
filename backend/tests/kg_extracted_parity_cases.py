"""``SourceSummary.kg_extracted`` 的判定矩阵 —— **两端吃同一张表**。

沿用 ``activity_parity_cases.py`` / ``agent_observation_parity_cases.py`` 的理由:
判据本身在 SQLite 与 PostgreSQL 各有一份手抄的方言 SQL(正则 vs GLOB、``rowid``
vs ``ordinal``),两边各自造一套用例,就等于让每一端的夹具去迎合自己那份实现——
其中一端漏掉某个分支时不会有任何东西变红。这里把「什么样的数据应该算已抽取」
写成一张与后端无关的表,``tests/test_sources_page_batched.py``(SQLite)与
``tests/postgres/test_core_store_conformance.py``(PostgreSQL)各自 import 它。

判据(权威表述是两端 ``sources_from_rows`` / ``source_from_row`` 里那条 SQL):

1. 该 source 至少有一行 ``knowledge_objects``(且 ``source_id`` 非空串);**并且**
2. 最近一次 ``run_type='kg'`` 的 ``extraction_runs`` 记录 ``status='completed'``
   ——没有任何记录时按 ``'completed'`` 兜底(直连/治理路径写进来的兼容行);**并且**
3. 那条记录的 ``error_message`` 里既没有「有失败窗口」标记
   (``windows_failed=N/T``,N≥1),也没有 ``retry_incomplete=1``(partial 重试的
   半成品)。

「最近一次」在两端都是 ``ORDER BY created_at DESC`` 加各自的行序 tie-break,所以
用例里的 run 用**时序序号**而不是具体时间戳表达,由各端映射到自己的时间格式。

不是测试模块(没有 ``test_`` 前缀),pytest 不会收集它;两个测试文件 import。
"""
from __future__ import annotations

#: 一条 extraction_runs 记录:``(时序序号, status, error_message)``。
#: 序号大 = 更晚,由各端翻译成自己的 ``created_at`` 字面量。
RunSpec = tuple[int, str, str]

#: ``(标签, 是否有 knowledge_objects 行, runs, 期望的 kg_extracted)``。
#: 覆盖判定的每一条分支,含「多条 run 时以最近一次为准」的两个方向。
KG_EXTRACTED_CASES: tuple[tuple[str, bool, tuple[RunSpec, ...], bool], ...] = (
    # (a) 正常的已抽取:有对象、最近一次跑完、消息里没有任何降级标记。
    ("有对象且最近一次干净完成", True, ((0, "completed", "kg objects=3 relations=1"),), True),
    # (b) 有失败窗口 → 这一篇的图谱是残的,不能显示成已抽取。
    (
        "最近一次有失败窗口",
        True,
        ((0, "completed", "kg objects=3 windows_failed=2/5"),),
        False,
    ),
    # windows_failed=0/N 是**干净**的完成记录,不是降级——正则/GLOB 的 [1-9] 就是为它写的。
    (
        "失败窗口数为零不算降级",
        True,
        ((0, "completed", "kg objects=3 windows_failed=0/5"),),
        True,
    ),
    # (c) partial 重试没补齐:留下的是上一轮的旧图谱,同样不算已抽取。
    (
        "最近一次是没补齐的 partial 重试",
        True,
        (
            (
                0,
                "completed",
                "partial KG retry incomplete; existing KG preserved "
                "retry_incomplete=1 windows_failed=0/2 empty_result=1",
            ),
        ),
        False,
    ),
    # (d) 最近一次没有 completed:失败/在跑,残留的对象行不构成一份完整图谱。
    ("最近一次失败", True, ((0, "failed", "RuntimeError: upstream timeout"),), False),
    ("最近一次还在跑", True, ((0, "running", ""),), False),
    # (e) 完全没有抽取记录 → COALESCE 兜底成 'completed',有对象就算已抽取。
    ("没有任何抽取记录", True, (), True),
    # (f) 没有对象行 → 无论 run 多干净都不算已抽取。
    ("没有对象行", False, ((0, "completed", "kg objects=0"),), False),
    # (g) 多条 run:判定只看最近一次,两个方向都要钉。
    (
        "多条 run:旧的坏、新的好",
        True,
        (
            (0, "failed", "windows_failed=1/2"),
            (1, "completed", "kg objects=3"),
        ),
        True,
    ),
    (
        "多条 run:旧的好、新的坏",
        True,
        (
            (0, "completed", "kg objects=3"),
            (1, "completed", "kg objects=3 windows_failed=1/2"),
        ),
        False,
    ),
)

#: pytest 参数化用的 ids,以及「第 index 条用例的 source_id」这一两端共用的命名。
KG_EXTRACTED_CASE_IDS: tuple[str, ...] = tuple(case[0] for case in KG_EXTRACTED_CASES)


def kg_case_source_id(index: int) -> str:
    """第 ``index`` 条用例的 source_id。两端用同一套 id,便于比对失败输出。"""
    return f"src-kgx-{index:02d}"


def kg_case_run_id(index: int, rank: int) -> str:
    """第 ``index`` 条用例、时序序号 ``rank`` 的那条 extraction_runs 记录 id。"""
    return f"run-kgx-{index:02d}-{rank}"
