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
