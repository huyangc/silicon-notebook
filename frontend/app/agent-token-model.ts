import { humanizedError } from "./errors.ts";

/** 权限五档(后端存储与对外的唯一词汇)。`ownerOnly` 的档位只对主人拥有的笔记本生效。
 *  调用记录里细粒度的 capability 串走 profile-model 自己的标签,与这里无关。 */
export const AGENT_SCOPE_OPTIONS = [
  {
    value: "read",
    label: "读取",
    description: "读取笔记本里的资料，以及你的私人记忆和 AI 对这个库的理解",
    ownerOnly: false,
  },
  {
    value: "ask",
    label: "问答",
    description: "向笔记本提问，拿到带引用的回答",
    ownerOnly: false,
  },
  {
    value: "contribute",
    label: "提交",
    description: "提交待你确认的记忆候选，写入 Knowhow 代码附件和使用线索",
    ownerOnly: false,
  },
  {
    value: "manage",
    label: "管理",
    description: "添加、重新解析来源，触发图谱与索引构建",
    ownerOnly: true,
  },
  {
    value: "delete",
    label: "删除",
    description: "删除 Agent 自己添加的来源，删除后不能恢复",
    ownerOnly: true,
  },
] as const;

export const AGENT_ACCESS_PAGE_SIZE = 25;

export type AgentScope = (typeof AGENT_SCOPE_OPTIONS)[number]["value"];

export type AgentTokenDraft = {
  default_notebook_id: string;
  notebook_ids: string[];
  scopes: string[];
  expires_at: string;
};

export function agentTokenDraft(defaultNotebookId = ""): AgentTokenDraft {
  return {
    default_notebook_id: defaultNotebookId,
    notebook_ids: defaultNotebookId ? [defaultNotebookId] : [],
    scopes: ["read"],
    expires_at: "",
  };
}

export function localDateTimeToUtcIso(
  value: string,
  timezoneOffsetMinutes?: number,
): string {
  // 这两句是写给用户的场景文案(不是诊断串),所以走 humanizedError 盖章:
  // 裸 new Error 的话,agent-access-manager 的 catch 过 toUserMessage 时认不出它已经
  // 安全化,会压成通用兜底,用户就不知道是「过期时间」这一栏填错了。
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/.exec(value);
  if (!match) throw humanizedError("过期时间格式无效");
  if (timezoneOffsetMinutes === undefined) {
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) throw humanizedError("过期时间格式无效");
    return parsed.toISOString();
  }
  const [, year, month, day, hour, minute, second = "0"] = match;
  const utcMillis = Date.UTC(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
  ) + timezoneOffsetMinutes * 60_000;
  return new Date(utcMillis).toISOString();
}

function agentTokenAccessFields(draft: AgentTokenDraft) {
  const notebookIds = Array.from(new Set([
    draft.default_notebook_id,
    ...draft.notebook_ids,
  ].filter(Boolean)));
  return {
    scopes: Array.from(new Set(draft.scopes)),
    default_notebook_id: draft.default_notebook_id,
    notebook_ids: notebookIds,
    expires_at: draft.expires_at ? localDateTimeToUtcIso(draft.expires_at) : null,
  };
}

export function agentTokenRequest(profileId: string, draft: AgentTokenDraft) {
  return { agent_profile_id: profileId, ...agentTokenAccessFields(draft) };
}

export function canIssueAgentToken(
  profileId: string,
  draft: AgentTokenDraft,
  ownedNotebookIds?: ReadonlySet<string>,
): boolean {
  return Boolean(profileId && isCompleteAgentTokenDraft(draft, ownedNotebookIds));
}

function isCompleteAgentTokenDraft(
  draft: AgentTokenDraft,
  ownedNotebookIds?: ReadonlySet<string>,
): boolean {
  return Boolean(
    draft.default_notebook_id
    && draft.notebook_ids.includes(draft.default_notebook_id)
    && draft.scopes.length
    && draft.expires_at
    && !(ownedNotebookIds && ownerTiersUnusable(draft, ownedNotebookIds)),
  );
}

/** 档位是否只对笔记本主人生效(管理 / 删除)。 */
export function isOwnerOnlyScope(scope: string): boolean {
  return AGENT_SCOPE_OPTIONS.some((option) => option.value === scope && option.ownerOnly);
}

/** 白名单里有没有用户自己拥有的笔记本。管理 / 删除只对这类笔记本生效,
 *  没有时服务端会以 422 拒绝,界面在提交前就拦下。 */
export function whitelistHasOwnedNotebook(
  draft: AgentTokenDraft,
  ownedNotebookIds: ReadonlySet<string>,
): boolean {
  return [draft.default_notebook_id, ...draft.notebook_ids].some((id) => id && ownedNotebookIds.has(id));
}

/** 草稿勾了管理 / 删除,但白名单里没有自己拥有的笔记本。 */
export function ownerTiersUnusable(
  draft: AgentTokenDraft,
  ownedNotebookIds: ReadonlySet<string>,
): boolean {
  return draft.scopes.some(isOwnerOnlyScope) && !whitelistHasOwnedNotebook(draft, ownedNotebookIds);
}

/** 全选 / 取消全选权限:被禁用的档位不参与。已勾选但被禁用的档位保留(用户应单独取消它)。 */
export function setAllScopes(
  current: readonly string[],
  selectable: readonly string[],
  checked: boolean,
): string[] {
  if (checked) return Array.from(new Set([...current, ...selectable]));
  const drop = new Set(selectable);
  return current.filter((scope) => !drop.has(scope));
}

/** 全选 / 取消全选白名单,只作用于当前可见项。默认笔记本不可取消。 */
export function setAllNotebooks(
  current: readonly string[],
  visibleIds: readonly string[],
  defaultNotebookId: string,
  checked: boolean,
): string[] {
  if (checked) return Array.from(new Set([...current, ...visibleIds]));
  const drop = new Set(visibleIds);
  return current.filter((id) => id === defaultNotebookId || !drop.has(id));
}

export const AGENT_EXPIRY_PRESET_DAYS = [7, 30, 90] as const;

function localDateString(date: Date): string {
  const shifted = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return shifted.toISOString().slice(0, 10);
}

/** 现在起 N 天后的本地墙钟值(datetime-local,精确到分钟)。 */
export function expiryAfterDays(days: number, now: Date = new Date()): string {
  const target = new Date(now.getTime() + days * 24 * 60 * 60 * 1000);
  target.setMinutes(target.getMinutes() - target.getTimezoneOffset());
  return target.toISOString().slice(0, 16);
}

/** 当前过期时间命中哪个快捷项(按日期比,页面开着几分钟也不会失配);都不命中返回 null。 */
export function matchedExpiryPreset(value: string, now: Date = new Date()): number | null {
  if (!value) return null;
  const day = value.slice(0, 10);
  const hit = AGENT_EXPIRY_PRESET_DAYS.find((days) => day === localDateString(
    new Date(now.getTime() + days * 24 * 60 * 60 * 1000),
  ));
  return hit ?? null;
}

export type AgentTokenStatus = "active" | "expired" | "revoked";

export function agentTokenStatus(
  token: { revoked_at?: string | null; expires_at?: string | null },
  now: number = Date.now(),
): AgentTokenStatus {
  if (token.revoked_at) return "revoked";
  if (token.expires_at) {
    const expiresAt = Date.parse(token.expires_at);
    if (Number.isFinite(expiresAt) && expiresAt <= now) return "expired";
  }
  return "active";
}

export const AGENT_TOKEN_STATUS_LABELS: Record<AgentTokenStatus, string> = {
  active: "有效",
  expired: "已过期",
  revoked: "已撤销",
};

export const AGENT_HIDE_INACTIVE_KEY = "agent-access:hide-inactive";

/** 白名单笔记本名称摘要:超过 3 个折叠为「等 N 个」。 */
export function allowedNotebookSummary(
  names: readonly string[],
  expanded: boolean,
): { text: string; folded: boolean } {
  if (expanded || names.length <= 3) return { text: names.join("、"), folded: false };
  return { text: `${names.slice(0, 3).join("、")} 等 ${names.length} 个`, folded: true };
}

export const AGENT_SCOPE_LABELS: Record<string, string> = Object.fromEntries(
  AGENT_SCOPE_OPTIONS.map((option) => [option.value, option.label]),
);

export type AgentTokenAccess = {
  default_notebook_id: string;
  notebook_ids: readonly string[];
  scopes: readonly string[];
  expires_at?: string | null;
};

/** UTC ISO → `<input type="datetime-local">` 的本地墙钟值(精确到分钟)。
 *  解析不出来返回空串,交给表单的「必填」校验拦下,而不是把 Invalid Date 塞进输入框。 */
export function utcIsoToLocalDateTime(value: string | null | undefined, timezoneOffsetMinutes?: number): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  const offset = timezoneOffsetMinutes ?? parsed.getTimezoneOffset();
  return new Date(parsed.getTime() - offset * 60_000).toISOString().slice(0, 16);
}

/** 已签发 token 的编辑草稿。已下线的 scope 不进草稿:后端对未知 scope 一律拒收,
 *  留着它只会让「保存」必然失败,而表单上又没有对应的勾选框能把它去掉。 */
export function agentTokenEditDraft(token: AgentTokenAccess): AgentTokenDraft {
  return {
    default_notebook_id: token.default_notebook_id,
    notebook_ids: Array.from(new Set([token.default_notebook_id, ...token.notebook_ids].filter(Boolean))),
    scopes: token.scopes.filter((scope) => Object.hasOwn(AGENT_SCOPE_LABELS, scope)),
    expires_at: utcIsoToLocalDateTime(token.expires_at),
  };
}

export function agentTokenAccessPath(tokenId: string): string {
  return `/agent-tokens/${encodeURIComponent(tokenId)}/access`;
}

/** 编辑器打开时读到的访问配置,原样回传给服务端做前置条件比对。 */
export function agentTokenAccessSnapshot(token: AgentTokenAccess) {
  return {
    scopes: [...token.scopes],
    default_notebook_id: token.default_notebook_id,
    notebook_ids: [...token.notebook_ids],
    expires_at: token.expires_at ?? null,
  };
}

/** 整体替换语义:四个字段都显式给出,`expires_at` 为 null 即无到期时间。
 *  `expected` 是编辑器打开时的配置——期间若被别的标签页改过,服务端拒写(409),
 *  而不是让这份旧草稿悄悄恢复刚被收回的权限。 */
export function agentTokenAccessRequest(draft: AgentTokenDraft, original: AgentTokenAccess) {
  // 过期时间没动就原样回传存储值:datetime-local 只到分钟,夏令时回拨那一小时的本地
  // 时间还有歧义,从草稿重算会让「只改权限」的保存悄悄把到期时间改掉(甚至提前)。
  const expiryUntouched = draft.expires_at === utcIsoToLocalDateTime(original.expires_at);
  const fields = expiryUntouched
    ? { ...agentTokenAccessFields({ ...draft, expires_at: "" }), expires_at: original.expires_at ?? null }
    : agentTokenAccessFields(draft);
  return { ...fields, expected: agentTokenAccessSnapshot(original) };
}

export function canSaveAgentTokenAccess(
  token: AgentTokenAccess,
  draft: AgentTokenDraft,
  ownedNotebookIds?: ReadonlySet<string>,
): boolean {
  return isCompleteAgentTokenDraft(draft, ownedNotebookIds) && agentTokenAccessChanged(token, draft);
}

function sameMembers(left: readonly string[], right: readonly string[]): boolean {
  const a = new Set(left);
  const b = new Set(right);
  return a.size === b.size && [...a].every((item) => b.has(item));
}

/** 与原配置逐项比较;过期时间按分钟粒度比(datetime-local 本身只到分钟)。 */
export function agentTokenAccessChanged(token: AgentTokenAccess, draft: AgentTokenDraft): boolean {
  const original = agentTokenEditDraft(token);
  return draft.default_notebook_id !== original.default_notebook_id
    || !sameMembers(draft.notebook_ids, original.notebook_ids)
    || !sameMembers(draft.scopes, token.scopes)
    || draft.expires_at !== original.expires_at;
}

export function agentPagePath(path: string, offset: number): string {
  const params = new URLSearchParams({
    offset: String(Math.max(0, offset)),
    limit: String(AGENT_ACCESS_PAGE_SIZE),
  });
  return `${path}?${params.toString()}`;
}

export function agentPageHasMore(page: readonly unknown[]): boolean {
  return page.length === AGENT_ACCESS_PAGE_SIZE;
}

export function mergeAgentPage<T extends { id: string }>(
  current: readonly T[],
  page: readonly T[],
): T[] {
  const incoming = new Map(page.map((item) => [item.id, item]));
  const merged = current.map((item) => incoming.get(item.id) ?? item);
  const known = new Set(current.map((item) => item.id));
  for (const item of page) {
    if (!known.has(item.id)) {
      merged.push(item);
      known.add(item.id);
    }
  }
  return merged;
}
