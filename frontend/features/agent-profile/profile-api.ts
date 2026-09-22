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
  AgentObservationsResponse,
  AgentRecordKind,
  ExperiencePartitionResponse,
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

/**
 * 读调用者自己的「Agent 记录」(P3-T5)——外部 Agent 经接口写下的使用线索,新到旧。
 * 服务端永远从登录身份解析归属,这里没有第二个人的记录可读。总闸关掉时后端回
 * `enabled:false` + 空列表(不是 404),与 `fetchUnderstanding` 同一口径。
 */
export function fetchAgentObservations(notebookId: string): Promise<AgentObservationsResponse> {
  return requestJson<AgentObservationsResponse>(
    `/notebooks/${notebookId}/agent-observations`,
    options,
  );
}

/**
 * 清调用者自己的 Agent 记录。省略 `agentProfileId` 清全部;传了只清那一个
 * Agent 名下的记录。返回删除的行数(界面目前不展示这个数字,重取列表本身
 * 就是最直接的确认)。
 */
export function clearAgentObservations(
  notebookId: string,
  agentProfileId?: string,
  kind?: AgentRecordKind,
): Promise<{ removed: number }> {
  // 两个收窄条件互相独立,各自缺省即「不收窄」:省略 `kind` 清两种记录(「清空
  // 全部记录」走的就是这一条),传了只清那一种。服务端对认不出的 kind 回 400 而
  // 不是静默清零行——那种静默与「本来就没有」长得一模一样。
  const params = new URLSearchParams();
  if (agentProfileId) params.set("agent_profile_id", agentProfileId);
  if (kind) params.set("kind", kind);
  const query = params.toString();
  return requestJson<{ removed: number }>(
    `/notebooks/${notebookId}/agent-observations${query ? `?${query}` : ""}`,
    { ...options, method: "DELETE" },
  );
}

/**
 * 读这个库攒下的检索经验(PR-2)。挂在同一个 `understanding` 路由家族下,复用它的
 * 读权判据——所以这里也没有第二套鉴权面要前端操心。
 *
 * 总闸关掉时后端回 `enabled:false` + 空列表(不是 404),与上面两个读端点同一口径。
 */
export function fetchExperiencePartition(
  notebookId: string,
): Promise<ExperiencePartitionResponse> {
  return requestJson<ExperiencePartitionResponse>(
    `/notebooks/${notebookId}/understanding/experiences`,
    options,
  );
}

/**
 * 手动整理一次。返回即「已经排上」,不是「已经做完」——与 `rebuildUnderstanding`
 * 同一语义,但这条链路服务端**没有**可轮询的状态字段,所以界面不轮询、也不宣布
 * 结局,只说「已经开始」。
 *
 * 两种 409 都由后端写成人话(带 `X-User-Message`,经 `errors.ts` 原样透出):
 * 总闸关掉、以及已经有一次整理在跑。
 */
export function distillExperiencePartition(
  notebookId: string,
): Promise<{ started: boolean }> {
  return requestJson<{ started: boolean }>(
    `/notebooks/${notebookId}/understanding/experiences/distill`,
    { ...options, method: "POST" },
  );
}

/**
 * 清空这个库攒下的检索经验,返回删掉的条数。
 *
 * 总闸关掉时**照样允许清空**(后端刻意如此):关开关是「从现在起不再攒」,不是
 * 「把攒过的藏起来」——与 Agent 记录那条逐字同一个道理。
 */
export function clearExperiencePartition(
  notebookId: string,
): Promise<{ removed: number }> {
  return requestJson<{ removed: number }>(
    `/notebooks/${notebookId}/understanding/experiences`,
    { ...options, method: "DELETE" },
  );
}
