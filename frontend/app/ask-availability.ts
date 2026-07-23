// 对话可用性硬约束(纯逻辑,单元测试见 ask-availability.test.mjs)。
//
// 笔记本在**任何可用问答模式下都取不到可检索证据**时,回答只会是凭空生成——
// 此时问答输入框、发送键与快捷提问都应禁用(接线见 page.tsx)。
//
// 判据对齐后端各模式**实际的取证来源**(经 ask_service / retrieval 追踪核实):
//   - 有可见导入来源 → chunk 模式命中文档 chunk。
//   - kg_ready(有任意 knowledge_objects)→ reasoning/graph 可用;**knowhow-only
//     笔记本也为真**(格子级 KG 写入 knowledge_objects),且 knowhow 内容在 chunk
//     层恒可检索。
//   - base_kg_available(挂载参考库中有已建 KG)→ 严格模式借用、chunk overlay/PPR
//     联邦。**故意用它而非「挂了几个参考库」:无 KG 的参考库对检索零贡献**
//     (基线 chunk 只查当前笔记本,联邦路径都要 KG)。
//   - 有 memory → 三模式都会注入 confirmed memory 作证据。
// 以上任一存在即放行;全无才禁止。

import type { NotebookSummary } from "./workspace-model.ts";

/**
 * 是否禁止对话:笔记本无任何可检索证据(无可见来源、无知识图谱、无带图参考库、
 * 无 memory)。放行判据故意从宽——任一证据线索存在即可对话。
 *
 * sourceTotal 取 page.tsx 的 notebookSourceTotal:排除 memory/knowhow 合成源后的
 * 真实来源数(list_sources_page.total_count),随上传/删除即时维护,比
 * NotebookSummary.counts["sources"] 更新。
 *
 * memory 用 counts["memories"]:该计数含 candidate,而只有 confirmed 才作证据
 * (后端未下发 confirmed-only 计数)。这里**故意从宽**——宁可放行只有候选 memory
 * 的笔记本,也不硬禁真有 confirmed memory 的笔记本;false-enable(放进来得到一个
 * 弱答案)的代价远小于 false-disable(把有内容的笔记本整个锁死)。
 */
export function isAskBlocked(
  notebook: NotebookSummary | null,
  sourceTotal: number,
): boolean {
  if (sourceTotal > 0) return false;             // 有可见来源(文档 chunk)
  if (notebook?.kg_ready) return false;          // 有知识图谱(含 knowhow 格子级 KG)
  if (notebook?.base_kg_available) return false; // 挂载参考库有知识图谱(严格模式/overlay 联邦)
  if ((notebook?.counts?.memories ?? 0) > 0) return false; // 有 memory(宽松:含候选)
  return true;
}
