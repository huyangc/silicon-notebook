# Claude Code 在本仓库的操作规范

本文件对 **Claude Code** 生效，每个会话自动加载——所以它的每一个字符都是**每次请求的固定成本**。

`AGENTS.md` 是本仓库完整的开发契约，但 **Claude Code 不会自动加载它**（只加载 `CLAUDE.md` 与 `.claude/rules/`）。两者因此分工：

- **本文件只收两类内容**：①「对下一个改动也成立」的通用约束；②只有 Claude Code 语境才有的规程（worktree、子代理、codex 评审闭环）——这些在 `AGENTS.md` 里没有，或者与它刻意不同。
- **特性级契约不进这里**：某个端点的护栏、某张表的迁移细节、某个页面的版式不变量、某次评审记下的取舍，一律写进 `AGENTS.md` 与 `docs/`；本文件只在第四章给出**路由**。
- **冲突时以 `AGENTS.md` 为准**——它是真源，本文件是通用约束加索引。穷举的例外见第一章末尾。

判据只有一句：**写下一条规则前先问「与该特性无关的下一个改动，还需要它吗？」** 不需要，就写进 `AGENTS.md` / `docs/`，不要往这里追加。本文件有字符数硬门（`scripts/check_claude_md_budget.py`，挂在 G1 contracts 泳道），追加特性段落会直接把门禁打红——那是刻意的：这个文件在 2026-07-24 到 08-27 之间从 20 KB 涨到 307 KB，靠的正是「每个 PR 追加一段、从不回收」。

---

## 一、随时在线的红线

不读 `AGENTS.md` 也必须遵守的部分。

### 工作区

- 凡任务会写仓库代码、测试、文档或配置，第一次写入前必须新开 **git worktree** 和分支；该任务期间主 checkout 只读，小修也不例外。当前目录若已是隔离 worktree，就在原地继续。纯调研、设计、状态汇报和只读审查除外。
- **在 worktree 里跑 `npm install` 之前先 `ls -l frontend/node_modules`**：若它是**软链**（指向主 checkout 的共享安装树），装依赖会写穿真树，绝不能跑——改依赖去主 checkout，或用 `cp -Rc` 拿隔离副本。若它是真目录或不存在，照常装即可。软链由开发者本机的 SessionStart hook 建立，不是仓库产物，所以这条**必须先看一眼再决定**，不能当成无条件红线。
- 不回滚用户的改动；不删生成物或用户提供的文件，除非用户明确要求。
- 改文件用 Edit/Write，不要用 shell 重定向整体覆写。

### 每次改动都适用

- **全栈对等**：面向用户的后端能力必须在同一次改动里带上前端 UI。不接受只做一侧。
- **文档同步**：影响安装、产品行为、架构或开发约束时，`README.md`、`README_zh.md`、`AGENTS.md` 三份一起改，并同步 `docs/` 下负责该主题的中英文权威文档。根 README 只保留精简入口，详细契约不得堆回 README。**`CLAUDE.md` 只在改动触及本文件已有的通用约束时才动**——特性级契约按上面的判据进 `AGENTS.md` / `docs/`。唯一的例外是本章「扩展点边界」那一条：有四条 G1 守卫要求它的关键词出现在本文件正文里（`backend/tests/test_phase0_architecture_guard.py` 三条、`backend/tests/test_architecture_documentation.py` 一条），改动它时要连那四条一起跑。
- **数值上限与截断**：生产路径不得新增会改变结果的数字字面量切片/上限（如 `hits[:20]`）。不可调的协议边界复用具名常量，部署需在质量/成本间调整的预算放进带校验的 `Settings`。用户编辑的数据不得静默截断：前端显示同一护栏，API 超限明确拒绝。embedding/模型输入截断必须共用既有配置真源，避免在线、批处理与回填路径分叉。测试 fixture 的显式数字不在本规则范围内。**精确数值只登记在 `docs/product-and-api.md` / `_zh.md`**，不要抄进本文件。
- **界面词汇**：面向用户的文案只用「界面词」，不得出现 `projection`/`tier`/`canonical`/`chunk`/`KG`/`schema` 这类内部黑话。真源是 `AGENTS.md`「界面词汇表」，`scripts/check_ui_vocabulary.py` 是硬门。唯一放行的英文界面词是「图谱 Schema」。
- **错误文案**：deny by default，信任按**出处**判定而非文本形状。后端中文用户文案必须走 `backend/app/api/deps.py` 的 `user_error()`（打 `X-User-Message` 头），前端翻译只在 `frontend/app/errors.ts`。
- **生产/测试目录边界**：前端生产代码只放 `frontend/app` 与 `frontend/features`；测试入口只放 `frontend/tests/{unit,component,guards}`，共享 setup/adapter 放 `frontend/test-support`。不得把测试搬回生产目录。用户文案守卫的信任边界同时覆盖 `app` 与 `features`。
- **schema 变更**：加表或改结构必须**追加** `_migration_N` 并 bump `SCHEMA_VERSION`，不要塞进已封版的旧迁移——版本闸会对已部署库短路，`IF NOT EXISTS` 救不了没被执行到的语句。当前版本号、双后端配对、正向 shadow 的表数/unique surface/row slot 不变量，以及每次迁移的逐条设计理由，都在 `AGENTS.md`「Architecture Baseline」。
- **加了守卫 ≠ 有效**：必须做**变异验证**——把代码改回违规形态，确认它真的报红。只做「删除」变异不够，还要做「移动」变异。变异本身极易打空（替了字面量但代码用的是常量、按行号插入而行号已漂），先 `grep -c` 确认改到了再跑。做变异实验前先 commit：`git checkout` 恢复会连未提交改动一起回滚。
- **效率是一等约束**：新增 LLM / embedding / DB 调用前先问代价——能否合并、缓存、异步、按需 gate。强一致做成 opt-in，默认走低开销路径。
- **UI 先找现成基座，不要另造一套**：浮动弹窗用 `frontend/app/use-floating-window.ts`（`page.tsx` 内联弹窗走 `FloatingModalCard`）；五档强度控件用 `frontend/app/effort-picker.tsx` 的 `EffortPicker`；异常小字用 `AnomalyBadge` + `sourceAnomalies()` 与 `--danger`/`--warning` token；长任务按钮点完必须立刻置灰并换成按该动作语义写的进行态文案。这几条各自有回归门在 `frontend/tests/guards/`，逐条契约见 `AGENTS.md`「Frontend/UI」。
- **架构守卫是语义化的**（`{path, scope, kind, target}`），**不含行号**——仓库里任何提到「行号钉死」的注释都是过时残留。重生成走 `--rebaseline-surface` / `--rebaseline-callers`；新端点必须跑默认模式的 `scripts/generate_repository_contract_fixtures.py` 刷 `api_contract`。
- **扩展点边界**：`frontend/features/extension-sdk` 是唯一 build-time UI registry 与 SDK；后端 `extensions` 拥有启动冻结 registry 与共享的 retrieval contributor host，required capability 启动时冻结、availability 每次实时算。部署插件只从 `EXTENSIONS_CONFIG`（deployment 配置）点名的那一份 TOML 装载，`trust` 只接受 `builtin` 与 `deployment`（`isolated` 是保留值，一律拒绝），改配置要重启才生效；插件自己的 api extensions 只挂在 `/api/extensions/{plugin_id}` 之下并经 router 级会话认证，每个 core 端口自己对当前请求用户做授权判定，插件拿不到 repository、全局 `Settings`、model client 或原始 token。Ask 的 reasoning 交接走 `backend/app/application` 的不可变 stage envelope，每个 stage 携带同一个 retrieval run，跨 stage 持有数据库 connection（连接）即 `StageBoundaryError`；深度报告同构，只有 `generating → done` 的原子 CAS 成功才触发 `report.completed observer`。每个模块化架构 PR 走两路独立 subagent review、CI 全绿后合入。逐条契约见 `AGENTS.md`「Architecture Baseline」与 `docs/modular-plugin-architecture-design-2026-08-21.md`。
- **依赖与环境**：不引入 Docker 作为一期默认工作流；装新包前先问。
- 完成 `silicon_notebook_fangan.md` 里定义的特性时，同批更新 `fangan_done.md`。
- 提交的文档保持通用口径：绝对解释器路径、本机端口占用这类**机器特定细节不进 git**。

### 硬门

- 门禁分为 **G0** 目标测试、**G1** `bash scripts/check.sh`（编辑期及每次 PR/push，三条并发泳道 contracts / backend / frontend，Apple Silicon warm 目标 ≤60 秒）、**G2** `bash scripts/check_extended.sh`（补跑 slow 与重活语义扫描，每日定时，也可手动触发）、**G3** 独立 PostgreSQL 集成门。G1/G2 的 backend marker 必须精确互补。测试加速不得改变断言与生产默认值；普通 UT 与 G1 测试必须环境自足，不绑定宿主端口、不依赖环境服务。泳道构成、Node 版本旁路、测试隔离与加速的逐条约束见 `AGENTS.md`「Verification」「Test Architecture」与 `docs/development*.md`。
- **Codex 权限规则**：GitHub 网络操作（`git fetch`、`git push`、`gh auth/repo/pr`）必须直接申请沙箱外执行；普通本地只读 Git 检查与环境自足的 G1 门禁在沙箱内完成。
- 数据库专项门只覆盖直接 PostgreSQL 后端；已退役的 SQLite 后端实现专项测试、SQLite→PostgreSQL 导入/正向 shadow 测试与跨后端 parity 测试不得重新加入当前套件。
- 收尾提 PR，不直接合 `master`。分支先 rebase 到基分支保持线性，再 push、`gh pr create --base <基分支>`（通常是 `master`，stacked PR 不是）。提 PR 与合入都必须经过 codex 评审，见第三章。

### 刻意不遵从 `AGENTS.md` 的几处

本文件其余部分都服从「冲突时以 `AGENTS.md` 为准」。以下是**穷举**的例外，各有理由：

1. **合入方式**：`AGENTS.md`「Feature Completion」第 1 步写的是把最新 `master` **3-way merge 进特性分支**，与本仓库实际使用的 Rebase and merge 冲突——把 `master` 合进来会破坏分支线性，GitHub 会报 `cannot be rebased`。以 **rebase** 为准，并提醒用户订正 `AGENTS.md`。
2. **worktree 里的 `npm install`**：`AGENTS.md`「Frontend/UI」写「缺 `frontend/node_modules` 就先跑 `npm install`」，在软链共享安装树的 worktree 里照做会写穿主 checkout。以上面「先 `ls -l` 再决定」为准。
3. **编辑方式**：`AGENTS.md`「Git And File Safety」写 `apply_patch`，那是 codex 语境；Claude Code 的等价物是 Edit/Write。这条是载体差异不是规则分歧。

---

## 二、子代理规范

### 1. 起子代理必须显式选模型

**不得默认继承主 agent 的模型。** 每次 `Agent` 调用要么显式传 `model`，要么用 `.claude/agents/` 里已钉好模型的角色。

`.claude/hooks/require-subagent-model.py` 是这条的 PreToolUse 硬门：没显式选模型且角色未钉模型的调用会被拦下。唯一的豁免是 `subagent_type: "fork"`——fork 语义上必须继承父模型，传 `model` 本就无效。省略 `subagent_type` **不算** fork，它等于默认 general-purpose，照样要选模型。

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

⚠️ 可用的 `subagent_type` 列表在**会话启动时枚举**：刚新增或改名的角色定义，要重启会话才会出现，否则调用会报 `Agent type not found`。这是**响亮失败**不是静默降级——未知的 `subagent_type` 一律直接报错，不会悄悄落回默认角色。撞上时的兜底是用内置角色并显式传 `model`，判据不变。

同理，`subagent_type` 是**精确匹配**，不做大小写与分隔符的宽松归一：`Impl Task` 不会解析成 `impl-task`，只会报错。

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
3. **适用的红线** —— 从第一章挑相关的贴过去，特性级契约从 `AGENTS.md` / `docs/` 里摘；子代理看不到你的对话历史，也不会自动读 `AGENTS.md`。
4. **输出格式** —— 改了什么 / 没改什么 / 跑了什么验证命令与真实结果 / 阻塞与存疑。

多个互相独立的子代理放在**同一条消息**里并发发起。默认并发 ≤ 4；超过 20 个必须用户明确要求。

### 6. 委托后不返工

派出去了就认结果：不要自己再重做一遍它的工作，也不要在它汇报后重新推导它的结论。有疑问就针对性复核具体某一条，而不是整体重来。

---

## 三、提 PR 与合入：codex 评审闭环

**提 PR 之后、合入之前必须有 codex 评审，每一轮的原始输出逐字贴回 PR。**

执行由本机全局的 PostToolUse hook `~/.claude/hooks/codex-pr-review.sh` 承担。**它不是仓库产物**，`git grep` 在本仓库里找不到它；换机器或新 clone 上没有它时规则依然成立，那就手动跑。

### 触发判据不看命令文本

**这条是踩出来的（2026-08-16，PR #511）**：原实现按 `gh pr create` / `git push` 的命令**文本**匹配。我用 `$GH pr create`（把 gh 放进 shell 变量）开了 PR，文本匹配不上 → 评审**一轮都没跑、PR 上零评论**，而我还在汇报「hook 正在评审」。这类失败不报错、只是什么都不发生，是整条链最危险的形态；命令文本是其中最脆的一环，所以判据整个换掉了。

PR 号的来源，按顺序：

1. **命令输出里出现 PR URL** —— `gh pr create` 必然打印，且不受调用形式影响（`gh` / `$GH` / 绝对路径一视同仁）。
2. **本分支已知的 PR** —— 第一次见到就把 branch→PR 缓存在本机，之后任何一条 Bash 命令都能零 API 认出它。

两者都只认**当前分支**的 PR（比对 `headRefName`），免得别处贴出的 PR URL 把评审引到无关 PR 上。

跑不跑，两个闸：

- **从没评审过（rounds=0）** → 跑第 1 轮。
- **上一轮判了 P0/P1（`awaiting_fix`）且 HEAD SHA 与上轮评审的不同** → 重审。「改完了」由提交本身证明，不由命令长什么样证明。

**推论，很容易踩：上一轮是 🟡 或 🟢 之后再提交修复，不会自动重审。** 这时必须自己补跑并补贴——hook 只代贴它自己跑的那几轮。

钩子必须 `cd` 到发起该命令的目录（PostToolUse 载荷里的 `cwd`），否则空 diff 硬失败会在每次多 worktree 的会话里误报。载荷不是合法 JSON 时**出声**而不是静默退出：它与「这条命令与评审无关」在退出码上原本长得一模一样。

手动命令与 hook 内部一致：

```bash
codex exec review --base <base> --ephemeral \
  -c 'model_reasoning_effort=medium' -c 'notify=[]' -o <out>
```

`review --base` 与自定义 PROMPT 位置参数互斥（报 `cannot be used with '[PROMPT]'`），所以不传自定义指令，正文是 codex 原生英文输出。对 codex 评审行为的约定只能经 `AGENTS.md` 传达（codex 每轮自动加载它）：已写明评审场景**勿重跑 `check.sh`/前端构建**（评审沙箱写不了 `frontend/node_modules`，必然 EPERM；完整门由提交方本地跑并在 PR 附结果）——该规则仅限评审场景，不放松实现场景的硬门。

### 判成败要双判据

**退出码 0 且输出非空。** codex 被 SIGTERM 杀掉时退出码也是 0，只看退出码会贴出一条空评论、假装评审通过。

### 每轮都贴原文

包括零意见的轮次，也包括手动补跑的轮次。评论里带上：触发方式（自动 / 手动）、完整命令、head SHA、退出码与输出字节数。**贴原始输出，不贴我的转述**——用户要能自己核对我确实跑了、且结论没被我复述失真。

### 分级与闸门

| 判定 | 含义 | 动作 |
| --- | --- | --- |
| 🔴 P0/P1 | 阻塞 | 状态自动置 `awaiting_fix`；**核实后修掉再重审**（2026-08-16 用户原话：「review不通过就返回修复，修复完之后再等CI变绿然后合入」）。只有意见站不住（走驳回三件套）、或修复方向需要人拍板时才停下来问人。改完提交后会自动触发下一轮 |
| 🟡 P2/P3 | 非阻塞 | 可以如实说明后不改 |
| ⚪️ 解析不出标签但正文很长 | 格式可能变了 | 保守拦人，绝不因解析失败默认放行 |
| 🟢 无输出标签且正文很短 | 通过 | 等 CI 绿后按下面「合入」处理 |

自动评审上限 5 轮（`CODEX_REVIEW_MAX_ROUNDS`）。人决定放弃修复时，由我显式落状态；🟡/🟢 之后修完再审也由我手动补跑（那种情况 push 不会自动触发，见上）：

```bash
~/.claude/hooks/codex-pr-review.sh set-state <PR号> <waived|awaiting_fix|passed>
~/.claude/hooks/codex-pr-review.sh show-state <PR号>
~/.claude/hooks/codex-pr-review.sh run <PR号> manual
~/.claude/hooks/codex-pr-review.sh verify <PR号>
```

`verify` 是**给人核对用的**：它不读本地状态文件（那是我写的），只查 GitHub 上的实际评论里有没有针对 **PR 远端 head**（`headRefOid`）的评审，没有就非零退出。基准必须是远端而不是本地 `git rev-parse HEAD`：本地落后时（别人推了、或我从另一个 checkout 推了），按本地 SHA 会命中一条旧评审而放行，而 `gh pr merge` 合的是远端那个没被评审过的 head。合入前必须先跑它——见下面「合入」。

### 意见不是照单全收

codex 的评审对象是 diff，它未必了解本 harness 的运行时事实。核实后可以驳回，但必须三件事齐全：在 PR 上写明驳回理由与证据、在代码里留下这条取舍的注释、加反向护栏用例钉住它。（PR#322 驳回过「豁免省略 `subagent_type` 的隐式 fork」，照做会把守卫整个掏空。）

### 空 diff 是硬失败

`base..HEAD` 算成空 diff 时 hook 会硬失败，而不是跑出一句「未发现问题」——假绿比不跑更糟。多半是当前目录不是 PR 改动所在的 worktree，`cd` 到正确目录再跑。

### 合入

- **默认闭环到合入**：codex 判 🟡/🟢（非阻塞）**且** CI 全绿时直接合，不再逐次问人。**唯一例外**：用户明确说过「等我合入」——说过就绝不自动合，只把 PR 链接和状态交回去。这是 2026-08-15 用户的明确决定，取代了原先「必须先拿到用户明确同意」。
- **合入前先跑 `verify <PR号>` 自证**：它只查 GitHub 上的实际评论、不信本地状态，判据是 **PR 远端 head**，非零退出就是「远端 head 没有已贴出的评审」，那就不许合。这条不是形式主义——评审静默没跑过一次（#511），而当时我自己汇报的是「正在评审」；本地状态和我的说法都不是证据，PR 上的评论才是。
- **闸有三个，缺一不可**（评审非阻塞 + CI 全绿 + `verify` 通过）：🔴 P0/P1 与 ⚪️（解析不出标签但正文很长）**一律不自动合**——先修掉并重审到非阻塞，修不动或意见站不住时才停下来问人；CI 未全绿也不合。判 CI 用 `gh pr checks <PR号>`，只有全部 `pass` 才算绿——`mergeStateStatus: CLEAN` 只说没有冲突/没有必需检查在拦，不等于检查跑绿了。
- 本地门禁有既有失败时，不拿它当合入依据也不假装没有：在 PR 里写清楚它**改动前就存在**（证据：相关目录零改动，或在 base commit 上跑出逐字相同的失败），CI 才是权威。
- 本仓库用 **Rebase and merge**：`gh pr merge <PR号> --rebase`（也有 squash 合入的历史，标题带 `(#NNN)` 后缀的即是）。
- base 不一定是 `master`——stacked PR 的 base 是它的基分支，别硬写 `master`。
- 判断分支是否已进 `master`，**只认 `gh pr view --json state` 为 `MERGED`**。别用 SHA 祖先判断（rebase 会改 SHA），也别只信 `git cherry`（squash 合并会把提交全报成未合）。

---

## 四、按需查阅：主题 → 真源

**动手改一个特性前，先把它的契约读进来。** 本文件不再内联这些契约——它们在 `AGENTS.md` 与 `docs/` 里有更全的版本，而那两处才是随代码一起维护的真源。

顺序：① 下面两张表定位到章节/文件 → ② 在该文件里按**标题或标识符**搜（不要依赖行号）→ ③ 找到钉住它的守卫测试（见本章末）。

### `AGENTS.md` 章节索引

| 要查什么 | `AGENTS.md` 章节 |
| --- | --- |
| 文档同步规则 | Documentation Sync |
| 完成 spec 特性怎么记账 | Tracking Completed Spec Features |
| 后端能力必须配前端 | Full-Stack Parity (Backend ⇄ Frontend) |
| 数值上限与截断的完整口径 | Numeric Limits and Truncation |
| 产品形态、四个页签、上传到问答的完整流程 | Product Flow / MVP Scope |
| 分层架构、repository facade、端口与适配器、schema 演进史 | Architecture Baseline |
| 插件共享 UI 基座、部署 `ask.engine` / `indexing.pipeline` | Extension UI Kit And Deployment Ask Engines |
| 后端当前基线（已实现到哪一步）、产品命名 | Current Backend Baseline / Product Name |
| Python 环境、`PYTHON_BIN`、后端启动命令 | Python Environment / Backend Commands |
| 前端约定、`object_type` 标签契约、错误层三段规则、前端状态 owner | Frontend/UI |
| 界面词 ↔ 内部词对照表（词汇守卫的真源） | 界面词汇表 (User-Facing Vocabulary) |
| 依赖政策、Python 版本下限、不加 Docker | Dependency Policy / No Docker In First Version |
| LLM 环境变量、模型服务状态与诊断 | LLM Configuration |
| 事件日志、按用户隔离的日志目录 | Logging / Observability |
| 深度报告可信度、检索 run 与报告运行时、来源证据多样性 | Deep Report Credibility Contract / Retrieval Run And Report Runtime / Source Evidence Diversity |
| 所选来源图激活 | Selected-Source Graph Activation |
| KG 探活响应、共享流式 JSON 传输、usage trailer、`model_response_invalid` | KG Probe Response Contract |
| `scripts/check.sh` 三泳道、暖门时间目标、CI | Verification / GitHub Actions CI |
| 测试怎么分层、测试根在哪、并发 worker | Test Architecture / Test Isolation |
| worktree、子代理逐任务、文件安全 | Git And File Safety |
| 收尾提 PR 的标准流程 | Feature Completion (Finish With a PR) |

### 特性契约在哪里

| 改到这些东西 | 先读 |
| --- | --- |
| 问答/深度报告的检索、意图、引用、证据装配 | `AGENTS.md`「Product Flow」+ `docs/product-and-api*.md` |
| 逐步推理档位、集合枚举、大纲便签、检索经验 | `docs/reasoning-enumeration-tools-design.md` + `docs/product-and-api*.md` |
| 数据库 schema、迁移、shadow 复制、仓储适配器 | `AGENTS.md`「Architecture Baseline」 |
| 扩展点、部署插件、UI registry、application stage | `AGENTS.md`「Architecture Baseline」+ `docs/modular-plugin-architecture-design-2026-08-21.md` + `docs/deployment-extensions-sop*.md` |
| 解析器、MinerU、图片摄取、Markdown ZIP | `AGENTS.md`「Parser Capability Registry」+ `docs/product-and-api*.md` |
| MCP 工具面、Agent token、scope 词表 | `docs/agent-mcp-memory-sop*.md` + `AGENTS.md`「MVP Scope」 |
| 权限、群组共享、授权边、能力映射 | `AGENTS.md`「Architecture Baseline」+ `docs/product-and-api*.md` |
| 前端状态 owner、弹窗协调、界面版式不变量 | `AGENTS.md`「Frontend/UI」 |
| 任何精确数值上限（预算、条数、字符数、阈值） | **只在** `docs/product-and-api.md` / `_zh.md` |
| 部署变量、模型服务配置、运维命令 | `docs/deployment-and-configuration*.md` / `docs/operations*.md` |
| 门禁、测试分层、守卫清单 | `docs/development*.md` |

### 守卫测试怎么找

大部分契约都有一条钉住它的守卫，**改契约前先找到它**，改完做变异验证（见第一章）：

- 后端：`backend/tests/test_*_guard.py`、`test_architecture_documentation.py`、`test_phase0_architecture_guard.py`；按契约里的标识符 `grep -rn` 最快。
- 前端：`frontend/tests/guards/`（语义 AST 守卫）、`frontend/tests/component/`、`frontend/tests/unit/`。
- 跨栈契约脚本：`scripts/check_*.py`，由 `scripts/check_contracts.sh` 运行。
