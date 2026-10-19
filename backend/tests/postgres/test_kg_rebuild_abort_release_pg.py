"""真实 `rebuild_communities` 在折叠循环中途抛异常时,不挂死、且行为符合原设计。

这是 `test_kg_row_stream_release_pg.py` 的**调用点级**守卫。那边直接调 `_fold_canonical_edges`
/ `_fold_source_rows`,证明 helper 本身会 `close()` 流;但它证明不了**真实调用点走的是
helper** —— 把循环内联回 `rebuild_communities` / `_compute_kg_analysis` 并去掉 `closing`,
那边的用例仍然全绿(这是个"移动"变异)。所以这里驱动真实的调用链。

变异实测(都在提交之后做):
  · 边表那处内联回 `for r in graph_rows:`(流绑在局部变量 `graph_rows` 上)→ `edges` 红(挂死超时);
  · 来源画像那处保留 helper 但去掉 `closing`,或内联成 `rows = …; for row in rows:` → `sources` 红;
  · ⚠ 来源画像那处内联成 `for row in store.source_canonical_rows(...)`(迭代器只是栈上的临时值)
    **不会红** —— CPython 在异常展开时就把临时迭代器回收、流被关掉,本来就不挂。这不是守卫的
    洞,而是那种形态确实安全;守卫盯的是会挂的两种形态(形参/局部变量持有流)。

三条用例:

  · `sources`:`SourceBoardCounter.add` 抛异常。`_compute_kg_analysis` 的失败按原设计
    **被吞掉**,发 `kg_analysis_precompute_failed`,板块照常发布、`rebuild_communities`
    正常返回。少了 `closing`,吞异常的 `with self._connect()` 在离开时挂死在 `rollback()`。
  · `edges`:`CanonicalEdgeTableBuilder.add` 抛异常。折叠边表的失败是核心路径,**异常冒泡**
    出 `rebuild_communities`(而不是被吞,也不是挂死)。
  · `none`:不做手脚的对照。证明夹具本身能建出板块、两个折叠点真的会被走到 —— 否则上面两
    条可能因为"根本没走到折叠"而空绿。

全部放进**子进程**并设超时:退化时子进程挂在 `rollback()` 上,这里按超时判红(并杀掉子
进程),而不是把整条 PG 泳道挂住。子进程里被加了桩的 `add` 记调用次数,断言 ≥ 1。
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.models.notebooks import NotebookCreate
from app.repositories.postgres._store_utils import normalize_timestamp

pytestmark = pytest.mark.postgres_integration

NOW = "2026-10-19T00:00:00+00:00"

_CHILD = textwrap.dedent(
    """
    import json
    import os
    import sys

    from app.core.config import Settings
    from app.repositories.postgres.repository import PostgresRepository
    from app.services import kg_analysis_precompute as precompute

    variant, notebook_id = sys.argv[1], sys.argv[2]
    boom_calls = []


    def boom(self, *args, **kwargs):
        boom_calls.append(1)
        raise RuntimeError("boom")


    if variant == "sources":
        precompute.SourceBoardCounter.add = boom
    elif variant == "edges":
        precompute.CanonicalEdgeTableBuilder.add = boom

    settings = Settings(
        _env_file=None,
        database_url=os.environ["DATABASE_URL"],
        postgres_pool_min_size=1,
        postgres_pool_max_size=2,
        postgres_pool_acquire_timeout_seconds=1,
        postgres_statement_timeout_seconds=2,
        postgres_chunk_fts_timeout_seconds=1.0,
        postgres_lock_timeout_seconds=1,
    )
    repository = PostgresRepository(settings)
    events = []
    repository._runtime.event_log.emit = events.append
    try:
        outcome = "returned:%s" % repository._runtime.knowledge_lifecycle.rebuild_communities(
            notebook_id, level=0
        )
    except RuntimeError as exc:
        outcome = "raised:%s" % exc
    with repository._runtime.database.connect() as db:
        assert db.execute("SELECT 1 AS x").fetchone()["x"] == 1
    print("RESULT " + json.dumps({
        "outcome": outcome,
        "boom_calls": len(boom_calls),
        "events": [event.get("kind") for event in events],
    }))
    repository.close()
    """
)


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def _seed_two_docs_sharing_a_clique(repository) -> str:
    """两篇文档共享 4 个 canonical,各自把自己的 4 个对象连成完全图 → 一个稠密社区
    (Louvain 不会把 K4 劈开),与 `tests/test_rebuild_communities.py` 同款夹具。"""
    notebook_id = repository.create_notebook(NotebookCreate(name="abort-fold")).id
    runtime = repository._runtime
    stamp = normalize_timestamp(runtime.seams.now())
    objects, relations, clusters = [], [], []
    for doc, source_id in (("A", "src-A"), ("B", "src-B")):
        for i in (1, 2, 3, 4):
            object_id = f"{doc}{i}"
            objects.append((
                object_id, notebook_id, "concept", "approved",
                json.dumps({"name": f"K{i}"}), "[]", source_id, NOW, NOW,
            ))
            clusters.append((
                f"cl-{object_id}", notebook_id, f"K{i}", object_id, f"K{i}",
                "concept", "", "", NOW,
            ))
        for a, b in itertools.combinations((1, 2, 3, 4), 2):
            relations.append((
                f"r-{doc}{a}{b}", notebook_id, source_id, f"{doc}{a}", f"{doc}{b}",
                "related_to", "[]", NOW,
            ))
    with runtime.database.write() as db:
        for source_id in ("src-A", "src-B"):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
                "file_name,file_path,file_size,file_hash,summary,created_at,updated_at,"
                "doc_type) VALUES (%s,%s,%s,'markdown','extracted','parsed','','',0,%s,'',"
                "%s,%s,'')",
                (source_id, notebook_id, source_id, source_id, stamp, stamp),
            )
        runtime.knowledge.insert_object_chunk(db, objects)
        runtime.knowledge.insert_relation_chunk(db, relations)
        runtime.unified_kg.replace_cluster_rows_streamed(
            db, notebook_id, "concept", clusters
        )
    return notebook_id


def _drive_rebuild(postgres_repository, variant: str) -> dict:
    notebook_id = _seed_two_docs_sharing_a_clique(postgres_repository)
    backend_root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "DATABASE_URL": postgres_repository._runtime.database.settings.database_url,
        "PYTHONPATH": str(backend_root),
        "LLM_LOG_ENABLED": "false",
    }
    try:
        result = subprocess.run(
            [sys.executable, "-c", _CHILD, variant, notebook_id],
            env=env, capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"rebuild_communities hung after an exception mid-fold ({variant}): the "
            "real call site did not close() its cursor().stream(), so the connection "
            "release blocked in rollback() instead of letting the exception through."
        )
    assert result.returncode == 0, result.stderr[-3000:]
    line = next(
        (row for row in result.stdout.splitlines() if row.startswith("RESULT ")), None
    )
    assert line is not None, result.stdout[-2000:] + result.stderr[-2000:]
    return json.loads(line[len("RESULT "):])


def test_the_untouched_rebuild_builds_boards_and_reaches_both_folds(postgres_repository):
    """对照:不做手脚时夹具能建出板块、预计算不失败 —— 下面两条的"抛异常"才有意义。"""
    result = _drive_rebuild(postgres_repository, "none")

    assert result["outcome"].startswith("returned:")
    assert int(result["outcome"].split(":")[1]) >= 1
    assert "kg_analysis_precompute_failed" not in result["events"]
    assert "communities_rebuilt" in result["events"]


def test_an_abort_in_the_source_fold_is_swallowed_and_reported_not_hung(postgres_repository):
    result = _drive_rebuild(postgres_repository, "sources")

    assert result["boom_calls"] >= 1, "SourceBoardCounter.add 根本没被走到"
    # 原设计:预计算失败不掀翻板块重建 —— 事件 + 正常返回。
    assert result["outcome"].startswith("returned:")
    assert int(result["outcome"].split(":")[1]) >= 1
    assert "kg_analysis_precompute_failed" in result["events"]


def test_an_abort_in_the_edge_fold_bubbles_out_of_the_rebuild_not_hung(postgres_repository):
    result = _drive_rebuild(postgres_repository, "edges")

    assert result["boom_calls"] >= 1, "CanonicalEdgeTableBuilder.add 根本没被走到"
    assert result["outcome"] == "raised:boom"
