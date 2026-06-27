# 按用户隔离日志(events + llm)实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `events` 与 `llm` 两类日志按当前用户写入 `.local/logs/{user_id}/` 子目录,读取 API 加用户门控(普通用户只看自己、admin 可跨用户、requests 仅 admin),requests 保持全局。

**Architecture:** 在 core 层 `event_logging.py` 新增一个 `_log_owner` ContextVar,由 service 层 `set_request_user` 同步维护;`EventLogger` 增加 `per_user` 模式,`emit()` 时按 `_log_owner` 决定写哪个用户子目录。这样所有 logger(`event_log` + 多个 `LLMInteractionLogger`)无需逐个注入 resolver,后台 KG job 经 `copy_context` 也自然带上 owner。读取侧 `debug_logs.py` 加 `get_current_user` 依赖 + owner 白名单门控。eval 脚本改用 `log_reader` 的聚合 helper 读所有用户子目录。

**Tech Stack:** Python 3.13, FastAPI, pydantic-settings, contextvars, pytest。

> **机制说明(对已批准 spec 的细化):** spec「写入侧设计」举例用「注入 `owner_resolver` 回调」。本计划改用 core 层 `_log_owner` ContextVar——因为 `OpenAICompatibleClient`(`core/llm.py:49`)会被实例化多次(主/推理/KG/per-user),每个都建自己的 `LLMInteractionLogger`,逐个注入 resolver 繁琐且易漏。ContextVar 方案行为完全等价(按 owner 分目录、无用户回退 `user-local`、`_system` 兜底、core 不依赖 service),且更 DRY。

---

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/app/core/event_logging.py` | 修改 | `_log_owner` ContextVar + set/reset/get + `owner_dir()`/`is_safe_owner()`;`EventLogger` per_user 模式 |
| `backend/app/services/sqlite_repository.py` | 修改 | `set_request_user`/`reset_request_user` 同步 `_log_owner`;`event_log` 切 `per_user=True` |
| `backend/app/core/llm_logging.py` | 修改 | `LLMInteractionLogger` 切 per_user(base_dir 取 `llm_log_path` 父目录) |
| `backend/app/services/log_reader.py` | 修改 | 新增 `expand_channel_paths()` 聚合 helper |
| `backend/app/eval/ask_latency.py` | 修改 | `read_ask_stage_records` 聚合读所有用户子目录 |
| `backend/app/eval/speed.py` | 修改 | `parse_llm_log` 聚合读所有用户子目录 |
| `backend/app/api/debug_logs.py` | 修改 | 加 `get_current_user` 依赖 + owner 门控 + `_channel_path(owner)` |
| `backend/tests/test_event_logging.py` | 创建 | EventLogger per_user + log_owner ContextVar 单测 |
| `backend/tests/test_debug_logs.py` | 修改 | 改写为 owner 子目录 + 门控/隔离用例 |
| `backend/tests/eval/test_ask_latency.py` | 修改 | 新增聚合读取用例 |

测试命令统一从 `backend/` 运行(那里有 `conftest.py`,默认 `auth_optional=true`)。

---

## Task 1: EventLogger per-user 写入 + `_log_owner` ContextVar

**Files:**
- Modify: `backend/app/core/event_logging.py`(顶部 import、模块级新增、`EventLogger.__init__`、`emit`)
- Test: `backend/tests/test_event_logging.py`(创建)

- [ ] **Step 1: 写失败测试**

创建 `backend/tests/test_event_logging.py`:

```python
import json

from app.core.config import Settings
from app.core.event_logging import (
    EventLogger, set_log_owner, reset_log_owner, get_log_owner, owner_dir,
)


def _read(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_owner_dir_mapping():
    assert owner_dir(None) == "user-local"
    assert owner_dir("") == "user-local"
    assert owner_dir("user-local") == "user-local"
    assert owner_dir("a00123456") == "a00123456"
    assert owner_dir("../etc/passwd") == "_system"
    assert owner_dir("Robert'); DROP") == "_system"


def test_per_user_writes_to_owner_subdir(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events", per_user=True)
    tok = set_log_owner("a00123456")
    try:
        log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "a00123456" / "events.jsonl").exists()
    assert not (tmp_path / "events.jsonl").exists()
    assert _read(tmp_path / "a00123456" / "events.jsonl")[0]["kind"] == "k"


def test_per_user_no_owner_falls_back_to_user_local(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events", per_user=True)
    log.emit({"kind": "k", "status": "ok"})  # ContextVar 未设
    assert (tmp_path / "user-local" / "events.jsonl").exists()


def test_per_user_illegal_owner_falls_back_to_system(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events", per_user=True)
    tok = set_log_owner("../escape")
    try:
        log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "_system" / "events.jsonl").exists()


def test_non_per_user_writes_global(tmp_path):
    log = EventLogger(Settings(event_log_dir=str(tmp_path)), channel="events")  # per_user=False
    tok = set_log_owner("a00123456")
    try:
        log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "events.jsonl").exists()        # 全局,忽略 owner
    assert not (tmp_path / "a00123456").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_event_logging.py -v`
Expected: FAIL — `ImportError: cannot import name 'set_log_owner'`(函数尚不存在)。

- [ ] **Step 3: 实现**

在 `backend/app/core/event_logging.py` 顶部 import 区(现有 `import json` / `import logging` 附近)加:

```python
import contextvars
import re
```

在 `new_id()` 函数之后(约 27 行后)新增模块级:

```python
# 请求级「日志归属」槽：由 sqlite_repository.set_request_user 同步维护，emit 时据此
# 决定写哪个用户子目录。与 _REQUEST_USER 配对，但定义在 core 层以免 EventLogger 依赖
# service 层（保持分层）。后台 job 经 contextvars.copy_context() 自然带上。
_log_owner: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "log_owner", default=None)

_OWNER_RE = re.compile(r"^[a-z]00\d{6}$")


def set_log_owner(owner: "str | None"):
    return _log_owner.set(owner)


def reset_log_owner(token) -> None:
    _log_owner.reset(token)


def get_log_owner() -> "str | None":
    return _log_owner.get()


def is_safe_owner(owner: str) -> bool:
    """owner 子目录名白名单：注册用户 id（^[a-z]00\\d{6}$）、内置 user-local、系统兜底
    _system。用于读取侧防路径穿越。"""
    return owner in ("user-local", "_system") or bool(_OWNER_RE.match(owner or ""))


def owner_dir(owner: "str | None") -> str:
    """把当前 owner 映射到日志子目录名。空/未设 → 'user-local'（离线/本地即 seeded
    admin）；非法（理论不出现，owner 来自 user.id）→ '_system' 兜底。"""
    if not owner:
        return "user-local"
    return owner if is_safe_owner(owner) else "_system"
```

把 `EventLogger.__init__`(当前 31-44 行)整体替换为:

```python
    def __init__(self, settings: Settings, channel: str = "events", *, per_user: bool = False):
        self.channel = channel
        self.enabled = getattr(settings, "event_log_enabled", True)
        self.max_chars = max(0, int(getattr(settings, "llm_log_max_chars", 4000)))
        self.logger = logging.getLogger(f"silicon_notebook.{channel}")
        log_dir = Path(getattr(settings, "event_log_dir", ".local/logs"))
        if not log_dir.is_absolute():
            log_dir = _ROOT_DIR / log_dir
        self.per_user = per_user
        self.log_dir = log_dir
        self.filename = f"{channel}.jsonl"
        self.path = log_dir / self.filename  # 全局路径（per_user=False 用；也是历史兼容属性）
        if self.enabled and not per_user:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            except Exception:  # pragma: no cover - never break startup
                self.logger.warning("could not create log dir at %s", self.path.parent)
```

把 `emit`(当前 52-75 行)整体替换为(新增 `_target_path` + emit 内按需 mkdir):

```python
    def _target_path(self) -> Path:
        if not self.per_user:
            return self.path
        try:
            sub = owner_dir(get_log_owner())
        except Exception:  # pragma: no cover - owner 解析绝不破坏写入
            sub = "_system"
        return self.log_dir / sub / self.filename

    def emit(self, event: Dict[str, Any], *, console: str = "") -> None:
        """Append `event` as a JSON line and emit a brief console line.

        `console` overrides the auto console summary. Wrapped so a logging
        failure never propagates to the caller.
        """
        if not self.enabled:
            return
        event.setdefault("ts", datetime.now().isoformat())
        event.setdefault("channel", self.channel)
        target = self._target_path()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:  # pragma: no cover - logging must not break flow
            self.logger.warning("failed to write %s log line", self.channel, exc_info=False)

        status = event.get("status")
        line = console or self._auto_console(event)
        if status in (None, "ok", "done", "start"):
            self.logger.info("%s", line)
        elif status == "slow":
            self.logger.warning("SLOW %s", line)
        else:
            self.logger.warning("%s", line)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_event_logging.py -v`
Expected: PASS(5 passed)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/event_logging.py backend/tests/test_event_logging.py
git commit -m "feat(logs): EventLogger per-user 模式 + log_owner ContextVar"
```

---

## Task 2: `set_request_user` 同步 `_log_owner`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:177-183`(`set_request_user` / `reset_request_user`)
- Test: `backend/tests/test_event_logging.py`(追加一个用例)

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_event_logging.py` 末尾追加:

```python
def test_set_request_user_syncs_log_owner(tmp_path):
    from app.core.config import Settings
    from app.services.sqlite_repository import (
        SQLiteRepository, set_request_user, reset_request_user,
    )
    repo = SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))
    user = repo.create_user("a00123456", "pw")
    assert get_log_owner() is None
    tok = set_request_user(user)
    try:
        assert get_log_owner() == "a00123456"
    finally:
        reset_request_user(tok)
    assert get_log_owner() is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_event_logging.py::test_set_request_user_syncs_log_owner -v`
Expected: FAIL — `assert None == 'a00123456'`(尚未同步)。

- [ ] **Step 3: 实现**

把 `backend/app/services/sqlite_repository.py:177-183` 的 `set_request_user` / `reset_request_user` 替换为:

```python
def set_request_user(user: "UserProfile | None"):
    """设当前请求用户，返回 token 供 reset_request_user 复位。
    同步设置 core 层 _log_owner，使 per-user 日志写入对应用户子目录。"""
    from app.core.event_logging import set_log_owner
    tok_user = _REQUEST_USER.set(user)
    tok_owner = set_log_owner(user.id if user is not None else None)
    return (tok_user, tok_owner)


def reset_request_user(token) -> None:
    from app.core.event_logging import reset_log_owner
    tok_user, tok_owner = token
    _REQUEST_USER.reset(tok_user)
    reset_log_owner(tok_owner)
```

> 注:`token` 由不透明的单值变为二元组,所有调用方(`deps.py:45/49`、各测试)都只是「持有再回传」,无需改动。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_event_logging.py -v`
Expected: PASS(6 passed)。

- [ ] **Step 5: 回归 ContextVar / owner 相关测试**

Run: `cd backend && python -m pytest tests/test_request_user_ctx.py tests/test_notebook_owner_scope.py tests/test_kg_job_user_context.py -v`
Expected: PASS(token 元组化向后兼容)。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_event_logging.py
git commit -m "feat(logs): set_request_user 同步 log_owner ContextVar"
```

---

## Task 3: 接线 `event_log` 与 `LLMInteractionLogger` 为 per-user

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:250`(`event_log` 构造)
- Modify: `backend/app/core/llm_logging.py:26-40`(`LLMInteractionLogger.__init__`)
- Test: `backend/tests/test_event_logging.py`(追加两个用例)

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_event_logging.py` 末尾追加:

```python
def test_llm_logger_per_user(tmp_path):
    from app.core.config import Settings
    from app.core.llm_logging import LLMInteractionLogger
    s = Settings(
        llm_log_path=str(tmp_path / "logs" / "llm.jsonl"),
        llm_log_enabled=True,
    )
    logger = LLMInteractionLogger(s)
    tok = set_log_owner("a00123456")
    try:
        logger.log({"kind": "chat", "model": "m", "status": "ok", "latency_ms": 1})
    finally:
        reset_log_owner(tok)
    assert (tmp_path / "logs" / "a00123456" / "llm.jsonl").exists()
    assert not (tmp_path / "logs" / "llm.jsonl").exists()


def test_repo_event_log_is_per_user(tmp_path):
    from app.core.config import Settings
    from app.services.sqlite_repository import (
        SQLiteRepository, set_request_user, reset_request_user,
    )
    repo = SQLiteRepository(Settings(
        database_url=f"sqlite:///{tmp_path}/t.db",
        event_log_dir=str(tmp_path / "logs"),
    ))
    user = repo.create_user("a00123456", "pw")
    tok = set_request_user(user)
    try:
        repo.event_log.emit({"kind": "k", "status": "ok"})
    finally:
        reset_request_user(tok)
    assert (tmp_path / "logs" / "a00123456" / "events.jsonl").exists()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_event_logging.py::test_llm_logger_per_user tests/test_event_logging.py::test_repo_event_log_is_per_user -v`
Expected: FAIL — 文件落在全局 `logs/llm.jsonl` / `logs/events.jsonl`,子目录断言失败。

- [ ] **Step 3: 实现**

`backend/app/services/sqlite_repository.py:250`,把:

```python
        self.event_log = EventLogger(settings, channel="events")
```

改为:

```python
        self.event_log = EventLogger(settings, channel="events", per_user=True)
```

`backend/app/core/llm_logging.py`,把 `LLMInteractionLogger.__init__`(当前 26-40 行)整体替换为:

```python
    def __init__(self, settings: Settings):
        # Reuse EventLogger's single write/console implementation in per-user mode;
        # honor the dedicated LLM settings (path + enable flag) for backward compat.
        self._events = EventLogger(settings, channel="llm", per_user=True)
        self._events.enabled = settings.llm_log_enabled
        self._events.max_chars = max(0, int(settings.llm_log_max_chars))
        path = Path(settings.llm_log_path)
        if not path.is_absolute():
            path = _ROOT_DIR / path
        # per-user：base_dir + filename 取自 llm_log_path；owner 子目录在 emit 按需建。
        self._events.log_dir = path.parent
        self._events.filename = path.name
        self._events.path = path  # 兼容属性（.path 仍被 smoke / llm.py 读取）
```

> 注:删掉原 `__init__` 末尾的预 `mkdir`(per_user 模式由 `emit` 按 owner 建目录)。`enabled` / `path` / `clip` / `log` 等其余成员不变。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_event_logging.py -v`
Expected: PASS(8 passed)。

- [ ] **Step 5: 回归 LLM 日志相关 smoke 测试**

Run: `cd backend && python -m pytest tests/ -k "llm or smoke or event" -v`
Expected: PASS(`.path` / `.enabled` 兼容属性保留,旧用例不破)。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/app/core/llm_logging.py backend/tests/test_event_logging.py
git commit -m "feat(logs): event_log 与 LLMInteractionLogger 切 per-user 写入"
```

---

## Task 4: `log_reader` 聚合 helper

**Files:**
- Modify: `backend/app/services/log_reader.py`(在 `load_records` 之后新增函数)
- Test: `backend/tests/test_event_logging.py`(追加一个用例)

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_event_logging.py` 末尾追加:

```python
def test_expand_channel_paths(tmp_path):
    from app.services.log_reader import expand_channel_paths
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "events.jsonl").write_text("legacy\n", encoding="utf-8")           # 旧全局
    (logs / "a00123456").mkdir()
    (logs / "a00123456" / "events.jsonl").write_text("u1\n", encoding="utf-8")
    (logs / "user-local").mkdir()
    (logs / "user-local" / "events.jsonl").write_text("u2\n", encoding="utf-8")
    (logs / "a00123456" / "llm.jsonl").write_text("other-channel\n", encoding="utf-8")  # 不应混入

    paths = expand_channel_paths(logs / "events.jsonl")
    rels = [str(p.relative_to(logs)) for p in paths]
    assert rels == ["events.jsonl", "a00123456/events.jsonl", "user-local/events.jsonl"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_event_logging.py::test_expand_channel_paths -v`
Expected: FAIL — `ImportError: cannot import name 'expand_channel_paths'`。

- [ ] **Step 3: 实现**

在 `backend/app/services/log_reader.py` 的 `load_records` 函数之后新增:

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_event_logging.py::test_expand_channel_paths -v`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/log_reader.py backend/tests/test_event_logging.py
git commit -m "feat(logs): log_reader.expand_channel_paths 聚合所有用户子目录"
```

---

## Task 5: eval 脚本聚合读取

**Files:**
- Modify: `backend/app/eval/ask_latency.py:87-122`(`read_ask_stage_records`)
- Modify: `backend/app/eval/speed.py:24-?`(`parse_llm_log` 读取部分)
- Test: `backend/tests/eval/test_ask_latency.py`(追加一个用例)

- [ ] **Step 1: 写失败测试**

在 `backend/tests/eval/test_ask_latency.py` 的 `class TestReadAskStageRecords` 内追加:

```python
    def test_aggregates_owner_subdirs(self, tmp_path):
        """全局文件不存在时，聚合读取所有用户子目录下的 events.jsonl。"""
        (tmp_path / "a00123456").mkdir()
        (tmp_path / "user-local").mkdir()
        (tmp_path / "a00123456" / "events.jsonl").write_text(
            json.dumps({"kind": "ask_stage", "stage": "score", "latency_ms": 10}) + "\n")
        (tmp_path / "user-local" / "events.jsonl").write_text(
            json.dumps({"kind": "ask_stage", "stage": "score", "latency_ms": 20}) + "\n")
        recs = list(read_ask_stage_records(str(tmp_path / "events.jsonl")))
        assert sorted(r["latency_ms"] for r in recs) == [10, 20]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/eval/test_ask_latency.py::TestReadAskStageRecords::test_aggregates_owner_subdirs -v`
Expected: FAIL — `assert [] == [10, 20]`(只读全局文件,子目录被忽略)。

- [ ] **Step 3: 实现 ask_latency**

把 `backend/app/eval/ask_latency.py` 的 `read_ask_stage_records`(当前 87-122 行)整体替换为:

```python
def read_ask_stage_records(
    path: str,
    last_n: Optional[int] = None,
) -> Iterator[dict]:
    """Stream ask_stage records from a JSONL channel.

    Aggregates the legacy global file (`path`) and all per-user subdir files
    (`<log_dir>/*/<basename>`). Skips malformed/blank lines and records where
    ``kind != "ask_stage"``. Empty iterator if nothing exists.

    Args:
        path:   Path to the global events JSONL file (default:
                .local/logs/events.jsonl). Per-user subdirs beside it are
                aggregated automatically.
        last_n: If given, only yield the last N matching records (after merge).
    """
    from app.services.log_reader import expand_channel_paths

    parsed: list[dict] = []
    for p in expand_channel_paths(Path(path)):
        try:
            raw_lines = p.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        for line in raw_lines:
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
```

- [ ] **Step 4: 实现 speed.parse_llm_log**

把 `backend/app/eval/speed.py` 的 `parse_llm_log`(当前 24 行起)的读取部分替换 —— 将:

```python
    try:
        raw = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        return {"calls": 0, "retries": 0, "latency_p50_s": 0.0,
                "latency_p95_s": 0.0, "total_tokens": 0}
    for line in raw:
```

替换为:

```python
    from pathlib import Path
    from app.services.log_reader import expand_channel_paths
    raw: List[str] = []
    for p in expand_channel_paths(Path(path)):
        try:
            raw.extend(p.read_text(encoding="utf-8").splitlines())
        except FileNotFoundError:
            continue
    if not raw:
        return {"calls": 0, "retries": 0, "latency_p50_s": 0.0,
                "latency_p95_s": 0.0, "total_tokens": 0}
    for line in raw:
```

> 其余 `parse_llm_log` 逻辑(按 `since_ts`/status 过滤、分位数)不变。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/eval/test_ask_latency.py -v`
Expected: PASS(原有用例 + 新增聚合用例全过;传入存在的全局文件仍被 `expand_channel_paths` 包含,向后兼容)。

- [ ] **Step 6: 提交**

```bash
git add backend/app/eval/ask_latency.py backend/app/eval/speed.py backend/tests/eval/test_ask_latency.py
git commit -m "feat(logs): eval 脚本聚合读取所有用户日志子目录"
```

---

## Task 6: debug_logs 读取 API 用户门控 + admin 跨用户

**Files:**
- Modify: `backend/app/api/debug_logs.py`(整体改写:加 `get_current_user`、owner 门控、`_channel_path(owner)`)
- Test: `backend/tests/test_debug_logs.py`(改写 `_make_client` + 新增门控用例)

- [ ] **Step 1: 改写测试**

把 `backend/tests/test_debug_logs.py` 顶部的 `_make_client`(当前 6-20 行)替换为(加 DB 隔离 + owner 子目录写入 + 单例缓存清理),并在文件末尾追加门控用例:

```python
def _make_client(tmp_path, monkeypatch, *, enabled=True, lines=None, channel="llm", owner="user-local"):
    logs = tmp_path / "logs"
    (logs / owner).mkdir(parents=True, exist_ok=True)
    if lines is not None:
        (logs / owner / f"{channel}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("EVENT_LOG_DIR", str(logs))  # absolute -> used as-is
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")  # 隔离 DB（seeded admin=user-local）
    if enabled is not None:
        monkeypatch.setenv("DEBUG_LOGS_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _login(username="a00123456"):
    """在 client 的同一 tmp DB 单例里造用户并发 session token。"""
    from app.api import deps
    repo = deps.repository()
    user = repo.create_user(username, "pw")
    return user, repo.create_session(user.id)
```

> 同时把现有用例里写到全局 `logs/{channel}.jsonl` 的隐含假设修正:现在默认无 token → seeded admin(`user-local`),`_make_client` 已把 `lines` 写到 `logs/user-local/{channel}.jsonl`,admin 默认读自己(user-local),故 `test_list_channels` / `test_list_records_and_stats` / `test_filters_and_search` / `test_pagination` / `test_detail_by_id_and_404` / `test_malformed_line_skipped` / `test_list_channels_count_excludes_malformed` 无需改断言,直接复用。

在 `backend/tests/test_debug_logs.py` 末尾追加:

```python
def test_normal_user_sees_only_own(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    user, token = _login("a00123456")
    (tmp_path / "logs" / user.id).mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / user.id / "llm.jsonl").write_text(CHAT + "\n", encoding="utf-8")
    h = {"Authorization": f"Bearer {token}"}
    body = c.get("/api/debug/logs/llm", headers=h).json()
    assert [r["id"] for r in body["records"]] == ["llm-a"]


def test_normal_user_cannot_read_others_owner(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    user, token = _login("a00123456")
    h = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/debug/logs/llm?owner=b00999999", headers=h).status_code == 403


def test_admin_can_read_any_owner(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)  # 无 token → seeded admin
    (tmp_path / "logs" / "b00999999").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "b00999999" / "llm.jsonl").write_text(CHAT + "\n", encoding="utf-8")
    body = c.get("/api/debug/logs/llm?owner=b00999999").json()
    assert [r["id"] for r in body["records"]] == ["llm-a"]


def test_requests_channel_admin_only(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    user, token = _login("a00123456")
    h = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/debug/logs/requests", headers=h).status_code == 403
    assert c.get("/api/debug/logs/requests").status_code == 200  # admin（无 token）可读


def test_admin_owner_traversal_rejected(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    assert c.get("/api/debug/logs/llm?owner=../../etc").status_code == 404


def test_requests_channel_hidden_from_normal_user_list(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    user, token = _login("a00123456")
    h = {"Authorization": f"Bearer {token}"}
    names = {ch["name"] for ch in c.get("/api/debug/logs", headers=h).json()["channels"]}
    assert "requests" not in names and {"events", "llm"} <= names
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_debug_logs.py -v`
Expected: FAIL — 新门控用例失败(当前 API 无 `owner` 参数、无认证,普通用户能读 requests / 别人 owner)。

- [ ] **Step 3: 实现**

把 `backend/app/api/debug_logs.py` 整体替换为:

```python
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


def _channel_path(settings: Settings, channel: str, owner: Optional[str]) -> Path:
    filename = log_reader.CHANNELS.get(channel)
    if filename is None:
        raise HTTPException(status_code=404, detail=f"unknown channel: {channel}")
    log_dir = Path(settings.event_log_dir)
    if not log_dir.is_absolute():
        log_dir = _ROOT_DIR / log_dir
    return log_dir / filename if owner is None else log_dir / owner / filename


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
        path = _channel_path(settings, name, target)
        records, _ = log_reader.load_records(path)
        out.append(
            {"name": name, "file": filename, "exists": path.exists(), "count": len(records)}
        )
    return {"channels": out}


@router.get("/{channel}")
def list_records(
    channel: str,
    user: UserProfile = Depends(get_current_user),
    settings: Settings = Depends(require_enabled),
    owner: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=2000),
    before: Optional[int] = Query(None, description="Return records with seq < before (older page)"),
    since: Optional[int] = Query(None, description="Return records with seq > since (newer; for polling)"),
    kind: Optional[str] = None,
    status: Optional[str] = None,
    model: Optional[str] = None,
    q: Optional[str] = None,
):
    target = _resolve_owner(channel, user, owner)
    path = _channel_path(settings, channel, target)
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
    user: UserProfile = Depends(get_current_user),
    settings: Settings = Depends(require_enabled),
    owner: Optional[str] = Query(None),
):
    target = _resolve_owner(channel, user, owner)
    path = _channel_path(settings, channel, target)
    records, _ = log_reader.load_records(path)
    matches = [r for r in records if r.get("id") == record_id]
    if not matches:
        raise HTTPException(status_code=404, detail=f"record not found: {record_id}")
    return matches[-1]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_debug_logs.py -v`
Expected: PASS(原有 + 新增门控用例全过)。

- [ ] **Step 5: 提交**

```bash
git add backend/app/api/debug_logs.py backend/tests/test_debug_logs.py
git commit -m "feat(logs): debug_logs 读取 API 用户门控 + admin 跨用户 + requests 仅 admin"
```

---

## Task 7: 全量回归

- [ ] **Step 1: 跑后端全量测试**

Run: `cd backend && python -m pytest -q`
Expected: 全绿(若个别用例因真实 DB / 网络跳过属正常,不应有 fail)。

- [ ] **Step 2: 跑 smoke 脚本(可选,本地)**

Run: `python scripts/smoke_backend.py`
Expected: 通过 / 与改动前一致。

- [ ] **Step 3: 若全绿,无额外提交;否则修复后补提交。**

---

## Self-Review

**Spec coverage:**
- events + llm 按 user_id 分目录 → Task 1(机制)+ Task 3(接线)。✅
- 无用户回退 user-local、异常 `_system` → Task 1 `owner_dir`。✅
- requests 不动 → 未改 `main.py` 的 `request_log`(默认 `per_user=False`)。✅
- 读取 API 普通用户只看自己 / admin 跨用户 / requests 仅 admin / owner 防穿越 → Task 6 `_resolve_owner` + `is_safe_owner`。✅
- 后台 KG job 不改(copy_context 传播)→ Task 2 同步的是 ContextVar,自动随 `copy_context`;Task 2 Step 5 回归 `test_kg_job_user_context`。✅
- 历史日志不迁移、聚合 helper 兼容 → Task 4 `expand_channel_paths`(含旧全局文件)。✅
- eval 同步改 → Task 5。✅
- 更新现有 test_debug_logs / test_ask_latency → Task 6 / Task 5。✅

**Placeholder scan:** 无 TODO/TBD;每个改动步骤含完整代码。✅

**Type consistency:** `set_log_owner`/`reset_log_owner`/`get_log_owner`/`owner_dir`/`is_safe_owner`(event_logging)、`expand_channel_paths`(log_reader)、`_resolve_owner`/`_channel_path`(debug_logs)在定义处与调用处签名一致;`set_request_user` 返回的二元组 token 在 `reset_request_user` 解包,调用方透明。✅
