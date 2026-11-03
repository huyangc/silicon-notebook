"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { toUserMessage } from "./errors.ts";
import { resolveUser, type UserRef } from "./group-api.ts";
import { listBases, type MountedBase } from "./notebook-bases.ts";
import {
  addEditor,
  basesClosedBySharing,
  collaboratorName,
  collaboratorRoleLabel,
  listCollaborators,
  removeCollaborator,
  type CollaboratorItem,
} from "./notebook-collaborators-api.ts";

/** 结果文字在按钮旁停留多久。比复制按钮的 1.6s 长:这里的结果常是一整句(查无此人、
 *  公共知识库不能添加编辑者),要留够读完的时间。 */
export const COLLABORATOR_RESULT_HOLD_MS = 4000;

/** 添加编辑者之前的那句后果说明(决策 D13)。挨着将要触发它的按钮,不事后解释。 */
export const EDITOR_CLOSES_BASES_WARNING =
  "添加后，这些参考库对所有人失效，包括你自己在这本库里的提问。";

/** 遗留的管理权限点名授权:PUT 会把它**降为**编辑者,按钮旁必须先说清。 */
export const LEGACY_ADMIN_DOWNGRADE_NOTE = "当前为管理权限，确认后将改为可编辑";

const FIND_SLOT = "find";
const ADD_SLOT = "add";
const rowSlot = (userId: string) => `row:${userId}`;
/** 遗留管理权限行里「改为可编辑」那颗句中按钮的格,与同一行的「移除」分开。 */
const rowAddSlot = (userId: string) => `row:${userId}:add`;

type Tone = "ok" | "failed" | "neutral";
type SlotResult = { slot: string; tone: Tone; text: string };

/** 添加前的二次确认。`closing` 为空时只因为遗留管理权限要降级而确认。 */
type PendingConfirm = { target: UserRef; closing: MountedBase[]; wasAdmin: boolean; slot: string };

/** 一段会自己消失的结果文字。计时器只清自己那一条,换新结果时撤掉旧计时器。 */
function useSlotResult(): [SlotResult | null, (next: SlotResult | null) => void] {
  const [result, setResult] = useState<SlotResult | null>(null);
  const timerRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);
  const show = useCallback((next: SlotResult | null) => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = null;
    setResult(next);
    if (next) {
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        setResult((current) => (current === next ? null : current));
      }, COLLABORATOR_RESULT_HOLD_MS);
    }
  }, []);
  return [result, show];
}

type CollaboratorsSectionProps = {
  notebookId: string;
  /** 名单变化后让外层重取笔记本清单(「已分享」徽标的口径含点名授权)。 */
  onChanged: () => void;
};

type NotebookCollaboratorsShareProps = CollaboratorsSectionProps & {
  /** `workspaceCapabilities(...).canManageCollaborators`(恒 owner)。为 false 时整节不渲染、
   *  也不发任何请求——组管理员打开分享弹窗只看到群组区(规格 §9)。 */
  canManageCollaborators: boolean;
};

/**
 * 「点名共享」一节的门。门放在组件里而不是挂载点的条件表达式里,这样「非库主不渲染、
 * 不请求」能被组件测试直接钉住;挂载点只负责把能力位原样传进来(接线守卫钉那一侧)。
 */
export function NotebookCollaboratorsShare({ canManageCollaborators, ...rest }: NotebookCollaboratorsShareProps) {
  if (!canManageCollaborators) return null;
  return <CollaboratorsSection key={rest.notebookId} {...rest} />;
}

/**
 * 分享弹窗里的「点名共享」一节(分享编辑与协作 P1-T13,规格 §4.4 / §4.6 / §9)。
 *
 * 只给库主渲染(`notebook:collaborate` 恒 owner,门见上面的 `NotebookCollaboratorsShare`)。流程:用户名 → 查到显示名 → 「添加为编辑者」。
 *
 * * 添加前现读 `GET .../bases`:有 `closes_when_shared` 的参考库时,在按钮下方列出它们
 *   并要求二次确认(决策 D13 选 (a))。确认就在本节内联完成,不叠新的弹窗。
 * * 名单是 `GET .../collaborators` 返回的**全部**点名授权——编辑者以外的行(同步来的
 *   只读点名授权、遗留的管理权限)同样列出、同样可移除(规格 §4.4 完整性约束)。
 * * 每个动作的结果落在被按的按钮旁并自带计时消失;请求进行中整节按钮禁用。这里不碰
 *   页面顶部的状态条。
 */
function CollaboratorsSection({ notebookId, onChanged }: CollaboratorsSectionProps) {
  const [items, setItems] = useState<CollaboratorItem[] | null>(null);
  const [loadError, setLoadError] = useState("");
  const [username, setUsername] = useState("");
  const [target, setTarget] = useState<UserRef | null>(null);
  const [confirm, setConfirm] = useState<PendingConfirm | null>(null);
  /** 空串 = 不忙;否则是正在忙的那一格。 */
  const [busySlot, setBusySlot] = useState("");
  /** 刚移除、还在结果计时内保留展示的行。 */
  const [removed, setRemoved] = useState<ReadonlySet<string>>(() => new Set());
  const [result, showResult] = useSlotResult();
  const removedTimers = useRef(new Map<string, number>());
  const reloadRevision = useRef(0);
  // 切库 / 卸载之后落地的响应一律丢弃(同 NotebookGroupShare 的 liveRef)。
  const liveRef = useRef(true);
  useEffect(() => {
    liveRef.current = true;
    const timers = removedTimers.current;
    return () => {
      liveRef.current = false;
      for (const timer of timers.values()) window.clearTimeout(timer);
      timers.clear();
    };
  }, []);

  const reload = useCallback(async () => {
    const revision = ++reloadRevision.current;
    try {
      const list = await listCollaborators(notebookId);
      if (!liveRef.current || revision !== reloadRevision.current) return;
      setItems(list.items);
      setLoadError("");
    } catch (err) {
      // 添加后的重取已取代初次加载；旧结果与旧失败都不能覆盖这份新名单。
      if (liveRef.current && revision === reloadRevision.current) throw err;
    }
  }, [notebookId]);

  useEffect(() => {
    setItems(null);
    reload().catch((err) => {
      if (liveRef.current) setLoadError(toUserMessage(err, "点名共享名单加载失败"));
    });
  }, [reload]);

  const busy = busySlot !== "";
  const existing = target ? (items ?? []).find((row) => row.user_id === target.id && !removed.has(row.user_id)) : undefined;
  const alreadyEditor = existing?.role === "editor";

  async function findUser() {
    const name = username.trim();
    if (!name || busy) return;
    setBusySlot(FIND_SLOT);
    setTarget(null);
    setConfirm(null);
    showResult(null);
    try {
      const found = await resolveUser(name);
      if (liveRef.current) setTarget(found);
    } catch (err) {
      if (liveRef.current) {
        showResult({ slot: FIND_SLOT, tone: "failed", text: toUserMessage(err, "没能查找这个用户名，请稍后重试") });
      }
    } finally {
      if (liveRef.current) setBusySlot("");
    }
  }

  // 第一次点「添加为编辑者」:现读挂载边,需要确认就停在确认态,否则直接添加。`slot` 是
  // 被按的那颗按钮——查找行的「添加为编辑者」,或遗留管理权限行里的「改为可编辑」——
  // 确认区与结果都落在它旁边。
  async function startAdd(person: UserRef, wasAdmin: boolean, slot: string) {
    if (busy) return;
    setBusySlot(slot);
    setConfirm(null);
    showResult(null);
    let closing: MountedBase[];
    try {
      closing = basesClosedBySharing(await listBases(notebookId));
    } catch (err) {
      if (liveRef.current) {
        setBusySlot("");
        showResult({ slot, tone: "failed", text: toUserMessage(err, "没能确认参考库的状态，请稍后重试") });
      }
      return;
    }
    if (!liveRef.current) return;
    if (closing.length > 0 || wasAdmin) {
      setBusySlot("");
      setConfirm({ target: person, closing, wasAdmin, slot });
      return;
    }
    await commitAdd(person, slot);
  }

  async function commitAdd(person: UserRef, slot: string) {
    setBusySlot(slot);
    setConfirm(null);
    showResult(null);
    try {
      const outcome = await addEditor(notebookId, person.id);
      if (!liveRef.current) return;
      // 新授权已生效，旧移除结果的计时器不能再隐藏这个人的新行。
      const removedTimer = removedTimers.current.get(person.id);
      if (removedTimer !== undefined) window.clearTimeout(removedTimer);
      removedTimers.current.delete(person.id);
      setRemoved((current) => {
        if (!current.has(person.id)) return current;
        const next = new Set(current);
        next.delete(person.id);
        return next;
      });
      // PUT 的返回值已经确认授权存在；重取失败时也保留这条可继续撤销的授权。
      setItems((current) => [
        ...(current ?? []).filter((item) => item.user_id !== person.id), outcome.grant,
      ]);
      const text = !outcome.changed
        ? "已经是编辑者"
        : outcome.previous_role ? "已改为可编辑" : "已添加";
      showResult({ slot, tone: "ok", text });
      setUsername("");
      onChanged();
    } catch (err) {
      if (liveRef.current) showResult({ slot, tone: "failed", text: toUserMessage(err, "添加失败，请稍后重试") });
    } finally {
      if (liveRef.current) {
        await reload().catch(() => undefined);
        if (liveRef.current) setBusySlot("");
      }
    }
  }

  async function remove(row: CollaboratorItem) {
    if (busy) return;
    const slot = rowSlot(row.user_id);
    setBusySlot(slot);
    setConfirm(null);
    showResult(null);
    try {
      await removeCollaborator(notebookId, row.user_id);
      if (!liveRef.current) return;
      // 行在结果计时内留着、变淡,「已移除」就落在原来按钮的位置;到点再从名单里摘掉。
      setRemoved((prev) => new Set(prev).add(row.user_id));
      const previousTimer = removedTimers.current.get(row.user_id);
      if (previousTimer !== undefined) window.clearTimeout(previousTimer);
      const timer = window.setTimeout(() => {
        if (removedTimers.current.get(row.user_id) !== timer) return;
        removedTimers.current.delete(row.user_id);
        if (!liveRef.current) return;
        setRemoved((prev) => {
          const next = new Set(prev);
          next.delete(row.user_id);
          return next;
        });
        setItems((prev) => prev?.filter((item) => item.user_id !== row.user_id) ?? prev);
      }, COLLABORATOR_RESULT_HOLD_MS);
      removedTimers.current.set(row.user_id, timer);
      setBusySlot("");
      onChanged();
    } catch (err) {
      if (!liveRef.current) return;
      showResult({ slot, tone: "failed", text: toUserMessage(err, "移除失败，请稍后重试") });
      // 失败时重取:这一行可能早被移除了(另一个标签页),界面别再挂着一份过期名单。
      await reload().catch(() => undefined);
      if (liveRef.current) setBusySlot("");
    }
  }

  const resultFor = (slot: string) =>
    result && result.slot === slot
      ? <span className={`collaborator-result is-${result.tone}`} role="status">{result.text}</span>
      : null;
  // 行级结果对应的那一行已经不在名单里(失败后重取把它摘掉了):结果落到名单下方。
  const orphanRowResult = Boolean(
    result
    && result.slot.startsWith("row:")
    && !(items ?? []).some((row) => rowSlot(row.user_id) === result.slot || rowAddSlot(row.user_id) === result.slot),
  );

  /** 二次确认区。挨着发起它的那颗按钮:查找行下方,或那一条名单行下方。 */
  const confirmFor = (slot: string) => confirm && confirm.slot === slot ? (
    <div className="collaborator-confirm" role="group" aria-label="确认添加编辑者">
      {confirm.closing.length > 0 && (<>
        <span>这本笔记本借来的这些参考库：</span>
        <ul>
          {confirm.closing.map((base) => <li key={base.id}>{base.name}</li>)}
        </ul>
        <span>{EDITOR_CLOSES_BASES_WARNING}</span>
      </>)}
      {confirm.wasAdmin && <span>{collaboratorName(confirm.target)}{LEGACY_ADMIN_DOWNGRADE_NOTE}。</span>}
      <div className="group-row-actions">
        <button
          className="sort-button"
          disabled={busy}
          onClick={() => setConfirm(null)}
        >先不添加</button>
        <button
          className="new-pill"
          disabled={busy}
          onClick={() => { void commitAdd(confirm.target, confirm.slot); }}
        >{busySlot === confirm.slot ? "添加中…" : "确认添加"}</button>
      </div>
    </div>
  ) : null;

  return (
    <div className="stack">
      <span className="section-title">点名共享</span>
      <p className="tool-hint" style={{ margin: 0 }}>
        按用户名把人添加为编辑者：可以添加来源、整理知识图谱、编辑知识条目，只能删除自己上传的来源；改名、分享和删除这本笔记本仍只由你决定。
      </p>

      {loadError && <p className="password-change-status error">{loadError}</p>}

      {items === null ? (
        !loadError && <p className="tool-hint">加载中…</p>
      ) : items.length === 0 ? (
        <p className="tool-hint">还没有点名共享给任何人。</p>
      ) : (
        items.map((row) => {
          const slot = rowSlot(row.user_id);
          const addSlot = rowAddSlot(row.user_id);
          const isRemoved = removed.has(row.user_id);
          const person: UserRef = { id: row.user_id, username: row.username, display_name: row.display_name };
          return (
            <div className="stack" key={row.user_id}>
              <div className={isRemoved ? "group-row is-removed" : "group-row"}>
                <div className="group-row-main">
                  <span className="group-row-name">{collaboratorName(row)}</span>
                  <span className="group-chip">{collaboratorRoleLabel(row.role)}</span>
                  {row.role === "admin" && !isRemoved && (
                    <span className="group-row-note">
                      早先留下的管理权限，比编辑者多出改名和管理群组共享。可以
                      <button
                        type="button"
                        className="link-button inline-action"
                        disabled={busy}
                        onClick={() => { void startAdd(person, false, addSlot); }}
                      >{busySlot === addSlot ? "处理中…" : "改为可编辑"}</button>
                      ，或直接移除。
                    </span>
                  )}
                </div>
                <div className="group-row-actions">
                  {isRemoved ? (
                    <span className="collaborator-result is-ok" role="status">已移除</span>
                  ) : (<>
                    {resultFor(addSlot)}
                    {resultFor(slot)}
                    <button
                      className="sort-button"
                      disabled={busy}
                      onClick={() => { void remove(row); }}
                    >{busySlot === slot ? "移除中…" : "移除"}</button>
                  </>)}
                </div>
              </div>
              {confirmFor(addSlot)}
            </div>
          );
        })
      )}
      {orphanRowResult && result && resultFor(result.slot)}

      <div className="group-form-row">
        <input
          className="group-input"
          value={username}
          aria-label="要添加的用户名"
          placeholder="输入对方的用户名"
          disabled={busy}
          onChange={(event) => {
            setUsername(event.target.value);
            setTarget(null);
            setConfirm(null);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void findUser();
            }
          }}
        />
        <button
          className="new-pill"
          disabled={busy || !username.trim()}
          onClick={() => { void findUser(); }}
        >{busySlot === FIND_SLOT ? "查找中…" : "查找"}</button>
        {resultFor(FIND_SLOT)}
      </div>

      {target && (
        <div className="group-row">
          <div className="group-row-main">
            <span className="group-row-name">{collaboratorName(target)}</span>
            {existing && <span className="group-chip">{collaboratorRoleLabel(existing.role)}</span>}
            {existing?.role === "admin" && <span className="group-row-note">{LEGACY_ADMIN_DOWNGRADE_NOTE}</span>}
            {existing && existing.role !== "admin" && existing.role !== "editor" && (
              <span className="group-row-note">当前为只读，添加后改为可编辑</span>
            )}
          </div>
          <div className="group-row-actions">
            {resultFor(ADD_SLOT)}
            {confirm?.slot !== ADD_SLOT && (
              <button
                className="new-pill"
                disabled={busy || alreadyEditor}
                onClick={() => { void startAdd(target, existing?.role === "admin", ADD_SLOT); }}
              >{busySlot === ADD_SLOT ? "添加中…" : alreadyEditor ? "已是编辑者" : "添加为编辑者"}</button>
            )}
          </div>
        </div>
      )}
      {confirmFor(ADD_SLOT)}
    </div>
  );
}
