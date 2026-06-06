# KG 去噪 + nb-012 删除重抽 设计

- 日期：2026-06-06
- 状态：设计已与用户确认，待写实施计划
- 适用 notebook（删除重抽动作）：`nb-012fb94249`（Analog CMOS IC Design，5 本教材）
- 关联：本设计兑现并扩展 `2026-06-06-kg-denoising-todo.md` 的方向 1/2（原计划推迟），是 `2026-06-06-kg-ingest-ask-merge-performance-repair.md` 任务 5 的落地。

## 背景

对 nb-012 触发 `rebuild_unified_kg`（master 新版有界 top-k）后实测：concept 6783→6240（再合并 543），但 **0.90 自动合并层大面积误并**——把正文里的符号(`V_DD`/`g_m1`)、实例标号(`Q1`/`Pole p8`)、图号(`Fig.5.38`)、章节标题概念(`8.4.1 Series-Shunt Feedback`)与真概念混并成"大杂烩簇"。

根因经实证有三条，本设计针对前两条（合并阈值留到重抽后再评估）：
1. **概念噪声未清就送去合并**：nb-012 至少 271 个章节号概念、234 个 ≤3 字符符号概念、29 个图号概念，外加大量 LaTeX 符号概念。
2. **窗口过滤治不了正文噪声**：spec 原窗口过滤只跳习题/索引/参考文献章节，上述噪声都在正文 body 章节里。
3. （暂不处理）0.90 自动合并阈值对短符号/技术名偏松。

## 目标 / 非目标

**目标**
- 把"去噪"做成抽取的固有能力：三层——章节窗口过滤 + 抽取后概念接受性过滤 + 抽取 prompt 收紧。
- 概念白名单可由用户通过 API 维护（保护真术语不被误删）。
- 对 nb-012 执行一次性"删除当前 KG → 复用已解析文档在新逻辑下重抽"，并带分阶段闸门、备份、回滚。

**非目标**
- 不改解析层（source_elements 复用，不重新 parse）。
- 不改合并聚类算法本身；阈值是否收紧留到重抽后据 P4 评估再定。
- 不动其他 notebook。
- formula/claim 去重、单证据剪枝等更激进项不在本轮（用户选了"双层过滤"，未选激进剪枝）。

## 架构与组件

去噪逻辑是**通用抽取代码**（对所有 notebook 未来抽取生效）；删除重抽是**仅对 nb-012** 的一次性维护编排。

### 1. `backend/app/services/kg/filters.py`（新）
两个纯函数，无 IO，可单测：

- `should_extract_window(section_path, elements, doc_type) -> (keep: bool, reason: str)`
  - `doc_type == "textbook"` 且 section 命中 `Problems/Exercises/习题/练习` → 跳过（`textbook_problem_section`）。
  - section 命中 `index/glossary/references/bibliography/索引/参考文献/术语表` → 跳过（`backmatter_section`）。
  - 窗口内"索引式行"（`词条, 页码`）占比 ≥ 0.6 → 跳过（`index_like_window`）。
- `is_noise_concept(name, whitelist: set[str]) -> (is_noise: bool, reason: str)`
  - 白名单保护优先：`_norm(name) in whitelist` → 直接判非噪声（keep）。
  - 否则按 §概念噪声规则判定。

### 2. `backend/app/services/kg_ingest.py`（改）
- 抽取前：对每个窗口过 `should_extract_window`，跳过的不送 LLM；累计 `windows_skipped`。
- 抽取后、入库前：对 LLM 返回的 concept 过 `is_noise_concept`，丢弃噪声 concept，并连带移除指向被丢节点的悬空边（复用已有的 edge grounding 过滤）；累计 `concepts_dropped`。
- extraction run message 追加：`windows_skipped=<n> concepts_dropped=<n>`。
- 抽取前加载一次白名单集合，传入过滤器（避免逐概念查库）。

### 3. `backend/app/services/kg/extract.py`（改 prompt）
在现有 "Be SELECTIVE with Concepts" 段落后追加显式负例：不要把以下当 Concept——裸符号/下标变量（V_DD、g_m1、i_b68）、实例标号（Q1、M5、Pole p8、R_E26）、图表/公式/章节引用（Fig./Table/Eq./Section）、章节标题或编号。从源头减少噪声产生；`is_noise_concept` 作为确定性兜底。

### 4. doc_type
nb-012 的 5 个源当前 `doc_type=''`。一次性 `UPDATE sources SET doc_type='textbook'`（5 本均为教材），使窗口过滤的"跳习题"生效。抽取路径读 `source.doc_type` 传入过滤器。

### 5. 概念白名单（用户可维护）
- **表** `concept_whitelist(term TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)`。`term` 存规范化形式（`_norm`：小写、折叠空白/连字符/下划线）。
- **作用域**：全局（VCO/PLL 等是通用 EE 术语，不分 notebook）。预置内置术语开箱即用；将来如需按 notebook 细分再扩展（加 `scope` 列）。
- **预置种子**：VCO、PLL、LNA、BJT、MOS、MOSFET、CMOS、FET、op amp、ADC、DAC、CMRR、PSRR 等常见会被符号规则误伤的真缩写。
- **API**（`backend/app/api/routes.py`，全局，不挂 notebook）：
  - `GET /kg/concept-whitelist` → 列出全部。
  - `POST /kg/concept-whitelist` body `{term, note?}` → 新增（规范化后写入，幂等）。
  - `DELETE /kg/concept-whitelist/{term}` → 删除（按规范化匹配）。
- **消费**：`is_noise_concept` 用它做保护覆盖；抽取时 repository 加载为 `set` 传入。

## 概念噪声判定规则（`is_noise_concept`，白名单不命中时）

**拒绝（判噪声）：**
- 图表/公式/章节引用：`^(Fig|Figure|Table|Eq|Equation|Sec|Section|§)\b`
- 章节标题概念：`^\d+(\.\d+)+`（如 "8.4.1 Series-Shunt Feedback"）
- 裸符号/下标变量：name 含 `_` 或 `^`（V_DD、g_m1、i_b68、R_E26、(W/L)_1、A_v^+）
- 实例标号：`^[A-Za-z]\d+$`（Q1、M5、p8、C2、A1）
- ≤2 字符、纯数字、纯标点

**保护（绝不丢）：**
- 白名单命中（见上）。
- 多词命名概念、含字母且无下标符号的术语（transconductance、current mirror、slew rate、channel length modulation…）天然不命中上面规则。

> 规则与白名单的最终取值以 **P1 离线验证**（在现有 7955 concept 上跑、看丢什么/误伤什么）的结果为准；上面是初版，按数据收敛。

## 删除 + 重抽编排（一次性维护脚本，分阶段带闸门）

脚本从 root 运行，**重抽期间停掉后端**（避免与 uvicorn 双写同一 SQLite），跑完重启。

- **P0 备份（硬闸门）**：导出 nb-012 的 knowledge_objects / knowledge_relations / concept_clusters / concept_merge_candidates / knowledge_embeddings / extraction_runs / unified_kg_state 到 `.local/backups/`。备份不成功不进入后续。
- **P1 过滤器离线验证（无 LLM、无写库）**：`is_noise_concept` 跑现有 7955 concept → 报告丢弃数、抽样丢弃项、抽样疑似误伤。**用户确认、规则/白名单调好 → 放行**。
- **P2 试抽 1 本（人工闸门）**：用新逻辑重抽最小的一本 CMOS Analog（3866 元素），对比新旧 concept 数与噪声率。**达标 → 放行**。
- **P3 全量**：删 nb-012 全部 KG（保留 sources + source_elements）→ 5 本复用已解析 source_elements 重抽 → 并发嵌入 → `rebuild_unified_kg`。
- **P4 评估**：concept 总数、噪声率、0.90 下合并是否仍误并；据此决定是否再收紧合并阈值（独立后续动作）。

## 验收标准
- concept 总数显著下降（目标：砍掉数百个符号/图号/章节号噪声）。
- 抽样新概念噪声率明显低于旧。
- 重抽后 `rebuild_unified_kg` 的语义合并簇不再出现"符号大杂烩 / 拓扑混并"。
- `scripts/check.sh` 与新增单测通过。

## 回滚
- P0 备份在手；P3 后若变差，从备份还原 nb-012 的全部 KG 表（删除新数据 + 回灌备份），一步可逆。

## 测试
- `backend/tests/kg/test_filters.py`：
  - `should_extract_window`：习题章节跳、索引式窗口跳、正文公式段保留。
  - `is_noise_concept`：正例必丢（V_DD、g_m1、Q1、Pole p8、Fig.5.38、"8.4.1 …"、≤2 字符）；反例必留（VCO、PLL、transconductance、current mirror、slew rate）；白名单命中覆盖符号规则（例：白名单加 "op amp" 后不被误判）。
- 白名单 API：增/查/删的最小往返测试。

## 风险与权衡
- **误删真概念**：靠 P1 离线验证 + 用户可维护白名单两道防线控制。
- **双写冲突**：重抽期间停后端，脚本单写。
- **重抽成本**：P2 先小后大，规则跑偏在 1 本上就发现，不浪费全量。
- **白名单全局**：跨域 notebook 共用一份；若未来多领域冲突，再加 scope 列细分（YAGNI，本轮不做）。
