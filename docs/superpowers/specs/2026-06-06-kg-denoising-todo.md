# KG 减噪专项（TODO，后续单独做）

- 日期：2026-06-06
- 状态：待排期（从 `2026-06-06-kg-ingest-ask-merge-performance-repair.md` 的任务 5 拆出，本轮不做）
- 背景：`Analog CMOS IC Design`（5 本教材）抽出 ~29,782 个 KG 对象、7,955 个 concept，其中相当一部分是低价值/噪声概念（习题号、索引词条、参考文献），导致跨文档合并时近义词爆炸、人工 review 不现实。

## 方向 1：抽取期窗口过滤（原 plan 任务 5，已推迟）

在 `kg_ingest.extract_graph()` 提交 LLM 抽取前，按窗口的 `section_path` / 元素特征跳过低价值窗口：

- 新建 `backend/app/services/kg/filters.py`，`should_extract_window(section_path, elements, doc_type) -> (keep, reason)`：
  - `doc_type == "textbook"` 且 section 命中 `Problems/Exercises/习题/练习` → 跳过（`textbook_problem_section`）。
  - section 命中 `index/glossary/references/bibliography/索引/参考文献/术语表` → 跳过（`backmatter_section`）。
  - 窗口内"索引式行"（`词条, 页码`）占比 ≥ 0.6 → 跳过（`index_like_window`）。
- 在 extraction run message 记录 `windows_skipped=<n>`。
- 前置依赖：parse 后把自动识别的 `doc_type`（textbook）持久化到 `sources.doc_type`，`_run_extraction` 才能拿到正确 doc_type。
- 只影响**未来**的 extraction / re-extraction，不动已有 KG 对象。

## 方向 2：抽取后概念剪枝（候选）

- 出现次数极低（如仅 1 处 evidence）且非 Formula/Procedure 的 concept，标低优先或折叠。
- 通用词/停用术语黑名单（training/inference/buffer/latency… 已在抽取 prompt 里弱化，可再加后处理）。

## 方向 3：合并治理（本轮已做，见 merge-review）

- 确定性别名归一化 + 有界向量 top-k 候选 + LLM 小批量预审 + 高置信自动确认。
- 减噪与合并互补：先减噪（少产生噪声概念）、再合并（把近义词归并）。

## 验收设想

- 对教材重抽后，concept 数显著下降、低价值概念占比下降；pending merge 候选规模可人工 review。
