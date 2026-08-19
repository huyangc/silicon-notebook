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
  SHARE_UPDATE_COUNTS_ERROR,
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

export function ConversationShareModal({
  notebookId,
  conversationId,
  title,
  onClose,
}: {
  notebookId: string;
  conversationId: string;
  title: string;
  onClose: () => void;
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
  const [countsError, setCountsError] = useState(false);
  const [busy, setBusy] = useState<BusyAction>("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError("");
    // 每次重跑（切会话/切库导致 notebookId/conversationId 变）都要清掉上一轮的
    // countsError，否则一次失败后即便重跑成功，"可能包含个人记忆摘录"的兜底告警
    // 会残留在一个已经算得出精确数字的会话上（codex T5 评审 P2-4）。
    setCountsError(false);
    // 分享状态与会话详情各自成败：详情失败只影响披露计数，不该拦住分享本身。
    const shareStatus = getConversationShare(notebookId, conversationId)
      .then((resp) => resp)
      .catch((err) => {
        if (httpErrorStatus(err) === 404) return null; // 未分享
        throw err;
      });
    const detail = getConversation(conversationId)
      .then((d) => d)
      .catch(() => null);
    Promise.all([shareStatus, detail])
      .then(([share, conversation]) => {
        if (cancelled) return;
        if (share) {
          setToken(share.share_token || "");
          setWatermark(share.shared_through_at || "");
          setWatermarkId(share.shared_through_id || "");
        }
        if (conversation) setTurns(conversation.turns || []);
        else setCountsError(true);
      })
      .catch((err) => {
        if (!cancelled) setError(toUserMessage(err, "加载分享状态失败"));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [notebookId, conversationId]);

  const shared = Boolean(token);
  const link = shared ? buildPublicConversationLink(token, typeof window !== "undefined" ? window.location.origin : "") : "";
  const disclosure = useMemo(
    () => summarizeShareDisclosure(turns, shared ? watermarkId : "", watermark),
    [turns, watermarkId, watermark, shared],
  );
  // 前瞻披露:「更新到最新」会把水位推到全部轮次,consent 判据是**这个按钮将要公开
  // 什么**,所以它必须在点击前就披露更新后的记忆/附图条数(codex #522 R1 P1)。
  const updatePreview = useMemo(
    () => summarizeShareUpdate(turns, shared ? watermarkId : "", watermark),
    [turns, watermarkId, watermark, shared],
  );

  async function doShare(action: "share" | "update") {
    if (busy) return;
    setBusy(action);
    setError("");
    setNotice("");
    try {
      // 水位钉死在弹窗据以算披露的那批 `turns` 的**最新**一条(ASC 排序,末条即最新)。
      // 发布的快照 == 披露的快照(codex #522 R2 P1)。详情没加载出来(countsError)时
      // turns 为空,expected="" 回退「当前最新」——那种情形披露也已退化成不带数字的告警。
      const expectedThroughId = turns.length ? turns[turns.length - 1].answer_id : "";
      const resp = await shareConversation(notebookId, conversationId, expectedThroughId);
      if (!aliveRef.current) return;
      setToken(resp.share_token || "");
      setWatermark(resp.shared_through_at || "");
      setWatermarkId(resp.shared_through_id || "");
      setNotice(action === "update" ? "已更新到最新" : "已生成分享链接");
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

  async function doCopy() {
    if (busy || !link) return;
    setBusy("copy");
    setError("");
    try {
      await copyToClipboard(link);
      if (aliveRef.current) setNotice("分享链接已复制");
    } catch {
      if (aliveRef.current) setNotice("自动复制失败，请手动复制上面的链接");
    } finally {
      if (aliveRef.current) setBusy("");
    }
  }

  return (
    <section className="utility-modal" role="dialog" aria-modal="true">
      <FloatingModalCard storageKey="conversation-share.window" className="utility-modal-card narrow">
        {(floating) => (<>
          <div className="source-modal-header" {...floating.dragHandleProps}>
            <div>
              <h2>分享会话</h2>
              <p style={{ wordBreak: "break-word" }}>{title || "未命名会话"}</p>
            </div>
            <button className="icon-button" onClick={onClose} title="关闭">×</button>
          </div>
          <div className="source-detail-body conversation-share-body">
            {loading ? (
              <p className="tool-hint" style={{ margin: 0 }}>正在加载…</p>
            ) : (<>
              {error && <p className="password-change-status error">{error}</p>}
              {notice && <p className="tool-hint" style={{ margin: 0 }}>{notice}</p>}

              <p className="tool-hint" style={{ margin: 0 }}>
                发布成一条免登录的只读快照。分享后新问的问题不会自动出现，需要再点一次
                「更新到最新」。撤销即刻失效。
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

                {disclosure.newCount > 0 && (
                  <div className="conversation-share-update">
                    {/* consent 红线:披露必须在按钮**之前**呈现,点了才涨会先公开
                        水位之后新轮引用的私有 Memory(codex #522 R1 P1)。 */}
                    <ShareUpdateDisclosureLines preview={updatePreview} countsError={countsError} />
                    <button
                      type="button"
                      className="sort-button"
                      disabled={busy !== ""}
                      onClick={() => void doShare("update")}
                    >
                      <RefreshCw size={13} className={busy === "update" ? "busy-spin" : undefined} />
                      {" "}{busy === "update" ? "更新中…" : "更新到最新"}
                    </button>
                  </div>
                )}

                <ShareDisclosureLines disclosure={disclosure} countsError={countsError} />

                <button
                  type="button"
                  className="sort-button conversation-share-revoke"
                  disabled={busy !== ""}
                  onClick={() => void doRevoke()}
                >
                  <X size={13} /> {busy === "revoke" ? "撤销中…" : "撤销分享"}
                </button>
              </>) : (<>
                <ShareDisclosureLines disclosure={disclosure} countsError={countsError} />
                <button
                  type="button"
                  className="button conversation-share-cta"
                  disabled={busy !== ""}
                  onClick={() => void doShare("share")}
                >
                  <Link2 size={14} className={busy === "share" ? "busy-spin" : undefined} />
                  {" "}{busy === "share" ? "分享中…" : "生成分享链接"}
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
}: {
  preview: ShareUpdatePreview;
  countsError: boolean;
}) {
  if (countsError) {
    return (
      <p className="tool-hint" style={{ margin: 0 }}>
        {SHARE_UPDATE_COUNTS_ERROR}
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
