# 按用户隔离日志(events + llm)设计

- 日期:2026-06-27
- 状态:已批准,待写实现计划
- 范围:后端可观测层(`backend/app/core/event_logging.py`、`llm_logging.py`、`api/debug_logs.py`、`services/log_reader.py`)

## 背景与问题

项目已经有完整的用户系统(注册/登录、owner 数据隔离、admin 角色、请求级 `_REQUEST_USER` ContextVar),但**日志层完全没有消费用户身份**:

- 事件日志写在全局单一文件 `.local/logs/{channel}.jsonl`(channel = events / llm / requests),不带任何用户字段,也不按用户隔离路径。
- 读取 API `api/debug_logs.py` 只有 `debug_logs_enabled` 总开关,**任何登录用户都能看到全局日志**——这同时是一个越权/隐私问题。
- 结果:出问题(如 model_error 横幅)时,从日志看不出是哪个用户触发的;且 A 用户能看到 B 用户的活动。

数据访问层已经做了 owner 隔离,但审计/可观测维度断了。本设计补上这一维度。

## 目标 / 非目标

**目标**
- `events` 与 `llm` 两类日志按用户隔离到各自目录,互不混淆。
- 读取 API 加用户门控:普通用户只能看自己的日志;admin 可跨用户查看。
- 顺手修掉"任何登录用户都能看全局日志"的越权问题。

**非目标(本期不做)**
- 不隔离 `requests`(HTTP 访问日志)。它在中间件层写,而中间件跑在认证依赖之前拿不到当前用户;改造它需要把日志后置或注入 `request.state`,成本与收益不匹配,本期保持全局单一文件。
- 不迁移历史日志文件。
- 不给日志行注入用户字段到 Python `logging` 的 formatter(本期只做文件路径隔离;字段注入可作为后续增强)。

## 决策记录

1. **隔离范围 = events + llm**(不含 requests)。理由见非目标。
2. **admin 可跨用户查看**;读取 API 加用户门控,普通用户只看自己。
3. **无用户场景回退到 `user-local` 而非 `_system`**。理由:与现有数据层 owner 隔离语义一致(离线脚本 / 本地单用户场景,实际就是 seeded admin `user-local` 在操作)。`_system` 只接 owner 解析彻底失败(异常)的兜底。
4. **`requests` 全局日志仅 admin 可读**。普通用户能看全局请求 = 看到别人的路径 / IP / 活动,故挡掉。

## 磁盘布局(写入)

```
.local/logs/
  requests.jsonl              # 不变:全局 HTTP 访问日志
  {user_id}/events.jsonl      # 新:按 owner 分目录
  {user_id}/llm.jsonl         # 新:按 owner 分目录
  _system/events.jsonl        # 兜底:owner 解析彻底失败时
  _system/llm.jsonl
  events.jsonl                # 旧历史文件,留在原地不迁移
  llm.jsonl                   # 旧历史文件,留在原地不迁移
```

`user_id` 形如 `a00123456`(注册用户,正则 `^[a-z]00\d{6}$`)或 `user-local`(seeded admin / 本地离线场景)。

## 写入侧设计

### EventLogger 增加 per-user 模式

`backend/app/core/event_logging.py` 的 `EventLogger`:

- 构造增加两个可选参数:`owner_resolver: Callable[[], str | None] | None = None`、`per_user: bool = False`。
- `emit()`(当前 `event_logging.py:52`,唯一写入口)在 `per_user=True` 时动态计算路径:
  `base_dir / (owner or "_system") / filename`,并按需 `mkdir(parents=True, exist_ok=True)`。
  `per_user=False` 时维持现状(固定 `self.path`)。
- `owner` 来自 `owner_resolver()`;解析为 `None` 或抛异常 → `"_system"`。
- **best-effort 铁律**:owner 解析失败、建目录失败都不能破坏被观测的请求/管道。emit 整体仍包在 try 中;owner 解析单独 try/except 兜底到 `_system`。

### owner_resolver 注入

- `event_log` 在 `backend/app/services/sqlite_repository.py:250` 创建,注入 `owner_resolver = lambda: self.current_user().id`。
- `current_user()`(`sqlite_repository.py:961`)已是"ContextVar 快路径优先,无则查 DB 回退 `user-local`",所以:
  - 请求上下文内(含 `copy_context` 传播的后台 KG job):零额外 DB 查询,直接拿到真实用户 id。
  - 离线 / 本地无 ContextVar:回退 `user-local`。
  - 真正异常(如极早期、无 DB):resolver try/except → `_system`。
- **分层**:`EventLogger` 在 core 层只持有一个 `Callable`,不 import service 层的 `_REQUEST_USER`,分层不破。

### LLM 日志

`backend/app/core/llm_logging.py` 的 `LLMInteractionLogger` 当前用 `settings.llm_log_path` 覆盖了内部 EventLogger 的 path(`llm_logging.py:29-35`)。改造为同样走 per-user 模式:

- base_dir 取 `llm_log_path` 的父目录,filename 取其文件名。
- per-user 模式 + 同一个 `owner_resolver`,复用 EventLogger 的路径计算逻辑(不重复实现)。
- 保留 `enabled` / `path` 等向后兼容属性(smoke 测试、`llm.py` 仍在用)。

### requests 不动

`backend/app/main.py:22` 的 `request_log` 保持 `per_user=False`,继续写全局 `requests.jsonl`。

## 读取侧设计(debug_logs API)

`backend/app/api/debug_logs.py` 三个路由(`list_channels`、`list_records`、`get_record`):

- 都加 `get_current_user` 依赖(当前只有 `require_enabled`)。
- `_channel_path(settings, channel, owner)` 增加 `owner` 参数:
  - `events` / `llm`(per-user):`log_dir / owner / filename`。
  - `requests`(全局):`log_dir / filename`,不带 owner。
- **owner 解析规则**:
  - 普通用户:`owner` 强制 = 自己的 id;若显式传 `?owner=` 指向别人 → **403**。
  - admin:`?owner=<user_id>` 可看任意用户;不传默认看自己;可传 `_system` 看兜底桶。
  - `requests` channel:**仅 admin 可读**,普通用户请求被挡(403)。
- **防路径穿越**:`owner` 必须通过白名单校验,正则 `^[a-z]00\d{6}$` 或字面量 `user-local` / `_system`;不匹配则拒绝(防 `../` 穿越)。

## 需同步改的读取端

路径从 `log_dir/{channel}.jsonl` 变为子目录后,以下硬编码读取点会静默读到空,必须一起改:

- **新增聚合 helper**:`backend/app/services/log_reader.py` 加一个函数,遍历 `log_dir/{channel}.jsonl`(旧历史)+ `log_dir/*/{channel}.jsonl`(所有用户子目录),合并返回。既兼容历史文件,又支持"看全部"。
- **eval 脚本**改用聚合 helper(eval 在开发者本地跑,要全量):
  - `backend/app/eval/ask_latency.py:31`、`ask_latency.py:156`(events.jsonl)
  - `backend/app/eval/speed.py:83`(llm.jsonl)
- **smoke 脚本** `scripts/smoke_backend.py` 的日志读取点(约 802 / 841 / 879 行)跟着改。
- **测试**:`backend/tests/test_debug_logs.py`、`backend/tests/eval/test_ask_latency.py` 更新预期路径,并新增隔离/门控用例。

## 关键不变量 / 风险

| 项 | 处理 |
|---|---|
| 分层(core 不依赖 service) | EventLogger 只收 `Callable` resolver,注入在 service 侧 |
| 日志永不破坏主流程 | owner 解析 / 建目录失败 → `_system`,全程 try 兜底 |
| 路径穿越 | 读取 API owner 参数白名单校验 |
| requests 隐私 | 全局文件仅 admin 可读 |
| 后台 KG job | 已用 `copy_context` 传播 ContextVar,日志自动归发起用户,**不用改** |
| 历史日志 | 不迁移,聚合 helper 兼容读旧文件 |
| 性能 | owner resolver 走 `current_user()` ContextVar 快路径,请求内无额外 DB 查询 |

## 测试范围(TDD)

- EventLogger per-user 路径:有 owner → 写 `{owner}/`;owner 为 None / 异常 → `_system/`。
- LLMInteractionLogger per-user 路径,兼容属性仍可用。
- owner_resolver:ContextVar 命中(真实用户)/ 未命中(回退 user-local)。
- debug_logs 门控:
  - 普通用户只看自己;传别人 owner → 403。
  - admin 跨用户 + 看 `_system`。
  - `requests` channel:普通用户 → 403,admin 可读。
  - owner 穿越(`../`、非法字符)→ 拒绝。
- log_reader 聚合 helper:根目录旧文件 + 多个用户子目录合并去重正确。
- 更新现有 `test_debug_logs.py`、`test_ask_latency.py` 全绿。
