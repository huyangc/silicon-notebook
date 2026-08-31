"use client";

import { Bug, CalendarDays, Heart, Lightbulb, Megaphone, Send } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchMe, type AuthUser } from "../auth.ts";
import { PageHeader } from "../components/PageHeader.tsx";
import { toUserMessage } from "../errors.ts";
import { createWish, listWishes, toggleWishVote } from "../wish-wall-api.ts";
import {
  WISH_CONTENT_MAX_CHARS,
  WISH_KIND_LABELS,
  WISH_PAGE_MAX,
  WISH_PAGE_SIZE,
  WISH_TITLE_MAX_CHARS,
  type WishItem,
  type WishKind,
  type WishSort,
} from "../wish-wall-model.ts";
import "./wish-wall.css";

const KIND_ICONS = { bug: Bug, feature: Lightbulb, plan: CalendarDays } as const;
const PYTHON_WHITESPACE_EDGES = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu;

function formattedTime(value: string): string {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function trimWishValue(value: string): string {
  return value.replace(PYTHON_WHITESPACE_EDGES, "");
}

async function loadWishWindow(kind: WishKind | "", count: number): Promise<{ items: WishItem[]; total: number }> {
  const items: WishItem[] = [];
  let total = count;
  while (items.length < count && items.length < total) {
    const page = await listWishes({
      kind: kind || undefined,
      sort: "priority",
      offset: items.length,
      limit: Math.min(WISH_PAGE_MAX, count - items.length),
    });
    total = page.total;
    items.push(...page.items);
    if (page.items.length === 0) break;
  }
  return { items, total };
}

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; notice: string }
  | { kind: "ready"; items: WishItem[]; total: number; nextOffset: number };

export default function WishWallPage() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [filter, setFilter] = useState<WishKind | "">("");
  const [sort, setSort] = useState<WishSort>("priority");
  const [formKind, setFormKind] = useState<WishKind>("feature");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formNotice, setFormNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [votingId, setVotingId] = useState("");
  const [voteNotice, setVoteNotice] = useState<Record<string, string>>({});
  const [loadingMore, setLoadingMore] = useState(false);
  const listRequestGeneration = useRef(0);
  const filterRef = useRef<WishKind | "">(filter);
  const sortRef = useRef<WishSort>(sort);
  const voteNoticeTimers = useRef<Record<string, number>>({});
  const mounted = useRef(true);

  const load = useCallback(async () => {
    const requestGeneration = ++listRequestGeneration.current;
    setLoadingMore(false);
    setState({ kind: "loading" });
    try {
      const [me, page] = await Promise.all([
        fetchMe(),
        listWishes({ kind: filter || undefined, sort }),
      ]);
      if (requestGeneration !== listRequestGeneration.current) return;
      setUser(me);
      setState({ kind: "ready", items: page.items, total: page.total, nextOffset: page.items.length });
    } catch (error) {
      if (requestGeneration !== listRequestGeneration.current) return;
      setState({ kind: "error", notice: toUserMessage(error, "许愿墙加载失败，请重试") });
    }
  }, [filter, sort]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!formNotice) return;
    const timer = window.setTimeout(() => setFormNotice(null), 6000);
    return () => window.clearTimeout(timer);
  }, [formNotice]);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      Object.values(voteNoticeTimers.current).forEach((timer) => window.clearTimeout(timer));
    };
  }, []);

  const allowedKinds = useMemo<WishKind[]>(
    () => user?.role === "admin" ? ["feature", "bug", "plan"] : ["feature", "bug"],
    [user?.role],
  );

  async function submit() {
    if (submitting) return;
    const submittedTitle = trimWishValue(title);
    const submittedContent = trimWishValue(content);
    if (!submittedTitle || !submittedContent) {
      setFormNotice({ tone: "error", text: "请填写标题和详细说明" });
      return;
    }
    if (codePointLength(submittedTitle) > WISH_TITLE_MAX_CHARS) {
      setFormNotice({ tone: "error", text: `标题不能超过 ${WISH_TITLE_MAX_CHARS} 个字符` });
      return;
    }
    if (codePointLength(submittedContent) > WISH_CONTENT_MAX_CHARS) {
      setFormNotice({ tone: "error", text: `详细说明不能超过 ${WISH_CONTENT_MAX_CHARS} 个字符` });
      return;
    }
    setSubmitting(true);
    setFormNotice(null);
    try {
      const submittedKind = formKind;
      await createWish({ kind: submittedKind, title: submittedTitle, content: submittedContent });
      setTitle("");
      setContent("");
      setFormNotice({ tone: "ok", text: submittedKind === "plan" ? "更新计划已发布" : "已提交，感谢你的反馈" });
      const requestGeneration = ++listRequestGeneration.current;
      try {
        const currentFilter = filterRef.current;
        const page = await listWishes({ kind: currentFilter || undefined, sort: sortRef.current });
        if (requestGeneration === listRequestGeneration.current) {
          setState({ kind: "ready", items: page.items, total: page.total, nextOffset: page.items.length });
        }
      } catch {
        if (requestGeneration === listRequestGeneration.current) {
          setFormNotice({ tone: "ok", text: "已提交，但列表暂未更新；请稍后重试或刷新页面" });
        }
      }
    } catch (error) {
      setFormNotice({ tone: "error", text: toUserMessage(error, "提交失败，请重试") });
    } finally {
      setSubmitting(false);
    }
  }

  async function vote(item: WishItem) {
    if (votingId || loadingMore) return;
    const visibleCount = state.kind === "ready" ? state.items.length : WISH_PAGE_SIZE;
    setVotingId(item.id);
    window.clearTimeout(voteNoticeTimers.current[item.id]);
    delete voteNoticeTimers.current[item.id];
    setVoteNotice((previous) => ({ ...previous, [item.id]: "" }));
    try {
      const result = await toggleWishVote(item.id);
      if (!mounted.current) return;
      setState((previous) => previous.kind === "ready" ? {
        ...previous,
        items: previous.items.map((row) => row.id === item.id ? {
          ...row, vote_count: result.vote_count, voted_by_me: result.voted,
        } : row),
      } : previous);
      setVoteNotice((previous) => ({ ...previous, [item.id]: result.voted ? "已点赞" : "已取消" }));
      if (sortRef.current === "priority") {
        setLoadingMore(false);
        const requestGeneration = ++listRequestGeneration.current;
        const currentFilter = filterRef.current;
        try {
          const page = await loadWishWindow(currentFilter, visibleCount);
          if (mounted.current && requestGeneration === listRequestGeneration.current) {
            setState((previous) => {
              if (previous.kind !== "ready") {
                return { kind: "ready", items: page.items, total: page.total, nextOffset: page.items.length };
              }
              if (page.items.some((row) => row.id === item.id)) {
                return { kind: "ready", items: page.items, total: page.total, nextOffset: page.items.length };
              }
              const votedItem = previous.items.find((row) => row.id === item.id);
              if (!votedItem) {
                return { kind: "ready", items: page.items, total: page.total, nextOffset: page.items.length };
              }
              const serverItems = page.items.slice(0, Math.max(0, visibleCount - 1));
              return {
                kind: "ready",
                items: [...serverItems, votedItem],
                total: page.total,
                nextOffset: serverItems.length,
              };
            });
          }
        } catch {
          if (mounted.current && requestGeneration === listRequestGeneration.current) {
            setState((previous) => previous.kind === "loading" ? {
              kind: "error",
              notice: `${result.voted ? "已点赞" : "已取消"}，但许愿墙刷新失败，请重试`,
            } : previous);
            setVoteNotice((previous) => ({
              ...previous,
              [item.id]: `${result.voted ? "已点赞" : "已取消"}，但排序暂未刷新；请稍后重试或刷新页面`,
            }));
          }
        }
      }
    } catch (error) {
      if (!mounted.current) return;
      setVoteNotice((previous) => ({
        ...previous,
        [item.id]: toUserMessage(error, "操作失败，请重试"),
      }));
    } finally {
      if (!mounted.current) return;
      voteNoticeTimers.current[item.id] = window.setTimeout(() => {
        setVoteNotice((previous) => ({ ...previous, [item.id]: "" }));
        delete voteNoticeTimers.current[item.id];
      }, 3000);
      setVotingId("");
    }
  }

  async function loadMore() {
    if (state.kind !== "ready" || loadingMore || votingId) return;
    const requestGeneration = ++listRequestGeneration.current;
    const existingItems = state.items;
    const nextOffset = state.nextOffset;
    setLoadingMore(true);
    try {
      const page = await listWishes({ kind: filter || undefined, sort, offset: nextOffset });
      if (requestGeneration !== listRequestGeneration.current) return;
      const existingIds = new Set(existingItems.map((item) => item.id));
      setState({
        kind: "ready",
        items: [...existingItems, ...page.items.filter((item) => !existingIds.has(item.id))],
        total: page.total,
        nextOffset: nextOffset + page.items.length,
      });
    } catch (error) {
      if (requestGeneration !== listRequestGeneration.current) return;
      setFormNotice({ tone: "error", text: toUserMessage(error, "加载更多失败，请重试") });
    } finally {
      if (requestGeneration === listRequestGeneration.current) setLoadingMore(false);
    }
  }

  function changeFilter(next: WishKind | "") {
    if (next === filterRef.current) return;
    filterRef.current = next;
    ++listRequestGeneration.current;
    setLoadingMore(false);
    setFilter(next);
  }

  function changeSort(next: WishSort) {
    if (next === sortRef.current) return;
    sortRef.current = next;
    ++listRequestGeneration.current;
    setLoadingMore(false);
    setSort(next);
  }

  return (
    <>
      <PageHeader title="许愿墙" />
      <main className="wish-page">
        <section className="wish-hero">
          <div className="wish-hero-icon"><Megaphone size={24} /></div>
          <div>
            <h1>许愿墙</h1>
            <p>告诉我们你遇到的问题和期待的功能。点赞会帮助更受关注的需求排到前面。</p>
          </div>
        </section>

        <section className="wish-compose-card" aria-labelledby="wish-compose-title">
          <div className="wish-section-heading">
            <div><span>分享你的想法</span><small>描述得越具体，越容易被理解和安排。</small></div>
          </div>
          <div className="wish-kind-picker" role="group" aria-label="内容类型">
            {allowedKinds.map((kind) => {
              const Icon = KIND_ICONS[kind];
              return (
                <button key={kind} type="button" className={formKind === kind ? "active" : ""} onClick={() => setFormKind(kind)}>
                  <Icon size={15} />{WISH_KIND_LABELS[kind]}
                </button>
              );
            })}
          </div>
          <label className="wish-field">
            <span>标题</span>
            <input aria-label="标题" value={title} aria-invalid={codePointLength(trimWishValue(title)) > WISH_TITLE_MAX_CHARS} disabled={submitting} placeholder="一句话说明你的想法" onChange={(event) => setTitle(event.target.value)} />
            <small>{codePointLength(trimWishValue(title))}/{WISH_TITLE_MAX_CHARS}</small>
          </label>
          <label className="wish-field">
            <span>详细说明</span>
            <textarea aria-label="详细说明" value={content} aria-invalid={codePointLength(trimWishValue(content)) > WISH_CONTENT_MAX_CHARS} disabled={submitting} rows={5} placeholder="可以写下复现步骤、使用场景或你期待的结果" onChange={(event) => setContent(event.target.value)} />
            <small>{codePointLength(trimWishValue(content))}/{WISH_CONTENT_MAX_CHARS}</small>
          </label>
          <div className="wish-submit-row">
            <button type="button" className="wish-submit" disabled={submitting} onClick={() => { void submit(); }}>
              <Send size={16} />{submitting ? "提交中…" : formKind === "plan" ? "发布计划" : "提交反馈"}
            </button>
            {formNotice && <span className={`wish-inline-notice ${formNotice.tone}`} role={formNotice.tone === "error" ? "alert" : "status"}>{formNotice.text}</span>}
          </div>
        </section>

        <section className="wish-list-section">
          <div className="wish-list-toolbar">
            <div className="wish-filter-tabs" role="group" aria-label="筛选许愿墙内容">
              {(["", "bug", "feature", "plan"] as const).map((kind) => (
                <button key={kind || "all"} type="button" className={filter === kind ? "active" : ""} onClick={() => changeFilter(kind)}>
                  {kind ? WISH_KIND_LABELS[kind] : "全部"}
                </button>
              ))}
            </div>
            <label className="wish-sort-label">排序
              <select value={sort} onChange={(event) => changeSort(event.target.value as WishSort)}>
                <option value="priority">优先级</option>
                <option value="latest">最新发布</option>
              </select>
            </label>
          </div>

          {state.kind === "loading" && <div className="wish-state">正在加载许愿墙…</div>}
          {state.kind === "error" && <div className="wish-state error"><span>{state.notice}</span><button type="button" onClick={() => { void load(); }}>重试</button></div>}
          {state.kind === "ready" && state.items.length === 0 && <div className="wish-state"><strong>这里还没有内容</strong><span>成为第一个分享想法的人吧。</span></div>}
          {state.kind === "ready" && (
            <div className="wish-list">
              {state.items.map((item) => {
                const Icon = KIND_ICONS[item.kind];
                const pending = votingId === item.id;
                return (
                  <article key={item.id} className={`wish-card wish-card-${item.kind}`}>
                    <div className="wish-card-main">
                      <div className="wish-card-meta">
                        <span className={`wish-kind wish-kind-${item.kind}`}><Icon size={14} />{WISH_KIND_LABELS[item.kind]}</span>
                        <span>{item.author_name}</span>
                        <span>{formattedTime(item.created_at)}</span>
                      </div>
                      <h2>{item.title}</h2>
                      <p>{item.content}</p>
                    </div>
                    {item.kind !== "plan" && (
                      <div className="wish-vote-area">
                        <button type="button" className={`wish-vote${item.voted_by_me ? " active" : ""}`} disabled={Boolean(votingId) || loadingMore} aria-pressed={item.voted_by_me} onClick={() => { void vote(item); }}>
                          <Heart size={17} fill={item.voted_by_me ? "currentColor" : "none"} />
                          <span>{pending ? "处理中…" : item.voted_by_me ? "已点赞" : "点赞"}</span>
                          <strong>{item.vote_count}</strong>
                        </button>
                        {voteNotice[item.id] && <small role="status">{voteNotice[item.id]}</small>}
                      </div>
                    )}
                  </article>
                );
              })}
              {state.items.length < state.total && (
                <button type="button" className="wish-load-more" disabled={loadingMore || Boolean(votingId)} onClick={() => { void loadMore(); }}>
                  {loadingMore ? "加载中…" : `加载更多（还有 ${state.total - state.items.length} 条）`}
                </button>
              )}
              {state.items.length > WISH_PAGE_SIZE && <span className="wish-result-count">已显示 {state.items.length} 条</span>}
            </div>
          )}
        </section>
      </main>
    </>
  );
}
