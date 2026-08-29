// 多领域基准库 —— 参考库挂载客户端(纯逻辑部分在 notebook-bases.test.mjs 里单测)。
// 通过共享 transport 发请求；模块本身不依赖 React。
//
// ⚠ 重要:listBases/setBases/listMountable 对应的三个后端端点是 owner-only,
// 非 owner 调用会收到 404(刻意不泄露存在性)。调用方不能在打开笔记本时无条件
// 调用它们 —— 只读共享的访客会拿到 404。访客的读路径应该走
// `NotebookSummary.base_notebooks`,不要走这个模块。

import { requestJson } from "./api-client.ts";

export type NotebookRef = { id: string; name: string; tier: string };
export type MountedBase = NotebookRef & { active: boolean; inactive_reason: string };

/**
 * 可挂候选。`origin` 说的是「这个候选凭什么能挂」——base(公共知识库)/ mine
 * (自己的库)/ shared(别人共享或经群组授权给我的库)。
 *
 * 可选是因为它只由 `GET .../mountable` 计算:挂载边那条查询(`/bases`)不带它,
 * 旧后端也不带。缺失时按 `tier` 退回两组分法,与本字段出现之前逐字一致。
 */
export type MountableNotebook = NotebookRef & { origin?: string };

export const listBases = (notebookId: string): Promise<MountedBase[]> =>
  requestJson(`/notebooks/${notebookId}/bases`, { tag: "bases" });

export const listMountable = (notebookId: string): Promise<MountableNotebook[]> =>
  requestJson(`/notebooks/${notebookId}/mountable`, { tag: "bases" });

// `signal`:笔记本设置的保存是多步写序列,用户中途关掉弹窗后必须能掐掉在飞的这一条
// (见 notebook-api.ts 同名参数的说明)。
export const setBases = (
  notebookId: string,
  baseNotebookIds: string[],
  signal?: AbortSignal
): Promise<MountedBase[]> =>
  requestJson(`/notebooks/${notebookId}/bases`, {
    method: "PUT",
    body: JSON.stringify({ base_notebook_ids: baseNotebookIds }),
    tag: "bases",
    signal,
  });

// 必办 4(spec §6):删除确认弹窗要显示"N 个笔记本正在把它作为参考库"—— CASCADE
// 不可逆,用户点删除前必须看到影响面。owner-only,同 DELETE 端点的权限口径。
export const mountedByCount = (notebookId: string): Promise<{ count: number }> =>
  requestJson(`/notebooks/${notebookId}/mounted-by-count`, { tag: "bases" });

// 检索开销线性于挂载数(跨层桥是 |active nodes| × topk per participant)。不硬性
// 拦截,只在超过这个数时提示 —— 用户可能确有同时挂多个领域的正当需求。
export const MOUNT_HINT_THRESHOLD = 3;

export const mountCostHint = (count: number): string =>
  count > MOUNT_HINT_THRESHOLD
    ? `已挂 ${count} 个参考库，检索会逐个搜索它们，响应可能变慢。`
    : "";


export const shouldShowBorrowedBaseHint = ({
  strict,
  kgAvailable,
  baseKgAvailable,
  kgReady,
  baseCount,
}: {
  strict: boolean;
  kgAvailable: boolean;
  baseKgAvailable: boolean;
  kgReady: boolean;
  baseCount: number;
}): boolean => (
  strict
  && kgAvailable
  && baseKgAvailable
  && !kgReady
  && baseCount > 0
);


// 挂载选择器的分组。**三组**,不是两组:群组知识共享把「读权 ⇒ 可挂载」放开之后,
// 别人 owner 的库(经只读共享或群组授权)也会出现在候选里,而前端此前只有 `tier`
// 可分——它们会被归进「我的笔记本」,一句事实错误的标签。
//
// 归属判据优先读后端给的 `origin`;缺失时才退回 `tier`。
//
// ⚠ `origin` 缺席只剩**一种**形态:候选里没有、只存在于挂载边的**失效边**(它按
// `MOUNT_VALID_EXPR` 的定义永远不会出现在 mountable 里,所以捎不到 origin)。这类边
// 里 `tier='base'` 的确实是降级了的公共知识库,但 `tier='personal'` 的恰恰是**别人
// 家的库**(共享被撤销、被挂库易主、或本笔记本自身被共享而关掉了借入边)——把它们
// 落进「我的笔记本」正是这次要修的那句事实错误的标签,而且是最容易复现的那一例。
// 所以这一支归「共享给我的」:那是它们真实的来路,而「我的库」不会因为共享变动而
// 失效(同 owner 支不依赖任何授权)。
//
// 旧后端(不发 origin)会让**全部**候选走到这里 —— 那时 `active` 恒为 true,判据
// 落在第一支/第二支上,与本字段出现之前逐字一致。
export const mountableGroupOf = (candidate: MountableNotebook & { active?: boolean }): "public" | "mine" | "shared" => {
  if (candidate.origin === "base") return "public";
  if (candidate.origin === "mine") return "mine";
  if (candidate.origin === "shared") return "shared";
  if (candidate.tier === "base") return "public";
  return candidate.active === false ? "shared" : "mine";
};

export const groupMountable = <T extends MountableNotebook & { active?: boolean }>(
  list: readonly T[]
): { public: T[]; mine: T[]; shared: T[] } => ({
  public: list.filter((n) => mountableGroupOf(n) === "public"),
  mine: list.filter((n) => mountableGroupOf(n) === "mine"),
  shared: list.filter((n) => mountableGroupOf(n) === "shared"),
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
//
// ⚠ 已挂载的候选取**边**的数据(active/inactive_reason/遮蔽过的名字才是权威),但
// `origin` 必须从候选那边捎过来:挂载边查询不计算它,直接用边就等于让每一条**已经
// 挂上的**群组共享库退回「按 tier 猜」,而那正是最常见的一种(挂了才最需要看清它
// 是借来的)。仅存在于边、不在候选里的死边没有 origin,照旧退回 tier 分法。
export const mergeMountCandidates = (
  mountable: readonly MountableNotebook[],
  mountEdges: readonly MountedBase[]
): (MountedBase & { origin?: string })[] => {
  const edgeById = new Map(mountEdges.map((edge) => [edge.id, edge] as const));
  const seen = new Set<string>();
  const merged: (MountedBase & { origin?: string })[] = [];
  for (const candidate of mountable) {
    const edge = edgeById.get(candidate.id);
    merged.push(
      edge
        ? { ...edge, origin: candidate.origin }
        : { ...candidate, active: true, inactive_reason: "" },
    );
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
