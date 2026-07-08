"""Read-only helpers for the debug log viewer.

Pure functions over a JSONL log channel: load lines into dicts (each tagged
with a stable `seq` = line index, since logs are append-only), filter, search,
summarize, compute stats, and paginate. No FastAPI / IO side effects beyond
reading the given file. Best-effort: malformed lines are skipped and counted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Channel name -> filename. The HTTP layer validates against this allowlist.
CHANNELS: Dict[str, str] = {
    "llm": "llm.jsonl",
    "events": "events.jsonl",
    "requests": "requests.jsonl",
}

# 正则表达式：日期参数验证与提取
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLAIN_DATE_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})\.jsonl$")
_GZ_DATE_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")


def load_records(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Return (records, malformed_count). Each record gets `seq` = 0-based line
    index. Blank lines are ignored (not malformed). Missing file -> ([], 0)."""
    records: List[Dict[str, Any]] = []
    malformed = 0
    if not path.exists():
        return records, malformed
    with path.open("r", encoding="utf-8") as fh:
        for i, raw in enumerate(fh):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                malformed += 1
                continue
            if isinstance(obj, dict):
                obj["seq"] = i
                records.append(obj)
            else:
                malformed += 1
    return records, malformed


def expand_channel_paths(channel_file: Path) -> List[Path]:
    """给定全局 channel 文件路径（如 .../logs/events.jsonl），返回:
      1) 该全局文件本身（若存在，兼容历史日志），
      2) 所有 per-user 子目录的同名文件（.../logs/*/events.jsonl，按路径排序）。
    供 eval / 离线聚合读取所有用户的日志。不递归、不混入其它 channel。"""
    channel_file = Path(channel_file)
    log_dir = channel_file.parent
    name = channel_file.name
    out: List[Path] = []
    if channel_file.exists():
        out.append(channel_file)
    if log_dir.exists():
        out.extend(sorted(log_dir.glob(f"*/{name}")))
    return out


def _text_blob(rec: Dict[str, Any]) -> str:
    parts: List[str] = []
    req = rec.get("request") or {}
    for m in req.get("messages") or []:
        parts.append(str(m.get("content", "")))
    parts.append(str(req.get("schema_hint", "")))
    resp = rec.get("response") or {}
    parts.append(str(resp.get("content", "")))
    parts.append(str(rec.get("error", "")))
    return "\n".join(parts)


def filter_records(
    records: List[Dict[str, Any]],
    *,
    kind: Optional[str] = None,
    status: Optional[str] = None,
    model: Optional[str] = None,
    q: Optional[str] = None,
) -> List[Dict[str, Any]]:
    out = records
    if kind:
        out = [r for r in out if r.get("kind") == kind]
    if status:
        out = [r for r in out if r.get("status") == status]
    if model:
        out = [r for r in out if r.get("model") == model]
    if q:
        needle = q.lower()
        out = [r for r in out if needle in _text_blob(r).lower()]
    return out


def _preview(rec: Dict[str, Any]) -> str:
    if rec.get("status") in ("error", "retry") and rec.get("error"):
        return str(rec["error"])[:200]
    kind = rec.get("kind")
    if kind == "chat":
        msgs = (rec.get("request") or {}).get("messages") or []
        for m in reversed(msgs):
            if m.get("role") == "user" and m.get("content"):
                return str(m["content"])[:200]
        if msgs:
            return str(msgs[-1].get("content", ""))[:200]
        return ""
    if kind == "embed":
        bits = []
        if rec.get("input_chars") is not None:
            bits.append(f"input_chars={rec['input_chars']}")
        if rec.get("dims") is not None:
            bits.append(f"dims={rec['dims']}")
        return " ".join(bits)
    return ""


def to_summary(rec: Dict[str, Any]) -> Dict[str, Any]:
    usage = rec.get("usage") or {}
    err = rec.get("error")
    return {
        "seq": rec.get("seq"),
        "id": rec.get("id"),
        "ts": rec.get("ts"),
        "kind": rec.get("kind"),
        "model": rec.get("model"),
        "status": rec.get("status"),
        "latency_ms": rec.get("latency_ms"),
        "total_tokens": usage.get("total_tokens"),
        "attempt": rec.get("attempt"),
        "error": (str(err)[:200] if err else None),
        "preview": _preview(rec),
    }


def _counts(records: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    c: Dict[str, int] = {}
    for r in records:
        v = r.get(key)
        if v is None:
            continue
        c[str(v)] = c.get(str(v), 0) + 1
    return c


def compute_stats(
    all_records: List[Dict[str, Any]],
    filtered: List[Dict[str, Any]],
    malformed: int,
) -> Dict[str, Any]:
    """count-类统计基于 filtered;facets(下拉可选项)基于 all_records(全量)。"""
    latencies = [r["latency_ms"] for r in filtered if isinstance(r.get("latency_ms"), (int, float))]
    total_tokens = sum((r.get("usage") or {}).get("total_tokens", 0) or 0 for r in filtered)
    return {
        "total": len(all_records),
        "filtered": len(filtered),
        "by_kind": _counts(filtered, "kind"),
        "by_status": _counts(filtered, "status"),
        "by_model": _counts(filtered, "model"),
        "total_tokens": total_tokens,
        "latency_ms": {
            "avg": round(sum(latencies) / len(latencies)) if latencies else 0,
            "max": max(latencies) if latencies else 0,
        },
        "malformed_lines": malformed,
        "facets": {
            "kinds": sorted(_counts(all_records, "kind").keys()),
            "statuses": sorted(_counts(all_records, "status").keys()),
            "models": sorted(_counts(all_records, "model").keys()),
        },
    }


def paginate(
    records_desc: List[Dict[str, Any]],
    *,
    before: Optional[int],
    since: Optional[int],
    limit: int,
) -> Tuple[List[Dict[str, Any]], bool]:
    """`records_desc` 已按 seq 降序。before→更旧(seq<before);since→更新(seq>since)。
    返回 (page, has_more);has_more 表示截断前还有更多旧记录。"""
    out = records_desc
    if since is not None:
        out = [r for r in out if r.get("seq", -1) > since]
    if before is not None:
        out = [r for r in out if r.get("seq", -1) < before]
    has_more = len(out) > limit
    return out[:limit], has_more


def valid_date_param(date: str) -> bool:
    """验证日期参数是否合法。接受 'legacy' 或 YYYY-MM-DD 格式。"""
    return date == "legacy" or bool(_DATE_RE.fullmatch(date or ""))


def available_days(dir: Path, channel: str) -> List[str]:
    """枚举目录中按天分文件的日志文件。返回日期列表（降序），末尾追加 'legacy'（若存在）。"""
    days = set()
    if dir.exists():
        for p in dir.glob(f"{channel}-*.jsonl"):
            m = _PLAIN_DATE_RE.search(p.name)
            if m:
                days.add(m.group(1))
        for p in dir.glob(f"{channel}-*.jsonl.gz"):
            m = _GZ_DATE_RE.search(p.name)
            if m:
                days.add(m.group(1))
    out = sorted(days, reverse=True)
    if (dir / f"{channel}.jsonl").exists():
        out.append("legacy")
    return out


def resolve_day_path(dir: Path, channel: str, date: str) -> Tuple[Path, bool]:
    """根据日期解析日志文件路径。优先级：plain > gz > legacy。
    返回 (path, is_gzipped)；文件不存在时返回默认路径（非 gz）。"""
    if date == "legacy":
        return dir / f"{channel}.jsonl", False
    plain = dir / f"{channel}-{date}.jsonl"
    if plain.exists():
        return plain, False
    gz = dir / f"{channel}-{date}.jsonl.gz"
    if gz.exists():
        return gz, True
    return plain, False
