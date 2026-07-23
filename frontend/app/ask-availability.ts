// 对话可用性硬约束(纯逻辑,单元测试见 ask-availability.test.mjs)。
//
// 笔记本在**任何可用问答模式下都取不到可检索证据**时,回答只会是凭空生成——
// 此时问答输入框、发送键与快捷提问都应禁用(接线见 page.tsx)。
//
// 判据权威源在**后端**:NotebookSummary.ask_available 由后端计算(有可见来源、或任意
// chunk[含 knowhow 格子——无可见来源却可检索]、或已建 KG、或挂载参考库有 KG、或该用户
// 有 confirmed memory 任一即真)。前端不再自行用代理信号推断——隐藏的 knowhow chunk、
// confirmed memory 与 base+overlay 配置前端都看不到,曾据此误禁/误放(codex PR#334 评审)。

import type { NotebookSummary } from "./workspace-model.ts";

/**
 * 是否禁止对话。以后端 ask_available 为权威,叠加一个前端乐观快路:
 *
 * - sourceTotal>0:本地已知有可见来源(notebookSourceTotal 随上传/删除即时维护,比
 *   currentNotebook 的重拉更新)——必有 chunk 证据,放行。这条快路让"刚上传第一个来源"
 *   立即解禁,不必等 currentNotebook 重新拉取 ask_available。
 * - 否则看 ask_available:显式为 false 才禁止;为 true 放行;**缺失(旧后端/版本 skew)
 *   时 fail-open 放行**——UX 守卫宁可漏禁,绝不因拿不到字段就把有内容的库整个锁死。
 */
export function isAskBlocked(
  notebook: NotebookSummary | null,
  sourceTotal: number,
): boolean {
  if (sourceTotal > 0) return false;
  return notebook?.ask_available === false;
}
