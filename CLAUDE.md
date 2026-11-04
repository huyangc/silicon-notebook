# Claude Code 在本仓库的操作规范

本文件由 Claude Code 会话自动加载，只保留载体专属的常驻规则。仓库通用工作规则与
权威文档路由先读 `AGENTS.md`；产品、架构、部署、运维和开发合同分别以它列出的
canonical documents 为准。共享开发、验证与交付规则以 `docs/development.md` 及中文
配对文档为准；入口有冲突时按权威文档修正。本文件的载体专属规则仅适用于 Claude Code。

## 子代理规范

### 显式选择模型

每次 `Agent` 调用必须显式传 `model`，或使用 `.claude/agents/` 中已经钉好模型的角色；
不得默认继承主 agent。唯一例外是 `subagent_type: "fork"`，因为 fork 语义要求继承父模型。
省略 `subagent_type` 等同于默认 general-purpose，不属于例外。

`.claude/hooks/require-subagent-model.py` 是这条规则的 PreToolUse 硬门。模型按任务所需判断力
选择，而不是按任务大小选择：

| 模型 | 适用任务 |
| --- | --- |
| `opus` | 计划、规格/代码评审、架构取舍、疑难归因、安全审查 |
| `sonnet` | 规格已固定的实现、机械重构、测试和文档同步 |
| `haiku` | 文件、符号、调用点的纯检索与清点 |

拿不准时选 `opus`。子代理报告计划有歧义或返工一次后，应先用 `opus` 修正计划，不能只换
更强模型重复执行同一份有问题的计划。

仓库预置角色：

| `subagent_type` | 模型 | 用途 |
| --- | --- | --- |
| `impl-task` | sonnet | 执行一个规格固定的实现任务 |
| `spec-review` | opus | 任务级规格符合性评审 |
| `code-quality-review` | opus | 任务级质量与回归风险评审 |

角色列表在会话启动时枚举；新增或改名角色后需重启会话。未知角色会响亮失败，此时使用内置
角色并显式传模型。角色名精确匹配，不做大小写或分隔符归一。

规格已定死的多文件实现默认经 `/codex-impl` 交给本机 codex，不可用时回退 `impl-task`。

### 委托与交付入口

委托前读 [Delegation and review](docs/development.md#delegation-and-review)；按完整验收边界
安排实现与评审，向子代理只提供目标、相关路径、权威章节和验证责任，接收结果后针对疑点复核。

用户授权 PR 交付时，读 [PR 评审与合入](docs/development_zh.md#pr-评审与合入)及
[Claude Code PR 操作](docs/development_zh.md#claude-code-pr-操作)，按原始评审证据、远端 head
与 CI 判定交付；本地修改请求的授权边界见 `AGENTS.md`。

## 权威文档索引

- 仓库通用 Agent 规则与路由：`AGENTS.md`
- 开发、验证、CI 与 PR 政策：`docs/development.md` / `docs/development_zh.md`
- 产品/API、架构、部署和运维：使用 `AGENTS.md` 的 routing table
- Claude Code hook 与角色：`.claude/hooks/`、`.claude/agents/`、`.claude/skills/`
