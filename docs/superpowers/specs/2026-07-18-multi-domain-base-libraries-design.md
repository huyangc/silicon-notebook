# 多领域基准库设计规格（2026-07-18）

## 背景与目标

半导体有多个子领域（模拟、物理设计、数字前端……），每个子领域应当有自己独立的权威语料。当前系统只允许**一个**全局基准库：`mark_notebook_base` 在同一事务里把其它 `tier='base'` 的库降级为 `personal`（`notebook_store.py:170-191`），检索侧则通过「找那个 `tier='base'` 的库」隐式联邦。个人笔记本从不声明自己挂在谁身上。

目标是把这套「全局隐式单例」换成「**每个笔记本显式声明挂载集合**」：

1. 多个领域各有独立的公共知识库；
2. 用户为自己的笔记本选择挂载哪些库，也可以一个都不挂；
3. 挂载对象既可以是公共知识库，也可以是自己的另一个笔记本。

### 一个关键的既有事实

检索侧其实早就是**集合形状**的。`notebook_store.py:50-86` 的 `participant_*` 族返回 `[active] + 所有 tier='base' 的库`，并且 `participant_rows` / `participant_tiers` 返回的是带 tier 的二元组。下游的 tier 标注、`AUTHORITY_FACTOR`、prompt 的「冲突以 base 为准」全都读这个逐库 tier，不读全局状态。

**所以本设计的主体是替换一个谓词**，而不是重写检索。

## 已确认的需求决策

| 决策点 | 结论 | 依据 |
| --- | --- | --- |
| 挂载数量 | **多挂 N 个** | 用户 2026-07-18 选定；一本芯片项目笔记可能同时需要模拟 + 物理设计 |
| 是否传递 | **不传递**，只看直接挂的那一跳 | 用户选定。因为可以多挂，链式没有必要；且免掉环检测 |
| 挂自己的笔记本时它算什么 tier | **固有属性：仍是 `personal`** | 用户选定。「权威」严格绑定 admin 背书 |
| 存量迁移 | **不回填**，所有库变成未挂载 | 用户选定，干净重来 |
| 上线断层 | **接受，加引导文案** | 用户选定，见 §7 |
| 挂载上限 | **不硬性限制**，超过 3 个时前端提示成本 | 用户选定 |
| 谁能发布公共知识库 | **admin only**（维持现状） | 否则 two-tier 的权威含义作废 |
| 可挂对象范围 | **公共知识库 + 自己 owner 的笔记本**；排除别人只读分享给我的库 | 见 §5 安全边界 |
| 领域实体 | **不引入**。公共知识库本身就是领域，它的名字就是领域名 | YAGNI |

---

## §1 数据模型

### 新表

```sql
CREATE TABLE IF NOT EXISTS notebook_bases (
  notebook_id      TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  base_notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  created_by TEXT REFERENCES users(id),
  PRIMARY KEY (notebook_id, base_notebook_id),
  CHECK (notebook_id != base_notebook_id)
);
CREATE INDEX IF NOT EXISTS idx_notebook_bases_base ON notebook_bases(base_notebook_id);
```

`PRIMARY KEY` 天然幂等（重复挂载无副作用），`CHECK` 拦自挂，`ON DELETE CASCADE` 双向清理悬边。

### 改列

`promotion_candidates` 加 `target_base_id TEXT NOT NULL DEFAULT ''`——挂多个公共库时晋升目标不再唯一，见 §6。

### 去约束

`NotebookStore.set_tier`（`notebook_store.py:170-191`）**删掉降级其它 base 的那条 UPDATE**（`:180`）。全局唯一性就此消失。`test_two_tier_federated.py:36` 的对应断言需反转为「设第二个 base 不会降级第一个」。

### 不碰

`notebooks.primary_domain`（`migrations.py:85`，自由文本，默认 `"Semiconductor"`）**不参与挂载判定**。它只是塞进 prompt 的一行提示词（`query_store.py:465`）。但它在编辑表单里的 label 恰好叫「领域」（`page.tsx:4525`），与新功能撞名——同 PR 把 label 改为「领域关键词」消歧，不动列名、不动语义。

### 迁移

按仓库约定**双写**：写进 `_migration_1` 的 baseline（服务全新库）**并且**新开 `_migration_20`（服务已部署库），`SCHEMA_VERSION` 由 19 bump 到 **20**。只写不改既有列的物理列序，无 `_migration_14` 那类列序陷阱。

---

## §2 解析层：一个函数收口

当前有 **4 种**互不一致的「找 base」方式，排序键分别是 `updated_at DESC` / `created_at ASC` / `created_at ASC`，且只有一个排除 active。多 base 场景下它们会各自指向不同的库。全部收口：

```
resolve_participants(notebook_id) -> [(id, tier), ...]
# 返回 [本库] + notebook_bases 里挂载的库，每项带各自真实的 tier
```

这是本设计**唯一**的语义定义点。「不传递」在这里体现为：解析只查一跳，不递归。

---

## §3 改动站点清单

| 站点 | 现状 | 改法 |
| --- | --- | --- |
| `notebook_store.py:57` `participant_ids` | `WHERE tier='base' AND id!=?` | join `notebook_bases` |
| `notebook_store.py:65` `participant_rows` | 同上 | 同上 |
| `notebook_store.py:76` `participant_tiers` | 同上 | 同上 |
| `unified_kg_store.py:632` `first_base_notebook_id` | 全局 `LIMIT 1` | 改返回**挂载集合**；社区扩展（`ask_service.py:750`、`reasoning_retrieval.py:741`）本就该跨多库 |
| `query_store.py:90` `base_notebook_info_row` | 全局 `LIMIT 1`，**不收 notebook_id** | 改成按 notebook_id 查挂载列表 |
| `knowledge_store.py:255/263/269` `any_base_has_kg` | 任一 base 有 KG | 改成**本库挂载的**任一库有 KG |
| `knowledge_store.py:465` follow_chain 起点门 | `OR n.tier='base'` | 改成本库 ∪ 挂载集合 |
| `governance_store.py:646` `first_base_notebook_row` | 晋升目标 | 见 §6 |
| `scale_artifact_runtime.py:152` + `notebook_scale.py:37` eligible | `tier=='base' or 已有索引` | 追加 `or 被任何笔记本挂载了`，见 §6 |

改完这一层，下游**免费兼容**：`retrieval_candidates.py:944/967`、`graph_retrieval.py:119/228/606`、`evidence_context.py:149/206` 等消费者只消费 `participant_*` 的返回值。

---

## §4 tier 身份语义与 UI 后果

挂自己的笔记本时它仍是 `personal`，于是：

- `graph_reason.py:379` `AUTHORITY_FACTOR` 给它 0.85，公共库给 1.0 ✔
- `follow_chain.py:48` `_TIER_FACTOR` 同理 ✔
- `prompts.py:180-187` / `:497-505` 的「冲突时 defer to base」对它不生效 ✔
- **但引用卡片会标「来自个人知识库」，用户分不清是本库还是挂的那本库** ✘

**因此徽章必须带库名**：`来自个人知识库` → `来自「模拟笔记」（个人知识库）`。`answer-panel.tsx:138-145` 已有 tier 徽章；`Citation` / `AnswerAnchor` 拿得到 notebook_id（`ask_service.py:854-884` 正是靠它反查 tier），补一个库名映射即可。

聚合分布徽章（`answer-panel.tsx:390`、`report-view.tsx:885`，现文案「来源 · 个人 N · 基准库 M」）**维持两档聚合，不逐库拆分**——挂 3 个库时拆成 4 段会把徽章撑爆，且这里的用途是「本次答案有多少来自权威层」这一个判断。逐库粒度由单条引用的徽章承载。文案随定稿词汇改为「来源 · 个人 N · 公共 M」。

---

## §5 API 与前端

### 端点

| 端点 | 说明 |
| --- | --- |
| `GET /api/notebooks/{id}/bases` | 已挂载列表 `[{id, name, tier}]` |
| `PUT /api/notebooks/{id}/bases` | **全量替换** `{base_notebook_ids: [...]}`。幂等，比单条增删简单 |
| `GET /api/notebooks/{id}/mountable` | 可挂候选 |
| `POST /api/notebooks/{id}/tier` | 保留，admin 发布公共知识库；去掉唯一性 |

`PUT` 需要对 `{id}` 的写权限；`mountable` 按当前用户过滤。

候选端点**刻意挂在 `{id}` 下**而不是 `/api/notebooks/mountable`：后者会与既有的 `/api/notebooks/{notebook_id}` 争抢路由匹配（FastAPI 按声明序，静态段必须先注册，是个易踩的坑）；挂在 `{id}` 下还顺带让后端自己排除掉本库。

### 安全边界（重要）

可挂候选 = **所有 `tier='base'` 的公共知识库** ∪ **当前用户 `created_by` 的笔记本**。

**刻意排除**别人只读分享给我的库（`notebook_members`）。理由：对方撤销分享后挂载边仍在，检索会继续读到它——这是越权通道。本仓库 2026-07-17 的 knowhow/memory 转移设计已就同一问题拍过同样的板（「只在自己 owner 的 notebook 之间」），保持一致。

即便如此，**解析时仍需实时校验**而非只信挂载时的校验：`resolve_participants` 要跳过已不满足「公共库或本人所有」的边。挂载边不是授权凭证。

两种边失效的情形，处置一致——**跳过但不删除边**：

- 被挂的笔记本转让给了别人 → 该边从此被跳过
- 被挂的公共知识库被 admin 降级为 `personal`（且不属于挂它的人）→ 该边被跳过；**若日后重新发布为公共库，挂载自动恢复**

保留边而非级联删除，是因为降级/转让往往是临时的，静默删掉别人的配置无法撤销。代价是存在「看得见但不生效」的边——因此 `GET /api/notebooks/{id}/bases` 必须为每条边返回 `active: bool` 与失效原因，前端置灰并说明，不能假装它还在工作。

公共知识库对普通用户的列表是隐藏的（README.md:24），所以 `GET /mountable` 必须独立于常规列表端点，专门放行公共库的 `id`/`name`/`tier` 三个字段。

### 响应体变更

`NotebookSummary.base_notebook_name: str` → `base_notebooks: List[{id, name, tier}]`（`schemas.py:399`）。破坏性变更，前后端同一 PR 改完。`base_kg_available: bool`（`schemas.py:396`）语义保留，改为按挂载集合计算。

### 前端落点

挂载入口放**笔记本编辑表单**（`page.tsx:4512-4530`），在「领域关键词」下方加一行「参考库」多选。

理由：挂载是每个用户的日常设置。「分析」弹窗（`page.tsx:3592-3612`）是 admin 治理动作的容器，不该混入。「设为基准库」按钮留在分析弹窗，但因为不再全局唯一，文案必须改（见下）。

选择器要求（承 UI 精致度约束）：公共知识库与「我的笔记本」分组显示、同列对齐、长名省略号截断；选满 3 个后再选时给一行成本提示（不拦截）。

### 文案

新增 UI 一律用已定稿词汇（`docs/superpowers/specs/2026-07-17-user-facing-vocabulary-design.md:38`：base tier = **公共知识库**）。挂载关系统一叫「**参考库**」。

存量「基准库」文案属于词汇整改 PR B 的范围，本设计不主动清理，**但下列三处因语义变化必须改**：

- `notebook-tier.ts:42-54` 三态：「设为/替换为/取消基准库」→ 「设为/取消**公共知识库**」。**`replace` 态整个删除**——不再存在「替换」，因为不再唯一
- `page.tsx:3033` 的 `window.confirm`「基准库全局唯一 —— 替换为…？」整段删除
- `page.tsx:3603` desc「设为全局唯一的权威参考层」→ 去掉「全局唯一」

---

## §6 治理与成本

### 发布权限

维持 admin only（`routes.py:1344-1345`）。

### 晋升目标

挂 N 个公共库时 `first_base_notebook_row` 的「取第一个」失去意义。改为：

- 挂 0 个公共库 → 「提交晋升」按钮禁用，提示先挂一个
- 挂 1 个 → 默认它
- 挂 >1 个 → 提交时由用户选，写入 `promotion_candidates.target_base_id`

`governance_store.py:699` 与 `:808` 两处写侧读该列，不再全局查。

### 成本

检索开销**线性于挂载数**：`graph_retrieval.py:375-423` 的跨层桥是 `|active nodes| × topk` **per participant**，combined 图合并同理。挂 3 个 ≈ 3× base 侧开销。按用户决定**不硬性限制**，仅在超过 3 个时前端提示。

缓存不受影响：`graph_retrieval.py:488-521` 的 combined 图版本键按各 base 的 manifest 版本组合，多一个 base 只是多一个维度，active 摄取 churn 仍不打穿缓存。

### scale 索引 eligible 扩展

eligible 追加 `or EXISTS(SELECT 1 FROM notebook_bases WHERE base_notebook_id = ?)`：「被任何笔记本挂载」本身就使一个库具备建索引资格。

**这一条的收益比本规格初稿声称的小得多，如实记录**（2026-07-19 实现期核实）。初稿写的是「挂自己的大笔记本没有 scale 索引 → 触发 `ppr_fallback_refused` 返回空 → 推理静默失效」。这个说法**是错的**：`eligible()` 早就有末行兜底 `return not notebook_copy_stats(notebook_id)["copyable"]`（`scale_artifact_runtime.py:158`，远早于本特性），任何「真正大」（不可深拷贝）的库本来就已经 eligible，与挂载状态无关；而触发 `ppr_fallback_refused` 的 `_federated_graph_is_large` 用的正是**同一个** `not copyable` 谓词。两者永远同时成立或同时不成立，那个「静默失效」的窗口不存在。

扩展后真正新增的范围窄得多：**挂载后仍小于每一个既有「大」阈值的库**（≤2000 chunks 且可深拷贝 ≤50MB/≤5000 行），且只通过手动 `/scale-index/rebuild` 路径——两条自动路径（`maybe_auto_index`、`maybe_enqueue_fold`）都在够到 `eligible()` 之前就被各自的前置条件短路了。

保留这条改动的理由不是「补洞」，而是**语义一致性**：被人当作参考库依赖的库，理应有资格建索引。代价接近零（一次走 `idx_notebook_bases_base` 的 EXISTS，且只在前两个分支都不成立时才跑）。

**口径选择：宽口径**（不过滤挂载边有效性）。决定性理由是结构性的——`eligible(notebook_id)` 的签名里根本没有「以哪个挂载方的视角」这个维度可供评估有效性，三个真实调用点都只传 `notebook_id`。次要理由：成本不对称（该建没建的代价远大于多建）、边失效本就是设计上的自愈瞬态。反向风险（失效边让被挂库永久保有资格）经代码路径核实为自损不损人：唯一真正花资源的手动端点按**被挂库当前 owner** 鉴权，挂载方伸不到手。

### 删除被挂载的库

`ON DELETE CASCADE` 会清边，但用户无感知地失去参考库。删除确认弹窗需显示「N 个笔记本正在把它作为参考库」。

---

## §7 迁移与上线断层

按决定**不回填**：迁移只建表，不写任何挂载边。`tier='base'` 的标记原样保留（那个库继续是公共知识库，只是没人挂它）。

### 断层是真实的，且是功能消失而非退化

深入分析模式（逐步推理 / 关联追溯）的可用性门是 `kg_ready || base_kg_available`（`page.tsx:1780`）。不回填后 `base_kg_available` 全变 false，**所有本库没建图的用户上线当天会看到深入分析被拦**，提示「需先为该 notebook 构建知识图谱」。

用户已明确接受，缓解措施为引导文案：

- 拦截 toast（`page.tsx:2433-2435`）改为：「深入分析需要知识图谱 —— 可在设置里挂一个参考库，或为本库构建图谱」，并给一键跳转到编辑表单的参考库那一行
- 输入框旁提示（`page.tsx:4156-4157`）「本笔记本无图，将使用底层库（base）推理」改为按实际挂载渲染：挂了就写具体库名，没挂就写引导语
- 构建按钮 title / hint（`page.tsx:3749-3759`）同步

### 关联工具

`scripts/merge_dbs.py` 的前提是「两边共享**恰好一个**公共 base library（同一个 base notebook id）」（README.md:934）。多 base 后该前提不再普遍成立。

**本设计不为它实现多 base 合并**，只做两件小事：任一侧检测到多于一个 `tier='base'` 时**明确报错退出**（而不是沿用 `--keep-base a|b` 去猜哪个是哪个），以及在 README 对应段落补一句适用范围。真正的多 base 合并留给将来真有这个需求时再做。

---

## §8 明确不做

- 领域实体表 / 领域树 / 子领域继承
- 挂载传递（链式）
- 多个公共知识库之间的优先级——保持零幅度策略：纯相关度排序，同分时 base 优先（`retrieval_candidates.py:961`），多个 base 之间无内部次序
- 存量「基准库」措辞的全量清理（属词汇整改 PR B）
- `primary_domain` 的语义变更
- `merge_dbs.py` 的多 base 支持（仅补边界报错）

---

## 测试策略

### 解析层

- 多挂：挂 2 个库，`resolve_participants` 返回 3 项且 tier 各自正确
- **不传递**：A 挂 B、B 挂 C，检索 A 时 C 不出现
- 自挂被 `CHECK` 拒绝
- 挂载别人只读分享给我的库被拒
- **实时校验**：挂载时合法、之后笔记本易主 → 解析跳过该边，且 `GET /bases` 返回 `active=false` + 原因
- **降级恢复**：公共库降为 personal → 别人的边失效；重新发布 → 边自动恢复生效（边始终未被删除）
- 撤销挂载后该库不再进入检索
- 删除被挂库 → 边级联清空

### tier 语义

- 挂自己的 personal 库，其命中标 `personal`、`AUTHORITY_FACTOR` 取 0.85
- 挂公共库，其命中标 `base`、prompt 冲突规则生效

### 唯一性移除

- 反向断言：设第二个 base **不会**降级第一个（反转 `test_two_tier_federated.py:36`）

### 治理与成本

- eligible 扩展：被挂载的 personal 库获得 scale 索引资格
- 晋升目标：0 个公共库禁用、1 个默认、>1 个必须显式传 `target_base_id`

### 迁移

- 老库升级到 20：`notebook_bases` 建出且为空，`tier='base'` 保留
- 全新库：baseline 双写生效，两条路径 schema 一致

---

## 实现约束（本仓库特有）

- **迁移双写**：新表必须同时进 `_migration_1` baseline 与新的 `_migration_20`，并 bump `SCHEMA_VERSION=20`。只写其一会让已部署库漏建表（`migrations.py:807-816` 有踩坑记录）
- **架构守卫**：新 store 方法要登记进 facade allowlist 并保持一跳委托；新增/移动 SQL 站点会移位 `callers_static` 的行号 pin，需重跑 `test_repository_surface_manifest.py` 的基线生成
- **文档守卫**：`test_architecture_documentation.py` 覆盖 README.md / README_zh.md / AGENTS.md / architecture.md。以下四处叙述会因本设计失效，必须同步改：
  - README.md:24 / README_zh.md:24 —「the only user who can mark a notebook as the base KG」仍成立，但「Base notebooks are hidden…」需补「可通过参考库选择器发现」
  - README.md:263 / README_zh.md:239 — 分析菜单三动作的描述（「基准库/个人层切换」措辞）
  - README.md:642-645 — tier row 与 `mark_notebook_base()` 的唯一性叙述
  - AGENTS.md:159 —「active notebook plus every participating base notebook」现在指挂载集合
- **前端弯引号**：`page.tsx` 中文文案里的 `“”` 是合法 JSX 文本，不得批量替换成直引号
- **前后端同 PR**：不接受后端先行、前端拖后续（见文末「实施拆分」）

## 实施拆分：两条流程，一个 PR

用户 2026-07-18 定：**拆两条实施流程，但合并进同一个 PR 提交**。

- **流程 A（后端 + 契约）**：新表 + 迁移 20 + `resolve_participants` 收口 + §3 全部站点 + eligible 扩展 + 晋升目标 + API 四端点 + `merge_dbs` 边界报错 + 文档守卫同步
- **流程 B（前端）**：编辑表单参考库多选（含 `active=false` 置灰）+ 徽章带库名 + 三处唯一性文案 + §7 引导文案

A 先行、B 紧随（B 消费 A 的契约），但**只开一个分支、一个 PR**。

这么定有三个实际好处，不只是流程偏好：

1. `NotebookSummary.base_notebook_name → base_notebooks` 是破坏性契约变更。单 PR 意味着**不存在前端未跟上的中间态**进入 master。
2. 避开 stacked PR 的 rebase 风险——本仓库合并策略是 Rebase and merge，栈式分支在基分支前进后容易出现「终态可合但逐提交重放冲突」。
3. 与既有的「前后端同 PR」约束天然一致。

代价是单个 PR 偏大，靠**提交粒度**补偿：A 与 B 各自保持可独立审阅的提交序列，不混在一个大提交里。
