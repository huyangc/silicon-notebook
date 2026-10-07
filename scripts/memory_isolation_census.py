#!/usr/bin/env python3
"""Read-only census of what the ruling-M1 post-readiness pass (PostgreSQL
0067 / SQLite v87) will do after the upgrade, with the numbers needed to
estimate its model cost. Run it on a production SNAPSHOT before deploying:
it writes nothing (PostgreSQL: ``default_transaction_read_only``, rolled back;
SQLite: ``mode=ro`` + ``query_only``; scale manifests are only read), calls no
model, and holds one long read transaction (a snapshot is the right target).

Per notebook it prints:

* set ``F`` -- holds a Memory source: always gets one full isolated rebuild;
* set ``G`` -- every other notebook with clusters: the signal that queues it
  for the same rebuild (``dirty`` / ``seed`` / ``stale_reference``; ``-`` =
  clean, marked isolated without a rebuild) and what that check cost
  (seconds, statements); with ``--all-signals`` every signal judged on its
  own (so "dirty only" can be told apart);
* for every notebook: its objects and ``merge_review_pairs`` -- the ambiguous
  seed pairs its last rebuild sent to the model (one more rebuild sends about
  as many; concept descriptions are regenerated only where evidence changed,
  communities need no model);
* set ``SCALE`` (with ``--storage-dir``) -- a published scale index built
  before the isolation on a non-copyable notebook (signal ``index``), or a
  standalone visualisation of that kind on a non-copyable notebook without a
  scale root and over VIZ_SYNC_BUILD_MAX_OBJECTS objects (signal ``viz``):
  the pass queues one full index build for it once the notebook's KG is
  isolated (no model call).

Before the upgrade F and G are computed live, as the migration would; after
it they are the notebooks still marked 0 and 2.

Usage:
  PYTHONPATH=backend python3 scripts/memory_isolation_census.py \\
      --database-url <snapshot URL> [--storage-dir <storage dir>] \\
      [--all-signals] [--notebook ID ...] [--json]
  (defaults: DATABASE_URL, SILICON_NOTEBOOK_STORAGE_DIR)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))


def _store_module(database_url: str):
    from app.core.database_url import database_identity

    if database_identity(database_url).scheme == "postgresql":
        from app.repositories.postgres import memory_isolation_store as module
    else:
        from app.repositories.sqlite import memory_isolation_store as module
    return module


def _setting(env: str, field: str) -> int:
    """A bound as the service reads it (environment, else the Settings
    default)."""
    from app.core.config import Settings

    return int(os.environ.get(env, Settings.model_fields[field].default))


def _scale_rows(module, db, storage_dir: str, notebook_ids) -> list[dict]:
    from app.services.memory_isolation_rebuild import manifest_predates_memory_isolation

    max_bytes = _setting("NOTEBOOK_COPY_MAX_BYTES", "notebook_copy_max_bytes")
    max_rows = _setting("NOTEBOOK_COPY_MAX_ROWS", "notebook_copy_max_rows")
    viz_budget = _setting("VIZ_SYNC_BUILD_MAX_OBJECTS", "viz_sync_build_max_objects")

    def roots(kind: str) -> dict:
        root = Path(storage_dir) / kind
        if not root.is_dir():
            return {}
        return {entry.name: entry / "manifest.json" for entry in sorted(root.iterdir())
                if entry.is_dir() and "." not in entry.name
                and (entry / "manifest.json").is_file()}

    scale_roots, viz_roots = roots("kg_index"), roots("kg_viz")
    candidates = [(nb, path, "index") for nb, path in scale_roots.items()]
    # a standalone viz without a scale root, over the synchronous viz budget
    candidates += [(nb, path, "viz") for nb, path in viz_roots.items()
                   if nb not in scale_roots]
    rows = []
    for notebook_id, manifest_path, kind in candidates:
        if notebook_ids is not None and notebook_id not in notebook_ids:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
        if not manifest_predates_memory_isolation(manifest):
            continue
        facts = module.MemoryIsolationStore.census_facts(db, notebook_id)
        if kind == "viz" and facts["objects"] <= viz_budget:
            continue  # rebuilt on its first read
        copyable = (facts["bytes"] <= max_bytes
                    and facts["chunks"] + facts["objects"] <= max_rows)
        if copyable:
            continue  # the operator rebuilds a copyable notebook's artifact
        rows.append({"notebook_id": notebook_id, "set": "SCALE", "signal": kind,
                     "signals": None, "seconds": 0.0, "statements": 0, **facts})
    return rows


def census(database_url: str, notebook_ids=None, *, all_signals: bool = False,
           storage_dir: str | None = None, page_size: int | None = None) -> dict:
    module = _store_module(database_url)
    kwargs = {} if page_size is None else {"page_size": page_size}
    with module.read_only_connection(database_url) as db:
        result = module.MemoryIsolationStore.seed_check_census(
            db, notebook_ids, all_signals=all_signals, **kwargs)
        if storage_dir:
            result["notebooks"] += _scale_rows(
                module, db, storage_dir,
                None if notebook_ids is None else set(notebook_ids))
    return result


def summarise(result: dict) -> dict:
    rows = result["notebooks"]
    f_rows = [r for r in rows if r["set"] == "F"]
    g_rows = [r for r in rows if r["set"] == "G"]
    queued = [r for r in g_rows if r["signal"]]
    rebuilt = f_rows + queued
    by_signal: dict[str, int] = {}
    for row in g_rows:
        key = row["signal"] or "clean"
        by_signal[key] = by_signal.get(key, 0) + 1
    summary = {
        "phase": result["phase"],
        "f_rebuilt": len(f_rows),
        "g_checked": len(g_rows),
        "g_queued": len(queued),
        "g_by_first_signal": dict(sorted(by_signal.items())),
        "rebuilds_total": len(rebuilt),
        "rebuild_objects_total": sum(r["objects"] for r in rebuilt),
        "merge_review_pairs_total": sum(r["merge_review_pairs"] for r in rebuilt),
        "scale_builds": sum(1 for r in rows if r["set"] == "SCALE"),
        "check_seconds_total": round(sum(r["seconds"] for r in g_rows), 3),
        "check_seconds_max": round(max((r["seconds"] for r in g_rows), default=0.0), 3),
        "check_statements_total": sum(r["statements"] for r in g_rows),
        "check_statements_max": max((r["statements"] for r in g_rows), default=0),
    }
    with_all = [r for r in g_rows if r["signals"] is not None]
    if with_all:
        summary["g_by_signal"] = {
            name: sum(1 for r in with_all if r["signals"][name])
            for name in ("dirty", "seed", "stale_reference")
        }
        summary["g_dirty_only"] = sum(
            1 for r in with_all
            if r["signals"]["dirty"] and not r["signals"]["seed"]
            and not r["signals"]["stale_reference"])
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--storage-dir",
                        default=os.environ.get("SILICON_NOTEBOOK_STORAGE_DIR", ""),
                        help="storage dir holding kg_index/ (scale manifests)")
    parser.add_argument("--all-signals", action="store_true",
                        help="judge every G signal on its own (no short circuit)")
    parser.add_argument("--notebook", action="append", default=None,
                        help="restrict to this notebook id (repeatable)")
    parser.add_argument("--json", action="store_true", help="print one JSON document")
    args = parser.parse_args(argv)
    if not args.database_url:
        print("error: --database-url (or DATABASE_URL) is required", file=sys.stderr)
        return 2
    result = census(args.database_url, args.notebook, all_signals=args.all_signals,
                    storage_dir=args.storage_dir or None)
    summary = summarise(result)
    if args.json:
        print(json.dumps({"summary": summary, "notebooks": result["notebooks"]},
                         ensure_ascii=False, indent=2))
        return 0
    for row in result["notebooks"]:
        signals = ""
        if row["signals"] is not None:
            signals = "\t" + ",".join(k for k, v in row["signals"].items() if v)
        print(f"{row['set']}\t{row['notebook_id']}\t{row['signal'] or '-'}\t"
              f"objects={row['objects']}\tmerge_review_pairs={row['merge_review_pairs']}\t"
              f"{row['seconds']:.3f}s\t{row['statements']} statements{signals}")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
