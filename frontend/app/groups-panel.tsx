"use client";

import { useCallback, useEffect, useState } from "react";

import { toUserMessage } from "./errors.ts";
import { FloatingModalCard } from "./floating-modal-card.tsx";
import {
  approveShareRequest,
  createGroup,
  creatableGroupKinds,
  GROUP_INPUT_LIMITS,
  deleteGroup,
  getGroup,
  groupLengthHint,
  groupKindLabel,
  groupRoleLabel,
  isGroupAdmin,
  leaveGroup,
  listGroupShareRequests,
  listGroupSharedNotebooks,
  listGroups,
  listMyPendingShareRequests,
  putGroupMember,
  rejectShareRequest,
  removeGroupMember,
  resolveUser,
  revokeGroupSharedNotebook,
  updateGroup,
  withdrawShareRequest,
  type GroupDetail,
  type GroupSharedNotebook,
  type GroupSummary,
  type ShareRequest,
} from "./group-api.ts";

/** 快到长度上限时才出声的余量提示;还早的时候一个字都不渲染。 */
function LengthHint({ value, max, label }: { value: string; max: number; label: string }) {
  const hint = groupLengthHint(value, max);
  return hint ? <p className="tool-hint" style={{ margin: 0 }}>{label}{hint}</p> : null;
}


type GroupsModalProps = {
  /** 系统管理员:能建部门/领域组,并有「全部群组」这一档运维视图。 */
  isSystemAdmin: boolean;
  /** 撤销共享会改变别人的笔记本列表——本人的列表也可能因此变化,让外层重取。 */
  onChanged: () => void;
  onClose: () => void;
};

/**
 * 群组管理弹窗(群组知识共享 P1-T4)。
 *
 * 一个弹窗装完整条链:我加入的群组 → 建组 → 组详情(成员增删改、自助退出)→
 * 共享给本组的知识库(组管理员可撤销)。刻意**不**为详情再开第二个浮动窗:两层
 * 浮动窗互相遮挡,而这条链上每一步都要看着上一步的清单做决定。
 *
 * 三条与后端口径直接相关的界面决定:
 *
 * 1. **写动作只对组管理员显示**。判据是 `my_role === "admin"`(`isGroupAdmin`),
 *    与后端守卫同一个字段。这不是权限判定——权威判定在后端,而且非组管理员在那边
 *    拿到的是 404(与「组不存在」同一个响应)。前端收起入口只是不给一个必然失败的
 *    按钮。
 * 2. **系统管理员的「全部群组」视图里 `my_role` 会是空串**。后端如实回报「他不是
 *    成员」,但运维旁路让他仍能管理。所以管理入口的判据是
 *    `isGroupAdmin(group) || isSystemAdmin`,而「退出群组」只按 `my_role` 非空
 *    显示——旁路刻意不覆盖自助退出(退出的前提是本人真的在组里)。
 * 3. **删组与移除成员是两步确认**,不是 `window.confirm`:删组会连带清掉指向它的
 *    全部共享授权,爆炸半径大到不该由一次误点决定。
 */
export function GroupsModal({ isSystemAdmin, onChanged, onClose }: GroupsModalProps) {
  const [scope, setScope] = useState<"mine" | "all">("mine");
  const [groups, setGroups] = useState<GroupSummary[] | null>(null);
  const [detail, setDetail] = useState<GroupDetail | null>(null);
  const [shared, setShared] = useState<GroupSharedNotebook[] | null>(null);
  const [shareRequests, setShareRequests] = useState<ShareRequest[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  /** 中性说明(**不是**错误):改动已经生效,只是这一屏没跟上。 */
  const [notice, setNotice] = useState("");
  /** 我发起的、仍待审批的申请(全局,不依赖任何笔记本权限)。 */
  const [myRequests, setMyRequests] = useState<ShareRequest[] | null>(null);

  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState("project");
  const [newDescription, setNewDescription] = useState("");
  const [memberName, setMemberName] = useState("");
  const [renameDraft, setRenameDraft] = useState("");
  const [descriptionDraft, setDescriptionDraft] = useState("");
  const [confirming, setConfirming] = useState("");

  const kinds = creatableGroupKinds(isSystemAdmin ? "admin" : "user");

  const refreshGroups = useCallback(async (which: "mine" | "all") => {
    setGroups(await listGroups(which));
  }, []);

  useEffect(() => {
    let cancelled = false;
    setGroups(null);
    listGroups(scope)
      .then((list) => { if (!cancelled) setGroups(list); })
      .catch((err) => { if (!cancelled) setError(toUserMessage(err, "群组清单加载失败")); });
    return () => { cancelled = true; };
  }, [scope]);

  // 我发起的待审批申请 —— 与 `scope` 无关(它不是群组维度的东西),所以单独一次加载。
  // ⚠ 失败**不**报错:这一节是给失权申请人的补救入口,它自己挂了不该盖住整个群组面板。
  useEffect(() => {
    let cancelled = false;
    listMyPendingShareRequests()
      .then((list) => { if (!cancelled) setMyRequests(list); })
      .catch(() => { if (!cancelled) setMyRequests([]); });
    return () => { cancelled = true; };
  }, []);

  // 每个写动作都走它:统一收敛忙碌位、错误文案与「动作之后重取哪几份数据」。
  async function run(action: () => Promise<void>, fallback: string) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
    } catch (err) {
      setError(toUserMessage(err, fallback));
    } finally {
      setBusy(false);
    }
  }

  /**
   * 「先把改动做掉,再去对账」——两段**分开**报(codex #519 R11 P2)。
   *
   * 批准/驳回都是**已经提交成功**的服务端改动。此前它们和随后的两三个列表请求同在一个
   * `run` 里,于是刷新任一失败就报「批准申请失败」、那条已批准的行还留在界面上,用户照着
   * 提示重试 —— 而它已经不是 pending 了,重试必然 404。两件事必须分开说:「批准失败」该让
   * 人重试,「已批准但列表没刷出来」只该让人刷新。
   *
   * ⚠ 不是把刷新错误吞掉:对账失败给中性说明(它不是故障,是视图落后了),与
   * `notebook-group-share` 那处「重取被拒 → 清空清单 + 中性说明」同口径。
   */
  async function mutateThenReconcile(
    mutate: () => Promise<void>,
    reconcile: () => Promise<void>,
    mutationFallback: string,
    staleNotice: string,
  ) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await mutate();
    } catch (err) {
      setError(toUserMessage(err, mutationFallback));
      setBusy(false);
      return;
    }
    try {
      await reconcile();
    } catch {
      // 改动已经生效,只是这一屏没跟上。绝不能报成「失败」——那会把人骗去重试一个
      // 已经完成的动作(重试拿 404,因为它已经不是待审批状态了)。
      setNotice(staleNotice);
    } finally {
      setBusy(false);
    }
  }

  async function openDetail(groupId: string) {
    setConfirming("");
    setMemberName("");
    // ⚠ **先把上一个组的详情与共享清单清空**,再去取新的。不清的话,详情请求失败时
    // 屏幕上会留着甲组的成员和「共享给本组的知识库」,而标题下面接的是刚点开的乙组
    // ——那一排「撤销共享」按钮打的是乙组,列的却是甲组的库。清空之后失败就是一片
    // 空白 + 一句错误,读起来是「没打开」,而不是「打开了别的组的内容」。
    setDetail(null);
    setShared(null);
    setShareRequests(null);
    await run(async () => {
      const group = await getGroup(groupId);
      setDetail(group);
      setRenameDraft(group.name);
      setDescriptionDraft(group.description);
      // 「共享给本组的知识库」与「待审批申请」都只有组管理员读得到(非管理员 404),
      // 所以对普通成员连查都不查——不是藏起来,是那两条清单本来就不属于他这一层。
      const manageable = isGroupAdmin(group) || isSystemAdmin;
      setShared(manageable ? await listGroupSharedNotebooks(groupId) : null);
      setShareRequests(manageable ? await listGroupShareRequests(groupId) : null);
    }, "群组详情加载失败");
  }

  const canManage = detail ? isGroupAdmin(detail) || isSystemAdmin : false;

  return (
    <section className="utility-modal" role="dialog" aria-modal="true">
      <FloatingModalCard storageKey="groups.window" className="utility-modal-card">
        {(floating) => (<>
        <div className="source-modal-header" {...floating.dragHandleProps}>
          <div>
            <h2>群组</h2>
            <p>把人按项目、部门或领域分到一起，再把知识库共享给整个群组。</p>
          </div>
          <button className="icon-button" onClick={onClose} title="Close">×</button>
        </div>
        <div className="source-detail-body">
          {isSystemAdmin && (
            <div className="tabs" role="tablist" aria-label="群组范围">
              {([["mine", "我加入的群组"], ["all", "全部群组"]] as const).map(([id, text]) => (
                <button
                  key={id}
                  role="tab"
                  aria-selected={scope === id}
                  className={`tab ${scope === id ? "active" : ""}`}
                  onClick={() => { setScope(id); setDetail(null); setShared(null); setShareRequests(null); }}
                >{text}</button>
              ))}
            </div>
          )}

          {error && <p className="password-change-status error">{error}</p>}
          {notice && <p className="tool-hint" style={{ margin: 0 }}>{notice}</p>}

          {/* 我发起的共享申请(codex #519 R11 P1)。
              ⚠ **刻意放在群组面板顶层、且不挂任何笔记本条件**:裁决 P2-7 让撤回只认「这条
              申请是你提的」,正是为了让**已失去笔记本管理权**的申请人还能收回自己的提议
              (否则批准会拒绝他、撤回也够不着,申请永远卡在组管理员队列里)。所以入口必须
              在笔记本工作区**之外** —— 他可能连那本库的读权都一起没了,打不开工作区。
              空清单时整节不渲染,不给没有申请的人添一行噪音。 */}
          {myRequests !== null && myRequests.length > 0 && (
            <div className="stack">
              <span className="section-title">我发起的共享申请</span>
              <p className="tool-hint" style={{ margin: 0 }}>
                等待组管理员审批。撤回后可以重新发起。
              </p>
              {/* 名称按**当前**权限逐个给:失权/退组之后后端不再返回它(否则改名后的新
                  名字会持续送到一个已经无权查看的人手里)。空的那半给一句中性说明,不留
                  空白——也不影响撤回,那条的授权轴是「这条申请是你提的」。 */}
              {myRequests.some((req) => !req.notebook_name || !req.group_name) && (
                <p className="tool-hint" style={{ margin: 0 }}>
                  部分名称不再显示：你已不再有权查看对应的知识库或群组。撤回不受影响。
                </p>
              )}
              {myRequests.map((req) => (
                <div className="checklist-row" key={req.id} style={{ alignItems: "center", gap: 8 }}>
                  <span style={{ flex: 1, wordBreak: "break-word" }}>
                    {req.notebook_name || "名称不再显示的知识库"}
                    {" → "}
                    {req.group_name || "名称不再显示的群组"}
                  </span>
                  <span className="new-pill">待审批</span>
                  <button
                    className="sort-button"
                    disabled={busy}
                    onClick={() => { void mutateThenReconcile(
                      // 撤回与批准/驳回是同一种形状,所以走同一条两段式:撤回成功之后
                      // 重取失败会把这条已经不存在的申请留在屏幕上,用户再点一次就撞
                      // 404——那是在把人骗去重试一个已经完成的动作。
                      () => withdrawShareRequest(req.notebook_id, req.id).then(() => {
                        setMyRequests((current) => (current ?? []).filter((r) => r.id !== req.id));
                      }),
                      async () => { setMyRequests(await listMyPendingShareRequests()); },
                      "撤回申请失败",
                      "已撤回，但列表没能刷新，请手动刷新查看最新状态。",
                    ); }}
                  >{busy ? "处理中…" : "撤回"}</button>
                </div>
              ))}
            </div>
          )}

          <div className="stack">
            <span className="section-title">新建群组</span>
            <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
              <input
                value={newName}
                placeholder="群组名称"
                aria-label="群组名称"
                disabled={busy}
                maxLength={GROUP_INPUT_LIMITS.nameMaxChars}
                style={{ flex: 1 }}
                onChange={(event) => setNewName(event.target.value)}
              />
              {kinds.length > 1 && (
                <select
                  value={newKind}
                  aria-label="群组分类"
                  disabled={busy}
                  onChange={(event) => setNewKind(event.target.value)}
                >
                  {kinds.map((kind) => (
                    <option key={kind} value={kind}>{groupKindLabel(kind)}</option>
                  ))}
                </select>
              )}
              <button
                className="new-pill"
                disabled={busy || !newName.trim()}
                onClick={() => { void run(async () => {
                  const created = await createGroup(
                    newName.trim(),
                    kinds.length > 1 ? newKind : "project",
                    newDescription.trim(),
                  );
                  setNewName("");
                  setNewDescription("");
                  await refreshGroups(scope);
                  setDetail(created);
                  setRenameDraft(created.name);
                  setDescriptionDraft(created.description);
                  setShared(await listGroupSharedNotebooks(created.id));
                  // ⚠ 建组路径也必须初始化 `shareRequests`:创建者恒是组管理员,所以
                  // 「待审批申请」区一定会渲染,而它把 `null` 当「加载中…」——不置值
                  // 的话新建的组一打开就是**永久加载态**,直到关掉重开(codex #519 R2
                  // P2-2)。直接置 `[]` 而不发请求:这个组是这一刻刚建出来的,申请只能
                  // 由成员对已存在的组提交,所以它的待审批集合可证明为空——多发一次
                  // 必然返回 `[]` 的请求换不来任何信息。
                  setShareRequests([]);
                }, "建组失败"); }}
              >{busy ? "创建中…" : "创建群组"}</button>
            </div>
            <textarea
              rows={2}
              value={newDescription}
              placeholder="群组说明（可选）"
              aria-label="新群组的说明"
              disabled={busy}
              maxLength={GROUP_INPUT_LIMITS.descriptionMaxChars}
              onChange={(event) => setNewDescription(event.target.value)}
            />
            {/* 前端显示同一护栏(数值上限红线):输入框直接敲不进去,快到上限时先出声,
                不必等提交吃一个 400 才知道边界在哪。 */}
            <LengthHint value={newName} max={GROUP_INPUT_LIMITS.nameMaxChars} label="群组名称" />
            <LengthHint value={newDescription} max={GROUP_INPUT_LIMITS.descriptionMaxChars} label="群组说明" />
            {kinds.length === 1 && (
              <p className="tool-hint" style={{ margin: 0 }}>
                你可以创建项目群组；部门与领域群组由管理员创建。
              </p>
            )}
          </div>

          <div className="stack">
            <span className="section-title">群组清单</span>
            {groups === null ? (
              <p className="tool-hint">加载中…</p>
            ) : groups.length === 0 ? (
              <p className="tool-hint">还没有群组。在上面新建一个，或者等组管理员把你加进来。</p>
            ) : (
              groups.map((group) => (
                <div className="checklist-row" key={group.id} style={{ alignItems: "center", gap: 8 }}>
                  <span style={{ flex: 1, wordBreak: "break-word" }}>{group.name}</span>
                  <span className="new-pill">{groupKindLabel(group.kind)}</span>
                  <span className="tool-hint">{group.member_count} 人</span>
                  {group.my_role && <span className="tool-hint">{groupRoleLabel(group.my_role)}</span>}
                  <button
                    className="sort-button"
                    disabled={busy}
                    onClick={() => { void openDetail(group.id); }}
                  >{detail?.id === group.id ? "已展开" : "查看"}</button>
                </div>
              ))
            )}
          </div>

          {detail && (
            <div className="stack">
              <span className="section-title">{detail.name} · {groupKindLabel(detail.kind)}</span>

              {canManage ? (
                <>
                  <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
                    <input
                      value={renameDraft}
                      aria-label="群组新名称"
                      disabled={busy}
                      maxLength={GROUP_INPUT_LIMITS.nameMaxChars}
                      style={{ flex: 1 }}
                      onChange={(event) => setRenameDraft(event.target.value)}
                    />
                  </div>
                  <textarea
                    rows={2}
                    value={descriptionDraft}
                    placeholder="群组说明（可选）"
                    aria-label="群组说明"
                    disabled={busy}
                    maxLength={GROUP_INPUT_LIMITS.descriptionMaxChars}
                    onChange={(event) => setDescriptionDraft(event.target.value)}
                  />
                  <LengthHint value={renameDraft} max={GROUP_INPUT_LIMITS.nameMaxChars} label="群组名称" />
                  <LengthHint value={descriptionDraft} max={GROUP_INPUT_LIMITS.descriptionMaxChars} label="群组说明" />
                  <div className="tag-row">
                    <button
                      className="sort-button"
                      disabled={
                        busy
                        || !renameDraft.trim()
                        || (renameDraft.trim() === detail.name
                            && descriptionDraft === detail.description)
                      }
                      onClick={() => { void run(async () => {
                        const updated = await updateGroup(
                          detail.id, renameDraft.trim(), descriptionDraft,
                        );
                        setDetail(updated);
                        setRenameDraft(updated.name);
                        setDescriptionDraft(updated.description);
                        await refreshGroups(scope);
                        // 组名进笔记本卡片的「来自群组《X》」标注,改完要让外层重取
                        // ——否则列表上挂的还是旧名字,直到下一次整页刷新。
                        onChanged();
                      }, "保存群组信息失败"); }}
                    >{busy ? "保存中…" : "保存群组信息"}</button>
                  </div>
                </>
              ) : (
                detail.description && (
                  <p className="tool-hint" style={{ margin: 0 }}>{detail.description}</p>
                )
              )}

              <span className="section-title">成员</span>
              {detail.members.map((member) => (
                <div className="checklist-row" key={member.id} style={{ alignItems: "center", gap: 8 }}>
                  <span style={{ flex: 1, wordBreak: "break-word" }}>
                    {member.display_name ? `${member.display_name}（${member.username}）` : member.username}
                  </span>
                  {canManage ? (
                    <select
                      value={member.role}
                      aria-label={`${member.username} 的角色`}
                      disabled={busy}
                      onChange={(event) => { void run(async () => {
                        setDetail(await putGroupMember(detail.id, member.id, event.target.value));
                        await refreshGroups(scope);
                      }, "角色修改失败"); }}
                    >
                      <option value="member">{groupRoleLabel("member")}</option>
                      <option value="admin">{groupRoleLabel("admin")}</option>
                    </select>
                  ) : (
                    <span className="tool-hint">{groupRoleLabel(member.role)}</span>
                  )}
                  {/* 两步确认,与删组同一套:移出成员会当场撤掉这个人经**本组**拿到的
                      全部知识库访问,误点一下的爆炸半径和删组同量级,不该一击即发。 */}
                  {canManage && (
                    confirming === `remove:${member.id}` ? (
                      <>
                        <span className="tool-hint">移出后，他将看不到共享给本组的知识库。</span>
                        <button
                          className="sort-button"
                          disabled={busy}
                          onClick={() => { void run(async () => {
                            await removeGroupMember(detail.id, member.id);
                            setDetail(await getGroup(detail.id));
                            setConfirming("");
                            await refreshGroups(scope);
                            onChanged();
                          }, "移除成员失败"); }}
                        >确认移出</button>
                        <button
                          className="sort-button"
                          disabled={busy}
                          onClick={() => setConfirming("")}
                        >取消</button>
                      </>
                    ) : (
                      <button
                        className="sort-button"
                        disabled={busy}
                        onClick={() => setConfirming(`remove:${member.id}`)}
                      >移出群组</button>
                    )
                  )}
                </div>
              ))}

              {canManage && (
                <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
                  <input
                    value={memberName}
                    placeholder="用户名（需完全一致）"
                    aria-label="要添加的用户名"
                    disabled={busy}
                    style={{ flex: 1 }}
                    onChange={(event) => setMemberName(event.target.value)}
                  />
                  <button
                    className="sort-button"
                    disabled={busy || !memberName.trim()}
                    onClick={() => { void run(async () => {
                      const found = await resolveUser(memberName.trim());
                      setDetail(await putGroupMember(detail.id, found.id, "member"));
                      setMemberName("");
                      await refreshGroups(scope);
                      onChanged();
                    }, "添加成员失败"); }}
                  >{busy ? "添加中…" : "添加成员"}</button>
                </div>
              )}

              {canManage && (
                <>
                  <span className="section-title">待审批申请</span>
                  {shareRequests === null ? (
                    <p className="tool-hint">加载中…</p>
                  ) : shareRequests.length === 0 ? (
                    <p className="tool-hint">没有待审批的共享申请。</p>
                  ) : (
                    shareRequests.map((req) => (
                      <div className="checklist-row" key={req.id} style={{ alignItems: "center", gap: 8 }}>
                        <span style={{ flex: 1, wordBreak: "break-word" }}>{req.notebook_name || "知识库"}</span>
                        <span className="tool-hint">申请人 {req.requested_by_username}</span>
                        {/* 批准会写一条授权边、把库交给整组——即时反馈够了(边可撤),
                            但不能默默无声,所以两个按钮各自忙碌态可见。 */}
                        <button
                          className="new-pill"
                          disabled={busy}
                          onClick={() => { void mutateThenReconcile(
                            () => approveShareRequest(detail.id, req.id).then(() => {
                              // 服务端已经改完:先把这一行从本地清单里拿掉。对账失败时它
                              // 若还挂在屏幕上,就是在诱人再点一次——而再点必然 404(它已经
                              // 不是待审批状态了)。
                              setShareRequests((current) => (current ?? []).filter((r) => r.id !== req.id));
                            }),
                            async () => {
                              setShareRequests(await listGroupShareRequests(detail.id));
                              setShared(await listGroupSharedNotebooks(detail.id));
                              onChanged();
                            },
                            "批准申请失败",
                            "已批准，但列表没能刷新，请手动刷新查看最新状态。",
                          ); }}
                        >{busy ? "处理中…" : "批准"}</button>
                        <button
                          className="sort-button"
                          disabled={busy}
                          onClick={() => { void mutateThenReconcile(
                            () => rejectShareRequest(detail.id, req.id).then(() => {
                              setShareRequests((current) => (current ?? []).filter((r) => r.id !== req.id));
                            }),
                            async () => {
                              setShareRequests(await listGroupShareRequests(detail.id));
                            },
                            "驳回申请失败",
                            "已驳回，但列表没能刷新，请手动刷新查看最新状态。",
                          ); }}
                        >{busy ? "处理中…" : "驳回"}</button>
                      </div>
                    ))
                  )}

                  <span className="section-title">共享给本组的知识库</span>
                  {shared === null ? (
                    <p className="tool-hint">加载中…</p>
                  ) : shared.length === 0 ? (
                    <p className="tool-hint">还没有知识库共享给这个群组。</p>
                  ) : (
                    shared.map((item) => (
                      <div className="checklist-row" key={item.notebook_id} style={{ alignItems: "center", gap: 8 }}>
                        <span style={{ flex: 1, wordBreak: "break-word" }}>{item.name}</span>
                        <span className="tool-hint">所有者 {item.owner_username}</span>
                        <button
                          className="sort-button"
                          disabled={busy}
                          onClick={() => { void run(async () => {
                            await revokeGroupSharedNotebook(detail.id, item.notebook_id);
                            setShared(await listGroupSharedNotebooks(detail.id));
                            onChanged();
                          }, "撤销共享失败"); }}
                        >撤销共享</button>
                      </div>
                    ))
                  )}
                </>
              )}

              <div className="tag-row" style={{ marginTop: 4 }}>
                {detail.my_role && (
                  confirming === `leave:${detail.id}` ? (
                    <>
                      <span className="tool-hint">退出后你将看不到共享给这个群组的知识库。</span>
                      <button
                        className="sort-button"
                        disabled={busy}
                        onClick={() => { void run(async () => {
                          await leaveGroup(detail.id);
                          setDetail(null);
                          setShared(null);
                          setShareRequests(null);
                          setConfirming("");
                          await refreshGroups(scope);
                          onChanged();
                        }, "退出群组失败"); }}
                      >确认退出</button>
                      <button className="sort-button" disabled={busy} onClick={() => setConfirming("")}>取消</button>
                    </>
                  ) : (
                    <button
                      className="sort-button"
                      disabled={busy}
                      onClick={() => setConfirming(`leave:${detail.id}`)}
                    >退出群组</button>
                  )
                )}
                {canManage && (
                  confirming === `delete:${detail.id}` ? (
                    <>
                      <span className="tool-hint">删除后，共享给这个群组的知识库会一并收回。</span>
                      <button
                        className="new-pill danger-pill"
                        disabled={busy}
                        onClick={() => { void run(async () => {
                          await deleteGroup(detail.id);
                          setDetail(null);
                          setShared(null);
                          setShareRequests(null);
                          setConfirming("");
                          await refreshGroups(scope);
                          onChanged();
                        }, "删除群组失败"); }}
                      >确认删除</button>
                      <button className="sort-button" disabled={busy} onClick={() => setConfirming("")}>取消</button>
                    </>
                  ) : (
                    <button
                      className="new-pill danger-pill"
                      disabled={busy}
                      onClick={() => setConfirming(`delete:${detail.id}`)}
                    >删除群组</button>
                  )
                )}
              </div>
            </div>
          )}
        </div>
        <div className="modal-actions padded">
          <button className="new-pill" onClick={onClose}>完成</button>
        </div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}
