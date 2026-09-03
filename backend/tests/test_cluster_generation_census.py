# backend/tests/test_cluster_generation_census.py
"""批 3·W2 §1.4:三张簇图派生表 SQL 站点的普查守卫(变更雷达)。

设计(docs/superpowers/specs/2026-09-03-batch3-w2-generational-cluster-swap-
design_zh.md §1.4)要求三分类普查:A·published 读者(加代次谓词)/
B·目标代写者(PR-2 参数化)/C·跨代维护(显式豁免+理由)。本守卫按
**逐文件表出现次数 + 代次谓词使用次数**钉住普查终态:

- 新增/移动一处 ``FROM|JOIN|INTO|UPDATE|DELETE FROM <表>`` 即计数漂移
  → 红,迫使实现者回到本清单登记分类并决定是否配谓词;
- 删掉某处谓词(计数下降)同样红——防「新查询忘加谓词」与「重构悄悄
  丢谓词」两个方向。

计数是文本级(docstring 里的示例 SQL 也计入)——本守卫是变更雷达,不做
语义判定;每条登记的 note 承载分类语义与豁免理由。C 类豁免清单非空是
设计的硬要求(per-source 清理必须跨代删,否则在飞代留死成员行,破
「零 orphan 生产者」不变量)。

**声明的盲区**(内评登记,由其它网兜):①动态表名清单(notebook_delete_
tables/KG_STATE_TABLES/_COPY_TABLES/kg_build_job_store 的表名字符串常量)
不进正则——它们全是 C 类整表维护,行为由各自套件钉;②同文件内把谓词从
LEFT JOIN 的 ON 挪进 WHERE 计数不变——由本文件的 ON 结构守卫单列检查;
③query_store 内层 `mc.generation = c.generation` 相关对齐无可计数 token
——由其行为测试(memory 排除语义)兜;④source_subgraph_projection 的局部
模板被两分支复用,删一个分支的引用计数不变——由该模块行为测试兜。
"""
from __future__ import annotations

import re
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
_REPO = _BACKEND.parent

_TABLE_PATTERNS = {
    "concept_clusters": re.compile(
        r"(?:FROM|JOIN|INTO|UPDATE|DELETE FROM)\s+(?:main\.|sec\.)?concept_clusters\b"
    ),
    "communities": re.compile(
        r"(?:FROM|JOIN|INTO|UPDATE|DELETE FROM)\s+(?:main\.|sec\.)?communities\b"
    ),
    "community_members": re.compile(
        r"(?:FROM|JOIN|INTO|UPDATE|DELETE FROM)\s+(?:main\.|sec\.)?community_members\b"
    ),
}


def _predicate_counts(text: str) -> tuple[int, int]:
    cluster = text.count("_PUBLISHED_CLUSTER_GEN") + text.count(
        "SELECT cluster_generation"
    )
    community = text.count("_PUBLISHED_COMMUNITY_GEN") + text.count(
        "SELECT community_generation"
    )
    return cluster, community


# {相对仓库根路径: (concept_clusters 出现, communities 出现, community_members
#  出现, cluster 谓词使用, community 谓词使用, 分类注记)}
_CENSUS: dict[str, tuple[int, int, int, int, int, str]] = {
    "backend/app/domain/kg_analysis_contracts.py": (3, 0, 0, 0, 0,
        "非站点:注释描述别处 SQL 的 JOIN 形状"),
    "backend/app/repositories/postgres/governance_store.py": (6, 0, 0, 0, 0,
        "B(insert_clusters/_existing_cluster_members/incremental_cluster_rows,"
        "PR-2 参数化)+C(孤儿清扫分页删,跨代豁免:死成员行在每一代都要清)"),
    "backend/app/repositories/sqlite/governance_store.py": (6, 0, 0, 0, 0,
        "PG 孪生同注记"),
    "backend/app/repositories/postgres/index_projection_store.py": (2, 0, 0, 2, 0,
        "A×2:version_facts 簇分量(版本身份红线)+scale-graph 读,均已配谓词"),
    "backend/app/repositories/sqlite/index_projection_store.py": (2, 0, 0, 2, 0,
        "PG 孪生同注记"),
    "backend/app/repositories/postgres/kg_build_job_store.py": (1, 0, 0, 0, 0,
        "C:_clear_notebook_derived_kg 整表按 notebook 清空(跨代豁免:"
        "staged 发布即整体作废全部派生 KG;communities 两表走动态表名清单)"),
    "backend/app/repositories/sqlite/kg_build_job_store.py": (1, 0, 0, 0, 0,
        "PG 孪生同注记"),
    "backend/app/repositories/postgres/knowledge_store.py": (10, 0, 0, 7, 0,
        "A×5 已配谓词(node_context/簇详情三查询/邻接同簇探针);其余 C:"
        "drain/终局 blanket 与 per-source 清理(跨代豁免:删源必须跨代删)"),
    "backend/app/repositories/sqlite/knowledge_store.py": (10, 0, 0, 7, 0,
        "PG 孪生同注记"),
    "backend/app/repositories/postgres/query_store.py": (2, 0, 0, 1, 0,
        "A:top_concept_names 外层谓词;内层 NOT EXISTS 用 mc.generation="
        "c.generation 相关对齐(零新参数,不计入谓词计数)"),
    "backend/app/repositories/sqlite/query_store.py": (2, 0, 0, 1, 0,
        "PG 孪生同注记"),
    "backend/app/repositories/postgres/sharing_store.py": (1, 0, 0, 2, 0,
        "C→§1.6:拷贝快照只取 published 代 + 校验两侧同谓词口径"),
    "backend/app/repositories/sqlite/sharing_store.py": (1, 0, 0, 2, 0,
        "PG 孪生同注记"),
    "backend/app/repositories/postgres/unified_kg_store.py": (26, 12, 5, 22, 11,
        "A 大头(29 站点已配谓词,LEFT JOIN 入 ON);B:swap/replace/append 供体"
        "(PR-2)+test-only replace_cluster_rows_streamed;board_partition_"
        "still_holds 为 PR-2 判据替换豁免"),
    "backend/app/repositories/sqlite/unified_kg_store.py": (26, 10, 6, 22, 11,
        "PG 孪生同注记(两侧 communities/members 出现数差异来自 SQLite 无"
        "窗口函数的 community_overview_on 分岔)"),
    "backend/app/repositories/source_subgraph_projection.py": (4, 0, 0, 2, 0,
        "A×4(两函数各 PG/SQLite 分支),共享 published_gen 局部模板"),
    "backend/app/repositories/sqlite/migrations.py": (3, 0, 0, 0, 0,
        "非站点:DDL(_migration_71 的索引重建)"),
    "backend/app/services/kg_analysis_precompute.py": (0, 0, 1, 0, 0,
        "非站点:注释里的表名"),
    "scripts/diag_open_latency.py": (3, 0, 0, 0, 0,
        "C:只读诊断镜像(version_facts/counts),不在生产读写路径"),
    "scripts/diag_pg_hotpaths.py": (3, 0, 0, 0, 0, "C:只读诊断探针"),
    "scripts/diag_slow.py": (1, 1, 0, 0, 0, "C:只读诊断对照组"),
    "scripts/verify_repository_snapshot.py": (1, 0, 0, 0, 0,
        "C:离线迁移一致性校验(v24 去重投影),只读"),
}


def _scan() -> dict[str, tuple[int, int, int, int, int]]:
    found: dict[str, tuple[int, int, int, int, int]] = {}
    for root in (_BACKEND / "app", _REPO / "scripts"):
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            counts = tuple(
                len(pattern.findall(text)) for pattern in _TABLE_PATTERNS.values()
            )
            if not any(counts):
                continue
            cluster_pred, community_pred = _predicate_counts(text)
            found[str(path.relative_to(_REPO))] = (*counts, cluster_pred, community_pred)
    return found


def test_cluster_table_census_matches_registry():
    found = _scan()
    registry = {k: v[:5] for k, v in _CENSUS.items()}
    missing = sorted(set(found) - set(registry))
    assert not missing, (
        "三表 SQL 出现在未登记文件——回到 test_cluster_generation_census 登记"
        f"分类并决定是否配 published 代次谓词: {missing}"
    )
    stale = sorted(set(registry) - set(found))
    assert not stale, f"登记的文件已无三表 SQL,清掉过期条目: {stale}"
    drifted = {
        path: {"expected": registry[path], "found": counts}
        for path, counts in found.items()
        if counts != registry[path]
    }
    assert not drifted, (
        "三表出现数或代次谓词数漂移——新增站点须登记分类,谓词不许悄悄"
        f"增删: {drifted}"
    )


def test_exemption_notes_are_non_empty_and_carry_reasons():
    """设计 §1.4:豁免清单非空且逐条带理由——C 类的存在本身是契约
    (per-source 清理必须跨代删),空清单意味着有人把豁免语义抹掉了。"""
    c_class = [note for *_counts, note in _CENSUS.values() if note.startswith("C")]
    assert len(c_class) >= 6, c_class
    for note in _CENSUS.values():
        assert len(note[5]) >= 8, note


_LEFT_JOIN_ON_SITES = {
    # 设计 §1.4 红线:LEFT JOIN 三表的代次谓词必须落在 ON 子句——落 WHERE
    # 会把 LEFT JOIN 退化成 INNER JOIN,端点无簇行的关系整体消失(质量级)。
    # (文件, 函数名, 期望 ON 段内 generation 谓词出现次数)
    "backend/app/repositories/postgres/unified_kg_store.py": (
        ("canonical_relation_seed_rows", 2),
        ("community_graph_rows", 2),
        ("relation_endpoint_name_rows", 2),
        ("source_canonical_rows", 1),
    ),
    "backend/app/repositories/sqlite/unified_kg_store.py": (
        ("canonical_relation_seed_rows", 2),
        ("community_graph_rows", 2),
        ("relation_endpoint_name_rows", 2),
        ("source_canonical_rows", 1),
    ),
}


def test_left_join_generation_predicates_live_in_on_clauses():
    """每处 `LEFT JOIN concept_clusters <alias> ON …` 的 ON 段(至下一个
    JOIN/WHERE/GROUP/ORDER 关键字前)必须含该别名的 generation 谓词。"""
    import ast

    joiner = re.compile(
        r"LEFT JOIN concept_clusters (\w+)\s+ON\s+(.*?)(?=LEFT JOIN|JOIN |WHERE |GROUP BY|ORDER BY|$)",
        re.S,
    )
    for rel_path, sites in _LEFT_JOIN_ON_SITES.items():
        source = (_REPO / rel_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
        by_name = {
            node.name: ast.get_source_segment(source, node)
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function, expected in sites:
            body = by_name.get(function)
            assert body, (rel_path, function)
            flat = " ".join(
                part.strip().strip('"').strip("'") for part in body.split("\n")
            )
            matches = joiner.findall(flat)
            hits = sum(
                1
                for alias, on_clause in matches
                if f"{alias}.generation" in on_clause
            )
            assert hits == expected, (
                rel_path, function, expected, hits,
                [m[0] for m in matches],
            )


def test_sqlite_published_predicate_subquery_is_evaluated_once(tmp_path, monkeypatch):
    """SQLite 侧的一次求值 pin(PG 侧由 EXPLAIN InitPlan 断言兜):绑定参数
    形式的 COALESCE 指针子查询在 EXPLAIN QUERY PLAN 里是非相关的
    SCALAR SUBQUERY——写成相关引用(u.notebook_id = t.notebook_id)会变
    CORRELATED SCALAR SUBQUERY,逐行求值,恰是复评否掉的形态。"""
    import sqlite3

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    from app.repositories.sqlite.unified_kg_store import _PUBLISHED_CLUSTER_GEN

    SQLiteRepository(Settings())
    db = sqlite3.connect(tmp_path / "t.db")
    plan = "\n".join(
        str(row[3]) for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT canonical_id, member_object_id "
            "FROM concept_clusters WHERE notebook_id = ? "
            f"AND generation = {_PUBLISHED_CLUSTER_GEN} "
            "ORDER BY canonical_id, member_object_id",
            ("nb",) * 2,
        ).fetchall()
    )
    assert "SCALAR SUBQUERY" in plan, plan
    assert "CORRELATED" not in plan, plan
