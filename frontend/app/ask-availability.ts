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
