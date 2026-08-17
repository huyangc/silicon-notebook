"use client";

import { useCallback, useEffect, useState } from "react";

import { toUserMessage } from "./errors.ts";
import { FloatingModalCard } from "./floating-modal-card.tsx";
import {
  createGroup,
  creatableGroupKinds,
  deleteGroup,
  getGroup,
  groupKindLabel,
  groupRoleLabel,
  isGroupAdmin,
  leaveGroup,
  listGroupSharedNotebooks,
  listGroups,
  putGroupMember,
  removeGroupMember,
  renameGroup,
  resolveUser,
  revokeGroupSharedNotebook,
  type GroupDetail,
  type GroupSharedNotebook,
  type GroupSummary,
} from "./group-api.ts";

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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState("project");
  const [memberName, setMemberName] = useState("");
  const [renameDraft, setRenameDraft] = useState("");
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

  // 每个写动作都走它:统一收敛忙碌位、错误文案与「动作之后重取哪几份数据」。
  async function run(action: () => Promise<void>, fallback: string) {
    setBusy(true);
    setError("");
    try {
      await action();
    } catch (err) {
      setError(toUserMessage(err, fallback));
    } finally {
      setBusy(false);
    }
  }

  async function openDetail(groupId: string) {
    setConfirming("");
    setMemberName("");
    await run(async () => {
      const group = await getGroup(groupId);
      setDetail(group);
      setRenameDraft(group.name);
      // 「共享给本组的知识库」只有组管理员读得到(非管理员 404),所以对普通成员
      // 连查都不查——不是藏起来,是那条清单本来就不属于他这一层。
      setShared(
        isGroupAdmin(group) || isSystemAdmin
          ? await listGroupSharedNotebooks(groupId)
          : null,
      );
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
                  onClick={() => { setScope(id); setDetail(null); setShared(null); }}
                >{text}</button>
              ))}
            </div>
          )}

          {error && <p className="password-change-status error">{error}</p>}

          <div className="stack">
            <span className="section-title">新建群组</span>
            <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
              <input
                value={newName}
                placeholder="群组名称"
                aria-label="群组名称"
                disabled={busy}
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
                  const created = await createGroup(newName.trim(), kinds.length > 1 ? newKind : "project");
                  setNewName("");
                  await refreshGroups(scope);
                  setDetail(created);
                  setRenameDraft(created.name);
                  setShared(await listGroupSharedNotebooks(created.id));
                }, "建组失败"); }}
              >{busy ? "创建中…" : "创建群组"}</button>
            </div>
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

              {canManage && (
                <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
                  <input
                    value={renameDraft}
                    aria-label="群组新名称"
                    disabled={busy}
                    style={{ flex: 1 }}
                    onChange={(event) => setRenameDraft(event.target.value)}
                  />
                  <button
                    className="sort-button"
                    disabled={busy || !renameDraft.trim() || renameDraft.trim() === detail.name}
                    onClick={() => { void run(async () => {
                      const updated = await renameGroup(detail.id, renameDraft.trim());
                      setDetail(updated);
                      await refreshGroups(scope);
                    }, "改名失败"); }}
                  >{busy ? "保存中…" : "保存名称"}</button>
                </div>
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
                  {canManage && (
                    <button
                      className="sort-button"
                      disabled={busy}
                      onClick={() => { void run(async () => {
                        await removeGroupMember(detail.id, member.id);
                        setDetail(await getGroup(detail.id));
                        await refreshGroups(scope);
                        onChanged();
                      }, "移除成员失败"); }}
                    >移出群组</button>
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
