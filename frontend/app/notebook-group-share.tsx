"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { toUserMessage } from "./errors.ts";
import {
  canDropManage,
  foldGroupShares,
  grantGroupAdminsManage,
  groupKindLabel,
  isGroupAdmin,
  listGroups,
  manageGrantIds,
  listMyShareRequests,
  listNotebookGrants,
  requestableGroups,
  revocationOrder,
  revokeNotebookGrant,
  shareableGroups,
  shareNotebookToGroup,
  shareRequestStatusLabel,
  submitShareRequest,
  visibleMyShareRequests,
  withdrawShareRequest,
  type GroupShareEntry,
  type GroupSummary,
  type ShareRequest,
} from "./group-api.ts";

/** `busySlot` 里代表「正在发出共享」的那一格 —— 它不是任何一个组的 id。 */
const SHARE_SLOT = "__share__";
/** 「正在提交申请」的那一格。 */
const REQUEST_SLOT = "__request__";
/** 某一项的「增减管理权」占的那一格 —— 与它的撤销格(裸 groupId)必须不同,否则两个
 *  按钮会一起显示进行态。群组 id 是 `grp-<32 位十六进制>`,不含冒号,拼不出撞车。 */
const manageSlot = (groupId: string) => `${groupId}:manage`;

/**
 * 取消管理权可能**把自己的管理权一起取消掉**时给的提醒。
 *
 * 组管理员(非库主)对本库的管理权正来自那条 `group_admins` 边:取消它,他当场就打不开
 * 这个面板了。措辞是**条件式**的——本组件只拿得到 `notebookId`,分不出「我的权限来自这个
 * 群组」与「我本来就是库主」(库主取消后毫发无损),所以只能如实说「若……则……」。
 */
export const SELF_MANAGE_WARNING =
  "若你对这本笔记本的管理权来自该群组，取消后你将无法再管理它。";

/** 真的把自己管没了之后给的收尾说明 —— 它是**结果**不是错误,所以不走红色错误条。 */
const SELF_MANAGE_LOST_NOTICE =
  "已取消该群组的管理权限。你对这本笔记本的管理权来自该群组，现在起无法再管理它。";

/**
 * 借入参考库的「未共享门」提示(设计文档 §6.1)。
 *
 * 一旦这本笔记本被共享出去(有了只读成员或群组授权),它**借来的**参考库就停止参与
 * 检索——借来的东西不转借。取消共享后自动恢复。文案必须挨着**将要触发它的那个
 * 按钮**(开启链接分享 / 共享给该群组),不是事后在失效边上解释:那时候用户已经不
 * 知道是哪一步造成的。
 */
export const BORROWED_BASE_SHARE_WARNING =
  "共享出去之后，本笔记本借来的参考库会暂停参与检索；取消共享即可恢复。";

type NotebookGroupShareProps = {
  notebookId: string;
  /** 共享面变化后让外层重取笔记本清单（「已分享」徽标口径含群组共享）。 */
  onChanged: () => void;
};

/**
 * 分享弹窗里的「共享给群组」一节(群组知识共享 P1-T4 + P2-T3)。
 *
 * 与既有的只读共享链接**并列**而不是替代:链接是发给具体某个人的,群组共享是发给
 * 一整个组、并随成员进出自动生效与失效。
 *
 * P2 之后有**两条**共享路径,取决于我在目标组里的角色:
 *
 * * **我是组管理员** → 直接发边(`shareableGroups`);可勾「组管理员可管理」再追加一条
 *   `group_admins`/`admin` 边,让整组的组管理员获得这本库的内容管理权。
 * * **我只是普通成员** → 发不了边(后端双重条件要求我同时是组管理员),只能**提交共享
 *   申请**(`requestableGroups`),由组管理员审批。申请状态(待审批 / 已驳回)在下方回显。
 */
export function NotebookGroupShare({ notebookId, onChanged }: NotebookGroupShareProps) {
  const [groups, setGroups] = useState<GroupSummary[]>([]);
  const [entries, setEntries] = useState<GroupShareEntry[] | null>(null);
  const [requests, setRequests] = useState<ShareRequest[]>([]);
  const [picked, setPicked] = useState("");
  const [manage, setManage] = useState(false);
  const [requestPick, setRequestPick] = useState("");
  /** 空串 = 不忙;否则是**正在忙的那一项**(条目 id / 申请 id / `SHARE_SLOT` / `REQUEST_SLOT`)。 */
  const [busySlot, setBusySlot] = useState("");
  const [error, setError] = useState("");
  /** 中性说明(不是错误)——目前只有「你把自己的管理权取消掉了」这一种。 */
  const [notice, setNotice] = useState("");
  // 切库 / 卸载之后落地的响应一律丢弃。`useEffect` 的局部 `cancelled` 只能覆盖它自己
  // 那一次加载,而写动作之后的 reload 是在事件回调里发的 —— 它同样可能在卸载后落地。
  const liveRef = useRef(true);
  useEffect(() => {
    liveRef.current = true;
    return () => { liveRef.current = false; };
  }, []);

  const reload = useCallback(async () => {
    const [grants, mine, myReqs] = await Promise.all([
      listNotebookGrants(notebookId),
      listGroups("mine"),
      listMyShareRequests(notebookId),
    ]);
    // ⚠ 成功路径也要看存活位:只在失败分支看它,等于「加载成功就无条件 setState」,
    // 那正是 React 卸载后更新状态的经典形态。
    if (!liveRef.current) return;
    setEntries(foldGroupShares(grants));
    setGroups(mine);
    setRequests(myReqs);
  }, [notebookId]);

  useEffect(() => {
    setEntries(null);
    reload().catch((err) => {
      if (liveRef.current) setError(toUserMessage(err, "共享清单加载失败"));
    });
  }, [reload]);

  async function run(
    slot: string,
    action: () => Promise<void>,
    fallback: string,
    /** 动作成功、但紧接着的重取失败时给的说明(见 `SELF_MANAGE_LOST_NOTICE`)。 */
    reloadDeniedNotice = "",
  ) {
    setBusySlot(slot);
    setError("");
    setNotice("");
    let acted = false;
    try {
      await action();
      acted = true;
      if (liveRef.current) onChanged();
    } catch (err) {
      if (liveRef.current) setError(toUserMessage(err, fallback));
    } finally {
      // 无论成败都重取:撤销是逐条删边,中途失败会留下一个「删了一半」的状态,
      // 不重取的话界面还按发起前那份清单渲染,用户看不出到底删掉了哪几条。
      let denied = false;
      await reload().catch(() => { denied = true; });
      if (liveRef.current) {
        setBusySlot("");
        // 动作**成功了**、重取却被拒 —— 最常见的原因就是这次动作把自己的管理权取消了
        // (三个 grants 端点都要 `notebook:manage`)。这是用户显式要求的结果,不是故障:
        // 给一句说明并把清单清空,别让面板挂着一份再也刷不新的旧数据、也别报红。
        if (acted && denied && reloadDeniedNotice) {
          setEntries([]);
          setNotice(reloadDeniedNotice);
        }
      }
    }
  }

  // 我担任组管理员的那些组 —— 只用来决定要不要给「取消管理权」挂条件式提醒(见
  // `SELF_MANAGE_WARNING`),不是权限判定。
  const myAdminGroupIds = new Set(groups.filter(isGroupAdmin).map((group) => group.id));
  const options = shareableGroups(groups, entries ?? []);
  const requestOptions = requestableGroups(groups, entries ?? [], requests);
  const pendingAndRejected = visibleMyShareRequests(requests);
  const busy = busySlot !== "";

  return (
    <div className="stack">
      <span className="section-title">共享给群组</span>
      <p className="tool-hint" style={{ margin: 0 }}>
        群组成员可以打开这本笔记本、提问、写自己的深度报告，并把它挂为参考库；不能修改内容。
      </p>

      {error && <p className="password-change-status error">{error}</p>}
      {notice && <p className="tool-hint" style={{ margin: 0 }}>{notice}</p>}

      {entries === null ? (
        <p className="tool-hint">加载中…</p>
      ) : entries.length === 0 ? (
        <p className="tool-hint">还没有共享给任何群组。</p>
      ) : (
        entries.map((entry) => (
          <div className="group-row" key={entry.groupId}>
            <div className="group-row-main">
              <span className="group-row-name">
                {entry.missing ? "已失效的群组共享" : entry.name}
              </span>
              {!entry.missing && <span className="group-chip">{groupKindLabel(entry.kind)}</span>}
              {/* 同组两条边(成员只读 + 组管理员可管)折成一项时,标注它带了管理权。 */}
              {!entry.missing && entry.manage && <span className="group-row-meta">组管理员可管理</span>}
              {entry.missing && (
                <span className="group-row-note">该群组已不存在，这条共享不再生效，可以删掉。</span>
              )}
            </div>
            <div className="group-row-actions">
            {/* 已存在的共享也要能增减管理权(codex #519 R9 P2):批准共享申请写的是
                `(group, viewer)` 单边,新建路径的「组管理员可管理」勾选对它够不着——
                不给这两个入口,P2 的招牌流程走完就永远停在「没有管理权」且无路可改,
                只能撤销整个共享再重发一次。后端两个端点都是现成的,缺的只是 UI。 */}
            {!entry.missing && !entry.manage && (
              <button
                className="sort-button"
                disabled={busy}
                onClick={() => { void run(manageSlot(entry.groupId), async () => {
                  await grantGroupAdminsManage(notebookId, entry.groupId);
                }, "开启管理权限失败"); }}
              >{busySlot === manageSlot(entry.groupId) ? "开启中…" : "允许组管理员管理"}</button>
            )}
            {!entry.missing && entry.manage && canDropManage(entry) && (
              <button
                className="sort-button"
                disabled={busy}
                title={myAdminGroupIds.has(entry.groupId) ? SELF_MANAGE_WARNING : undefined}
                onClick={() => { void run(manageSlot(entry.groupId), async () => {
                  // 只删**授予管理权**的那几条边,读权边原样保留——判据与「可管理」
                  // 标注共用 `confersManage`(R7 P2 立的规矩:两份写法迟早分叉)。
                  for (const grantId of manageGrantIds(entry)) {
                    await revokeNotebookGrant(notebookId, grantId);
                  }
                }, "取消管理权限失败", SELF_MANAGE_LOST_NOTICE); }}
              >{busySlot === manageSlot(entry.groupId) ? "取消中…" : "取消组管理员管理"}</button>
            )}
            <button
              className="sort-button"
              disabled={busy}
              onClick={() => { void run(entry.groupId, async () => {
                // 同一个组可能有两条边(成员只读 / 组管理员可管),撤销要一并删掉,
                // 否则界面上「撤销」过的条目会因为剩下那条边而重新出现。
                // ⚠ 顺序必须走 `revocationOrder`——它把**给管理权的那条边排到最后**。
                // 撤销权限本身可能就来自那条边(三个 grant 端点都是 admin 档能力,组管理员
                // 也能进这个面板),先删它就等于当场把自己的删除权删掉:第二次 DELETE 拿
                // 404,只读边留下、共享仍然生效,而界面已经报了「撤销」(codex #519 R7 P2)。
                for (const grantId of revocationOrder(entry)) {
                  await revokeNotebookGrant(notebookId, grantId);
                }
              }, "撤销共享失败"); }}
            >{busySlot === entry.groupId ? "撤销中…" : "撤销共享"}</button>
            </div>
          </div>
        ))
      )}

      {options.length > 0 ? (<>
        {/* 未共享门的提示挨着**将要触发它的那个按钮**(设计文档 §6.1)。 */}
        <p className="tool-hint" style={{ margin: 0 }}>{BORROWED_BASE_SHARE_WARNING}</p>
        <div className="group-form-row">
          <select
            className="group-input"
            value={picked}
            aria-label="选择群组"
            disabled={busy}
            onChange={(event) => setPicked(event.target.value)}
          >
            <option value="">选择一个群组…</option>
            {options.map((group) => (
              <option key={group.id} value={group.id}>
                {group.name}（{groupKindLabel(group.kind)}）
              </option>
            ))}
          </select>
          <button
            className="new-pill"
            disabled={busy || !picked}
            onClick={() => { void run(SHARE_SLOT, async () => {
              await shareNotebookToGroup(notebookId, picked, { manage });
              setPicked("");
              setManage(false);
            }, "共享失败"); }}
          >{busySlot === SHARE_SLOT ? "共享中…" : "共享给该群组"}</button>
        </div>
        {/* 「组管理员可管理」:追加一条 group_admins/admin 边,组管理员因此能改内容。 */}
        <label className="group-row group-check">
          <input
            type="checkbox"
            checked={manage}
            disabled={busy}
            onChange={(event) => setManage(event.target.checked)}
            aria-label="组管理员可管理这本笔记本"
          />
          <span className="group-row-note">
            允许该群组的组管理员管理这本笔记本（添加/删除来源、构建索引、编辑知识）。
          </span>
        </label>
      </>) : (
        entries !== null && options.length === 0 && requestOptions.length === 0 && (
          <p className="tool-hint" style={{ margin: 0 }}>
            没有可共享的群组。你可以在账户菜单的「群组」里创建群组，或等组管理员把你加进来。
          </p>
        )
      )}

      {/* 我只是普通成员的组:发不了边,只能申请。 */}
      {requestOptions.length > 0 && (
        <div className="stack" style={{ gap: 8 }}>
          <span className="section-title">申请共享给群组</span>
          <p className="tool-hint" style={{ margin: 0 }}>
            你不是这些群组的组管理员，无法直接共享；提交申请后由组管理员审批。
          </p>
          <div className="group-form-row">
            <select
              className="group-input"
              value={requestPick}
              aria-label="选择要申请的群组"
              disabled={busy}
              onChange={(event) => setRequestPick(event.target.value)}
            >
              <option value="">选择一个群组…</option>
              {requestOptions.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name}（{groupKindLabel(group.kind)}）
                </option>
              ))}
            </select>
            <button
              className="new-pill"
              disabled={busy || !requestPick}
              onClick={() => { void run(REQUEST_SLOT, async () => {
                await submitShareRequest(notebookId, requestPick);
                setRequestPick("");
              }, "提交共享申请失败"); }}
            >{busySlot === REQUEST_SLOT ? "提交中…" : "提交共享申请"}</button>
          </div>
        </div>
      )}

      {/* 我发起过的、尚未落地的申请(待审批 / 已驳回)。待审批的可撤回。 */}
      {pendingAndRejected.length > 0 && (
        <div className="stack" style={{ gap: 8 }}>
          <span className="section-title">我的共享申请</span>
          {pendingAndRejected.map((req) => (
            <div className="group-row" key={req.id}>
              <div className="group-row-main">
                <span className="group-row-name">{req.group_name || "群组"}</span>
                <span className={`group-chip ${req.status === "rejected" ? "is-rejected" : "is-pending"}`}>
                  {shareRequestStatusLabel(req.status)}
                </span>
              </div>
              {req.status === "pending" && (
                <div className="group-row-actions">
                  <button
                    className="sort-button"
                    disabled={busy}
                    onClick={() => { void run(req.id, async () => {
                      await withdrawShareRequest(notebookId, req.id);
                    }, "撤回申请失败"); }}
                  >{busySlot === req.id ? "撤回中…" : "撤回申请"}</button>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
