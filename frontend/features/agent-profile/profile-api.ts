/**
 * 「AI 对这个库的理解」的四个端点(P1-T6 的 API 面)。
 *
 * 面板**只**经这个模块发请求——所有 URL 拼接、方法与作用域参数收在一处,组件里
 * 不再出现第二条通往网络的路(`tests/guards/agent-profile-guard.test.mjs` 钉住)。
 * 请求本身仍走 `api-client` 的 `requestJson`:401 清 token、失败经 `errors.ts`
 * 翻成人话,这两条是全仓共用的,本特性不另开一套。
 *
 * 错误怎么上屏:后端这四个端点的中文文案全部经 `user_error()` 发出(带
 * `X-User-Message`),`errors.ts` 因此会原样透出——409「这段理解刚被更新过，请刷新
 * 后再改」「正在整理，请稍候」、422「内容过长…」都不需要前端再翻译一次。
 */
import { requestJson } from "../../app/api-client.ts";
import type {
  UnderstandingBlock,
  UnderstandingResponse,
  UnderstandingScope,
} from "./profile-model.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

/** 读全量:两档块 + 两条链的状态 + 能不能改共享那一档。 */
export function fetchUnderstanding(notebookId: string): Promise<UnderstandingResponse> {
  return requestJson<UnderstandingResponse>(
    `/notebooks/${notebookId}/understanding`,
    options,
  );
}

/**
 * 写一块。`expected_revision` 必带:服务端按它做 CAS,对不上回 409 而不是覆盖。
 * 新建一块时传读回来的 `0`。
 */
export function saveUnderstandingBlock(
  notebookId: string,
  label: string,
  body: { scope: UnderstandingScope; value: string; expected_revision: number },
): Promise<UnderstandingBlock> {
  return requestJson<UnderstandingBlock>(
    `/notebooks/${notebookId}/understanding/${label}`,
    { ...options, method: "PUT", body: JSON.stringify(body) },
  );
}

/**
 * 清空一块的内容(行与历史仍在服务端保留)。冷启动时是幂等的。
 * `expectedRevision` 是界面上**看到过**的版本号(codex R1 P2:与保存同享乐观
 * 并发)——加载后内容又被整理/他人改过时服务端回 409,而不是清掉没看过的内容。
 */
export function clearUnderstandingBlock(
  notebookId: string,
  label: string,
  scope: UnderstandingScope,
  expectedRevision: number,
): Promise<UnderstandingBlock> {
  const query =
    `scope=${encodeURIComponent(scope)}` +
    `&expected_revision=${encodeURIComponent(String(expectedRevision))}`;
  return requestJson<UnderstandingBlock>(
    `/notebooks/${notebookId}/understanding/${label}?${query}`,
    { ...options, method: "DELETE" },
  );
}

/**
 * 手动重新整理一档。返回即「已经排上」,不是「已经做完」——权威进度要回头轮询
 * `fetchUnderstanding` 的 `job` 字段(服务端刻意没有单独的状态端点)。忙碌时是 409。
 */
export function rebuildUnderstanding(
  notebookId: string,
  scope: UnderstandingScope,
): Promise<{ started: boolean }> {
  return requestJson<{ started: boolean }>(
    `/notebooks/${notebookId}/understanding/rebuild`,
    { ...options, method: "POST", body: JSON.stringify({ scope }) },
  );
}
