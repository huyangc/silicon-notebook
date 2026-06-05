"""Read-only debug endpoints for viewing the JSONL log channels.

Gated by `debug_logs_enabled` (every endpoint 404s when off). Channels are
validated against `log_reader.CHANNELS` (no path traversal). Never writes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.config import Settings, get_settings
from app.core.event_logging import _ROOT_DIR
from app.services import log_reader

router = APIRouter(prefix="/debug/logs", tags=["debug"])


def require_enabled(settings: Settings = Depends(get_settings)) -> Settings:
    if not getattr(settings, "debug_logs_enabled", False):
        raise HTTPException(status_code=404, detail="debug logs disabled")
    return settings


def _channel_path(settings: Settings, channel: str) -> Path:
    filename = log_reader.CHANNELS.get(channel)
    if filename is None:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")
    log_dir = Path(settings.event_log_dir)
    if not log_dir.is_absolute():
        log_dir = _ROOT_DIR / log_dir
    return log_dir / filename


@router.get("")
def list_channels(settings: Settings = Depends(require_enabled)):
    out = []
    for name, filename in log_reader.CHANNELS.items():
        path = _channel_path(settings, name)
        exists = path.exists()
        count = 0
        if exists:
            with path.open("r", encoding="utf-8") as fh:
                count = sum(1 for line in fh if line.strip())
        out.append({"name": name, "file": filename, "exists": exists, "count": count})
    return {"channels": out}


@router.get("/{channel}")
def list_records(
    channel: str,
    settings: Settings = Depends(require_enabled),
    limit: int = Query(200, ge=1, le=2000),
    before: Optional[int] = None,
    since: Optional[int] = None,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    model: Optional[str] = None,
    q: Optional[str] = None,
):
    path = _channel_path(settings, channel)
    file_exists = path.exists()
    records, malformed = log_reader.load_records(path)
    filtered = log_reader.filter_records(records, kind=kind, status=status, model=model, q=q)
    stats = log_reader.compute_stats(records, filtered, malformed)
    filtered_desc = sorted(filtered, key=lambda r: r.get("seq", -1), reverse=True)
    page, has_more = log_reader.paginate(filtered_desc, before=before, since=since, limit=limit)
    newest_seq = filtered_desc[0]["seq"] if filtered_desc else None
    return {
        "channel": channel,
        "file_exists": file_exists,
        "records": [log_reader.to_summary(r) for r in page],
        "stats": stats,
        "has_more": has_more,
        "newest_seq": newest_seq,
    }


@router.get("/{channel}/{record_id}")
def get_record(
    channel: str,
    record_id: str,
    settings: Settings = Depends(require_enabled),
):
    path = _channel_path(settings, channel)
    records, _ = log_reader.load_records(path)
    matches = [r for r in records if r.get("id") == record_id]
    if not matches:
        raise HTTPException(status_code=404, detail=f"record not found: {record_id}")
    return matches[-1]
