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

「最近一次」在两端都是 ``ORDER BY created_at DESC`` 加各自的行序 tie-break(PG
``ordinal DESC``、SQLite ``rowid DESC``,两者都是插入序),所以每条用例里的 run 用
**时序序号**(生成 ``created_at`` 字面量,允许重复以制造同刻场景)表达时间,由各端
映射到自己的时间格式;run id 后缀与插入顺序则由这条 run 在 ``runs`` 元组里的**位置**
决定(两端 seeder 都按元组顺序逐条插入,位置即插入序)——这两个职责刻意分开:同一张
时序序号若还要兼任 run id 后缀,两条同刻 run 会生成同一个 id 而在插入时主键冲突。

不是测试模块(没有 ``test_`` 前缀),pytest 不会收集它;两个测试文件 import。
"""
from __future__ import annotations

#: 一条 extraction_runs 记录:``(时序序号, status, error_message)``。时序序号只负责
#: 生成 ``created_at`` 字面量,可以在多条记录间重复(制造「同刻」场景);它不再兼任
#: run id 后缀或插入序——那两者由这条记录在所属 ``runs`` 元组里的**位置**决定,见
#: ``kg_case_run_id`` 与两端 seeder(``enumerate(runs)``)。
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
    # (h) tie-break:两条 run **created_at 相同**(时序序号都是 0),先插入的干净
    # completed、后插入的 failed —— 「最近一次」在两端都是 created_at DESC 之后再按
    # 插入序 DESC(PG ordinal、SQLite rowid)break tie,所以后插入的那条(failed)赢,
    # 覆盖判定应为 False。这条用例专门钉住「插入序 tie-break」这一分支:此前的用例
    # 里时序序号与插入位置永远相等,从未表达过「created_at 打平,只能靠插入序分胜负」
    # 的场景。
    (
        "同刻两条 run:插入序决定胜者(后插入的 failed 赢)",
        True,
        (
            (0, "completed", "kg objects=3"),
            (0, "failed", "RuntimeError: upstream timeout"),
        ),
        False,
    ),
)


def kg_case_source_id(index: int) -> str:
    """第 ``index`` 条用例的 source_id。两端用同一套 id,便于比对失败输出。"""
    return f"src-kgx-{index:02d}"


def kg_case_run_id(index: int, position: int) -> str:
    """第 ``index`` 条用例、``runs`` 元组里第 ``position`` 条(0-based,即插入序)
    的那条 extraction_runs 记录 id。``position`` 不是时序序号(RunSpec 的第一个字
    段)——两条 run 可以共享同一个时序序号(同刻场景),但位置始终唯一,id 也就始终
    唯一。"""
    return f"run-kgx-{index:02d}-{position}"
