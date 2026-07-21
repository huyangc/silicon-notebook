#!/usr/bin/env python3
"""Bounded, metadata-only base-reference recall diagnosis.

The source SQLite files are never opened through SQLite.  ``diag_db`` pins the
source with ``O_NOATIME``, takes a bounded copy under a non-blocking shared
lock, and runs this command's fixed aggregate projection against that owned
snapshot.  No application module, repository, migration, model, or retrieval
path is loaded.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import diag_common
from diag_db import REPORT_LIMIT_BYTES, collect_db_evidence


_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_DB = _REPO_ROOT / ".local" / "silicon_notebook.db"
_MAX_MOUNTS_RENDERED = 64
_SAFE_CATEGORIES = frozenset(
    {
        "active_rollback_journal",
        "busy",
        "corrupt",
        "deadline",
        "evidence_limit",
        "interrupted",
        "locked",
        "missing",
        "missing_schema",
        "noatime_unavailable",
        "permission",
        "reference_limit",
        "row_limit",
        "snapshot_byte_limit",
        "source_changed",
        "unavailable",
        "unsafe_diagnostics",
        "unsafe_file",
    }
)


def _bounded_int(value: Any) -> int:
    try:
        return max(0, min(int(value), 10**15))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_token(value: Any, allowed: Sequence[str], fallback: str) -> str:
    text = str(value or "")
    return text if text in allowed else fallback


def _append_categories(lines: list[str], evidence: Mapping[str, Any]) -> None:
    categories = []
    degraded = evidence.get("degraded")
    if isinstance(degraded, list):
        for row in degraded[:64]:
            if not isinstance(row, Mapping):
                continue
            category = str(row.get("category") or "")
            if category in _SAFE_CATEGORIES and category not in categories:
                categories.append(category)
    if categories:
        lines.append("degraded=" + ",".join(f"category={item}" for item in categories))
    else:
        lines.append("degraded=none")


def _truncate_report(lines: Sequence[str], limit_bytes: int) -> str:
    limit = max(1024, min(int(limit_bytes), REPORT_LIMIT_BYTES))
    result: list[str] = []
    used = 0
    for line in lines:
        clean = str(line).replace("\r", " ").replace("\n", " ")
        encoded = (clean + "\n").encode("utf-8", "strict")
        if used + len(encoded) > limit:
            marker = "[report truncated]\n".encode("utf-8")
            if used + len(marker) <= limit:
                result.append("[report truncated]")
            break
        result.append(clean)
        used += len(encoded)
    report = "\n".join(result) + "\n"
    while len(report.encode("utf-8")) > limit and result:
        result.pop()
        report = "\n".join(result) + "\n"
    return report


def render_base_recall_report(
    evidence: Mapping[str, Any], *, limit_bytes: int = REPORT_LIMIT_BYTES
) -> str:
    """Render one explicit-field report; arbitrary evidence text is never echoed."""

    recall = evidence.get("base_recall")
    if not isinstance(recall, Mapping):
        recall = {}
    status = _safe_token(evidence.get("status"), ("ok", "degraded"), "degraded")
    selected = bool(recall.get("selected"))
    selection = _safe_token(
        recall.get("selection"),
        ("operator", "latest_report", "first_personal", "first", "none"),
        "none",
    )
    notebook = "notebook#1" if selected else "none"
    lines = [
        "silicon-notebook base-recall metadata report",
        f"status={status} evidence_complete={str(bool(evidence.get('evidence_complete'))).lower()}",
        f"selection={selection} notebook={notebook}",
        (
            "notebooks_total="
            f"{_bounded_int(recall.get('notebook_count'))} "
            f"public_bases={_bounded_int(recall.get('public_base_count'))}"
        ),
        (
            "mounts_total="
            f"{_bounded_int(recall.get('mounts_total'))} "
            f"active={_bounded_int(recall.get('mounts_active'))} "
            f"inactive={_bounded_int(recall.get('mounts_inactive'))}"
        ),
        (
            "active_notebook_metadata "
            f"kg_objects={_bounded_int(recall.get('active_kg_objects'))} "
            f"embeddings={_bounded_int(recall.get('active_embeddings'))}"
        ),
    ]
    mounted = recall.get("mounted_bases")
    rendered = 0
    if isinstance(mounted, list):
        for index, row in enumerate(mounted[:_MAX_MOUNTS_RENDERED], 1):
            if not isinstance(row, Mapping):
                continue
            active = bool(row.get("active"))
            tier = _safe_token(row.get("tier"), ("base", "personal", "unknown"), "unknown")
            reason = _safe_token(
                row.get("inactive_reason"),
                ("", "copying", "not_authorized"),
                "unknown",
            )
            lines.append(
                f"mount base#{index} tier={tier} active={str(active).lower()} "
                f"inactive_reason={reason or 'none'} "
                f"kg_objects={_bounded_int(row.get('kg_objects'))} "
                f"embeddings={_bounded_int(row.get('embeddings'))} "
                f"chunks={_bounded_int(row.get('chunks'))} "
                f"relations={_bounded_int(row.get('relations'))}"
            )
            rendered += 1
    lines.append(f"mount_rows_rendered={rendered}")

    latest = recall.get("latest_report")
    if not isinstance(latest, Mapping):
        latest = {}
    lines.append(
        "latest_done_report_metadata "
        f"exists={str(bool(latest.get('exists'))).lower()} "
        f"references={_bounded_int(latest.get('references_total'))} "
        f"base_tier={_bounded_int(latest.get('base_tier_references'))} "
        f"personal_tier={_bounded_int(latest.get('personal_tier_references'))} "
        f"mounted_base={_bounded_int(latest.get('mounted_base_references'))} "
        f"malformed={_bounded_int(latest.get('malformed_references'))}"
    )
    _append_categories(lines, evidence)
    lines.extend(
        (
            "interpretation:",
            "- mounts_active=0: verify the notebook's reference-library selection.",
            "- active mounts with zero KG objects/embeddings: inspect ingestion and indexing separately.",
            "- mounted_base references below base_tier references: some cited base evidence was not from an active mount.",
            "privacy=metadata-only; no query, content, filename, raw identifier, path, or exception is emitted.",
        )
    )
    return _truncate_report(lines, limit_bytes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect bounded base-reference metadata through a source-side-effect-free "
            "SQLite snapshot."
        )
    )
    parser.add_argument(
        "active_notebook_id",
        nargs="?",
        help="legacy positional notebook id (never printed)",
    )
    parser.add_argument(
        "legacy_query",
        nargs="?",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--db", default=str(_DEFAULT_DB), help="source SQLite path")
    parser.add_argument("--notebook-id", help="notebook id to select (never printed)")
    parser.add_argument("--deadline-seconds", type=float, default=4.0)
    parser.add_argument("--output-limit-kib", type=int, default=32)
    return parser


def _main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    notebook_id = args.notebook_id or args.active_notebook_id
    evidence = collect_db_evidence(
        os.path.abspath(os.fspath(args.db)),
        notebook_id=notebook_id,
        deadline_seconds=args.deadline_seconds,
        projection="base-recall",
    )
    limit = max(1, min(int(args.output_limit_kib), 32)) * 1024
    sys.stdout.write(render_base_recall_report(evidence, limit_bytes=limit))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    return diag_common.run_copy_safe(lambda: _main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
