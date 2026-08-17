// Notebook 分享与拷贝 — 分享客户端(纯逻辑,纯 helper 在 notebook-share.test.mjs
// 单测)。请求经共享 transport，模块本身不依赖 React。

import { requestJson, requestVoid } from "./api-client.ts";

// 复用 tier.ts 里的宽松形状:后端 copy 返回一个完整 NotebookSummary,这里只声明
// 我们会读到的字段,其余用索引签名兜底。
export type NotebookSummaryLike = {
  id: string;
  name: string;
  tier?: string;
  [k: string]: unknown;
};

// 分享库的规模统计(bytes/sources/chunks/nodes/edges),后端以 Dict[str,int] 下发。
export type ShareSize = {
  bytes: number;
  sources: number;
  chunks: number;
  nodes: number;
  edges: number;
};

// POST /notebooks/{id}/share 的响应。
export type ShareResponse = {
  share_token: string;
  copyable: boolean;
  size: ShareSize;
};

// GET /notebooks/{id}/share 的响应:**只读**当前状态,不铸新 token。
//
// `share_token` 为空 = 这本笔记本还没有分享链接。此时后端刻意**不算**规模统计
// (`copyable`/`size` 无意义,`size` 是空对象),所以消费方必须先判 token 再读它们。
export type ShareState = {
  share_token: string;
  copyable: boolean;
  size: Partial<ShareSize>;
};

// GET /shared/{token} 的响应。mode: "copy"(可拷贝) | "readonly"(库太大→只读共享,加入为只读成员)。
export type SharedPreview = {
  name: string;
  owner_display: string;
  source_count: number;
  node_count: number;
  edge_count: number;
  source_titles: string[];
  mode: "copy" | "readonly";
  size: ShareSize;
};

// GET /notebooks/shared-by-me 的每项:owner 的「已分享总览」。readonly 库带只读成员名单。
export type SharedByMeItem = {
  id: string;
  name: string;
  share_token: string;
  mode: "copy" | "readonly";
  /** 只在 `share_token` 非空时是真值:纯群组共享的行不算规模(空对象)。 */
  size: Partial<ShareSize>;
  members: { username: string; added_at: string }[];
  /**
   * 这本库共享给了几个**不同的**群组(群组知识共享 P1)。
   *
   * 总览的范围因此是「只读共享 ∨ 共享给群组」,与卡片上的「已分享」徽标同一个
   * 判据——只按链接取会让徽标亮着而这张总览说「尚未分享任何笔记本」。
   * `share_token` 为空而本字段非 0 的行,是只因群组共享而出现的:它没有链接可发,
   * 也不该给「取消分享」(那个动作对授权边是空操作)。
   */
  group_count: number;
};

// 读当前的分享链接状态(仅 owner)。**无副作用**——打开分享弹窗走它,不会铸出链接。
export const getShareState = (notebookId: string): Promise<ShareState> =>
  requestJson(`/notebooks/${notebookId}/share`, { tag: "share" });

// 开启分享,拿到分享 token / 是否可拷贝 / 规模(仅 owner)。幂等:已有 token 原样返回。
export const shareNotebook = (notebookId: string): Promise<ShareResponse> =>
  requestJson(`/notebooks/${notebookId}/share`, { method: "POST", tag: "share" });

// 取消分享(仅 owner,期望 204)。
export const unshareNotebook = (notebookId: string): Promise<void> =>
  requestVoid(`/notebooks/${notebookId}/share`, { method: "DELETE", tag: "share" });

// 预览分享内容(任意登录用户;错码/已撤销 → 404 → throw)。
export const previewShared = (token: string): Promise<SharedPreview> =>
  requestJson(`/shared/${token}`, { tag: "share" });

// 拷贝分享库到当前用户空间(库太大 → 409 → throw)。
export const copyShared = (token: string): Promise<NotebookSummaryLike> =>
  requestJson(`/shared/${token}/copy`, { method: "POST", tag: "share" });

// 加入大库为只读成员(小库应走拷贝 → 400 → throw)。返回该库 summary(access=reader)。
export const joinShared = (token: string): Promise<NotebookSummaryLike> =>
  requestJson(`/shared/${token}/join`, { method: "POST", tag: "share" });

// 退出只读共享(移除自己的成员身份,期望 204)。
export const leaveNotebook = (notebookId: string): Promise<void> =>
  requestVoid(`/notebooks/${notebookId}/membership`, { method: "DELETE", tag: "share" });

// owner 的「已分享总览」:所有我 owner 且 is_shared 的库(readonly 带成员名单)。
export const sharedByMe = (): Promise<SharedByMeItem[]> =>
  requestJson(`/notebooks/shared-by-me`, { tag: "share" });

// --- 纯 helper(单测) --------------------------------------------------------

// 从 `?share=shr-xxx` 取分享 token;无则 null。容错前导 `?`、多参数。
export const parseShareToken = (search: string): string | null => {
  const qs = (search ?? "").replace(/^\?/, "");
  if (!qs) return null;
  for (const pair of qs.split("&")) {
    const eq = pair.indexOf("=");
    if (eq === -1) continue;
    const key = pair.slice(0, eq);
    if (key === "share") {
      const raw = pair.slice(eq + 1);
      const value = decodeURIComponent(raw);
      return value || null;
    }
  }
  return null;
};

// 拼可复制的分享链接:${origin}/?share=${token}。
export const buildShareLink = (token: string, origin: string): string =>
  `${origin}/?share=${token}`;

// 分享模式的中文文案:readonly→「只读共享」,其余(copy)→「可拷贝」。
export const shareModeLabel = (mode: string): string =>
  mode === "readonly" ? "只读共享" : "可拷贝";

// 分享链接复制的可见反馈。失败时链接仍在紧邻按钮的只读输入框中，可手动选择。
export const shareLinkCopyToast = (copied: boolean): string =>
  copied ? "分享链接已复制" : "复制失败，请手动选择上方链接";
