// 对话可用性硬约束(纯逻辑,单元测试见 ask-availability.test.mjs)。
//
// 笔记本既没有自有来源、也没挂载任何参考库时,没有任何可据以回答的内容——
// 此时问答输入框、发送键与快捷提问都应禁用(接线见 page.tsx)。
//
// sourceTotal 取 page.tsx 的 notebookSourceTotal —— 与后端 counts["sources"]
// 同义:排除 memory/knowhow 合成源后的真实来源数(list_sources_page.total_count)。
// 参考库计数取 NotebookSummary.base_notebooks(owner/reader 都能看到,权威只读)。

import type { NotebookSummary } from "./workspace-model.ts";

/** 挂载的参考库数量(0..N);缺字段按 0。 */
export function mountedBaseCount(notebook: NotebookSummary | null): number {
  return notebook?.base_notebooks?.length ?? 0;
}

/**
 * 是否禁止对话:无自有来源且无挂载参考库。
 * 任一存在(有来源 或 挂了参考库)即放行——放行的判据故意宽松,
 * 参考库是否已建知识图谱不在此判定(默认模式可直接检索其来源)。
 */
export function isAskBlocked(
  notebook: NotebookSummary | null,
  sourceTotal: number,
): boolean {
  return sourceTotal <= 0 && mountedBaseCount(notebook) === 0;
}
