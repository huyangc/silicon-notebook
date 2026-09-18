# 受管配套服务示例

[English](./README.md)

这个可安装的示例演示配套进程契约。Bundle 不声明 UI 或业务贡献，导入时不启动任何
进程。独立前台模块在本地回环地址提供 `GET /health`，收到 SIGTERM/SIGINT 后关闭
监听并退出。这是生命周期示例，不是生产 HTTP 服务器，也不提供自动发现机制。

在 Silicon Notebook 仓库根目录激活后端 Python 环境，确保 `python3` 指向该解释器，
再安装示例：

```bash
python3 -m pip install -e examples/extensions/managed-service
export EXTENSIONS_CONFIG="$PWD/examples/extensions/managed-service/extensions.example.toml"
bash scripts/cli.sh extensions check
bash scripts/cli.sh extensions services validate
npm run dev
```

正常启动入口等待健康端点就绪后才启动主应用；开发模式退出时也回收本次创建的服务。
也可以只操作配套服务：

```bash
bash scripts/cli.sh extensions services start
bash scripts/cli.sh extensions services status
bash scripts/cli.sh extensions services logs
bash scripts/cli.sh extensions services stop
```

如果不方便激活环境，可在部署使用的 TOML 副本中把 `command[0]` 改成安装了本示例的
解释器绝对路径。`cwd = "."` 按 TOML 所在目录解析，不按调用目录解析。放入已有生产
配置前先确认示例使用的回环端口可用；需要换端口时同时修改命令参数和健康 URL。
启动过程不安装依赖。

示例不记录请求日志。管理器丢弃进程 stdout/stderr，只记录安全的生命周期元信息。
真实插件应自行配置脱敏的诊断日志目的地，并在实际依赖和初始化就绪后才报告健康。
若用 Shell 包装服务，使用 `exec` 并保持前台运行，让终止信号能到达受管进程组。

若实例已经由其他入口管理，将服务表替换为：

```toml
[extensions."examples.managed_service".services.health]
mode = "external"
healthcheck_url = "http://127.0.0.1:9100/health"
```

在主应用之前自行执行
`python3 -m silicon_notebook_managed_service.server --port 9100`。
Core 检查就绪，但停止时保留外部进程。`external` 表不得带启动命令、工作目录或环境
覆盖。两种模式均不会被普通批处理命令隐式启动。

环境引用、依赖顺序、超时边界及完整作者契约见
[扩展 SOP](../../../docs/deployment-extensions-sop_zh.md#配套服务交付契约)。
示例测试使用 fake handler 和 fake server，不绑定宿主端口。
