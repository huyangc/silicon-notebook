"use client";

import {
  ArrowLeft,
  BookOpen,
  ChevronRight,
  Inbox,
  Library,
  Link,
  Plus,
  Search,
  Settings,
  ShieldCheck,
  Users,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { toUserMessage } from "./errors.ts";
import { copyTextSafely } from "./copy-text.ts";
import { useCopyResult } from "./copy-result.ts";
import {
  approveShareRequest,
  createGroup,
  createGroupInvite,
  buildGroupInviteLink,
  creatableGroupKinds,
  deleteGroup,
  foldGroupShares,
  getGroup,
  getGroupInvite,
  grantGroupAdminsManage,
  GROUP_INPUT_LIMITS,
  groupKindLabel,
  groupRoleLabel,
  isGroupAdmin,
  leaveGroup,
  listGroupShareRequests,
  listGroupSharedNotebooks,
  listGroups,
  listMyPendingShareRequests,
  listNotebookGrants,
  manageGrantIds,
  putGroupMember,
  rejectShareRequest,
  removeGroupMember,
  revokeGroupInvite,
  resolveUser,
  revokeGroupSharedNotebook,
  revokeNotebookGrant,
  shareNotebookToGroup,
  rotateGroupInvite,
  transferGroupOwner,
  updateGroup,
  withdrawShareRequest,
  type GroupDetail,
  type GroupInviteState,
  type GroupSharedNotebook,
  type GroupSummary,
  type GroupPageTab,
  type ShareRequest,
} from "./group-api.ts";
import type { NotebookSummary } from "./workspace-model.ts";

type GroupsPageProps = {
  currentUserId: string;
  isSystemAdmin: boolean;
  notebooks: NotebookSummary[];
  initialGroupId?: string;
  initialTab?: GroupPageTab;
  onBack: () => void;
  onChanged: () => void;
  /** 正在打开的笔记本 id(与集合页 `openingNotebookId` 同一份状态的镜像)。
   * 群组页的「打开笔记本」入口与集合页的卡片是同一个动作,忙碌反馈必须同权。 */
  openingNotebookId: string | null;
  onOpenNotebook: (notebookId: string) => void;
  onNavigate: (groupId: string, tab: GroupPageTab) => void;
};

const TABS: Array<{ id: GroupPageTab; label: string; icon: typeof BookOpen }> = [
  { id: "notebooks", label: "知识库", icon: BookOpen },
  { id: "members", label: "成员", icon: Users },
  { id: "requests", label: "共享申请", icon: Inbox },
  { id: "settings", label: "设置", icon: Settings },
];

function EmptyState({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <div className="group-page-empty">
      <span className="group-page-empty-icon">{icon}</span>
      <strong>{title}</strong>
      <p>{children}</p>
    </div>
  );
}

function memberLabel(member: GroupDetail["members"][number]): string {
  return member.display_name
    ? `${member.display_name}（${member.username}）`
    : member.username;
}

export function GroupsPage({
  currentUserId,
  isSystemAdmin,
  notebooks,
  initialGroupId = "",
  initialTab = "notebooks",
  onBack,
  onChanged,
  openingNotebookId,
  onOpenNotebook,
  onNavigate,
}: GroupsPageProps) {
  const [scope, setScope] = useState<"mine" | "all">("mine");
  const [groups, setGroups] = useState<GroupSummary[] | null>(null);
  const [detail, setDetail] = useState<GroupDetail | null>(null);
  const [shared, setShared] = useState<GroupSharedNotebook[] | null>(null);
  const [requests, setRequests] = useState<ShareRequest[] | null>(null);
  const [invite, setInvite] = useState<GroupInviteState | null>(null);
  const [myRequests, setMyRequests] = useState<ShareRequest[]>([]);
  const [tab, setTab] = useState<GroupPageTab>(initialTab);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState("project");
  const [newDescription, setNewDescription] = useState("");
  const [memberName, setMemberName] = useState("");
  const [renameDraft, setRenameDraft] = useState("");
  const [descriptionDraft, setDescriptionDraft] = useState("");
  const [notebookQuery, setNotebookQuery] = useState("");
  const [pickedNotebooks, setPickedNotebooks] = useState<Set<string>>(new Set());
  const [grantManage, setGrantManage] = useState(false);
  const [transferTarget, setTransferTarget] = useState("");
  const [confirming, setConfirming] = useState("");
  // 复制结果必须落在按钮自身:唯一的反馈曾经是页面顶部那条 notice 横幅,它在长页面上
  // 会滚出视口,于是「复制」看起来像没点上。置位与回 idle 由共享的 useCopyResult 负责。
  const inviteCopy = useCopyResult();
  const inviteLinkRef = useRef<HTMLInputElement | null>(null);
  const loadEpoch = useRef(0);
  const removedGroupIds = useRef(new Set<string>());

  const kinds = creatableGroupKinds(isSystemAdmin ? "admin" : "user");
  const canManage = Boolean(detail && (isGroupAdmin(detail) || isSystemAdmin));
  const isOwner = Boolean(detail && (detail.owner_id === currentUserId || isSystemAdmin));

  const refreshGroups = useCallback(async (which: "mine" | "all") => {
    const items = await listGroups(which);
    setGroups(items);
    return items;
  }, []);

  const loadGroup = useCallback(async (groupId: string, nextTab?: GroupPageTab) => {
    const epoch = ++loadEpoch.current;
    setError("");
    setNotice("");
    setShared(null);
    setRequests(null);
    setInvite(null);
    setPickedNotebooks(new Set());
    setConfirming("");
    try {
      const group = await getGroup(groupId);
      if (epoch !== loadEpoch.current) return;
      setDetail(group);
      setRenameDraft(group.name);
      setDescriptionDraft(group.description);
      const manageable = isGroupAdmin(group) || isSystemAdmin;
      const [inventory, queue, invitation] = await Promise.all([
        listGroupSharedNotebooks(groupId),
        manageable ? listGroupShareRequests(groupId) : Promise.resolve([]),
        manageable ? getGroupInvite(groupId) : Promise.resolve(null),
      ]);
      if (epoch !== loadEpoch.current) return;
      setShared(inventory);
      setRequests(queue);
      setInvite(invitation);
      if (nextTab) setTab(nextTab);
    } catch (err) {
      if (epoch === loadEpoch.current) {
        setDetail(null);
        setShared([]);
        setRequests([]);
        setInvite(null);
        setError(toUserMessage(err, "群组详情加载失败"));
      }
    }
  }, [isSystemAdmin]);

  useEffect(() => {
    let cancelled = false;
    setGroups(null);
    listGroups(scope)
      .then((items) => {
        if (cancelled) return;
        setGroups(items);
        const requested = initialGroupId && items.some((item) => item.id === initialGroupId)
          ? initialGroupId
          : detail?.id && items.some((item) => item.id === detail.id)
            ? detail.id
            : items[0]?.id ?? "";
        if (requested && requested !== detail?.id) void loadGroup(requested, initialTab);
      })
      .catch((err) => {
        if (!cancelled) {
          setGroups([]);
          setError(toUserMessage(err, "群组清单加载失败"));
        }
      });
    return () => { cancelled = true; };
  }, [scope]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    listMyPendingShareRequests().then(setMyRequests).catch(() => setMyRequests([]));
  }, []);

  // Hash/back-forward navigation can change the selected tab or group without
  // remounting this page. Keep the independent page addressable at that exact
  // state instead of only honoring the hash on first entry.
  useEffect(() => {
    if (!initialGroupId) {
      setTab(initialTab);
      return;
    }
    if (detail?.id === initialGroupId) {
      setTab(initialTab);
      return;
    }
    if (
      !removedGroupIds.current.has(initialGroupId)
      && groups?.some((group) => group.id === initialGroupId)
    ) {
      void loadGroup(initialGroupId, initialTab);
    }
  }, [detail?.id, groups, initialGroupId, initialTab, loadGroup]);

  async function run(key: string, action: () => Promise<void>, fallback: string) {
    setBusy(key);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (err) {
      setError(toUserMessage(err, fallback));
    } finally {
      setBusy("");
    }
  }

  function chooseGroup(groupId: string) {
    setTab("notebooks");
    onNavigate(groupId, "notebooks");
    void loadGroup(groupId, "notebooks");
  }

  function chooseTab(next: GroupPageTab) {
    setTab(next);
    if (detail) onNavigate(detail.id, next);
  }

  async function selectAfterGroupRemoval(removedGroupId: string) {
    removedGroupIds.current.add(removedGroupId);
    loadEpoch.current += 1;
    setGroups((items) => items?.filter((item) => item.id !== removedGroupId) ?? items);
    onNavigate("", "notebooks");
    const remaining = await refreshGroups(scope);
    const next = remaining[0];
    if (!next) {
      return;
    }
    onNavigate(next.id, "notebooks");
    await loadGroup(next.id, "notebooks");
  }

  const sharedIds = useMemo(
    () => new Set((shared ?? []).map((item) => item.notebook_id)),
    [shared],
  );
  const manageableNotebooks = useMemo(
    () => notebooks.filter((item) => item.access !== "reader" || Boolean(item.can_manage_content)),
    [notebooks],
  );
  const manageableById = useMemo(
    () => new Map(manageableNotebooks.map((item) => [item.id, item])),
    [manageableNotebooks],
  );
  const candidates = useMemo(() => {
    const needle = notebookQuery.trim().toLocaleLowerCase();
    return manageableNotebooks.filter((item) => (
      !sharedIds.has(item.id)
      && (!needle || `${item.name} ${item.purpose} ${item.primary_domain}`.toLocaleLowerCase().includes(needle))
    ));
  }, [manageableNotebooks, notebookQuery, sharedIds]);

  const ownerMember = detail?.members.find((member) => member.id === detail.owner_id);
  const pendingCount = requests?.length ?? 0;

  return (
    <main className="page group-page">
      {/* 页面标题「群组」在顶栏(page.tsx 的 .brand-title,就在 SN 徽标旁边),这里不再重复。
          返回控件与笔记本工作区共用 .back-home-button —— 同一个动作(showCollection)不该
          在两个页面上长成两种样子。 */}
      <header className="group-page-head">
        <button className="back-home-button" onClick={onBack}>
          <ArrowLeft size={16} />
          <span>返回主页</span>
        </button>
        <button className="new-pill" onClick={() => setCreating((value) => !value)}>
          <Plus size={16} /> 新建群组
        </button>
      </header>

      {error && <p className="password-change-status error group-page-status">{error}</p>}
      {notice && <p className="group-page-status success">{notice}</p>}

      {creating && (
        <section className="group-page-create">
          <div>
            <h2>新建群组</h2>
            <p>创建者将成为首位群组 owner，后续可在设置中转让。</p>
          </div>
          <div className="group-page-create-form">
            <input
              value={newName}
              maxLength={GROUP_INPUT_LIMITS.nameMaxChars}
              placeholder="群组名称"
              aria-label="群组名称"
              onChange={(event) => setNewName(event.target.value)}
            />
            {kinds.length > 1 && (
              <select value={newKind} aria-label="群组分类" onChange={(event) => setNewKind(event.target.value)}>
                {kinds.map((kind) => <option value={kind} key={kind}>{groupKindLabel(kind)}</option>)}
              </select>
            )}
            <textarea
              value={newDescription}
              maxLength={GROUP_INPUT_LIMITS.descriptionMaxChars}
              rows={2}
              placeholder="群组说明（可选）"
              aria-label="新群组的说明"
              onChange={(event) => setNewDescription(event.target.value)}
            />
            <div className="group-page-create-actions">
              <button className="sort-button" onClick={() => setCreating(false)}>取消</button>
              <button className="new-pill" disabled={Boolean(busy) || !newName.trim()}
                onClick={() => { void run("create", async () => {
                  const created = await createGroup(newName.trim(), kinds.length > 1 ? newKind : "project", newDescription.trim());
                  setNewName(""); setNewDescription(""); setCreating(false);
                  await refreshGroups(scope);
                  await loadGroup(created.id, "notebooks");
                  onNavigate(created.id, "notebooks");
                  onChanged();
                }, "创建群组失败"); }}>
                {busy === "create" ? "创建中…" : "创建群组"}
              </button>
            </div>
          </div>
        </section>
      )}

      <section className="group-page-shell">
        <aside className="group-page-sidebar">
          <div className="group-page-sidebar-head">
            <h2>{scope === "all" ? "全部群组" : "我的群组"}</h2>
            {groups && <span className="group-count-chip">{groups.length}</span>}
          </div>
          {isSystemAdmin && (
            <div className="group-page-scope" role="tablist" aria-label="群组范围">
              <button className={scope === "mine" ? "active" : ""} onClick={() => setScope("mine")}>我加入的</button>
              <button className={scope === "all" ? "active" : ""} onClick={() => setScope("all")}>全部</button>
            </div>
          )}
          <div className="group-page-group-list">
            {groups === null && <p className="tool-hint">加载中…</p>}
            {groups?.length === 0 && (
              <EmptyState icon={<Users size={20} />} title="还没有群组">新建一个项目群组，或等待管理员邀请。</EmptyState>
            )}
            {groups?.map((group) => (
              <button key={group.id} className={`group-page-group-card ${detail?.id === group.id ? "active" : ""}`}
                onClick={() => chooseGroup(group.id)}>
                <span className="group-page-group-icon">{group.name.trim().slice(0, 1).toUpperCase() || "G"}</span>
                <span className="group-page-group-copy">
                  <strong>{group.name}</strong>
                  <small>{groupKindLabel(group.kind)} · {group.member_count} 人 · {group.owner_id === currentUserId ? "Owner" : groupRoleLabel(group.my_role)}</small>
                </span>
                <ChevronRight size={16} />
              </button>
            ))}
          </div>
        </aside>

        <div className="group-page-workspace">
          {!detail ? (
            <EmptyState icon={<ShieldCheck size={24} />} title="选择一个群组">从左侧选择群组，查看它的知识库、成员和共享申请。</EmptyState>
          ) : (<>
            <header className="group-page-detail-head">
              <div className="group-page-detail-id">
                <div className="group-page-avatar">{detail.name.trim().slice(0, 1).toUpperCase() || "G"}</div>
                <div className="group-page-detail-copy">
                  <div className="group-page-title-row">
                    <h2>{detail.name}</h2>
                    <span className="group-chip">{groupKindLabel(detail.kind)}</span>
                    {detail.owner_id === currentUserId && <span className="group-owner-chip">Owner</span>}
                  </div>
                  <p>{detail.description || "这个群组还没有填写说明。"}</p>
                  <span>Owner {ownerMember ? memberLabel(ownerMember) : "—"}</span>
                </div>
              </div>
              {/* 三个计数全部来自本页已经取回的数据（共享清单 / 群组详情 / 审批队列），
                  不新增任何请求。「待审批」只对有审批权的人显示——普通成员根本不加载
                  队列，给他看一个恒为 0 的格子只会让人以为审批过了。 */}
              <dl className="group-stat-row">
                <div><dt>知识库</dt><dd>{shared === null ? "—" : shared.length}</dd></div>
                <div><dt>成员</dt><dd>{detail.member_count}</dd></div>
                {canManage && (
                  <div className={pendingCount > 0 ? "alert" : ""}>
                    <dt>待审批</dt>
                    <dd>{requests === null ? "—" : pendingCount}</dd>
                  </div>
                )}
              </dl>
            </header>

            <nav className="group-page-tabs" aria-label="群组管理">
              {TABS.map((item) => {
                const Icon = item.icon;
                return <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => chooseTab(item.id)}>
                  <Icon size={16} />{item.label}
                  {item.id === "requests" && pendingCount > 0 && <span>{pendingCount}</span>}
                </button>;
              })}
            </nav>

            <div className="group-page-panel">
              {tab === "notebooks" && (<>
                <div className="group-page-section-head">
                  <div>
                    <h3>群组知识库</h3>
                    <p>成员可以打开、提问、写自己的深度报告，并挂载为参考库。</p>
                  </div>
                  {shared && <span className="group-count-chip">{shared.length}</span>}
                </div>
                {shared === null ? <p className="tool-hint">加载中…</p> : shared.length === 0 ? (
                  <EmptyState icon={<BookOpen size={22} />} title="还没有群组知识库">{canManage ? "从下方选择你有权管理的笔记本。" : "等待群组管理员添加知识库。"}</EmptyState>
                ) : <div className="group-notebook-grid">{shared.map((item) => {
                  const adminsManage = item.roles.includes("admin");
                  const ICanManageNotebook = manageableById.has(item.notebook_id);
                  const opening = openingNotebookId === item.notebook_id;
                  return <article className="group-notebook-card" key={item.notebook_id}>
                    {/* 群组页与集合页同权的忙碌反馈:命中即刻禁用 + spinner + 「打开中…」,
                        不再是「按下整页就切走所以不用管」的已知余量。 */}
                    <button
                      className={`group-notebook-open${opening ? " is-opening" : ""}`}
                      aria-busy={opening || undefined}
                      disabled={opening}
                      onClick={() => onOpenNotebook(item.notebook_id)}
                    >
                      <span className="group-notebook-mark"><Library size={19} /></span>
                      <span>
                        <strong>{item.name}</strong>
                        <small>所有者 {item.owner_username || "—"}</small>
                        {opening && (
                          <small className="group-notebook-open-status">
                            <span className="notebook-card-open-spinner" aria-hidden="true" />
                            打开中…
                          </small>
                        )}
                      </span>
                    </button>
                    <div className="group-notebook-meta">
                      <span className={adminsManage ? "group-access-chip manage" : "group-access-chip"}>
                        {adminsManage ? "组管理员可管理" : "成员可查看"}
                      </span>
                      {canManage && ICanManageNotebook && !adminsManage && <button className="sort-button" disabled={Boolean(busy)}
                        onClick={() => { void run(`grant:${item.notebook_id}`, async () => {
                          await grantGroupAdminsManage(item.notebook_id, detail.id);
                          setShared(await listGroupSharedNotebooks(detail.id)); onChanged();
                        }, "开启管理权限失败"); }}>允许管理员管理</button>}
                      {canManage && ICanManageNotebook && adminsManage && <button className="sort-button" disabled={Boolean(busy)}
                        onClick={() => { void run(`drop:${item.notebook_id}`, async () => {
                          const entry = foldGroupShares(await listNotebookGrants(item.notebook_id)).find((share) => share.groupId === detail.id);
                          if (entry) for (const grantId of manageGrantIds(entry)) await revokeNotebookGrant(item.notebook_id, grantId);
                          setShared(await listGroupSharedNotebooks(detail.id)); onChanged();
                        }, "取消管理权限失败"); }}>改为仅查看</button>}
                      {canManage && (confirming === `revoke:${item.notebook_id}` ? <>
                        <button className="new-pill danger-pill compact" disabled={Boolean(busy)} onClick={() => { void run(`revoke:${item.notebook_id}`, async () => {
                          await revokeGroupSharedNotebook(detail.id, item.notebook_id);
                          setConfirming(""); setShared(await listGroupSharedNotebooks(detail.id)); onChanged();
                        }, "撤销共享失败"); }}>确认撤销</button>
                        <button className="sort-button" onClick={() => setConfirming("")}>取消</button>
                      </> : <button className="sort-button danger-text" onClick={() => setConfirming(`revoke:${item.notebook_id}`)}>撤销</button>)}
                    </div>
                  </article>;
                })}</div>}

                {canManage && <section className="group-add-notebooks">
                  <div className="group-page-section-head compact">
                    <div>
                      <h3>添加知识库</h3>
                      <p>这里只显示你有管理权、且尚未共享给本组的笔记本。</p>
                    </div>
                  </div>
                  <label className="group-page-search"><Search size={16} /><input value={notebookQuery} placeholder="搜索可添加的笔记本"
                    onChange={(event) => setNotebookQuery(event.target.value)} /></label>
                  {candidates.length === 0 ? <p className="group-inline-empty">没有符合条件的笔记本。</p> : <div className="group-candidate-list">
                    {candidates.map((item) => <label className="group-candidate-row" key={item.id}>
                      <input type="checkbox" checked={pickedNotebooks.has(item.id)} onChange={(event) => {
                        setPickedNotebooks((current) => { const next = new Set(current); if (event.target.checked) next.add(item.id); else next.delete(item.id); return next; });
                      }} />
                      <span><strong>{item.name}</strong><small>{item.primary_domain || item.purpose || "未填写说明"}</small></span>
                    </label>)}
                  </div>}
                  <div className="group-add-actions">
                    <label><input type="checkbox" checked={grantManage} onChange={(event) => setGrantManage(event.target.checked)} /> 同时允许组管理员管理内容</label>
                    <button className="new-pill" disabled={Boolean(busy) || pickedNotebooks.size === 0}
                      onClick={() => { void run("add-notebooks", async () => {
                        const ids = Array.from(pickedNotebooks);
                        const results = await Promise.allSettled(ids.map((id) => shareNotebookToGroup(id, detail.id, { manage: grantManage })));
                        const failed = results.filter((result) => result.status === "rejected").length;
                        setPickedNotebooks(new Set()); setShared(await listGroupSharedNotebooks(detail.id)); onChanged();
                        setNotice(failed ? `已添加 ${ids.length - failed} 本，${failed} 本未能添加。` : `已添加 ${ids.length} 本知识库。`);
                      }, "添加知识库失败"); }}>{busy === "add-notebooks" ? "添加中…" : `添加已选（${pickedNotebooks.size}）`}</button>
                  </div>
                </section>}
              </>)}

              {tab === "members" && (<>
                <div className="group-page-section-head">
                  <div>
                    <h3>成员与角色</h3>
                    <p>Owner 唯一且不可被降级或移出；管理员负责日常成员与知识库管理。</p>
                  </div>
                  <span className="group-count-chip">{detail.members.length}</span>
                </div>
                {canManage && <section className="group-settings-card group-invite-card">
                  <div className="group-invite-title"><span className="group-page-empty-icon"><Link size={20} /></span><div><h4>邀请链接</h4><p>拿到链接的登录用户会自动以普通成员身份加入。链接可重复使用，撤销或重新生成后旧链接立即失效。</p></div></div>
                  {invite === null ? <p className="tool-hint">邀请链接加载中…</p> : invite.active ? <>
                    <label>当前邀请链接<div className="group-invite-link"><input ref={inviteLinkRef} readOnly value={buildGroupInviteLink(invite.token, window.location.origin)} onFocus={(event) => event.currentTarget.select()} /><button className={inviteCopy.resultFor(invite.token) === "copied" ? "new-pill copy-result-copied" : inviteCopy.resultFor(invite.token) === "failed" ? "new-pill copy-result-failed" : "new-pill"} disabled={Boolean(busy)} onClick={() => { void run("copy-invite", async () => {
                      const link = buildGroupInviteLink(invite.token, window.location.origin);
                      const copied = await copyTextSafely(link);
                      // key 用 token 而不是固定串:结果要挂 1.6s,期间切群组或重新生成
                      // 链接都会让这颗按钮指向另一条链接,固定串会让新链接顶着上一条的
                      // 「已复制」出现(codex #612 R2 P2)。
                      inviteCopy.report(invite.token, copied);
                      // 复制没成到剪贴板时，把链接选中——用户当场就能 ⌘C/Ctrl+C，
                      // 不用先看懂提示再自己去框选。
                      //
                      // ⚠ 先核对框里还是不是**这条**链接。剪贴板那一步可以挂很久(权限
                      // 提示、非安全上下文),期间侧栏没有禁用,用户切了群组 ref 就指向新
                      // 群组的输入框——旧的失败去选中它,用户 ⌘C 拿到的是**另一个群组**的
                      // 邀请链接(codex #612 R3 P2)。比对 value 而不是比对节点:React 会
                      // 把同位置同类型的 <input> 复用给新群组,节点相等骗不过去。
                      const linkInput = inviteLinkRef.current;
                      const stillThisLink = linkInput?.value === link;
                      if (!copied && stillThisLink) { linkInput.focus(); linkInput.select(); }
                      setNotice(copied
                        ? "邀请链接已复制。"
                        : stillThisLink ? "复制失败，链接已选中，请手动复制。" : "复制失败，请手动复制链接。");
                    }, "复制邀请链接失败"); }}>{busy === "copy-invite" ? "复制中…" : inviteCopy.resultFor(invite.token) === "copied" ? "已复制" : inviteCopy.resultFor(invite.token) === "failed" ? "复制失败" : "复制"}</button></div></label>
                    {confirming === "rotate-invite" ? <div className="group-inline-confirm"><span>重新生成后，旧链接会立即失效。</span><button className="new-pill" disabled={Boolean(busy)} onClick={() => { void run("rotate-invite", async () => {
                      setInvite(await rotateGroupInvite(detail.id)); setConfirming(""); setNotice("已重新生成邀请链接，旧链接已失效。");
                    }, "重新生成邀请链接失败"); }}>确认重新生成</button><button className="sort-button" onClick={() => setConfirming("")}>取消</button></div> : <div className="group-invite-actions"><button className="sort-button" disabled={Boolean(busy)} onClick={() => setConfirming("rotate-invite")}>重新生成</button><button className="sort-button danger-text" disabled={Boolean(busy)} onClick={() => { void run("revoke-invite", async () => {
                      await revokeGroupInvite(detail.id); setInvite({ active: false, token: "", created_at: null }); setNotice("邀请链接已撤销。");
                    }, "撤销邀请链接失败"); }}>撤销链接</button></div>}
                  </> : <div className="group-invite-actions"><span className="tool-hint">当前没有生效中的邀请链接。</span><button className="new-pill" disabled={Boolean(busy)} onClick={() => { void run("create-invite", async () => {
                    setInvite(await createGroupInvite(detail.id)); setNotice("邀请链接已生成。");
                  }, "生成邀请链接失败"); }}>生成邀请链接</button></div>}
                </section>}
                <div className="group-member-list">{detail.members.map((member) => {
                  const owner = member.id === detail.owner_id;
                  return <div className="group-member-row" key={member.id}>
                    <span className="group-member-avatar">{member.username.slice(0, 1).toUpperCase()}</span>
                    <span className="group-member-copy"><strong>{memberLabel(member)}</strong><small>{owner ? "群组所有者" : groupRoleLabel(member.role)}</small></span>
                    {owner ? <span className="group-owner-chip">Owner</span> : canManage ? <select value={member.role} disabled={Boolean(busy)} aria-label={`${member.username} 的角色`}
                      onChange={(event) => { void run(`role:${member.id}`, async () => {
                        const updated = await putGroupMember(detail.id, member.id, event.target.value); setDetail(updated); await refreshGroups(scope);
                      }, "角色修改失败"); }}><option value="member">成员</option><option value="admin">组管理员</option></select> : <span className="group-chip">{groupRoleLabel(member.role)}</span>}
                    {canManage && !owner && (confirming === `remove:${member.id}` ? <div className="group-inline-confirm">
                      <span>移出后将失去本组知识库访问权。</span><button className="new-pill danger-pill compact" onClick={() => { void run(`remove:${member.id}`, async () => {
                        await removeGroupMember(detail.id, member.id); setDetail(await getGroup(detail.id)); setConfirming(""); await refreshGroups(scope); onChanged();
                      }, "移除成员失败"); }}>确认移出</button><button className="sort-button" onClick={() => setConfirming("")}>取消</button>
                    </div> : <button className="sort-button danger-text" onClick={() => setConfirming(`remove:${member.id}`)}>移出</button>)}
                  </div>;
                })}</div>
                {canManage && <div className="group-add-member"><input value={memberName} placeholder="输入完整用户名" aria-label="要添加的用户名"
                  onChange={(event) => setMemberName(event.target.value)} /><button className="new-pill" disabled={Boolean(busy) || !memberName.trim()}
                  onClick={() => { void run("add-member", async () => { const user = await resolveUser(memberName.trim()); setDetail(await putGroupMember(detail.id, user.id, "member")); setMemberName(""); await refreshGroups(scope); onChanged(); }, "添加成员失败"); }}>添加成员</button></div>}
              </>)}

              {tab === "requests" && (<>
                <div className="group-page-section-head">
                  <div>
                    <h3>待审批贡献</h3>
                    <p>批准后笔记本先以成员可查看方式进入群组，管理权可由笔记本管理者另行开启。</p>
                  </div>
                  {pendingCount > 0 && <span className="group-count-chip">{pendingCount}</span>}
                </div>
                {!canManage ? <EmptyState icon={<Inbox size={22} />} title="没有审批权限">只有 owner 和组管理员可以处理贡献申请。</EmptyState>
                  : requests === null ? <p className="tool-hint">加载中…</p> : requests.length === 0 ? <EmptyState icon={<Inbox size={22} />} title="没有待审批申请">新的成员贡献申请会出现在这里。</EmptyState>
                    : <div className="group-request-list">{requests.map((request) => <div className="group-request-row" key={request.id}>
                      <span className="group-request-mark"><BookOpen size={17} /></span>
                      <span className="group-request-copy"><strong>{request.notebook_name || "知识库"}</strong><small>申请人 {request.requested_by_username}</small></span>
                      <div><button className="new-pill" disabled={Boolean(busy)} onClick={() => { void run(`approve:${request.id}`, async () => {
                        await approveShareRequest(detail.id, request.id); setRequests(await listGroupShareRequests(detail.id)); setShared(await listGroupSharedNotebooks(detail.id)); onChanged();
                      }, "批准申请失败"); }}>批准</button><button className="sort-button" disabled={Boolean(busy)} onClick={() => { void run(`reject:${request.id}`, async () => {
                        await rejectShareRequest(detail.id, request.id); setRequests(await listGroupShareRequests(detail.id));
                      }, "驳回申请失败"); }}>驳回</button></div>
                    </div>)}</div>}
                {myRequests.filter((request) => request.group_id === detail.id).length > 0 && <section className="group-my-requests"><h3>我发起的申请</h3>{myRequests.filter((request) => request.group_id === detail.id).map((request) => <div className="group-request-row" key={request.id}>
                  <span className="group-request-mark"><BookOpen size={17} /></span>
                  <span className="group-request-copy"><strong>{request.notebook_name || "名称不再显示的知识库"}</strong><small>等待群组管理员审批</small></span>
                  <button className="sort-button" disabled={Boolean(busy)} onClick={() => { void run(`withdraw:${request.id}`, async () => {
                    await withdrawShareRequest(request.notebook_id, request.id); setMyRequests((items) => items.filter((item) => item.id !== request.id));
                  }, "撤回申请失败"); }}>撤回</button>
                </div>)}</section>}
              </>)}

              {tab === "settings" && (<>
                <div className="group-page-section-head">
                  <div>
                    <h3>群组设置</h3>
                    <p>日常资料与所有权操作分区处理。</p>
                  </div>
                </div>
                {canManage ? <section className="group-settings-card"><h4>基本信息</h4><label>群组名称<input value={renameDraft} maxLength={GROUP_INPUT_LIMITS.nameMaxChars} onChange={(event) => setRenameDraft(event.target.value)} /></label>
                  <label>群组说明<textarea rows={3} value={descriptionDraft} maxLength={GROUP_INPUT_LIMITS.descriptionMaxChars} onChange={(event) => setDescriptionDraft(event.target.value)} /></label>
                  <div className="group-settings-actions"><button className="new-pill" disabled={Boolean(busy) || !renameDraft.trim()} onClick={() => { void run("save", async () => {
                    const updated = await updateGroup(detail.id, renameDraft.trim(), descriptionDraft); setDetail(updated); await refreshGroups(scope); onChanged(); setNotice("群组信息已保存。");
                  }, "保存群组信息失败"); }}>保存群组信息</button></div></section> : <p className="tool-hint">只有 owner 和组管理员可以编辑群组信息。</p>}

                {isOwner && <section className="group-settings-card"><h4>转让群组</h4><p>新 owner 必须是现有成员，将自动成为管理员；你转让后仍保留管理员身份。</p>
                  <select value={transferTarget} onChange={(event) => setTransferTarget(event.target.value)} aria-label="选择新的群组所有者"><option value="">选择成员…</option>{detail.members.filter((member) => member.id !== detail.owner_id).map((member) => <option value={member.id} key={member.id}>{memberLabel(member)}</option>)}</select>
                  <div className="group-settings-actions">{confirming === "transfer" ? <div className="group-inline-confirm"><span>确认把群组所有权转让给所选成员？</span><button className="new-pill" disabled={Boolean(busy) || !transferTarget} onClick={() => { void run("transfer", async () => {
                    const transferredOwnGroup = detail.owner_id === currentUserId;
                    const updated = await transferGroupOwner(detail.id, transferTarget); setDetail(updated); setTransferTarget(""); setConfirming(""); await refreshGroups(scope);
                    setNotice(transferredOwnGroup ? "群组所有权已转让，你仍是组管理员。" : "群组所有权已转让。");
                  }, "转让群组失败"); }}>确认转让</button><button className="sort-button" onClick={() => setConfirming("")}>取消</button></div>
                    : <button className="sort-button" disabled={!transferTarget} onClick={() => setConfirming("transfer")}>转让群组</button>}</div>
                </section>}

                <section className="group-settings-card danger"><h4>{isOwner ? "删除群组" : "退出群组"}</h4>
                  {isOwner ? <p>删除会收回成员对全部群组知识库的访问权；知识库本身仍属于原作者，不会删除。</p> : <p>退出后，你将失去经本群组获得的知识库访问权。</p>}
                  <div className="group-settings-actions">{isOwner ? (confirming === "delete" ? <div className="group-inline-confirm"><button className="new-pill danger-pill" onClick={() => { void run("delete", async () => {
                    const removedGroupId = detail.id; await deleteGroup(removedGroupId); setDetail(null); setShared(null); setRequests(null); setConfirming(""); await selectAfterGroupRemoval(removedGroupId); onChanged();
                  }, "删除群组失败"); }}>确认删除群组</button><button className="sort-button" onClick={() => setConfirming("")}>取消</button></div> : <button className="new-pill danger-pill" onClick={() => setConfirming("delete")}>删除群组</button>)
                    : (confirming === "leave" ? <div className="group-inline-confirm"><button className="new-pill danger-pill" onClick={() => { void run("leave", async () => {
                      const removedGroupId = detail.id; await leaveGroup(removedGroupId); setDetail(null); setShared(null); setRequests(null); setConfirming(""); await selectAfterGroupRemoval(removedGroupId); onChanged();
                    }, "退出群组失败"); }}>确认退出</button><button className="sort-button" onClick={() => setConfirming("")}>取消</button></div> : <button className="sort-button danger-text" onClick={() => setConfirming("leave")}>退出群组</button>)}</div>
                </section>
              </>)}
            </div>
          </>)}
        </div>
      </section>
    </main>
  );
}
