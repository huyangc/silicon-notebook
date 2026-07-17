# 面向用户的表达层（词汇表 · 枚举映射 · 错误人话）

日期：2026-07-17
状态：设计已确认，待写实现计划

## 背景

对 `frontend/app` 全部渲染字符串（22,267 行）做了一次通读，找到 98 条面向用户但不是用户视角的文案，其中 27 条 P0（普通用户天天看得见且看不懂）。

病根只有一个：**功能按「我们内部怎么实现的」命名，不按「用户想干什么」**。「严格推理」是后端 `group: "strict"`，「图谱多跳」是图算法的 hop，「投影」是 projection。用户要在这些之间做选择，就必须先懂实现——而这正是他没有、也不该有的知识。

最有力的证据是一物多名：「投影」9 种叫法、「基准库」7 种、「笔记本」9 种、「来源」9 种。原因不是谁偷懒，是**没有一份产品词汇表**，所以每处文案都在描述当时那段代码在做什么。

三个结构性根因：

1. **真源被绕开**——`ask-modes.ts` 本该是模式文案的唯一真源，但 `page.tsx` 有 5 处（3555 / 3569 / 3577 / 4563 / 4566）硬编码了「推理 / 图谱」简称。
2. **没有枚举映射层**——12+ 处形如 `<span>{item.status}</span>` 把后端枚举直接渲染给用户；4 处 `?? 原值` 兜底意味着后端每加一个枚举，UI 就自动泄漏一个英文 id。
3. **没有错误人话层**——`page.tsx:332` 把 `状态码 + statusText + detail + [request-id]` 拼给用户，界面上会出现 `403 Forbidden - admin only [req-7f3a2b]`。

## 目标

建立一个**面向用户的表达层**：机器的词在这里变成人的词，任何东西不经过它就到不了屏幕上。

**不在范围内**：内部词汇。`projection` / `tier` / `canonical` / `chunk` 在代码、AGENTS.md、architecture.md 里保持原名——AGENTS.md §134 用整段定义了 projection 的契约，那是架构术语，不是界面词。本设计只管**界面这一层**。

## 一、词汇表定稿

概念分三层，判据是「这个东西的边界到哪」：

- **笔记本** = 整个容器（原始文件 + 用户对话 + KG，KG 可有可无）
- **知识库** = 其中 KG 那部分
- **个人知识库 / 公共知识库** = 知识库的两个 tier

定稿表：

| 概念（内部名） | 要杀掉的叫法 | 定稿 |
|---|---|---|
| base tier | 基准库 / 基准语料 / Base KG / 权威参考层 / 底层库(base) / base | **公共知识库** |
| personal tier | 个人层 / 个人 / 个人 KG 节点 / 本库 | **个人知识库** |
| notebook | Notebook collection / notebook / 库 / SN / 知识库(误用时) | **笔记本** |
| source | Source Stack / Source / 源 / 篇 / 论文 | **资料**（上传/管理）· **来源**（被引用）|
| knowledge graph | KG / 图 / 建图 / 入图 / Base KG / Object 级知识图谱 / 知识关系图 | **知识图谱** |
| projection | 投影 / 投影产物 / 重建投影 / 进入知识图谱 / 参与问答检索 | **同步** |
| scale index | 向量检索索引（CSR 图 + KG/chunk ANN）/ 暴力检索 / 图谱索引 | **索引** |
| memory | Memory / Notebook Memory / 私有 Memory | **记忆** |
| promotion | 晋升 / 提升到 KG / 已进入 Base KG | 动作 **贡献到公共知识库** · 状态 **已收录** |
| edge review | 边审 / 边审查队列 / 实体关系边 | **关系审核** |
| cell | 格 / 格子 / 这一格 | **单元格** |
| row / branch | 分支 / 概念（指一行时） | **记录** |
| knowhow table | Knowhow 表 / knowhow 表 / 结构化经验表 | **经验表** |
| chunk | chunk / chunks | **段**（N 段内容） |

三处刻意保留，理由记录在案，避免日后被当成漏网：

- **「知识图谱」保留**，不改「知识网络」。用户是半导体研发，这个词在他们词典里。要杀的是 `KG` / `图` / `建图` / `入图` 这些缩写和变体。
- **「索引」保留**，不改「检索加速」。「加速」只覆盖「快」，没覆盖「全」——不建索引是又慢又漏。且「书后面的索引」是中文里现成且准确的心智模型。
- **「知识库」保留**为 Knowledge tab 名（`workspace-model.ts:249`）。按上面三层定义，那个 tab 渲染的就是 `KnowledgeBrowser`（KG 对象），名字是对的。

来源分布定稿为：**「引用来源：个人知识库 12 · 公共知识库 3」**

### 「知识库」误用的 8 处

判据：**这个操作的对象是整个容器，还是只有 KG**。

| 位置 | 现状 | 改为 |
|---|---|---|
| `workspace-model.ts:249` | `["rules", "知识库"]` | 不动（tab 名，指 KG 库，正确） |
| `page.tsx:3299` | 查看我分享出去的**知识库**及其只读成员 | 笔记本 |
| `page.tsx:4051` | 可将这个**知识库**整份拷贝到自己的空间 | 笔记本 |
| `page.tsx:4144` | 你分享出去的**知识库** | 笔记本 |
| `page.tsx:4154` | 尚未分享任何**知识库** | 笔记本 |
| `page.tsx:4155` | 在某个**知识库**里点「分享」 | 笔记本 |
| `page.tsx:3430` | 把当前**知识库**设为全局唯一的权威参考层 | 把这个笔记本设为公共知识库 |
| `page.tsx:2876` | 已设为基准库 — 该**知识库**将作为…冲突仲裁 | 已设为公共知识库 — 以后所有人提问都会优先参考它 |
| `answer-panel.tsx:369` | 此**知识库**较大且尚未建立检索索引 | 这个笔记本资料较多，建好索引后搜得更全 |

分享和拷贝的是整份容器（原始文件 + 对话 + KG），所以是笔记本；索引覆盖 chunk + KG，也是容器级。

## 二、共享核心的形状

**关键认识：词分两种，只有一种能被代码共享。**

- **散文里的词**（「这个笔记本还没同步过，会参考公共知识库来回答」）——抽不成常量，硬抽成 `TERMS.publicKb` 只会让代码更难读。这类靠**文档 + lint 守卫**管。
- **枚举对应的词**（`base` → 公共知识库）——必须抽，这正是 12 处裸值直出的根源。这类靠**共享映射模块**管。

所以共享核心不是词典，是**枚举映射 + 严格查表器**。

新建 `frontend/app/vocabulary.ts`，只装跨模块枚举：

```ts
export const TIER: Record<string, string> = { base: "公共知识库", personal: "个人知识库" };
// 另有 parse_status / element_type / evidence_level / memory status /
// memory promotion state / report status / trace step type / edge type
//
// 注意:object_type 不在此处 —— 后端已是它的真源,见 §2.1。

export function label(map: Record<string, string>, value: string, fallback: string): string {
  const hit = map[value];
  if (hit) return hit;
  if (process.env.NODE_ENV !== "production") console.error(`未映射的枚举值：${value}`);
  return fallback;   // 永远不会是 value 本身
}
```

### 2.1 object_type 是例外:真源在后端,前端那份是重复

写实现计划时挖出来的,推翻了本节初稿把 `OBJECT_TYPE` 放进 `vocabulary.ts` 的设计。

`backend/app/services/extraction_profiles.py:84-89` 已经有中文显示名,且**已经通过 API 下发给前端**(`KnowledgeTypeCount = { object_type, label, count }`、`ObjectSchema.label`),自定义类型也走同一条路:

```python
OBJECT_TYPE_LABELS: Dict[str, str] = {
    "concept": "概念 Concept", "claim": "论断 Claim",
    "formula": "公式 Formula", "procedure": "过程 Procedure",
}
```

前端 `kg-type-mark.tsx:11-24` 自己又存了一份**英文的**(`concept: "Concept"`),外加一个把未知 snake_case 转 Title Case 的兜底。两份并存 → 同一类型在不同位置显示不同,且前端那份更差。

四个约束,决定了这不是「机械改映射」:

1. **label 在迁移时被写进表**(`migrations.py:1520` 的 `INSERT ... 'builtin'`)。改常量只对全新库生效,**已部署库仍是旧 label** → 必须补迁移更新存量行。
2. **label 参与搜索匹配**(`query_store.py:505`:`if needle not in f"{label} {headline} {body}"`)。去掉英文半截 = 搜 `Concept` 不再命中 → **保留中英并排**。
3. **用户可以改内置类型的 label**(`schema_registry.py:112-131` 的 `update_object_schema` 接受 `payload.label`;`knowledge_store.py:1316` 的 `UPDATE object_schemas SET label = ?` 没有 `source='builtin'` 守卫,只有删除被挡)。迁移**不能无条件覆盖**。
4. `OBJECT_TYPE_LABELS` **未进 LLM prompt**(已核 `prompts.py` / `kg/extract.py`),故改词不影响抽取。

定稿:**保留中英并排,只对齐词**。只改两个:

| object_type | 现在 | 改为 |
|---|---|---|
| `claim` | 论断 Claim | **结论 Claim** |
| `procedure` | 过程 Procedure | **步骤 Procedure** |
| `concept` / `formula` | 概念 Concept / 公式 Formula | 不动 |

迁移必须条件式,只动仍是旧默认值的 builtin 行:

```sql
UPDATE object_schemas SET label = '结论 Claim'
 WHERE object_type = 'claim' AND label = '论断 Claim' AND source = 'builtin';
UPDATE object_schemas SET label = '步骤 Procedure'
 WHERE object_type = 'procedure' AND label = '过程 Procedure' AND source = 'builtin';
```

前端侧:**删掉** `kg-type-mark.tsx` 的 `KG_TYPE_LABELS` 与 TitleCase 兜底,改用 API 下发的 label。`kgTypeLabel()` 拿不到 API label 的调用点(如答案引用浮层只有 `object_type` 字符串),需要把 label 从 API plumb 过去,或退回一张由跨栈守卫钉住的小表——实现计划里定。

`label()` 的签名**强制**调用方传兜底词，所以「兜底即原值」这个 bug 在类型层面就写不出来。今天两种病都会被它堵死：

- `admin/usage/notebooks.ts:33` 的 `return STATUS_CN[s] ?? s`
- `kg-type-mark.tsx:18-24` 的 fallback——把未知 snake_case 转成 Title Case 英文（`evidence_tier` → `Evidence Tier`），等于后端每加一个类型就自动泄漏一个英文词

### 分层归属

- **跨模块枚举** → `vocabulary.ts`
- **功能自己的枚举**（knowhow 列类型、报告阶段）→ 留在原文件，但必须走 `label()`，共享词从 `vocabulary.ts` 导入
- **`ask-modes.ts` 保持独立**——它已是能用的 per-feature 真源，且有 `check_ask_modes_contract.py` 跨栈守卫钉着。只把 `page.tsx` 那 5 处硬编码简称收回去

AGENTS.md 加一节词汇表，并写明一句：**界面词 ≠ 内部词，`projection` / `tier` / `canonical` 在代码和架构文档里保持原名。**

## 三、错误人话层

后端加结构化 error code，**做成加法而非改动**——这套 API 不只服务前端，还通过 MCP 服务外部 agent（Claude Code / Codex 的 token），中文和改形状都会漏进机器消费方。

```
后端  {"detail": "Notebook not found"}                    ← 字符串原样不动，MCP/日志无感
  →   {"detail": "Notebook not found",
       "code": "notebook_not_found"}                      ← 新增字段

前端  按 code 查表           → 「没找到这个笔记本」
      查不到 code → 按 HTTP 码兜底 → 「服务暂时不可用，请稍后再试」
      状态码 / detail / request-id → 收进「复制诊断信息」，不进正文
```

后端 53 条 `detail=` 一条都不改措辞，只挂 code。`page.tsx:332` 那条拼装串（`${response.status} ${response.statusText}${suffix}[${requestId}]`）就此消失。

同类要收的还有：`auth.ts:45`（拿不到 detail 时 `throw new Error(\`${res.status}\`)`，导致登录框里显示孤零零一个「401」）、`memory-panel.tsx:76`（`Memory request failed (500)`）、`report-view.tsx:814/524/347`（裸 `Error.message`）。

`page.tsx:332` 的注释写着「Surface the backend's error detail instead of an opaque status line」——本意是为了好排查。**可排查性和用户友好不冲突，只是不该共用同一个字符串**：前者进「复制诊断信息」，后者进正文。

## 四、守卫

新建 `scripts/check_ui_vocabulary.py`，照 `check_ask_modes_contract.py` 的样子挂进 `scripts/check.sh`，硬失败（返回 1）。

钉两件事：

1. **黑名单词不许出现在 JSX 渲染文本里**：`KG`、`chunk`、`notebook`、`Notebook`、`投影`、`边审`、`晋升`、`基准库`、`基准语料`、`底层库`、`个人层`、`权威参考层`、`暴力检索`、`多跳`、`Memory`、`Knowhow`、`格子`
2. **label 查表不许 `?? 原值` 兜底**：正则捕捉 `LABELS[x] ?? x` / `?? stage` / `?? status` 这类形状
3. **枚举映射表须覆盖后端真实取值**：照 `check_ask_modes_contract.py` 的跨栈对照，把 `vocabulary.ts` 各表的键与后端真值集比对。PR A 的评审证明了这条的必要性——手写的 `PROMOTION_STATUS` 漏掉了 `proposed`/`under_review` 两个最常见的态，`PARSE_STATUS` 漏掉了 `metadata-only`，而前端测试无从发现（没有独立锚点，照抄自己 key 的「完整性测试」恒真）

**「知识库」不进黑名单。** tab 名就是个合法的裸「知识库」，而 lint 分不清上下文——一个分不清上下文的 lint 不该假装分得清。那 8 处误用在 PR B 里人工改、靠 review 把关。

守卫必须和它约束的修复**同一个 PR 落地**，否则 CI 会被自己弄红。

## 五、PR 切分

| PR | 内容 | 依赖 |
|---|---|---|
| **A** | `vocabulary.ts` + `label()` + 收编 15 处枚举直出、5 处原值兜底（**不含 object_type**） | 无。`label()` 签名即强制力，不需要 lint |
| **A2** | object_type 对齐（§2.1）：后端改 2 个词 + 条件式迁移更新存量 builtin 行 + 删前端重复表 | 独立成 PR——**唯一带 schema 迁移风险的一个**，隔离出来单独评审 |
| **B** | ~60 处散文按词汇表改（含「知识库」误用 8 处）+ AGENTS.md 词汇表 + `check_ui_vocabulary.py` | A（枚举侧先干净，B 只管散文） |
| **C** | 错误层：后端挂 `code` + 前端按 code 映射 + 「复制诊断信息」 | 无，可并行 |
| **D** | 文档校正：6 个文档 + `test_architecture_documentation.py` | 无，0 代码 |

A2 单拎出来的理由：其余四个 PR 全是纯展示层改动，改错了最多是文案难看；A2 要动 `_migration_N` + bump `SCHEMA_VERSION`，改错了会写坏已部署库的数据。两种风险不该混在一个评审面里。

PR D 是纯文档欠账：`README.md` / `README_zh.md` / `AGENTS.md` / `architecture.md` / `fangan_done.md` / `silicon_notebook_fangan.md` 与 `test_architecture_documentation.py::test_workspace_documentation_names_four_tabs_and_actual_toolbar_actions` 都在描述 `Ask / Knowledge / Memory / Deep Report` 四个 tab，**但代码早已渲染中文**（`workspace-model.ts:247-251` 的 `CHAT_MODES` → `page.tsx:3692`）：问答 / 知识库 / 记忆 / 深度报告。文档描述的是一个不存在的界面。

## 六、测试与验证

- **PR A**：`vocabulary.test.mjs` 覆盖 `label()` 的兜底行为（未知值返回 fallback 而非原值）；各枚举映射的键与后端枚举对齐。
- **PR A2**：迁移测试必须覆盖「**用户改过的 label 不被覆盖**」——预置一行 `label='我自己的叫法'` 的 builtin claim，跑迁移后断言它没变；再预置一行仍是 `'论断 Claim'` 的，断言它变成了 `'结论 Claim'`。迁移须**追加** `_migration_N` 并 bump `SCHEMA_VERSION`，不得塞进已封版的既有迁移——否则版本闸对已部署库短路，迁移根本不会执行。
- **PR B**：`check_ui_vocabulary.py` 自身要有对照测试（能抓到黑名单词、能抓到 `?? 原值`）。
- **PR C**：后端测试断言错误响应含 `code` 且 `detail` 字符串未变（保护 MCP 消费方）；前端测试覆盖 code→人话、未知 code→HTTP 码兜底。
- **PR D**：改完 `test_architecture_documentation.py` 必须跑通。

**执行环境**：本 worktree 无 `frontend/node_modules`，而 `npm run test / lint / build` 是 `check.sh` 的一部分。在本 worktree 跑一次 `npm install`，避免「主 checkout 改完再 patch 过来」的来回。

**行号敏感性**：`test_repository_surface_manifest.py` 按行号钉住若干文件。本设计只动 `frontend/` 与 `scripts/`，PR C 会动 `backend/app/api/`——需确认是否触发 manifest 的行位移守卫，若触发则按既有流程重生成 `EXPECTED_PATCH_DELTAS`。

## 七、不做

- 不改内部词汇（`projection` / `tier` / `canonical` 等在代码与架构文档中保持原名）
- 不改四个 tab 的中文名（已经是对的）
- 不去掉 object_type label 的英文半截（它参与搜索匹配，见 §2.1）
- 不改 `ask-modes.ts` 的 mode `id`（跨栈契约锁死；label/desc 可改）
- 不把 `vocabulary.ts` 做成装下所有词的 god-module（逆着 PR#241 拆 facade 的方向）
- 模式改名（严格推理 / 深挖推理 / 图谱多跳）**不在本设计范围**，另案处理
