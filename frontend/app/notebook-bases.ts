// 多领域基准库 —— 参考库挂载客户端(纯逻辑部分在 notebook-bases.test.mjs 里单测)。
// 自带 fetch 封装,与 notebook-tier.ts 同款,以便在 `node --test` 下免 React。
//
// ⚠ 重要:listBases/setBases/listMountable 对应的三个后端端点是 owner-only,
// 非 owner 调用会收到 404(刻意不泄露存在性)。调用方不能在打开笔记本时无条件
// 调用它们 —— 只读共享的访客会拿到 404。访客的读路径应该走
// `NotebookSummary.base_notebooks`,不要走这个模块。

import { authHeaders } from "./auth.ts";
import { throwHumanizedHttpError } from "./errors.ts";

export type NotebookRef = { id: string; name: string; tier: string };
export type MountedBase = NotebookRef & { active: boolean; inactive_reason: string };

const API_BASE =
  (typeof process !== "undefined"
    ? process.env?.NEXT_PUBLIC_API_BASE_URL
    : undefined) ?? "http://127.0.0.1:8000/api";

async function apiFetch<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(API_BASE + url, {
    headers: { "Content-Type": "application/json", ...authHeaders() },
    ...init,
  });
  if (!res.ok) await throwHumanizedHttpError(res, "bases");
  return res.json() as Promise<T>;
}

export const listBases = (notebookId: string): Promise<MountedBase[]> =>
  apiFetch(`/notebooks/${notebookId}/bases`);

export const listMountable = (notebookId: string): Promise<NotebookRef[]> =>
  apiFetch(`/notebooks/${notebookId}/mountable`);

export const setBases = (
  notebookId: string,
  baseNotebookIds: string[]
): Promise<MountedBase[]> =>
  apiFetch(`/notebooks/${notebookId}/bases`, {
    method: "PUT",
    body: JSON.stringify({ base_notebook_ids: baseNotebookIds }),
  });

// 必办 4(spec §6):删除确认弹窗要显示"N 个笔记本正在把它作为参考库"—— CASCADE
// 不可逆,用户点删除前必须看到影响面。owner-only,同 DELETE 端点的权限口径。
export const mountedByCount = (notebookId: string): Promise<{ count: number }> =>
  apiFetch(`/notebooks/${notebookId}/mounted-by-count`);

// 检索开销线性于挂载数(跨层桥是 |active nodes| × topk per participant)。不硬性
// 拦截,只在超过这个数时提示 —— 用户可能确有同时挂多个领域的正当需求。
export const MOUNT_HINT_THRESHOLD = 3;

export const mountCostHint = (count: number): string =>
  count > MOUNT_HINT_THRESHOLD
    ? `已挂 ${count} 个参考库，检索会逐个搜索它们，响应可能变慢。`
    : "";

export const groupMountable = <T extends NotebookRef>(
  list: readonly T[]
): { public: T[]; mine: T[] } => ({
  public: list.filter((n) => n.tier === "base"),
  mine: list.filter((n) => n.tier !== "base"),
});

// 最终审查 BLOCKER 1:编辑表单的参考库选择器过去只渲染 groupMountable(mountable)
// ——但 mountable_notebooks 与失效边的判定谓词(后端 MOUNT_VALID_EXPR)是同一个
// 表达式,一条失效边(被挂库降级/易转让后)永远不会出现在 mountable 里。结果是
// 那一行永远渲染不出来:用户看不到它、没法取消勾选,而 mountedIds 初始化时又把
// 它包含了进去(见 page.tsx openNotebookEditor),保存时原样发给后端,PUT 端点
// 若只认 mountable 白名单就会 400——表单在这条边恢复生效之前永久存不了。
//
// 修法:渲染 mountable ∪ mountEdges 的并集——已挂载的边(不论 active 与否)优先用
// 边自身的数据(active/inactive_reason,失效时 name 可能已被后端遮蔽,见
// notebook_store.list_mount_edges 的隐私说明);仅存在于 mountable、尚未挂载的
// 候选补全 active:true/inactive_reason:""。纯函数,不依赖 React state,可单测。
export const mergeMountCandidates = (
  mountable: readonly NotebookRef[],
  mountEdges: readonly MountedBase[]
): MountedBase[] => {
  const edgeById = new Map(mountEdges.map((edge) => [edge.id, edge] as const));
  const seen = new Set<string>();
  const merged: MountedBase[] = [];
  for (const candidate of mountable) {
    const edge = edgeById.get(candidate.id);
    merged.push(edge ?? { ...candidate, active: true, inactive_reason: "" });
    seen.add(candidate.id);
  }
  for (const edge of mountEdges) {
    if (!seen.has(edge.id)) merged.push(edge);
  }
  return merged;
};

// 提交晋升时的目标解析。晋升只能进公共知识库(tier==='base')且挂载边当前生效
// (active——死边是降级/易主后的残留,不是可用候选,呼应 list_mount_edges 的
// "失效边保留展示+置灰"语义)。三种结果:
//   none   — 0 个候选:「提交晋升」按钮应禁用,提示先挂载一个公共知识库。
//   auto   — 1 个候选:直接用它,不需要打扰用户选择。
//   choose — >1 个候选:必须弹出选择器,否则后端 target_base_id 未指定会拒绝(400)。
export type PromotionTargetResolution =
  | { kind: "none" }
  | { kind: "auto"; baseId: string }
  | { kind: "choose"; options: MountedBase[] };

export const resolvePromotionTarget = (
  bases: readonly MountedBase[]
): PromotionTargetResolution => {
  const options = bases.filter((b) => b.tier === "base" && b.active);
  if (options.length === 0) return { kind: "none" };
  if (options.length === 1) return { kind: "auto", baseId: options[0].id };
  return { kind: "choose", options };
};

// NotebookSummary.base_notebooks(owner/reader 都能看到,见文件顶部的 owner-only
// 警告)是 NotebookRef[] —— 没有 active/inactive_reason。该查询本就只回填当前
// 生效的挂载边(参与集,mounted_bases_row 走 MOUNT_VALID),天然等价于
// active=true,补上这两个字段就能喂给 resolvePromotionTarget。两处调用点
// (page.tsx 的 notebookPromotionBases/notebookBasesById)共用这一份适配,避免
// 各写各的字面量对象。
export const toMountedBases = (refs: readonly NotebookRef[]): MountedBase[] =>
  refs.map((ref) => ({ ...ref, active: true, inactive_reason: "" }));
