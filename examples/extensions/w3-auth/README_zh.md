# W3 统一认证 Provider 示例

[English](./README.md)

这是 Silicon Notebook `auth.provider` 扩展点的可独立打包部署插件。它没有前端包，
也不暴露 HTTP 路由。公开 start/callback 路由、OAuth `state`、浏览器证明、PKCE、
目标本站账号、身份映射和本站会话都由主仓负责。插件只处理一次授权码兑换，并返回
`ExternalIdentity(provider_namespace, subject, username, display_name)`。

仓库中的 TOML 默认 `enabled=false`。联调时，把本目录安装到后端解释器，将
`extensions.example.toml` 复制到仓库外，设置 `W3_LOGIN_ORIGIN`、`W3_CLIENT_ID`、
`W3_CLIENT_SECRET`，按需设置 `W3_CA_BUNDLE`，再启用副本并由主仓认证策略选择它。
不要把 Secret 或内网地址直接写进提交的 TOML。登录 origin 必须是 HTTPS；可选 CA
变量指向 `httpx` 信任的 PEM 文件。客户端不跟随重定向，每次请求同时受插件超时和
宿主剩余 deadline 限制。
token 与 userinfo 响应体各自最多读取 256 KiB；即使分块响应没有
`Content-Length` 也受同一上限约束。

适配器按已知示例实现：授权端点为 `/saaslogin1/oauth2/authorize`，固定参数含
`response_type=code`、`scope=base.profile`、`display=page`；令牌端点为 JSON
`POST /saaslogin1/oauth2/accesstoken`（客户端字段为 `client_id` / `client_secret`）；用户信息端点为
`GET /saaslogin1/oauth2/userinfo`。默认采用示例已展示的 query token，也可在平台
明确确认后配置为 Bearer header。

`uid` 必须是非空 JSON 字符串，并始终作为 `username`。显示姓名依次取
`displayNameCn`、`displayName`、`uid`。默认 `subject_field` 也是 `uid`。
**在平台确认 `uid` 在 `provider_namespace` 内唯一、改名或客户端升级时保持稳定、且
永不重分配以前，不得按这个默认值上线。** 如果 W3 提供独立的不可变主体字段，应
明确把那个顶层字段配置成 `subject_field`；改变该字段或命名空间必须走身份迁移，
不能作为普通插件升级静默进行。

`pkce_supported=false` 表示当前未知，不是认定平台不支持；只有完成平台及回调登记
验证后才打开。`userinfo_auth_mode="bearer"` 也必须经平台确认。query 模式只访问
固定的后端 HTTPS 端点，插件不会把该 URL、token、上游响应或异常原文返回主仓。

本包只导入 `app.extension_sdk` 和自身依赖。主仓不导入此示例，因此未安装插件的部署
保持可选 provider 为空。manifest 使用当前 `EXTENSION_API_VERSION`，不兼容宿主会在
发现阶段拒绝启动。
