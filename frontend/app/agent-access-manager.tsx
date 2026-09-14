"use client";

// 「Agent 接入」——Agent Profile、token 签发、已签发 token 的权限修改与撤销。
//
// 独立页 `/agents` 是它的唯一宿主(账户菜单直达)。它原先折叠在「私有记忆」总览页
// 顶部,要先进记忆页、再展开才看得到;记忆页现在只留一条指向本页的入口。
//
// 已签发 token 的「修改权限」是行内编辑:忙碌态、失败文案与「已保存」都落在那一行
// (按钮自身 + 紧邻处),不走本组件顶部那条加载/签发共用的错误条。保存是整体替换
// (`PUT /agent-tokens/{id}/access`),服务端每次工具调用都实时重读 token 状态,所以
// 保存成功即对已连接的 Agent 生效,不需要重签或重连。
import { FormEvent, useEffect, useRef, useState } from "react";
import { Check, Copy, KeyRound, Pencil, Plus, X } from "lucide-react";

import {
  AGENT_SCOPE_LABELS,
  AGENT_SCOPE_OPTIONS,
  agentPageHasMore,
  agentPagePath,
  agentTokenAccessPath,
  agentTokenAccessRequest,
  agentTokenDraft,
  agentTokenEditDraft,
  agentTokenRequest,
  canIssueAgentToken,
  canSaveAgentTokenAccess,
  mergeAgentPage,
  type AgentTokenDraft,
} from "./agent-token-model";
import { requestJson } from "./api-client.ts";
import { copyTextSafely } from "./copy-text";
import { httpErrorStatus, toUserMessage } from "./errors.ts";
import { subscribeMemorySessionAbort } from "./memory-model";
import { label } from "./vocabulary";
import type {
  AgentProfile,
  AgentTokenIssued,
  AgentTokenSummary,
  NotebookSummary,
} from "./workspace-model";
import "./agent-access.css";

/** 「已保存」在行内停留的时长;与许愿墙卡片提示同一量级。 */
const TOKEN_SAVED_NOTICE_MS = 3000;

async function agentApi<T>(path: string, options: RequestInit = {}): Promise<T> {
  return requestJson(path, { ...options, tag: "memory", unauthorized: "clear-and-reload" });
}

function defaultExpiry(): string {
  const date = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

function formatTime(value: string): string {
  return new Date(value).toLocaleString("zh-CN");
}

function isExpired(token: AgentTokenSummary): boolean {
  if (!token.expires_at) return false;
  const expiresAt = Date.parse(token.expires_at);
  return Number.isFinite(expiresAt) && expiresAt <= Date.now();
}

/** 默认笔记本、笔记本白名单、Scopes、过期时间——签发表单与行内编辑共用同一组字段。
 *  草稿里有、但当前笔记本列表里没有的笔记本(已失去访问权或已删除)照样列出来,
 *  否则用户无从取消勾选它。 */
function AgentAccessFields({
  draft,
  setDraft,
  notebooks,
  disabled = false,
}: {
  draft: AgentTokenDraft;
  setDraft: (update: (current: AgentTokenDraft) => AgentTokenDraft) => void;
  notebooks: NotebookSummary[];
  disabled?: boolean;
}) {
  const known = new Set(notebooks.map((notebook) => notebook.id));
  const options = [
    ...notebooks.map((notebook) => ({ id: notebook.id, name: notebook.name })),
    ...Array.from(new Set([draft.default_notebook_id, ...draft.notebook_ids]))
      .filter((id) => id && !known.has(id))
      .map((id) => ({ id, name: "无法访问的笔记本" })),
  ];

  function setDefaultNotebook(notebookId: string) {
    setDraft((current) => ({
      ...current,
      default_notebook_id: notebookId,
      notebook_ids: Array.from(new Set([notebookId, ...current.notebook_ids].filter(Boolean))),
    }));
  }

  return (
    <>
      <label>默认笔记本<select value={draft.default_notebook_id} disabled={disabled} onChange={(event) => setDefaultNotebook(event.target.value)}>
        <option value="">选择笔记本</option>
        {options.map((notebook) => <option key={notebook.id} value={notebook.id}>{notebook.name}</option>)}
      </select></label>
      <fieldset><legend>笔记本白名单</legend>{options.map((notebook) => (
        <label className="agent-access-check" key={notebook.id} title={known.has(notebook.id) ? undefined : notebook.id}><input type="checkbox" checked={draft.notebook_ids.includes(notebook.id)} disabled={disabled || draft.default_notebook_id === notebook.id} onChange={(event) => setDraft((current) => ({ ...current, notebook_ids: event.target.checked ? [...current.notebook_ids, notebook.id] : current.notebook_ids.filter((id) => id !== notebook.id) }))} />{notebook.name}</label>
      ))}</fieldset>
      <fieldset><legend>Scopes</legend>{AGENT_SCOPE_OPTIONS.map((scope) => (
        <label className="agent-access-check" key={scope.value}><input type="checkbox" checked={draft.scopes.includes(scope.value)} disabled={disabled} onChange={(event) => setDraft((current) => ({ ...current, scopes: event.target.checked ? [...current.scopes, scope.value] : current.scopes.filter((item) => item !== scope.value) }))} />{scope.label}</label>
      ))}</fieldset>
      <label>过期时间<input type="datetime-local" value={draft.expires_at} disabled={disabled} onChange={(event) => setDraft((current) => ({ ...current, expires_at: event.target.value }))} /></label>
    </>
  );
}

type TokenEdit = { tokenId: string; original: AgentTokenSummary; draft: AgentTokenDraft };

export function AgentAccessManager({ sessionSignal }: { sessionSignal: AbortSignal }) {
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [tokens, setTokens] = useState<AgentTokenSummary[]>([]);
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [profileName, setProfileName] = useState("");
  const [profileDescription, setProfileDescription] = useState("");
  const [selectedProfile, setSelectedProfile] = useState("");
  const [draft, setDraft] = useState<AgentTokenDraft>(() => agentTokenDraft());
  const [issued, setIssued] = useState<AgentTokenIssued | null>(null);
  const [tokenCopyStatus, setTokenCopyStatus] = useState<"idle" | "copying" | "copied" | "manual">("idle");
  const [loading, setLoading] = useState(false);
  const [profilePageLoading, setProfilePageLoading] = useState(false);
  const [tokenPageLoading, setTokenPageLoading] = useState(false);
  const [profileHasMore, setProfileHasMore] = useState(false);
  const [tokenHasMore, setTokenHasMore] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [edit, setEdit] = useState<TokenEdit | null>(null);
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState("");
  const [savedTokenId, setSavedTokenId] = useState<string | null>(null);
  const [confirmRevokeId, setConfirmRevokeId] = useState<string | null>(null);
  const requestEpochRef = useRef(0);
  const listControllerRef = useRef<AbortController | null>(null);
  const profilePageControllerRef = useRef<AbortController | null>(null);
  const tokenPageControllerRef = useRef<AbortController | null>(null);
  const profileOffsetRef = useRef(0);
  const tokenOffsetRef = useRef(0);
  const mutationControllersRef = useRef(new Set<AbortController>());
  const issuedTokenRef = useRef<HTMLTextAreaElement | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => subscribeMemorySessionAbort(sessionSignal, () => {
    listControllerRef.current?.abort();
    profilePageControllerRef.current?.abort();
    tokenPageControllerRef.current?.abort();
    mutationControllersRef.current.forEach((controller) => controller.abort());
  }), [sessionSignal]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      listControllerRef.current?.abort();
      profilePageControllerRef.current?.abort();
      tokenPageControllerRef.current?.abort();
      mutationControllersRef.current.forEach((controller) => controller.abort());
    };
  }, []);

  useEffect(() => {
    if (!savedTokenId) return;
    const timer = window.setTimeout(() => setSavedTokenId(null), TOKEN_SAVED_NOTICE_MS);
    return () => window.clearTimeout(timer);
  }, [savedTokenId]);

  useEffect(() => {
    if (sessionSignal.aborted) return;
    const epoch = ++requestEpochRef.current;
    const controller = new AbortController();
    listControllerRef.current = controller;
    profileOffsetRef.current = 0;
    tokenOffsetRef.current = 0;
    setProfilePageLoading(false);
    setTokenPageLoading(false);
    setLoading(true);
    setError("");
    Promise.all([
      agentApi<AgentProfile[]>(agentPagePath("/agent-profiles", 0), { signal: controller.signal }),
      agentApi<AgentTokenSummary[]>(agentPagePath("/agent-tokens", 0), { signal: controller.signal }),
      agentApi<NotebookSummary[]>("/notebooks", { signal: controller.signal }),
    ]).then(([nextProfiles, nextTokens, nextNotebooks]) => {
      if (controller.signal.aborted || epoch !== requestEpochRef.current) return;
      setProfiles(nextProfiles);
      setTokens(nextTokens);
      profileOffsetRef.current = nextProfiles.length;
      tokenOffsetRef.current = nextTokens.length;
      setProfileHasMore(agentPageHasMore(nextProfiles));
      setTokenHasMore(agentPageHasMore(nextTokens));
      setNotebooks(nextNotebooks);
      const activeProfiles = nextProfiles.filter((profile) => profile.status === "active");
      setSelectedProfile((current) => activeProfiles.some((profile) => profile.id === current)
        ? current
        : activeProfiles[0]?.id || "");
      setDraft((current) => {
        if (current.default_notebook_id && nextNotebooks.some((notebook) => notebook.id === current.default_notebook_id)) {
          return current;
        }
        const next = agentTokenDraft(nextNotebooks[0]?.id || "");
        return { ...next, expires_at: defaultExpiry() };
      });
    }).catch((cause) => {
      if (!controller.signal.aborted && epoch === requestEpochRef.current) {
        setError(toUserMessage(cause, "加载失败，请稍后重试"));
      }
    }).finally(() => {
      if (epoch === requestEpochRef.current) setLoading(false);
    });
    return () => {
      controller.abort();
      profilePageControllerRef.current?.abort();
      profilePageControllerRef.current = null;
      tokenPageControllerRef.current?.abort();
      tokenPageControllerRef.current = null;
      if (listControllerRef.current === controller) listControllerRef.current = null;
      if (requestEpochRef.current === epoch) requestEpochRef.current += 1;
    };
  }, [refresh, sessionSignal]);

  async function loadMoreProfiles() {
    if (loading || profilePageLoading || !profileHasMore || sessionSignal.aborted) return;
    const epoch = requestEpochRef.current;
    const controller = new AbortController();
    profilePageControllerRef.current?.abort();
    profilePageControllerRef.current = controller;
    setProfilePageLoading(true);
    setError("");
    try {
      const page = await agentApi<AgentProfile[]>(
        agentPagePath("/agent-profiles", profileOffsetRef.current),
        { signal: controller.signal },
      );
      if (controller.signal.aborted || epoch !== requestEpochRef.current || !mountedRef.current) return;
      setProfiles((current) => mergeAgentPage(current, page));
      profileOffsetRef.current += page.length;
      setProfileHasMore(agentPageHasMore(page));
    } catch (cause) {
      if (!controller.signal.aborted && epoch === requestEpochRef.current && mountedRef.current) {
        setError(toUserMessage(cause, "加载失败，请稍后重试"));
      }
    } finally {
      if (profilePageControllerRef.current === controller) {
        profilePageControllerRef.current = null;
        if (mountedRef.current && epoch === requestEpochRef.current) setProfilePageLoading(false);
      }
    }
  }

  async function loadMoreTokens() {
    if (loading || tokenPageLoading || !tokenHasMore || sessionSignal.aborted) return;
    const epoch = requestEpochRef.current;
    const controller = new AbortController();
    tokenPageControllerRef.current?.abort();
    tokenPageControllerRef.current = controller;
    setTokenPageLoading(true);
    setError("");
    try {
      const page = await agentApi<AgentTokenSummary[]>(
        agentPagePath("/agent-tokens", tokenOffsetRef.current),
        { signal: controller.signal },
      );
      if (controller.signal.aborted || epoch !== requestEpochRef.current || !mountedRef.current) return;
      setTokens((current) => mergeAgentPage(current, page));
      tokenOffsetRef.current += page.length;
      setTokenHasMore(agentPageHasMore(page));
    } catch (cause) {
      if (!controller.signal.aborted && epoch === requestEpochRef.current && mountedRef.current) {
        setError(toUserMessage(cause, "加载失败，请稍后重试"));
      }
    } finally {
      if (tokenPageControllerRef.current === controller) {
        tokenPageControllerRef.current = null;
        if (mountedRef.current && epoch === requestEpochRef.current) setTokenPageLoading(false);
      }
    }
  }

  async function mutate<T>(path: string, options: RequestInit): Promise<T | null> {
    if (sessionSignal.aborted) return null;
    const controller = new AbortController();
    mutationControllersRef.current.add(controller);
    setLoading(true);
    setError("");
    try {
      const result = await agentApi<T>(path, { ...options, signal: controller.signal });
      return controller.signal.aborted || !mountedRef.current ? null : result;
    } catch (cause) {
      if (!controller.signal.aborted && mountedRef.current) {
        setError(toUserMessage(cause));
      }
      return null;
    } finally {
      mutationControllersRef.current.delete(controller);
      if (mountedRef.current) setLoading(false);
    }
  }

  async function createProfile(event: FormEvent) {
    event.preventDefault();
    if (!profileName.trim()) return;
    const profile = await mutate<AgentProfile>("/agent-profiles", {
      method: "POST",
      body: JSON.stringify({ name: profileName.trim(), description: profileDescription.trim() }),
    });
    if (!profile) return;
    setProfileName("");
    setProfileDescription("");
    setSelectedProfile(profile.id);
    setRefresh((value) => value + 1);
  }

  async function disableProfile(profileId: string) {
    const profile = await mutate<AgentProfile>(`/agent-profiles/${encodeURIComponent(profileId)}`, {
      method: "PATCH",
      body: JSON.stringify({ status: "revoked" }),
    });
    if (profile) setRefresh((value) => value + 1);
  }

  async function issueToken(event: FormEvent) {
    event.preventDefault();
    if (!canIssueAgentToken(selectedProfile, draft)) return;
    const token = await mutate<AgentTokenIssued>(
      `/agent-profiles/${encodeURIComponent(selectedProfile)}/tokens`,
      { method: "POST", body: JSON.stringify(agentTokenRequest(selectedProfile, draft)) },
    );
    if (!token) return;
    setIssued(token);
    setTokenCopyStatus("idle");
    setRefresh((value) => value + 1);
  }

  async function copyIssuedToken() {
    if (!issued || tokenCopyStatus === "copying") return;
    setTokenCopyStatus("copying");
    const copied = await copyTextSafely(issued.token);
    if (!mountedRef.current) return;
    if (copied) {
      setTokenCopyStatus("copied");
      return;
    }
    setTokenCopyStatus("manual");
    issuedTokenRef.current?.focus();
    issuedTokenRef.current?.select();
  }

  async function revokeToken(tokenId: string) {
    setConfirmRevokeId(null);
    const token = await mutate<AgentTokenSummary>(`/agent-tokens/${encodeURIComponent(tokenId)}`, { method: "DELETE" });
    if (token) setRefresh((value) => value + 1);
  }

  function startEdit(token: AgentTokenSummary) {
    if (editSaving) return;
    setEdit({ tokenId: token.id, original: token, draft: agentTokenEditDraft(token) });
    setEditError("");
    setSavedTokenId(null);
    setConfirmRevokeId(null);
  }

  function updateEditDraft(update: (current: AgentTokenDraft) => AgentTokenDraft) {
    setEdit((current) => current && { ...current, draft: update(current.draft) });
  }

  const disabledProfileIds = new Set(
    profiles.filter((profile) => profile.status !== "active").map((profile) => profile.id),
  );

  async function saveTokenAccess(event: FormEvent, token: AgentTokenSummary) {
    event.preventDefault();
    if (!edit || edit.tokenId !== token.id || editSaving || sessionSignal.aborted) return;
    if (!canSaveAgentTokenAccess(edit.original, edit.draft)) return;
    const controller = new AbortController();
    mutationControllersRef.current.add(controller);
    setEditSaving(true);
    setEditError("");
    try {
      const saved = await agentApi<AgentTokenSummary>(agentTokenAccessPath(token.id), {
        method: "PUT",
        body: JSON.stringify(agentTokenAccessRequest(edit.draft, edit.original)),
        signal: controller.signal,
      });
      if (controller.signal.aborted || !mountedRef.current) return;
      setTokens((current) => current.map((item) => (item.id === saved.id ? saved : item)));
      setEdit(null);
      setSavedTokenId(saved.id);
      // 重拉一次列表:保存期间已发出的旧列表请求会被新的 epoch 作废,
      // 不会在「已保存」旁边把这一行覆盖回旧配置。
      setRefresh((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted && mountedRef.current) {
        setEditError(toUserMessage(cause, "保存失败，请稍后重试"));
        // 409:已撤销、Profile 已停用或已被别处改过——都重拉列表,让这一行
        // 与再次打开的编辑器以服务端现状为准。
        if (httpErrorStatus(cause) === 409) setRefresh((value) => value + 1);
      }
    } finally {
      mutationControllersRef.current.delete(controller);
      if (mountedRef.current) setEditSaving(false);
    }
  }

  return (
    <section className="agent-access-card" aria-label="Agent 接入">
      <div className="agent-access-body">
        {error && <div className="agent-access-error" role="alert">{error}</div>}
        <div className="agent-access-grid">
          <form className="agent-config-block" onSubmit={createProfile}>
            <h3>Agent Profile</h3>
            <p>稳定身份用于记忆来源和审计；停用后它的全部 token 立即失效。</p>
            <label>名称<input value={profileName} maxLength={80} onChange={(event) => setProfileName(event.target.value)} placeholder="例如 Claude Code" /></label>
            <label>说明<input value={profileDescription} maxLength={500} onChange={(event) => setProfileDescription(event.target.value)} placeholder="用途或运行环境" /></label>
            <button type="submit" disabled={loading || !profileName.trim()}><Plus size={14} /> 新建 Profile</button>
            <div className="agent-profile-list">
              {profiles.map((profile) => (
                <div key={profile.id}>
                  <span><strong>{profile.name}</strong><small>{profile.description || "无说明"} · {profile.status === "active" ? "启用" : "已停用"}</small></span>
                  {profile.status === "active" && <button type="button" disabled={loading} onClick={() => disableProfile(profile.id)}>停用</button>}
                </div>
              ))}
              {profileHasMore && (
                <button type="button" className="agent-load-more" disabled={loading || profilePageLoading} onClick={() => loadMoreProfiles()}>
                  {profilePageLoading ? "加载中…" : "加载更多 Profile"}
                </button>
              )}
            </div>
          </form>

          <form className="agent-config-block" onSubmit={issueToken}>
            <h3>签发 Token</h3>
            <label>Profile<select value={selectedProfile} onChange={(event) => setSelectedProfile(event.target.value)}>
              <option value="">选择 Profile</option>
              {profiles.filter((profile) => profile.status === "active").map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
            </select></label>
            <AgentAccessFields draft={draft} setDraft={setDraft} notebooks={notebooks} />
            <button type="submit" className="primary" disabled={loading || !canIssueAgentToken(selectedProfile, draft)}><KeyRound size={14} /> 签发 Token</button>
          </form>
        </div>

        {issued && (
          <div className="agent-issued-token" role="status">
            <strong>请立即保存：明文 token 仅显示这一次</strong>
            <textarea
              ref={issuedTokenRef}
              className="agent-issued-token-value"
              aria-label="新签发的明文 token"
              readOnly
              rows={2}
              value={issued.token}
              onFocus={(event) => event.currentTarget.select()}
            />
            <button type="button" disabled={tokenCopyStatus === "copying"} onClick={() => { void copyIssuedToken(); }}>
              {tokenCopyStatus === "copied" ? <Check size={14} /> : <Copy size={14} />}
              {tokenCopyStatus === "copying" ? "复制中…" : tokenCopyStatus === "copied" ? "已复制" : "复制"}
            </button>
            <button type="button" onClick={() => { setIssued(null); setTokenCopyStatus("idle"); }}><X size={14} /> 我已保存</button>
            <p className="agent-token-onboarding">
              把上方 token 与
              <a href="/api/agent-mcp/onboarding" target="_blank" rel="noreferrer">Agent MCP 接入说明链接</a>
              一起交给 Agent，它可以读取说明并自行配置 MCP；链接本身不包含 token。
            </p>
            {tokenCopyStatus === "manual" && (
              <span className="agent-token-copy-feedback" aria-live="polite">自动复制失败，token 已全选，请按 Ctrl/Cmd+C 复制。</span>
            )}
          </div>
        )}

        <div className="agent-token-list">
          <h3>已签发 Token</h3>
          <p>修改权限保存后立即对正在使用这个 token 的 Agent 生效，无需重新签发或重新配置。</p>
          {tokens.length === 0 ? <p>暂无 token。</p> : tokens.map((token) => {
            // 停用 Profile 不会写 token 的 revoked_at;按已加载的 Profile 状态判断,
            // 免得给一个保存必然 409 的 token 留着「修改权限」。
            const profileDisabled = disabledProfileIds.has(token.agent_profile_id);
            const editing = edit?.tokenId === token.id && !token.revoked_at && !profileDisabled ? edit : null;
            const expired = isExpired(token);
            return (
              <article key={token.id} className={editing ? "editing" : undefined}>
                <div className="agent-token-row">
                  <span>
                    <strong>{token.profile_name}</strong>
                    <small>
                      {token.scopes.map((scope) => label(AGENT_SCOPE_LABELS, scope, "已下线的权限")).join(" · ")}
                      <br />
                      默认：{notebooks.find((notebook) => notebook.id === token.default_notebook_id)?.name || "无法访问的笔记本"} · 允许 {token.notebook_ids.length} 个笔记本 · {token.expires_at ? `${expired ? "已于" : "到期"} ${formatTime(token.expires_at)}${expired ? " 过期" : ""}` : "无到期时间"}
                    </small>
                  </span>
                  <div className="agent-token-actions">
                    {savedTokenId === token.id && (
                      <small className="agent-token-saved" role="status"><Check size={13} /> 已保存，立即生效</small>
                    )}
                    {token.revoked_at ? <em>已撤销</em> : confirmRevokeId === token.id ? (
                      <>
                        <small className="agent-token-confirm">撤销后立即失效，且不能恢复</small>
                        <button type="button" className="danger" disabled={loading} onClick={() => revokeToken(token.id)}>确认撤销</button>
                        <button type="button" disabled={loading} onClick={() => setConfirmRevokeId(null)}>不撤销</button>
                      </>
                    ) : (
                      <>
                        {profileDisabled && <em>Profile 已停用</em>}
                        {!editing && !profileDisabled && (
                          <button type="button" disabled={loading || editSaving} onClick={() => startEdit(token)}><Pencil size={13} /> 修改权限</button>
                        )}
                        <button type="button" className="danger" disabled={loading || (editSaving && Boolean(editing))} onClick={() => setConfirmRevokeId(token.id)}>撤销</button>
                      </>
                    )}
                  </div>
                </div>
                {editing && (
                  <form className="agent-config-block agent-token-editor" aria-label={`修改 ${token.profile_name} 的 token 权限`} onSubmit={(event) => { void saveTokenAccess(event, token); }}>
                    <AgentAccessFields draft={editing.draft} setDraft={updateEditDraft} notebooks={notebooks} disabled={editSaving} />
                    {!editing.draft.expires_at && <p>请设置过期时间。</p>}
                    {editError && <div className="agent-access-error" role="alert">{editError}</div>}
                    <div className="agent-token-editor-actions">
                      <button type="submit" className="primary" disabled={editSaving || !canSaveAgentTokenAccess(editing.original, editing.draft)}>
                        {editSaving ? "保存中…" : "保存"}
                      </button>
                      <button type="button" disabled={editSaving} onClick={() => { setEdit(null); setEditError(""); }}>取消</button>
                    </div>
                  </form>
                )}
              </article>
            );
          })}
          {tokenHasMore && (
            <button type="button" className="agent-load-more" disabled={loading || tokenPageLoading} onClick={() => loadMoreTokens()}>
              {tokenPageLoading ? "加载中…" : "加载更多 Token"}
            </button>
          )}
        </div>
      </div>
    </section>
  );
}
