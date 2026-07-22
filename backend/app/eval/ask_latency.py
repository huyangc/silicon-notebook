"""Per-stage ask latency aggregator.

Reads `.local/logs/events.jsonl` (channel="events") produced by
`sqlite_repository.EventLogger` and reports P50/P95/max per stage.

Event schema (sqlite_repository.py:3222-3225):
    {"kind": "ask_stage", "notebook_id": "...", "stage": "<name>",
     "latency_ms": <int>, "ts": "...", "channel": "events"}

Percentile method (nearest-rank, ceiling / upper index):
    p50 = sorted_values[ceil(0.50 * n) - 1]   (1-indexed, clamped to last)
    p95 = sorted_values[min(n-1, ceil(0.95 * n) - 1)]
  This matches the convention already used in app/eval/speed.py.
  For n=5: p50=values[2] (3rd), p95=values[4] (5th/last).

Usage:
    python -m app.eval.ask_latency [--log PATH] [--last N]
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional

# Default log path: resolved from the repo root (three levels up from this file:
#   app/eval/ask_latency.py  ->  app/  ->  backend/  ->  <repo_root>/)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_LOG = str(_REPO_ROOT / ".local" / "logs" / "events.jsonl")


# ---------------------------------------------------------------------------
# Pure aggregation
# ---------------------------------------------------------------------------

def _percentile_nearest(values: list[float], p: float) -> float:
    """Nearest-rank (ceiling) percentile on a *sorted* list."""
    n = len(values)
    if n == 0:
        return 0.0
    # ceil(p * n) gives 1-indexed rank; subtract 1 for 0-indexed; clamp to last
    idx = min(n - 1, max(0, math.ceil(p * n) - 1))
    return float(values[idx])


def aggregate_stage_latencies(records: Iterable[dict]) -> Dict[str, dict]:
    """Aggregate per-stage latencies from an iterable of event dicts.

    Only processes records where ``kind == "ask_stage"`` and both ``stage``
    and ``latency_ms`` fields are present.  All other records are silently
    skipped.

    Returns a mapping of stage name -> {"count": int, "p50": float,
    "p95": float, "max": float}.
    """
    buckets: Dict[str, list[float]] = {}
    for rec in records:
        if rec.get("kind") != "ask_stage":
            continue
        stage = rec.get("stage")
        if not stage:
            continue
        latency = rec.get("latency_ms")
        if latency is None:
            continue
        buckets.setdefault(stage, []).append(float(latency))

    result: Dict[str, dict] = {}
    for stage, vals in buckets.items():
        vals.sort()
        n = len(vals)
        result[stage] = {
            "count": n,
            "p50": _percentile_nearest(vals, 0.50),
            "p95": _percentile_nearest(vals, 0.95),
            "max": float(vals[-1]),
        }
    return result


# ---------------------------------------------------------------------------
# JSONL reader
# ---------------------------------------------------------------------------

def read_ask_stage_records(
    path: str,
    last_n: Optional[int] = None,
) -> Iterator[dict]:
    """Stream ask_stage records from a JSONL channel.

    Aggregates the legacy global file (`path`), every per-day dated file and
    gzip-archived dated file beside it, and all per-user subdir files
    (`<log_dir>/*/<basename>`, same dated/gz expansion) — see
    ``log_reader.expand_channel_paths``. Skips malformed/blank lines and
    records where ``kind != "ask_stage"``. Empty iterator if nothing exists.

    Args:
        path:   Path to the global events JSONL file (default:
                .local/logs/events.jsonl). Per-user subdirs beside it are
                aggregated automatically.
        last_n: If given, only yield the last N matching records (after merge).
    """
    from app.services.log_reader import expand_channel_paths, read_lines

    parsed: list[dict] = []
    for p in expand_channel_paths(Path(path)):
        # read_lines (log_reader.py) is the shared defensive reader (mirrors
        # scripts/diag.py::_read_lines): it skips a path in the read-set that
        # is a directory (a subdir literally named like the log basename) or
        # missing/corrupt, and transparently gunzips archived dated files
        # (`events-YYYY-MM-DD.jsonl.gz`) instead of decoding their binary
        # bytes as UTF-8 text — a plain read_text(errors="replace") on a .gz
        # path decodes to noise that never parses as JSON below, so archived
        # days would silently vanish from the aggregation rather than error.
        for line in read_lines(p):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if rec.get("kind") != "ask_stage":
                continue
            parsed.append(rec)

    if last_n is not None:
        parsed = parsed[-last_n:]

    yield from parsed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_STAGE_ORDER = ["load_indexes", "score", "expand", "answer_llm", "total"]


def _print_table(stats: Dict[str, dict]) -> None:
    if not stats:
        print("no ask_stage events found")
        return

    # Deterministic order: known stages first, then any extras sorted
    known = [s for s in _STAGE_ORDER if s in stats]
    extras = sorted(s for s in stats if s not in _STAGE_ORDER)
    ordered = known + extras

    header = f"{'stage':<16}  {'count':>6}  {'P50 ms':>8}  {'P95 ms':>8}  {'max ms':>8}"
    sep = "-" * len(header)
    print(header)
    print(sep)
    for stage in ordered:
        s = stats[stage]
        print(
            f"{stage:<16}  {s['count']:>6}  "
            f"{s['p50']:>8.1f}  {s['p95']:>8.1f}  {s['max']:>8.1f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report per-stage ask P50/P95 latency from events.jsonl"
    )
    parser.add_argument(
        "--log",
        default=_DEFAULT_LOG,
        help=f"Path to events JSONL file (default: {_DEFAULT_LOG})",
    )
    parser.add_argument(
        "--last",
        type=int,
        default=None,
        metavar="N",
        help="Only analyse the most recent N ask_stage records",
    )
    args = parser.parse_args()

    records = list(read_ask_stage_records(args.log, last_n=args.last))
    stats = aggregate_stage_latencies(records)
    _print_table(stats)


if __name__ == "__main__":
    main()
