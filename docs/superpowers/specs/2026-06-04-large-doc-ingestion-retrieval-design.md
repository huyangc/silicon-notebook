# 大型结构化文档的稳健摄取与检索 — 设计文档（spec）

- 日期：2026-06-04
- 状态：待用户 review（review 通过后转 writing-plans 出实施计划）
- 触发：以 Cadence Innovus User Guide（2.6MB / 43,133 行的 Markdown，从 HTML 转来）为样本，评估 notebook 处理「超大型、强结构化、命令语法密集」技术手册的现状链路，发现解析、成本、检索三类问题。本 spec 把分析与已确认的方向固化为可执行设计。
- 约束（用户已确认）：单机 / 单用户为主，**坚持 SQLite + 现有依赖**（已装 `markdown-it-py 4.0.0`、`numpy 2.4.1`），**零新基建**；变更生效需对已摄取文档**重新摄取一次**（可接受）。

## 0. 目标（一句话）
让 notebook 在不引入新基建的前提下，能把一份 2.6MB 级的结构化技术手册**正确解析（代码/表格/层级不丢、噪声不进）、可控成本地建 KG（LLM 调用从 ~4330 降到百级、且失败可恢复）、亚秒级检索（去掉纯 Python O(N) 余弦）**。

## 1. 背景与根因（均为对样本文件的实测口径）

样本画像：2.6MB，43,133 行，~371k 词；标题 H1–H6 = 1 / 275 / 560 / 745 / 841 / 185（六级深嵌套）；2195 行含表格分隔符；~928 个围栏代码块；2776 行残留 `<a id="...">` 锚点；1024 处图片引用。

### 1.1 问题 A — 解析破坏结构 + 制造噪声
存在**两套互相独立的行级正则 Markdown 解析器**，且都不识别围栏代码块/表格：
- `parse_markdown`（[parsers.py:81](backend/app/services/parsers.py)）→ 供存储 / embedding / 元素检索。
- `kg/parsing.parse_elements`（[kg/parsing.py:59](backend/app/services/kg/parsing.py)）→ 供 KG 窗口化（直接重读原始 `.md`，见 [sqlite_repository.py:1110](backend/app/services/sqlite_repository.py)）。

实测 `parse_markdown` 在样本上产出 **21,171 个元素**（2,754 heading / 11,698 paragraph / 6,719 list_item），其中：
- **代码块被压平**：6,048 行代码内容被并进普通段落，换行被 `" ".join(text.split())` 压掉（[parsers.py:531](backend/app/services/parsers.py)）。
- **表格被压平**：2,195 个表格行塌成 233 个「流水账段落」，`|`/`---` 变正文噪声；KG 侧只认 HTML `<table>`/`<details>`（[kg/parsing.py:44](backend/app/services/kg/parsing.py)），管道表同样退化为段落。
- **纯锚点垃圾段落 2,365 个**（占段落 20%），全被 embedding + 存储 + 参与检索。
- **图片信息丢失**：764 个含 `![...]` 的段落只把语法当文本；KG 的 `_FIGURE` 还只认空 alt 的 `![](`（[kg/parsing.py:45](backend/app/services/kg/parsing.py)）。
- **正文丢层级**：embedding 侧正文 chunk 不带「所属标题路径」。

### 1.2 问题 B — 规模放大致成本/耗时/脆弱性失控
- **KG 抽取 ≈ 4,330 次 LLM 调用（单篇）**：窗口按 section 分组（[kg/windowing.py:38](backend/app/services/kg/windowing.py)），样本有 2,298 个 section，大量碎小节也各占一次完整调用。16 并发（[kg_ingest.py:16](backend/app/services/kg_ingest.py)）+ retries=4 下约 10–45 分钟，且**无窗口/成本上限**。
- **Embedding 全有或全无**：~2.1 万元素一次性传入 `embed_texts`（[sqlite_repository.py:1130](backend/app/services/sqlite_repository.py)），内部 10 条/批（[embedding_dashscope.py:7](backend/app/services/embedding_dashscope.py)）串行 ~2100 次调用，整体包在一个 `try/except: return`（[sqlite_repository.py:1131](backend/app/services/sqlite_repository.py)）——中途任一批失败→整篇 0 向量。

### 1.3 问题 C — 检索纯 Python 全量扫描不扩展
`cosine()` 为纯 Python 实现（[retrieval.py:141](backend/app/services/retrieval.py)）；`score_knowledge`（[retrieval.py:223](backend/app/services/retrieval.py)）/`score_elements`（[retrieval.py:299](backend/app/services/retrieval.py)）对每个对象逐个算余弦。单篇手册可产出数万 KG 节点 + 2.1 万元素向量，每次提问做 O(N×dim) 的 Python 循环 + 逐条 JSON 解析。

## 2. 用户故事（验收锚点）
- U1：上传这份 2.6MB Innovus 手册并完成摄取后，问「`set_message` 的语法和 `-severity` 取值」→ 答案能引用到**完整、未被压平的命令/参数表格**对应原文。
- U2：摄取后端日志显示 **KG 窗口数为百级（~300–400）而非 4330**；窗口数超阈值时有 WARN 但仍全量抽取。
- U3：摄取过程中某批 embedding 失败 → 其余批的向量仍落库，可增量补齐，不出现「整篇 0 向量」。
- U4：在已含数万向量的 notebook 里提问，检索（向量相似度部分）从「秒级 Python 循环」降到**亚秒**，且排序结果与旧实现一致。
- U5：检索结果元素**不含纯 `<a id>` 锚点之类噪声**。

## 3. 决策（已确认 2026-06-04，采纳推荐）
- **D1 路线 = 务实零新基建**：保持 SQLite + 现有依赖；问题 C 用 numpy 矩阵化替掉纯 Python 余弦，不引入向量库；接受对已有文档重摄取一次。
- **D2 KG 成本策略 = 全覆盖 + 高窗口安全阀**：靠「高效窗口化」把成本降一个量级后**全文抽取**；设高阈值 `kg_window_warn_threshold` 仅防失控，超出**只 WARN 不静默丢弃/截断**。
- **D3 解析统一程度 = 共享实现 + 两个薄适配器**：本期不彻底合并 `SourceElement` 与 `SourceElementQ` 两个模型（改动面大、风险高），只共享一份结构化解析；模型彻底合并列为将来项。
- **D4 图片 = 仅取 alt/caption，不做 OCR**（OCR 超本期范围）。

## 4. 设计

### 4.A 统一的结构化 Markdown 解析

**新增 `backend/app/services/structural_markdown.py`**：以 `markdown-it-py` 解析 MD（启用表格：用 `gfm-like` 预设或 `.enable("table")`），把 token 流转成块序列：

```
Block = {
  type: "heading"|"paragraph"|"list_item"|"code_block"|"table"|"image"|"blockquote",
  level: int,                # heading 1..6，其余 0
  text: str,                 # 供检索/embedding 的可读文本
  raw:  str,                 # 原始片段（代码块 verbatim、表格原结构）
  lang: str,                 # 代码块语言标签
  char_start, char_end,      # 绝对字符跨度（由 token.map 行号 + line_offsets 换算）
  line_start, line_end,
  section_path: str,         # 所属标题面包屑（遍历时维护 heading 栈）
  anchor_id: str|None,       # 命中的 <a id> 归到最近标题，不单独成元素
}
```

要点：
- **代码块整块保留**（`fence`/`code_block` token，verbatim 含换行 + lang）。
- **表格结构化**：管道表（GFM）与 HTML `<table>` 都成 `table` 块，`text` 给可读行列文本，`raw` 留原结构。
- **`<a id>` 锚点丢弃**：`html_block`/`html_inline` 里的纯锚点不成内容元素，其 id 记到最近标题的 `anchor_id`（留作将来锚点跳转/引用）。
- **图片**：取 alt/caption 作 `image` 块文本，丢弃裸 `![](…)`；不做 OCR（D4）。
- **section_path**：遍历 token 时维护标题栈，每个块带面包屑。

**两个薄适配器复用它**：
- `parse_markdown`（parsers.py）→ `SourceElement`：`element_type` 扩展 `code_block`/`table`/`image`；`section_path`、`raw`、`lang` 入 `metadata`。
- `kg/parsing.parse_elements`（kg/parsing.py）→ `SourceElementQ`：保留 `char_start/char_end`（KG 证据锚定 `text=source_file[char_start:char_end]` 不变）。

**预期效果**：纯锚点段落 2,365 → 0；代码块从「压平」→ 整块；表格不再塌成流水账；正文携带 section_path。

### 4.B KG 成本护栏 + Embedding 容错

- **B1 高效窗口化（4330 → ~300–400）**：重写 `kg/windowing.make_windows`，由「每 section 一刀」改为**按文档顺序遍历 prose 元素、贪心打包到目标大小**（默认 ~9000 字符，可配），**吸收碎小相邻小节**；仅当单个元素跨度超目标时才在其内部按 `step=n-m` 切并保留 overlap。窗口 `section_path` 取打包起点所在小节。相邻窗口按 ~m 字符（以尾部元素为单位）重叠以保上下文。
- **B2 高窗口安全阀（全覆盖、告警不丢弃）**：新增 `kg_window_warn_threshold`（默认 1200）。窗口数超阈值 → extraction_run 记 WARN + 控制台告警，但**继续全量抽取**。`KnowledgeGraph` 已有 `total_windows/failed_windows`（[kg_ingest.py:126](backend/app/services/kg_ingest.py)），落库供展示。
- **B3 Embedding 逐批容错**：重写 `_embed_source`（sqlite_repository.py），由它驱动分批（默认 100 元素/批）、**每批成功即落库**；某批异常→记录 + 跳过 + 继续；幂等跳过已嵌入元素，支持增量重跑。消除「任一批失败→整篇 0 向量」。
- **B4 硬编码旋钮入配置**（config.py，env 可调）：窗口目标 `n` / overlap `m`、`_WORKERS`、embedding 批大小、`kg_window_warn_threshold`、检索 top-N。

### 4.C numpy 矩阵化检索（零新基建）

- **新增向量相似度助手**（retrieval.py）：`sims_for(query_vec, id_to_vec) -> {id: cosine}`，把向量堆成 numpy 矩阵、行归一化，一次 `M @ q̂` 得全部余弦。
- **改 `ask()`**：一次性算出 `element_sims = sims_for(q, element_vectors)`、`knowledge_sims = sims_for(q, knowledge_vectors)`，下传给 `score_knowledge`/`score_elements`；其内部把 `cosine(query_vector, vec)` 调用替换成 `sims.get(id, 0.0)` 查表。**融合权重（0.4/0.6）、`RELEVANCE_FLOOR`、`structured_boost`、`_TYPE_WEIGHT`、evidence-max 取最大等逻辑完全保留**。
- **矩阵缓存**：按 `notebook_id` 在 repository 进程内缓存「堆叠 + 归一化矩阵 + id 顺序」，版本键 = (向量行数, `max(created_at)`)，摄取后行数/时间变化即重建。
- **保留 `cosine()`**：作回退与**奇偶校验测试**基准（numpy 路径排序须与旧实现一致）。
- 顺带：`_TOP_N`(12) / 元素 limit(8) 改为可配（默认保持现值 12/8，可按需调高），改善宽问题召回。

## 5. 数据 / 接口 / 配置变更
- **无破坏性表结构变更**：`source_elements.metadata` 增 `section_path`/`raw`/`lang` 等键（JSON，向后兼容）；`element_type` 新增枚举值 `code_block`/`table`/`image`。
- **config.py 新增**：`kg_window_target_chars`(默认 9000)、`kg_window_overlap_chars`(450)、`kg_extract_workers`(16)、`embed_batch_size`(10)、`kg_window_warn_threshold`(1200)、`retrieval_top_n`(12)、`retrieval_element_limit`(8)。
- **API**：无新端点；复用 `POST /sources/{source_id}/parse`（[routes.py:214](backend/app/api/routes.py)）作为重摄取入口。

## 6. 迁移 / 重摄取
解析与窗口化变更只对**重新摄取**的源生效。重摄取（经 `/sources/{id}/parse`）须：① 清理该 source 的旧 `source_elements` / `element_embeddings` / KG 派生对象与关系；② 重跑 parse → embed → extract；③ 幂等（可重复执行）。本期不写自动批量迁移脚本，由用户对需要的源手动触发重摄取。

## 7. 测试（TDD，贴合现有 `backend/tests/` 习惯）
- **解析单测**：代码块整块保留（含换行/lang）、管道表与 HTML 表都结构化、`<a id>` 锚点不成元素、section_path 正确挂载、图片取 alt/caption。小 fixture + 截取 Innovus 真实片段。
- **窗口化单测**：碎小相邻小节被合并；窗口数有界且远小于 section 数；char 跨度合法、overlap 正确；超阈值触发 WARN 但不截断。
- **Embedding 容错单测**：mock embedder 在第 k 批抛错 → 前 k-1 批向量已落库；重跑只补缺失。
- **检索奇偶校验单测**：同一 query 下 numpy 路径与旧 `cosine` 的 top-N 排序一致；确认实际走 numpy 矩阵路径（非逐条）。
- **特征化测试**：对 Innovus 样本断言「窗口数为百级（< warn 阈值）」「锚点噪声元素 ≈ 0」「存在 ≥1 个整块 code_block 元素」。

## 8. 范围与分期（各期独立可测）
- **Phase 1 — 结构化解析（问题 A）**：`structural_markdown.py` + 两适配器 + 解析单测。先落地，因为它同时改善 B（窗口质量）和 C（检索质量）。
- **Phase 2 — KG 成本护栏（问题 B）**：高效窗口化 + 安全阀 + embedding 逐批容错 + 配置旋钮 + 窗口化/容错单测。
- **Phase 3 — numpy 检索（问题 C）**：相似度助手 + 缓存 + 改 score_* + 奇偶校验单测。
- **Phase 4 — 重摄取打通 + 真机验证**：清理-重跑路径 + 对 Innovus 样本端到端跑通（U1–U5 验收）。

## 9. 明确不做（YAGNI）
sqlite-vec / ANN 索引、图片 OCR、直接吃 HTML 原文、后台任务队列、多用户扩展、rerank 模型、`SourceElement` 与 `SourceElementQ` 两模型彻底合并。

## 10. 文件级改动地图
| 文件 | 改动 |
|---|---|
| `services/structural_markdown.py` | **新增**：共享结构化解析（markdown-it-py） |
| `services/parsers.py` | `parse_markdown` 走共享解析、丢锚点、扩展 element_type |
| `services/kg/parsing.py` | `parse_elements` 走共享解析、保留 char 跨度 |
| `services/kg/windowing.py` | 打包式窗口（合并碎小节、目标大小、overlap） |
| `services/kg_ingest.py` | 消费已解析元素 + 窗口数 WARN 阈值 + 配置旋钮 |
| `services/sqlite_repository.py` | `_embed_source` 逐批容错；重摄取清理；检索接缓存矩阵 |
| `services/retrieval.py` | `sims_for` 助手 + 改 `score_knowledge`/`score_elements`（保留 `cosine`） |
| `services/embedding_dashscope.py` | 批大小走配置；单批失败不炸全量 |
| `core/config.py` | 新增旋钮 |
| `backend/tests/…` | 解析 / 窗口化 / 容错 / 奇偶校验 / 特征化测试 |

## 11. 风险与取舍
- **重摄取成本**：旧文档需重摄取才生效；KG 重抽仍有 LLM 成本（已被 B1 降一个量级）。
- **内存矩阵**：按 notebook 缓存的归一化矩阵随语料增长（如 5万×1024×4B≈200MB），单用户可接受；超大规模需 sqlite-vec（将来项）。
- **markdown-it-py 行→字符映射**：依赖 token `.map`（块级行范围），个别内联结构跨度可能偏粗；对元素/证据锚定足够，单测覆盖边界。
- **两套解析仍各跑一次**（未合并模型）：多一次解析开销，换取低改动风险；可观测性日志记录解析耗时以便将来决策是否合并。
