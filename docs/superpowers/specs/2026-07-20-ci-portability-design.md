# CI 可移植性专题设计

## 背景

GitHub Actions `CI / full-gate` 首次在干净的 `ubuntu-24.04` runner
执行 PR #306 的 exact head 时失败，但失败并不是产品代码断言回归：

- `fangan/testcases/harness/tests/` 下 5 个测试把仓库写死为
  `/Users/hzf/workspace/silicon_notebook`，导致 Ubuntu runner 找不到已随仓库
  提交的 14 份 `gold.yaml`。
- `backend/tests/conftest.py` 为共享 Matplotlib 字体缓存直接导入
  `matplotlib.font_manager`，而 `backend/requirements.txt` 没有声明
  `matplotlib`。开发机的 Homebrew Miniconda 环境碰巧已有该包，干净 CI
  环境则在测试收集阶段立即失败。
- 前端测试、类型检查和生产构建全部通过；冷 runner 的 frontend lane
  记录为 61 秒。既有文档已明确：60 秒是 Apple Silicon 本地 warm gate
  的实测目标，不是所有 CI 主机的可移植超时断言。

最新 `origin/master` 基线为 `e824489e`。隔离 worktree 的未修改代码验证结果：

- 冷首跑：完整门禁通过，65.24 秒，frontend lane 38 秒。
- warm 复跑：完整门禁通过，52.777 秒，满足本地 warm gate 小于 60 秒。

## 目标

1. 在全新 Ubuntu/Python 3.13/Node.js 22 环境中，仅依赖仓库声明即可安装并
   运行完整 `scripts/check.sh`。
2. CI 执行的测试不得依赖开发者用户名、checkout 绝对路径、仓库外源文档或
   开发机预装 Python 包。
3. 用稳定的语义契约防止绝对开发机路径和未声明的 Matplotlib 依赖回归。
4. 保持 `scripts/check.sh` 为本地与 GitHub Actions 的唯一完整门禁入口。
5. 保持 Apple Silicon Homebrew Python warm gate 小于 60 秒；CI lane 时长
   继续输出供观察，但不设置 60 秒失败阈值。
6. PR 推送后以 exact-head GitHub Actions 绿灯和独立 subagent 全量审查作为
   本专题的远端验收证据。`CI / full-gate` 仍不是 required check。

## 非目标

- 不清理依赖仓库外 PDF/解析产物的 `build.py`、`validate.py`、gold 生成器或
  一次性开发脚本；它们不在 `scripts/check.sh` 的 CI 执行面。
- 不引入 Docker、devcontainer、uv/Poetry 锁文件或新的依赖分层体系。
- 不缓存 `node_modules`、virtualenv、数据库、`.local` 状态或 Next.js 构建
  产物。
- 不把 GitHub hosted runner 的冷启动或依赖安装时间压到 60 秒内。
- 不改产品运行时行为、数据库 schema 或前端交互。

## 方案

采用“可移植性契约 + 干净环境验证”方案。

### 1. Harness 路径由测试文件位置推导

新增 `fangan/testcases/harness/tests/conftest.py`，以
`Path(__file__).resolve().parents[2]` 得到已提交的
`fangan/testcases` 根目录，并提供 session-scope fixture：

- `testcases_root: Path`
- `gold_paths: tuple[Path, ...]`

5 个失败测试改为消费这些 fixture。特定章节的 gold 使用
`testcases_root / "engram" / ... / "gold.yaml"` 构造；全量评分使用
`gold_paths`。子进程的 `cwd`、CLI 参数及文件读取全部使用 `Path`，不再通过
开发机仓库根目录绕回已提交数据。

fixture 只表达“测试数据相对当前测试包的位置”，不读取当前工作目录、`HOME`
或 Git metadata，因此从普通 checkout、linked worktree、GitHub runner 和任意
临时目录运行都得到同一批 14 份 gold。

### 2. 增加语义可移植性契约

新增 `backend/tests/test_ci_portability_contract.py`，承担两项独立契约：

1. 用 Python AST 扫描 `fangan/testcases/harness/tests/test_*.py` 的字符串常量，
   拒绝 POSIX 或 Windows 绝对路径字面量。检查的是路径语义，不依赖源码行号、
   格式或变量名；失败消息可报告文件和行号用于定位，但行号不是测试身份。
2. 解析 `backend/requirements.txt` 的规范化 distribution 名，要求
   `backend/tests/conftest.py` 直接导入并用于门禁启动的 `matplotlib` 有直接
   声明。该契约只锁定真实 CI 启动依赖，不尝试建立通用的“所有 import 到
   distribution”推断系统。

第一项在修改 harness 测试前应对现有 5 个文件 RED；第二项在补依赖前应因
缺少 `matplotlib` RED。

### 3. 闭合 Python 依赖声明

在 `backend/requirements.txt` 增加 `matplotlib>=3.10`，并注释其两个直接
消费者：

- pytest controller 的共享字体缓存预热；
- `python-igraph` 首次图构建时可能加载的 drawing adapter。

不使用可选导入或 `try/except ModuleNotFoundError` 掩盖依赖缺失。测试启动
明确需要该包，干净安装就必须提供它。Matplotlib 官方安装文档确认发布包为
macOS、Windows 和 Linux 提供 wheel，并建议用包管理器直接安装：
<https://matplotlib.org/stable/install/index.html>。

### 4. 保持 CI 与本地门禁同源

`.github/workflows/ci.yml` 继续只做以下工作：

1. checkout；
2. 安装 `backend/requirements.txt`；
3. `npm ci --prefix frontend`；
4. 调用 `bash scripts/check.sh`。

不在 workflow 中追加第二份 pytest root、跳过列表或平台特判。
`scripts/check.sh` 现有 `contracts/backend/frontend` 三 lane 计时输出继续作为
CI 时间观察数据，不增加超时比较逻辑。

### 5. 文档同步

按仓库约束同步更新：

- `README.md`
- `README_zh.md`
- `AGENTS.md`

三处都写明：

- CI 测试数据必须相对仓库定位；
- 测试直接导入的第三方包必须进入声明依赖；
- 干净 runner 全绿是可移植性验收；
- 本地 warm gate 小于 60 秒，CI 时长只监测；
- developer-only、依赖外部源文档的生成/校验脚本不属于完整离线门禁。

## 验证策略

### TDD 顺序

1. 先添加 AST 绝对路径契约，运行后看到 5 个 harness 测试文件被报告。
2. 添加 Matplotlib 声明契约，运行后看到 `matplotlib` 缺失。
3. 引入 fixture 并迁移 5 个测试，绝对路径契约转绿。
4. 声明 `matplotlib>=3.10`，依赖契约转绿。

### 聚焦验证

- `fangan/testcases/harness/tests`：54 个确定性测试全部通过，gold-vs-gold
  仍精确识别 14 章。
- `backend/tests/test_ci_portability_contract.py` 与
  `backend/tests/test_ci_workflow_contract.py` 全部通过。
- 在不以仓库根目录为当前目录的情况下运行 harness 测试，证明不依赖 `cwd`。
- 从空 Python 3.13 环境安装 `backend/requirements.txt` 后验证
  `import matplotlib.font_manager` 和 backend test collection。

### 完整验证

- Homebrew Miniconda Python：
  `PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh`
  必须全绿；缓存建立后的 warm wall time 必须小于 60 秒。
- GitHub Actions exact head：
  `CI / full-gate` 必须在 `ubuntu-24.04` 全绿。记录 contracts/backend/frontend
  lane 时长，但任何 lane 超过 60 秒都不单独造成失败。
- 独立 subagent 审查 exact pushed head，Critical/Important 必须为 0。

## 错误处理与边界

- 如果干净 Python 环境在补 Matplotlib 后暴露新的缺失依赖，先读取完整 import
  traceback，确认它是否为 CI 直接执行路径，再用同一“直接消费即直接声明”规则
  逐项处理；不批量猜测安装包。
- 如果 GitHub runner 失败而本地干净环境通过，保留原 run 并检查 exact SHA、
  失败 lane 与日志；不通过重跑掩盖首个失败。
- 如果 CI 仅因 runner incident、下载服务或平台故障失败，记录外部根因并等待，
  不修改测试选择或放宽断言。
- required check 保持关闭，除非后续已观察到稳定 PR/post-merge 绿跑且用户另行
  明确批准。

## 交付

本专题在 `codex/ci-portability` 分支和独立 worktree 中完成，形成一个 PR。
实现任务使用按复杂度分配的 subagent；PR 创建后，由未参与实现的
`gpt-5.6-terra` high-reasoning subagent 审查 exact pushed head。worktree 在
PR 合并前保留。
