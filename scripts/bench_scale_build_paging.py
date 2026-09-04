#!/usr/bin/env python3
"""批 3·W4 T-W4-3 分页取数 / 分页 ANN 喂入的可复现测量台（**不进 CI**）。

这个脚本落盘的唯一目的是「结论可复现」：T-W4-3 的 ≤10% 性能门、缓冲/峰值内存
改善、以及 keyset 分页在真实规划器下走的是不是 range 续扫，全部由它产出，而不是
由一次性的临时脚本产出。它需要一个**专用的 PostgreSQL 测试库**，会写入并可清空
数据，因此绝不在 `scripts/check.sh` 或 CI 中运行。

前置：

    createdb silicon_notebook_bench_test
    psql -d silicon_notebook_bench_test -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm'
    export BENCH_POSTGRES_URL="postgresql://$USER@localhost/silicon_notebook_bench_test"

子命令：

  seed      建一个合成笔记本（对象/关系/chunk/簇/embedding），规模可调；
            `--notebooks N` 额外种 N-1 个同规模笔记本，用来复现「多库共存」——
            evidence 分页读的退化就只在这个形状下出现。
  explain   对六条改动过的读逐条打 EXPLAIN (ANALYZE, BUFFERS)：分页形与未分页形
            并列，用来判定「每页 range 续扫」还是「每页重扫 + Sort」。
  evidence  notebook_object_evidence_rows 的双模 A/B（在线无参形 vs 构建分页形），
            交替多轮取中位，这是「不降检索性能」红线的证据。
  build     build_scale_index 的整体与分阶段耗时，重复 N 次取中位。
  rss       每臂**独立子进程**测 ru_maxrss 峰值：整矩阵加载 vs 分页加载，
            以及「任一时刻最大存活 ndarray 字节」的直接测量。
  drop      清空脚本种下的笔记本。

例：

    python scripts/bench_scale_build_paging.py seed --objects 16000 --notebooks 3
    python scripts/bench_scale_build_paging.py explain
    python scripts/bench_scale_build_paging.py evidence --rounds 3
    python scripts/bench_scale_build_paging.py rss --table knowledge_embeddings
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "backend"))

BENCH_PREFIX = "bench-paging"


def _url() -> str:
    url = os.environ.get("BENCH_POSTGRES_URL") or os.environ.get("TEST_POSTGRES_URL")
    if not url:
        raise SystemExit(
            "set BENCH_POSTGRES_URL (or TEST_POSTGRES_URL) to a DEDICATED test database"
        )
    # Validate the EFFECTIVE database name, not the whole URL and not just
    # the URL path (codex #676 R3 P1 + R7 P1): a substring check over the
    # full URL passes when '_test' appears in the username / host, and a
    # path-only parse is bypassed by legal libpq semantics — in
    # ``postgresql:///safe_test?dbname=production`` the ``dbname`` query
    # parameter WINS and psycopg connects to ``production`` while the path
    # says ``safe_test``. psycopg's own conninfo parser resolves those
    # semantics, so validate what it says the connection will actually use.
    # This tool seeds, drops and VACUUMs; the resolved name must end in
    # '_test'.
    from psycopg import conninfo

    try:
        database = str(conninfo.conninfo_to_dict(url).get("dbname") or "")
    except Exception as exc:  # noqa: BLE001 - unparseable == refuse
        raise SystemExit(f"refusing to run: unparseable connection URL ({exc})")
    if not database.endswith("_test"):
        raise SystemExit(
            "refusing to run: the effective database name "
            f"{database!r} does not end in '_test' (a dedicated bench/test "
            "database is required; '_test' elsewhere in the URL does not count)"
        )
    return url


def _repository():
    from app.core.config import Settings
    from app.repositories.postgres.repository import PostgresRepository

    settings = Settings(
        database_url=_url(),
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        postgres_statement_timeout_seconds=600,
        postgres_lock_timeout_seconds=30,
    )
    return PostgresRepository(settings)


# ────────────────────────────────────────────────────────────────── seed ──

def _seed_notebook(repo, name: str, objects: int, chunks_per_object: int) -> str:
    from app.models.schemas import NotebookCreate

    notebook = repo.create_notebook(NotebookCreate(name=name))
    now = "2026-09-05T00:00:00"
    nodes: list = []
    edges: list = []
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (f"src-{notebook.id}", notebook.id, "bench", "md", "ready", now, now),
        )
        for index in range(objects):
            element_ids = []
            for offset in range(chunks_per_object):
                element_id = f"el-{index}-{offset}"
                element_ids.append(element_id)
                db.execute(
                    "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
                    "element_ids,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        f"chunk-{notebook.id}-{index}-{offset}", notebook.id,
                        f"src-{notebook.id}",
                        f"Mixture of Experts routing note {index}/{offset}. " * 8,
                        "S", json.dumps([element_id]), now,
                    ),
                )
            nodes.append({
                "local_id": f"n{index}",
                "object_type": "concept",
                # evidence 故意做厚：它是这条读的实际负载主体
                "payload": {"name": f"expert router v{index}", "section_path": ""},
                "evidence": [
                    {"element_id": eid, "quote": "routing " * 40}
                    for eid in element_ids
                ],
            })
            if index:
                edges.append({
                    "source_local_id": f"n{index}",
                    "target_local_id": f"n{index - 1}",
                    "edge_type": "depends_on",
                    "evidence": [],
                })
    repo.store_kg(notebook.id, f"src-{notebook.id}", nodes, edges)
    repo.rebuild_unified_kg(notebook.id)
    return notebook.id


def cmd_seed(args) -> None:
    from app.services.embedding import FakeEmbedder

    repo = _repository()
    try:
        try:
            from tests.model_testkit import bind_all_embedding_clients

            bind_all_embedding_clients(repo, FakeEmbedder(dim=args.dim))
        except Exception as exc:  # noqa: BLE001 — testkit is optional here
            print(f"  (embedding testkit unavailable: {type(exc).__name__})")
        created = []
        for i in range(args.notebooks):
            started = time.perf_counter()
            nid = _seed_notebook(
                repo, f"{BENCH_PREFIX} {i}", args.objects, args.chunks_per_object
            )
            created.append(nid)
            print(
                f"  seeded {nid} ({args.objects} objects) in "
                f"{round((time.perf_counter() - started) * 1000)}ms"
            )
        # VACUUM ANALYZE cannot run inside a transaction block, so it goes
        # through a plain autocommit connection of its own.
        import psycopg

        with psycopg.connect(_url(), autocommit=True) as conn:
            conn.execute("VACUUM ANALYZE")
        print("primary notebook:", created[0])
    finally:
        repo.close()


def _primary_notebook(repo) -> str:
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM notebooks WHERE name LIKE %s ORDER BY created_at LIMIT 1",
            (f"{BENCH_PREFIX}%",),
        ).fetchone()
    if row is None:
        raise SystemExit("no seeded bench notebook; run `seed` first")
    return row["id"]


def cmd_drop(args) -> None:
    repo = _repository()
    try:
        with repo._connect() as db:
            ids = [
                r["id"] for r in db.execute(
                    "SELECT id FROM notebooks WHERE name LIKE %s",
                    (f"{BENCH_PREFIX}%",),
                ).fetchall()
            ]
        for notebook_id in ids:
            repo.delete_notebook(notebook_id)
            print("  dropped", notebook_id)
    finally:
        repo.close()


# ───────────────────────────────────────────────────────────────  explain ──

_EXPLAIN_CASES = {
    # 在线检索形（无参 notebook_object_evidence_rows）——红线基线，必须无 Sort
    "evidence.online": (
        "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=%s", 1),
    # 构建分页形：首页 + 续页
    "evidence.paged.first": (
        "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=%s "
        "ORDER BY id LIMIT {page}", 1),
    "evidence.paged.next": (
        "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=%s "
        "AND id>%s ORDER BY id LIMIT {page}", 2),
    "objects.paged.next": (
        "SELECT id, object_type, payload, ordinal FROM knowledge_objects "
        "WHERE notebook_id=%s AND status IN ('approved','pending') "
        "AND ordinal>%s ORDER BY ordinal, id COLLATE \"C\" LIMIT {page}", 2),
    "chunks.paged.next": (
        "SELECT id, ordinal FROM chunks WHERE notebook_id=%s AND ordinal>%s "
        "ORDER BY ordinal, id COLLATE \"C\" LIMIT {page}", 2),
    "elements.paged.next": (
        "SELECT id,element_ids,ordinal FROM chunks WHERE notebook_id=%s "
        "AND ordinal>%s ORDER BY ordinal LIMIT {page}", 2),
    "relations.paged.next": (
        "SELECT id, source_object_id, target_object_id, edge_type "
        "FROM knowledge_relations WHERE notebook_id=%s "
        "AND review_status!='rejected' AND id COLLATE \"C\">%s "
        "ORDER BY id COLLATE \"C\" LIMIT {page}", 2),
    "clusters.paged.next": (
        "SELECT canonical_id, member_object_id FROM concept_clusters "
        "WHERE notebook_id=%s AND generation = %s "
        "AND (canonical_id COLLATE \"C\", member_object_id COLLATE \"C\") > (%s, %s) "
        "ORDER BY canonical_id COLLATE \"C\", member_object_id COLLATE \"C\" "
        "LIMIT {page}", 4),
}


def cmd_explain(args) -> None:
    repo = _repository()
    try:
        notebook_id = args.notebook or _primary_notebook(repo)
        with repo._connect() as db:
            mid_id = db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=%s "
                "ORDER BY id OFFSET 5 LIMIT 1", (notebook_id,)).fetchone()
            mid_ordinal = db.execute(
                "SELECT ordinal FROM knowledge_objects WHERE notebook_id=%s "
                "ORDER BY ordinal OFFSET 5 LIMIT 1", (notebook_id,)).fetchone()
            chunk_ordinal = db.execute(
                "SELECT ordinal FROM chunks WHERE notebook_id=%s "
                "ORDER BY ordinal OFFSET 5 LIMIT 1", (notebook_id,)).fetchone()
            relation_id = db.execute(
                "SELECT id FROM knowledge_relations WHERE notebook_id=%s "
                "ORDER BY id OFFSET 5 LIMIT 1", (notebook_id,)).fetchone()
            cluster = db.execute(
                "SELECT canonical_id, member_object_id FROM concept_clusters "
                "WHERE notebook_id=%s ORDER BY canonical_id, member_object_id "
                "OFFSET 5 LIMIT 1", (notebook_id,)).fetchone()
            generation = db.execute(
                "SELECT COALESCE(cluster_generation, 0) AS g FROM unified_kg_state "
                "WHERE notebook_id=%s", (notebook_id,)).fetchone()
            params = {
                "evidence.online": (notebook_id,),
                "evidence.paged.first": (notebook_id,),
                "evidence.paged.next": (notebook_id, mid_id["id"] if mid_id else ""),
                "objects.paged.next": (
                    notebook_id, mid_ordinal["ordinal"] if mid_ordinal else 0),
                "chunks.paged.next": (
                    notebook_id, chunk_ordinal["ordinal"] if chunk_ordinal else 0),
                "elements.paged.next": (
                    notebook_id, chunk_ordinal["ordinal"] if chunk_ordinal else 0),
                "relations.paged.next": (
                    notebook_id, relation_id["id"] if relation_id else ""),
                "clusters.paged.next": (
                    notebook_id, int(generation["g"] if generation else 0),
                    cluster["canonical_id"] if cluster else "",
                    cluster["member_object_id"] if cluster else "",
                ),
            }
            for name, (statement, _arity) in _EXPLAIN_CASES.items():
                print(f"\n=== {name} (page={args.page_rows}) ===")
                for row in db.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) "
                    + statement.format(page=int(args.page_rows)),
                    params[name],
                ).fetchall():
                    print(" ", list(row.values())[0])
    finally:
        repo.close()


# ──────────────────────────────────────────────────────────────── evidence ──

def cmd_evidence(args) -> None:
    """在线无参形 vs 构建分页形的交替 A/B（同一进程内轮换，消掉缓存漂移）。"""
    repo = _repository()
    try:
        notebook_id = args.notebook or _primary_notebook(repo)
        knowledge = repo._runtime.knowledge
        online: list[float] = []
        paged: list[float] = []
        for _round in range(args.rounds):
            for label, bucket in (("online", online), ("paged", paged)):
                with repo._connect() as db:
                    started = time.perf_counter()
                    if label == "online":
                        rows = knowledge.notebook_object_evidence_rows(db, notebook_id)
                        count = sum(1 for _ in rows)
                    else:
                        count = sum(
                            1 for _ in knowledge.notebook_object_evidence_rows_paged(
                                db, notebook_id, args.page_rows)
                        )
                    bucket.append((time.perf_counter() - started) * 1000)
                assert count, "the seeded notebook must have evidence rows"
        print(f"rows: {count}  rounds: {args.rounds}  page_rows: {args.page_rows}")
        print("online ms:", [round(v, 1) for v in sorted(online)],
              "median", round(statistics.median(online), 1))
        print("paged  ms:", [round(v, 1) for v in sorted(paged)],
              "median", round(statistics.median(paged), 1))
        ratio = statistics.median(paged) / max(statistics.median(online), 1e-9)
        print(f"paged/online = {ratio:.4f}")
    finally:
        repo.close()


# ─────────────────────────────────────────────────────────────────── build ──

def cmd_build(args) -> None:
    repo = _repository()
    try:
        notebook_id = args.notebook or _primary_notebook(repo)
        totals: list[int] = []
        stages: dict[str, list[int]] = {}
        for _ in range(args.repeats):
            manifest = repo.build_scale_index(notebook_id)
            build_ms = manifest["build_ms"]
            totals.append(int(build_ms["total"]))
            for stage, value in build_ms.items():
                stages.setdefault(stage, []).append(int(value))
        print("total_build_ms:", sorted(totals), "median",
              statistics.median(totals))
        for stage in sorted(stages):
            print(f"  {stage:18s} median {statistics.median(stages[stage])}")
    finally:
        repo.close()


# ───────────────────────────────────────────────────────────────────── rss ──

_RSS_CHILD = "__rss_child__"


def _peak_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB, macOS bytes.
    return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024


def cmd_rss(args) -> None:
    """每臂一个独立子进程：整矩阵加载 vs 分页加载的 ru_maxrss 峰值 +
    「任一时刻最大存活 ndarray 字节」。"""
    for arm in ("whole", "paged"):
        proc = subprocess.run(
            [sys.executable, __file__, _RSS_CHILD, arm, args.table,
             args.id_column, args.notebook or "", str(args.page_rows)],
            capture_output=True, text=True, env=os.environ.copy(),
        )
        sys.stdout.write(proc.stdout)
        if proc.returncode:
            sys.stderr.write(proc.stderr)
            raise SystemExit(f"{arm} arm failed with {proc.returncode}")


def _rss_child(arm: str, table: str, id_column: str, notebook: str,
               page_rows: str) -> None:
    repo = _repository()
    try:
        notebook_id = notebook or _primary_notebook(repo)
        projections = repo._runtime.index_projections
        largest = 0
        if arm == "whole":
            ids, matrix = projections.embedding_matrix(
                notebook_id, table, id_column)
            largest = int(getattr(matrix, "nbytes", 0))
            count = len(ids)
        else:
            count = 0
            for page_ids, page_matrix in projections.embedding_pages(
                notebook_id, table, id_column, int(page_rows)
            ):
                largest = max(largest, int(page_matrix.nbytes))
                count += len(page_ids)
        print(
            f"  {arm:6s} rows={count} largest_live_array_bytes={largest} "
            f"peak_rss_mb={_peak_rss_mb():.1f}"
        )
    finally:
        repo.close()


# ──────────────────────────────────────────────────────────────────── main ──

def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == _RSS_CHILD:
        _rss_child(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5],
                   sys.argv[6])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed")
    seed.add_argument("--objects", type=int, default=4000)
    seed.add_argument("--chunks-per-object", type=int, default=2)
    seed.add_argument("--notebooks", type=int, default=1)
    seed.add_argument("--dim", type=int, default=64)
    seed.set_defaults(func=cmd_seed)

    explain = sub.add_parser("explain")
    explain.add_argument("--notebook", default="")
    explain.add_argument("--page-rows", type=int, default=10_000)
    explain.set_defaults(func=cmd_explain)

    evidence = sub.add_parser("evidence")
    evidence.add_argument("--notebook", default="")
    evidence.add_argument("--rounds", type=int, default=3)
    evidence.add_argument("--page-rows", type=int, default=10_000)
    evidence.set_defaults(func=cmd_evidence)

    build = sub.add_parser("build")
    build.add_argument("--notebook", default="")
    build.add_argument("--repeats", type=int, default=3)
    build.set_defaults(func=cmd_build)

    rss = sub.add_parser("rss")
    rss.add_argument("--notebook", default="")
    rss.add_argument("--table", default="knowledge_embeddings")
    rss.add_argument("--id-column", default="object_id")
    rss.add_argument("--page-rows", type=int, default=10_000)
    rss.set_defaults(func=cmd_rss)

    drop = sub.add_parser("drop")
    drop.set_defaults(func=cmd_drop)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
