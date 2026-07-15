# MinerU 批量解析客户端重写 + .env 配置建议

日期：2026-07-15
分支：`claude/batch-pdf-mineru-parsing`

## 背景与结论

内网部署了一套 MinerU server（FastAPI），同时暴露：

- `POST /file_parse`（**同步**）——提交到共享异步任务管理器、等完成、同响应返回结果
- `POST /tasks` + `GET /tasks/{id}` + `GET /tasks/{id}/result`（**异步**任务队列）
- `GET /health`（含 `max_concurrent_requests`）

关键事实：

1. **backend 现有 client 已兼容**。[`mineru_client.py`](../../../backend/app/services/mineru_client.py) 的 http 模式打的正是
   `/file_parse`，字段（`backend`/`parse_method`/`return_content_list`/`formula_enable`/
   `table_enable`/`lang_list`/`server_url`）与内网 server 的 OpenAPI 一一对得上，返回走
   `_extract_content_list` 能解的 `results→content_list`。→ 让 app 内联解析走内网 server
   **不需要改代码，只要配 `.env`**（`MINERU_MODE=http` + `MINERU_API_URL`）。

2. **异步 transport（曾拟的 Part B）无必要**。上传后解析在后台 job 里跑
   （[source_ingestion.py:324](../../../backend/app/services/source_ingestion.py) `upload_sources` →
   `scheduler(source_id)` = `kg_scheduler.submit_job(process_source, ...)`，"heavy parse/embed/
   extract pipeline runs out of band"）。同步 `/file_parse` 阻塞不影响用户 UI，异步对体验零增益 →
   YAGNI，砍掉。唯一残留风险是大文档同步连接超时 → 由 `MINERU_TIMEOUT_SECONDS` 配置解决，非代码。

3. 用户贴的批量脚本（deploy 侧工具）写得粗糙，值得干净重写。

**最终范围 = A（批量脚本重写）+ C（.env 建议）。backend 不动。**

## Part A — `scripts/mineru_batch_parse.py`

目录 → `.md` 的批量解析工具，走内网 server 的异步 `/tasks` API 跨多台负载均衡。

### 现有脚本的粗糙点（要修的）

| 问题 | 现状 | 修法 |
|---|---|---|
| HTTP 靠 `curl` 子进程 | 丢掉 HTTP 状态码/server 错误体，`False,None` 一把梭 | `requests.Session`（连接复用、`raise_for_status`、错误体可见） |
| 配置硬编码 | `SERVERS`/`SRC`/`OUT`/`CONCURRENCY` 写死在源码 | 全部 `.env`（`MINERU_BATCH_*`）+ argparse 覆盖 |
| 并发不看 server 容量 | 固定 `CONCURRENCY=6`，无视各 server `max_concurrent_requests` | 每台按 `/health` 自动限在途数（可 env 覆盖） |
| 结果提取脆弱 | 只试 `md_content`/`md` | dict/list + JSON 字符串兜底，统一提取器 |
| 只有 `failed.txt` | skip 不入账、崩溃即丢进度 | **JSONL run-manifest**：每文件 status/task_id/server/耗时/大小/attempts/error |
| Ctrl-C 丢账 | 无信号处理 | SIGINT：停止派发、flush manifest、已完成不重跑 |

### 组件

- `load_dotenv(path)`：~10 行零依赖 `.env` 解析（`KEY=VALUE`、忽略注释/空行），os.environ 优先。
- `Config`（dataclass）：默认值 ← `.env` ← argparse，逐层覆盖。**默认值保持通用**（占位 `http://mineru-host:8000`、相对/通用路径），真实内网 IP/FTP 路径只进用户本地 `.env`（守 committed-docs-stay-generic）。
- `MinerUServer`：一个 base URL + `requests.Session` + 容量信号量。`health()` / `submit(pdf)->task_id` / `poll(task_id)->status` / `fetch_result(task_id)->md`。
- `extract_md(result_payload)->str`：健壮提取器。
- `process_file(server, pdf, cfg)->record`：out `.md` 已存在且非平凡则 skip；否则 submit→poll→fetch→写盘；重试退避；返回 manifest 记录。
- `Dispatcher`：构建文件列表（`--list` 或 `rglob("*.pdf")`）→ 轮询分配 server → 单 ThreadPool + 每台信号量限流 → 逐条 append manifest + 进度/ETA。

### 请求契约（保持与已跑通脚本一致，不改 server 侧）

- 提交：`POST {server}/tasks`，multipart `files=@pdf` + `backend`/`lang_list`/`formula_enable`/`table_enable`/`return_md=true`
- 轮询：`GET {server}/tasks/{id}` → `status ∈ {queued,running,completed,failed}`
- 取结果：`GET {server}/tasks/{id}/result` → `results[key].md_content`

### CLI

`--src` `--out` `--list` `--dry-run` `--only-failed`（从上轮 manifest 只重跑 fail）`--limit N`（配额调试）。用法进 README.md + README_zh.md（守 document-cli-in-readme）。

### 测试

`backend/tests/` 或 `scripts` 侧一个冒烟测试：以可覆写的 HTTP 接缝 mock `/tasks` 三段式，断言 submit→poll→fetch→写 md + manifest 记录，无需真连内网 server。

## Part C — `.env` 配置建议

### app 内联解析走内网 server（复用现有 `MINERU_*` key，无需改码）

```
MINERU_MODE=http
MINERU_API_URL=http://mineru-host:8000
MINERU_BACKEND=pipeline
MINERU_LANG=              # 空 → server 默认 ch
MINERU_FORMULA_ENABLE=true
MINERU_TABLE_ENABLE=true
MINERU_TIMEOUT_SECONDS=1800   # ⚠ 大书同步 /file_parse 要挂久，调大避免 10 分钟超时回落 pypdf
```

### 批量脚本（新增 `MINERU_BATCH_*`，与 app 侧解耦）

```
MINERU_BATCH_SERVERS=http://mineru-host:8000,http://mineru-host:8001
MINERU_BATCH_SRC_DIR=/path/to/pdf/books
MINERU_BATCH_OUT_DIR=/path/to/output
MINERU_BATCH_BACKEND=pipeline
MINERU_BATCH_LANG=ch
MINERU_BATCH_FORMULA_ENABLE=true
MINERU_BATCH_TABLE_ENABLE=true
MINERU_BATCH_CONCURRENCY_PER_SERVER=0   # 0=按 /health 的 max_concurrent_requests 自动
MINERU_BATCH_POLL_INTERVAL=10
MINERU_BATCH_MAX_POLL_SECONDS=1800
MINERU_BATCH_RETRY_MAX=3
MINERU_BATCH_SUBMIT_TIMEOUT=120
MINERU_BATCH_RESULT_TIMEOUT=120
MINERU_BATCH_MANIFEST=                   # 空 → OUT_DIR/_manifest.jsonl
```

`.env.example` 只写通用占位（不含真实内网 IP/FTP 路径）。

## 交付物

1. `scripts/mineru_batch_parse.py`
2. README.md + README_zh.md 增用法段
3. `.env.example` 补 `MINERU_BATCH_*` 段 + `MINERU_TIMEOUT_SECONDS` 大书注释
4. 冒烟测试
5. 收尾提 PR（rebase 到 master 保持线性）
