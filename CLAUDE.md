# Claude Code 在本仓库的操作规范

本文件对 **Claude Code** 生效，每个会话自动加载。

`AGENTS.md` 是本仓库完整的开发契约，但 **Claude Code 不会自动加载它**（只加载 `CLAUDE.md` 与 `.claude/rules/`），所以本文件承担两件事：把必须随时在线的红线内联在这里，再给出 `AGENTS.md` 的章节索引供按需查阅。**两者冲突时以 `AGENTS.md` 为准**——它是真源，本文件是摘要。改动开发约束时两边一起改。

---

## 一、红线

不读 `AGENTS.md` 也必须遵守的部分。

### 工作区

- 每个新特性开一个 **git worktree** 在新分支上做，不要在主 checkout 上直接切分支。当前目录若已是 worktree，就在原地继续。
- **绝不在 worktree 里跑 `npm install`**：`frontend/node_modules` 是软链到主 checkout 的共享安装树，会写穿真树。要改依赖去主 checkout，或用 `cp -Rc` 拿隔离副本。
- 不回滚用户的改动；不删生成物或用户提供的文件，除非用户明确要求。
- 改文件用 Edit/Write，不要用 shell 重定向整体覆写。（`AGENTS.md` 写的 `apply_patch` 是 codex 语境，Claude Code 的等价物就是 Edit/Write。）

### 交付完整性

- **全栈对等**：面向用户的后端能力必须在同一次改动里带上前端 UI。不接受只做一侧。
- **文档同步**：影响安装、产品行为、架构或开发约束时，`README.md`、`README_zh.md`、`AGENTS.md` 三份一起改。
- 完成 `silicon_notebook_fangan.md` 里定义的特性时，同批更新 `fangan_done.md`。
- 提交的文档保持通用口径：绝对解释器路径、本机端口占用这类**机器特定细节不进 git**。

### 硬门

- 完整本地门是 `bash scripts/check.sh`（后端 pytest + 语法/契约/harness + 前端 test/typecheck/build 三条并发泳道）。CI 只是它的只读包装，不要在 workflow 里另起测试根。
- **schema**：加表或改结构必须**追加** `_migration_N` 并 bump `SCHEMA_VERSION`，不要塞进已封版的旧迁移——版本闸会对已部署库短路，`IF NOT EXISTS` 救不了没被执行到的语句。
- **界面词汇**：面向用户的文案只用「界面词」，不得出现 `projection`/`tier`/`canonical`/`chunk`/`KG` 这类内部黑话。真源是 `AGENTS.md`「界面词汇表」，`scripts/check_ui_vocabulary.py` 是硬门。
- **错误文案**：deny by default，信任按**出处**判定而非文本形状。后端中文用户文案必须走 `backend/app/api/deps.py` 的 `user_error()`（打 `X-User-Message` 头），前端翻译只在 `frontend/app/errors.ts`。
- **`object_type` 标签**：后端 `OBJECT_TYPE_LABELS` 与前端 `KG_TYPE_LABELS` 必须逐字一致，改一侧就要改另一侧。
- **架构守卫**是语义化的（`{path, scope, kind, target}`），**不含行号**——仓库里任何提到「行号钉死」的注释都是过时残留。重生成走 `--rebaseline-surface` / `--rebaseline-callers`；新端点必须跑默认模式刷 `api_contract`。

### 工程约束

- **效率是一等约束**：新增 LLM / embedding / DB 调用前先问代价——能否合并、缓存、异步、按需 gate。强一致做成 opt-in，默认走低开销路径。
- 不引入 Docker 作为一期默认工作流；装新包前先问。
- **加了守卫 ≠ 有效**：必须做**变异验证**——把代码改回违规形态，确认它真的报红。只做「删除」变异不够，还要做「移动」变异。变异本身极易打空（替了字面量但代码用的是常量、按行号插入而行号已漂），先 `grep -c` 确认改到了再跑。
- 收尾提 PR，不直接合 `master`。分支先 rebase 到 `master` 保持线性，再 push、`gh pr create --base master`。

---

## 二、子代理规范

### 1. 起子代理必须显式选模型

**不得默认继承主 agent 的模型。** 每次 `Agent` 调用要么显式传 `model`，要么用 `.claude/agents/` 里已钉好模型的角色。

`.claude/hooks/require-subagent-model.py` 是这条的 PreToolUse 硬门：没显式选模型且角色未钉模型的调用会被拦下。

**判据是任务需要多少判断力，不是任务有多大。**

| 模型 | 适用 | 典型任务 |
| --- | --- | --- |
| `opus` | 需要判断力，要能推翻既有结论 | 写实现计划、规格/代码评审、架构取舍、疑难 bug 归因、安全审查、跨文件因果推理 |
| `sonnet` | 规格已定死的转录型工作 | 计划已写明改哪些文件怎么改、机械重构、补测试、文档同步、照既定模式扩展 |
| `haiku` | 纯检索定位清点 | 找文件、列符号、grep 汇总、清点调用点——只需汇报不需推理 |

内置角色（`Explore`、`general-purpose`、`Plan` 等）同样要传 `model`：`Explore` 做的是检索，一般 `haiku` 或 `sonnet` 就够。

**拿不准就上 `opus`**：返工一次的成本远高于模型差价。

**返工时先查计划，别默认怪模型。** 实测归因（PR#288）：`sonnet` 实现者反而抓到了计划里的 bug，问题出在计划不在执行。所以升级路径是——子代理报告「计划有歧义」或返工 ≥1 次时，**回去用 `opus` 重写计划**，而不是把同一份烂计划换 `opus` 再跑一遍。

### 2. 仓库角色（模型已钉在定义里）

| `subagent_type` | 模型 | 用途 |
| --- | --- | --- |
| `impl-task` | sonnet | 执行单个规格已定死的实现任务 |
| `spec-review` | opus | 任务级规格符合性评审 |
| `code-quality-review` | opus | 任务级代码质量与回归风险评审 |

实现任务本身需要设计取舍时，不要用 `impl-task`，改用通用子代理并显式传 `model: opus`。

### 3. 何时**不**起子代理

子代理会重建上下文、重新探索、写报告，然后你还要再读一遍报告。收益不明显就自己做：

- 几个文件的读改、简单搜索、小范围验证 —— 自己做更快更准。
- 一个不大的任务不要拆给多个并行子代理。
- 能一个子代理做完的，就不要用多个。

### 4. 本仓库刻意覆盖的通用默认：评审外包

Claude Code 的通用默认是「评审和验证留在主 agent 循环里，不要外包给子代理」。**本仓库刻意相反**：

> 已批准的多步实现计划按「子代理逐任务」执行——每个任务用一个全新的实现子代理，完成后跑任务级的规格评审与代码质量评审，再推进下一个任务。

理由是实测的（PR#288）：opus 的价值主要兑现在评审环节——变异验证、CSS 推演、推翻错误诊断都出自评审子代理。这是用户的明确决定，别按通用默认改回去。

例外：纯研究、设计、状态查询、只读评审类工作**不需要** worktree，也不需要子代理。

### 5. 子代理简报必备字段

一次把上下文喂足，避免「起 → 等 → 补简报」的往返：

1. **目标与验收标准** —— 怎样算完成。
2. **相关文件的绝对路径** —— 别让它自己猜。
3. **适用的红线** —— 从上面第一节挑相关的贴过去；子代理看不到你的对话历史。
4. **输出格式** —— 改了什么 / 没改什么 / 跑了什么验证命令与真实结果 / 阻塞与存疑。

多个互相独立的子代理放在**同一条消息**里并发发起。默认并发 ≤ 4；超过 20 个必须用户明确要求。

### 6. 委托后不返工

派出去了就认结果：不要自己再重做一遍它的工作，也不要在它汇报后重新推导它的结论。有疑问就针对性复核具体某一条，而不是整体重来。

---

## 三、`AGENTS.md` 章节索引

按需查阅，用章节标题定位（不要依赖行号）：

| 要查什么 | `AGENTS.md` 章节 |
| --- | --- |
| 文档三份同步规则 | Documentation Sync |
| 完成 spec 特性怎么记账 | Tracking Completed Spec Features |
| 后端能力必须配前端 | Full-Stack Parity (Backend ⇄ Frontend) |
| 产品形态、四个页签、上传到问答的完整流程 | Product Flow / MVP Scope |
| 分层架构、repository facade、端口与适配器 | Architecture Baseline |
| Python 环境、`PYTHON_BIN`、后端启动命令 | Python Environment / Backend Commands |
| 前端约定、`object_type` 标签契约、错误层三段规则 | Frontend/UI |
| 界面词 ↔ 内部词对照表（词汇守卫的真源） | 界面词汇表 (User-Facing Vocabulary) |
| 依赖政策、Python 版本下限、不加 Docker | Dependency Policy / No Docker In First Version |
| LLM 环境变量、模型服务状态与诊断 | LLM Configuration |
| 事件日志、按用户隔离的日志目录 | Logging / Observability |
| `scripts/check.sh` 三泳道、暖门时间目标、CI | Verification / GitHub Actions CI |
| 测试怎么分层、测试根在哪、并发 worker | Test Architecture |
| worktree、子代理逐任务、文件安全 | Git And File Safety |
| 收尾提 PR 的标准流程 | Feature Completion (Finish With a PR) |
