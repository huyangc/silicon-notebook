"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { ArrowUpCircle, Bookmark, Bot, Check, CheckSquare, ChevronDown, Copy, Edit3, KeyRound, LayoutGrid, List, Plus, Search, Sparkles, Trash2, X } from "lucide-react";

import { AnswerMarkdown } from "./answer-markdown";
import {
  AGENT_SCOPE_OPTIONS,
  agentPageHasMore,
  agentPagePath,
  agentTokenDraft,
  agentTokenRequest,
  canIssueAgentToken,
  mergeAgentPage,
  type AgentTokenDraft,
} from "./agent-token-model";
import { requestJson } from "./api-client.ts";
import { copyTextSafely } from "./copy-text";
import { humanizedError, logDiagnostic, toUserMessage } from "./errors.ts";
import { FloatingModalCard } from "./floating-modal-card";
import { resolvePromotionTarget, type MountedBase } from "./notebook-bases";
import { useFloatingWindow } from "./use-floating-window";
import {
  canEditMemory,
  canPromoteMemory,
  confirmMemoryBody,
  fromAnswerMemoryBody,
  memoryListPath,
  memoryEvidenceRows,
  memoryCitationReferences,
  memoryOriginMeta,
  memoryPromotionLabel,
  memoryPromotionPath,
  memoryProvenanceRows,
  memoryStatusMeta,
  subscribeMemorySessionAbort,
  EVIDENCE_TYPE,
  EVIDENCE_STATUS,
  MEMORY_INPUT_LIMITS,
  validateMemoryDraft,
  type MemoryScope,
} from "./memory-model";
import { transferMemories } from "./memory-transfer.ts";
import { Pagination } from "./Pagination";
import { confirmedOnly, singleSourceNotebookId, summarizeTransferResults, type TransferMode } from "./transfer-model.ts";
import { DestinationPicker } from "./transfer-picker.tsx";
import { label, EVIDENCE_LEVEL } from "./vocabulary";
import type {
  MemoryOrigin,
  MemoryPreview,
  MemoryRecord,
  MemoryStatus,
  AgentProfile,
  AgentTokenIssued,
  AgentTokenSummary,
  NotebookSummary,
  PaginatedMemories,
} from "./workspace-model";
import "./memory-panel.css";

const MEMORY_PAGE_SIZE = 20;

async function memoryApi<T>(path: string, options: RequestInit = {}): Promise<T> {
  return requestJson(path, { ...options, tag: "memory", unauthorized: "clear-and-reload" });
}

type MemoryDraft = { title: string; content_md: string; tags: string };

// C4「复制/移动到…」结果提示——五个字段各自独立设置(warning/sourceChanged/
// promotionBlocked/failure 可以在同一批结果里同时非空,分属结果数组里不相交
// 的子集;success 与其它四个互斥,只在没有任何 warning/sourceChanged/
// promotionBlocked/failure 时才可能非空):
// warning 对应 transfer-model.ts summarizeTransferResults() 的
// copiedSourceNotRemoved 子集减去 sourceChanged 子集,即 error_code ===
// "cleanup_error" 的那部分——清理操作本身出错,源确实原封未动,弃用/删除都
// 安全;sourceChanged 对应它的 sourceChanged 子集(round 10 P1-B):源不是清理
// 失败,是在复制完成后被并发编辑/状态变更/提交了审批提案而被有意保留下来
// 的——这两种成因绝不能共用一条"请手动删除源"的指引,那对 sourceChanged 是
// 危险的(照做会连带丢失被保留下来的改动),只能是「核对目标/重新发起搬迁」
// 或「弃用源」(弃用会真的清理掉源的 KG 派生源,删除不会——MemoryService.
// delete 只删行本身,是 deprecate 才会调 remove_memory_source);promotionBlocked
// 对应它的 promotionBlocked 子集(round 8 P2-A:该 Memory 正在公共知识库审批
// 队列中,move 被拒绝——同样不能引导"重试",得指向待确认中心);failure 对应
// 剩下的普通 status==="failed"。四者渲染成不同文案的横幅,不能合并成一条消息
// ——AMENDMENT 2 明确要求 warning/failure 视觉上可区分,promotionBlocked/
// sourceChanged 是同一原则的延伸:原因不同,提示和后续动作也该不同。
// success 是复审 Minor 补充,round 10 P2 从"仅 copy"扩到 copy/move 都设置:
// 批量结果里没有 warning/sourceChanged/promotionBlocked/failure(即全部成功)
// 就设置——原先 move 的成功被认为"靠列表刷新自然可见"，但未过滤的全局
// Memory 视图里列表刷新后卡片依旧在(只是换了 notebook，卡片不显示归属笔记
// 本)，用户拿不到任何"确实移动成功了"的确认；copy 同理(源视图不变、目标又
// 不在眼前)。两种模式统一带上目标笔记本名字,不给提示用户没法确认"点了到底
// 有没有效果、去了哪"。复用同一套 .memory-transfer-notice 视觉,不新造
// chrome。
type TransferNotice = {
  warning: string | null;
  sourceChanged: string | null;
  promotionBlocked: string | null;
  failure: string | null;
  success: string | null;
};

function draftFor(memory: Pick<MemoryRecord, "title" | "content_md" | "tags">): MemoryDraft {
  return { title: memory.title, content_md: memory.content_md, tags: memory.tags.join(", ") };
}

function tagsFromDraft(value: string): string[] {
  return Array.from(new Set(value.split(/[,，\n]/).map((tag) => tag.trim()).filter(Boolean)));
}

function MemoryBody({ content }: { content: string }) {
  return (
    <div className="memory-markdown">
      <AnswerMarkdown answer={content} onReferenceClick={() => undefined} />
    </div>
  );
}

function MemoryEditor({
  draft,
  setDraft,
}: {
  draft: MemoryDraft;
  setDraft: (draft: MemoryDraft) => void;
}) {
  return (
    <div className="memory-editor">
      <label>
        标题
        <input
          autoFocus
          maxLength={MEMORY_INPUT_LIMITS.titleMaxChars}
          value={draft.title}
          onChange={(event) => setDraft({ ...draft, title: event.target.value })}
        />
      </label>
      <label>
        内容
        <textarea
          rows={8}
          maxLength={MEMORY_INPUT_LIMITS.contentMaxChars}
          value={draft.content_md}
          onChange={(event) => setDraft({ ...draft, content_md: event.target.value })}
        />
      </label>
      <label>
        标签（逗号分隔）
        <input
          maxLength={MEMORY_INPUT_LIMITS.tagMaxCount * (MEMORY_INPUT_LIMITS.tagMaxChars + 2)}
          value={draft.tags}
          onChange={(event) => setDraft({ ...draft, tags: event.target.value })}
        />
      </label>
    </div>
  );
}

function defaultExpiry(): string {
  const date = new Date(Date.now() + 30 * 24 * 60 * 60 * 1000);
  date.setMinutes(date.getMinutes() - date.getTimezoneOffset());
  return date.toISOString().slice(0, 16);
}

function AgentAccessManager({ sessionSignal }: { sessionSignal: AbortSignal }) {
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [tokens, setTokens] = useState<AgentTokenSummary[]>([]);
  const [notebooks, setNotebooks] = useState<NotebookSummary[]>([]);
  const [profileName, setProfileName] = useState("");
  const [profileDescription, setProfileDescription] = useState("");
  const [selectedProfile, setSelectedProfile] = useState("");
  const [draft, setDraft] = useState<AgentTokenDraft>(() => agentTokenDraft());
  const [issued, setIssued] = useState<AgentTokenIssued | null>(null);
  const [tokenCopyStatus, setTokenCopyStatus] = useState<"idle" | "copying" | "copied" | "manual">("idle");
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [profilePageLoading, setProfilePageLoading] = useState(false);
  const [tokenPageLoading, setTokenPageLoading] = useState(false);
  const [profileHasMore, setProfileHasMore] = useState(false);
  const [tokenHasMore, setTokenHasMore] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
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
    if (!expanded || sessionSignal.aborted) return;
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
      memoryApi<AgentProfile[]>(agentPagePath("/agent-profiles", 0), { signal: controller.signal }),
      memoryApi<AgentTokenSummary[]>(agentPagePath("/agent-tokens", 0), { signal: controller.signal }),
      memoryApi<NotebookSummary[]>("/notebooks", { signal: controller.signal }),
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
  }, [expanded, refresh, sessionSignal]);

  async function loadMoreProfiles() {
    if (loading || profilePageLoading || !profileHasMore || sessionSignal.aborted) return;
    const epoch = requestEpochRef.current;
    const controller = new AbortController();
    profilePageControllerRef.current?.abort();
    profilePageControllerRef.current = controller;
    setProfilePageLoading(true);
    setError("");
    try {
      const page = await memoryApi<AgentProfile[]>(
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
      const page = await memoryApi<AgentTokenSummary[]>(
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
      const result = await memoryApi<T>(path, { ...options, signal: controller.signal });
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
    const token = await mutate<AgentTokenSummary>(`/agent-tokens/${encodeURIComponent(tokenId)}`, { method: "DELETE" });
    if (token) setRefresh((value) => value + 1);
  }

  function setDefaultNotebook(notebookId: string) {
    setDraft((current) => ({
      ...current,
      default_notebook_id: notebookId,
      notebook_ids: Array.from(new Set([notebookId, ...current.notebook_ids].filter(Boolean))),
    }));
  }

  return (
    <section className="agent-access-card" aria-label="Agent 接入">
      <button type="button" className="agent-access-toggle" onClick={() => setExpanded((value) => !value)}>
        <span><KeyRound size={17} /><strong>Agent 接入</strong><small>管理 Claude Code、Codex 等客户端的最小权限 token</small></span>
        <ChevronDown size={16} className={expanded ? "open" : ""} />
      </button>
      {expanded && (
        <div className="agent-access-body">
          {error && <div className="memory-error" role="alert">{error}</div>}
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
              <label>默认笔记本<select value={draft.default_notebook_id} onChange={(event) => setDefaultNotebook(event.target.value)}>
                <option value="">选择笔记本</option>
                {notebooks.map((notebook) => <option key={notebook.id} value={notebook.id}>{notebook.name}</option>)}
              </select></label>
              <fieldset><legend>笔记本白名单</legend>{notebooks.map((notebook) => (
                <label className="agent-check" key={notebook.id}><input type="checkbox" checked={draft.notebook_ids.includes(notebook.id)} disabled={draft.default_notebook_id === notebook.id} onChange={(event) => setDraft((current) => ({ ...current, notebook_ids: event.target.checked ? [...current.notebook_ids, notebook.id] : current.notebook_ids.filter((id) => id !== notebook.id) }))} />{notebook.name}</label>
              ))}</fieldset>
              <fieldset><legend>Scopes</legend>{AGENT_SCOPE_OPTIONS.map((scope) => (
                <label className="agent-check" key={scope.value}><input type="checkbox" checked={draft.scopes.includes(scope.value)} onChange={(event) => setDraft((current) => ({ ...current, scopes: event.target.checked ? [...current.scopes, scope.value] : current.scopes.filter((item) => item !== scope.value) }))} />{scope.label}</label>
              ))}</fieldset>
              <label>过期时间<input type="datetime-local" value={draft.expires_at} onChange={(event) => setDraft((current) => ({ ...current, expires_at: event.target.value }))} /></label>
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
            {tokens.length === 0 ? <p>暂无 token。</p> : tokens.map((token) => (
              <article key={token.id}>
                <span><strong>{token.profile_name}</strong><small>{token.scopes.join(" · ")}<br />默认：{notebooks.find((notebook) => notebook.id === token.default_notebook_id)?.name || token.default_notebook_id} · 允许 {token.notebook_ids.length} 个笔记本 · {token.expires_at ? `到期 ${new Date(token.expires_at).toLocaleString("zh-CN")}` : "无到期时间"}</small></span>
                {token.revoked_at ? <em>已撤销</em> : <button type="button" className="danger" disabled={loading} onClick={() => revokeToken(token.id)}>撤销</button>}
              </article>
            ))}
            {tokenHasMore && (
              <button type="button" className="agent-load-more" disabled={loading || tokenPageLoading} onClick={() => loadMoreTokens()}>
                {tokenPageLoading ? "加载中…" : "加载更多 Token"}
              </button>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

export function MemoryPanel({
  scope,
  notebookId,
  bases,
  notebookBases,
  sessionSignal,
  initialNotebookId,
  initialStatus,
  initialMemoryId,
  onOpenSource,
}: {
  scope: MemoryScope;
  notebookId: string | null;
  /** 多领域基准库(Task 14 追加项)：仅 scope==="notebook" 时由 page.tsx 传入，
   * 供 resolvePromotionTarget 判定晋升按钮的三态（0 个禁用/1 个直接用/>1 个
   * 弹选择器）。scope==="notebook" 下所有记忆共享同一个 notebookId，这份
   * bases 对它们都成立。 */
  bases?: MountedBase[];
  /** codex 对 PR#304 的审查(2026-07-19)P2 #2:scope==="global" 时由 page.tsx
   * 传入——一条 Memory 可能来自任意一个 notebook，没有单一"当前笔记本"，
   * 但每条记忆自带 notebook_id，按它各自查询挂载集合即可；"没有单一当前
   * 笔记本"不等于"无法解析"，此前把两者混为一谈，导致这类笔记本从全局页
   * 晋升永远做不到（挂了 >1 个公共库时后端必拒，而这个视图没有任何选择器）。
   * key 是 notebook_id，value 形状同 `bases`。 */
  notebookBases?: Record<string, MountedBase[]>;
  sessionSignal: AbortSignal;
  initialNotebookId?: string | null;
  initialStatus?: MemoryStatus | null;
  initialMemoryId?: string | null;
  onOpenSource?: (sourceId: string, elementId?: string) => void;
}) {
  // 晋升目标三态按记忆各自的 notebook 解析，两个 scope 共用同一套
  // resolvePromotionTarget 判定(不另写一份规则)。notebook 视图里所有记忆
  // 共享同一个 notebookId，退化成固定的 `bases`；global 视图按
  // memory.notebook_id 查 notebookBases。
  const promotionTargetFor = (memory: MemoryRecord) =>
    resolvePromotionTarget(
      scope === "notebook" ? (bases ?? []) : (notebookBases?.[memory.notebook_id] ?? [])
    );
  const [pendingPromotionMemory, setPendingPromotionMemory] = useState<MemoryRecord | null>(null);
  const [items, setItems] = useState<MemoryRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [ownerTotal, setOwnerTotal] = useState(0);
  const [ownerPending, setOwnerPending] = useState(0);
  const [notebookOptions, setNotebookOptions] = useState<PaginatedMemories["notebook_options"]>([]);
  const [notebookFilter, setNotebookFilter] = useState(
    scope === "global" ? initialNotebookId ?? "" : "",
  );
  const [page, setPage] = useState(0);
  const [status, setStatus] = useState<MemoryStatus | "all">(
    initialStatus ?? "all",
  );
  const [origin, setOrigin] = useState<MemoryOrigin | "all">("all");
  const [queryDraft, setQueryDraft] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<MemoryDraft>({ title: "", content_md: "", tags: "" });
  const [kgEligible, setKgEligible] = useState(false);
  const [extractKg, setExtractKg] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [layout, setLayout] = useState<"grid" | "list">("grid");
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [pendingDelete, setPendingDelete] = useState<{ kind: "single"; memory: MemoryRecord } | { kind: "bulk" } | null>(null);
  // C4「复制/移动到…」：ids 在打开选择器那一刻就冻结(单条=[memory.id]，批量=
  // 当时选中集合解出的 id 列表)，不是留一个"kind"标记再到确认时重新从
  // selectedIds 现算——DestinationPicker 是全屏遮罩(.utility-modal)，打开期间
  // 背后不可能再变更选中态，冻结与现算在这里等价，冻结更简单也避免选择器开着
  // 时选中状态被其它路径意外改动而导致 sourceNotebookId 和 ids 对不上。
  // excludedCount（P2-B）：批量入口在冻结 ids 之前已经用 confirmedOnly 筛掉了
  // 非 confirmed 项——这里记一下筛掉了几条，好让下面的 picker 标题如实告诉
  // 用户"看到的数字比你选中的少，是因为剔除了非已确认状态"，而不是悄悄少传。
  // 单条入口（卡片操作区本就只在 confirmed 卡片上渲染）永远是 undefined。
  const [pendingTransfer, setPendingTransfer] = useState<{ ids: string[]; sourceNotebookId: string; excludedCount?: number } | null>(null);
  const [transferNotice, setTransferNotice] = useState<TransferNotice | null>(null);
  // Important 4（复审）：批量/单条传输弹窗自己的「同时抽取到知识图谱」勾选
  // 状态——与上面 confirm 编辑态那个 extractKg 分开维护(两处是不同流程各自
  // 的一次性选择,合用一个 state 会在"正编辑一条候选 memory 的同时打开传输
  // 弹窗"这类交叠场景里互相污染)。两处打开传输弹窗的入口都会把它重置回
  // 默认 true,保证每次打开都是"默认勾选"，匹配 confirm 时的默认行为。
  const [transferExtractKg, setTransferExtractKg] = useState(true);
  const listRequestEpochRef = useRef(0);
  const listControllerRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  const mutationControllersRef = useRef(new Set<AbortController>());
  const navigationExpandedIdRef = useRef<string | null>(null);

  useEffect(() => subscribeMemorySessionAbort(sessionSignal, () => {
    listControllerRef.current?.abort();
    mutationControllersRef.current.forEach((controller) => controller.abort());
  }), [sessionSignal]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      mutationControllersRef.current.forEach((controller) => controller.abort());
      mutationControllersRef.current.clear();
    };
  }, []);

  useEffect(() => {
    const saved = window.localStorage.getItem("memory-layout");
    if (saved === "list" || saved === "grid") setLayout(saved);
  }, []);
  useEffect(() => {
    window.localStorage.setItem("memory-layout", layout);
  }, [layout]);

  useEffect(() => {
    if (scope !== "global") return;
    setNotebookFilter(initialNotebookId ?? "");
    setStatus(initialStatus ?? "all");
    setPage(0);
    setOrigin("all");
    setQueryDraft("");
    setQuery("");
    setEditingId(null);
    setExpandedIds((previous) => {
      const next = new Set(previous);
      if (navigationExpandedIdRef.current) next.delete(navigationExpandedIdRef.current);
      navigationExpandedIdRef.current = initialMemoryId ?? null;
      if (initialMemoryId) next.add(initialMemoryId);
      return next;
    });
  }, [scope, initialNotebookId, initialStatus, initialMemoryId]);

  const toggleExpanded = (id: string) => setExpandedIds((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });

  const toggleSelected = (id: string) => setSelectedIds((prev) => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return next;
  });

  async function deleteMemory(memory: MemoryRecord) {
    if (busyId || sessionSignal.aborted) return;
    const controller = new AbortController();
    mutationControllersRef.current.add(controller);
    setBusyId(memory.id);
    setError("");
    setTransferNotice(null);
    try {
      await memoryApi(`/memories/${encodeURIComponent(memory.id)}`, { method: "DELETE", signal: controller.signal });
      if (controller.signal.aborted || !mountedRef.current) return;
      setSelectedIds((prev) => { const next = new Set(prev); next.delete(memory.id); return next; });
      setRefresh((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted && mountedRef.current) setError(toUserMessage(cause));
    } finally {
      mutationControllersRef.current.delete(controller);
      if (mountedRef.current) setBusyId(null);
    }
  }

  async function bulkDeleteMemories(ids: string[]) {
    if (busyId || sessionSignal.aborted || ids.length === 0) return;
    const controller = new AbortController();
    mutationControllersRef.current.add(controller);
    setBusyId("__bulk__");
    setError("");
    setTransferNotice(null);
    try {
      await memoryApi("/memories/bulk-delete", {
        method: "POST",
        body: JSON.stringify({ memory_ids: ids }),
        signal: controller.signal,
      });
      if (controller.signal.aborted || !mountedRef.current) return;
      setSelectedIds(new Set());
      setSelectionMode(false);
      setRefresh((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted && mountedRef.current) setError(toUserMessage(cause));
    } finally {
      mutationControllersRef.current.delete(controller);
      if (mountedRef.current) setBusyId(null);
    }
  }

  function confirmDelete() {
    const target = pendingDelete;
    setPendingDelete(null);
    if (!target) return;
    if (target.kind === "single") void deleteMemory(target.memory);
    else void bulkDeleteMemories(Array.from(selectedIds));
  }

  useEffect(() => {
    if (sessionSignal.aborted || (scope === "notebook" && !notebookId)) return;
    const epoch = ++listRequestEpochRef.current;
    const controller = new AbortController();
    listControllerRef.current = controller;
    setLoading(true);
    setError("");
    memoryApi<PaginatedMemories>(memoryListPath({
      scope,
      notebookId: scope === "global" ? notebookFilter || null : notebookId,
      status,
      origin,
      query,
      offset: page * MEMORY_PAGE_SIZE,
      limit: MEMORY_PAGE_SIZE,
    }), { signal: controller.signal })
      .then((result) => {
        if (listRequestEpochRef.current !== epoch) return;
        setItems(result.items);
        setTotal(result.total_count);
        setOwnerTotal(result.owner_total_count);
        setOwnerPending(result.owner_pending_count);
        setNotebookOptions(result.notebook_options);
        setKgEligible(result.kg_extract_eligible ?? false);
      })
      .catch((cause) => {
        if (controller.signal.aborted || listRequestEpochRef.current !== epoch) return;
        setError(toUserMessage(cause, "加载失败，请稍后重试"));
      })
      .finally(() => {
        if (listRequestEpochRef.current === epoch) setLoading(false);
      });
    return () => {
      controller.abort();
      if (listControllerRef.current === controller) listControllerRef.current = null;
      if (listRequestEpochRef.current === epoch) listRequestEpochRef.current += 1;
    };
  }, [notebookFilter, notebookId, origin, page, query, refresh, scope, sessionSignal, status]);

  function beginEdit(memory: MemoryRecord) {
    setEditingId(memory.id);
    setDraft(draftFor(memory));
    setExtractKg(true);
  }

  async function updateMemory(memory: MemoryRecord, action: "save" | "confirm" | "reject" | "deprecate") {
    if (busyId || sessionSignal.aborted) return;
    const controller = new AbortController();
    mutationControllersRef.current.add(controller);
    setBusyId(memory.id);
    setError("");
    setTransferNotice(null);
    try {
      const validationError = validateMemoryDraft({
        title: draft.title,
        content_md: draft.content_md,
        tags: tagsFromDraft(draft.tags),
      });
      if ((action === "save" || action === "confirm") && validationError) {
        setError(validationError);
        return;
      }
      const fields = {
        title: draft.title.trim(),
        content_md: draft.content_md.trim(),
        tags: tagsFromDraft(draft.tags),
      };
      const path = action === "save"
        ? `/memories/${encodeURIComponent(memory.id)}`
        : `/memories/${encodeURIComponent(memory.id)}/${action}`;
      // save (PATCH → MemoryUpdate) must never carry extract_kg; only confirm may.
      const body = action === "confirm"
        ? JSON.stringify(confirmMemoryBody({ ...fields, eligible: kgEligible, extractKg }))
        : action === "save"
          ? JSON.stringify(fields)
          : undefined;
      await memoryApi<MemoryRecord>(path, {
        method: action === "save" ? "PATCH" : "POST",
        body,
        signal: controller.signal,
      });
      if (controller.signal.aborted || !mountedRef.current) return;
      setEditingId(null);
      setRefresh((value) => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted && mountedRef.current) {
        setError(toUserMessage(cause));
      }
    } finally {
      mutationControllersRef.current.delete(controller);
      if (mountedRef.current) setBusyId(null);
    }
  }

  // 多领域基准库(Task 14 追加项):targetBaseId 未传时按 promotionTargetFor(memory)
  // 三态分派——none(0 个公共库挂载)拒绝、auto(1 个)直接用、choose(>1 个)转去
  // 弹选择器,选好后选择器自己会带着 targetBaseId 回调本函数,这一次不再重新
  // 分派,直接提交。与 page.tsx::submitPromotion 同一套规则(不另写一份判定);
  // global 视图(P2 #2 修复)现在也走这同一套三态,不再是"不作前置判定直接提交"。
  async function promoteMemory(memory: MemoryRecord, targetBaseId?: string) {
    if (busyId || sessionSignal.aborted || !canPromoteMemory(memory)) return;
    if (!targetBaseId) {
      const target = promotionTargetFor(memory);
      if (target.kind === "none") {
        setError("需先挂载一个公共知识库，才能贡献内容");
        return;
      }
      if (target.kind === "choose") {
        setPendingPromotionMemory(memory);
        return;
      }
      targetBaseId = target.baseId;
    }
    const controller = new AbortController();
    mutationControllersRef.current.add(controller);
    setBusyId(memory.id);
    setError("");
    setTransferNotice(null);
    try {
      await memoryApi(memoryPromotionPath(memory.id), {
        method: "POST",
        // 不传时不带 body——与后端 PromoteRequest 的可选请求体默认值对称,
        // 镜像 promotion-queue.ts::proposePromotion 的既有惯例。
        ...(targetBaseId ? { body: JSON.stringify({ target_base_id: targetBaseId }) } : {}),
        signal: controller.signal,
      });
      if (!controller.signal.aborted && mountedRef.current) {
        setRefresh((value) => value + 1);
      }
    } catch (cause) {
      if (!controller.signal.aborted && mountedRef.current) {
        setError(toUserMessage(cause));
      }
    } finally {
      mutationControllersRef.current.delete(controller);
      if (mountedRef.current) setBusyId(null);
    }
  }

  // C4「复制/移动到…」：单条与批量共用同一个提交函数——两者最终都是同一个
  // POST /memories/transfer 请求(数组里放 1 个或多个 id)，不像
  // deleteMemory/bulkDeleteMemories 那样拆成两个函数——那个拆分的理由(两个不
  // 同的端点)在这里不成立。busyId 用 ids.length > 1 区分"占住批量栏"还是
  // "占住这一张卡"，与 bulkDeleteMemories 用 "__bulk__" 哨兵同一个道理。
  //
  // onSubmit 的契约(transfer-picker.tsx 头注释)：resolve 才代表"可以卸载
  // modal 了"——批量级失败(如目标笔记本不可写/不存在，后端 4xx)必须让它继续
  // 抛出、不能在这里吞掉，好让 DestinationPicker 自己的 catch 接管错误态、解锁
  // 「确认」按钮重试；只有 200 成功之后才关 modal、清选中、刷新列表——这里没
  // 有 try/catch 包住 await 本身正是为了让异常原样穿透出去。复审 Important 3
  // 修复：忙碌/会话已中止时同样不能静默 return——那会让 onSubmit resolve 却
  // 什么都没发生，而 DestinationPicker 自己不会在 resolve 时复位 busy(它的
  // 约定正是"resolve=调用方接管卸载"，见 transfer-picker.tsx submit() 里的
  // 注释)，modal 会永久卡在"处理中…"：两个按钮都 disabled，也没有背景点击/
  // Escape 兜底，用户只能刷新页面才能脱困。busyId 这半边在正常使用中是可达
  // 的：卡片级按钮只用 busyId===该条 memory.id 判 disabled，所以 memory A 忙
  // 着时 memory B 的「复制/移动到…」仍然点得开、能打开这个弹窗。改成 throw
  // 让它落进 DestinationPicker 自己的 catch(会显示这条消息并重新点亮
  // 「确认」)，一次改动同时兜住 busyId 和 sessionSignal.aborted 两种情形。
  //
  // 200 之内逐条结果用 summarizeTransferResults 拆成若干类，分别落进
  // transferNotice 的 warning/sourceChanged/failure 字段，特意不写回全局 error
  // state：下面 setRefresh 触发的重新拉取 effect 自己会在起手处 setError("")
  // (见该 effect 顶部)，如果复用 error 存这条消息，会在同一批状态更新后几乎
  // 立刻被那次清空冲掉——用户根本来不及看见，AMENDMENT 2 要求的"可见提示"就
  // 形同虚设。transferNotice 不挂在那个 effect 上，只在下一次别的写操作开始
  // 时才清空(与 knowhow-panel.tsx 里 actionError 的清空方式同一套)，所以能
  // 稳定留到用户看见为止。复审 Minor 补充，round 10 P2 从"仅 copy"扩到
  // copy/move 都设置：批量结果里没有 warning/sourceChanged/promotionBlocked/
  // failure(即全部成功)就落一条 transferNotice.success，带上目标笔记本名字
  // ——原先只在 mode==="copy" 时设置，理由是"move 的成功靠列表刷新自然可
  // 见"，但那对未过滤的全局视图不成立：卡片不显示 notebook 归属，一次成功
  // 的移动会让列表刷新后卡片原样还在(只是换了笔记本)，用户拿不到任何确认。
  async function submitTransfer(
    ids: string[],
    targetNotebookId: string,
    mode: TransferMode,
    extractKg: boolean,
    targetNotebookName: string,
  ) {
    if (busyId || sessionSignal.aborted) {
      throw humanizedError("有其它操作正在进行，请稍后重试");
    }
    setBusyId(ids.length > 1 ? "__bulk__" : ids[0]);
    setError("");
    setTransferNotice(null);
    try {
      const { results } = await transferMemories(ids, targetNotebookId, mode, extractKg);
      if (sessionSignal.aborted || !mountedRef.current) return;
      setPendingTransfer(null);
      setSelectedIds(new Set());
      setRefresh((value) => value + 1);
      const summary = summarizeTransferResults(results);
      // round 10 P1-B：copiedSourceNotRemoved 现在按 error_code 拆成两个互
      // 斥的展示桶——sourceChanged（源被有意保留，绝不能建议删除）和
      // "剩下的那部分"即 cleanupErrorCount（清理操作本身出错，源确实原封
      // 未动，弃用/删除都安全）。两者相加恒等于 copiedSourceNotRemoved.length
      // （sourceChanged 是它的子集），所以不需要再单独一个"总数"字段。
      const cleanupErrorCount = summary.copiedSourceNotRemoved.length - summary.sourceChanged.length;
      // round 8 P2-A：promotionBlocked 也从"普通失败"计数里摘出去——它和
      // copiedSourceNotRemoved 一样不可重试，不该被算进"N 条失败，请稍后
      // 重试"那句会误导用户去点"重试"的通用文案。
      const plainFailedCount =
        summary.failed - summary.copiedSourceNotRemoved.length - summary.promotionBlocked.length;
      if (
        summary.copiedSourceNotRemoved.length > 0 ||
        summary.promotionBlocked.length > 0 ||
        plainFailedCount > 0
      ) {
        // 逐条失败原因是后端 str(exc)(memory_service.transfer 内层 except 的
        // 兜底文案,见 backend/app/services/memory_service.py)——不是产品文案,
        // 不能拼进用户可见的 failure 串。只送 console 排障,用户看不带原文的
        // 通用汇总(同 report-view.tsx 的 activeError 一个模式)。promotion_
        // proposed 那条的原文也在这批里(它的 status 仍是 "failed")，走的
        // 是同一条既有诊断出口，不需要单独处理。
        const failedReasons = Array.from(new Set(
          results
            .map((result) => (result.status === "failed" ? result.error : null))
            .filter((reason): reason is string => Boolean(reason))
        ));
        if (failedReasons.length > 0) logDiagnostic("memory-transfer", failedReasons.join("；"));
        setTransferNotice({
          // round 10 P1-B：这条消息现在只覆盖 cleanupErrorCount（真正的清理
          // 失败）——绝不能再建议"删除"：MemoryService.delete 只删行本身，
          // 不会清理派生的 KG 源（只有 deprecate 会），照做会让这条已经搬去
          // 目标的 Memory 在源 notebook 的知识图谱里继续可被检索到，变成一
          // 个搜得到、点不开的幽灵。
          warning: cleanupErrorCount > 0
            ? `${cleanupErrorCount} 条已复制到目标笔记本，但源未能自动清理。建议将源记忆标记为「弃用」（会一并清理其知识图谱关联），或重新发起一次移动；直接删除不会清理知识图谱，可能留下可搜索的残留。`
            : null,
          // round 10 P1-B：源不是清理失败——是在复制完成后被并发编辑/状态
          // 变更/提交了审批提案而被有意保留下来的，目标里的副本才是那次改
          // 动之前的旧版本。绝不能建议删除源（会连带永久丢失那份改动）。
          sourceChanged: summary.sourceChanged.length > 0
            ? `${summary.sourceChanged.length} 条源记忆有更新的修改已被保留；请核对后删除目标笔记本里的旧副本，或重新发起一次移动。`
            : null,
          promotionBlocked: summary.promotionBlocked.length > 0
            ? `${summary.promotionBlocked.length} 条正在公共知识库审批中，暂不能移动；请先在待确认中心处理提案。`
            : null,
          failure: plainFailedCount > 0
            ? `${plainFailedCount} 条复制/移动失败，请稍后重试`
            : null,
          success: null,
        });
      } else if (summary.succeeded > 0) {
        const verb = mode === "move" ? "移动" : "复制";
        setTransferNotice({
          warning: null,
          sourceChanged: null,
          promotionBlocked: null,
          failure: null,
          success: summary.succeeded > 1
            ? `已${verb} ${summary.succeeded} 条记忆到「${targetNotebookName}」`
            : `已${verb}到「${targetNotebookName}」`,
        });
      }
    } finally {
      if (mountedRef.current) setBusyId(null);
    }
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    setPage(0);
    setQuery(queryDraft.trim());
  }

  // 选择器弹窗展示的候选:按弹窗当前挂起的那条记忆(而非某个固定 scope 级
  // 别的值)解析——global 视图下不同记忆的候选集本就可能不同。
  const pendingPromotionTarget = pendingPromotionMemory
    ? promotionTargetFor(pendingPromotionMemory)
    : null;
  const pendingPromotionOptions =
    pendingPromotionTarget?.kind === "choose" ? pendingPromotionTarget.options : [];

  return (
    <section className={`memory-panel memory-panel-${scope}`} aria-label={scope === "global" ? "全部记忆" : "笔记本记忆"}>
      <header className="memory-panel-heading">
        <div>
          <h1>{scope === "global" ? "记忆" : "笔记本记忆"}</h1>
          <p>{scope === "global" ? "跨笔记本管理你主动保存和待审核的私有记忆。" : "只显示你自己绑定到当前笔记本的私有记忆。"}</p>
        </div>
        <span className="memory-total">
          {scope === "global" ? `${ownerTotal} 条 · ${ownerPending} 条待确认` : `${total} 条`}
        </span>
      </header>

      {scope === "global" && <AgentAccessManager sessionSignal={sessionSignal} />}

      <div className="memory-filterbar">
        <form className="memory-search" onSubmit={submitSearch}>
          <Search size={16} />
          <input
            type="search"
            value={queryDraft}
            placeholder="搜索标题、内容或标签"
            onChange={(event) => setQueryDraft(event.target.value)}
          />
          <button type="submit">搜索</button>
        </form>
        {scope === "global" && (
          <label>
            笔记本
            <select value={notebookFilter} onChange={(event) => { setNotebookFilter(event.target.value); setPage(0); }}>
              <option value="">全部笔记本</option>
              {notebookOptions.map((option) => (
                <option key={option.notebook_id} value={option.notebook_id}>
                  {option.name}（{option.memory_count}）
                </option>
              ))}
            </select>
          </label>
        )}
        <label>
          状态
          <select value={status} onChange={(event) => { setStatus(event.target.value as MemoryStatus | "all"); setPage(0); }}>
            <option value="all">全部</option>
            <option value="candidate">待确认</option>
            <option value="confirmed">已确认</option>
            <option value="rejected">已拒绝</option>
            <option value="deprecated">已停用</option>
          </select>
        </label>
        <label>
          来源
          <select value={origin} onChange={(event) => { setOrigin(event.target.value as MemoryOrigin | "all"); setPage(0); }}>
            <option value="all">全部</option>
            <option value="ask_answer">Ask 回答</option>
            <option value="external_agent">Agent 提议</option>
          </select>
        </label>
      </div>

      <div className="memory-toolbar">
        <button
          type="button"
          className={`memory-select-toggle ${selectionMode ? "active" : ""}`}
          aria-pressed={selectionMode}
          onClick={() => { setSelectionMode((value) => !value); setSelectedIds(new Set()); }}
        >
          <CheckSquare size={15} /> {selectionMode ? "退出选择" : "批量选择"}
        </button>
        <div className="memory-layout-switch" role="group" aria-label="布局方式">
          <button type="button" className={layout === "grid" ? "active" : ""} aria-pressed={layout === "grid"} title="网格视图" onClick={() => setLayout("grid")}><LayoutGrid size={16} /></button>
          <button type="button" className={layout === "list" ? "active" : ""} aria-pressed={layout === "list"} title="列表视图" onClick={() => setLayout("list")}><List size={16} /></button>
        </div>
      </div>

      {selectionMode && (
        <div className="memory-bulk-bar">
          <span>已选 {selectedIds.size} / {items.length} 项</span>
          <button type="button" onClick={() => setSelectedIds(new Set(items.map((item) => item.id)))}>全选本页</button>
          <button type="button" onClick={() => setSelectedIds(new Set())}>清空</button>
          {/* AMENDMENT 1：批量传输要求所选全部同源(DestinationPicker 的
              sourceNotebookId 是单数，用来在目标候选里排除源自身)——全局
              Memory 视图的多选可以跨 notebook，真跨了源就没有"唯一源"这回事，
              打开选择器前先用 singleSourceNotebookId 拦截，不悄悄拿第一条的
              notebook 顶上。
              选中态跨页存活(selectedIds 不随 page/filter 变化清空，"全选本页"
              这个命名本身就暗示还有别的页)，但 items 只装当前页——要拿
              notebook_id 判同源，只能先从 items 里解析出选中项，若跨页选择导
              致解不全(chosen.length !== selectedIds.size)，宁可拦下也不能悄悄
              只传当前页能看到的那一部分：按钮上明明写着"（{selectedIds.size}）"，
              真传输的却只是 chosen 这个子集，用户完全不会发现少传了。
              bulkDeleteMemories 不用管这个，因为它直接把整个 selectedIds 送后
              端、不需要在前端解析 notebook_id。
              P2-B（round 6 评审）：批量选择走 checkbox，不像单条卡片操作区
              那样天生只在 confirmed 卡片上才有传输入口——用户能选中
              candidate/rejected/deprecated 一起点，后端会把这些逐条报成
              per-item failed（可预见、能在前端拦下来的失败）。顺序固定：
              先过跨页完整性守卫、再过 confirmedOnly 状态过滤、最后才过
              singleSourceNotebookId——过滤之后的子集才是真正要传输的，同源
              校验必须对着这个子集做，不是对着未过滤的 chosen。 */}
          <button
            type="button"
            className="memory-bulk-transfer"
            disabled={selectedIds.size === 0 || Boolean(busyId)}
            onClick={() => {
              const chosen = items.filter((item) => selectedIds.has(item.id));
              if (chosen.length !== selectedIds.size) {
                setError("批量复制/移动只能处理当前页面内的选中项，部分选中的记忆不在本页，请分页分别操作");
                setTransferNotice(null);
                return;
              }
              const confirmed = confirmedOnly(chosen);
              if (confirmed.length === 0) {
                setError("所选记忆均非已确认状态，只有已确认的记忆才能复制/移动");
                setTransferNotice(null);
                return;
              }
              const sourceNotebookId = singleSourceNotebookId(confirmed);
              if (!sourceNotebookId) {
                setError("批量复制/移动要求所选记忆属于同一个笔记本，请重新选择");
                setTransferNotice(null);
                return;
              }
              setTransferExtractKg(true);
              setPendingTransfer({
                ids: confirmed.map((item) => item.id),
                sourceNotebookId,
                excludedCount: chosen.length - confirmed.length,
              });
            }}
          >
            <Copy size={14} /> 复制/移动到…（{selectedIds.size}）
          </button>
          <button
            type="button"
            className="danger memory-bulk-delete"
            disabled={selectedIds.size === 0 || Boolean(busyId)}
            onClick={() => setPendingDelete({ kind: "bulk" })}
          >
            <Trash2 size={14} /> 删除选中（{selectedIds.size}）
          </button>
        </div>
      )}

      {error && <div className="memory-error" role="alert">{error}</div>}
      {/* AMENDMENT 2：copied_source_not_removed 不能混进普通失败——它是"副本已
          落地，只是源没删掉"，引导用户重试只会在目标堆更多重复副本。两个提示各
          用独立 state、不同色调渲染，与下面普通失败横幅（复用既有 .memory-error
          红色调）视觉上明确分开。round 8 P2-A：promotionBlocked 是同一原则的
          第三条分支——正在公共知识库审批中，重试同样无效，需要指向待确认中心
          而不是"稍后重试"。round 10 P1-B：sourceChanged 是第四条分支——源不是
          清理失败，是被有意保留下来保护一份并发改动的，指引和 warning（真正的
          清理失败）截然不同，不能合并成一条消息。四者共用同一套非警示色的
          .memory-transfer-notice 视觉（都是"已知原因、非报错"的提示，不是
          failure 那种需要用户重新操作的错误态）。 */}
      {transferNotice?.warning && (
        <div className="memory-transfer-notice" role="status">{transferNotice.warning}</div>
      )}
      {transferNotice?.sourceChanged && (
        <div className="memory-transfer-notice" role="status">{transferNotice.sourceChanged}</div>
      )}
      {transferNotice?.promotionBlocked && (
        <div className="memory-transfer-notice" role="status">{transferNotice.promotionBlocked}</div>
      )}
      {transferNotice?.failure && (
        <div className="memory-error" role="alert">{transferNotice.failure}</div>
      )}
      {transferNotice?.success && (
        <div className="memory-transfer-notice" role="status">{transferNotice.success}</div>
      )}
      {loading ? (
        <div className="memory-empty">正在读取你的记忆…</div>
      ) : items.length === 0 ? (
        <div className="memory-empty">
          <Sparkles size={26} />
          <strong>还没有符合条件的记忆</strong>
          <p>可在 Ask 回答下方手动保存，Agent 提议则会先进入待确认状态。</p>
        </div>
      ) : (
        <div className={`memory-list ${layout === "list" ? "is-list" : ""}`}>
          {items.map((memory) => {
            const statusMeta = memoryStatusMeta(memory.status);
            const originMeta = memoryOriginMeta(memory.origin);
            const editing = editingId === memory.id;
            const busy = busyId === memory.id;
            const promotionTarget = promotionTargetFor(memory);
            const provenanceRows = memoryProvenanceRows(memory);
            const citationReferences = memoryCitationReferences(memory);
            const evidenceRows = memoryEvidenceRows(memory);
            const isExpanded = expandedIds.has(memory.id);
            const detailBlocks = (
              <>
                {provenanceRows.length > 0 && (
                  <details className="memory-provenance">
                    <summary><ChevronDown size={14} /> 来源与依据</summary>
                    <dl>
                      {provenanceRows.map(([label, value]) => (
                        <div key={label}><dt>{label}</dt><dd>{value}</dd></div>
                      ))}
                    </dl>
                  </details>
                )}
                {citationReferences.length > 0 && (
                  <details className="memory-provenance memory-citations">
                    <summary><ChevronDown size={14} /> 原文引用（{citationReferences.length}）</summary>
                    <div className="memory-citation-list">
                      {citationReferences.map((citation, index) => (
                        <article key={`${memory.id}-citation-${citation.sourceId}-${citation.elementId}-${index}`}>
                          <div>
                            <strong>{citation.sourceTitle || citation.sourceFileName || `来源 ${index + 1}`}</strong>
                            {citation.locationLabel && <span>{citation.locationLabel}</span>}
                            {citation.sourceFileName && citation.sourceFileName !== citation.sourceTitle && (
                              <span>原始文件：{citation.sourceFileName}</span>
                            )}
                          </div>
                          {citation.quotedSpan && <p>{citation.quotedSpan}</p>}
                          {citation.archival ? (
                            <small>该引用来自传输前的笔记本，仅作来源存档。</small>
                          ) : onOpenSource && citation.sourceId && citation.elementId ? (
                            <button
                              type="button"
                              onClick={() => onOpenSource(citation.sourceId, citation.elementId)}
                            >查看原文</button>
                          ) : null}
                        </article>
                      ))}
                    </div>
                  </details>
                )}
                {evidenceRows.length > 0 && (
                  <details className="memory-provenance memory-evidence-review" open={memory.status === "candidate" && editing}>
                    <summary><ChevronDown size={14} /> Agent 证据引用（{evidenceRows.length}）</summary>
                    <div className="memory-evidence-list">
                      {evidenceRows.map((evidence, index) => (
                        <article key={`${memory.id}-evidence-${index}`} className={evidence.trusted ? "validated" : "invalid"}>
                          <strong>{index + 1}. {label(EVIDENCE_TYPE, evidence.type, "未知来源")}</strong>
                          <code title={evidence.identity}>{evidence.identity.slice(0, 12)}…</code>
                          <span>{label(EVIDENCE_STATUS, evidence.status, "未能核对")}</span>
                        </article>
                      ))}
                    </div>
                  </details>
                )}
              </>
            );
            return (
              <article className={`memory-card memory-${memory.status}`} key={memory.id}>
                <div className="memory-card-head">
                  {selectionMode && (
                    <input
                      type="checkbox"
                      className="memory-select-box"
                      checked={selectedIds.has(memory.id)}
                      onChange={() => toggleSelected(memory.id)}
                      aria-label={`选择「${memory.title}」`}
                    />
                  )}
                  <div className="memory-card-badges">
                    <span className={`memory-badge tone-${statusMeta.tone}`}>{statusMeta.label}</span>
                    <span className={`memory-badge tone-${originMeta.tone}`}>
                      {memory.origin === "external_agent" ? <Bot size={13} /> : <Sparkles size={13} />}
                      {originMeta.label}
                    </span>
                  </div>
                  {canEditMemory(memory.status) && !editing && (
                    <button type="button" className="memory-icon-action" onClick={() => beginEdit(memory)}>
                      <Edit3 size={15} /> {memory.status === "candidate" ? "审核" : "编辑"}
                    </button>
                  )}
                </div>

                {editing ? (
                  <>
                    <MemoryEditor draft={draft} setDraft={setDraft} />
                    {memory.status === "candidate" && kgEligible && (
                      <label className="agent-check">
                        <input
                          type="checkbox"
                          checked={extractKg}
                          onChange={(event) => setExtractKg(event.target.checked)}
                        />
                        同时整理进知识图谱
                      </label>
                    )}
                    {detailBlocks}
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      className="memory-card-title"
                      aria-expanded={isExpanded}
                      onClick={() => toggleExpanded(memory.id)}
                    >
                      <h2>{memory.title}</h2>
                      <ChevronDown size={18} className="memory-card-caret" aria-hidden="true" />
                    </button>
                    {memory.tags.length > 0 && (
                      <div className="memory-tags">{memory.tags.map((tag) => <span key={tag}>#{tag}</span>)}</div>
                    )}
                    {isExpanded && (
                      <>
                        <MemoryBody content={memory.content_md} />
                        {detailBlocks}
                      </>
                    )}
                  </>
                )}

                <footer className="memory-card-footer">
                  <time dateTime={memory.updated_at}>更新于 {new Date(memory.updated_at).toLocaleString("zh-CN")}</time>
                  {!editing && (
                    <div className="memory-card-actions">
                      {memory.status === "confirmed" && (
                        <>
                          {canPromoteMemory(memory) ? (
                            <button
                              type="button"
                              className="memory-promote-action"
                              disabled={busy || promotionTarget.kind === "none"}
                              title={promotionTarget.kind === "none" ? "需先挂载一个公共知识库，才能贡献内容" : undefined}
                              onClick={() => promoteMemory(memory)}
                            >
                              <ArrowUpCircle size={14} /> 贡献到公共知识库
                            </button>
                          ) : (
                            <span className={`memory-promotion-state state-${memory.promotion_state}`}>
                              {memoryPromotionLabel(memory.promotion_state)}
                            </span>
                          )}
                          {/* C4：只有 confirmed 才能传输——candidate/rejected/
                              deprecated 传过去后端会把该条报成 per-item failed
                              (transfer-model.ts TransferStatus 注释)，与其让用户
                              点了才看到失败，不如干脆不给入口。 */}
                          <button
                            type="button"
                            className="memory-transfer-action"
                            disabled={busy}
                            onClick={() => {
                              setTransferExtractKg(true);
                              setPendingTransfer({ ids: [memory.id], sourceNotebookId: memory.notebook_id });
                            }}
                          >
                            <Copy size={14} /> 复制/移动到…
                          </button>
                        </>
                      )}
                      <button
                        type="button"
                        className="memory-delete-action"
                        disabled={busy}
                        onClick={() => setPendingDelete({ kind: "single", memory })}
                      >
                        <Trash2 size={14} /> 删除
                      </button>
                    </div>
                  )}
                  {editing && (
                    <div className="memory-actions">
                      <button type="button" disabled={busy} onClick={() => setEditingId(null)}><X size={14} /> 取消</button>
                      {memory.status === "candidate" ? (
                        <>
                          <button type="button" disabled={busy} className="danger" onClick={() => updateMemory(memory, "reject")}><Trash2 size={14} /> 拒绝</button>
                          <button type="button" disabled={busy} onClick={() => updateMemory(memory, "save")}><Edit3 size={14} /> 保存草稿</button>
                          <button type="button" disabled={busy || !draft.title.trim() || !draft.content_md.trim()} className="primary" onClick={() => updateMemory(memory, "confirm")}><Check size={14} /> 确认收录</button>
                        </>
                      ) : (
                        <>
                          <button type="button" disabled={busy} className="danger" onClick={() => updateMemory(memory, "deprecate")}><Trash2 size={14} /> 停用</button>
                          <button type="button" disabled={busy || !draft.title.trim() || !draft.content_md.trim()} className="primary" onClick={() => updateMemory(memory, "save")}><Check size={14} /> 保存</button>
                        </>
                      )}
                    </div>
                  )}
                </footer>
              </article>
            );
          })}
        </div>
      )}
      <Pagination page={page} pageSize={MEMORY_PAGE_SIZE} total={total} busy={loading} onPage={setPage} />
      {pendingDelete && (
        <div className="utility-modal utility-modal-top" role="dialog" aria-modal="true" aria-label="确认删除">
          <FloatingModalCard storageKey="memory.delete.window" className="utility-modal-card memory-confirm-card">
            {(floating) => (<>
            <h3 {...floating.dragHandleProps}>{pendingDelete.kind === "bulk" ? `删除选中的 ${selectedIds.size} 条记忆？` : "删除这条记忆？"}</h3>
            <p>删除后不可恢复。</p>
            <div className="memory-dialog-actions">
              <button type="button" disabled={Boolean(busyId)} onClick={() => setPendingDelete(null)}>取消</button>
              <button type="button" className="danger" disabled={Boolean(busyId)} onClick={confirmDelete}>删除</button>
            </div>
            </>)}
          </FloatingModalCard>
        </div>
      )}
      {/* C4「复制/移动到…」：单条来自卡片操作区(source=该条自己的
          notebook_id)，批量来自选中集合(先经 singleSourceNotebookId 拦截跨源，
          见批量按钮 onClick)。allowMove 恒为 true——Memory 传输的权限边界不像
          knowhow 表那样需要区分"只读也能复制"：能在这个列表里看到某条 Memory
          就必然是它的 owner(私有 Memory 不做只读共享)，目标笔记本是否可写由
          后端 /memories/transfer 自己校验，前端不需要另开一道 canEdit 门。
          P2-B：pendingTransfer.ids 在批量入口已经过 confirmedOnly 过滤，标题
          里的数字天然就是"真正会传输的条数"；excludedCount>0 时额外带一句
          提示，告诉用户按钮上看到的选中数和这里的数字对不上是因为剔除了非
          已确认状态，不是漏传了。 */}
      {pendingTransfer && (
        <DestinationPicker
          sourceNotebookId={pendingTransfer.sourceNotebookId}
          allowMove
          // Memory transfer 的目标走 user_can_access_notebook（owner-only）——组管理员
          // 可管理的共享库接收会 404,所以**不**放行(默认 false),免得给出必失败的目标
          // (P2-T2 评审 P2-4)。若将来把 Memory transfer 目标也放宽到 admin,这里同改。
          allowManagedTargets={false}
          title={
            pendingTransfer.excludedCount
              ? `复制/移动 ${pendingTransfer.ids.length} 条记忆（已排除 ${pendingTransfer.excludedCount} 条非已确认状态）`
              : `复制/移动 ${pendingTransfer.ids.length} 条记忆`
          }
          // Important 4（复审）：spec §5.2.6/§9/§12 要求的opt-out——checkbox
          // 状态由本组件持有(transferExtractKg)，picker 只负责渲染+回调,两处
          // 打开入口都已把它重置为默认 true(匹配 confirm 时的默认行为)。
          showExtractKg
          extractKg={transferExtractKg}
          onExtractKgChange={setTransferExtractKg}
          onCancel={() => setPendingTransfer(null)}
          onSubmit={(targetNotebookId, mode, targetNotebookName) =>
            submitTransfer(pendingTransfer.ids, targetNotebookId, mode, transferExtractKg, targetNotebookName)
          }
        />
      )}
      {pendingPromotionMemory && (
        <section
          className="utility-modal"
          role="dialog"
          aria-modal="true"
          onClick={(event) => { if (event.currentTarget === event.target) setPendingPromotionMemory(null); }}
        >
          <FloatingModalCard storageKey="memory.promotion.window" className="utility-modal-card narrow">
            {(floating) => (<>
            <div className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>选择贡献目标</h2>
                <p>这条记忆所在的笔记本挂载了多个公共知识库，请选择要进入哪一个。</p>
              </div>
              <button className="icon-button" onClick={() => setPendingPromotionMemory(null)} title="Close">×</button>
            </div>
            <div className="promotion-target-list">
              {pendingPromotionOptions.map((base) => (
                <button
                  key={base.id}
                  type="button"
                  className="sort-button promotion-target-option"
                  onClick={() => {
                    const memory = pendingPromotionMemory;
                    setPendingPromotionMemory(null);
                    if (memory) {
                      promoteMemory(memory, base.id).catch(
                        (cause) => setError(toUserMessage(cause, "提交失败，请稍后重试")),
                      );
                    }
                  }}
                >
                  <span className="promotion-target-name" title={base.name}>{base.name}</span>
                </button>
              ))}
            </div>
            </>)}
          </FloatingModalCard>
        </section>
      )}
    </section>
  );
}

export function MemorySaveDialog({
  answerId,
  notebookId,
  onClose,
  onSaved,
  sessionSignal,
  interactive = true,
  zIndex,
}: {
  answerId: string;
  notebookId: string;
  onClose: () => void;
  onSaved: (memory: MemoryRecord) => void;
  sessionSignal: AbortSignal;
  interactive?: boolean;
  zIndex?: number;
}) {
  const [draft, setDraft] = useState<MemoryDraft>({ title: "", content_md: "", tags: "" });
  const [provenance, setProvenance] = useState<Record<string, unknown>>({});
  const [previewEligible, setPreviewEligible] = useState(false);
  const [extractKg, setExtractKg] = useState(true);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const requestEpochRef = useRef(0);
  const mountedRef = useRef(true);
  const previewControllerRef = useRef<AbortController | null>(null);
  const saveControllerRef = useRef<AbortController | null>(null);
  const floating = useFloatingWindow({ storageKey: "memory.save.window", resizable: false });

  useEffect(() => subscribeMemorySessionAbort(sessionSignal, () => {
    previewControllerRef.current?.abort();
    saveControllerRef.current?.abort();
  }), [sessionSignal]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      saveControllerRef.current?.abort();
      saveControllerRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (sessionSignal.aborted) return;
    const epoch = ++requestEpochRef.current;
    const controller = new AbortController();
    previewControllerRef.current = controller;
    setLoading(true);
    memoryApi<MemoryPreview>(`/answers/${encodeURIComponent(answerId)}/memory-preview`, {
      method: "POST",
      signal: controller.signal,
    })
      .then((preview) => {
        if (requestEpochRef.current !== epoch) return;
        setDraft(draftFor(preview));
        setProvenance(preview.provenance_summary);
        setPreviewEligible(preview.kg_extract_eligible ?? false);
      })
      .catch((cause) => {
        if (!controller.signal.aborted && requestEpochRef.current === epoch) {
          setError(toUserMessage(cause, "加载失败，请稍后重试"));
        }
      })
      .finally(() => {
        if (requestEpochRef.current === epoch) setLoading(false);
      });
    return () => {
      controller.abort();
      if (previewControllerRef.current === controller) previewControllerRef.current = null;
      if (requestEpochRef.current === epoch) requestEpochRef.current += 1;
    };
  }, [answerId, sessionSignal]);

  async function save() {
    if (saving || sessionSignal.aborted || !draft.title.trim() || !draft.content_md.trim()) return;
    const controller = new AbortController();
    saveControllerRef.current = controller;
    setSaving(true);
    setError("");
    try {
      const validationError = validateMemoryDraft({
        title: draft.title,
        content_md: draft.content_md,
        tags: tagsFromDraft(draft.tags),
      });
      if (validationError) {
        setError(validationError);
        return;
      }
      const memory = await memoryApi<MemoryRecord>(`/notebooks/${encodeURIComponent(notebookId)}/memories/from-answer`, {
        method: "POST",
        body: JSON.stringify(fromAnswerMemoryBody({
          answerId,
          title: draft.title.trim(),
          content_md: draft.content_md.trim(),
          tags: tagsFromDraft(draft.tags),
          eligible: previewEligible,
          extractKg,
        })),
        signal: controller.signal,
      });
      if (!controller.signal.aborted && mountedRef.current) onSaved(memory);
    } catch (cause) {
      if (!controller.signal.aborted && mountedRef.current) {
        setError(toUserMessage(cause));
      }
    } finally {
      if (saveControllerRef.current === controller) saveControllerRef.current = null;
      if (mountedRef.current) setSaving(false);
    }
  }

  return (
    <section className="utility-modal memory-save-modal" role="dialog" aria-modal={interactive} aria-hidden={!interactive} inert={interactive ? undefined : true} aria-label="保存回答到记忆" style={{ zIndex }}>
      <div ref={floating.cardRef} className="utility-modal-card memory-save-card" style={floating.style}>
        <header {...floating.dragHandleProps}>
          <div className="memory-save-title">
            <span className="memory-save-icon"><Bookmark size={16} /></span>
            <div>
              <h2>保存到记忆</h2>
              <p>预览并编辑后，确认才会写入你的私有记忆。</p>
            </div>
          </div>
          <button type="button" className="memory-close" onClick={onClose} aria-label="关闭" disabled={saving}><X size={18} /></button>
        </header>
        {loading ? <div className="memory-empty compact">正在生成预览…</div> : (
          <>
            <MemoryEditor draft={draft} setDraft={setDraft} />
            {previewEligible && (
              <div className="memory-preview-provenance">
                <label className="agent-check">
                  <input
                    type="checkbox"
                    checked={extractKg}
                    onChange={(event) => setExtractKg(event.target.checked)}
                  />
                  同时整理进知识图谱
                </label>
              </div>
            )}
            <div className="memory-preview-provenance">
              <strong>来源摘要</strong>
              <span>依据：{label(EVIDENCE_LEVEL, String(provenance.evidence_level ?? ""), "未知")}</span>
              <span>引用：{String(provenance.citation_count ?? 0)} 条</span>
            </div>
          </>
        )}
        {error && <div className="memory-error" role="alert">{error}</div>}
        <footer className="memory-dialog-actions">
          <button type="button" disabled={saving} onClick={onClose}>取消</button>
          <button type="button" className="primary" disabled={loading || saving || !draft.title.trim() || !draft.content_md.trim()} onClick={() => save()}>
            <Check size={15} /> {saving ? "保存中…" : "确认保存"}
          </button>
        </footer>
      </div>
    </section>
  );
}
