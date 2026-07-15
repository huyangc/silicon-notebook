# MCP HTTPS 强制改为 opt-in（`MCP_REQUIRE_HTTPS`，默认不强制）

- 日期：2026-07-15
- 状态：设计已确认，待写实现计划
- 分支：`claude/mcp-server-https-deploy-4c2c35`

## 背景与问题

Agent Memory 的 MCP 适配器（Streamable HTTP，`/mcp`）当前对远程部署**强制 HTTPS**，
防止 Agent Bearer token 在明文 HTTP 上被嗅探。现状有**两道 fail-closed 闸** + 一处主机校验：

1. **启动闸** — `validate_mcp_deployment(bind_host, public_url)`
   （`backend/app/main.py:78` 调用，`backend/app/api/mcp_server.py:64` 定义）。
   「远程可达」（`bind_host` 非 loopback，或 `MCP_PUBLIC_URL` 的 host 非 loopback）
   且 scheme ≠ `https` → 启动即 `raise RuntimeError("remote MCP deployment requires HTTPS")`。

2. **请求闸** — `AgentBearerMiddleware.__call__`（`backend/app/api/mcp_server.py:489`）。
   逐请求看 ASGI `scope["scheme"]`：非 https 且客户端非 loopback → `403`。
   `backend/tests/test_memory_mcp.py:987` 钉死：带 `X-Forwarded-Proto: https` 头也 403。

3. **主机校验** — MCP SDK `TransportSecuritySettings(enable_dns_rebinding_protection=True)`
   校验 `Host`/`Origin`，只精确匹配或 `:*` 端口通配（无裸 `*`），`allowed_hosts` 仅含
   loopback 与 `MCP_PUBLIC_URL` netloc。

**痛点**：内网部署（非 loopback 绑定 + 明文 HTTP）现在**无任何配置可通过**，启动即报错。
用户在可信内网部署，不需要这么严格，且希望**默认即放开**、无需额外设置。

## 目标

把 HTTPS 强制从「默认强制」改为 **opt-in**：

- **默认（不设 `MCP_REQUIRE_HTTPS`）**：放开——允许远程明文 HTTP，并跳过 Host/Origin 校验；
  但只要跑在「远程明文」状态，每次启动打印醒目 `WARNING`。
- **`MCP_REQUIRE_HTTPS=1`（显式打开）**：恢复今天的 fail-closed 严格态（两道闸 + rebinding 保护）。

**非目标**：不改 token 认证/授权；不触前端；无 schema 变更。

## 设计

### 开关 `MCP_REQUIRE_HTTPS`

- 反向命名：语义是「是否强制 HTTPS」，**默认关**（不强制）。
- 真值判定：`{"1","true","yes","on"}`（大小写不敏感、两端 strip）为真；其余（含未设）为假。
- 只在 `backend/app/main.py` 的 `create_app()` 里解析一次（就近于现有
  `BACKEND_HOST`/`MCP_PUBLIC_URL` 的 `os.environ.get` 读取），下传给两个函数。

### 安全默认的分层（关键实现约定）

- **可复用安全原语（函数签名）默认仍安全**：
  `validate_mcp_deployment(..., *, require_https=True)`、
  `create_memory_mcp(..., require_https=True)`、
  `AgentBearerMiddleware(..., *, require_https=True)` 的签名默认值都是 `True`。
  → 直接单元调用/既有严格测试的行为不变，安全原语自身保持 fail-closed。
- **「默认放开」是部署策略，集中在组合根**：`main.py` 读
  `require_https = _truthy(os.environ.get("MCP_REQUIRE_HTTPS"))`（env 默认 `False`），
  把这个值显式传下去。产品开箱默认放开只发生在这一处，并配注释说明缘由与恢复方法。

### `require_https=False` 时放宽的三处

1. **启动闸**：`validate_mcp_deployment` 中原本要 `raise` 的分支，`require_https=False`
   时改为 `logger.warning(...)`（明文传输、Agent token 明文过网、仅限可信内网、
   生产/公网请设 `MCP_REQUIRE_HTTPS=1`）后 `return`。

2. **请求闸**：`AgentBearerMiddleware` 增 `require_https: bool` 字段；`__call__` 里
   `require_https=False` 时跳过 `scope["scheme"] != "https"` → 403 分支。token 认证等不变。

3. **主机校验**：`create_memory_mcp` 用
   `TransportSecuritySettings(enable_dns_rebinding_protection=require_https, ...)`
   构造。`require_https=False` → 关闭，middleware 跳过 Host/Origin 校验（保留
   Content-Type 校验）。`allowed_hosts`/`allowed_origins` 照旧组装（关闭后不生效）。
   同时把 `require_https` 透传给 `AgentBearerMiddleware`。

### 数据流

```
环境变量 MCP_REQUIRE_HTTPS  (未设 → False → 默认放开)
        │  main.py: require_https = _truthy(os.environ.get("MCP_REQUIRE_HTTPS"))
        ├─▶ validate_mcp_deployment(bind_host, mcp_public_url, require_https=…)
        │        └─ False 且本会 raise → logger.warning(...) + return
        └─▶ create_memory_mcp(..., require_https=…)
                 ├─ TransportSecuritySettings(enable_dns_rebinding_protection=require_https)
                 └─ AgentBearerMiddleware(app, repo, require_https=…)
                          └─ __call__: False → 跳过 scheme→403 检查
```

### 不变量

- 函数签名默认 `require_https=True`：任何不传该参的直接调用/既有测试 = 今天的严格态。
- 产品默认（不设 env）：`main.py` 传 `require_https=False` → 三处放开 + 远程明文启动有 WARNING。
- `MCP_REQUIRE_HTTPS=1`：产品恢复与今天逐字节一致的 fail-closed。
- loopback-only 部署（bind 127.0.0.1 + loopback public url）：`remotely_reachable=False`，
  无 raise 也无 warning，与 `require_https` 无关，本地开发不受影响。

### 用法

内网（默认即可，无需设变量）：
```bash
BACKEND_HOST=0.0.0.0
MCP_PUBLIC_URL=http://<内网IP或主机名>:8000/mcp   # 可选;Host 校验已默认关,不设也能连
# MCP_REQUIRE_HTTPS 不设 → 默认放开
```
公网/需要严格：
```bash
MCP_REQUIRE_HTTPS=1     # 恢复 fail-closed:强制 HTTPS + Host/Origin 校验
MCP_PUBLIC_URL=https://<域名>/mcp
```

## 改动清单

### 代码
- `backend/app/api/mcp_server.py`
  - 顶部加 `import logging` + `logger = logging.getLogger(__name__)`。
  - `validate_mcp_deployment(..., *, require_https=True)` + `require_https=False` 的 WARNING 分支。
  - `create_memory_mcp(..., require_https: bool = True)`：据此设
    `enable_dns_rebinding_protection` 并透传中间件（实例化点当前在 `mcp_server.py:956`）。
  - `AgentBearerMiddleware.__init__(..., *, require_https: bool = True)`，`__call__` 用之。
- `backend/app/main.py`
  - 加真值解析 helper，读 `MCP_REQUIRE_HTTPS`（默认 False），下传两处调用，附「默认放开」注释。

### 测试（`backend/tests/test_memory_mcp.py`，新用例追加到文件尾，尽量少动既有行号）
- 启动闸：`validate_mcp_deployment("0.0.0.0", "http://10.0.0.5:8000/mcp", require_https=False)`
  不抛错；**既有** `require_https` 缺省（=True）下 `"0.0.0.0","http://..."` 仍抛的用例不改。
- 请求闸：用 `require_https=False` 组装的 app，远端非 loopback 客户端走 http → 200（非 403）；
  既有默认（严格）app 远端 http → 403 的用例不改。
- 主机校验：`require_https=False` 下，带一个不在 `allowed_hosts` 的 `Host` 头的远端请求 → 200。

### 冻结契约（surface manifest）
- `mcp_server.py` 被 `backend/tests/test_repository_surface_manifest.py` 以行号锚点盯着
  （`user_can_read_notebook@mcp_server.py:609` 等，均在改动插入点之后）；插行会下移锚点。
- **解法**：把 `"backend/app/api/mcp_server.py"` 加入该文件的 `LINE_NUMBER_INSENSITIVE_FILES`
  （附注释）。成员+路径覆盖保留、仅对该文件忽略行号。**不重生成冻结 fixture**
  （遵循「fixture 冻结走 allowlist 不 regen」约定）。
- 无新增 facade 调用/成员，故不涉及 `EXPECTED_PATCH_DELTAS`。

### 文档（保持通用口径，机器特定细节不进 git）
- `README.md` / `README_zh.md`：MCP 段说明「默认允许明文、如何用 `MCP_REQUIRE_HTTPS=1` 恢复严格」
  + 安全告警（token 明文、仅可信内网）。
- `architecture.md:80`：把「非 loopback 的 public URL 必须是 HTTPS」改为「默认允许明文；
  设 `MCP_REQUIRE_HTTPS=1` 恢复强制 HTTPS + rebinding 保护」。
- `.env.example`：新增 `MCP_REQUIRE_HTTPS` 条目 + 注释（默认放开、公网请设 1）。
- `packaging/DEPLOY.md`：env 表补一行。
- ⚠ 实现时核 `backend/tests/test_architecture_documentation.py` 是否对上述文档措辞有断言，
  若有需同步。

## 风险与缓解

- **默认放开=公网误部署风险**：这是 owner 明确选择；用远程明文启动的 WARNING + 文档强调
  「仅可信内网、公网设 `MCP_REQUIRE_HTTPS=1`」缓解。函数签名保持安全默认，降级只集中在
  main.py 一处、有注释，便于日后收紧。
- **surface manifest 失败**：用 `LINE_NUMBER_INSENSITIVE_FILES` 白名单（既定机制）。
- **文档断言测试**：实现时先看 `test_architecture_documentation.py`。
- **无 schema 变更**：不涉及 `SCHEMA_VERSION` / `_migration_N`。

## 测试与验证

- `cd backend && python -m pytest tests/test_memory_mcp.py tests/test_repository_surface_manifest.py tests/test_architecture_documentation.py -q`
  全绿。
- 手动：不设 `MCP_REQUIRE_HTTPS`、`BACKEND_HOST=0.0.0.0` 启动，从远端 IP
  `curl -si http://<IP>:8000/mcp` 的 `initialize` 返回 200（非 403）且日志有 WARNING；
  设 `MCP_REQUIRE_HTTPS=1` 后同样远程明文启动应 `RuntimeError`。

## 收尾

- 按 `dev-flow-finish-with-pr`：分支 rebase 到 master 保持线性 → push → `gh pr create --base master`。
