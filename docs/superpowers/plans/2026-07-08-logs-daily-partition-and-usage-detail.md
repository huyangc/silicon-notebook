# 日志按天分文件归档 + 用户总览笔记本下钻 + 返回主页 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 日志按天分文件 + 旧天 gzip 归档、查看器默认只读当天按天有界读（根治卡死）；`/admin/usage` 下钻某用户名下笔记本详情；独立管理页加返回主页页头栏。

**Architecture:** 写入侧文件名带日期（`<channel>-YYYY-MM-DD.jsonl`），当天明文/旧天 `.gz`。归档双触发：后端启动后台扫一遍 + 写入跨天补压（模块级单线程池，best-effort 不阻塞 emit）。读取侧新增按天原语（明文尾读、gz 流式读，`seq` 改为「行内位置」），端点默认读当天、可选历史某天。下钻与返回主页是独立的前后端小改。

**Tech Stack:** Python/FastAPI/SQLite（后端），Next.js 15 + React（前端单页），`node --test` + pytest。

## Global Constraints（每个任务隐含遵守）

- 交互与文案**中文**；前端中文弯引号 `""` 有意保留，不得批量替直引号。
- **运行效率一等**：写入 `emit` 热路径新增逻辑必须 O(1) 摊还、best-effort、绝不阻塞/抛错破坏被观测请求；读取必须按天有界。
- **归档绝不碰当天活跃文件**（只压 `day < today`）；日志写入 best-effort（日期/切文件/归档入队异常不得传出 emit）。
- **不删数据、不迁移历史**：老单文件 `<channel>.jsonl` 只读认作 `legacy`。
- **路径安全**：新增 `date` 参数校验 `^\d{4}-\d{2}-\d{2}$` 或字面量 `legacy`，否则拒绝。
- **admin 门控**：新 `/admin/*` 端点 `user.role=="admin"` 否则 403；前端 403 纵深防御。
- **无 DB schema 改动**（本计划不加表/列/索引；不 bump SCHEMA_VERSION）。
- **前端 helper 测试放 `frontend/app/` 顶层**（`npm test`=`node --test app/*.test.mjs` 只匹配顶层）。Node v22 支持 `.mjs` import `.ts`。
- 常量（`log_reader.py`）：`MAX_RECORDS_PER_WINDOW = 50_000`、`MAX_TAIL_BYTES = 32 * 1024 * 1024`。
- `seq` 语义：明文（可变、被轮询）用**行首字节偏移**（追加下稳定）；gz（不可变）用**行索引**（更简单，不可变故稳定）。二者都单调递增随文件顺序；前端只当不透明游标。

## File Structure

- `backend/app/core/event_logging.py`（改）：带日期文件名、跨天补压、`_gzip_day_file`、`archive_stale_days`、`_archive_pool`。
- `backend/app/main.py`（改）：`create_app` 启动提交 `archive_stale_days` 到后台。
- `backend/app/services/log_reader.py`（改）：`available_days`/`resolve_day_path`/`valid_date_param`/`_load_plain_window`/`_load_gz_window`/`load_day_window`；保留现有纯函数。
- `backend/app/api/debug_logs.py`（改）：`/days` 端点、`date` 参数、detail 带 seq、channel 列表报字节数。
- `backend/app/services/sqlite_repository.py`（改）：`list_user_notebooks`。
- `backend/app/models/schemas.py`（改）：`AdminUserNotebook`。
- `backend/app/api/routes.py`（改）：`GET /admin/users/{user_id}/notebooks`。
- `frontend/app/dev/logs/{api.ts,types.ts,page.tsx,components/ChannelTabs.tsx,logs.css}`（改）：日期选择、默认今天、轮询仅今天、截断提示。
- `frontend/app/admin/usage/{api.ts→新 notebooks.ts,page.tsx,usage.css}`（改/增）：可展开行 + 懒加载笔记本子表。
- `frontend/app/components/PageHeader.tsx` + `page-header.css`（新）：返回主页页头栏，用于两页。
- 测试：`backend/tests/test_log_archive.py`、`test_log_reader_windows.py`、`test_debug_logs_days.py`、`test_admin_user_notebooks.py`（新）；`frontend/app/logs-date.test.mjs`、`admin-notebooks.test.mjs`（新，顶层）。

---

### Task 1: 归档核心 + 启动扫一遍接线

**Files:**
- Modify: `backend/app/core/event_logging.py`
- Modify: `backend/app/main.py`
- Test: `backend/tests/test_log_archive.py`

**Interfaces:**
- Produces: `_gzip_day_file(plain: Path) -> None`、`archive_stale_days(settings) -> None`、模块级 `_archive_pool`（`ThreadPoolExecutor(max_workers=1)`）。

- [ ] **Step 1: 失败测试** `backend/tests/test_log_archive.py`

```python
import gzip, json
from pathlib import Path
from app.core import event_logging as el


def _write(p: Path, lines):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def test_gzip_day_file_atomic_idempotent_removes_plain(tmp_path):
    plain = tmp_path / "llm-2026-07-01.jsonl"
    _write(plain, [{"a": 1}, {"a": 2}])
    el._gzip_day_file(plain)
    gz = tmp_path / "llm-2026-07-01.jsonl.gz"
    assert gz.exists() and not plain.exists()
    with gzip.open(gz, "rt", encoding="utf-8") as fh:
        assert [json.loads(l) for l in fh] == [{"a": 1}, {"a": 2}]
    # 幂等:再调不报错、不改动
    el._gzip_day_file(plain)  # plain 已不在 → no-op
    assert gz.exists()


def test_gzip_missing_or_already_gz_is_noop(tmp_path):
    el._gzip_day_file(tmp_path / "nope-2026-01-01.jsonl")  # 不存在 → 静默
    plain = tmp_path / "llm-2026-01-02.jsonl"
    _write(plain, [{"x": 1}])
    (tmp_path / "llm-2026-01-02.jsonl.gz").write_bytes(b"stub")
    el._gzip_day_file(plain)  # gz 已存在 → 不覆盖、不删明文
    assert plain.exists()


def test_archive_stale_days_skips_today_and_legacy(tmp_path, monkeypatch):
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    # 旧天(应压)、今天(不压)、legacy 无日期(不碰)、per-user 子目录旧天(应压)
    _write(tmp_path / "llm-2026-01-01.jsonl", [{"a": 1}])
    _write(tmp_path / f"llm-{today}.jsonl", [{"a": 2}])
    _write(tmp_path / "llm.jsonl", [{"legacy": 1}])
    _write(tmp_path / "user-abc" / "events-2026-01-01.jsonl", [{"e": 1}])

    class S:  # 最小 settings 替身
        event_log_dir = str(tmp_path)
    el.archive_stale_days(S())
    el._archive_pool.shutdown(wait=True)  # 等后台压缩完成后断言
    # 重新起池供后续测试（shutdown 后不可再 submit）
    import concurrent.futures as _f
    el._archive_pool = _f.ThreadPoolExecutor(max_workers=1, thread_name_prefix="log-archive")

    assert (tmp_path / "llm-2026-01-01.jsonl.gz").exists()
    assert (tmp_path / "user-abc" / "events-2026-01-01.jsonl.gz").exists()
    assert (tmp_path / f"llm-{today}.jsonl").exists()          # 今天不压
    assert not (tmp_path / f"llm-{today}.jsonl.gz").exists()
    assert (tmp_path / "llm.jsonl").exists()                    # legacy 不碰
```

- [ ] **Step 2: 运行确认失败** `cd backend && python -m pytest tests/test_log_archive.py -q` → FAIL（`_gzip_day_file`/`archive_stale_days` 不存在）。

- [ ] **Step 3: 实现**。在 `event_logging.py` 顶部补 `import gzip`, `import os`, `import shutil`, `import threading`, `from concurrent.futures import ThreadPoolExecutor`, `from typing import Dict, Tuple`（合并进现有 typing import）。在 `_anchor` 附近加：

```python
# 日志归档：把「非今天」的天文件 gzip。单线程池串行执行，best-effort，绝不阻塞写入。
_archive_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="log-archive")
_DATED_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})\.jsonl$")


def _gzip_day_file(plain: Path) -> None:
    """把某天明文 jsonl 压成同名 .gz，再删明文。先写 .gz.tmp 再原子 rename，故读取器
    「先明文缺则 .gz」在任一时刻至少一份可读、绝不读到半个 gz。异常吞掉（下次启动补）。"""
    try:
        plain = Path(plain)
        if not plain.exists():
            return
        gz = plain.parent / (plain.name + ".gz")
        if gz.exists():
            return
        tmp = plain.parent / (plain.name + ".gz.tmp")
        with plain.open("rb") as fin, gzip.open(tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        os.replace(tmp, gz)
        plain.unlink()
    except Exception:  # pragma: no cover - 归档失败不致命
        pass


def archive_stale_days(settings) -> None:
    """启动扫一遍：glob 全局与 per-user 一层子目录下的带日期明文，date<today 且无 .gz
    者提交压缩。老无日期单文件（legacy）不匹配 _DATED_RE，天然不碰。best-effort。"""
    try:
        log_dir = Path(getattr(settings, "event_log_dir", ".local/logs"))
        if not log_dir.is_absolute():
            log_dir = _ROOT_DIR / log_dir
        if not log_dir.exists():
            return
        today = datetime.now().strftime("%Y-%m-%d")
        for p in list(log_dir.glob("*.jsonl")) + list(log_dir.glob("*/*.jsonl")):
            m = _DATED_RE.search(p.name)
            if m and m.group(1) < today:
                _archive_pool.submit(_gzip_day_file, p)
    except Exception:  # pragma: no cover
        pass
```

在 `main.py` `create_app()` 内、`settings = get_settings()` 之后加（best-effort 后台，不阻塞启动）：

```python
    from app.core.event_logging import archive_stale_days, _archive_pool
    try:
        _archive_pool.submit(archive_stale_days, settings)
    except Exception:  # pragma: no cover - 归档接线绝不阻断启动
        logger.warning("log archive sweep 提交失败（不影响启动）", exc_info=False)
```

- [ ] **Step 4: 运行通过** `cd backend && python -m pytest tests/test_log_archive.py -q` → PASS。

- [ ] **Step 5: Commit** `feat(logs): 归档核心 _gzip_day_file/archive_stale_days + 启动后台扫一遍`

---

### Task 2: 写入按天分文件 + 跨天补压

**Files:**
- Modify: `backend/app/core/event_logging.py`（`_target_path` → 带日期；`emit` 尾部跨天检测）
- Test: `backend/tests/test_log_archive.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `_archive_pool` / `_gzip_day_file`。
- Produces: 写入路径 `<dir>/<channel>-YYYY-MM-DD.jsonl`；模块级 `_last_write_day: Dict[Tuple[str,str],str]`、`_last_write_lock`。

- [ ] **Step 1: 失败测试**（追加到 `test_log_archive.py`）

```python
def _logger(tmp_path, monkeypatch, channel="llm", per_user=False):
    class S:
        event_log_enabled = True
        event_log_dir = str(tmp_path)
        llm_log_max_chars = 4000
    return el.EventLogger(S(), channel=channel, per_user=per_user)


def test_emit_writes_dated_file(tmp_path, monkeypatch):
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    lg = _logger(tmp_path, monkeypatch)
    lg.emit({"id": "x1", "kind": "chat"})
    dated = tmp_path / f"llm-{today}.jsonl"
    assert dated.exists()
    assert not (tmp_path / "llm.jsonl").exists()  # 不再写无日期文件
    rec = json.loads(dated.read_text(encoding="utf-8").strip())
    assert rec["id"] == "x1" and "ts" in rec


def test_rollover_enqueues_prev_day_gzip(tmp_path, monkeypatch):
    # 清空跨天账目，避免测试间串扰
    el._last_write_day.clear()
    lg = _logger(tmp_path, monkeypatch)
    # 用一个可控 now 逐步推进日期
    seq = iter(["2026-03-01", "2026-03-01", "2026-03-02"])
    real = el.datetime

    class FakeDT:
        @staticmethod
        def now():
            # ts 用真实 now 即可；这里只需 strftime 的日期可控
            class _N:
                def __init__(self, day): self._day = day
                def isoformat(self): return f"{self._day}T00:00:00"
                def strftime(self, fmt): return self._day
            return _N(next(seq))
    monkeypatch.setattr(el, "datetime", FakeDT)
    lg.emit({"id": "a"})   # 03-01 首写：prev=None → 不压
    lg.emit({"id": "b"})   # 03-01 再写：同天 → 不压
    lg.emit({"id": "c"})   # 03-02：跨天 → 压 03-01
    el._archive_pool.shutdown(wait=True)
    import concurrent.futures as _f
    el._archive_pool = _f.ThreadPoolExecutor(max_workers=1, thread_name_prefix="log-archive")
    assert (tmp_path / "llm-2026-03-01.jsonl.gz").exists()   # 昨天被压
    assert (tmp_path / "llm-2026-03-02.jsonl").exists()      # 今天明文在
```

- [ ] **Step 2: 运行确认失败** → FAIL（当前写 `llm.jsonl`，无跨天逻辑）。

- [ ] **Step 3: 实现**。加模块级状态（`_archive_pool` 附近）：

```python
_last_write_day: "Dict[Tuple[str, str], str]" = {}
_last_write_lock = threading.Lock()
```

改 `_target_path`（保留 per_user/global 目录逻辑，仅文件名带日期）：

```python
    def _dir(self) -> Path:
        if not self.per_user:
            return self.log_dir
        try:
            sub = owner_dir(get_log_owner())
        except Exception:  # pragma: no cover
            sub = "_system"
        return self.log_dir / sub

    def _target_path_for_day(self, day: str) -> Path:
        return self._dir() / f"{self.channel}-{day}.jsonl"
```

改 `emit`：把开头改成一次性取 `now`，并在成功写入后做跨天检测：

```python
        if not self.enabled:
            return
        now = datetime.now()
        event.setdefault("ts", now.isoformat())
        event.setdefault("channel", self.channel)
        day = now.strftime("%Y-%m-%d")
        target = self._target_path_for_day(day)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:  # pragma: no cover
            self.logger.warning("failed to write %s log line", self.channel, exc_info=False)
        self._maybe_archive_prev_day(target.parent, day)
        # ...（保留原有 console 输出逻辑不变）
```

加跨天检测助手：

```python
    def _maybe_archive_prev_day(self, dir_: Path, day: str) -> None:
        """本进程内某(目录,channel)序列首次见到新 day 时，把上一 day 的明文提交压缩。
        prev is None（本进程首写）不压（无从判断是否翻天；启动扫一遍已管历史）。O(1) 摊还。"""
        try:
            key = (str(dir_), self.channel)
            with _last_write_lock:
                prev = _last_write_day.get(key)
                should = prev is not None and prev != day
                if prev != day:
                    _last_write_day[key] = day
            if should:
                _archive_pool.submit(_gzip_day_file, dir_ / f"{self.channel}-{prev}.jsonl")
        except Exception:  # pragma: no cover - 归档入队绝不破坏 emit
            pass
```

注：`_target_path`（旧无参版本）若别处仍被引用需保留兼容；否则删除。`self.path`（无日期）保留为历史属性，不再写入。

- [ ] **Step 4: 运行通过** `pytest tests/test_log_archive.py -q`；并跑现有 `pytest tests/ -k event_log -q` 确认未破坏既有日志测试。

- [ ] **Step 5: Commit** `feat(logs): 日志按天分文件 + 写入跨天补压`

---

### Task 3: 读取原语 A — 天目录枚举与解析

**Files:**
- Modify: `backend/app/services/log_reader.py`
- Test: `backend/tests/test_log_reader_windows.py`

**Interfaces:**
- Produces: `valid_date_param(date:str)->bool`、`available_days(dir:Path, channel:str)->List[str]`、`resolve_day_path(dir:Path, channel:str, date:str)->Tuple[Path,bool]`。

- [ ] **Step 1: 失败测试** `backend/tests/test_log_reader_windows.py`

```python
from pathlib import Path
from app.services import log_reader as lr


def test_valid_date_param():
    assert lr.valid_date_param("2026-07-08")
    assert lr.valid_date_param("legacy")
    assert not lr.valid_date_param("2026-7-8")
    assert not lr.valid_date_param("../etc")
    assert not lr.valid_date_param("")


def test_available_days_sorted_desc_with_legacy(tmp_path):
    (tmp_path / "llm-2026-07-01.jsonl").write_text("{}\n")
    (tmp_path / "llm-2026-07-03.jsonl.gz").write_bytes(b"x")
    (tmp_path / "llm.jsonl").write_text("{}\n")           # legacy
    (tmp_path / "events-2026-07-02.jsonl").write_text("{}\n")  # 别的 channel 不混入
    assert lr.available_days(tmp_path, "llm") == ["2026-07-03", "2026-07-01", "legacy"]


def test_resolve_day_path_prefers_plain_then_gz_then_legacy(tmp_path):
    (tmp_path / "llm-2026-07-01.jsonl").write_text("{}\n")
    (tmp_path / "llm-2026-07-02.jsonl.gz").write_bytes(b"x")
    (tmp_path / "llm.jsonl").write_text("{}\n")
    assert lr.resolve_day_path(tmp_path, "llm", "2026-07-01") == (tmp_path / "llm-2026-07-01.jsonl", False)
    assert lr.resolve_day_path(tmp_path, "llm", "2026-07-02") == (tmp_path / "llm-2026-07-02.jsonl.gz", True)
    assert lr.resolve_day_path(tmp_path, "llm", "legacy") == (tmp_path / "llm.jsonl", False)
    # 不存在的天 → 按明文空处理（path 不存在、非 gz）
    p, gz = lr.resolve_day_path(tmp_path, "llm", "2026-07-09")
    assert not p.exists() and gz is False
```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现**（`log_reader.py` 顶部补 `import re`；加）：

```python
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_PLAIN_DATE_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})\.jsonl$")
_GZ_DATE_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")


def valid_date_param(date: str) -> bool:
    return date == "legacy" or bool(_DATE_RE.match(date or ""))


def available_days(dir: Path, channel: str) -> List[str]:
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
    if date == "legacy":
        return dir / f"{channel}.jsonl", False
    plain = dir / f"{channel}-{date}.jsonl"
    if plain.exists():
        return plain, False
    gz = dir / f"{channel}-{date}.jsonl.gz"
    if gz.exists():
        return gz, True
    return plain, False
```

- [ ] **Step 4: 运行通过**。
- [ ] **Step 5: Commit** `feat(logs): 读取原语 available_days/resolve_day_path/valid_date_param`

---

### Task 4: 读取原语 B — 明文窗口（字节偏移 seq）

**Files:**
- Modify: `backend/app/services/log_reader.py`
- Test: `backend/tests/test_log_reader_windows.py`（追加）

**Interfaces:**
- Produces: `_load_plain_window(path, *, since, before, max_records, max_bytes) -> Tuple[List[dict], int, bool]`（records 按 seq 升序、seq=字节偏移；返回 (records, malformed, truncated)）。

- [ ] **Step 1: 失败测试**（追加）

```python
import json


def _write_lines(p, objs):
    p.write_text("".join(json.dumps(o) + "\n" for o in objs), encoding="utf-8")


def test_plain_window_seq_is_byte_offset_and_monotonic(tmp_path):
    p = tmp_path / "llm-2026-07-08.jsonl"
    _write_lines(p, [{"i": 0}, {"i": 1}, {"i": 2}])
    recs, malformed, trunc = lr._load_plain_window(
        p, since=None, before=None, max_records=100, max_bytes=1 << 20)
    assert [r["i"] for r in recs] == [0, 1, 2]
    seqs = [r["seq"] for r in recs]
    assert seqs == sorted(seqs) and seqs[0] == 0     # 首行偏移 0
    assert malformed == 0 and trunc is False


def test_plain_window_tail_truncates_by_bytes(tmp_path):
    p = tmp_path / "llm-2026-07-08.jsonl"
    _write_lines(p, [{"i": i, "pad": "x" * 50} for i in range(200)])
    recs, _, trunc = lr._load_plain_window(
        p, since=None, before=None, max_records=100000, max_bytes=300)  # 极小字节预算
    assert trunc is True                              # 丢了更旧的
    assert recs[-1]["i"] == 199                        # 保到最新
    assert recs[0]["seq"] > 0                          # 尾读起点不在 0


def test_plain_window_since_returns_only_newer(tmp_path):
    p = tmp_path / "llm-2026-07-08.jsonl"
    _write_lines(p, [{"i": 0}, {"i": 1}, {"i": 2}])
    all_recs, _, _ = lr._load_plain_window(p, since=None, before=None, max_records=100, max_bytes=1 << 20)
    cut = all_recs[1]["seq"]                            # 第二行的偏移
    newer, _, _ = lr._load_plain_window(p, since=cut, before=None, max_records=100, max_bytes=1 << 20)
    assert [r["i"] for r in newer] == [2]              # 只回严格更新的


def test_plain_window_before_returns_older(tmp_path):
    p = tmp_path / "llm-2026-07-08.jsonl"
    _write_lines(p, [{"i": 0}, {"i": 1}, {"i": 2}])
    all_recs, _, _ = lr._load_plain_window(p, since=None, before=None, max_records=100, max_bytes=1 << 20)
    cut = all_recs[2]["seq"]
    older, _, _ = lr._load_plain_window(p, since=None, before=cut, max_records=100, max_bytes=1 << 20)
    assert [r["i"] for r in older] == [0, 1]
```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现**（加到 `log_reader.py`；`MAX_*` 常量也在此定义）：

```python
MAX_RECORDS_PER_WINDOW = 50_000
MAX_TAIL_BYTES = 32 * 1024 * 1024


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
        # 轮询：seek 到 since 向后读到 EOF，只回 seq>since（since 那行是已见的最后一行）
        with path.open("rb") as fh:
            fh.seek(since)
            blob = fh.read()
        recs, malformed = _parse_blob(blob, since, drop_partial_first=False)
        recs = [r for r in recs if r["seq"] > since]
        return recs, malformed, False
    end = before if before is not None else size
    start = max(0, end - max_bytes)
    with path.open("rb") as fh:
        fh.seek(start)
        blob = fh.read(end - start)
    recs, malformed = _parse_blob(blob, start, drop_partial_first=(start > 0))
    if before is not None:
        recs = [r for r in recs if r["seq"] < before]
    truncated = start > 0
    if len(recs) > max_records:
        recs = recs[-max_records:]
        truncated = True
    return recs, malformed, truncated
```

- [ ] **Step 4: 运行通过**。
- [ ] **Step 5: Commit** `feat(logs): 明文窗口尾读 _load_plain_window(字节偏移 seq)`

---

### Task 5: 读取原语 C — gz 窗口 + 派发器

**Files:**
- Modify: `backend/app/services/log_reader.py`
- Test: `backend/tests/test_log_reader_windows.py`（追加）

**Interfaces:**
- Consumes: Task 4 常量。
- Produces: `_load_gz_window(path, *, since, before, max_records) -> (records, malformed, truncated)`（seq=行索引）；`load_day_window(path, is_gzip, *, since, before, max_records=MAX_RECORDS_PER_WINDOW, max_bytes=MAX_TAIL_BYTES)` 派发器。

- [ ] **Step 1: 失败测试**（追加）

```python
import gzip as _gz


def test_gz_window_line_index_seq_and_truncate(tmp_path):
    p = tmp_path / "llm-2026-07-01.jsonl.gz"
    with _gz.open(p, "wt", encoding="utf-8") as fh:
        for i in range(10):
            fh.write(json.dumps({"i": i}) + "\n")
    recs, malformed, trunc = lr._load_gz_window(p, since=None, before=None, max_records=100)
    assert [r["i"] for r in recs] == list(range(10))
    assert [r["seq"] for r in recs] == list(range(10))      # 行索引
    assert trunc is False
    # maxlen 截断保最新
    recs2, _, trunc2 = lr._load_gz_window(p, since=None, before=None, max_records=3)
    assert [r["i"] for r in recs2] == [7, 8, 9] and trunc2 is True


def test_load_day_window_dispatches(tmp_path):
    plain = tmp_path / "llm-2026-07-08.jsonl"
    _write_lines(plain, [{"i": 0}, {"i": 1}])
    r1, _, _ = lr.load_day_window(plain, False, since=None, before=None)
    assert [r["i"] for r in r1] == [0, 1]
    gzp = tmp_path / "llm-2026-07-01.jsonl.gz"
    with _gz.open(gzp, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"i": 9}) + "\n")
    r2, _, _ = lr.load_day_window(gzp, True, since=None, before=None)
    assert [r["i"] for r in r2] == [9]
```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现**（补 `import gzip` 与 `from collections import deque`）：

```python
def _load_gz_window(path, *, since, before, max_records):
    """gz 不可变、不被轮询：流式解压逐行，deque(maxlen) 只保最新 max_records 行，
    seq=行索引（不可变故稳定）。truncated=行数超过 maxlen。"""
    if not path.exists():
        return [], 0, False
    buf = deque(maxlen=max_records)
    malformed = 0
    seen = 0
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for idx, raw in enumerate(fh):
            line = raw.strip()
            if not line:
                continue
            seen += 1
            try:
                obj = json.loads(line)
            except Exception:
                malformed += 1
                continue
            if isinstance(obj, dict):
                obj["seq"] = idx
                buf.append(obj)
            else:
                malformed += 1
    recs = list(buf)
    if since is not None:
        recs = [r for r in recs if r["seq"] > since]
    if before is not None:
        recs = [r for r in recs if r["seq"] < before]
    truncated = seen > max_records
    return recs, malformed, truncated


def load_day_window(path, is_gzip, *, since=None, before=None,
                    max_records=MAX_RECORDS_PER_WINDOW, max_bytes=MAX_TAIL_BYTES):
    if is_gzip:
        return _load_gz_window(path, since=since, before=before, max_records=max_records)
    return _load_plain_window(path, since=since, before=before,
                              max_records=max_records, max_bytes=max_bytes)
```

- [ ] **Step 4: 运行通过**；跑 `pytest tests/test_log_reader_windows.py -q` 全绿。
- [ ] **Step 5: Commit** `feat(logs): gz 流式窗口 + load_day_window 派发器`

---

### Task 6: 端点改造（/days、date 参数、detail seq、channel 字节数）

**Files:**
- Modify: `backend/app/api/debug_logs.py`
- Test: `backend/tests/test_debug_logs_days.py`

**Interfaces:**
- Consumes: Task 3-5 的 `available_days`/`resolve_day_path`/`valid_date_param`/`load_day_window`；现有 `filter_records`/`compute_stats`/`paginate`/`to_summary`。

- [ ] **Step 1: 失败测试** `backend/tests/test_debug_logs_days.py`（用 TestClient；参照 `test_admin_users.py` 的 `client`/`_auth`/`_auth_admin`，另需开 `DEBUG_LOGS_ENABLED`）

```python
import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("DEBUG_LOGS_ENABLED", "true")
    monkeypatch.setenv("EVENT_LOG_DIR", str(tmp_path / "logs"))
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _auth_admin(client):
    t = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _seed_day(tmp_path, owner, channel, day, objs):
    d = tmp_path / "logs" / owner
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{channel}-{day}.jsonl").write_text(
        "".join(json.dumps(o) + "\n" for o in objs), encoding="utf-8")


def test_days_endpoint_lists_days(client, tmp_path):
    admin = _auth_admin(client)
    # admin 自己的 owner 目录名 = 其 user.id；先查出来
    me = client.get("/api/me", headers=admin).json()
    owner = me["id"]
    _seed_day(tmp_path, owner, "llm", "2026-07-07", [{"id": "a", "kind": "chat"}])
    r = client.get("/api/debug/logs/llm/days", headers=admin)
    assert r.status_code == 200 and "2026-07-07" in r.json()["days"]


def test_date_param_reads_that_day_and_rejects_bad(client, tmp_path):
    admin = _auth_admin(client)
    owner = client.get("/api/me", headers=admin).json()["id"]
    _seed_day(tmp_path, owner, "llm", "2026-07-07", [{"id": "a", "kind": "chat"}])
    ok = client.get("/api/debug/logs/llm?date=2026-07-07", headers=admin)
    assert ok.status_code == 200 and ok.json()["date"] == "2026-07-07"
    assert len(ok.json()["records"]) == 1 and "truncated" in ok.json()
    bad = client.get("/api/debug/logs/llm?date=../etc", headers=admin)
    assert bad.status_code in (400, 404, 422)
```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现**。`debug_logs.py`：加 `from datetime import datetime`；把 `_channel_path` 旁加 `_channel_dir`：

```python
def _channel_dir(settings, owner):
    log_dir = Path(settings.event_log_dir)
    if not log_dir.is_absolute():
        log_dir = _ROOT_DIR / log_dir
    return log_dir if owner is None else log_dir / owner
```

改 `list_channels` 计数为字节数（不解析）：

```python
        path_dir = _channel_dir(settings, target)
        today = datetime.now().strftime("%Y-%m-%d")
        today_file = path_dir / f"{name}-{today}.jsonl"
        out.append({"name": name, "file": filename,
                    "exists": today_file.exists(),
                    "bytes": today_file.stat().st_size if today_file.exists() else 0})
```

新增 `/days`：

```python
@router.get("/{channel}/days")
def list_days(channel, user=Depends(get_current_user), settings=Depends(require_enabled),
              owner: Optional[str] = Query(None)):
    if channel not in log_reader.CHANNELS:
        raise HTTPException(404, f"unknown channel: {channel}")
    target = _resolve_owner(channel, user, owner)
    return {"channel": channel, "days": log_reader.available_days(_channel_dir(settings, target), channel)}
```

改 `list_records`：加 `date: Optional[str] = Query(None)`；校验；解析当天：

```python
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    if not log_reader.valid_date_param(date):
        raise HTTPException(400, f"bad date: {date}")
    target = _resolve_owner(channel, user, owner)
    if channel not in log_reader.CHANNELS:
        raise HTTPException(404, f"unknown channel: {channel}")
    path, is_gzip = log_reader.resolve_day_path(_channel_dir(settings, target), channel, date)
    records, malformed, truncated = log_reader.load_day_window(
        path, is_gzip, since=since, before=before)
    filtered = log_reader.filter_records(records, kind=kind, status=status, model=model, q=q)
    stats = log_reader.compute_stats(records, filtered, malformed)
    filtered_desc = sorted(filtered, key=lambda r: r.get("seq", -1), reverse=True)
    page, has_more = log_reader.paginate(filtered_desc, before=before, since=since, limit=limit)
    newest_seq = filtered_desc[0]["seq"] if filtered_desc else None
    return {"channel": channel, "date": date, "file_exists": path.exists(),
            "records": [log_reader.to_summary(r) for r in page], "stats": stats,
            "has_more": has_more or truncated, "truncated": truncated, "newest_seq": newest_seq}
```

改 `get_record`：加 `date` 与可选 `seq`；有 seq（明文字节偏移）时直接读；否则在当天窗口内按 id 找：

```python
@router.get("/{channel}/{record_id}")
def get_record(channel, record_id, user=Depends(get_current_user), settings=Depends(require_enabled),
               owner: Optional[str] = Query(None), date: Optional[str] = Query(None),
               seq: Optional[int] = Query(None)):
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")
    if not log_reader.valid_date_param(date):
        raise HTTPException(400, f"bad date: {date}")
    target = _resolve_owner(channel, user, owner)
    path, is_gzip = log_reader.resolve_day_path(_channel_dir(settings, target), channel, date)
    records, _, _ = log_reader.load_day_window(path, is_gzip, since=None, before=None)
    matches = [r for r in records if r.get("id") == record_id]
    if not matches:
        raise HTTPException(404, f"record not found: {record_id}")
    return matches[-1]
```

（注：`_channel_path` 若不再被引用可删；`load_records` 保留供既有测试/eval。`EVENT_LOG_DIR` 需能被 Settings 读到——若 config 里字段名不同，测试 fixture 相应改用真实 alias。实现者据 `Settings` 定义核对环境变量名。）

- [ ] **Step 4: 运行通过** `pytest tests/test_debug_logs_days.py -q`；跑既有 `pytest tests/ -k debug_logs -q` 不回归。
- [ ] **Step 5: Commit** `feat(logs): 端点按天读(/days + date 参数 + detail seq + channel 字节数)`

---

### Task 7: 前端 /dev/logs 日期选择 + 默认今天 + 轮询仅今天 + 截断提示

**Files:**
- Modify: `frontend/app/dev/logs/api.ts`、`types.ts`、`page.tsx`、`components/ChannelTabs.tsx`、`logs.css`
- Test: `frontend/app/logs-date.test.mjs`（顶层）+ 新 `frontend/app/dev/logs/date.ts`（放可测 helper）

**Interfaces:**
- Consumes: 后端 `/debug/logs/{channel}/days`、`?date=`、响应新增 `date`/`truncated`。

- [ ] **Step 1: 失败测试** `frontend/app/logs-date.test.mjs`

```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { dayLabel, TODAY_VALUE } from "./dev/logs/date.ts";

test("dayLabel maps sentinels", () => {
  assert.equal(dayLabel(TODAY_VALUE), "今天");
  assert.equal(dayLabel("legacy"), "历史(未分天)");
  assert.equal(dayLabel("2026-07-08"), "2026-07-08");
});
```

- [ ] **Step 2: 运行确认失败** `cd frontend && npm test`。

- [ ] **Step 3: 实现**。新建 `frontend/app/dev/logs/date.ts`：

```typescript
export const TODAY_VALUE = ""; // 空 = 让后端按其本地时区兜底为「今天」，避开浏览器/服务器时区错位
export function dayLabel(v: string): string {
  if (v === TODAY_VALUE) return "今天";
  if (v === "legacy") return "历史(未分天)";
  return v;
}
```

`api.ts`：`RecordQuery` 加 `date?: string`（`fetchRecords` 已用 URLSearchParams 遍历，自动带上）；加：

```typescript
export function fetchDays(channel: string, owner?: string): Promise<{ channel: string; days: string[] }> {
  const suffix = owner ? `?owner=${encodeURIComponent(owner)}` : "";
  return get(`/debug/logs/${channel}/days${suffix}`);
}
```

`fetchRecord(channel, id, date?, seq?)` 追加 `?date=&seq=`（拼查询串）。

`types.ts`：`ListResponse` 加 `date: string; truncated: boolean;`；`ChannelInfo` 的 `count` 改为可选并加 `bytes?: number`。

`page.tsx`：加 `const [date, setDate] = useState(TODAY_VALUE)`、`const [days, setDays] = useState<string[]>([])`、`const [truncated, setTruncated] = useState(false)`。
- `filterParams` 纳入 `date`（`useMemo` 依赖加 `date`）；`reload`/`loadMore`/poll 的 `fetchRecords` 自动带 `date`；`reload` 里 `setTruncated(r.truncated)`。
- 拉 days：`useEffect(()=>{ fetchDays(channel, owner).then(r=>setDays(r.days)).catch(()=>{}) }, [owner])`。
- 顶部加日期下拉（放 `logview-owner` 行或 filters 行）：`今天`(value=TODAY_VALUE) + `days.map` + 若 days 含 "legacy" 归入其中；`onChange` 调 `setDate` 并写入 URL（与 owner 同法）。
- **轮询仅今天**：`useEffect` poll 的 guard 改为 `if (!autoRefresh || date !== TODAY_VALUE) return;`；`autoRefresh` 勾选框在 `date !== TODAY_VALUE` 时禁用并提示「仅当天可实时」。
- 截断提示:`{truncated ? <div className="logview-trunc">已截断,仅显示最近 {records.length} 条,请选择具体某天或缩小范围</div> : null}`。
- detail：`select` 里 `fetchRecord(channel, rec.id, date || undefined, rec.seq)`。
- 从 URL 读 `?date=` 与读 `?owner=` 同处理。

`ChannelTabs.tsx`：把显示 `count` 改为显示 `bytes`（如 `(x.bytes/1024).toFixed(0)+" KB"`）或直接去掉数字徽章（择一，保持对齐精致）。

`logs.css`：加 `.logview-trunc`（醒目提示条）与日期下拉样式，保持与既有 filters 对齐。

- [ ] **Step 4: 验证** `cd frontend && npm test`（顶层含新用例）通过；`npx tsc --noEmit` clean。
- [ ] **Step 5: Commit** `feat(fe/logs): 日期选择+默认今天+轮询仅当天+截断提示`

---

### Task 8: 仓库 list_user_notebooks

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Test: `backend/tests/test_admin_user_notebooks.py`

**Interfaces:**
- Produces: `list_user_notebooks(self, user_id: str) -> List[Dict[str, Any]]`，每项 `id/name/status/sources/conversations/reports/created_at/updated_at`。

- [ ] **Step 1: 失败测试** `backend/tests/test_admin_user_notebooks.py`（复用 `test_admin_users.py` 的 `repo` fixture 与 `_seed` 风格）

```python
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    return SQLiteRepository(Settings())


def _seed(repo):
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at)"
                   " VALUES (?,?,?,?,?,?,?,?)", ("u1", "u1@x", "U1", "user", "active", "a00000001", now, now))
        for nid, status in (("n1", "ready"), ("n2", "ready"), ("n3", "copying")):
            db.execute("INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,?)", (nid, f"NB-{nid}", "u1", status, now, now))
        for sid in ("s1", "s2"):
            db.execute("INSERT INTO sources (id,notebook_id,title,source_type,created_at,updated_at)"
                       " VALUES (?,?,?,?,?,?)", (sid, "n1", sid, "md", now, now))
        db.execute("INSERT INTO reports (id,notebook_id,question,created_at,updated_at)"
                   " VALUES (?,?,?,?,?)", ("r1", "n1", "q?", now, now))
        db.execute("INSERT INTO conversations (id,notebook_id,created_by,created_at,updated_at)"
                   " VALUES (?,?,?,?,?)", ("c1", "n1", "u1", now, now))


def test_list_user_notebooks_counts_and_excludes_copying(repo):
    _seed(repo)
    rows = repo.list_user_notebooks("u1")
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"n1", "n2"}                 # copying n3 排除
    assert by_id["n1"]["name"] == "NB-n1"
    assert by_id["n1"]["sources"] == 2
    assert by_id["n1"]["reports"] == 1
    assert by_id["n1"]["conversations"] == 1
    assert by_id["n2"]["sources"] == 0
    assert repo.list_user_notebooks("nobody") == []
```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现**（放在 `list_user_usage` 附近）：

```python
def list_user_notebooks(self, user_id: str) -> List[Dict[str, Any]]:
    """某用户名下笔记本 + 每本 sources/conversations/reports 计数。只读、固定条数
    GROUP BY，Python 按 notebook_id 合并，无 per-notebook N+1。排除 status='copying'。"""
    with self._connect() as db:
        nbs = db.execute(
            "SELECT id, name, status, created_at, updated_at FROM notebooks "
            "WHERE created_by = ? AND status != 'copying' ORDER BY created_at DESC",
            (user_id,)).fetchall()
        ids = [r["id"] for r in nbs]
        src, conv, rep = {}, {}, {}
        if ids:
            ph = ",".join("?" * len(ids))
            src = {r["k"]: r["c"] for r in db.execute(
                f"SELECT notebook_id AS k, COUNT(*) AS c FROM sources "
                f"WHERE notebook_id IN ({ph}) GROUP BY notebook_id", ids).fetchall()}
            conv = {r["k"]: r["c"] for r in db.execute(
                f"SELECT notebook_id AS k, COUNT(*) AS c FROM conversations "
                f"WHERE notebook_id IN ({ph}) GROUP BY notebook_id", ids).fetchall()}
            rep = {r["k"]: r["c"] for r in db.execute(
                f"SELECT notebook_id AS k, COUNT(*) AS c FROM reports "
                f"WHERE notebook_id IN ({ph}) GROUP BY notebook_id", ids).fetchall()}
    return [{"id": r["id"], "name": r["name"], "status": r["status"],
             "created_at": r["created_at"], "updated_at": r["updated_at"],
             "sources": src.get(r["id"], 0), "conversations": conv.get(r["id"], 0),
             "reports": rep.get(r["id"], 0)} for r in nbs]
```

（注：`ids` 数量 = 该用户笔记本数，通常远小于 SQLite 变量上限 999；如需极端规模再分批，本计划不做。）

- [ ] **Step 4: 运行通过**。
- [ ] **Step 5: Commit** `feat(admin): 仓库 list_user_notebooks(按用户列笔记本+计数)`

---

### Task 9: schema + 端点 GET /admin/users/{id}/notebooks

**Files:**
- Modify: `backend/app/models/schemas.py`（加 `AdminUserNotebook`）、`backend/app/api/routes.py`（加路由）
- Test: `backend/tests/test_admin_user_notebooks.py`（追加 API 测试）

**Interfaces:**
- Consumes: Task 8 `list_user_notebooks`；现有 `AdminUserUsage` 旁的 import 结构。

- [ ] **Step 1: 失败测试**（追加到 `test_admin_user_notebooks.py`，用 `test_admin_users.py` 同款 `client`/`_auth`/`_auth_admin`）

```python
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _auth(client, username):
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    t = client.post("/api/auth/login", json={"username": username, "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _auth_admin(client):
    t = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def test_user_notebooks_forbidden_for_regular(client):
    b = _auth(client, "z00123456")
    assert client.get("/api/admin/users/whoever/notebooks", headers=b).status_code == 403


def test_user_notebooks_lists_for_admin(client):
    admin = _auth_admin(client)
    a = _auth(client, "z00123456")
    uid = client.get("/api/me", headers=a).json()["id"]
    client.post("/api/notebooks", json={"name": "NB-One"}, headers=a)
    resp = client.get(f"/api/admin/users/{uid}/notebooks", headers=admin)
    assert resp.status_code == 200
    rows = resp.json()
    assert any(r["name"] == "NB-One" and "sources" in r for r in rows)
```

- [ ] **Step 2: 运行确认失败**。

- [ ] **Step 3: 实现**。`schemas.py`（`AdminUserUsage` 之后）：

```python
class AdminUserNotebook(BaseModel):
    id: str
    name: str
    status: str
    sources: int
    conversations: int
    reports: int
    created_at: str
    updated_at: str
```

`routes.py`：import 里加 `AdminUserNotebook`；`list_admin_users` 之后加：

```python
@router.get("/admin/users/{user_id}/notebooks", response_model=List[AdminUserNotebook])
def list_admin_user_notebooks(user_id: str, user: UserProfile = Depends(get_current_user)) -> List[AdminUserNotebook]:
    """某用户名下笔记本详情。仅 admin。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可查看用户笔记本")
    return [AdminUserNotebook(**row) for row in repository().list_user_notebooks(user_id)]
```

- [ ] **Step 4: 运行通过**。
- [ ] **Step 5: Commit** `feat(admin): GET /admin/users/{id}/notebooks(admin-only 下钻)`

---

### Task 10: 前端 /admin/usage 可展开行 + 懒加载笔记本子表

**Files:**
- Create: `frontend/app/admin/usage/notebooks.ts`
- Modify: `frontend/app/admin/usage/page.tsx`、`usage.css`
- Test: `frontend/app/admin-notebooks.test.mjs`（顶层）

**Interfaces:**
- Consumes: `GET /api/admin/users/{id}/notebooks`。

- [ ] **Step 1: 失败测试** `frontend/app/admin-notebooks.test.mjs`

```javascript
import { test } from "node:test";
import assert from "node:assert/strict";
import { notebookStatusLabel } from "./admin/usage/notebooks.ts";

test("notebookStatusLabel maps known + falls back", () => {
  assert.equal(notebookStatusLabel("ready"), "就绪");
  assert.equal(notebookStatusLabel("draft"), "草稿");
  assert.equal(notebookStatusLabel("weird"), "weird");
});
```

- [ ] **Step 2: 运行确认失败** `cd frontend && npm test`。

- [ ] **Step 3: 实现**。`notebooks.ts`：

```typescript
import { API_BASE, authHeaders } from "../../auth.ts";

export type AdminUserNotebook = {
  id: string; name: string; status: string;
  sources: number; conversations: number; reports: number;
  created_at: string; updated_at: string;
};

export async function fetchUserNotebooks(userId: string): Promise<AdminUserNotebook[]> {
  const res = await fetch(`${API_BASE}/admin/users/${encodeURIComponent(userId)}/notebooks`,
    { headers: authHeaders() });
  if (res.status === 403) throw new Error("forbidden");
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

const STATUS_CN: Record<string, string> = {
  ready: "就绪", draft: "草稿", processing: "处理中", error: "失败", copying: "复制中",
};
export function notebookStatusLabel(s: string): string {
  return STATUS_CN[s] ?? s;
}
```

`page.tsx`：每行加展开钮;`useState` 存 `expanded: string | null` 与 `nbCache: Record<string, AdminUserNotebook[] | "loading" | "error">`。点行 → 若无缓存则 `fetchUserNotebooks(u.id)` 存入缓存并展开;展开时在该用户行下插入一行 `<tr><td colSpan={9}>` 子表(笔记本名/状态`notebookStatusLabel`/来源/对话/报告/创建`formatLastActive`/最近更新)。加载中/空/错误占位。已缓存不重复请求。

`usage.css`：加 `.usage-subtable`、展开钮、行 hover 等，保持对齐精致。

- [ ] **Step 4: 验证** `npm test` 通过;`npx tsc --noEmit` clean。
- [ ] **Step 5: Commit** `feat(fe/admin): 用户总览行展开懒加载笔记本子表`

---

### Task 11: 共享「返回主页」页头栏 + 应用到两页

**Files:**
- Create: `frontend/app/components/PageHeader.tsx`、`frontend/app/components/page-header.css`
- Modify: `frontend/app/admin/usage/page.tsx`、`frontend/app/dev/logs/page.tsx`
- Test:（无独立逻辑;靠 `tsc` 与视觉验证）

**Interfaces:**
- Produces: `<PageHeader title="用户使用总览" />`（含「← 返回主页」链到 `/`）。

- [ ] **Step 1: 实现组件** `frontend/app/components/PageHeader.tsx`

```tsx
import "./page-header.css";

export function PageHeader({ title }: { title: string }) {
  return (
    <div className="page-header">
      <a className="page-header-back" href="/">← 返回主页</a>
      <span className="page-header-title">{title}</span>
    </div>
  );
}
```

`page-header.css`：一条窄栏,高度/内边距/字色对齐主页顶栏那一带(约 `height:48px; padding:0 20px; border-bottom:1px solid var(--border); display:flex; align-items:center; gap:16px;`),`.page-header-back` 用 accent 色、无下划线 hover 才有,`.page-header-title` 稍弱。

- [ ] **Step 2: 接入两页**。`admin/usage/page.tsx`:在 `ready` 分支 `<main>` 顶部渲染 `<PageHeader title="用户使用总览" />`(替换/置于 `<h1>` 之上,或去掉 `<h1>` 由页头承载标题)。`dev/logs/page.tsx`:在 `logview` 最顶插 `<PageHeader title="日志查看" />`。

- [ ] **Step 3: 验证** `cd frontend && npm test`(现有全绿)+ `npx tsc --noEmit` clean。

- [ ] **Step 4: Commit** `feat(fe): 独立管理页加返回主页页头栏(用户总览/日志)`

---

## 收尾（SDD 控制器执行，非单独任务）

- 全部任务过后:后端 `cd backend && python -m pytest -q` 全绿;前端 `cd frontend && npm test` + `npx tsc --noEmit` clean。
- 末尾派 opus 全分支评审(重点:emit 热路径 best-effort 无回归、字节偏移 seq 语义一致、归档不碰当天、date 参数无路径穿越、admin 403 契约、前端轮询仅当天)。
- rebase 到最新 origin/master 保持线性 → push → `gh pr create --base master`;PR 正文末尾附 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`。
