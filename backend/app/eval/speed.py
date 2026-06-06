"""抽取速度:截片段真实抽取计时 + 公式外推。"""
from __future__ import annotations
import json, math
from statistics import median
from typing import Dict, List, Optional, Tuple

from app.services.kg_ingest import plan_window_size


def plan_windows(chars: int, workers: int, w_min: int, w_max: int) -> Tuple[int, int]:
    size = plan_window_size(chars, workers, w_min, w_max)
    n = math.ceil(chars / size) if size else 0
    return size, n


def estimate_extract_seconds(n_windows: int, effective_concurrency: int,
                             per_window_p50_s: float, fixed_overhead_s: float) -> float:
    conc = max(1, effective_concurrency)
    batches = math.ceil(n_windows / conc) if n_windows else 0
    return round(batches * per_window_p50_s + fixed_overhead_s, 2)


def parse_llm_log(path: str, since_ts: str) -> Dict[str, float]:
    lats: List[float] = []
    tokens = 0
    retries = 0
    try:
        raw = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return {"calls": 0, "retries": 0, "latency_p50_s": 0.0,
                "latency_p95_s": 0.0, "total_tokens": 0}
    for line in raw:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("kind") != "chat" or rec.get("ts", "") < since_ts:
            continue
        if rec.get("status") == "retry":
            retries += 1
            continue
        if rec.get("status") != "ok":
            continue
        lats.append(rec.get("latency_ms", 0) / 1000.0)
        tokens += (rec.get("usage") or {}).get("total_tokens", 0)
    lats.sort()

    def pct(p):
        if not lats:
            return 0.0
        return round(lats[min(len(lats) - 1, int(p * len(lats)))], 3)
    return {
        "calls": len(lats),
        "retries": retries,
        "latency_p50_s": round(median(lats), 3) if lats else 0.0,
        "latency_p95_s": pct(0.95),
        "total_tokens": tokens,
    }
