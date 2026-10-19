"""双后端孪生:`community_graph_rows` 在 PostgreSQL 与 SQLite 上返回**同一批边、同一个行序**。

为什么要有这条:PG 侧这条查询走服务端流式游标(`cursor().stream()`),SQLite 侧仍是
惰性的 `sqlite3` 游标 —— 两侧的**形态刻意分岔**,SQL 文本也各写各的方言
(`ORDER BY kr.id COLLATE "C"` / `ORDER BY kr.id`)。parity 要求的是语义等价而不是文本
相同,所以这里对**同一批夹具数据**在两侧各跑一遍,逐行比对:Louvain 对边序敏感
(`rebuild_communities` 里「首次出现序」那段说明),行序漂了就是已部署库的板块划分全漂。

夹具里每一处都有证人:
  · 关系**故意不按 id 顺序插入**,并且 id 里混了大写 / 下划线 / 小写 —— `COLLATE "C"`
    是逐字节序(`B`=0x42 < `_`=0x5F < `a`=0x61),而 `en_US` 这类区域排序会把它们排成另一个
    次序,所以「PG 用了区域排序」或「根本没排序」都会让这里变红;
  · 被否决的关系排除;
  · 端点经 `concept_clusters` 映射到 canonical,没进任何簇的端点回退成对象自己的 id;
  · 两端映射到同一个 canonical 的边(`s == t`)与重复边**原样返回**(丢弃是调用方的事);
  · 派生自 Memory 的关系 / 指向 Memory 对象的关系排除(两条不同的谓词);
  · 别的 notebook 的关系不混进来。
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.repositories.postgres.knowledge_store import KnowledgeStore as PgKnowledgeStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.unified_kg_store import UnifiedKgStore as PgUnifiedKgStore
from app.services.repository_runtime import RepositoryCompatibilitySeams
from app.services.sqlite_repository import SQLiteRepository

pytestmark = pytest.mark.postgres_integration

NOW = "2026-10-19T00:00:00+00:00"
NB = "nb-twin"
OTHER_NB = "nb-twin-other"

# 期望的 (names, 有序 (s, t))。人工推出来的,不是拿一侧的输出抄过来的:
#   ko-a1/ko-a2 -> K-one,ko-a3 -> K-two,其余没进簇、回退成自己的 id。
#   纳入的关系 id 按逐字节序:rel-B < rel-_u < rel-a < rel-a2 < rel-b < rel-self。
EXPECTED_NAMES = {"K-one": "One", "K-two": "Two"}
EXPECTED_EDGES = [
    ("K-one", "ko-a5"),   # rel-B     ko-a1 -> ko-a5
    ("ko-a4", "ko-a5"),   # rel-_u    ko-a4 -> ko-a5(两端都没进簇)
    ("K-one", "K-two"),   # rel-a     ko-a2 -> ko-a3
    ("K-one", "K-two"),   # rel-a2    ko-a1 -> ko-a3(与 rel-a 同一对 canonical,重复边)
    ("K-two", "ko-a4"),   # rel-b     ko-a3 -> ko-a4
    ("K-one", "K-one"),   # rel-self  ko-a1 -> ko-a2(两端同一 canonical,调用方才丢)
]


def _object(object_id: str, *, notebook: str = NB, source: str = "src-file") -> tuple:
    return (object_id, notebook, "concept", "approved",
            json.dumps({"name": object_id}), "[]", source, NOW, NOW)


def _cluster(index: int, canonical: str, object_id: str, name: str) -> tuple:
    return (f"cluster-twin-{index}", NB, canonical, object_id, name, "concept",
            "", "", NOW)


def _seed(connection, mark: str, knowledge, unified) -> None:
    """用两侧**同一套**行元组往 `connection` 铺夹具;`mark` 是方言的占位符。"""
    def q(sql: str) -> str:
        return sql.replace("?", mark)

    for notebook in (NB, OTHER_NB):
        connection.execute(
            q("INSERT INTO notebooks (id,name,created_at,updated_at) VALUES (?,?,?,?)"),
            (notebook, notebook, NOW, NOW),
        )
    for source_id, source_type in (("src-file", "file"), ("src-mem", "memory")):
        connection.execute(
            q("INSERT INTO sources (id,notebook_id,title,source_type,created_at,"
              "updated_at) VALUES (?,?,?,?,?,?)"),
            (source_id, NB, source_id, source_type, NOW, NOW),
        )
    knowledge.insert_object_chunk(connection, [
        *[_object(f"ko-a{i}") for i in range(1, 6)],
        _object("ko-mem", source="src-mem"),
        _object("ko-other", notebook=OTHER_NB),
    ])
    # 故意**乱序**插入(id 既不是升序也不是降序),这样「没写 ORDER BY」也会被抓到。
    knowledge.insert_relation_chunk(connection, [
        (rid, notebook, source, src, dst, "about", "[]", NOW)
        for rid, notebook, source, src, dst in (
            ("rel-b", NB, "src-file", "ko-a3", "ko-a4"),
            ("rel-self", NB, "src-file", "ko-a1", "ko-a2"),
            ("rel-a", NB, "src-file", "ko-a2", "ko-a3"),
            ("rel-rej", NB, "src-file", "ko-a1", "ko-a2"),
            ("rel-B", NB, "src-file", "ko-a1", "ko-a5"),
            ("rel-memobj", NB, "src-file", "ko-a1", "ko-mem"),
            ("rel-a2", NB, "src-file", "ko-a1", "ko-a3"),
            ("rel-memsrc", NB, "src-mem", "ko-a1", "ko-a4"),
            ("rel-other", OTHER_NB, "src-file", "ko-other", "ko-other"),
            ("rel-_u", NB, "src-file", "ko-a4", "ko-a5"),
        )
    ])
    connection.execute(
        q("UPDATE knowledge_relations SET review_status='rejected' WHERE id=?"),
        ("rel-rej",),
    )
    unified.replace_cluster_rows_streamed(connection, NB, "concept", [
        _cluster(1, "K-one", "ko-a1", "One"),
        _cluster(2, "K-one", "ko-a2", "One"),
        _cluster(3, "K-two", "ko-a3", "Two"),
    ])


def _read(store, connection) -> tuple[dict, list]:
    names, rows = store.community_graph_rows(connection, NB)
    return dict(names), [(row["s"], row["t"]) for row in rows]


def _seams() -> RepositoryCompatibilitySeams:
    return RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-twin", now=lambda: NOW,
        copy_chunk_size=lambda: 100, remap_json_ids=lambda value, _m: value,
        in_chunk_size=lambda: 100,
    )


def test_postgres_and_sqlite_return_the_same_edges_in_the_same_order(
    postgres_database, tmp_path, monkeypatch,
):
    PostgresMigrator(postgres_database).migrate()
    pg_knowledge = PgKnowledgeStore(postgres_database, _seams())
    pg_unified = PgUnifiedKgStore(postgres_database, now=lambda: NOW)
    with postgres_database.write() as connection:
        _seed(connection, "%s", pg_knowledge, pg_unified)

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'twin.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    sqlite_repo = SQLiteRepository(Settings())
    with sqlite_repo._write() as connection:
        _seed(connection, "?", sqlite_repo._runtime.knowledge,
              sqlite_repo._runtime.unified_kg)

    with postgres_database.connect() as pg_db:
        pg_names, pg_edges = _read(pg_unified, pg_db)
    with sqlite_repo._connect() as lite_db:
        lite_names, lite_edges = _read(sqlite_repo._runtime.unified_kg, lite_db)

    # 逐行比对(含顺序),并且都等于人工推出来的期望 —— 不是只证「两侧彼此一致」。
    assert pg_edges == lite_edges == EXPECTED_EDGES
    assert pg_names == lite_names == EXPECTED_NAMES
