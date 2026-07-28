#!/usr/bin/env python3
"""Read-only audit of persisted KG relations against the product edge contract.

This command never mutates or automatically rejects rows.  It reports aggregate
counts by edge type, endpoint types, origin signal and review status, plus a
bounded list of relation ids.  It intentionally never prints evidence or source
text.

Run from the repository root:

    PYTHONPATH=backend python scripts/audit_kg_edge_contract.py \
      --db .local/silicon_notebook.db [--notebook nb-...] [--sample-limit 10]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

try:
    from app.services.kg.edge_schema import (
        EDGE_SPECS,
        is_valid_edge_pair,
        normalize_node_type,
    )
except ImportError as exc:  # pragma: no cover - operator environment failure
    sys.stderr.write(
        f"无法导入 KG 边契约: {exc}\n"
        "请从仓库根目录运行并设置 PYTHONPATH=backend。\n"
    )
    raise SystemExit(2)


def _open_readonly(path: str) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise SystemExit(f"数据库文件不存在: {resolved}")
    connection = sqlite3.connect(
        f"file:{quote(str(resolved), safe='/')}?mode=ro", uri=True, timeout=30.0
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _origin(evidence_raw: str) -> str:
    try:
        evidence = json.loads(evidence_raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return "llm_or_legacy"
    bases = {
        str(item.get("basis") or "")
        for item in evidence
        if isinstance(item, dict)
    }
    if any(value.startswith("relink:") for value in bases):
        return "deterministic_relink"
    if any(value.startswith("completion:") for value in bases):
        return "bounded_completion"
    return "llm_or_legacy"


def audit(connection: sqlite3.Connection, notebook_id: str, sample_limit: int) -> dict:
    params: list[object] = []
    where = ""
    if notebook_id:
        where = "WHERE r.notebook_id = ?"
        params.append(notebook_id)
    rows = connection.execute(
        "SELECT r.id, r.notebook_id, r.edge_type, r.evidence, "
        "       r.review_status, so.object_type AS source_type, "
        "       tp.object_type AS target_type "
        "FROM knowledge_relations r "
        "LEFT JOIN knowledge_objects so ON so.id = r.source_object_id "
        "LEFT JOIN knowledge_objects tp ON tp.id = r.target_object_id "
        f"{where} ORDER BY r.notebook_id, r.id",
        params,
    )

    counts: Counter[tuple] = Counter()
    samples: dict[tuple, list[str]] = defaultdict(list)
    totals = Counter()
    for row in rows:
        source_type = row["source_type"] or "<missing>"
        target_type = row["target_type"] or "<missing>"
        valid = is_valid_edge_pair(row["edge_type"], source_type, target_type)
        is_extension = (
            row["edge_type"] in EDGE_SPECS
            and source_type != "<missing>"
            and target_type != "<missing>"
            and (not normalize_node_type(source_type) or not normalize_node_type(target_type))
        )
        validity = "valid" if valid else ("extension" if is_extension else "invalid")
        origin = _origin(row["evidence"])
        review_status = row["review_status"] or "pending"
        key = (
            row["edge_type"], source_type, target_type, validity, origin,
            review_status,
        )
        counts[key] += 1
        totals[validity] += 1
        totals[f"origin:{origin}"] += 1
        if len(samples[key]) < sample_limit:
            samples[key].append(row["id"])

    groups = [
        {
            "edge_type": key[0],
            "source_type": key[1],
            "target_type": key[2],
            "validity": key[3],
            "origin": key[4],
            "review_status": key[5],
            "count": count,
            "sample_relation_ids": samples[key],
        }
        for key, count in sorted(counts.items())
    ]
    return {
        "notebook_id": notebook_id or "*",
        "read_only": True,
        "total": totals["valid"] + totals["extension"] + totals["invalid"],
        "valid": totals["valid"],
        "extension": totals["extension"],
        "invalid": totals["invalid"],
        "origins": {
            key.removeprefix("origin:"): value
            for key, value in sorted(totals.items())
            if key.startswith("origin:")
        },
        "groups": groups,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite database path")
    parser.add_argument("--notebook", default="", help="optional notebook id")
    parser.add_argument("--sample-limit", type=int, default=10)
    args = parser.parse_args()
    sample_limit = max(0, min(int(args.sample_limit), 100))
    with _open_readonly(args.db) as connection:
        report = audit(connection, args.notebook, sample_limit)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["invalid"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
