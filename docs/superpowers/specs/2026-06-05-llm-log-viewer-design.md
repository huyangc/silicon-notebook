# LLM 日志可视化 debug 页面 — 设计文档

- 日期: 2026-06-05
- 状态: 已批准设计,待生成实现计划
- 作者: Claude + huzhifeng

## 1. 目标与范围

### 核心问题
"**送了什么给 LLM 来交互**"。本工具让开发者把单条 LLM 交互看清楚:发给模型的 **system prompt / schema_hint / user 内容**,以及模型的 **回复**、token 用量、延迟、错误。

### 形态
- 在现有 **Next.js 前端** 加一个 debug 路由 `/dev/logs`。
- 在 **FastAPI 后端** 加一个**只读** debug 接口 `/api/debug/logs/...`,由后端直接读日志文件(适合 5MB+ 的 `llm.jsonl`)。

### 范围
- **v1 只把 LLM 通道(`llm.jsonl`)做完整**。
- 接口与页面在结构上为 `events` / `requests` 两个通道留好扩展位(同一套接口形状、前端加 tab 即可),但 v1 不实现它们的完整视图。

### 不做(YAGNI)
- 不做 `events` / `requests` 的完整视图(仅占位 tab + 接口可列出通道)。
- 不做实时 SSE 推送(用轮询)。
- 不做写 / 删 / 重放 LLM 调用。
- 不做账号鉴权体系(本地 debug 工具,靠 `debug_logs_enabled` 门控)。
- 不做跨文件聚合大盘 / 图表(先把"看清单条交互"做扎实)。

## 2. 背景:日志数据现状

日志由 `EventLogger`(`backend/app/core/event_logging.py`)写到 `<event_log_dir>/<channel>.jsonl`,默认 `event_log_dir = .local/logs`,相对路径基于仓库根 `_ROOT_DIR`(`event_logging.py` 中 `Path(__file__).resolve().parents[3]`)。每行一个 JSON 对象,**append-only**(只追加)。

当前 `.local/logs/` 通道:
- `llm.jsonl` — LLM 交互(本工具 v1 目标)
- `events.jsonl` — 通用事件
- `requests.jsonl` — HTTP 请求(中间件写入)
- `uvicorn.log` / `next_dev.log` — 纯文本(**非 jsonl,本工具不处理**)

### `llm.jsonl` 记录形状(实测)

记录由 `backend/app/core/llm.py` 与 embedding 服务写入。统计样本:1295 行,`kind` ∈ {chat:792, embed:503},`status` ∈ {ok:1000, retry:208, error:87},`model` ∈ {deepseek-v4-flash, qwen3.7-max, text-embedding-v4}。

**chat 记录**(核心):
```json
{
  "ts": "2026-05-29T16:26:37.790660",
  "id": "llm-b92a26b0",
  "kind": "chat",
  "model": "qwen3.7-max",
  "request": {
    "messages": [
      {"role": "system", "content": "You are the extraction ... Schema hint: {...}"},
      {"role": "user", "content": "You extract structured ... Source elements: ..."}
    ],
    "schema_hint": "{...}"
  },
  "status": "ok",
  "latency_ms": 118726,
  "usage": {"prompt_tokens": 447, "completion_tokens": 3449, "total_tokens": 3896},
  "response": {"content": "{ ...模型返回的 JSON 文本... }"},
  "channel": "llm"
}
```

**embed 记录**(无 messages/response):
```json
{
  "ts": "...", "id": "llm-xxxx", "kind": "embed", "model": "text-embedding-v4",
  "status": "ok", "latency_ms": 42,
  "usage": {"total_tokens": 123},
  "input_chars": 1024, "dims": 1024,
  "channel": "llm"
}
```

**error / retry 记录**:在 chat/embed 基础上,`status` 为 `"error"` 或 `"retry"`,带 `"error": "TypeName: message"`;retry 还带 `"attempt": <int>`,且通常**没有** `usage`/`response`/`dims`。

> 注:正文经 `clip()` 按 `llm_log_max_chars` 截断,超长会带 `...[+N chars]` 后缀 —— 这是文件里就有的内容,本工具原样展示即可,不需要去还原。

**实现要求**:详情渲染必须**防御式** —— 按"字段存在才渲染"处理,不假定任何可选字段一定存在(messages、response、usage、dims、error、attempt 都可能缺)。

## 3. 整体架构

```
浏览器 /dev/logs (Next.js client page)
        │  fetch  ${API_BASE}/debug/logs/...
        ▼
FastAPI  /api/debug/logs/...  (新 router: debug_logs.py, 只读)
        │  read
        ▼
.local/logs/llm.jsonl  (append-only, 由 root master 后端进程写)
```

- 前端走现有约定:`API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000/api"`(见 `frontend/app/page.tsx`)。新接口路径形如 `/debug/logs/llm`。
- 后端在 `create_app()`(`backend/app/main.py`)里 `include_router` 新的 debug router。
- **运行约定**:服务从 **root master** 启动(用户偏好),后端 `_ROOT_DIR` 即 root master,读到的就是 root master 的 `.local/logs`。本 worktree 的 `.local/logs` 为空属正常。
- **不加新依赖**:前端纯 React + fetch,图标复用已有 `lucide-react`;后端只用标准库 + 现有 FastAPI/pydantic。

## 4. 后端接口设计(只读、可门控)

新文件 `backend/app/api/debug_logs.py`,定义 `router = APIRouter(prefix="/debug/logs", tags=["debug"])`(全局已有 `/api` 前缀),在 `main.py` include。

### 4.1 门控与安全
- 新增 setting `debug_logs_enabled: bool = True`(`backend/app/core/config.py` 的 `Settings`)。
- 当 `debug_logs_enabled` 为 false 时,**所有** debug 接口返回 **404**(不返回 403,避免暴露存在性)。用一个依赖 `require_debug_logs_enabled` 统一处理。
- **通道白名单**:`CHANNELS = {"llm": "llm.jsonl", "events": "events.jsonl", "requests": "requests.jsonl"}`。路径参数 `channel` 不在白名单 → **404**。**杜绝路径穿越**(绝不用用户输入直接拼文件名)。
- 文件路径解析复用日志约定:`<event_log_dir>/<filename>`,`event_log_dir` 默认 `.local/logs`,相对路径基于 `_ROOT_DIR`。保证读到的就是 logger 写的文件。
- **纯读**:不写、不删、不改文件。

### 4.2 记录的 `seq`(分页游标)
读取时给每行分配 `seq` = 其 0-based 行号。因日志 append-only,已存在行的 `seq` 稳定,可安全用于翻页与轮询增量。"最新在上" = 按 `seq` 降序。

### 4.3 接口列表

**`GET /api/debug/logs`** — 列出通道(给 tab 用)
```json
{ "channels": [
  {"name": "llm", "file": "llm.jsonl", "exists": true, "count": 1295},
  {"name": "events", "file": "events.jsonl", "exists": true, "count": 0},
  {"name": "requests", "file": "requests.jsonl", "exists": true, "count": 0}
]}
```
`count` 为行数(快速 `sum(1 for _ in file)`,文件不存在则 `exists=false, count=0`)。

**`GET /api/debug/logs/{channel}`** — 列表(精简摘要)

查询参数:
| 参数 | 含义 | 默认 |
|---|---|---|
| `limit` | 返回条数 | 200 |
| `before` | 只取 `seq < before` 的(往旧翻页) | 无 |
| `since` | 只取 `seq > since` 的(自动轮询取增量) | 无 |
| `kind` | 过滤 `kind`(chat/embed) | 无 |
| `status` | 过滤 `status`(ok/error/retry) | 无 |
| `model` | 过滤 `model` | 无 |
| `q` | 文本搜索(大小写不敏感,匹配 messages 内容 + response.content + error) | 无 |

返回:
```json
{
  "channel": "llm",
  "records": [ /* 摘要,按 seq 降序 */ ],
  "stats": {
    "total": 1295,
    "filtered": 1295,
    "by_kind":   {"chat": 792, "embed": 503},
    "by_status": {"ok": 1000, "retry": 208, "error": 87},
    "by_model":  {"deepseek-v4-flash": 703, "text-embedding-v4": 503, "qwen3.7-max": 89},
    "total_tokens": 1234567,
    "latency_ms": {"avg": 812, "max": 118726},
    "malformed_lines": 0,
    "facets": {"kinds": ["chat","embed"], "statuses": ["ok","retry","error"], "models": ["deepseek-v4-flash","qwen3.7-max","text-embedding-v4"]}
  },
  "has_more": true,
  "newest_seq": 1294
}
```

- **count 类统计基于"应用过滤后"的全量**(不只当前页),让统计条与过滤一致:`by_kind`/`by_status`/`by_model`/`total_tokens`/`latency_ms`/`filtered` 均针对过滤后命中集合;`total` 为通道全量行数。
- **例外:`facets` 始终基于通道全量**(不受当前过滤影响),否则一旦按某值过滤,下拉就只剩该值、无法再切换。即:`facets` 提供"可选项",count 类统计反映"当前所看"。
- **摘要 record 形状**(不含大段正文,保证列表轻快):
```json
{
  "seq": 1294, "id": "llm-b92a26b0", "ts": "2026-05-29T16:26:37.790660",
  "kind": "chat", "model": "qwen3.7-max", "status": "ok",
  "latency_ms": 118726, "total_tokens": 3896, "attempt": null,
  "error": null,
  "preview": "You extract structured engineering knowhow from ..."  
}
```
  - `preview`:chat 取最后一条 user message 的前 ~160 字符;embed 取 `input_chars=… dims=…` 之类简述;error/retry 优先取 `error`。
  - `total_tokens` 取 `usage.total_tokens`(缺则 null)。

**`GET /api/debug/logs/{channel}/{record_id}`** — 单条完整记录
- 按 `id` 在文件中查找,返回**该行的完整解析对象**(含 `request.messages` 全文、`response.content`、`schema_hint` 等)。
- 命中多个同 id(极少)取最新;找不到 → 404。
- 为简单起见 v1 直接扫文件找 id(可接受);如需可加 `seq` 直达参数,但非必须。

### 4.4 读取与健壮性
- 读取策略:逐行读 → `json.loads` → 带 `seq` 收集。1295 行 / 5MB 直接全读没问题。
- **坏行**(best-effort 日志可能写半行 / 非法 JSON):`try/except` 跳过,`malformed_lines += 1`,**绝不 500**。
- 过滤 / 搜索在服务端做,覆盖全量。
- 排序:按 `seq` 降序;应用 `before`/`since`/过滤后再 `limit`。
- 文件不存在:返回 `records: []`、`stats` 全 0、并在响应里带一个明确信号(如 `"file_exists": false`),前端显示空状态与路径提示。

## 5. 前端页面设计

路由:`frontend/app/dev/logs/page.tsx`(`"use client"`)。不挂主导航,是 debug 工具,直接访问路径即可。

### 5.1 文件拆分(小而专一)
```
frontend/app/dev/logs/
  page.tsx                  # 顶层:状态管理、数据获取、轮询、布局拼装
  types.ts                  # 与后端对齐的 TS 类型(摘要 / 完整记录 / stats)
  format.ts                 # 纯函数:状态配色、token/延迟格式化、response 美化、preview 兜底
  format.test.mjs           # format.ts 的轻量单测(node 跑,仿 answer-formatting.test.mjs)
  api.ts                    # 封装 fetch(list / detail / channels),走 API_BASE
  components/
    ChannelTabs.tsx         # 通道 tab(LLM 实做,events/requests 占位禁用)
    StatsBar.tsx            # 顶部统计 chips
    Filters.tsx             # kind/status/model 下拉 + 搜索框 + 刷新/自动刷新开关
    LogList.tsx             # 左侧列表(master) + 加载更多 + "N 条新"
    LogRow.tsx              # 单行(时间/徽章/model/status/延迟/tokens)
    LogDetail.tsx           # 右侧详情(detail) 容器,按 kind 分派
    ChatTranscript.tsx      # chat:system/user/assistant 块 + schema_hint 高亮 + response
    CopyButton.tsx          # 一键复制(复用)
```

### 5.2 布局(master-detail)
- **顶部栏**:`ChannelTabs`(LLM 选中;Events/Requests 占位、置灰)+ `StatsBar`(总数、ok/retry/error 占比、各 model 计数、总 token、avg/max 延迟)。
- **过滤栏** `Filters`:kind / status / model 下拉(选项来自 stats.facets)+ 文本搜索框(防抖 ~300ms)+ "刷新"按钮 + "自动刷新"开关(间隔默认 5s,可关)。
- **左侧** `LogList`(master):`LogRow` 列表,最新在上;底部"加载更多";顶部出现新记录时显示"N 条新,点击查看"。
- **右侧** `LogDetail`(detail):选中某行后渲染。

### 5.3 详情渲染(重点)
- **chat** → `ChatTranscript`:
  - 顶部 meta:model、status 徽章、延迟、token(prompt / completion / total)、id、ts;按钮"复制完整 prompt""复制整条 JSON"。
  - **对话块**:`request.messages` 按顺序渲染,每条带角色标签(system / user / assistant)与等宽、可滚、可复制的正文;**system 与 `schema_hint` 用单独高亮样式**(这是"送了什么给 LLM"的关键)。
  - **response**:`response.content` 若能 `JSON.parse` 则美化展示(缩进),并提供 raw/美化切换;不能解析就原样等宽显示。
  - 超长正文可折叠/展开。
- **embed** → 简版:展示 `input_chars`、`dims`、`usage`、延迟。
- **error / retry**:`error` 字符串与 `attempt` 醒目展示(红色基调)。
- **防御式**:任何可选字段缺失都安全降级,不崩。

### 5.4 配色/样式
沿用现有 `frontend/app/globals.css` 风格,无额外偏好。status 配色约定:ok=绿、retry=黄、error=红;kind 徽章 chat / embed 用不同中性色。

## 6. 数据流与刷新

1. 进入页面 → `GET /debug/logs`(取通道)→ 默认选中 `llm` → `GET /debug/logs/llm?limit=200` → 渲染列表 + 统计。
2. 点某行 → `GET /debug/logs/llm/{id}` 取完整记录 → 右侧详情。
3. 改过滤 / 搜索 → 带参数重查(服务端过滤,覆盖全量),重置列表。
4. **自动刷新**(开关开时):每 N 秒 `GET /debug/logs/llm?since=<newest_seq>&<当前过滤>` → 命中的新记录按 `seq` 去重后插到顶部 → 顶部显示"N 条新";同时刷新 stats。
5. **加载更多**:`GET /debug/logs/llm?before=<当前最旧 seq>&<当前过滤>&limit=200` → 追加到列表尾部。

## 7. 错误处理与边界

| 情况 | 后端 | 前端 |
|---|---|---|
| `debug_logs_enabled=false` | 所有 debug 接口 404 | 显示"debug 接口已关闭"提示 |
| 非法 channel | 404 | tab 不会产生非法 channel;若手动访问给错误 banner |
| 文件不存在 | 空 records + `file_exists:false` | 空状态 + 显示期望路径 |
| 坏行 | 跳过 + `malformed_lines` 计数 | stats 里展示"N 行损坏" |
| record id 找不到 | 404 | 详情区提示"记录不存在(可能已轮转)" |
| fetch 失败 / 后端未起 | — | 顶部 banner + 重试按钮 |
| 加载中 / 空结果 | — | loading 骨架 / 空状态文案 |

## 8. 测试策略

### 8.1 后端(pytest,仿 `backend/tests/`)
新文件 `backend/tests/test_debug_logs.py`,用临时 `.jsonl` fixture(写入若干 chat/embed/error/retry + 一行坏 JSON),用 FastAPI `TestClient`,覆盖:
- 通道白名单:非法 channel → 404;路径穿越尝试(如 `..%2f`)→ 404。
- 门控:`debug_logs_enabled=false` → 404;为 true → 200。
- 列表过滤:`kind` / `status` / `model` 各自命中正确子集。
- 搜索:`q` 在 messages / response / error 文本中大小写不敏感命中。
- 分页:`before` 取更旧、`since` 取更新、`limit` 生效;`has_more`/`newest_seq` 正确。
- stats:`by_kind`/`by_status`/`by_model`/`total_tokens`/`latency_ms`/`facets` 计算正确,且基于过滤后集合。
- 坏行:被跳过且 `malformed_lines` 计数。
- 详情:按 id 取到完整记录;不存在 → 404。
- 文件缺失:空 records + `file_exists:false`,不报错。

### 8.2 前端
- `frontend/app/dev/logs/format.test.mjs`(node 跑,仿 `frontend/app/answer-formatting.test.mjs`):测纯函数 —— preview 兜底、response JSON 美化(含不可解析时降级)、token/延迟格式化、status 配色映射、防御式字段缺失。
- 页面:用 preview 工作流跑 dev server(前后端起在 root master),snapshot 验证列表/详情/过滤/自动刷新,完成后截图作为证据。

## 9. 文件清单

**新增**
- `backend/app/api/debug_logs.py` — debug 日志 router 与读取逻辑
- `backend/tests/test_debug_logs.py` — 后端测试
- `frontend/app/dev/logs/page.tsx` — 页面
- `frontend/app/dev/logs/types.ts` — TS 类型
- `frontend/app/dev/logs/api.ts` — fetch 封装
- `frontend/app/dev/logs/format.ts` — 纯函数
- `frontend/app/dev/logs/format.test.mjs` — 前端单测
- `frontend/app/dev/logs/components/*.tsx` — ChannelTabs / StatsBar / Filters / LogList / LogRow / LogDetail / ChatTranscript / CopyButton

**改动**
- `backend/app/core/config.py` — 新增 `debug_logs_enabled: bool = True`
- `backend/app/main.py` — include 新 debug router

## 10. 验收标准(v1)
- 起服务后访问 `/dev/logs`,默认 LLM 通道,列表展示最新交互(最新在上),统计条正确。
- 点开一条 chat,能完整看到发给 LLM 的 system / schema_hint / user 内容与模型回复,可复制。
- kind/status/model 过滤与文本搜索生效且与统计一致。
- "加载更多"可往旧翻;"自动刷新"开关可拉取新记录并提示"N 条新"。
- 文件缺失 / 坏行 / 后端未起 / 门控关闭 等边界均有明确、不崩的表现。
- 后端与前端测试通过。
