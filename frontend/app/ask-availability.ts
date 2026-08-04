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
 * 本地(当前笔记本自己)那一维**有没有可检索证据**:后端 ask_available 四个判据里的
 * 前三个 —— 任意 chunk(含 Knowhow 格子)、本地可用 KG、该用户在本库的已确认 Memory。
 *
 * 只用来回答「把参考库全部取消勾选之后,本地还剩不剩东西可搜」。这个问题**不能**用
 * 「可见来源数 > 0」代答:Knowhow 表和 Memory 都没有可见来源,只有 Knowhow / 只有
 * 已确认 Memory 的库来源数恒为 0,却照常可搜(codex #431 R7 P1)。
 *
 * 字段缺失(旧后端/版本 skew)时返回 false —— 消费侧(source-scope.localScopeIsEmpty)
 * 把它与可见来源数取**或**,所以 false 就是逐字回落到该字段存在之前的判据,只增不减。
 */
export function hasLocalEvidence(notebook: NotebookSummary | null): boolean {
  return notebook?.local_evidence_available === true;
}

/**
 * 严格推理(深入分析 / 知识图谱)这一组模式**这一次**有没有图可用。
 *
 * 判据不再只是 `kg_ready || base_kg_available`:`base_kg_available` 是
 * `NotebookSummary` 上的**整库聚合**字段,它回答的是「挂着的库里有没有图」,而不是
 * 「这次勾了的库里有没有图」。把参考库整库取消勾选之后仍拿它开门,就是同一次真机
 * 事故的镜像——界面按一个这次根本搜不到的图放行模式(codex #431 R8 P2)。
 *
 * 前端只收紧它**能证明**的那一种情形:一个参考库都没勾时,任何参考库的图都不参与,
 * 所以本地无图 = 这次无图。刻意到此为止,不做更激进的推断:
 *
 *  * 部分勾选(挂了多个库、恰好把带图的那个取消勾了)前端**证不出来** ——
 *    `base_kg_available` 是聚合布尔,没有逐库的图状态,而为这一格 UI 亲和度把逐库
 *    图状态搬上浏览器要新增一个 `NotebookSummary` 字段;
 *  * 那一格的**权威判据在后端**:`RetrievalService.any_base_has_kg` 现在按同一维度
 *    收窄,所以前端放行、后端判无图时,graph 模式给的是「尚未构建知识图谱…」这句
 *    可操作的话,reasoning 模式照常用 chunk/元素证据作答并如实回 `kg_required`。
 *    换言之这里放宽的代价是一句诚实的后端回答,而收紧的代价是**藏掉一个本可用的
 *    模式** —— 所以方向必须是「只在证得出来时才禁用」。
 *
 * `selectedBaseCount` 由调用方按当前挂载集与勾选状态算出(page.tsx 已有一份,
 * 工具条与回执文案共用),这里不重复推导。
 */
export function strictModeKgAvailable(
  notebook: NotebookSummary | null,
  selectedBaseCount: number,
): boolean {
  if (notebook?.kg_ready) return true;
  return !!notebook?.base_kg_available && selectedBaseCount > 0;
}
