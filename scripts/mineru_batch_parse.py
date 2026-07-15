#!/usr/bin/env python3
"""MinerU 批量解析工具（部署侧独立脚本）。

把一个目录下的 PDF 通过内网 MinerU server 的**异步** `/tasks` API 批量转成
Markdown，可跨多台 server 并发、按各台 `/health` 的 `max_concurrent_requests`
自动限流。这是对一版更粗糙脚本（`curl` 子进程、硬编码配置、丢错误体、
`failed.txt` 是唯一账本）的干净重写。

**独立性**：本脚本不 import 任何 `app.*`（backend 包），只依赖标准库 +
`requests`（backend/requirements.txt 已有该依赖）。在部署机上直接跑：

    python scripts/mineru_batch_parse.py --src /data/pdfs --out /data/md

配置优先级：命令行参数 > 环境变量（`MINERU_BATCH_*`，可放 `.env`）> 内置默认值。
内置默认值刻意保持通用占位（`http://mineru-host:8000`），真实内网地址/路径
只应出现在未提交的本地 `.env` 里。

服务端契约（保持与已跑通的内网 server 一致，不可更改字段名）：
    - 提交：POST {server}/tasks，multipart file 字段 `files`=PDF，
      表单字段 backend/lang_list/formula_enable/table_enable/return_md=true
      → 202，JSON 含 task_id
    - 轮询：GET {server}/tasks/{task_id} → {"status": ..., "error": ...}
      status ∈ {queued,running,completed,failed}（也兼容 pending/processing/
      success；未知的非终态一律当"还在跑"处理）
    - 取结果：GET {server}/tasks/{task_id}/result → {"results": {...}}
    - 健康检查：GET {server}/health → {"status": ..., "max_concurrent_requests": N}
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

# 轮询/退避终态。process_file/run 里一律经 _sleep(...) 调用，测试可 monkeypatch
# 成 no-op（同 mineru_cloud_client.py 的 _sleep 接缝）。
_TERMINAL_OK = {"completed", "success"}
_TERMINAL_FAIL = {"failed"}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# .env 加载（零依赖，真实环境变量优先）
# ---------------------------------------------------------------------------


def _parse_dotenv_value(raw_value: str) -> str:
    """解析 `KEY=VALUE` 右侧的原始字符串：去首尾空白 → 处理引号 / 行内注释。

    - 带引号（`"..."`/`'...'`）：取到匹配的闭合引号为止，引号内的 `#` 一律
      保留、不当注释处理；没有闭合引号时退化为只剥掉那个引号字符。
    - 不带引号：先去掉左侧空白，若整体以 `#` 开头，说明这一段全是注释（值
      为空字符串）；否则只把「空格+`#`」当作行内注释起点并截断——`#` 前必须
      紧邻空白，因此贴在其他字符上的 `#`（如 URL fragment
      `http://h/x#frag`、密码里的 `#`）不受影响。
    """
    value = raw_value.strip()
    if not value:
        return value
    if value[0] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end != -1 else value[1:]
    if value.startswith("#"):
        return ""
    idx = value.find(" #")
    if idx != -1:
        value = value[:idx]
    return value.strip()


def load_dotenv(path) -> None:
    """极简 .env 解析：KEY=VALUE，忽略空行/`#`注释，去除首尾引号/行内注释。

    只在 os.environ 里**尚未设置**该 key 时才写入——真实环境变量始终优先。
    文件不存在则静默跳过（`.env` 是可选的便利文件，不是必需配置源）。
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, raw_value = stripped.partition("=")
        key = key.strip()
        value = _parse_dotenv_value(raw_value)
        if key and key not in os.environ:
            os.environ[key] = value


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class Config:
    servers: List[str] = field(default_factory=lambda: ["http://mineru-host:8000"])
    src_dir: str = ""
    out_dir: str = ""
    backend: str = "pipeline"
    lang: str = "ch"
    formula_enable: bool = True
    table_enable: bool = True
    concurrency_per_server: int = 0  # 0 = 自动，取各 server /health 的 max_concurrent_requests
    poll_interval: int = 10
    max_poll_seconds: int = 1800
    retry_max: int = 3
    submit_timeout: int = 120
    result_timeout: int = 120
    manifest: str = ""  # 空 → {out_dir}/_manifest.jsonl

    # 以下字段只来自命令行（无对应 env key）
    list_file: str = ""
    dry_run: bool = False
    only_failed: bool = False
    limit: int = 0

    @property
    def manifest_path(self) -> Path:
        return Path(self.manifest) if self.manifest else Path(self.out_dir) / "_manifest.jsonl"

    @classmethod
    def from_env_and_args(cls, args: argparse.Namespace) -> "Config":
        def env(key: str, default: str) -> str:
            return os.environ.get(key, default)

        servers_raw = args.servers or env("MINERU_BATCH_SERVERS", "http://mineru-host:8000")
        servers = [s.strip() for s in servers_raw.split(",") if s.strip()]

        src_dir = args.src or env("MINERU_BATCH_SRC_DIR", "")
        out_dir = args.out or env("MINERU_BATCH_OUT_DIR", "")

        if not src_dir or not out_dir:
            print(
                "错误: 必须提供源/输出目录（--src/--out 或 "
                "MINERU_BATCH_SRC_DIR/MINERU_BATCH_OUT_DIR）",
                file=sys.stderr,
            )
            sys.exit(2)
        if not servers:
            print("错误: MINERU_BATCH_SERVERS 不能为空", file=sys.stderr)
            sys.exit(2)

        manifest = env("MINERU_BATCH_MANIFEST", "").strip()

        return cls(
            servers=servers,
            src_dir=src_dir,
            out_dir=out_dir,
            backend=env("MINERU_BATCH_BACKEND", "pipeline"),
            lang=env("MINERU_BATCH_LANG", "ch"),
            formula_enable=_as_bool(env("MINERU_BATCH_FORMULA_ENABLE", "true")),
            table_enable=_as_bool(env("MINERU_BATCH_TABLE_ENABLE", "true")),
            concurrency_per_server=int(env("MINERU_BATCH_CONCURRENCY_PER_SERVER", "0")),
            poll_interval=int(env("MINERU_BATCH_POLL_INTERVAL", "10")),
            max_poll_seconds=int(env("MINERU_BATCH_MAX_POLL_SECONDS", "1800")),
            retry_max=int(env("MINERU_BATCH_RETRY_MAX", "3")),
            submit_timeout=int(env("MINERU_BATCH_SUBMIT_TIMEOUT", "120")),
            result_timeout=int(env("MINERU_BATCH_RESULT_TIMEOUT", "120")),
            manifest=manifest,
            list_file=getattr(args, "list_file", None) or "",
            dry_run=bool(getattr(args, "dry_run", False)),
            only_failed=bool(getattr(args, "only_failed", False)),
            limit=int(args.limit) if getattr(args, "limit", None) else 0,
        )


# ---------------------------------------------------------------------------
# extract_md —— 健壮提取器（纯函数，无 I/O）
# ---------------------------------------------------------------------------


def _maybe_json(value):
    """若 value 是字符串则尝试 json.loads；解析失败返回 None。

    镜像 mineru_client.py::_coerce_content_list 的兜底风格：服务端有的版本
    把整段结果、或某个字段，序列化成了未解析的 JSON 字符串。
    """
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return value


def extract_md(result_payload) -> str:
    """从 `/tasks/{id}/result` 的响应里健壮地取出 markdown 正文。

    兼容形状：
      - 顶层可能是 JSON 字符串（先 json.loads）
      - `results` 可能是 dict（{key: item}）或 list（[item]）
      - 每个 item 的 markdown 字段可能叫 `md_content` 或 `md`
    取不到任何非空 markdown 时返回 ""。
    """
    payload = _maybe_json(result_payload)
    if not isinstance(payload, dict):
        return ""

    results = payload.get("results")
    if isinstance(results, dict):
        items = list(results.values())
    elif isinstance(results, list):
        items = list(results)
    elif results is not None:
        items = [results]
    else:
        items = []

    for item in items:
        item = _maybe_json(item)
        if isinstance(item, dict):
            md = item.get("md_content") or item.get("md")
            if md:
                return md

    # 兜底：顶层直接带 md_content/md（非 results 包裹的极简响应）
    top_md = payload.get("md_content") or payload.get("md")
    return top_md or ""


# ---------------------------------------------------------------------------
# MinerUServer —— 一台 server 的 HTTP 适配（session 注入，便于测试）
# ---------------------------------------------------------------------------


class MinerUServer:
    def __init__(
        self,
        base_url: str,
        session,
        *,
        capacity: int,
        submit_timeout: float,
        result_timeout: float,
        poll_interval: float,
        max_poll_seconds: float,
        form_fields: dict,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.capacity = max(1, int(capacity))
        self.submit_timeout = submit_timeout
        self.result_timeout = result_timeout
        self.poll_interval = poll_interval
        self.max_poll_seconds = max_poll_seconds
        self.form_fields = form_fields
        self.semaphore = threading.Semaphore(self.capacity)

    def _raise_for_status(self, resp) -> None:
        """非 2xx 时把响应体原文一并抛出——旧脚本最大的坑就是把错误体丢了。"""
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = getattr(resp, "text", "") or ""
            raise RuntimeError(f"{exc} | 响应体: {body}") from exc

    def health(self) -> dict:
        url = f"{self.base_url}/health"
        resp = self.session.get(url, timeout=self.submit_timeout)
        self._raise_for_status(resp)
        return resp.json() or {}

    def submit(self, pdf_path) -> str:
        url = f"{self.base_url}/tasks"
        with open(pdf_path, "rb") as fh:
            resp = self.session.post(
                url,
                files={"files": fh},
                data=self.form_fields,
                timeout=self.submit_timeout,
            )
        self._raise_for_status(resp)
        payload = resp.json() or {}
        task_id = payload.get("task_id")
        if not task_id:
            raise RuntimeError(f"MinerU 提交未返回 task_id: {resp.text}")
        return str(task_id)

    def status(self, task_id: str) -> Tuple[str, str]:
        url = f"{self.base_url}/tasks/{task_id}"
        resp = self.session.get(url, timeout=self.result_timeout)
        self._raise_for_status(resp)
        payload = resp.json() or {}
        return str(payload.get("status", "")), str(payload.get("error", ""))

    def result(self, task_id: str) -> dict:
        url = f"{self.base_url}/tasks/{task_id}/result"
        resp = self.session.get(url, timeout=self.result_timeout)
        self._raise_for_status(resp)
        return resp.json() or {}


# ---------------------------------------------------------------------------
# process_file —— 单文件 submit → poll → fetch → 写盘，返回 manifest 记录
# ---------------------------------------------------------------------------


def process_file(server: MinerUServer, pdf_path, cfg: Config, out_md) -> dict:
    pdf_path = Path(pdf_path)
    out_md = Path(out_md)
    rel = _rel(pdf_path, cfg.src_dir)
    try:
        size_kb = round(pdf_path.stat().st_size / 1024, 1)
    except OSError:
        size_kb = 0.0

    record = {
        "rel": rel,
        "status": "",
        "server": server.base_url,
        "task_id": "",
        "size_kb": size_kb,
        "attempts": 0,
        "seconds": 0.0,
        "error": "",
    }

    if out_md.exists() and out_md.stat().st_size > 100:
        record["status"] = "skip"
        return record

    start = time.monotonic()
    last_error = ""

    with server.semaphore:
        for attempt in range(1, max(1, cfg.retry_max) + 1):
            record["attempts"] = attempt
            record["task_id"] = ""  # 每次尝试重置，避免 submit() 失败时残留上一次尝试的 task_id
            try:
                task_id = server.submit(pdf_path)
                record["task_id"] = task_id
                deadline = time.monotonic() + server.max_poll_seconds
                while True:
                    status, error = server.status(task_id)
                    status_l = status.lower()
                    if status_l in _TERMINAL_OK:
                        payload = server.result(task_id)
                        md = extract_md(payload)
                        out_md.parent.mkdir(parents=True, exist_ok=True)
                        out_md.write_text(md, encoding="utf-8")
                        record["status"] = "ok"
                        record["error"] = ""
                        record["seconds"] = round(time.monotonic() - start, 1)
                        return record
                    if status_l in _TERMINAL_FAIL:
                        last_error = error or "server 报告 failed（无详情）"
                        break
                    if time.monotonic() >= deadline:
                        last_error = f"轮询超时 (>{server.max_poll_seconds}s)"
                        break
                    # 未知的非终态（如 queued/pending/processing）一律当作"还在跑"
                    _sleep(server.poll_interval)
            except Exception as exc:  # noqa: BLE001 - 记录后继续重试
                last_error = str(exc)

            if attempt < cfg.retry_max:
                _sleep(5 * attempt)

    record["status"] = "fail"
    record["error"] = last_error
    record["seconds"] = round(time.monotonic() - start, 1)
    return record


# ---------------------------------------------------------------------------
# 文件发现 + 断点续跑过滤
# ---------------------------------------------------------------------------


def _rel(pdf_path: Path, src_dir: str) -> str:
    return os.path.relpath(pdf_path, src_dir) if src_dir else str(pdf_path)


def _read_manifest_status(manifest_path: Path) -> Dict[str, str]:
    """读已有 manifest jsonl，返回 {rel: 最后一次记录的 status}（同一文件多次
    出现——比如重试续跑——以最后一条为准）。"""
    statuses: Dict[str, str] = {}
    if not manifest_path.exists():
        return statuses
    try:
        with open(manifest_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rel = rec.get("rel")
                status = rec.get("status")
                if rel and status:
                    statuses[rel] = status
    except OSError:
        pass
    return statuses


def _select_files(cfg: Config) -> List[Path]:
    if cfg.list_file:
        lines = Path(cfg.list_file).read_text(encoding="utf-8").splitlines()
        files = [Path(line.strip()) for line in lines if line.strip()]
    else:
        files = sorted(Path(cfg.src_dir).rglob("*.pdf"))

    prev = _read_manifest_status(cfg.manifest_path)
    if cfg.only_failed:
        files = [p for p in files if prev.get(_rel(p, cfg.src_dir)) == "fail"]
    elif prev:
        # 常规续跑优化：manifest 里已经 ok 的文件不必再排进队列
        # （process_file 自己的"输出已存在"检查也会兜底，这里只是提前过滤）。
        files = [p for p in files if prev.get(_rel(p, cfg.src_dir)) != "ok"]

    if cfg.limit and cfg.limit > 0:
        files = files[: cfg.limit]

    return files


# ---------------------------------------------------------------------------
# run —— 编排：文件列表 → 轮询分配 server → 线程池 → 追加 manifest → 进度/ETA
# ---------------------------------------------------------------------------


def run(cfg: Config, servers: List[MinerUServer]) -> dict:
    files = _select_files(cfg)
    total = len(files)

    stats = {"total": total, "ok": 0, "skip": 0, "fail": 0}
    if total == 0:
        print("没有待处理的 PDF 文件。")
        return stats

    manifest_path = cfg.manifest_path
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_lock = threading.Lock()
    stop_event = threading.Event()
    fail_rels: List[str] = []

    def _on_sigint(signum, frame):  # noqa: ARG001
        print("\n收到 Ctrl-C：停止派发新任务，等待进行中的任务完成…", file=sys.stderr)
        stop_event.set()

    old_handler = signal.signal(signal.SIGINT, _on_sigint)

    start_time = time.monotonic()
    done_count = 0

    def _out_md_for(pdf_path: Path) -> Path:
        rel = _rel(pdf_path, cfg.src_dir)
        return (Path(cfg.out_dir) / rel).with_suffix(".md")

    def _run_one(pdf_path: Path, server: MinerUServer) -> dict:
        out_md = _out_md_for(pdf_path)
        record = process_file(server, pdf_path, cfg, out_md)
        nonlocal done_count
        with manifest_lock:
            try:
                with open(manifest_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError as exc:  # 磁盘满/权限错误等：告警但不让单条 manifest 写入拖垮整个 run
                print(f"警告: 追加 manifest 失败（{manifest_path}）：{exc}", file=sys.stderr)
            stats[record["status"]] = stats.get(record["status"], 0) + 1
            if record["status"] == "fail":
                fail_rels.append(record["rel"])
            done_count += 1
            elapsed = time.monotonic() - start_time
            rate = elapsed / done_count if done_count else 0.0
            eta = int(rate * (total - done_count))
            port = server.base_url.rsplit(":", 1)[-1]
            pct = done_count * 100 // total
            print(
                f"[{port}] {done_count}/{total} ({pct}%) "
                f"ok={stats['ok']} skip={stats['skip']} fail={stats['fail']} eta={eta}s"
            )
        return record

    try:
        with ThreadPoolExecutor(max_workers=max(1, sum(s.capacity for s in servers))) as pool:
            futures = []
            for i, pdf_path in enumerate(files):
                if stop_event.is_set():
                    break
                server = servers[i % len(servers)]
                futures.append(pool.submit(_run_one, pdf_path, server))
            for fut in as_completed(futures):
                fut.result()
    finally:
        signal.signal(signal.SIGINT, old_handler)

    if fail_rels:
        failed_txt = Path(cfg.out_dir) / "failed.txt"
        failed_txt.write_text("\n".join(fail_rels) + "\n", encoding="utf-8")

    print(
        f"完成: {stats['ok']} 成功, {stats['skip']} 跳过, {stats['fail']} 失败 "
        f"(共 {total})"
    )
    return stats


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MinerU 批量 PDF → Markdown 解析工具")
    parser.add_argument("--env-file", default=None, help=".env 文件路径（默认 CWD 下的 .env）")
    parser.add_argument("--src", default=None, help="源目录（覆盖 MINERU_BATCH_SRC_DIR）")
    parser.add_argument("--out", default=None, help="输出目录（覆盖 MINERU_BATCH_OUT_DIR）")
    parser.add_argument("--servers", default=None, help="逗号分隔的 server 列表（覆盖 MINERU_BATCH_SERVERS）")
    parser.add_argument("--list", dest="list_file", default=None, help="显式 PDF 路径列表文件，一行一个")
    parser.add_argument("--dry-run", action="store_true", help="只打印分配计划，不真正解析")
    parser.add_argument("--only-failed", action="store_true", help="只重跑上次 manifest 记为 fail 的文件")
    parser.add_argument("--limit", type=int, default=None, help="限制处理文件数（调试用）")
    return parser.parse_args(argv)


def _build_servers(cfg: Config) -> List[MinerUServer]:
    form_fields = {
        "backend": cfg.backend,
        "lang_list": cfg.lang,
        "formula_enable": "true" if cfg.formula_enable else "false",
        "table_enable": "true" if cfg.table_enable else "false",
        "return_md": "true",
    }
    servers: List[MinerUServer] = []
    for base_url in cfg.servers:
        session = requests.Session()
        servers.append(
            MinerUServer(
                base_url,
                session,
                capacity=cfg.concurrency_per_server or 4,
                submit_timeout=cfg.submit_timeout,
                result_timeout=cfg.result_timeout,
                poll_interval=cfg.poll_interval,
                max_poll_seconds=cfg.max_poll_seconds,
                form_fields=form_fields,
            )
        )
    return servers


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    env_path = Path(args.env_file) if args.env_file else Path(".env")
    load_dotenv(env_path)
    cfg = Config.from_env_and_args(args)

    servers = _build_servers(cfg)

    for server in servers:
        try:
            health = server.health()
        except Exception as exc:  # noqa: BLE001
            if cfg.dry_run:
                print(f"警告: [dry-run] 无法连接 {server.base_url}: {exc}", file=sys.stderr)
                continue
            print(f"错误: 无法连接 MinerU server {server.base_url}: {exc}", file=sys.stderr)
            return 1
        else:
            if not cfg.concurrency_per_server:
                cap = health.get("max_concurrent_requests") or 4
                server.capacity = max(1, int(cap))
                server.semaphore = threading.Semaphore(server.capacity)

    if cfg.dry_run:
        files = _select_files(cfg)
        print(f"[dry-run] 待处理 {len(files)} 个文件，前 20 个的 server 分配预览：")
        for i, pdf_path in enumerate(files[:20]):
            server = servers[i % len(servers)]
            print(f"  [{server.base_url}] {pdf_path}")
        return 0

    run(cfg, servers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
