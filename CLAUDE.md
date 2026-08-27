# Claude Code 在本仓库的操作规范

本文件由 Claude Code 会话自动加载，只保留载体专属的常驻规则。仓库通用工作规则与
权威文档路由先读 `AGENTS.md`；产品、架构、部署、运维和开发合同分别以它列出的
canonical documents 为准。本文件不是第二份产品或架构手册。

## 会话与工作区

- 开始写入前先执行 `git status --short`，确认当前目录已经是隔离的 linked worktree，
  并在独立分支上工作。保留用户已有改动，不用破坏性命令换取干净工作树。
- 在 worktree 里运行 `npm install` 前先执行 `ls -l frontend/node_modules`。若它是指向主
  checkout 的软链，不得写穿共享依赖树；需要独立依赖时复制或在正确 checkout 安装。
- 编辑文件使用 Claude Code 的 Edit/Write 能力，不用 shell 重定向整体覆写。
- GitHub 网络操作（`git fetch`、`git push`、`gh auth/repo/pr`）直接申请沙箱外执行；
  本地只读 Git 检查和环境自足的标准门在沙箱内完成。
- 文档所有权、测试门和 PR 政策以 `docs/development.md` / `_zh.md` 为准。产品或架构变化
  只更新对应权威文档，不把细节复制到本文件。

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

### 何时使用

已批准的多步实现计划默认逐任务委托：每个任务使用新的实现子代理，完成后分别做规格评审和
代码质量评审，再推进下一项。纯研究、设计、状态查询和只读评审不强制使用子代理。

几个文件的简单读改、小范围搜索和验证由主 agent 直接完成。不要把一个小任务拆成多个并行
代理；一个代理能完成时不要使用多个。

### 简报与接收结果

子代理简报一次提供：

1. 目标与验收标准；
2. 相关文件的绝对路径；
3. 适用的权威文档和红线；
4. 输出格式，包括改动、验证结果、阻塞与存疑。

独立任务可同批并发，默认并发不超过 4；超过 20 个需用户明确要求。委托后接受其工作结果，
只针对具体疑点复核，不把整项任务重新做一遍。

## 提 PR 与合入：codex 评审闭环

PR 合入前必须有 codex 评审。每一轮原始输出都要逐字贴到 PR，包括零意见和手动补跑的
轮次，并附触发方式、完整命令、PR 远端 head SHA、退出码和输出字节数。

自动化由开发者本机的 `~/.claude/hooks/codex-pr-review.sh` 提供，它不是仓库文件。换机器或
新 clone 没有 hook 时，规则仍然成立，必须手动执行：

```bash
codex exec review --base <base> --ephemeral \
  -c 'model_reasoning_effort=medium' -c 'notify=[]' -o <out>
```

`review --base` 与自定义 prompt 互斥。评审只检查 diff 和提交方的验证证据，不在评审沙箱中
重跑 `scripts/check.sh` 或前端构建；完整门由实现方执行并在 PR 中说明。

### 评审有效性

- 一轮只有在退出码为 0 且输出非空时才有效；SIGTERM 可能返回 0，不能只看退出码。
- P0/P1 阻塞合入：核实后修复并重审，直到非阻塞。只有 finding 不成立或修复方向需要人决定
  时才停下；若驳回 finding，必须在 PR 给出理由与证据、在代码记录取舍并加回归测试。
- P2/P3 不阻塞，可以说明理由后不改。优先级无法解析时保守阻塞，不得默认通过。
- 非阻塞评审后若又提交修复，自动 hook 未必重跑；必须手动补跑并贴回新 head 的输出。
- `base..HEAD` 为空是硬失败，通常说明命令运行在错误 worktree。

手动 hook 操作：

```bash
~/.claude/hooks/codex-pr-review.sh set-state <PR号> <waived|awaiting_fix|passed>
~/.claude/hooks/codex-pr-review.sh show-state <PR号>
~/.claude/hooks/codex-pr-review.sh run <PR号> manual
~/.claude/hooks/codex-pr-review.sh verify <PR号>
```

`verify` 只以 GitHub PR 评论为证据，核对评论中的 head 是否等于 PR 的远端 `headRefOid`；
不得用本地 `git rev-parse HEAD` 代替，因为本地可能落后于实际将要合入的提交。

### CI 与合入

默认闭环到合入：评审非阻塞、`gh pr checks <PR号>` 中每个 check 都是 `pass`，并且
`verify <PR号>` 成功后，直接执行：

```bash
gh pr merge <PR号> --rebase
```

用户明确说“等我合入”时例外，只交回 PR 链接和状态。`mergeStateStatus: CLEAN` 不代表 CI
全绿。既有本地失败必须在 PR 说明并给出基线证据，不能当作已通过；CI 才是远端权威。
stacked PR 使用自己的基分支，不能硬编码 `master`。是否已合入只认
`gh pr view --json state` 返回 `MERGED`，不靠 SHA 祖先关系判断 rebase/squash 结果。

## 权威文档索引

- 仓库通用 Agent 规则与路由：`AGENTS.md`
- 开发、验证、CI 与 PR 政策：`docs/development.md` / `docs/development_zh.md`
- 产品/API、架构、部署和运维：使用 `AGENTS.md` 的 routing table
- Claude Code hook 与角色：`.claude/hooks/`、`.claude/agents/`
