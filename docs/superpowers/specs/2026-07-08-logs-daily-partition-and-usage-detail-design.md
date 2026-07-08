# 日志按天分文件归档 + 用户总览笔记本下钻 + 返回主页 设计

> 承接 PR#209（管理员用户使用总览）。本设计完善三点：①日志查看卡死 → 日志按天分文件、旧天自动压缩归档、默认只读当天；②`/admin/usage` 能下钻看某用户名下笔记本详情；③独立管理页加「返回主页」。

**日期：** 2026-07-08
**分支：** `feat/logs-daily-partition`
**基线：** origin/master `edb87d7`

---

## 目标

1. **日志按天分文件 + 旧天自动归档，读取有界不再卡死。** 当前 `log_reader.load_records()` 每次请求把整个 channel 文件读进内存并逐行 `json.loads`；批量摄取会把 `llm.jsonl` 灌到 GB 级、上百万行，导致「一进日志页就卡死」。改为按天分文件（`<channel>-YYYY-MM-DD.jsonl`），前序天 gzip 归档，查看器默认只读当天、可选历史某天，读取按天有界 + 尾部封顶。
2. **用户总览可下钻笔记本详情。** `/admin/usage` 表格行可展开，懒加载该用户名下的笔记本（名称/状态/来源数/对话数/报告数/创建/最近更新）。
3. **独立管理页加返回主页。** `/admin/usage` 与 `/dev/logs` 顶部各加一条窄页头栏（页名 + 「← 返回主页」链到 `/`），位置对齐主页顶栏那一带。

## 全局约束（每个任务都隐含遵守）

- **交互与文案中文**；前端中文弯引号 `""` 是有意的模板/JSX 文本，不得批量替直引号。
- **运行效率是一等约束**：新增读写路径必须有界；写入侧（`emit` 热路径）新增逻辑必须 O(1) 摊还、best-effort、绝不阻塞或抛错破坏被观测的请求/流水线。
- **归档压缩绝不触碰「当天活跃文件」**：只压 `day < today` 的文件。
- **日志写入 best-effort**：日期计算/切文件/归档入队的任何异常都不得传播出 `emit`。
- **不删数据、不迁移历史**：现存单文件 `llm.jsonl`/`events.jsonl`/`requests.jsonl` 保留只读，不改写、不切分。
- **owner 与路径安全**：per-user 日志目录名仍是 `user.id`（`user-local` / `user-<hex>` / `_system`），沿用 `is_safe_owner`；新增的 `date` 参数必须校验 `^\d{4}-\d{2}-\d{2}$` 或字面量 `legacy`，防路径穿越。
- **admin 门控**：新增 `/admin/*` 端点一律 `user.role == "admin"` 否则 403；前端做 403 纵深防御（不只靠隐藏入口）。
- **schema 迁移铁律**：如需改 DB 结构，必须 bump `SCHEMA_VERSION` + 新 `_migration_N`，不得追加进已封版迁移（本设计预计**不需要**改 DB schema）。
- **测试铁律**：前端 helper 测试放 `frontend/app/` 顶层（`npm test` = `node --test app/*.test.mjs` 只匹配顶层，嵌套不跑）。

---

## Part 1 — 日志按天分文件 + 自动归档

### 1.1 写入侧：带日期的文件名

`backend/app/core/event_logging.py` 的 `EventLogger`：

- 文件名从 `f"{channel}.jsonl"` 改为**当天** `f"{channel}-{today}.jsonl"`，`today = datetime.now().strftime("%Y-%m-%d")`。`emit()` 已调 `datetime.now()` 取 `ts`，从同一个 `now` 取 date，零额外系统调用。
- per-user 路径：`<log_dir>/<owner>/<channel>-<today>.jsonl`；全局（requests）：`<log_dir>/<channel>-<today>.jsonl`。
- `_target_path()` 内联当天日期即可；`self.path`（历史全局属性）保留为**旧的无日期路径**仅作兼容，不再写入。

### 1.2 写入侧：跨天补压（O(1) 摊还）

模块级状态 + 单线程执行器（都在 `event_logging.py`）：

```python
# 键：写入目标目录 + channel，唯一标识一条「按天序列」。值：该序列上次写入的日期串。
_last_write_day: Dict[Tuple[str, str], str] = {}
_last_write_lock = threading.Lock()
_archive_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="log-archive")
```

`emit()` 写完当天文件后：

```python
key = (str(target_dir), self.channel)
with _last_write_lock:
    prev = _last_write_day.get(key)
    if prev != today:
        _last_write_day[key] = today
        rollover = prev is not None  # 本进程内确实翻了天（非首写）
if rollover:
    _archive_pool.submit(_gzip_day_file, target_dir / f"{self.channel}-{prev}.jsonl")
```

- `prev is None`（本进程对该序列首写）**不压**：无从判断是翻天还是刚启动；启动扫一遍已负责历史旧天。
- 命中 rollover **仅一次/天/序列**：此后 `prev == today` 直接跳过，热路径只是一次 dict 查（加轻量锁）。
- gzip 在后台线程，绝不阻塞 `emit`。

### 1.3 归档压缩助手（原子、幂等、best-effort）

```python
def _gzip_day_file(plain: Path) -> None:
    """把某天的明文 jsonl 压成 .gz。只压存在且未压的；先写 .gz.tmp 再原子 rename，
    然后删明文——读取器「先试明文、缺则试 .gz」，故任一时刻至少有一份可读，且绝不会
    读到半个 .gz。任何异常吞掉（归档失败不致命，下次启动扫一遍会补）。"""
```

- 守卫：`plain.exists() and not gz.exists()`；`today` 文件永不进这里（调用点已保证 `day < today`）。
- 原子：`gzip` 写到 `plain.with_suffix(".jsonl.gz.tmp")` → `os.replace` 成 `.gz` → `plain.unlink()`。
- 幂等：重复调用对已压的天是 no-op。

### 1.4 启动扫一遍（后台，不阻塞启动）

`event_logging.py` 提供：

```python
def archive_stale_days(settings) -> None:
    """glob 全部 per-user 与全局的 <channel>-YYYY-MM-DD.jsonl，凡 date < today 且无 .gz
    兄弟者，提交 _archive_pool 压缩。best-effort。"""
```

`backend/app/main.py` `create_app()` 内（settings 就绪后）**提交到 `_archive_pool` 后台执行**（不同步阻塞 uvicorn 启动；首次部署积压的历史旧天异步压）。包 try/except，失败仅告警。

### 1.5 读取侧：按天有界读，`seq = 天内字节偏移`

新读取原语（放 `backend/app/services/log_reader.py`，与现有纯函数并存）。**`seq` 语义从「行号」改为「行首在该天文件内的字节偏移」**：

- 字节偏移在尾部/区间读时**顺手可得**，无需全文扫描即可赋值；
- 单调（偏移越大越新）、追加稳定（一行的偏移永不变）、行内唯一 → 满足前端把 `seq` 当不透明数值用于 React key / 去重 / `before`·`since` 游标。

原语：

- `available_days(dir, channel) -> List[str]`：glob `<dir>/<channel>-*.jsonl` 与 `*.jsonl.gz`，抽出 `YYYY-MM-DD` 去重降序；若存在旧无日期 `<channel>.jsonl` 追加字面量 `"legacy"` 于末尾。
- `resolve_day_path(dir, channel, date) -> (Path, is_gzip)`：`date=="legacy"` → 旧 `<channel>.jsonl`；否则优先 `<channel>-<date>.jsonl`（明文），缺则 `.jsonl.gz`。
- `load_day_window(path, is_gzip, *, before, since, limit, max_records, max_bytes) -> (records, malformed, truncated)`：
  - **明文（当天热路径）**：`before` 缺省 → 尾读 `[max(0,size-max_bytes), size)`，丢首个可能残行，按偏移赋 `seq`，取最新 `limit`；`since` → seek 到 `since` 向后读到 EOF 取 `seq>since`；`before` → 读 `[max(0,before-max_bytes), before)` 取 `seq<before` 的最后 `limit` 行。
  - **gz（历史，不可变、无轮询）**：流式解压逐行，用 `deque(maxlen=max_records)` 滚动保留最新若干行（有界内存），偏移用解压流累计计数；再按 `before`/`limit` 切片。
  - `truncated`：尾读丢了更旧的行 / gz 触及 `maxlen` → True，供前端标注「已截断，仅显示最近 N 条」。
- 现有 `filter_records` / `to_summary` / `_preview` / `compute_stats` / `paginate` **纯函数不变**，对 `load_day_window` 返回的记录照常用（stats/facets 现基于「当天窗口」而非全文，更贴切；标注范围）。

默认封顶常量（`log_reader` 内，可留注释说明依据）：`MAX_RECORDS_PER_WINDOW = 50_000`，`MAX_TAIL_BYTES = 32 * 1024 * 1024`。

### 1.6 端点变化（`backend/app/api/debug_logs.py`）

- `GET /debug/logs`（列 channel）：**不再全量 load 计数**。`count` 字段去掉（或恒 0 保结构），改报当天文件 `st_size`（`stat`，不解析）+ `exists`；真实条数/统计由 records 端点的 `stats` 承载。
- `GET /debug/logs/{channel}/days`（新）：返回 `available_days`（合并该 owner 的 per-user 目录）。owner 解析沿用 `_resolve_owner`。前端**单独拉此端点**取日期列表，不内嵌进 records 响应（日期列表变化慢，分开更简洁）。
- `GET /debug/logs/{channel}`：新增 `date: Optional[str]`（校验 `^\d{4}-\d{2}-\d{2}$|^legacy$`，非法 400/404）。缺省 = 今天 `strftime`。走 `resolve_day_path` + `load_day_window`。保留 `before/since/limit/kind/status/model/q`。响应新增 `date`、`truncated`（不含 `days`）。
- `GET /debug/logs/{channel}/{record_id}`：新增 `date` 与可选 `seq`。有 `seq`（字节偏移）→ seek 单行 O(1) 校验 id；无 `seq` → 在该天窗口内按 id 找（有界）。
- 全局 `requests` channel 同样按天分文件（`requests-YYYY-MM-DD.jsonl`），admin-only 不变。

### 1.7 前端 `/dev/logs`

`frontend/app/dev/logs/`：

- 新增**日期选择**：拉 `GET /debug/logs/{channel}/days`，渲染下拉「今天(默认) / <每个历史天> / 历史(未分天)=legacy」。`date` 进 URL（`?date=`）与所有 fetch 参数、轮询依赖。
- 默认 `date` = 今天（前端本地 `toISOString` 取 `YYYY-MM-DD` 或不传由后端兜底为今天）。
- **轮询只对「今天」开**：查看历史某天时禁用/隐藏「自动刷新」（历史文件不再增长）。
- `before`/`since`/`newest_seq`/去重逻辑不动（`seq` 仍是不透明数值）。
- detail 请求带上当前 `date` 与所选行 `seq`。
- `truncated` 为真时在列表顶部提示「已截断，仅显示最近 N 条，请缩小时间范围或选择具体某天」。
- 顶部加返回主页页头栏（见 Part 3）。

---

## Part 2 — 用户总览下钻笔记本

### 2.1 仓库方法（只读，无 N+1）

`backend/app/services/sqlite_repository.py` 新增：

```python
def list_user_notebooks(self, user_id: str) -> List[Dict[str, Any]]:
    """某用户名下的笔记本 + 每本的来源/对话/报告数。固定条数 GROUP BY 聚合，Python 按
    notebook_id 合并，无 per-notebook N+1。排除 status='copying' 的半拷贝哨兵。只读。"""
```

- 主查询：`SELECT id, name, status, created_at, updated_at FROM notebooks WHERE created_by=? AND status!='copying' ORDER BY created_at DESC`。
- 计数：`sources`（`GROUP BY notebook_id WHERE notebook_id IN (该用户的本)`，或 JOIN notebooks 过滤 `created_by`）、`conversations`、`reports` 各一条 GROUP BY，Python 合并。
- 返回每本：`id, name, status, sources, conversations, reports, created_at, updated_at`。

### 2.2 端点

`backend/app/api/routes.py` 新增 `GET /admin/users/{user_id}/notebooks`：`user.role != "admin"` → 403；返回 `[AdminUserNotebook(**row) for row in repository().list_user_notebooks(user_id)]`。

### 2.3 schema

`backend/app/models/schemas.py` 新增：

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

### 2.4 前端 `/admin/usage`

- 表格行可展开：点行（或行首展开钮）→ 懒加载 `GET /api/admin/users/{id}/notebooks`，展开一张子表：**笔记本名 / 状态 / 来源 / 对话 / 报告 / 创建 / 最近更新**。
- 懒加载 + 缓存：同一用户已拉过就不重复请求；未展开不请求（总览页零额外成本）。
- 加载中/空态/失败态各有占位。
- 新增 `frontend/app/admin/usage/notebooks.ts`（`fetchUserNotebooks(userId)` + `AdminUserNotebook` TS 类型）与纯 helper（如格式化状态中文名），helper 测试放 `frontend/app/` 顶层。

---

## Part 3 — 返回主页页头栏

- `/admin/usage` 与 `/dev/logs` 顶部各加一条**窄页头栏**：左侧「← 返回主页」链到 `/`，右侧或紧邻页名（用户使用总览 / 日志查看）。样式对齐主页顶栏那一带（一致的高度/内边距/字色，不硬抄整条工具栏）。
- 抽一个共享的极简页头组件或 CSS 类复用于两页，避免各写一份。
- 返回主页是 `<a href="/">`（整页导航回单页应用根即可）。

---

## 数据流与错误处理

- **写入**：请求/流水线 → `EventLogger.emit` → 当天文件（append）→（跨天时）后台 gzip 昨天。任何异常吞在 `emit` 内。
- **启动**：`create_app` → 提交 `archive_stale_days` 到后台池 → 压缩历史旧天。
- **读取**：前端选 date（默认今天）→ 端点 `resolve_day_path` →（明文尾读 / gz 流式）`load_day_window` → filter/summary/stats → 分页返回。找不到该天文件 → 空列表 + `file_exists:false`，不报错。
- **归档失败**：不致命，下次启动扫一遍补压；读取器兼容明文与 gz 并存。

## 测试策略

- **写入分天**：emit 后断言写到 `<channel>-<today>.jsonl`；mock/monkeypatch date 触发跨天 → 断言旧天被提交压缩（可同步调用 `_gzip_day_file` 验证产物 + 明文消失 + gz 可解回原内容）；断言 `prev is None` 首写不压。
- **归档助手**：明文→gz 原子/幂等；已存 gz 时 no-op；today 文件不被扫入。
- **读取窗口**：构造大文件断言尾读只解析尾部（`seq=偏移` 单调）；`before`/`since` 语义；gz 天流式 + deque 截断 + `truncated`；`legacy` 路径；`date` 非法被拒（路径穿越 `../` 被 400/404）。
- **端点**：`/days` 列表；缺省 date=今天；detail 带 seq 命中；admin-only 403（notebooks 下钻 + 若 requests 跨用户）。
- **仓库**：`list_user_notebooks` 计数正确、排除 copying、只返该用户。
- **前端**：`fetchUserNotebooks`/日期与 owner helper/返回主页链（顶层 `.test.mjs`）；`tsc --noEmit` clean。
- **回归**：现有 `log_reader` 纯函数测试保持绿；现存无日期单文件仍可作为 `legacy` 读出。

## 非目标

- **不提供「全部时间」聚合视图**：那会请回卡死；最大粒度是「某一天」，跨久远历史检索用别的工具。
- **不迁移/不切分历史单文件**：仅作 `legacy` 只读读出。
- **不改 DB schema**（无需加表/列/索引）。
- **不做日志保留期删除/轮转清理**（只压缩不删除；留存策略后续再议）。
- **不改 owner 隔离模型**（仍 `user.id` 子目录；username 仅显示）。

## 兼容与迁移

- 无 DB 迁移。
- 首次部署本特性后：老的 `llm.jsonl` 等单文件原地保留，读取器识别为 `legacy`；新日志进 `<channel>-<today>.jsonl`；启动扫一遍只碰**带日期**的旧天（老单文件无日期，不会被压，安全）。
- `seq` 语义变更仅影响进行中的浏览会话游标（内存态，无持久化），部署后新会话一致即可。
