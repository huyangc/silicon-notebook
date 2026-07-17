# Notebook 进出体验 + knowhow/memory 跨库转移设计规格（2026-07-17）

## 背景与目标

三个来自使用现场的 UI 逻辑问题：

1. **打开 notebook 落在空白新对话**。用户回到一个 notebook 通常是想接着上次聊，现在每次都得先点开「历史」再选一条。
2. **返回主页的入口太隐晦**。工作区里唯一的返回口是左上角一个没有文字的 `SN` 方块，用户得靠猜。
3. **knowhow 表和 memory 被锁死在单个 notebook 里**。沉淀在 A 库的表/记忆，B 库用不上，只能手工重建。

前两项是同一件事（进出 notebook 的体验），纯前端，合为 **PR A**。第三项是全栈新特性，独立为 **PR B**。

## 已确认的需求决策

| 决策点 | 结论 |
| --- | --- |
| 恢复哪条对话 | 最近一条（`updated_at DESC` 的第一条）。不做「上次打开的那条」——提问会 bump `updated_at`，两者实际重合，且无需任何持久化 |
| 零对话的库 | 保持新会话，行为不变 |
| 返回方式 | 三件一起：带文字的返回按钮 + 浏览器返回键 + 刷新回到笔记本 |
| 转移语义 | **真复制：两份独立，互不影响**。用户 2026-07-17 在「指针 / 指针+fork / 真复制」三选中明确选定真复制，指针方案作废 |
| 移动 | = 复制到目标库 + 从源库删除 |
| 转移范围 | 只在自己 owner 的 notebook 之间。排除「从别人只读分享给我的库引用走、他撤销分享也撤不掉」的越权通道 |
| 复制后的 KG | 由**目标库**跑现有的同一个门（`memory_kg_eligible`），不开任何复制专用路径。建 KG 的语义全局单一决定点 |
| 复制的 memory 溯源 | `source_answer_id` 置空（全局唯一索引所迫），真实溯源由 `memory_provenance` 承载并跟随复制 |
| Schema | **零变更**。真复制不需要链接表，移动=复制+删除，不碰 `SCHEMA_VERSION` / `schema_contract.txt` |

---

# Part A：进出 notebook 的体验

## A1 打开即恢复最近对话

### 现状

新对话是**第一次提问时由后端隐式创建**的（`ask_state_store.py:71-102` 的 `ensure_conversation`），打开 notebook 完全不碰写路径。列表 SQL（`ask_state_store.py:397-424`）已经是 `WHERE notebook_id=? AND created_by=? ORDER BY updated_at DESC`，`sessions[0]` 天然就是最近一条。

`openNotebook`（`page.tsx:1907-1956`）已经在 `:1951` 把这个列表拉到手，却在 `:1935` 主动 `setConversationId(null)`，一条都不选。

**所以这一项是纯前端读路径改动，没有后端工作。**

### 改法

1. `loadSessions`（`page.tsx:2361-2372`）目前只 `setSessions(list)` 不返回，改为返回列表。
2. 从 `openSession`（`page.tsx:2374-2410`）抽出「把某会话详情灌进 state」的内核，**不含** `:2375` 的 `++workspaceEpochRef.current`。`openSession` 和 `openNotebook` 共用这个内核。
3. `openNotebook` 在 `:1951` 之后：列表非空 → 用内核恢复 `list[0]`；列表空 → 维持现状（`conversationId === null`，UI 落在新会话卡片）。

### 为什么必须抽内核

`openSession:2375` 自增 epoch，而 `openNotebook:1952` 在其后还要校验 epoch。直接串起来 `openNotebook` 会自己撞自己的守卫。

### 白送的收益

内核里包含 `openSession:2393-2403` 已有的在途 job 重连。所以「离开时正在跑的提问，回来自动接上」免费成立，与 `in-progress-action-resilience` 的取向一致。

### 逃生口

「新对话」按钮（`startNewSession`，`page.tsx:2424`）不动，用户随时可开新的。

### 已知代价

打开 notebook 多一次 `GET /conversations/{id}`（`routes.py:1270-1277`），且 `ConversationDetail` 返回全量 turns。长会话会拖慢打开——**这一项必须实测**（notebook 打开延迟是本仓库的历史痛点）。实测超标再在此处加 turns 上限，不预先优化。

## A2 明显的返回路径

### 现状

- 工作区里唯一的返回口是 `page.tsx:3375` 的 `.notebook-home`——一个 46×46 深色圆角方块，内容就是裸文字 `SN`（`globals.css:648-657`）。没有面包屑，没有返回按钮。
- 进 notebook 后全局顶栏被 `globals.css:52-54` 整个隐藏（`.app.workspace-mode .topbar { display: none; }`），所以待确认铃铛、头像菜单、Memory 入口在笔记本里全都看不见。
- `#notebook=<id>` 这个 hash **只写不读**：`openNotebook:1953` 用 `replaceState` 写，但挂载时的还原（`page.tsx:1328-1349`）只调 `parseMemoryHash`，而它（`memory-model.ts:134-142`）只认 `#memory` 或同时带 `tab=memory` 的形式。所以刷新回不到笔记本，浏览器返回键也没用（全程 `replaceState`，无 `popstate` 监听）。

### 改法

1. `.notebook-home` 从裸 `SN` 方块换成带 `ArrowLeft` 图标 + 文字的返回控件。文案沿用仓库已有词汇——`components/PageHeader.tsx:6` 的 `← 返回主页`。
2. `openNotebook` 的 hash 写入从 `replaceState` 改 `pushState`；`showCollection`（`page.tsx:1992-2015`）保持 `replaceState` 抹掉 hash（否则返回会在两态间死循环）。
3. 加 `popstate` 监听：hash 有 `notebook=<id>` → `openNotebook(id)`；无 → `showCollection()`。
4. 挂载还原（`page.tsx:1328-1349`）扩展成能认单独的 `#notebook=<id>`。**不要改 `parseMemoryHash` 的现有返回契约**（`memory-navigation.test.mjs` 锁着它）——新增一个解析器，或让挂载逻辑在 `parseMemoryHash` 返回 null 时再试新解析器。

刷新恢复走的是同一个 `openNotebook`，所以 **A1 的恢复最近对话自动叠加**：刷新回来直接是上次那个对话。

### 布局约束

`workspace-layout.test.mjs:38-42` 锁死 `.workspace-title { max-width: min(48vw, 720px); }`。返回控件变宽会挤压 notebook 标题输入框，两者要一起调，并做视觉验证（对齐/截断，不接受粗糙堆叠）。`:116-128` 锁死 `openNotebook`/`showCollection` 体内都含 `setReconnectJob(null);`——改动别把它弄丢。

### 明确不做

**不把全局顶栏在工作区里放出来。** 顶栏 56px + 工作区头 72px = 128px，垂直空间代价太大；合并两个 header 是另一件事。「笔记本里看不见铃铛」是真问题，但不是本次的问题。

---

# Part B：knowhow 表 / memory 跨 notebook 复制与移动

## B0 语义与被否掉的方案

**最终语义：真复制。** 复制到 B 之后是完全独立的一份，A 改了 B 不变，反之亦然。

**指针方案（内容一份、多库共享）已被用户否决**，但否决前的调查结论值得留档，因为它排除了一整条路：

- **「底层只存一份 + 检索时跨库捞」在本仓库不成立。** `_retrieve_chunks`（`retrieval_candidates.py:1038`）走 `self._scale_index(notebook_id)`，**ANN 索引是按 notebook 建的**。寄居在 A 的 chunk 不在 B 的 ANN 索引里 → B 检索时静默零召回。这正是 `vector-dim-1024-decision` 记录的头号风险类型。
- 所以指针方案也必须给每个引用库各建一份影子投影，那就只剩「内容一份」这一点价值。用户权衡后选了成本更低的真复制。

## B1 零成本论证

复制**不产生任何模型调用**，这不是估计，是三条独立证据：

1. **投影器只给 chunk 生成向量，从不给 KG 节点生成向量。** `sharing_store.py:77-78` 的注释记录 `knowledge_embeddings` / `relation_embeddings` / `concept_clusters` 里从来没有 knowhow 行（投影器一行都不写），所以它们不需要 knowhow 过滤谓词；`sharing_store.py:67-72` 进一步说明 `_write_elements` 从不给 element 生成向量（only chunks get vectors），那条 knowhow 过滤是个 harmless no-op。
2. **chunk 向量靠稳定 id 重算 + 直接搬运，不重算。** `element_id` / `cell_chunk_id`（`projection.py:122-151`）是纯函数，其 docstring 明说导出就是给 `notebook_sharing.NotebookCopyService` 用的：拷贝方为重映射后的 `(row_id, column_id)` 算出投影器将来会独立算出的同一个 id，于是拷贝后 `project_table` 看到 `old_specs == new_specs`（`projection.py:379-391`），直接跳过 embedder。
3. **图片 URL 改写不影响 embed。** 投影器在切 chunk 前先跑 `textops.strip_images()`（`projection.py:347`），把 `![alt](url)` 换成基于 alt 文本的占位符（`textops.py:31-50`）。URL 根本不进被 embed 的文本，所以改写 URL 后文本仍然逐字相同。

## B2 knowhow 复制

把 `NotebookCopyService` 的作用域从「整本 notebook」缩到「单张表」。`_COPY_SNAPSHOT_QUERIES`（`sharing_store.py:31-109`）是「什么属于一个 notebook」的权威清单，单表版照它改成按 `table_id` / `hidden_source_id` 取：

| 表 | 单表取法 |
| --- | --- |
| `knowhow_tables` | `WHERE id = ?` |
| `knowhow_columns` / `knowhow_rows` | `WHERE table_id = ?` |
| `knowhow_cells` / `knowhow_cell_code` | join 下来（它们没有自己的 notebook_id） |
| `sources` | `WHERE id = <hidden_source_id>` |
| `source_elements` | `WHERE source_id = <hidden_source_id>` |
| `chunks` | `WHERE source_id = <hidden_source_id>` |
| `chunk_embeddings` | 跟随 chunks |
| `notebook_assets` | **只拷这张表实际引用的**（见下） |
| `knowledge_objects` / `knowledge_relations` / `knowledge_embeddings` | **不拷**，由 reproject 重建（与 `copy_notebook` 一致） |

id 全部重映射（沿用 `remapped_id`，保留 `-` 前缀）；chunk/element id 用稳定公式对新 `row_id` 重算并把向量放到位；然后 `project_table` 做结构化重建。

### 图片资产

图片 URL 里烤进了 notebook id——`![alt](${apiBase}/notebooks/${notebookId}/assets/${assetId})`（`knowhow-model.ts:697`），这段文本就存在 `knowhow_cells.content_md` 里。资产服务路由 `GET /notebooks/{nb}/assets/{id}`（`routes.py:454`）按 `require_notebook_read` 守卫。

**做法**：从被拷贝的 cell `content_md` 里扫出 `/notebooks/{nb}/assets/{id}` 引用，为这些资产插新的 `notebook_assets` 行（新 id、`notebook_id` = 目标库），并改写拷贝后 cell 里的 URL。

**为什么不能不拷**：不拷的话 B 的表引用 A 的资产，A 一删图就全碎；「移动」尤其荒谬（表搬走了图还在老家）。**为什么可以拷**：见 B1 第 3 点，改 URL 不触发重 embed。

## B3 memory 复制

四步，没有一步是新逻辑：

1. 拷 `memory_items` 一行：新 id、`notebook_id` = 目标库。
2. 拷 `memory_revisions` / `memory_provenance`。
3. 拷 `memory_embeddings`（`vector` BLOB 直接搬 → 零 embed）。
4. 调**现有的** `MemoryService._maybe_schedule_kg(new_item, extract_kg=True)`（`memory_service.py:136-143`）。

### KG 语义为什么不需要任何新代码

`_maybe_schedule_kg` 的门是 `self.memory_kg.memory_kg_eligible(item.notebook_id)` ——**它从 item 自己的 notebook_id 上读**。`memory_kg_eligible`（`source_ingestion.py:769-772`）= `should_extract_kg(nb) and notebook_tier(nb) != "base"`。编辑路径（`memory_service.py:661-667`）也是重跑这同一个门 + 同一个 scheduler。

所以：新 item 的 `notebook_id` 就是目标库 → 门自动按目标库判定 → 够格就走 `_kg_ingest_job` → `ingest_memory_source(...)` 自己建隐藏源并抽 KG（后台、可取消）。**建 KG 的决定点全局仍然只有一个。**

而且 `_kg_ingest_job:151` 已有 `if item.status != "confirmed": return` 守卫，所以复制一条 `candidate` 不会抽取——等用户在目标库确认时走的还是那条老路。

**隐藏源不需要拷贝**，`ingest_memory_source` 会自己建。

### 三个字段的处理

| 字段 | 处理 | 理由 |
| --- | --- | --- |
| `source_answer_id` | **置空** | `idx_memory_answer_once UNIQUE(created_by, source_answer_id)` 是**全局**的（同一用户同一答案只能有一条），跨库复制天然与它冲突。真实溯源在 `memory_provenance`，跟随复制不丢 |
| `promotion_state` | 重置为 `none` | 晋升到 base KG 是每库各自的治理状态，复制出来的这份没被提过 |
| `status` / `confirmed_by` / `confirmed_at` | 原样带走 | 同一个人确认过的同一份内容 |

### 一处不能拷的东西

`notebook_sharing.py:175-190` 把 `sources.memory_id` 置空，理由写在注释里：`copy_notebook` **不拷 memory_items 本体**，所以拷出来的 source 会指向同一个 memory_id 而撞 `idx_sources_memory_id`（`migrations.py:1195-1200`，全局部分唯一索引）。**这个约束不适用于本特性**——我们拷了本体、拿到新 memory id。（起草时曾误搬此约束，特此留档。）

### 已知成本

目标库够格时会有**一次后台 LLM 抽取**。曾考虑「把源库抽好的 KG 直接拷过去」来省掉它，**已否决**：那等于给复制开一条专用的建 KG 路径，违反「建 KG 语义全局一致」。且该抽取是后台的、可取消的、只在目标库自己开了 KG 时才发生——本身就是按需 gate，符合效率约束。

`extract_kg=True` 为固定值，不在复制 modal 里给开关：`extract_kg` 是 confirm 那一刻的一次性选择、从未被持久化，没有东西可继承；门仍由目标库决定。

## B4 移动 = 复制 + 删除

**不做 in-place 改 `notebook_id`。**

原地移动要正确更新 `knowhow_tables` / `sources` / `chunks` / `chunk_embeddings` / `knowledge_objects` / `knowledge_relations` / `notebook_assets` 七张表的 notebook_id，外加改写图片 URL（资产搬到 B 后 `/notebooks/A/assets/x` 会 404）。**漏一张就是静默的数据不一致**——比如 chunk 到了 B 而 chunk_embedding 还在 A，B 检索时按 notebook_id 拉向量矩阵，那些 chunk 没向量，静默降级。这正是 `vector-dim-1024-decision` 记录的「漏消费点静默零召回」事故类型。

复制+删除是两条已有路径的组合：复制侧的行数对账由 `validate_copy`（`sharing_store.py:402+`）的同款机制覆盖，删除侧走现有的删表路径。**零新风险面，且移动几乎不用写代码。**

**代价**：`table_id` / `row_id` 会变。外部 agent 若缓存了 row_id（`/agent/knowhow/rows/{r}`）会在移动后失效。移动是低频人工操作，接受，写入 README/AGENTS 的说明。

## B5 后端 API

```
POST /notebooks/{notebook_id}/knowhow/{table_id}/transfer
     body: {target_notebook_id: str, mode: "copy" | "move"}
     -> KnowhowTableSummary   (目标库里的新表)

POST /memories/{memory_id}/transfer
     body: {target_notebook_id: str, mode: "copy" | "move"}
     -> MemoryRecord          (目标库里的新条目)
```

**为什么 memory 必须开新端点而不是扩 PATCH**：`MemoryUpdate`（`schemas.py:125-131`）是 `model_config = ConfigDict(extra="forbid")` 且只有 `title/content_md/tags`，PATCH 传 `notebook_id` 会被 422 拒掉。

### 守卫

源库：`Depends(require_notebook_access)`——它是 `require_notebook_write` 的向后兼容别名（`deps.py:97`），owner-only，非 owner 404 不泄露存在性（`deps.py:72-82`）。

**目标库不能靠 `Depends` 守。** `require_notebook_write` 是**路径参数依赖**（签名 `(notebook_id: str, user = Depends(get_current_user))`），只认 URL 里的 `notebook_id`，而目标库在 body 里。必须在 handler/service 内**显式**调 `notebook_access_repository().user_can_access_notebook(target_notebook_id, user.id)`，不通过则同样 404。

前端只列 `access === "owner"` 的库是 UX，不是安全边界。

## B6 前端

### 目标库选择器

`GET /notebooks`（`routes.py:265-267`）返回 owner ∪ 只读成员的库，`NotebookSummary.access`（`schemas.py:363-400`）区分 `"owner"` / `"reader"`。选择器只列 `access === "owner"` 且 ≠ 当前库。

`<select>` 写法复用 `memory-panel.tsx:721-731` 的现成惯例。`page.tsx:795` 已在顶层持有 `notebooks`，给 `KnowhowPanel` 加一个 prop 传下去（挂载点 `page.tsx:5075-5082`），零新请求。

### knowhow 落点

表格视图工具栏（`KnowhowTableGrid`，`knowhow-panel.tsx:2876-2949`，整块 `canEdit` 门控），在「管理」（`:2914-2921`）旁加**一个**按钮开 modal。modal 内：目标库下拉 + 「复制过去」/「移动过去」两个动作。

- **加一个按钮而不是两个**：工具栏已有 6 个（添加行/下载模板/追加导入/管理/重建投影/删除表），再加两个就是粗糙堆叠。
- **不动表卡片**（`knowhow-panel.tsx:2758-2772`）：整张卡片是一个 `<button>`，嵌 button 是非法 HTML。

### memory 落点

卡脚 `.memory-card-actions`（`memory-panel.tsx:898-923`）在「删除」旁加按钮，开同一个 modal。

### 一并补的缺口

**全局 Memory 视图的卡片不显示所属 notebook**（卡片只有 status/origin 徽标、标题、tags、正文、来源折叠、更新时间）。不显示归属就没法有意义地做移动——用户不知道东西从哪来、移到哪去。全局视图（`scope="global"`）的卡片加 notebook 名徽标。

这不是加戏，是让本功能可用的前提。

## B7 架构约束（硬性）

1. **SQL 只能进 `backend/app/repositories/sqlite/*_store.py`。** facade（`sqlite_repository.py`）里一个 `.execute(` 都不许有——`test_repository_facade_contract.py:651` 逐字断言。
2. **服务层也不许写裸 SQL**（`projection.py:35-38` 立的规矩）。
3. facade 加 **one-hop 纯委派**；`ownership_manifest.py` 的 `SURFACE_MEMBERS` 补条目（owner 填 store 类名，consumers 填**准确的文件:行号**）；更新金文件 `backend/tests/fixtures/repository_contract/facade_surface.json`。
4. **前端纯逻辑必须抽到 `-logic.ts`**（新建 `notebook-transfer-logic.ts`）。knowhow 测试只 import `.ts`（Node 原生类型剥离不支持 `.tsx`），逻辑留在 `.tsx` 里就测不到。
5. **零 schema 变更**，不动 `SCHEMA_VERSION` / `schema_contract.txt`。

## B8 测试雷区

memory 的前端测试**读源码文本做正则断言**，三条会咬人：

| 断言 | 约束 |
| --- | --- |
| `answer-memory.test.mjs:40-46` | 把 `updateMemory`(`memory-panel.tsx:617`) 到 `submitSearch`(`:689`) 之间整段源码切出来断言。**新的转移函数必须写在 `:689` 之后** |
| `answer-memory.test.mjs:73` | `sessionSignal={memorySessionAbortRef.current.signal}` 在 page.tsx 里的出现次数**写死为 3**。不能加第 4 个 |
| `answer-memory.test.mjs:53-55` 等 | 逐字 JSX 断言，锁死属性顺序与中文文案。既有 class 名（`.memory-delete-action` 等）和文案是契约，不能顺手改名 |

`workspace-layout.test.mjs` 同理约束 Part A（见 A2「布局约束」）。

## 测试策略

**后端**

- 单表复制的行数对账（照 `validate_copy` 的机制）+ 无悬挂引用。
- **零 embedder 调用**：用 fake embedder 断言 call count == 0（含带图片的表——验证 URL 改写不触发重 embed）。
- 图片资产：复制后 URL 指向目标库的新资产 id；源库资产不受影响。
- memory：`source_answer_id` 置空、`promotion_state` 重置、向量搬运、`_maybe_schedule_kg` 被调用且门按**目标库**判定（目标库不够格 → 不调度；够格 → 调度一次）。
- 复制 `candidate` 状态的 memory → `_kg_ingest_job` 的 status 守卫拦住，不抽取。
- 守卫：目标库非 owner → 404。
- 移动：源库该表/该条不复存在，目标库存在且可检索。

**前端**

- `notebook-transfer-logic.ts` 的目标库过滤（`access === "owner"` 且排除当前库）与校验。
- Part A 的会话恢复逻辑抽到可测的 `.ts`（若涉及 epoch/列表判空的纯逻辑）。

**真机验证**

- Part A：打开一个有历史的库 → 落在最近对话；刷新 → 仍在该库该对话；浏览器返回 → 回到列表。
- Part B：复制一张带图的 knowhow 表 → 目标库里图能显示、能被检索命中；观察 `events.jsonl` 确认零 embed 事件。

## 明确不做（YAGNI）

- 指针/链接式共享（用户已否决，见 B0）。
- 全局顶栏在工作区里的显示（见 A2）。
- 跨用户转移（转移范围锁定为自己 owner 的库）。
- 复制 modal 里的 `extract_kg` 开关（见 B3）。
- 打开 notebook 时的 turns 上限（实测超标再加，见 A1）。
- 批量转移（先做单个；批量是纯 UI 增量，有需求再加）。

## 风险与已知代价

| 风险 | 缓解 |
| --- | --- |
| 打开 notebook 多一次全量 turns 请求，长会话变慢 | 实测；超标则加 turns 上限 |
| 移动改变 `table_id`/`row_id`，外部 agent 缓存的 row_id 失效 | 低频人工操作；写入文档 |
| `pushState` 往浏览器历史塞条目，返回行为变化 | 真机验证进出/刷新/返回三条路径 |
| 单表复制清单漏表（照抄 `_COPY_SNAPSHOT_QUERIES` 时遗漏） | 行数对账测试 + 无悬挂引用检查 |
| 复制大表时的单事务时长 | knowhow 表设计上限是「单表百行内」（见 knowhow 表设计规格），可接受 |
