#!/usr/bin/env python3
"""Read-only source-fact coverage audit; prints counts and ids, never evidence."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote


def _sqlite(path: str):
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise SystemExit(f"数据库文件不存在: {resolved}")
    db = sqlite3.connect(
        f"file:{quote(str(resolved), safe='/')}?mode=ro", uri=True, timeout=30.0
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db, "?", "sqlite"


def _postgres(url: str):
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise SystemExit("缺少 psycopg，无法审计 PostgreSQL") from exc
    db = psycopg.connect(url, row_factory=dict_row)
    db.execute("SET TRANSACTION READ ONLY")
    return db, "%s", "postgresql"


def _classified_cte(placeholder: str, dialect: str, notebook_id: str) -> tuple[str, tuple]:
    where = "WHERE s.source_type NOT IN ('memory','knowhow')"
    params: list[object] = []
    if notebook_id:
        where += f" AND s.notebook_id={placeholder}"
        params.append(notebook_id)
    order_id = "er.id COLLATE \"C\"" if dialect == "postgresql" else "er.id"
    retry_expr = (
        "POSITION('retry_incomplete=1' IN COALESCE(l.error_message,''))>0"
        if dialect == "postgresql"
        else "instr(COALESCE(l.error_message,''),'retry_incomplete=1')>0"
    )
    return f"""
WITH visible AS (
  SELECT s.id,s.notebook_id FROM sources s {where}
), ranked AS (
  SELECT er.id,er.source_id,er.status,er.error_message,
         ROW_NUMBER() OVER (
           PARTITION BY er.source_id
           ORDER BY er.created_at DESC,{order_id} DESC
         ) AS rn
  FROM extraction_runs er JOIN visible v
    ON v.id=er.source_id AND v.notebook_id=er.notebook_id
  WHERE er.run_type='kg'
), effective AS (
  SELECT v.id,v.notebook_id,
    CASE
      WHEN l.status='completed'
       AND substr(COALESCE(l.error_message,''),1,11)='kg objects='
      THEN l.id
      WHEN l.status='completed' AND {retry_expr}
      THEN COALESCE((
        SELECT p.id FROM extraction_runs p
        WHERE p.notebook_id=v.notebook_id AND p.source_id=v.id
          AND p.run_type='kg' AND p.status='completed'
          AND substr(COALESCE(p.error_message,''),1,11)='kg objects='
        ORDER BY p.created_at DESC,{('p.id COLLATE "C"' if dialect == 'postgresql' else 'p.id')} DESC
        LIMIT 1
      ),'')
      ELSE ''
    END AS current_generation
  FROM visible v LEFT JOIN ranked l ON l.source_id=v.id AND l.rn=1
), object_memberships(source_id,object_id) AS (
  SELECT ko.source_id,ko.id
  FROM knowledge_objects ko JOIN visible v
    ON v.id=ko.source_id AND v.notebook_id=ko.notebook_id
  UNION
  SELECT kos.source_id,kos.object_id
  FROM knowledge_object_sources kos JOIN visible v
    ON v.id=kos.source_id AND v.notebook_id=kos.notebook_id
  JOIN knowledge_objects ko
    ON ko.id=kos.object_id AND ko.notebook_id=kos.notebook_id
), object_counts AS (
  SELECT source_id,COUNT(*) AS expected_objects
  FROM object_memberships GROUP BY source_id
), fact_counts AS (
  SELECT e.id AS source_id,COUNT(f.id) AS persisted_facts,
         COALESCE(SUM(CASE WHEN f.id IS NOT NULL
                           AND f.source_generation<>e.current_generation
                      THEN 1 ELSE 0 END),0) AS stale_generation_facts,
         COALESCE(SUM(CASE WHEN f.id IS NOT NULL
                           AND f.projection_version<>1
                      THEN 1 ELSE 0 END),0) AS wrong_projection_facts
  FROM effective e LEFT JOIN knowledge_source_facts f ON f.source_id=e.id
  GROUP BY e.id
), classified AS (
  SELECT e.id,e.notebook_id,b.objects_scanned,b.facts_written,
         b.incomplete_objects,COALESCE(fc.persisted_facts,0) AS persisted_facts,
         COALESCE(oc.expected_objects,0) AS expected_objects,
    CASE
      WHEN b.source_id IS NULL THEN 'missing'
      WHEN e.current_generation='' THEN 'no_generation'
      WHEN b.source_generation<>e.current_generation
        OR COALESCE(fc.stale_generation_facts,0)>0 THEN 'generation_drift'
      WHEN b.projection_version<>1
        OR COALESCE(fc.wrong_projection_facts,0)>0 THEN 'projection_version_drift'
      WHEN b.objects_scanned<>COALESCE(oc.expected_objects,0)
        OR b.facts_written<>COALESCE(fc.persisted_facts,0) THEN 'count_drift'
      ELSE b.status
    END AS effective_status
  FROM effective e
  LEFT JOIN knowledge_source_fact_backfills b ON b.source_id=e.id
  LEFT JOIN fact_counts fc ON fc.source_id=e.id
  LEFT JOIN object_counts oc ON oc.source_id=e.id
)
""", tuple(params)


def audit(
    connection, placeholder: str, notebook_id: str, sample_limit: int,
    dialect: str = "sqlite",
) -> dict:
    cte, params = _classified_cte(placeholder, dialect, notebook_id)
    rows = connection.execute(
        cte
        + "SELECT effective_status,COUNT(*) AS source_count,"
          "COALESCE(SUM(persisted_facts),0) AS persisted_facts,"
          "COALESCE(SUM(expected_objects),0) AS expected_objects,"
          "COALESCE(SUM(objects_scanned),0) AS objects_scanned,"
          "COALESCE(SUM(incomplete_objects),0) AS incomplete_objects "
          "FROM classified GROUP BY effective_status ORDER BY effective_status",
        params,
    ).fetchall()
    counts: dict[str, int] = {}
    samples: dict[str, list[str]] = defaultdict(list)
    facts = expected = scanned = incomplete_objects = 0
    for raw in rows:
        row = dict(raw)
        status = str(row["effective_status"])
        counts[status] = int(row["source_count"])
        facts += int(row["persisted_facts"] or 0)
        expected += int(row["expected_objects"] or 0)
        scanned += int(row.get("objects_scanned") or 0)
        incomplete_objects += int(row.get("incomplete_objects") or 0)
    if sample_limit:
        sample_rows = connection.execute(
            cte
            + "SELECT effective_status,id FROM ("
              "SELECT effective_status,id,ROW_NUMBER() OVER ("
              "PARTITION BY effective_status ORDER BY id) AS sample_rank "
              "FROM classified) sampled "
              f"WHERE sample_rank<={placeholder} "
              "ORDER BY effective_status,id",
            (*params, sample_limit),
        ).fetchall()
        for raw in sample_rows:
            row = dict(raw)
            samples[str(row["effective_status"])].append(str(row["id"]))
    mismatch_statuses = {
        "generation_drift", "projection_version_drift", "count_drift"
    }
    return {
        "notebook_id": notebook_id or "*",
        "read_only": True,
        "sources": sum(counts.values()),
        "status_counts": dict(sorted(counts.items())),
        "integrity_mismatches": sum(
            count for status, count in counts.items()
            if status in mismatch_statuses
        ),
        "persisted_facts": facts,
        "expected_objects": expected,
        "objects_scanned": scanned,
        "incomplete_objects": incomplete_objects,
        "sample_source_ids": dict(sorted(samples.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--db", help="SQLite database path")
    target.add_argument("--database-url", help="PostgreSQL connection URL")
    parser.add_argument("--notebook", default="", help="optional notebook id")
    parser.add_argument("--sample-limit", type=int, default=20)
    args = parser.parse_args()
    connection, placeholder, dialect = (
        _sqlite(args.db) if args.db else _postgres(args.database_url)
    )
    try:
        report = audit(
            connection, placeholder, args.notebook,
            max(0, min(int(args.sample_limit), 100)), dialect,
        )
    finally:
        connection.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    bad = sum(
        count for status, count in report["status_counts"].items()
        if status != "complete"
    )
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
