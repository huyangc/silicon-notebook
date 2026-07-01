# 设计：来源 / 知识库列表真·分页(替换「加载更多」)

- 日期：2026-06-30
- 范围：把 Sources(来源面板)与 Knowledge Browser(知识库标签)从「加载更多」/全量渲染改为**页码分页**;顺带解决知识库大库卡顿。
- 不在本 spec：晋升入口的额外可发现性(可选后续)、虚拟化/懒渲染(分页后不需要)。

## 背景与动机

用户诉求:**要页码分页,不要「加载更多」;来源面板也一并改。**

现状:
- **来源**:后端已分页(`PaginatedSources`:`items/total_count/offset/limit`,`GET /notebooks/{id}/sources?offset=&limit=&q=`,`routes.py:262`,`sqlite_repository.py:1553`);但前端 `loadSourcesPage`(`page.tsx:1427`)是 append,配「加载更多（N/total）」按钮(`page.tsx:2702`)。
- **知识库**:后端 `GET /knowledge?type=`(`routes.py:389`)返回**全量** `List[KnowledgeRecord]`(`list_knowledge`,`sqlite_repository.py:2708`,无分页/无 total);计数在单独的 `/knowledge-types`(`KnowledgeTypeCount.count`)。前端 `loadKnowledge`(`page.tsx:1885`)全量加载,`KnowledgeBrowser`(`page.tsx:4162`)`filtered.map()` 全量渲染 + **客户端 status 过滤** + 逐项 `LatexText`/KaTeX → 1K+ 对象多秒冻结。
- **无任何页码 UI 组件**(仅 load-more 按钮)。

## 架构

一个共享 `Pagination` 组件 + 两个列表页码化;知识库补服务端分页(含 status 服务端过滤,保证页码正确)。分页后每页仅渲染 ~50 条,DOM 与逐项 KaTeX 被页大小天然限住,卡顿消除(无需虚拟化)。

## 组件

### 1. 共享 `Pagination`(前端新建 `frontend/app/Pagination.tsx`)
- Props:`page`(0-indexed)、`pageSize`、`total`、`onPage(next)`、可选 `busy`。
- UI:**上一页** · 「第 `page+1` / `Math.max(1, ceil(total/pageSize))` 页」 · **下一页** · 跳页输入框(输入页码回车/失焦跳转,越界钳制)。首页禁用上一页、末页禁用下一页。
- 不渲染满屏页码按钮(大 N 下 2000+ 页列不下)。
- 样式进 `globals.css`(`.pagination` 对齐精致,按 UI 标准)。

### 2. 来源(仅前端)
后端不动(已分页)。
- 状态加 `sourcesPage`(0-indexed);`loadSourcesPage(nb, {page})`:`offset = page*SOURCES_PAGE_SIZE`,`setSources(page.items)`(**替换**,不再 append),`setSourcesTotal(total_count)`,`setSourcesPage(page)`。
- 渲染 `<Pagination page={sourcesPage} pageSize={SOURCES_PAGE_SIZE} total={sourcesTotal} onPage={p => loadSourcesPage(nb,{page:p})} />`;**删除「加载更多」按钮**。
- 搜索 `q` 变化 / 清空 → `page=0` 重取。新增来源后刷新回当前页(或第 1 页,取简单一致)。

### 3. 知识库(后端加分页 + 前端页码化)
- **后端**:
  - `schemas.py` 新增 `PaginatedKnowledge{items: List[KnowledgeRecord]; total_count: int; offset: int; limit: int}`。
  - `GET /notebooks/{id}/knowledge?type=&status=&offset=0&limit=50` 返回 `PaginatedKnowledge`(保持 `type` 必填;`status` 可选,默认全部;`offset/limit` 有界 `le=200`)。
  - `list_knowledge(notebook_id, object_type, status=None, offset=0, limit=50)`:**status 服务端过滤** + COUNT(该 type+status)+ LIMIT/OFFSET 一页;返回 items + total_count。
  - 兼容:若既有调用方(如别处)依赖旧 `List` 返回,评估后要么保留旧方法名+新增分页方法,要么就地改(实现前核对调用点)。
- **前端**:
  - `loadKnowledge(kind, {status, page})`:拉一页(`type&status&offset&limit`)→ 替换 `knowledge[kind]`,存 `knowledgeTotal[kind]` + `knowledgePage[kind]`。
  - `KnowledgeBrowser`:status 过滤改为**触发重取第 1 页**(非客户端 filter);底部渲染 `<Pagination>`;切 kind → 回第 1 页重取。删客户端 `filtered` 全量过滤。
  - `↑ 提交晋升` 按钮保持在条目上(分页后可用)。

## 数据流 / 边界

- 页大小统一 `SOURCES_PAGE_SIZE = 50`(来源已有)、知识库 `KNOWLEDGE_PAGE_SIZE = 50`。
- 空列表 / 单页:`Pagination` 显示「第 1 / 1 页」,上下页禁用。
- 跳页越界 → 钳到 `[0, lastPage]`。
- status/kind/q 任一变化 → 回第 1 页(避免停在越界页)。
- 删除/新增条目后:重取当前页(若当前页因删除变空且非首页,回退一页)——v1 取「重取当前页,空则回第 1 页」的简单一致行为。

## 测试

- **后端**:`list_knowledge` 分页正确(offset/limit/total、status 服务端过滤、边界 offset≥total→空)、`PaginatedKnowledge` 形状;路由参数校验(limit le=200)。
- **前端**:tsc + 现有测试;`Pagination` 组件单测(禁用态、跳页钳制、页数计算);来源「替换非追加」+ 无 load-more 按钮;知识库切 kind/status 回第 1 页且服务端过滤。
- **视觉验证**:分页控件对齐精致(show_widget/preview),两列表底部一致。

## 风险

- **status 过滤移服务端**:需确认前端不再有依赖客户端全量过滤的地方(如同页多状态混显);实现前核对 `KnowledgeBrowser` 的 status 用法。
- **list_knowledge 调用点**:改签名前 grep 所有调用者,避免破坏(如 induce/其它)。
- **计数一致性**:知识库 tab 标签的 `/knowledge-types` count 与分页 total 应一致(同 status 口径);tab 计数是「全 status」总数,分页 total 是「当前 status」——UI 上区分清楚(tab 数=该类型总量,分页数=当前过滤下的量)。

## 实施分期(可并行)

- **P1**:共享 `Pagination` 组件 + 单测(纯前端,独立文件)。**可与 P2 并行**。
- **P2**:知识库后端分页(schemas + routes + repo + 测试)。**可与 P1 并行**。
- **P3**:前端接线 —— 来源页码化(删 load-more)+ 知识库页码化(接 P2 端点 + P1 组件)。依赖 P1、P2。
