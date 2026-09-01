#!/usr/bin/env python3
"""Aggregate content-free retrieval timings by indexed notebook size.

    python3 scripts/diag_retrieval_latency.py --since 24
    python3 scripts/diag_retrieval_latency.py --since 24 \
        --local /srv/silicon-notebook/.local \
        --index-root /srv/silicon-notebook/storage/kg_index

This command is read-only and stdlib-only.  It reads the existing ``events``
JSONL channel through ``diag_common`` (legacy, dated, gzip, and per-user files)
and the published scale-index manifests.  It never opens the product database
and never prints notebook ids, questions, source names, text, SQL, or errors.

The size bucket is the last published manifest's ``n_chunks``.  It deliberately
does not claim that this includes post-watermark delta; an ``unknown`` bucket is
kept when a timing event has no readable manifest.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import diag_common


_TIMING_FIELDS = {
    "chunk_ann": (
        "ann_prepare_ms",
        "ann_open_ms",
        "knn_ms",
        "delta_ms",
        "lexical_prepare_ms",
        "chunk_fts_ms",
        "hydrate_ms",
        "score_ms",
        "total_ms",
    ),
    "chunk_scale_index": ("scale_index_load_ms",),
    "_retrieve_scored": (
        "candidate_ms",
        "scale_index_ms",
        "kg_ann_open_ms",
        "kg_ann_knn_ms",
        "kg_delta_ms",
        "kg_lexical_ms",
        "kg_lexical_knn_ms",
        "kg_lexical_legacy_ms",
        "kg_lexical_short_fallback_ms",
        "hydrate_ms",
        "score_ms",
        "fold_ms",
        "total_ms",
    ),
}
_KG_LEXICAL_ROUTE_FIELDS = (
    "kg_lexical_term_count",
    "kg_lexical_knn_term_count",
    "kg_lexical_direct_legacy_term_count",
    "kg_lexical_short_fallback_term_count",
)
_RUN_KINDS = frozenset({
    "ask_chunk",
    "ask_reasoning",
    "ask_plugin_engine",
    "report_planning",
    "report_generation",
})
_MANIFEST_SCAN_CHARS = 64 * 1024
_MANIFEST_SCAN_OVERLAP = 256
_N_CHUNKS_PATTERN = re.compile(
    r'"n_chunks"\s*:\s*(-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)'
)


def _finite_number(value: Any) -> float | None:
    number = diag_common.finite_number(value)
    return float(number) if number is not None else None


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return float(ordered[index])


@dataclass
class Samples:
    values: list[float] = field(default_factory=list)

    def add(self, value: Any) -> None:
        number = _finite_number(value)
        if number is not None:
            self.values.append(number)

    def summary(self) -> tuple[int, float, float, float]:
        if not self.values:
            return 0, 0.0, 0.0, 0.0
        return (
            len(self.values),
            _percentile(self.values, 0.50),
            _percentile(self.values, 0.95),
            max(self.values),
        )


def _stream_manifest_chunk_count(manifest_path: Path) -> int | None:
    """Find generated manifest ``n_chunks`` with bounded memory.

    Production manifests embed every watermark source id and can be multiple
    MiB.  A whole-file size cutoff therefore rejects the largest, most useful
    notebooks; reading the whole file before checking the cutoff is not a
    memory bound either.  The artifact writer emits one top-level numeric
    ``n_chunks`` field, so scan incrementally with enough overlap for a key and
    JSON number split across chunks.  No source id or other payload is retained.
    """
    tail = ""
    with manifest_path.open("r", encoding="utf-8", errors="strict") as handle:
        while True:
            chunk = handle.read(_MANIFEST_SCAN_CHARS)
            if not chunk:
                return None
            window = tail + chunk
            match = _N_CHUNKS_PATTERN.search(window)
            if match is not None:
                value = _finite_number(match.group(1))
                if value is None or value < 0 or not float(value).is_integer():
                    return None
                return int(value)
            tail = window[-_MANIFEST_SCAN_OVERLAP:]


def load_indexed_chunk_counts(index_root: Path) -> dict[str, int]:
    """Read only constant-memory manifest metadata, keyed by notebook id."""
    counts: dict[str, int] = {}
    if not index_root.is_dir():
        return counts
    for manifest_path in sorted(index_root.glob("*/manifest.json")):
        try:
            value = _stream_manifest_chunk_count(manifest_path)
            if value is not None:
                counts[manifest_path.parent.name] = value
        except (OSError, UnicodeError):
            continue
    return counts


def _bucket_label(
    notebook_id: Any,
    indexed_chunks: dict[str, int],
    medium_chunks: int,
    large_chunks: int,
) -> str:
    count = indexed_chunks.get(str(notebook_id or ""))
    if count is None:
        return "unknown"
    if count < medium_chunks:
        return f"small(<{medium_chunks})"
    if count < large_chunks:
        return f"medium({medium_chunks}-{large_chunks - 1})"
    return f"large(>={large_chunks})"


def _safe_status(value: Any) -> str:
    status = str(value or "unknown")
    return status if status in {
        "ok", "timeout", "failed_open", "skipped_circuit_open"
    } else "other"


def _safe_run_kind(value: Any) -> str:
    run_kind = str(value or "unknown")
    return run_kind if run_kind in _RUN_KINDS else "unknown"


def _format_samples(samples: Samples) -> str:
    count, p50, p95, maximum = samples.summary()
    return f"n={count:<6} p50={p50:>8.1f} p95={p95:>8.1f} max={maximum:>8.1f} ms"


def build_report(
    records: Iterable[dict[str, Any]],
    indexed_chunks: dict[str, int],
    *,
    medium_chunks: int = 100_000,
    large_chunks: int = 500_000,
) -> str:
    """Return a bounded aggregate report without rendering any identifiers."""
    leaf_latency: dict[str, Samples] = defaultdict(Samples)
    leaf_status: dict[str, Counter[str]] = defaultdict(Counter)
    component_latency: dict[tuple[str, str, str], Samples] = defaultdict(Samples)
    lexical_routes: dict[str, Counter[str]] = defaultdict(Counter)
    run_stats: dict[str, Counter[str]] = defaultdict(Counter)
    relevant = 0

    for event in records:
        kind = event.get("kind")
        if kind == "retrieval_run_stats":
            relevant += 1
            run_kind = _safe_run_kind(event.get("run_kind"))
            stats = run_stats[run_kind]
            stats["runs"] += 1
            for field_name in (
                "embedding_requests",
                "embedding_hits",
                "embedding_errors",
                "fanout_acquires",
                "fanout_waits",
                "fanout_wait_ms",
                "chunk_fts_timeouts",
                "chunk_fts_circuit_skips",
            ):
                value = _finite_number(event.get(field_name))
                if value is not None:
                    stats[field_name] += int(value)
            continue
        if kind != "ask_stage":
            continue
        site = str(event.get("site") or "")
        bucket = _bucket_label(
            event.get("notebook_id"), indexed_chunks, medium_chunks, large_chunks
        )
        if site == "chunk_fts":
            relevant += 1
            leaf_latency[bucket].add(event.get("chunk_fts_ms", event.get("latency_ms")))
            leaf_status[bucket][_safe_status(event.get("status"))] += 1
        fields = _TIMING_FIELDS.get(site)
        if fields is None:
            continue
        relevant += 1
        for field_name in fields:
            component_latency[(site, bucket, field_name)].add(event.get(field_name))
        if site == "_retrieve_scored" and any(
            field_name in event for field_name in _KG_LEXICAL_ROUTE_FIELDS
        ):
            route_stats = lexical_routes[bucket]
            route_stats["events"] += 1
            for field_name in _KG_LEXICAL_ROUTE_FIELDS:
                value = _finite_number(event.get(field_name))
                if value is not None and value >= 0 and value.is_integer():
                    route_stats[field_name] += int(value)

    lines = [
        "=== Retrieval latency distribution (content-free) ===",
        (
            "size_basis=published_manifest.n_chunks "
            f"medium_threshold={medium_chunks} large_threshold={large_chunks}"
        ),
        f"relevant_events={relevant} manifests={len(indexed_chunks)}",
    ]

    lines.append("\n[generic chunk FTS leaf by indexed size]")
    if not leaf_latency:
        lines.append("  (no site=chunk_fts events)")
    for bucket in sorted(leaf_latency):
        statuses = " ".join(
            f"{name}={count}" for name, count in sorted(leaf_status[bucket].items())
        )
        lines.append(f"  {bucket:<28} {_format_samples(leaf_latency[bucket])}  {statuses}")

    lines.append("\n[retrieval components by indexed size]")
    if not component_latency:
        lines.append("  (no component timing events)")
    current_group: tuple[str, str] | None = None
    for (site, bucket, field_name), samples in sorted(component_latency.items()):
        group = (site, bucket)
        if group != current_group:
            lines.append(f"  site={site} size={bucket}")
            current_group = group
        lines.append(f"    {field_name:<24} {_format_samples(samples)}")

    lines.append("\n[KG lexical routes by indexed size]")
    if not lexical_routes:
        lines.append("  (no adaptive-routing events)")
    for bucket in sorted(lexical_routes):
        stats = lexical_routes[bucket]
        lines.append(
            "  "
            f"{bucket:<28} events={stats['events']} "
            f"terms={stats['kg_lexical_term_count']} "
            f"knn_terms={stats['kg_lexical_knn_term_count']} "
            f"direct_legacy_terms={stats['kg_lexical_direct_legacy_term_count']} "
            "short_fallback_terms="
            f"{stats['kg_lexical_short_fallback_term_count']}"
        )

    lines.append("\n[retrieval-run totals by run kind]")
    if not run_stats:
        lines.append("  (no retrieval_run_stats events)")
    for run_kind in sorted(run_stats):
        stats = run_stats[run_kind]
        lines.append(
            "  "
            f"{run_kind:<20} runs={stats['runs']} "
            f"fts_timeouts={stats['chunk_fts_timeouts']} "
            f"circuit_skips={stats['chunk_fts_circuit_skips']} "
            f"fanout_waits={stats['fanout_waits']} "
            f"fanout_wait_ms={stats['fanout_wait_ms']} "
            f"embed_requests={stats['embedding_requests']} "
            f"embed_hits={stats['embedding_hits']} "
            f"embed_errors={stats['embedding_errors']}"
        )

    lines.extend([
        "",
        "notes:",
        "  - unknown means the event's notebook had no readable local scale manifest.",
        "  - manifest n_chunks excludes sources added after its watermark.",
        "  - leaf size buckets combine Ask/report traffic; run totals remain separated by run kind.",
    ])
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local", type=Path, default=Path(".local"),
        help="deployment .local directory (default: .local)",
    )
    parser.add_argument(
        "--index-root", type=Path, default=None,
        help="scale index root (default: <local>/storage/kg_index)",
    )
    parser.add_argument("--since", type=float, default=24.0, help="lookback hours")
    parser.add_argument("--max-events", type=int, default=50_000)
    parser.add_argument(
        "--max-input-mb",
        type=int,
        default=0,
        help="decoded log input cap in MiB (default: 0, read the complete dated window)",
    )
    parser.add_argument("--medium-chunks", type=int, default=100_000)
    parser.add_argument("--large-chunks", type=int, default=500_000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        args.since <= 0
        or args.max_events <= 0
        or args.max_input_mb < 0
        or args.medium_chunks <= 0
        or args.large_chunks <= args.medium_chunks
    ):
        print(
            "error: require since/max-events/medium-chunks > 0, "
            "max-input-mb >= 0, and large > medium"
        )
        return 2
    index_root = args.index_root or args.local / "storage" / "kg_index"
    channel = diag_common.read_channel(
        args.local / "logs",
        "events",
        since_hours=float(args.since),
        limit=int(args.max_events),
        max_input_bytes=(
            None if args.max_input_mb == 0 else int(args.max_input_mb) * 1024 * 1024
        ),
    )
    print(
        build_report(
            channel.records,
            load_indexed_chunk_counts(index_root),
            medium_chunks=int(args.medium_chunks),
            large_chunks=int(args.large_chunks),
        ),
        end="",
    )
    stats = channel.stats
    print(
        "log_scan="
        f"files:{stats.files} matched:{stats.matched} malformed:{stats.malformed} "
        f"duplicates:{stats.duplicates} retained:{stats.retained} "
        f"truncated:{str(stats.truncated).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
