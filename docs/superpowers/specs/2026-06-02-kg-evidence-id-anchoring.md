# KG evidence by element-id anchoring (shrink LLM output) — design

- 日期：2026-06-02
- 状态：已与用户确认方向 + 两个关键决策；待实现 + 评测验证
- 背景：日志显示每个 window 的 LLM 输出 ~8100 completion tokens（中位，最高 ~19800）。`evidence` 字段（逐字整句）占 name+evidence 字符量的 ~80%（中位 90 字符、p90 237、max 571，公式句更长），却**只用于定位**——抽取期 `_locate` 在原文里核验、读取期展示的是 `source_elements.text`，并非 LLM 的 evidence 字符串。即 ~80% 的输出是模型在**重打一遍我们已有的文本**。

## 决策
1. **方案 A：element-id 锚定**。窗口里的每个 source element 预先编号 `[i]`，LLM 只输出 `"ev": i`（整数标号），后端用标号映射回该 element 的精确 text/offsets。
2. **粒度：element 级**（id 指向一个 `SourceElementQ`）。与数据模型、读取期展示（element_text）1:1，最稳。
3. **边也保留 evidence**，同样用 `ev:<id>`（现在只是一个 int，几乎免费），保留 supports/defines 等关系的支撑句。

## 架构（保留 kg / product 两层，最小风险）
- `kg_ingest.extract_graph(client, raw_text, ...)` 内部 `parse_elements(raw_text)` 解析一次，按现有 `make_windows` 切窗；对每个窗口取**落在窗口内的 elements** 传给 `extract_window`。
- `extract.py::extract_window(client, elements: List[SourceElementQ], section_path, doc_type)`：
  - prompt 文本 = `"\n".join(f"[{i}] {e.text}")`；新 `_prompt` 要求节点/边输出 `"ev": <int 标号>`（不再要求逐字 evidence）。
  - 解析：`ev` → `elements[ev]` → `Evidence(file, char_start=e.char_start, char_end=e.char_end, line_start/end, quote=e.text)`。**quote 现在是某个真实 element 的逐字全文**。
  - 兜底：`ev` 越界/缺失 → 在窗口 elements 里按 node `name` 子串搜索定位；都失败才丢弃该节点（沿用现有"无证据即丢"语义）。
- `kg_ingest.build_records` 基本不变：quote 现在是真实 element 全文 → `_bind_quote` 走精确子串命中（保留 token-overlap≥0.6 兜底），绑定到 product `source_elements` 更确定。
- **读取期 / 前端 / 现有数据：完全不变**（Evidence 结构与 element_id 绑定不变；display 仍走 `source_elements.text`）。无迁移。

## 收益与风险
- **收益**：每节点/边的 evidence 从 ~90+ 字符整句降为 1 个 int → 预计输出 token 降 ~40–60%，解码加速接近 1.5–2×；kg 层定位变确定（删掉 `_locate` 的脆弱匹配）。
- **代价**：输入端多了 `[i]` 标号（输入便宜且可缓存，输出主导延迟，净赚）。
- **风险**：模型可能选错/越界标号 → 兜底按 name 本地定位；element 级比单句粗 → 用户已确认可接受（展示本就是 element 级）。
- **可能影响抽取精度**：逐字 evidence 也有轻微 grounding/CoT 作用。**必须用评测验证**：同一真实文档跑 旧 prompt vs 新 prompt，比较 node/edge 数、grounding 命中率、输出 token、抽样核对 evidence 正确性；F1 不退化才合入。

## 非目标
- 不改读取期/前端/检索。
- 不做 sentence 级（留作后续）。
- 不强制对存量文档重抽（用户按需重传）。
