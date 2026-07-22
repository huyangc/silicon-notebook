"""Read-only helpers for the debug log viewer.

Pure functions over a JSONL log channel: load lines into dicts (each tagged
with a stable `seq` = line index, since logs are append-only), filter, search,
summarize, compute stats, and paginate. No FastAPI / IO side effects beyond
reading the given file. Best-effort: malformed lines are skipped and counted.
"""

from __future__ import annotations

import gzip
import json
import re
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Channel name -> filename. The HTTP layer validates against this allowlist.
CHANNELS: Dict[str, str] = {
    "llm": "llm.jsonl",
    "events": "events.jsonl",
    "requests": "requests.jsonl",
}

# 正则表达式：日期参数验证与提取。两个按天正则锚定匹配"channel 前缀截掉后剩下的
# 整段"（调用方先用 glob(f"{channel}-*.jsonl[.gz]") 保证前缀字面匹配、再切片去掉
# 前缀），而不是在完整文件名里 search 日期子串——否则像 `events-foo-2026-07-21.jsonl`
# 这种文件会被 search 命中"-2026-07-21.jsonl"子串，误当成 channel `events` 的按天
# 文件（真实 stem 其实是 `events-foo`）。
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLAIN_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
_GZ_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")

# 按天分文件读取窗口的上限（防止单次窗口读取吃满内存/耗时过长）。
MAX_RECORDS_PER_WINDOW = 50_000
MAX_TAIL_BYTES = 32 * 1024 * 1024


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


def _dated_paths_in_dir(d: Path, channel: str) -> List[Path]:
    """目录 d 下某 channel 的按天文件：明文 `<channel>-YYYY-MM-DD.jsonl` 与归档后
    的 `<channel>-YYYY-MM-DD.jsonl.gz`（event_logging.py 的 _target_path_for_day /
    _gzip_day_file 两种产物），按日期升序。复用 available_days 已用的两个正则，
    与之保持同一套「什么算按天文件」的判定——都是先用 glob 保证字面前缀
    `<channel>-` 匹配，再对切掉前缀后的剩余部分做锚定 match，因此 stem 必须
    整段等于 channel，不会把 `<channel>-<其它东西>-YYYY-MM-DD.jsonl` 误收进来。"""
    prefix = f"{channel}-"
    found: List[Tuple[str, str, Path]] = []
    for p in d.glob(f"{channel}-*.jsonl"):
        m = _PLAIN_DATE_RE.match(p.name[len(prefix):])
        if m:
            found.append((m.group(1), p.name, p))
    for p in d.glob(f"{channel}-*.jsonl.gz"):
        m = _GZ_DATE_RE.match(p.name[len(prefix):])
        if m:
            found.append((m.group(1), p.name, p))
    found.sort(key=lambda t: (t[0], t[1]))
    return [p for _, _, p in found]


def expand_channel_paths(channel_file: Path) -> List[Path]:
    """给定全局 channel 文件路径（如 .../logs/events.jsonl），返回该 channel 在
    EventLogger 实际写入的全部文件（app/core/event_logging.py）：
      1) 该全局无日期文件本身（若存在，兼容历史日志 / 未跨过按天分文件上线日的
         部署），
      2) 全局目录下该 channel 的全部按天文件（明文 + 已归档 .gz，按日期升序 —
         一个实际部署的无日期文件在跨过第一个自然日后就不再更新，只看它会
         静默漏掉几乎全部数据），
      3) 所有 per-user 子目录下 1)+2) 的同构文件（按子目录路径排序）。
    供 eval / 离线聚合读取所有用户、所有日子的日志。不递归、不混入其它 channel。
    返回的路径可能是 .gz —— 读取内容一律用本模块的 read_lines()，不要自己
    read_text()/open()（那会把 gzip 的二进制字节当 UTF-8 硬解码）。
    """
    channel_file = Path(channel_file)
    log_dir = channel_file.parent
    name = channel_file.name
    channel = channel_file.stem
    out: List[Path] = []
    if channel_file.exists():
        out.append(channel_file)
    if not log_dir.exists():
        return out
    out.extend(_dated_paths_in_dir(log_dir, channel))
    for sub in sorted(log_dir.iterdir()):
        if not sub.is_dir():
            continue
        sub_file = sub / name
        if sub_file.exists():
            out.append(sub_file)
        out.extend(_dated_paths_in_dir(sub, channel))
    return out


def read_lines(path: Path) -> List[str]:
    """按行读取一个日志文件，明文与 .gz 归档透明：.gz 走 gzip 流式解压 + 文本
    模式，其余直接 read_text；两者都用 errors="replace" 容忍历史脏字节 /
    非 UTF-8 行，不因单个坏字节抛 UnicodeDecodeError 炸掉整个聚合。不存在 /
    是目录 / 截断或损坏的 gz 一律静默跳过（返回 []），不让调用方逐个 try/except。

    是 expand_channel_paths() 返回路径的标准读取方式——parse_llm_log
    (app/eval/speed.py) 与 read_ask_stage_records (app/eval/ask_latency.py)
    都经它，不再各自维护 read_text()/open()。与 scripts/diag.py 里的
    _read_lines 保持同一套行为；那边是纯 stdlib、不能 import app，因此维护一份
    独立镜像（不能直接 import 这里）。
    """
    try:
        if path.name.endswith(".gz"):
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                return fh.read().splitlines()
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (FileNotFoundError, IsADirectoryError, OSError, EOFError):
        return []


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
    """枚举目录中按天分文件的日志文件。返回日期列表（降序），末尾追加 'legacy'（若存在）。
    与 _dated_paths_in_dir 同一套 stem 精确匹配规则（见其 docstring）。"""
    days = set()
    prefix = f"{channel}-"
    if dir.exists():
        for p in dir.glob(f"{channel}-*.jsonl"):
            m = _PLAIN_DATE_RE.match(p.name[len(prefix):])
            if m:
                days.add(m.group(1))
        for p in dir.glob(f"{channel}-*.jsonl.gz"):
            m = _GZ_DATE_RE.match(p.name[len(prefix):])
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


def _parse_blob(blob: bytes, base_off: int, *, drop_partial_first: bool) -> Tuple[List[dict], int]:
    """把一段字节按 \n 切行，每行 seq=其在文件内的字节偏移(base_off + 段内起点)。
    drop_partial_first：若 base_off>0，首段可能是被截断的半行，丢弃。返回 (records, malformed)。"""
    records: List[dict] = []
    malformed = 0
    off = base_off
    segments = blob.split(b"\n")
    last = len(segments) - 1
    for i, seg in enumerate(segments):
        seg_off = off
        off += len(seg) + 1  # 该段后有一个 \n（末段无，多算 1 但不再使用）
        if drop_partial_first and i == 0:
            continue
        if i == last and seg == b"":
            continue  # 末尾 \n 之后的空段
        s = seg.strip()
        if not s:
            continue
        try:
            obj = json.loads(seg)
        except Exception:
            malformed += 1
            continue
        if isinstance(obj, dict):
            obj["seq"] = seg_off
            records.append(obj)
        else:
            malformed += 1
    return records, malformed


def _load_plain_window(path, *, since, before, max_records, max_bytes):
    if not path.exists():
        return [], 0, False
    size = path.stat().st_size
    if since is not None:
        with path.open("rb") as fh:
            fh.seek(since)
            blob = fh.read()
        recs, malformed = _parse_blob(blob, since, drop_partial_first=False)
        recs = [r for r in recs if r["seq"] > since]
        truncated = False
        if len(recs) > max_records:            # since 分支也收口:仅回最新 max_records + 回传截断
            recs = recs[-max_records:]
            truncated = True
        return recs, malformed, truncated
    end = before if before is not None else size
    start = max(0, end - max_bytes)
    with path.open("rb") as fh:
        if start > 0:
            fh.seek(start - 1)
            at_boundary = fh.read(1) == b"\n"   # start 紧跟 \n 之后 = 行首对齐,首段是完整行不该丢
            blob = fh.read(end - start)
        else:
            at_boundary = True
            fh.seek(start)
            blob = fh.read(end - start)
    recs, malformed = _parse_blob(blob, start, drop_partial_first=(start > 0 and not at_boundary))
    if before is not None:
        recs = [r for r in recs if r["seq"] < before]
    truncated = start > 0
    if len(recs) > max_records:
        recs = recs[-max_records:]
        truncated = True
    return recs, malformed, truncated


def _load_gz_window(path, *, since, before, max_records):
    """gz 不可变、不被轮询：流式解压逐行，seq=行索引（不可变故稳定）。since/before 在
    入 deque 前过滤，使窗口锚定在游标上（与明文分支一致）；deque(maxlen) 只保最新
    max_records 条，内存有界。truncated=通过过滤的有效行数超过 max_records（丢了更旧的）。"""
    if not path.exists():
        return [], 0, False
    buf = deque(maxlen=max_records)
    malformed = 0
    kept = 0  # 通过 since/before 过滤的有效记录数，用于判定是否真的截断（不含 malformed）
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for idx, raw in enumerate(fh):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                malformed += 1
                continue
            if not isinstance(obj, dict):
                malformed += 1
                continue
            if since is not None and idx <= since:
                continue
            if before is not None and idx >= before:
                continue
            obj["seq"] = idx
            kept += 1
            buf.append(obj)
    truncated = kept > max_records
    return list(buf), malformed, truncated


def load_day_window(path, is_gzip, *, since=None, before=None,
                    max_records=MAX_RECORDS_PER_WINDOW, max_bytes=MAX_TAIL_BYTES):
    if is_gzip:
        return _load_gz_window(path, since=since, before=before, max_records=max_records)
    return _load_plain_window(path, since=since, before=before,
                              max_records=max_records, max_bytes=max_bytes)


def read_record_at(path, offset):
    """明文日志按字节偏移直读一行(get_record 的 O(1) 详情路径,不受尾窗 32MB 限制)。
    offset 由列表返回的 seq(字节偏移)提供,必落在行首。越界/解析失败→None(调用方兜底)。"""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            line = fh.readline()
        obj = json.loads(line)
        if isinstance(obj, dict):
            obj["seq"] = offset
            return obj
    except Exception:
        return None
    return None
