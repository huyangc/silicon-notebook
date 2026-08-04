// 对话可用性硬约束(纯逻辑,单元测试见 ask-availability.test.mjs)。
//
// 笔记本在**任何可用问答模式下都取不到可检索证据**时,回答只会是凭空生成——
// 此时问答输入框、发送键与快捷提问都应禁用(接线见 page.tsx)。
//
// 判据权威源在**后端**:NotebookSummary.ask_available 由后端计算(有可见来源、或任意
// chunk[含 knowhow 格子——无可见来源却可检索]、或已建 KG、或挂载参考库有 KG、或该用户
// 有 confirmed memory 任一即真)。前端不自行用代理信号推断——隐藏的 knowhow chunk、
// confirmed memory 与 base+overlay 配置前端都看不到,曾据此误禁/误放(codex PR#334)。
//
// 刻意**不**用「本地来源计数>0」做乐观快路:上传的来源要排队解析,尚未解析(或解析失败)
// 时并没有任何 chunk,拿它解禁会绕过硬约束(codex PR#334 第3轮 P1)。改为纯听后端——
// 来源解析出 chunk 后 ask_available 翻真,由来源处理轮询重拉 currentNotebook 自动解禁
// (page.tsx 的 reachedExtracted 分支);解析失败则永不解禁,正确。

import type { NotebookRef } from "./notebook-bases.ts";
import type { NotebookSummary } from "./workspace-model.ts";

/**
 * 是否禁止对话:后端判定该库无任何可检索证据(ask_available === false)。
 *
 * 字段**缺失(旧后端/版本 skew)时 fail-open 放行**——UX 守卫宁可漏禁,绝不因拿不到
 * 字段就把有内容的库整个锁死。
 */
export function isAskBlocked(notebook: NotebookSummary | null): boolean {
  return notebook?.ask_available === false;
}

/**
 * 这个笔记本**自己**(不含挂载的参考库)有没有可检索证据。
 *
 * `ask_available` 是合并后的单个布尔,分不出「有得可搜」是本地撑起来的还是参考库
 * 撑起来的。参考库现在可以按库取消勾选,取消之后还剩不剩东西,只能问后端并列暴露
 * 的 `local_evidence_available`(Knowhow 格子 / 该用户已确认的 Memory / 本地图谱
 * 这三条本地判据的或)。
 *
 * ⚠ 它与「可见来源数」不是一回事:Knowhow-only 或 confirmed-memory-only 的库可见
 * 来源恒为 0,却照常可搜。消费侧(localScopeIsEmpty)把两者取**或**,所以字段缺失
 * (旧后端 / 版本 skew)时返回 false 正好逐字回落到该字段存在之前的旧判据。
 */
export function hasLocalEvidence(notebook: NotebookSummary | null): boolean {
  return notebook?.local_evidence_available === true;
}


// --- 严格推理(深入分析 / 知识图谱)本轮取不取得到图谱 -------------------------
//
// 参考库现在可以**按库取消勾选**。本笔记本自己没有图谱、用户又取消勾选了唯一带图的
// 那个参考库时,后端的知识图谱可用性闸已经按库维度收窄、会如实答「无图」,但界面若仍
// 读**聚合**的 `base_kg_available`,就会:
//   1. 放行「深入分析 / 知识图谱」这两个这轮根本取不到图谱的模式;
//   2. 当着用户的面点名一个本轮压根不参与的参考库(「将借用参考库《X》推理」)。
// 判定因此必须落在「本次勾选集 ∩ 带图库」上,数据来自后端并列下发的分解
// `base_kg_notebook_ids`(与 `base_kg_available` 出自同一次读取的同一批行)。


/**
 * 本轮检索**真的会借用其知识图谱**的参考库 —— 勾选集 ∩ 已建图谱的库,按挂载顺序。
 *
 * 取消勾选的库、以及勾了但没建图的库都不算。
 *
 * 字段缺失(旧后端 / 版本 skew)时拿不到「哪几个带图」这份分解,退回聚合布尔:为假一个
 * 都不算,为真则按勾选集作答 —— 勾选状态是浏览器自己的,不受 skew 影响,所以退化路径
 * 仍然不会点名一个本轮不参与的库,且**只会比旧判据更保守**,不会凭空多放行一次。
 */
export function borrowedKgBases(
  notebook: NotebookSummary | null,
  selectedBaseNotebookIds: readonly string[],
): NotebookRef[] {
  const mounted = notebook?.base_notebooks ?? [];
  const selected = new Set(selectedBaseNotebookIds);
  const decomposition = notebook?.base_kg_notebook_ids;
  if (decomposition === undefined) {
    return notebook?.base_kg_available === true
      ? mounted.filter((base) => selected.has(base.id))
      : [];
  }
  const withKg = new Set(decomposition);
  return mounted.filter((base) => selected.has(base.id) && withKg.has(base.id));
}


/** 「将借用参考库「…」推理」这句话里该点名的库名 —— 只有真会被借用的那几个。 */
export function borrowedKgBaseNames(
  notebook: NotebookSummary | null,
  selectedBaseNotebookIds: readonly string[],
): string[] {
  return borrowedKgBases(notebook, selectedBaseNotebookIds).map((base) => base.name);
}


/**
 * 严格推理(深入分析 / 知识图谱)本轮能否取到图谱证据:
 * 本笔记本自己已建图谱,**或**本次勾选的参考库里有带图的。
 */
export function kgAvailableForScope(
  notebook: NotebookSummary | null,
  selectedBaseNotebookIds: readonly string[],
): boolean {
  return notebook?.kg_ready === true
    || borrowedKgBases(notebook, selectedBaseNotebookIds).length > 0;
}


/**
 * 取不到图谱,**只是因为已建图谱的参考库这次都没勾选**吗?
 *
 * 上面那道门开始按勾选集拦人之后,这一半就必须分得出来:两种情形的出路差着真金白银。
 * 「一个带图的参考库都没挂」的出路确实是为本笔记本整理一次知识图谱(整库的模型调用);
 * 「挂了、只是这次没勾」的出路是把那个勾点回来。不分开就会在用户只需点回一个复选框
 * 的时候,劝他去跑一次整库整理。
 */
export function kgBlockedByBaseScope(
  notebook: NotebookSummary | null,
  selectedBaseNotebookIds: readonly string[],
): boolean {
  if (kgAvailableForScope(notebook, selectedBaseNotebookIds)) return false;
  const mounted = new Set((notebook?.base_notebooks ?? []).map((base) => base.id));
  const decomposition = notebook?.base_kg_notebook_ids;
  if (decomposition === undefined) {
    // skew:只知道「挂载的库里有图」,不知道是哪几个。这一支只影响措辞,判不准也仅仅
    // 是少给一句更贴切的指路,不会放行或拦下任何模式。
    return notebook?.base_kg_available === true && mounted.size > 0;
  }
  return decomposition.some((id) => mounted.has(id));
}
