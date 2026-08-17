"use client";

import { useCallback, useEffect, useState } from "react";

import { toUserMessage } from "./errors.ts";
import {
  foldGroupShares,
  groupKindLabel,
  listGroups,
  listNotebookGrants,
  revokeNotebookGrant,
  shareNotebookToGroup,
  shareableGroups,
  type GroupShareEntry,
  type GroupSummary,
} from "./group-api.ts";

/**
 * 借入参考库的「未共享门」提示(设计文档 §6.1)。
 *
 * 一旦这本笔记本被共享出去(有了只读成员或群组授权),它**借来的**参考库就停止参与
 * 检索——借来的东西不转借。取消共享后自动恢复。文案要在用户按下共享之前就在,不是
 * 事后在失效边上解释:那时候他已经不知道是哪一步造成的。
 */
export const BORROWED_BASE_SHARE_WARNING =
  "共享出去之后，本笔记本借来的参考库会暂停参与检索；取消共享即可恢复。";

type NotebookGroupShareProps = {
  notebookId: string;
  /** 共享面变化后让外层重取笔记本清单（「已分享」徽标口径含群组共享）。 */
  onChanged: () => void;
};

/**
 * 分享弹窗里的「共享给群组」一节(群组知识共享 P1-T4)。
 *
 * 与既有的只读共享链接**并列**而不是替代:链接是发给具体某个人的,群组共享是发给
 * 一整个组、并随成员进出自动生效与失效。
 *
 * 两条 P1 的口径直接写进了这里:
 *
 * * **只列我担任组管理员的组**(`shareableGroups`)。后端要求发边的人同时对这本库
 *   有管理权、又是目标组的组管理员;列出别的组只会让用户点一次拿一个 403。
 * * **只发一条只读授权**(已定裁决 4)。组管理员的写权限随 P2 一起上;现在多发一条
 *   等于放一条当前没有任何效果的授权在库上。
 */
export function NotebookGroupShare({ notebookId, onChanged }: NotebookGroupShareProps) {
  const [groups, setGroups] = useState<GroupSummary[]>([]);
  const [entries, setEntries] = useState<GroupShareEntry[] | null>(null);
  const [picked, setPicked] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    const [grants, mine] = await Promise.all([
      listNotebookGrants(notebookId),
      listGroups("mine"),
    ]);
    setEntries(foldGroupShares(grants));
    setGroups(mine);
  }, [notebookId]);

  useEffect(() => {
    let cancelled = false;
    setEntries(null);
    reload().catch((err) => {
      if (!cancelled) setError(toUserMessage(err, "共享清单加载失败"));
    });
    return () => { cancelled = true; };
  }, [reload]);

  async function run(action: () => Promise<void>, fallback: string) {
    setBusy(true);
    setError("");
    try {
      await action();
      await reload();
      onChanged();
    } catch (err) {
      setError(toUserMessage(err, fallback));
    } finally {
      setBusy(false);
    }
  }

  const options = shareableGroups(groups, entries ?? []);

  return (
    <div className="stack">
      <span className="section-title">共享给群组</span>
      <p className="tool-hint" style={{ margin: 0 }}>
        群组成员可以打开这本笔记本、提问、写自己的深度报告，并把它挂为参考库；不能修改内容。
      </p>
      <p className="tool-hint" style={{ margin: 0 }}>{BORROWED_BASE_SHARE_WARNING}</p>

      {error && <p className="password-change-status error">{error}</p>}

      {entries === null ? (
        <p className="tool-hint">加载中…</p>
      ) : entries.length === 0 ? (
        <p className="tool-hint">还没有共享给任何群组。</p>
      ) : (
        entries.map((entry) => (
          <div className="checklist-row" key={entry.groupId} style={{ alignItems: "center", gap: 8 }}>
            <span style={{ flex: 1, wordBreak: "break-word" }}>
              {entry.missing ? "已失效的群组共享" : entry.name}
            </span>
            {!entry.missing && <span className="new-pill">{groupKindLabel(entry.kind)}</span>}
            {entry.missing && (
              <span className="tool-hint">该群组已不存在，这条共享不再生效，可以删掉。</span>
            )}
            <button
              className="sort-button"
              disabled={busy}
              onClick={() => { void run(async () => {
                // 同一个组可能有两条边(成员只读 / 组管理员可管),撤销要一并删掉,
                // 否则界面上「撤销」过的条目会因为剩下那条边而重新出现。
                for (const grantId of entry.grantIds) {
                  await revokeNotebookGrant(notebookId, grantId);
                }
              }, "撤销共享失败"); }}
            >{busy ? "撤销中…" : "撤销共享"}</button>
          </div>
        ))
      )}

      {options.length > 0 ? (
        <div className="tag-row" style={{ alignItems: "center", gap: 8 }}>
          <select
            value={picked}
            aria-label="选择群组"
            disabled={busy}
            style={{ flex: 1 }}
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
            onClick={() => { void run(async () => {
              await shareNotebookToGroup(notebookId, picked);
              setPicked("");
            }, "共享失败"); }}
          >{busy ? "共享中…" : "共享给该群组"}</button>
        </div>
      ) : (
        entries !== null && (
          <p className="tool-hint" style={{ margin: 0 }}>
            没有可选的群组。只有你担任组管理员的群组才能收到共享，可在账户菜单的「群组」里创建或管理。
          </p>
        )
      )}
    </div>
  );
}
