"use client";

// 会话公开分享弹窗（T5，设计 §八）。
//
// 刻意**不是**笔记本链接分享那个弹窗：两者爆炸半径完全不同，合在一起会让用户以为
// 撤销其一会撤销另一个。这里发放的是一条**只读会话快照**的免登录链接。
//
// 三条与报告分享同源的规矩：
//   * 「分享」与「更新到最新」是**同一个** POST（幂等复用链接口令，同时把水位推到
//     当前）；界面按当前是否已分享决定按钮文案。
//   * 每个「点一下就发请求」的按钮都给按该动作语义写的进行态文案（分享中/更新中/
//     撤销中），点完立即不可点（仓库长任务按钮红线）。
//   * 分享请求是异步的、而弹窗态是会话级：完成时用 `aliveRef` 确认弹窗还挂着、没被
//     切到别的会话，否则把上一个会话的分享态按到新会话头上（镜像 report-view 的
//     activeIdRef 手法；本弹窗按 conversationId 作 key，切会话即重挂，aliveRef 足矣）。
//
// 两条披露（设计 §五 是用户 consent 红线）：包含 M 张附图、包含 K 条个人记忆摘录。
// 计数不来自 share 回执（它只给链接口令 + 水位），而是打开弹窗时加载该会话、按**水位
// 之前**的轮次统计。**附图与 Memory 披露都绝不可静默省略**：M>0 / K>0 各自必显示；加载
// 失败时退化成不带数字、但**两者都提**的兜底文案（见 conversation-share-disclosure 里的
// SHARE_*_COUNTS_ERROR 常量），而不是只提其一或不显示。

import { useEffect, useMemo, useRef, useState } from "react";
import { Copy, Link2, RefreshCw, X } from "lucide-react";

import { getConversation, getConversationShare, shareConversation, unshareConversation } from "./ask-api.ts";
import { buildPublicConversationLink } from "./public-conversation.ts";
import { FloatingModalCard } from "./floating-modal-card.tsx";
import { httpErrorStatus, toUserMessage } from "./errors.ts";
import {
  SHARE_DISCLOSURE_COUNTS_ERROR,
  SHARE_UPDATE_BOUNDED_COUNTS_ERROR,
  SHARE_UPDATE_COUNTS_ERROR,
  resolveShareBoundary,
  shareScopeState,
  summarizeShareDisclosure,
  summarizeShareUpdate,
  type ShareDisclosure,
  type ShareUpdatePreview,
} from "./conversation-share-disclosure.ts";
import type { ConversationDetail } from "./workspace-model.ts";

/** 复制到剪贴板：优先 navigator.clipboard，回退隐藏 textarea + execCommand。 */
async function copyToClipboard(text: string): Promise<void> {
  if (navigator?.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try {
    if (!document.execCommand("copy")) throw new Error("clipboard copy was rejected");
  } finally {
    document.body.removeChild(area);
  }
}

type BusyAction = "" | "share" | "update" | "revoke" | "copy";

/** 复制前那次水位复核的结果。三值而非布尔：`changed`（确实变了，界面已刷新）与
 *  `unavailable`（这次读不到，界面**没**变）都拦下复制，但对用户是两件不同的事，
 *  给同一句话就是在编造一次并不存在的变化（codex #530 R4 P2）。 */
type ShareStateCheck = "same" | "changed" | "unavailable";

/** 会话列表那个入口的既有文案，一个字未动。 */
const UNBOUNDED_SCOPE_COPY =
  "发布成一条免登录的只读快照。分享后新问的问题不会自动出现，需要再点一次「更新到最新」。撤销即刻失效。";

/** 边界模式（分享到某条回答为止）按 `ShareScopeState` 分五句。每一句描述的都是**当前**
 *  链接里有什么——只有 `unshared` 那句才是对「按下去会发布什么」的承诺，因为那时还没有
 *  链接可复制。三条评审意见（#530 R1 P1、R2 P1、R2 P2）全部出在把这五种情形写成一句。 */
const BOUNDED_SCOPE_COPY: Record<ReturnType<typeof shareScopeState>, string> = {
  unshared:
    "发布成一条免登录的只读快照，只包含这条回答以及它之前的问答。之后的问答不会出现在链接里。撤销即刻失效。",
  at: "链接的内容就到这条回答为止，它之后的问答不在里面。撤销即刻失效。",
  behind:
    "当前链接停在更早的一轮，还不包含这条回答——现在复制发出去的是不含它的快照。点「更新到这一条」才会把它纳入。撤销即刻失效。",
  ahead:
    "这条会话此前已经分享过，链接覆盖的范围比你点的这条回答更靠后。公开范围只能往后推、不能收回，所以它不会被缩小到这一条。撤销即刻失效。",
  unknown:
    "无法确认当前链接的范围——它可能已在别处被推进到这里看不到的轮次。现在复制发出去的内容可能多于这条回答，请刷新后重新查看。",
};

export function ConversationShareModal({
  notebookId,
  conversationId,
  title,
  throughAnswerId = "",
  onClose,
  interactive = true,
  zIndex,
}: {
  notebookId: string;
  conversationId: string;
  title: string;
  /** 发布边界：分享到**这条答案**为止，即每条回答下面那个分享按钮（T6）。空串 = 整条
   *  会话 /「更新到最新」，也就是会话列表里那个按钮的既有语义——该模式下本组件的行为
   *  与接入前逐字相同（`resolveShareBoundary` 原样返回 turns，两个 ahead 位恒 false）。 */
  throughAnswerId?: string;
  onClose: () => void;
  interactive?: boolean;
  zIndex?: number;
}) {
  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const [loading, setLoading] = useState(true);
  const [token, setToken] = useState("");
  const [watermark, setWatermark] = useState("");
  // 水位答案 id——「已分享 vs 新增」的权威分类判据(codex #522 R3),按它在权威 turn
  // 顺序里的位置分,而不是 created_at 时间戳。watermark（时间戳）只作显示与删除兜底。
  const [watermarkId, setWatermarkId] = useState("");
  const [turns, setTurns] = useState<ConversationDetail["turns"]>([]);
  // 会话详情没加载出来 → 无法算数。Memory 披露此时退化成不带数字的告警，绝不省略。
  // **仍可分享**（水位/是否已分享是另一路独立加载的）。
  const [countsError, setCountsError] = useState(false);
  // 分享状态（是否已分享 + 水位）**非 404** 加载失败 → 当前是否已分享/水位**未知**。
  // 此时**不可**分享:发空 expected 会让服务端按当前最新兜底发布(可能推进一个已存在
  // 的隐藏分享,或在未显示任何披露的情况下公开)。与 countsError 是两回事——那个是「披露
  // 算不出但可分享」,这个是「分享状态未知、禁用分享」(codex #522 R6 P1)。404(未分享)
  // 是正常态、仍可分享,不置此位。
  const [shareStateError, setShareStateError] = useState(false);
  const [busy, setBusy] = useState<BusyAction>("");
  const busyRef = useRef<BusyAction>("");
  busyRef.current = busy;
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    // 每次重跑（切会话/切库导致 notebookId/conversationId 变）都要清掉上一轮的
    // countsError / shareStateError，否则一次失败后即便重跑成功，兜底告警/禁用态
    // 会残留在一个已经算得出精确数字、已加载出分享状态的会话上（codex T5 评审 P2-4）。
    setCountsError(false);
    setShareStateError(false);
    // 分享状态与会话详情**各自成败、各自 set**：绝不用 Promise.all 的全有全无——
    // 分享状态非 404 失败时详情可能已成功（披露仍算得出），反之亦然。旧写法把两者
    // 塞进一个 Promise.all，任一 reject 就在 set turns 之前整体短路，turns 停在 []、
    // countsError 停在 false，而 loading 仍被 finally 清掉 → CTA 可点 → 发空 expected
    // → 服务端按当前最新兜底发布，**未显示任何披露就公开了**（codex #522 R6 P1）。
    const shareDone = getConversationShare(notebookId, conversationId)
      .then((share) => {
        if (cancelled) return;
        setToken(share.share_token || "");
        setWatermark(share.shared_through_at || "");
        setWatermarkId(share.shared_through_id || "");
      })
      .catch((err) => {
        if (cancelled) return;
        if (httpErrorStatus(err) === 404) return; // 未分享：正常态，仍可分享
        // 非 404 → 当前是否已分享/水位**未知**，无法安全发布 → 禁用分享 CTA + 可操作
        // 错误。这与 countsError（披露算不出但可分享）是两条独立的降级路径。
        setShareStateError(true);
        setError(toUserMessage(err, "分享状态加载失败，请重试"));
      });
    const detailDone = getConversation(conversationId)
      .then((conversation) => {
        if (!cancelled) setTurns(conversation.turns || []);
      })
      .catch(() => {
        // 详情失败：披露算不出但仍可分享，退化成不带数字的兜底告警（countsError）。
        if (!cancelled) setCountsError(true);
      });
    // 两条链都自己 catch、绝不 reject，allSettled 在这里只用来「等两者都落定后收 loading」。
    Promise.allSettled([shareDone, detailDone]).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, [notebookId, conversationId]);

  const shared = Boolean(token);
  const link = shared ? buildPublicConversationLink(token, typeof window !== "undefined" ? window.location.origin : "") : "";
  const bounded = Boolean(throughAnswerId);
  // 本次要发布的那批轮次。`throughAnswerId` 为空时逐字等于 `turns`（既有语义）;非空时
  // 截到那条答案（含）为止——披露、`expected_through_id` 与所有按钮文案都从同一批派生,
  // 「披露的快照 == 发布的快照」这条不变量因此对两种模式同时成立。
  const boundary = useMemo(
    () => resolveShareBoundary(turns, throughAnswerId, shared ? watermarkId : ""),
    [turns, throughAnswerId, watermarkId, shared],
  );
  const scopedTurns = boundary.turns;
  // 当前链接相对这条回答处在什么位置——所有范围文案的唯一判据（五值，不是「越没越过」
  // 的二值；codex #530 R1/R2 三条都出在把它当二值上）。
  const scope = shareScopeState(boundary, shared);
  // 边界解析不出（详情没加载出来,或这条答案已不在会话里）→ 一个数都算不出,与详情加载
  // 失败同一条退化路径:不带数字、但**附图与个人记忆两个面都提**的兜底文案。仍可发布——
  // expected 是用户点的那条 id,不依赖 turns。水位指向本地看不到的答案（unknown）同理:
  // 链接的真实范围我们答不上来,给数字就是编（codex #530 R2 P1）。
  const countsUnavailable = countsError || boundary.unresolved || boundary.watermarkUnknown;
  // ⚠ 水位已越过边界时,当前链接实际公开的是「整条到水位为止」而不是用户点的那一段,所以
  // 披露必须按**完整** turns 统计。按截断批次算会**少报**公开页真实包含的附图/记忆条数——
  // 披露描述的是「链接里有什么」,不是「用户点了哪一条」。unknown 同走这一支(它至少是
  // 我们已知的上界),数字本身已由 countsUnavailable 压掉。
  const disclosureTurns = (boundary.watermarkAhead || boundary.watermarkUnknown) ? turns : scopedTurns;
  const disclosure = useMemo(
    () => summarizeShareDisclosure(disclosureTurns, shared ? watermarkId : "", watermark),
    [disclosureTurns, watermarkId, watermark, shared],
  );
  // 前瞻披露:推进水位会公开新增轮次,consent 判据是**这个按钮将要公开什么**,所以它必须
  // 在点击前就披露更新后的记忆/附图条数(codex #522 R1 P1)。边界模式下「更新后」的范围
  // 就是截断批次(只消费于 `canAdvance`,而那要求 !watermarkAhead,故这里用 scopedTurns)。
  const updatePreview = useMemo(
    () => summarizeShareUpdate(scopedTurns, shared ? watermarkId : "", watermark),
    [scopedTurns, watermarkId, watermark, shared],
  );
  // 「更新」块的渲染条件。既有模式沿用 newCount>0（含 countsError 时 turns 为空 → 不显示,
  // 逐字保留接入前行为）;边界模式另接住 countsUnavailable——算不出新增轮数,但确实还没
  // 发布到这里,藏起来等于把一次合法的推进变没了。水位已越过边界时后端只会 409,所以那一
  // 支不给按钮（见 `ShareBoundary` 的注释:后端水位 advance-only）。
  const canAdvance = shared
    && !boundary.watermarkAhead
    && (disclosure.newCount > 0 || (bounded && countsUnavailable));

  async function doShare(action: "share" | "update") {
    // 防御性复查:分享状态未知时绝不发布(CTA 已 disabled,这里兜底 codex #522 R6 P1)。
    if (busy || shareStateError) return;
    setBusy(action);
    setError("");
    setNotice("");
    try {
      // 水位钉死在弹窗据以算披露的那批轮次的**最新**一条(ASC 排序,末条即最新)。
      // 发布的快照 == 披露的快照(codex #522 R2 P1)。详情没加载出来(countsError)时
      // turns 为空,expected="" 回退「当前最新」——那种情形披露也已退化成不带数字的告警。
      //
      // ⚠ 边界模式下 `throughAnswerId` **本人**就是 expected,不经 turns 兜底:详情加载
      // 失败时回退成 "" 会让服务端按**当前最新**发布,而那恰恰是用户没点的那些轮次——
      // 界面上写着「分享到这一条」,发出去的却是整条会话。
      const expectedThroughId = throughAnswerId
        || (scopedTurns.length ? scopedTurns[scopedTurns.length - 1].answer_id : "");
      const resp = await shareConversation(notebookId, conversationId, expectedThroughId);
      if (!aliveRef.current) return;
      setToken(resp.share_token || "");
      setWatermark(resp.shared_through_at || "");
      setWatermarkId(resp.shared_through_id || "");
      setNotice(
        action === "update"
          ? (bounded ? "已更新到这一条" : "已更新到最新")
          : (bounded ? "已生成分享链接（到这一条为止）" : "已生成分享链接"),
      );
    } catch (err) {
      if (!aliveRef.current) return;
      setError(toUserMessage(err, action === "update" ? "更新失败" : "分享失败"));
    } finally {
      if (aliveRef.current) setBusy("");
    }
  }

  async function doRevoke() {
    if (busy) return;
    setBusy("revoke");
    setError("");
    setNotice("");
    try {
      await unshareConversation(notebookId, conversationId);
      if (!aliveRef.current) return;
      setToken("");
      setWatermark("");
      setWatermarkId("");
      setNotice("已取消分享，原链接立即失效");
    } catch (err) {
      if (!aliveRef.current) return;
      setError(toUserMessage(err, "撤销失败"));
    } finally {
      if (aliveRef.current) setBusy("");
    }
  }

  /** 重读分享状态并回答「与此刻显示的范围是否一致」（codex #530 R3 P1）。
   *
   *  它关的是**弹窗打开之后**的漂移：另一个标签页推进同一条分享时 token 是稳定的，
   *  于是链接当场就指向更多轮次，而这边的范围文案还停在初次加载那一刻。前面几条修的
   *  都是「加载时就已经不一致」，这条是「加载后才不一致」。
   *
   *  ⚠ 它**只**关得住按钮那条路。链接框是可选中的 readonly input（`onFocus` 还会全选），
   *  用户手动选中复制我们拦不住——所以这是尽力而为的收敛，不是保证。也正因如此，读不到
   *  状态时按「不一致」处理：拦不住的路已经够多了，这条能拦的不该放过。
   *
   *  刻意**不**碰 `shareStateError`：那个位表示「不知道是否已分享 → 不能安全发布」，而
   *  发布本身是安全的（`expected_through_id` 显式送出，服务端按 advance-only 钉住或 409），
   *  危险的只有「照着一句已经过时的范围声明把链接发出去」。一次网络抖动不该顺手废掉发布。 */
  async function refreshShareState(): Promise<ShareStateCheck> {
    try {
      const share = await getConversationShare(notebookId, conversationId);
      if (!aliveRef.current) return "same";
      const nextToken = share.share_token || "";
      const nextAt = share.shared_through_at || "";
      const nextId = share.shared_through_id || "";
      const same = nextToken === token && nextAt === watermark && nextId === watermarkId;
      setToken(nextToken);
      setWatermark(nextAt);
      setWatermarkId(nextId);
      return same ? "same" : "changed";
    } catch (err) {
      if (!aliveRef.current) return "same";
      if (httpErrorStatus(err) === 404) {
        // 已在别处撤销。链接当场失效，与「范围变了」是两回事，但同样不该继续复制。
        setToken("");
        setWatermark("");
        setWatermarkId("");
        return token === "" ? "same" : "changed";
      }
      // ⚠ 「读不到」与「变了」必须分开回报（codex #530 R4 P2）。两者都拦下复制——拦是
      // 刻意的 fail-closed，见上面的注释——但说成「已在别处变化，说明已刷新」是**假的**：
      // 什么都没变，也什么都没刷新。这个 PR 从头到尾修的就是「别说不真的范围」，收尾处
      // 自己犯一次同样的错说不过去。
      return "unavailable";
    }
  }

  // 另一个标签页改了分享之后，用户多半是切回本窗口才动手复制的——所以窗口重新获得焦点
  // 时先对一次账，让范围文案在任何复制方式（含手动选中）之前就已经刷新。不做轮询：这是
  // 事件驱动的一次读取，弹窗本身也活不长。
  useEffect(() => {
    if (!token) return;
    const onFocus = () => {
      if (busyRef.current) return; // 别打断正在飞的动作
      void refreshShareState();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  });

  async function doCopy() {
    if (busy || !link) return;
    setBusy("copy");
    setError("");
    try {
      // 复制是链接真正离开本机的那一刻，也是上面那句范围声明变成行动的那一刻。
      const check = await refreshShareState();
      if (!aliveRef.current) return;
      if (check === "changed") {
        setNotice("分享范围已在别处变化，上面的说明已刷新——确认后再复制。");
        return;
      }
      if (check === "unavailable") {
        setNotice("暂时无法确认分享范围，请稍后重试。");
        return;
      }
      await copyToClipboard(link);
      if (aliveRef.current) setNotice("分享链接已复制");
    } catch {
      if (aliveRef.current) setNotice("自动复制失败，请手动复制上面的链接");
    } finally {
      if (aliveRef.current) setBusy("");
    }
  }

  return (
    <section className="utility-modal" role="dialog" aria-modal={interactive} aria-hidden={!interactive} inert={interactive ? undefined : true} style={{ zIndex }}>
      <FloatingModalCard storageKey="conversation-share.window" className="utility-modal-card narrow">
        {(floating) => (<>
          <div className="source-modal-header" {...floating.dragHandleProps}>
            <div>
              <h2>分享会话</h2>
              <p style={{ wordBreak: "break-word" }}>{title || "未命名会话"}</p>
              {bounded && !boundary.unresolved && (
                <p className="tool-hint" style={{ margin: "4px 0 0" }}>
                  分享至第 {boundary.index + 1} 轮回答（本会话共 {turns.length} 轮）。
                </p>
              )}
            </div>
            <button className="icon-button" onClick={onClose} title="关闭">×</button>
          </div>
          <div className="source-detail-body conversation-share-body">
            {loading ? (
              <p className="tool-hint" style={{ margin: 0 }}>正在加载…</p>
            ) : (<>
              {error && <p className="password-change-status error">{error}</p>}
              {notice && <p className="tool-hint" style={{ margin: 0 }}>{notice}</p>}

              {/* ⚠ 这句话排在链接框与复制按钮**之上**，所以它必须描述链接**当前**的内容，
                  而不是「按下按钮会发布什么」——用户读完这句就复制发出去了，纠正写在下面
                  没用（codex #530 R1 P1 / R2 P2）。五个状态各一句，不共用。 */}
              <p className="tool-hint" style={{ margin: 0 }}>
                {bounded ? BOUNDED_SCOPE_COPY[scope] : UNBOUNDED_SCOPE_COPY}
              </p>

              {shared ? (<>
                <div className="conversation-share-link">
                  <Link2 size={14} />
                  <input readOnly value={link} onFocus={(event) => event.currentTarget.select()} aria-label="分享链接" />
                  <button
                    type="button"
                    className="sort-button"
                    disabled={busy !== ""}
                    onClick={() => void doCopy()}
                  >
                    <Copy size={13} /> {busy === "copy" ? "复制中…" : "复制"}
                  </button>
                </div>

                <p className="tool-hint" style={{ margin: 0 }}>
                  内容截至 <ShareTime value={watermark} />
                  {disclosure.newCount > 0 && `，之后新增 ${disclosure.newCount} 轮未包含`}
                </p>

                {/* 水位已经越过用户点的这条回答。后端水位 advance-only,把范围收回来在
                    服务端做不到(只会换回一句"这条会话已有变化"的 409,而实际什么都没变),
                    所以这一支**不给发布按钮**,改为说清现状与唯一可行的出路:先撤销、再从
                    这条回答重新分享(撤销会清空水位,重发即可钉在更早的边界上)。 */}
                {boundary.watermarkAhead && (
                  <p className="tool-hint" style={{ margin: 0 }}>
                    这条回答已经在链接里了——当前链接还多包含它之后的 {boundary.aheadCount} 轮。
                    公开范围只能往后推、不能收回；要缩小到这一条，请先「撤销分享」，再从这条回答重新分享。
                  </p>
                )}

                {canAdvance && (
                  <div className="conversation-share-update">
                    {/* consent 红线:披露必须在按钮**之前**呈现,点了才涨会先公开
                        水位之后新轮引用的私有 Memory(codex #522 R1 P1)。 */}
                    <ShareUpdateDisclosureLines preview={updatePreview} countsError={countsUnavailable} bounded={bounded} />
                    <button
                      type="button"
                      className="sort-button"
                      disabled={busy !== "" || shareStateError}
                      onClick={() => void doShare("update")}
                    >
                      <RefreshCw size={13} className={busy === "update" ? "busy-spin" : undefined} />
                      {" "}{busy === "update" ? "更新中…" : (bounded ? "更新到这一条" : "更新到最新")}
                    </button>
                  </div>
                )}

                <ShareDisclosureLines disclosure={disclosure} countsError={countsUnavailable} />

                <button
                  type="button"
                  className="sort-button conversation-share-revoke"
                  disabled={busy !== ""}
                  onClick={() => void doRevoke()}
                >
                  <X size={13} /> {busy === "revoke" ? "撤销中…" : "撤销分享"}
                </button>
              </>) : (<>
                <ShareDisclosureLines disclosure={disclosure} countsError={countsUnavailable} />
                <button
                  type="button"
                  className="button conversation-share-cta"
                  disabled={busy !== "" || shareStateError}
                  onClick={() => void doShare("share")}
                >
                  <Link2 size={14} className={busy === "share" ? "busy-spin" : undefined} />
                  {" "}{busy === "share" ? "分享中…" : (bounded ? "分享到这一条" : "生成分享链接")}
                </button>
              </>)}
            </>)}
          </div>
        </>)}
      </FloatingModalCard>
    </section>
  );
}

/** 两条披露文案。Memory 那条只要 K>0 或计数加载失败就必显示（红线：绝不静默省略）。 */
function ShareDisclosureLines({
  disclosure,
  countsError,
}: {
  disclosure: ShareDisclosure;
  countsError: boolean;
}) {
  return (
    <div className="conversation-share-disclosure">
      {countsError ? (
        <p className="tool-hint" style={{ margin: 0 }}>
          {SHARE_DISCLOSURE_COUNTS_ERROR}
        </p>
      ) : (<>
        {disclosure.imageCount > 0 && (
          <p className="tool-hint" style={{ margin: 0 }}>公开页会包含 {disclosure.imageCount} 张附图。</p>
        )}
        {disclosure.memoryCount > 0 && (
          <p className="tool-hint" style={{ margin: 0 }}>
            公开页会包含 {disclosure.memoryCount} 条你引用到的个人记忆摘录。
          </p>
        )}
      </>)}
    </div>
  );
}

/** 「更新到最新」的前瞻披露:更新后公开页**全部**轮次会包含多少条记忆/附图,以及其中
 *  几条是本次更新才新暴露的(设计 §五 consent 红线;codex #522 R1 P1)。Memory 那条只要
 *  afterUpdate.memoryCount>0 或计数加载失败就必显示——绝不静默省略。 */
function ShareUpdateDisclosureLines({
  preview,
  countsError,
  bounded,
}: {
  preview: ShareUpdatePreview;
  countsError: boolean;
  /** 边界模式（分享到某条回答为止）。只影响措辞:兜底文案里的按钮名必须与旁边那个
   *  按钮上写的字一致,否则用户据以决定的那句话说的是另一个公开范围。 */
  bounded?: boolean;
}) {
  if (countsError) {
    return (
      <p className="tool-hint" style={{ margin: 0 }}>
        {bounded ? SHARE_UPDATE_BOUNDED_COUNTS_ERROR : SHARE_UPDATE_COUNTS_ERROR}
      </p>
    );
  }
  const { afterUpdate, newMemoryCount, newImageCount } = preview;
  return (
    <div className="conversation-share-disclosure">
      {afterUpdate.imageCount > 0 && (
        <p className="tool-hint" style={{ margin: 0 }}>
          更新后公开页共 {afterUpdate.imageCount} 张附图{newImageCount > 0 ? `（新增 ${newImageCount} 张）` : ""}。
        </p>
      )}
      {afterUpdate.memoryCount > 0 && (
        <p className="tool-hint" style={{ margin: 0 }}>
          更新后公开页共 {afterUpdate.memoryCount} 条你引用到的个人记忆摘录{newMemoryCount > 0 ? `（新增 ${newMemoryCount} 条）` : ""}。
        </p>
      )}
    </div>
  );
}

/** 浏览器本地时区渲染水位时刻；SSR 期先留空避免 hydration 不一致。 */
function ShareTime({ value }: { value: string }) {
  const [text, setText] = useState("");
  useEffect(() => {
    if (!value) return setText("");
    const parsed = new Date(value);
    setText(Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString());
  }, [value]);
  return <>{text}</>;
}
