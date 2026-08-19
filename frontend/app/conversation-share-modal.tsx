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
// 之前**的轮次统计。**Memory 披露绝不可静默省略**：只要 K>0 必显示；加载失败时退化成
// 不带数字的「可能包含个人记忆摘录」，而不是不显示。

import { useEffect, useMemo, useRef, useState } from "react";
import { Copy, Link2, RefreshCw, X } from "lucide-react";

import { getConversation, getConversationShare, shareConversation, unshareConversation } from "./ask-api.ts";
import { buildPublicConversationLink } from "./public-conversation.ts";
import { FloatingModalCard } from "./floating-modal-card.tsx";
import { httpErrorStatus, toUserMessage } from "./errors.ts";
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

type ShareDisclosure = {
  sharedCount: number;
  newCount: number;
  imageCount: number;
  memoryCount: number;
};

/** 一轮的 created_at 是否落在水位（含）之前。水位为空（未分享）时全部计入——那是
 *  按下「分享」将要发布的预览。任一时刻解析失败按"计入"处理（宁可多披露）。 */
function withinWatermark(createdAt: string, watermark: string): boolean {
  if (!watermark) return true;
  const created = new Date(createdAt).getTime();
  const mark = new Date(watermark).getTime();
  if (Number.isNaN(created) || Number.isNaN(mark)) return true;
  return created <= mark;
}

/**
 * 按水位统计快照的披露计数——纯函数，方便对"Memory 披露不省略"这条红线单测。
 *
 * M（附图）：各轮 anchors ∪ citations 里图片按 asset_id 去重后逐轮求和（读者每轮
 * 看到几张就是几张）。K（记忆）：各轮 citations 里 memory_id 非空的按 memory_id
 * 去重（K 条不同的个人记忆，而不是被引用几次）。
 */
export function summarizeShareDisclosure(
  turns: ConversationDetail["turns"],
  watermark: string,
): ShareDisclosure {
  let sharedCount = 0;
  let newCount = 0;
  let imageCount = 0;
  const memoryIds = new Set<string>();
  for (const turn of turns) {
    if (!withinWatermark(turn.created_at, watermark)) {
      newCount += 1;
      continue;
    }
    sharedCount += 1;
    const response = turn.response || ({} as ConversationDetail["turns"][number]["response"]);
    const assetIds = new Set<string>();
    for (const anchor of response.anchors || []) {
      for (const image of anchor.images || []) {
        if (image.asset_id) assetIds.add(image.asset_id);
      }
    }
    for (const citation of response.citations || []) {
      for (const image of citation.images || []) {
        if (image.asset_id) assetIds.add(image.asset_id);
      }
      if (citation.memory_id) memoryIds.add(citation.memory_id);
    }
    imageCount += assetIds.size;
  }
  return { sharedCount, newCount, imageCount, memoryCount: memoryIds.size };
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
  const disclosure = useMemo(() => summarizeShareDisclosure(turns, shared ? watermark : ""), [turns, watermark, shared]);

  async function doShare(action: "share" | "update") {
    if (busy) return;
    setBusy(action);
    setError("");
    setNotice("");
    try {
      const resp = await shareConversation(notebookId, conversationId);
      if (!aliveRef.current) return;
      setToken(resp.share_token || "");
      setWatermark(resp.shared_through_at || "");
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
                  <button
                    type="button"
                    className="sort-button"
                    disabled={busy !== ""}
                    onClick={() => void doShare("update")}
                  >
                    <RefreshCw size={13} className={busy === "update" ? "busy-spin" : undefined} />
                    {" "}{busy === "update" ? "更新中…" : "更新到最新"}
                  </button>
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
          公开页可能包含引用到的个人记忆摘录（本次未能统计条数）。
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
