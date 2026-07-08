"""Read-only debug endpoints for viewing the JSONL log channels.

Gated by `debug_logs_enabled` (every endpoint 404s when off) AND authenticated:
- events / llm are per-user — a normal user only sees their own
  (`<log_dir>/<user_id>/<file>`); an admin may pass `?owner=<user_id>` (or
  `_system`) to read any user's.
- requests is a single global file (not per-user) and is admin-only.
Channels are validated against `log_reader.CHANNELS`; `owner` is validated by
`is_safe_owner` (no path traversal). Never writes.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import get_current_user
from app.core.config import Settings, get_settings
from app.core.event_logging import _ROOT_DIR, is_safe_owner
from app.models.schemas import UserProfile
from app.services import log_reader

router = APIRouter(prefix="/debug/logs", tags=["debug"])

# 全局单文件 channel（不按用户分目录），仅 admin 可读。
_GLOBAL_CHANNELS = {"requests"}


def require_enabled(settings: Settings = Depends(get_settings)) -> Settings:
    if not getattr(settings, "debug_logs_enabled", False):
        raise HTTPException(status_code=404, detail="debug logs disabled")
    return settings


def _resolve_owner(channel: str, user: UserProfile, requested: Optional[str]) -> Optional[str]:
    """返回该请求应读取的 owner 子目录名；None = 全局文件（requests）。无权 → 403。"""
    is_admin = user.role == "admin"
    if channel in _GLOBAL_CHANNELS:
        if not is_admin:
            raise HTTPException(status_code=403, detail="admin only")
        return None
    # per-user channel（events / llm）
    if requested is None or requested == user.id:
        return user.id
    if not is_admin:
        raise HTTPException(status_code=403, detail="forbidden")
    if not is_safe_owner(requested):
        raise HTTPException(status_code=404, detail=f"unknown owner: {requested}")
    return requested


def _channel_dir(settings: Settings, owner: Optional[str]) -> Path:
    """返回该 channel/owner 对应的日志目录（按天分文件的文件都落在这里）。"""
    log_dir = Path(settings.event_log_dir)
    if not log_dir.is_absolute():
        log_dir = _ROOT_DIR / log_dir
    return log_dir if owner is None else log_dir / owner


@router.get("")
def list_channels(
    user: UserProfile = Depends(get_current_user),
    settings: Settings = Depends(require_enabled),
    owner: Optional[str] = Query(None),
):
    out = []
    for name, filename in log_reader.CHANNELS.items():
        try:
            target = _resolve_owner(name, user, owner)
        except HTTPException:
            continue  # 无权的 channel（如非 admin 的 requests）不列出
        path_dir = _channel_dir(settings, target)
        today = datetime.now().strftime("%Y-%m-%d")
        today_file = path_dir / f"{name}-{today}.jsonl"
        out.append({"name": name, "file": filename,
                    "exists": today_file.exists(),
                    "bytes": today_file.stat().st_size if today_file.exists() else 0})
    return {"channels": out}


@router.get("/{channel}/days")
def list_days(
    channel: str,
    user: UserProfile = Depends(get_current_user),
    settings: Settings = Depends(require_enabled),
    owner: Optional[str] = Query(None),
):
    if channel not in log_reader.CHANNELS:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")
    target = _resolve_owner(channel, user, owner)
    return {"channel": channel, "days": log_reader.available_days(_channel_dir(settings, target), channel)}


@router.get("/{channel}")
def list_records(
    channel: str,
    user: UserProfile = Depends(get_current_user),
    settings: Settings = Depends(require_enabled),
    owner: Optional[str] = Query(None),
    date: Optional[str] = Query(None, description="YYYY-MM-DD；缺省=今天"),
    limit: int = Query(200, ge=1, le=2000),
    before: Optional[int] = Query(None, description="Return records with seq < before (older page)"),
    since: Optional[int] = Query(None, description="Return records with seq > since (newer; for polling)"),
    kind: Optional[str] = None,
    status: Optional[str] = None,
    model: Optional[str] = None,
    q: Optional[str] = None,
):
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    if not log_reader.valid_date_param(date):
        raise HTTPException(status_code=400, detail=f"bad date: {date}")
    target = _resolve_owner(channel, user, owner)
    if channel not in log_reader.CHANNELS:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")
    path, is_gzip = log_reader.resolve_day_path(_channel_dir(settings, target), channel, date)
    records, malformed, truncated = log_reader.load_day_window(
        path, is_gzip, since=since, before=before)
    filtered = log_reader.filter_records(records, kind=kind, status=status, model=model, q=q)
    stats = log_reader.compute_stats(records, filtered, malformed)
    filtered_desc = sorted(filtered, key=lambda r: r.get("seq", -1), reverse=True)
    page, has_more = log_reader.paginate(filtered_desc, before=before, since=since, limit=limit)
    newest_seq = filtered_desc[0]["seq"] if filtered_desc else None
    return {
        "channel": channel,
        "date": date,
        "file_exists": path.exists(),
        "records": [log_reader.to_summary(r) for r in page],
        "stats": stats,
        "has_more": has_more or truncated,
        "truncated": truncated,
        "newest_seq": newest_seq,
    }


@router.get("/{channel}/{record_id}")
def get_record(
    channel: str,
    record_id: str,
    user: UserProfile = Depends(get_current_user),
    settings: Settings = Depends(require_enabled),
    owner: Optional[str] = Query(None),
    date: Optional[str] = Query(None, description="YYYY-MM-DD；缺省=今天"),
    seq: Optional[int] = Query(None, description="明文文件内的字节偏移；跳过按 id 查找"),
):
    if channel not in log_reader.CHANNELS:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    if not log_reader.valid_date_param(date):
        raise HTTPException(status_code=400, detail=f"bad date: {date}")
    target = _resolve_owner(channel, user, owner)
    path, is_gzip = log_reader.resolve_day_path(_channel_dir(settings, target), channel, date)
    # 明文文件:seq=字节偏移 → 直接 seek 单行 O(1),不受尾窗 32MB 限制(修早期记录误 404 + 免每次重载)
    if seq is not None and not is_gzip and path.exists():
        rec = log_reader.read_record_at(path, seq)
        if rec is not None and rec.get("id") == record_id:
            return rec
    # 兜底(gz / 无 seq / seek 未命中):当天窗口内按 id 找(有界)
    records, _, _ = log_reader.load_day_window(path, is_gzip, since=None, before=None)
    matches = [r for r in records if r.get("id") == record_id]
    if not matches:
        raise HTTPException(status_code=404, detail=f"record not found: {record_id}")
    return matches[-1]
