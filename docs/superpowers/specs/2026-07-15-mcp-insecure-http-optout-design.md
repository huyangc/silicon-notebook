# MCP 内网明文 HTTP 部署开关 (`MCP_ALLOW_INSECURE_HTTP`)

- 日期：2026-07-15
- 状态：设计已确认，待写实现计划
- 分支：`claude/mcp-server-https-deploy-4c2c35`

## 背景与问题

Agent Memory 的 MCP 适配器（Streamable HTTP，`/mcp`）对远程部署强制 HTTPS，
目的是防止 Agent Bearer token 在明文 HTTP 上被网络嗅探。当前有**两道 fail-closed 闸**：

1. **启动闸** — `validate_mcp_deployment(bind_host, public_url)`
   （`backend/app/main.py:78` 调用，`backend/app/api/mcp_server.py:64` 定义）。
   当「远程可达」（`bind_host` 非 loopback，或 `MCP_PUBLIC_URL` 的 host 非 loopback）
   且 URL scheme ≠ `https` 时，启动即
   `raise RuntimeError("remote MCP deployment requires HTTPS")`。

2. **请求闸** — `AgentBearerMiddleware.__call__`
   （`backend/app/api/mcp_server.py:489`）。逐请求检查 ASGI `scope["scheme"]`：
   非 https 且客户端非 loopback → `403 "remote MCP transport requires HTTPS"`。
   已被 `backend/tests/test_memory_mcp.py:987` 钉死：即使带 `X-Forwarded-Proto: https`
   头也 403（中间件只认 `scope["scheme"]`，不自解析转发头）。

此外还有第三处严格性：MCP SDK 的 `TransportSecuritySettings`
（`enable_dns_rebinding_protection=True`）会校验 `Host`/`Origin` 头，只有精确匹配
或 `:*` 端口通配才放行（SDK 的 `_validate_host` 不支持裸 `*`）。`allowed_hosts`
只包含 loopback 和 `MCP_PUBLIC_URL` 的 netloc。

**痛点**：内网部署（非 loopback 绑定 + 明文 HTTP）目前**没有任何配置开关能通过**，
启动即报错。用户在可信内网部署，不需要这么严格。

## 目标

给出一个**默认关闭的显式 opt-out 开关**，内网部署时用户主动打开即可放宽严格性；
不设时行为与今天逐字节相同（fail-closed，强制 HTTPS，rebinding 保护开）。

**非目标**：不改动 token 认证/授权逻辑；不引入自动降级（必须显式打开）；
不触碰前端；不做 schema 变更。

## 设计

### 开关

- 新增环境变量 `MCP_ALLOW_INSECURE_HTTP`。
- 真值判定：`{"1","true","yes","on"}`（大小写不敏感、两端 strip）；其余（含未设）为假。
- 在 `backend/app/main.py` 的 `create_app()` 里解析一次（就近于现有
  `BACKEND_HOST` / `MCP_PUBLIC_URL` 的 `os.environ.get` 读取，风格一致），
  分别下传给两个函数。

### 打开后放宽的三处（对应用户选定的「HTTPS + 主机校验都放宽」）

1. **启动闸**：`validate_mcp_deployment(bind_host, public_url, *, allow_insecure=False)`。
   原本要 `raise` 的分支，当 `allow_insecure=True` 时改为打印一条醒目 `WARNING`
   （明文传输、Agent token 明文过网、仅限可信内网、生产/公网务必关闭）后 `return`。

2. **请求闸**：`AgentBearerMiddleware` 增加 `allow_insecure: bool` 字段
   （`__init__` 关键字参数，默认 `False`）。`__call__` 里当 `allow_insecure=True` 时
   跳过 `scope["scheme"] != "https"` → 403 的分支；token 认证/其余逻辑不变。

3. **主机校验**：`create_memory_mcp(..., allow_insecure_http: bool = False)`。
   当 `allow_insecure_http=True` 时，`TransportSecuritySettings` 以
   `enable_dns_rebinding_protection=False` 构造（middleware 直接跳过 Host/Origin 校验，
   保留 Content-Type 校验）。`allowed_hosts`/`allowed_origins` 内容照旧组装即可
   （被禁用后不生效，无需清空）。同时把 `allow_insecure_http` 透传给
   `AgentBearerMiddleware(..., allow_insecure=allow_insecure_http)`。

### 数据流

```
环境变量 MCP_ALLOW_INSECURE_HTTP
        │  main.py: _truthy(os.environ.get(...)) → allow_insecure: bool
        ├─▶ validate_mcp_deployment(bind_host, mcp_public_url, allow_insecure=…)
        │        └─ True 且本会 raise → logger.warning(...) + return
        └─▶ create_memory_mcp(..., allow_insecure_http=…)
                 ├─ TransportSecuritySettings(enable_dns_rebinding_protection=not …)
                 └─ AgentBearerMiddleware(app, repo, allow_insecure=…)
                          └─ __call__: True → 跳过 scheme→403 检查
```

### 默认安全不变量

- `MCP_ALLOW_INSECURE_HTTP` 未设 / 假值时：三处行为与现状完全一致
  （`validate_mcp_deployment` 默认参 `allow_insecure=False`；
  `create_memory_mcp` 默认 `allow_insecure_http=False` →
  `enable_dns_rebinding_protection=True`；中间件默认 `allow_insecure=False`）。
- 打开时每次启动都有 `WARNING`，避免误上公网而不自知。

### 内网用法（打开后）

```bash
BACKEND_HOST=0.0.0.0
MCP_PUBLIC_URL=http://<内网IP或主机名>:8000/mcp
MCP_ALLOW_INSECURE_HTTP=1
```

## 改动清单

### 代码
- `backend/app/api/mcp_server.py`
  - 顶部加 `import logging` + `logger = logging.getLogger(__name__)`。
  - `validate_mcp_deployment(...)` 加 `*, allow_insecure=False` 与 WARNING 分支。
  - `create_memory_mcp(...)` 加 `allow_insecure_http: bool = False`；据此设
    `enable_dns_rebinding_protection` 并透传给中间件（实例化点当前在
    `mcp_server.py:956`）。
  - `AgentBearerMiddleware.__init__` 加 `*, allow_insecure: bool = False`，`__call__`
    用之。
- `backend/app/main.py`
  - 加真值解析 helper，读 `MCP_ALLOW_INSECURE_HTTP`，下传两处调用。

### 测试（`backend/tests/test_memory_mcp.py`，新用例追加到文件尾，尽量少动既有行号）
- 启动闸：`validate_mcp_deployment("0.0.0.0", "http://10.0.0.5:8000/mcp", allow_insecure=True)`
  不抛错；`allow_insecure=False` 仍抛（既有用例不改）。
- 请求闸：用 `allow_insecure_http=True` 组装的 app，远端非 loopback 客户端走 http →
  200（非 403）。
- 主机校验：开关下，带一个不在 `allowed_hosts` 的 `Host` 头的远端请求 → 200
  （验证 rebinding 保护确被关）。

### 冻结契约（surface manifest）
- `mcp_server.py` 被 `backend/tests/test_repository_surface_manifest.py` 以**行号锚点**
  盯着（如 `user_can_read_notebook@mcp_server.py:609` 等，均在改动插入点之后）。
  插行会下移这些锚点。
- **解法**：把 `"backend/app/api/mcp_server.py"` 加入该文件的
  `LINE_NUMBER_INSENSITIVE_FILES`（附说明注释）。成员+路径覆盖保留、仅对该文件忽略
  行号。**不重生成冻结 fixture**（遵循「fixture 冻结走 allowlist 不 regen」约定）。
- 无新增 facade 调用/成员，故不涉及 `EXPECTED_PATCH_DELTAS` / 新 consumer 白名单。

### 文档（保持通用口径，机器特定细节不进 git）
- `README.md` / `README_zh.md`：MCP 段补 `MCP_ALLOW_INSECURE_HTTP` 用法 + 安全告警。
- `architecture.md:80`：现有「非 loopback 的 public URL 必须是 HTTPS」一句补 opt-out 说明。
- `.env.example`：新增变量条目 + 注释（默认关、仅可信内网）。
- `packaging/DEPLOY.md`：env 表补一行。
- ⚠ 实现时核 `backend/tests/test_architecture_documentation.py` 是否对上述文档措辞有
  断言，若有需同步。

## 风险与缓解

- **误上公网**：默认关 + 每次启动 WARNING + 文档强调「仅可信内网」。
- **surface manifest 失败**：用 `LINE_NUMBER_INSENSITIVE_FILES` 白名单（既定机制）。
- **文档断言测试**：实现时先看 `test_architecture_documentation.py`。
- **无 schema 变更**：不涉及 `SCHEMA_VERSION` / `_migration_N`。

## 测试与验证

- `cd backend && python -m pytest tests/test_memory_mcp.py tests/test_repository_surface_manifest.py tests/test_architecture_documentation.py -q`
  全绿。
- 手动：设三件套环境变量启动后端，从远端 IP `curl -si http://<IP>:8000/mcp` 的
  `initialize` 请求返回 200（非 403）；不设开关时启动应仍 `RuntimeError`。

## 收尾

- 按 `dev-flow-finish-with-pr`：分支 rebase 到 master 保持线性 → push → `gh pr create --base master`。
