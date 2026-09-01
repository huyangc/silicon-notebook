"use client";

import { Bug, ChevronLeft, ChevronRight, Heart, Lightbulb } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { toUserMessage } from "./errors.ts";
import { listWishes, toggleWishVote } from "./wish-wall-api.ts";
import { WISH_KIND_LABELS, type WishItem } from "./wish-wall-model.ts";

export const WAITING_WISH_ROTATION_MS = 8000;

type WishState =
  | { kind: "loading" }
  | { kind: "error"; notice: string }
  | { kind: "ready"; items: VotableWishItem[] };

type VotableWishItem = WishItem & { kind: "bug" | "feature" };

const WISH_ICONS = { bug: Bug, feature: Lightbulb } as const;

function compareWaitingWishes(left: WishItem, right: WishItem): number {
  if (left.vote_count !== right.vote_count) return right.vote_count - left.vote_count;
  const timeDelta = Date.parse(right.created_at) - Date.parse(left.created_at);
  if (Number.isFinite(timeDelta) && timeDelta !== 0) return timeDelta;
  return left.id < right.id ? 1 : left.id > right.id ? -1 : 0;
}

function isVotableWish(item: WishItem): item is VotableWishItem {
  return item.kind === "bug" || item.kind === "feature";
}

export function WaitingWishCarousel() {
  const [state, setState] = useState<WishState>({ kind: "loading" });
  const [index, setIndex] = useState(0);
  const [hovered, setHovered] = useState(false);
  const [focusWithin, setFocusWithin] = useState(false);
  const [votingId, setVotingId] = useState("");
  const [notice, setNotice] = useState<{ wishId: string; text: string; tone: "ok" | "error" } | null>(null);
  const loadGeneration = useRef(0);
  const noticeTimer = useRef<number | null>(null);
  const mounted = useRef(true);

  const load = useCallback(async () => {
    const generation = ++loadGeneration.current;
    setState({ kind: "loading" });
    setIndex(0);
    try {
      // 更新计划没有投票动作；分类型读取可避免计划占满综合列表的第一页，
      // 让等待卡始终只轮播用户确实可以赞同/取消赞同的条目。
      const [bugs, features] = await Promise.all([
        listWishes({ kind: "bug", sort: "priority" }),
        listWishes({ kind: "feature", sort: "priority" }),
      ]);
      if (!mounted.current || generation !== loadGeneration.current) return;
      setState({
        kind: "ready",
        items: [...bugs.items, ...features.items]
          .filter(isVotableWish)
          .sort(compareWaitingWishes),
      });
    } catch (error) {
      if (!mounted.current || generation !== loadGeneration.current) return;
      setState({
        kind: "error",
        notice: toUserMessage(error, "许愿墙暂时加载失败"),
      });
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => {
      mounted.current = false;
      ++loadGeneration.current;
      if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current);
    };
  }, [load]);

  const items = state.kind === "ready" ? state.items : [];
  const activeIndex = items.length > 0 ? index % items.length : 0;
  const item = items[activeIndex];
  const paused = hovered || focusWithin;

  useEffect(() => {
    if (items.length < 2 || paused || votingId) return;
    if (typeof window.matchMedia === "function"
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const timer = window.setInterval(() => {
      setIndex((current) => (current + 1) % items.length);
    }, WAITING_WISH_ROTATION_MS);
    return () => window.clearInterval(timer);
  }, [items.length, paused, votingId]);

  const positionLabel = useMemo(
    () => items.length > 0 ? `${activeIndex + 1} / ${items.length}` : "",
    [activeIndex, items.length],
  );

  function move(delta: number) {
    if (items.length < 2 || votingId) return;
    setIndex((current) => (current + delta + items.length) % items.length);
  }

  async function vote(current: VotableWishItem) {
    if (votingId) return;
    setVotingId(current.id);
    setNotice(null);
    if (noticeTimer.current !== null) window.clearTimeout(noticeTimer.current);
    try {
      const result = await toggleWishVote(current.id);
      if (!mounted.current) return;
      if (state.kind === "ready") {
        const nextItems = state.items.map((row) => row.id === current.id ? {
          ...row,
          vote_count: result.vote_count,
          voted_by_me: result.voted,
        } : row).sort(compareWaitingWishes);
        setState({ kind: "ready", items: nextItems });
        setIndex(Math.max(0, nextItems.findIndex((row) => row.id === current.id)));
      }
      setNotice({
        wishId: current.id,
        text: result.voted ? "已赞同" : "已取消赞同",
        tone: "ok",
      });
    } catch (error) {
      if (!mounted.current) return;
      setNotice({
        wishId: current.id,
        text: toUserMessage(error, "操作失败，请重试"),
        tone: "error",
      });
    } finally {
      if (!mounted.current) return;
      setVotingId("");
      noticeTimer.current = window.setTimeout(() => {
        setNotice((currentNotice) => currentNotice?.wishId === current.id ? null : currentNotice);
        noticeTimer.current = null;
      }, 3000);
    }
  }

  return (
    <section
      className="waiting-wish-carousel"
      aria-label="等待时浏览许愿墙"
      aria-busy={state.kind === "loading"}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocusCapture={() => setFocusWithin(true)}
      onBlurCapture={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setFocusWithin(false);
      }}
    >
      <header className="waiting-wish-head">
        <span>等待时看看大家的愿望</span>
        <a href="/wishes">打开许愿墙</a>
      </header>

      {state.kind === "loading" && <p className="waiting-wish-state">正在加载愿望…</p>}
      {state.kind === "error" && (
        <div className="waiting-wish-state error">
          <span>{state.notice}</span>
          <button type="button" onClick={() => { void load(); }}>重试</button>
        </div>
      )}
      {state.kind === "ready" && items.length === 0 && (
        <p className="waiting-wish-state">还没有可赞同的愿望，欢迎去许愿墙分享想法。</p>
      )}
      {item && (() => {
        const Icon = WISH_ICONS[item.kind];
        const pending = votingId === item.id;
        const itemNotice = notice?.wishId === item.id ? notice : null;
        return (
          <article className="waiting-wish-card">
            <div className="waiting-wish-copy">
              <span className={`waiting-wish-kind ${item.kind}`}>
                <Icon size={13} />{WISH_KIND_LABELS[item.kind]}
              </span>
              <strong title={item.title}>{item.title}</strong>
              <a href="/wishes">查看完整说明</a>
            </div>
            <div className="waiting-wish-actions">
              <button
                type="button"
                className={`waiting-wish-vote${item.voted_by_me ? " active" : ""}`}
                disabled={Boolean(votingId)}
                aria-pressed={item.voted_by_me}
                aria-label={`${item.voted_by_me ? "取消赞同" : "赞同"}“${item.title}”，当前 ${item.vote_count} 人赞同`}
                onClick={() => { void vote(item); }}
              >
                <Heart size={15} fill={item.voted_by_me ? "currentColor" : "none"} />
                <span>{pending ? "处理中…" : item.voted_by_me ? "已赞同" : "赞同"}</span>
                <strong>{item.vote_count}</strong>
              </button>
              {itemNotice && (
                <small className={itemNotice.tone} role={itemNotice.tone === "error" ? "alert" : "status"}>
                  {itemNotice.text}
                </small>
              )}
              {items.length > 1 && (
                <nav className="waiting-wish-nav" aria-label="切换愿望">
                  <button type="button" disabled={Boolean(votingId)} aria-label="上一个愿望" onClick={() => move(-1)}><ChevronLeft size={14} /></button>
                  <span>{positionLabel}</span>
                  <button type="button" disabled={Boolean(votingId)} aria-label="下一个愿望" onClick={() => move(1)}><ChevronRight size={14} /></button>
                </nav>
              )}
            </div>
          </article>
        );
      })()}
    </section>
  );
}
