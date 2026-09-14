"use client";

import { Bug, CalendarDays, Check, Heart, Lightbulb, Megaphone, Pencil, Send, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchMe, type AuthUser } from "../auth.ts";
import { PageHeader } from "../components/PageHeader.tsx";
import { toUserMessage } from "../errors.ts";
import { createWish, deleteWish, listWishes, setWishStatus, toggleWishVote, updateWish } from "../wish-wall-api.ts";
import {
  WISH_CONTENT_MAX_CHARS,
  WISH_KIND_LABELS,
  WISH_PAGE_MAX,
  WISH_STATUS_LABELS,
  WISH_STATUS_ORDER,
  WISH_TITLE_MAX_CHARS,
  type WishItem,
  type WishKind,
  type WishSort,
  type WishStatus,
} from "../wish-wall-model.ts";
import "./wish-wall.css";

const KIND_ICONS = { bug: Bug, feature: Lightbulb, plan: CalendarDays } as const;
const PYTHON_WHITESPACE_EDGES = /^[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+|[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+$/gu;
const CLOSED_STATUSES: ReadonlySet<WishStatus> = new Set(["done", "declined"]);
const CARD_NOTICE_MS = 3000;

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

/** 与后端校验同口径；返回错误文案，合法时返回空串。 */
function validateWishText(title: string, content: string): string {
  if (!title || !content) return "请填写标题和详细说明";
  if (codePointLength(title) > WISH_TITLE_MAX_CHARS) return `标题不能超过 ${WISH_TITLE_MAX_CHARS} 个字符`;
  if (codePointLength(content) > WISH_CONTENT_MAX_CHARS) return `详细说明不能超过 ${WISH_CONTENT_MAX_CHARS} 个字符`;
  return "";
}

/**
 * 服务端 `priority` 排序的本地镜像：更新计划最前，已关闭（已完成/不采纳）沉底，
 * 其余按点赞数、发布时间、id 降序。只用于把状态/类型改动后的可见窗口就地重排，
 * 不代替服务端翻页。
 */
function comparePriority(left: WishItem, right: WishItem): number {
  const planDelta = Number(left.kind !== "plan") - Number(right.kind !== "plan");
  if (planDelta !== 0) return planDelta;
  const closedDelta = Number(CLOSED_STATUSES.has(left.status)) - Number(CLOSED_STATUSES.has(right.status));
  if (closedDelta !== 0) return closedDelta;
  const leftVotes = left.kind === "plan" ? 0 : left.vote_count;
  const rightVotes = right.kind === "plan" ? 0 : right.vote_count;
  if (leftVotes !== rightVotes) return rightVotes - leftVotes;
  const timeDelta = Date.parse(right.created_at) - Date.parse(left.created_at);
  if (Number.isFinite(timeDelta) && timeDelta !== 0) return timeDelta;
  return left.id < right.id ? 1 : left.id > right.id ? -1 : 0;
}

async function loadWishWindow(kind: WishKind | "", status: WishStatus | "", sort: WishSort, count: number): Promise<{ items: WishItem[]; total: number }> {
  const items: WishItem[] = [];
  let total = count;
  while (items.length < count && items.length < total) {
    const page = await listWishes({
      kind: kind || undefined,
      status: status || undefined,
      sort,
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
  | { kind: "ready"; items: WishItem[]; total: number; nextOffset: number; pageSize: number };

type EditDraft = { id: string; kind: WishKind; title: string; content: string };

export default function WishWallPage() {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [filter, setFilter] = useState<WishKind | "">("");
  const [statusFilter, setStatusFilter] = useState<WishStatus | "">("");
  const [sort, setSort] = useState<WishSort>("priority");
  const [formKind, setFormKind] = useState<WishKind>("feature");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formNotice, setFormNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [votingId, setVotingId] = useState("");
  const [voteNotice, setVoteNotice] = useState<Record<string, string>>({});
  const [loadingMore, setLoadingMore] = useState(false);
  const [realigning, setRealigning] = useState(false);
  // 对齐失败后游标仍是旧的：不能放行翻页，「加载更多」改为重试对齐。
  const [realignFailed, setRealignFailedState] = useState(false);
  const realignFailedRef = useRef(false);
  const realignRetry = useRef<{ itemId: string; visibleCount: number } | null>(null);
  function setRealignFailed(value: boolean) {
    realignFailedRef.current = value;
    setRealignFailedState(value);
  }
  const [edit, setEdit] = useState<EditDraft | null>(null);
  const [editNotice, setEditNotice] = useState("");
  const [savingEdit, setSavingEdit] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState("");
  const [deletingId, setDeletingId] = useState("");
  const [statusPendingId, setStatusPendingId] = useState("");
  const [cardNotice, setCardNotice] = useState<Record<string, { tone: "ok" | "error"; text: string }>>({});
  const listRequestGeneration = useRef(0);
  const filterRef = useRef<WishKind | "">(filter);
  const statusFilterRef = useRef<WishStatus | "">(statusFilter);
  const sortRef = useRef<WishSort>(sort);
  const voteNoticeTimers = useRef<Record<string, number>>({});
  const cardNoticeTimers = useRef<Record<string, number>>({});
  // 当前有效代际里在途的整窗对齐（含点赞后的窗口刷新）所属的代际；-1 表示没有。
  // 按代际而不是按计数跟踪：被作废的旧请求可能迟迟不返回，不能让它拖住翻页。
  const realignGeneration = useRef(-1);
  const mounted = useRef(true);

  const stateKindRef = useRef(state.kind);
  stateKindRef.current = state.kind;
  // 异步变更落地时读「此刻」的窗口，而不是发起变更那一刻闭包里的旧窗口。
  const stateRef = useRef(state);
  stateRef.current = state;

  // 只读 ref 里的筛选/排序：changeFilter/changeSort 会先写 ref 再触发重渲染，
  // 因此变更落地后需要「重启当前查询」时（见 invalidatePendingReads）也能拿到最新条件。
  const load = useCallback(async () => {
    const requestGeneration = ++listRequestGeneration.current;
    realignGeneration.current = -1;
    setRealigning(false);
    setRealignFailed(false);
    setLoadingMore(false);
    setState({ kind: "loading" });
    try {
      const [me, page] = await Promise.all([
        fetchMe(),
        listWishes({
          kind: filterRef.current || undefined,
          status: statusFilterRef.current || undefined,
          sort: sortRef.current,
        }),
      ]);
      if (requestGeneration !== listRequestGeneration.current) return;
      setUser(me);
      setState({
        kind: "ready",
        items: page.items,
        total: page.total,
        nextOffset: page.items.length,
        pageSize: page.limit,
      });
    } catch (error) {
      if (requestGeneration !== listRequestGeneration.current) return;
      setState({ kind: "error", notice: toUserMessage(error, "许愿墙加载失败，请重试") });
    }
  }, []);

  useEffect(() => { void load(); }, [load, filter, statusFilter, sort]);
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
      Object.values(cardNoticeTimers.current).forEach((timer) => window.clearTimeout(timer));
    };
  }, []);

  const isAdmin = user?.role === "admin";
  const allowedKinds = useMemo<WishKind[]>(
    () => isAdmin ? ["feature", "bug", "plan"] : ["feature", "bug"],
    [isAdmin],
  );

  function showCardNotice(wishId: string, tone: "ok" | "error", text: string) {
    if (!mounted.current) return;
    window.clearTimeout(cardNoticeTimers.current[wishId]);
    setCardNotice((previous) => ({ ...previous, [wishId]: { tone, text } }));
    cardNoticeTimers.current[wishId] = window.setTimeout(() => {
      setCardNotice((previous) => {
        const next = { ...previous };
        delete next[wishId];
        return next;
      });
      delete cardNoticeTimers.current[wishId];
    }, CARD_NOTICE_MS);
  }

  /**
   * 变更落地后作废所有在途的列表读取（加载更多、点赞后的窗口刷新）：它们捕获的是
   * 变更前的窗口，落地时会把已删除的卡片复活、把已改的内容覆盖回旧值。
   */
  function invalidatePendingReads() {
    if (stateKindRef.current === "loading") {
      // 筛选/排序切换的首页加载正在途中：它可能在变更提交前就已发出，直接作废会让
      // 页面停在加载态，放行则可能带回旧数据——所以按当前条件重启这次加载。
      void load();
      return;
    }
    ++listRequestGeneration.current;
    realignGeneration.current = -1;
    setRealigning(false);
    setRealignFailed(false);
    setLoadingMore(false);
  }

  /**
   * 优先级排序下，状态/类型改动会让条目在服务端排序里跨页移动：只重排已加载的窗口
   * 会让 nextOffset 与服务端游标错位（未见过的条目挤进前一页后被永久跳过）。
   * 因此按当前可见条数整窗重拉，采用服务端顺序并对齐游标；被改的条目若已沉出窗口，
   * 保留在末尾但游标只按服务端条目计数，后续翻页照旧衔接。刷新失败不回滚本地
   * 重排，只在卡片旁提示。
   */
  async function reloadPriorityWindow(itemId: string, visibleCount: number) {
    const requestGeneration = listRequestGeneration.current;
    // 对齐完成前禁用「加载更多」：此时 nextOffset 还是旧游标，按它翻页会跳过挤进
    // 当前窗口的条目。只有本代际的对齐结束才解锁；被作废的旧对齐无论何时返回都
    // 不再影响它。
    realignGeneration.current = requestGeneration;
    realignRetry.current = { itemId, visibleCount };
    setRealigning(true);
    try {
      const page = await loadWishWindow(filterRef.current, statusFilterRef.current, sortRef.current, visibleCount);
      if (!mounted.current || requestGeneration !== listRequestGeneration.current) return;
      setRealignFailed(false);
      setState((previous) => {
        if (previous.kind !== "ready") return previous;
        const kept = previous.items.find((row) => row.id === itemId);
        if (!kept || page.items.some((row) => row.id === itemId)) {
          return { kind: "ready", items: page.items, total: page.total, nextOffset: page.items.length, pageSize: previous.pageSize };
        }
        const serverItems = page.items.slice(0, Math.max(0, visibleCount - 1));
        return { kind: "ready", items: [...serverItems, kept], total: page.total, nextOffset: serverItems.length, pageSize: previous.pageSize };
      });
    } catch {
      if (!mounted.current || requestGeneration !== listRequestGeneration.current) return;
      // 旧游标不能再用于翻页：把「加载更多」切成重试对齐，直到对齐成功或窗口被整体替换。
      setRealignFailed(true);
      showCardNotice(itemId, "error", "已更新，但列表排序暂未刷新；请稍后重试或刷新页面");
    } finally {
      if (mounted.current && realignGeneration.current === requestGeneration) {
        realignGeneration.current = -1;
        setRealigning(false);
      }
    }
  }

  /**
   * 把服务端返回的整条内容写回可见窗口：仍匹配当前筛选就原位替换（优先级排序下
   * 顺带就地重排），不再匹配就从窗口移除并同步 total / 翻页游标。
   * `affectsPriority` 为真且处于优先级排序时，再按服务端窗口对齐一次。
   */
  function applyServerItem(updated: WishItem, affectsPriority: boolean) {
    // 作废在途读取也会作废一次尚未完成的整窗对齐；那次对齐守护的游标契约不能随之
    // 丢失，所以只要有对齐在途，这次变更之后就重新对齐一次。
    // 在途的对齐、或上次对齐失败留下的旧游标，都要在这次变更后重新对齐。
    const realignPending = realignGeneration.current === listRequestGeneration.current || realignFailedRef.current;
    invalidatePendingReads();
    const kindFilter = filterRef.current;
    const statusFilterValue = statusFilterRef.current;
    const keep = (!kindFilter || updated.kind === kindFilter) && (!statusFilterValue || updated.status === statusFilterValue);
    // 变更在途时切换过筛选：窗口里已经没有这条，但它可能正好匹配新筛选（如改成
    // 已完成后切到「已完成」）。整窗重拉一次，让服务端结果说了算。
    const current = stateRef.current;
    const absent = current.kind === "ready" && !current.items.some((row) => row.id === updated.id);
    setState((previous) => {
      if (previous.kind !== "ready") return previous;
      const present = previous.items.some((row) => row.id === updated.id);
      if (!present) return previous;
      if (!keep) {
        return {
          ...previous,
          items: previous.items.filter((row) => row.id !== updated.id),
          total: Math.max(0, previous.total - 1),
          nextOffset: Math.max(0, previous.nextOffset - 1),
        };
      }
      const items = previous.items.map((row) => row.id === updated.id ? updated : row);
      if (sortRef.current === "priority") items.sort(comparePriority);
      return { ...previous, items };
    });
    if (current.kind !== "ready") return;
    if (absent) {
      // 卡片已不在窗口：它匹配新筛选就重拉让它出现；即使不匹配，只要作废了一次在途/失败
      // 的对齐，也要重启对齐，否则旧游标会带着别的卡片一起被跳过。
      if (keep || realignPending) void reloadPriorityWindow(updated.id, current.items.length || current.pageSize);
      return;
    }
    // 优先级排序下影响排序的改动要对齐；任何排序下，只要有窗口替换在途被作废，都要重启它。
    if (keep && ((affectsPriority && sortRef.current === "priority") || realignPending)) {
      void reloadPriorityWindow(updated.id, current.items.length);
    } else if (!keep && realignPending) {
      void reloadPriorityWindow(updated.id, Math.max(1, current.items.length - 1));
    }
  }

  function removeItem(wishId: string) {
    const realignPending = realignGeneration.current === listRequestGeneration.current || realignFailedRef.current;
    invalidatePendingReads();
    setState((previous) => {
      if (previous.kind !== "ready" || !previous.items.some((row) => row.id === wishId)) return previous;
      return {
        ...previous,
        items: previous.items.filter((row) => row.id !== wishId),
        total: Math.max(0, previous.total - 1),
        nextOffset: Math.max(0, previous.nextOffset - 1),
      };
    });
    const current = stateRef.current;
    if (realignPending && current.kind === "ready") {
      void reloadPriorityWindow(wishId, Math.max(1, current.items.length - 1));
    }
  }

  async function submit() {
    if (submitting) return;
    const submittedTitle = trimWishValue(title);
    const submittedContent = trimWishValue(content);
    const problem = validateWishText(submittedTitle, submittedContent);
    if (problem) {
      setFormNotice({ tone: "error", text: problem });
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
      // 发布后整页重拉会取代当前窗口：同 load()，把仍在途的对齐登记一并清零，
      // 否则被作废却迟迟不返回的对齐会一直拖住「加载更多」。
      // 这次重拉本身也登记为本代际的窗口替换：它在途时落地的编辑/删除会把它重启，
      // 而不是作废后让新发布的条目从列表里消失、游标停在插入之前。
      const requestGeneration = ++listRequestGeneration.current;
      realignGeneration.current = requestGeneration;
      setRealigning(true);
      setRealignFailed(false);
      setLoadingMore(false);
      try {
        const page = await listWishes({
          kind: filterRef.current || undefined,
          status: statusFilterRef.current || undefined,
          sort: sortRef.current,
        });
        if (requestGeneration === listRequestGeneration.current) {
          setState({
            kind: "ready",
            items: page.items,
            total: page.total,
            nextOffset: page.items.length,
            pageSize: page.limit,
          });
        }
      } catch {
        if (requestGeneration === listRequestGeneration.current) {
          setFormNotice({ tone: "ok", text: "已提交，但列表暂未更新；请稍后重试或刷新页面" });
          // 游标停在插入之前：翻页改为重试整窗替换，直到成功。
          if (state.kind === "ready") {
            realignRetry.current = { itemId: "", visibleCount: state.items.length };
            setRealignFailed(true);
          }
        }
      } finally {
        if (mounted.current && realignGeneration.current === requestGeneration) {
          realignGeneration.current = -1;
          setRealigning(false);
        }
      }
    } catch (error) {
      setFormNotice({ tone: "error", text: toUserMessage(error, "提交失败，请重试") });
    } finally {
      setSubmitting(false);
    }
  }

  async function vote(item: WishItem) {
    if (state.kind !== "ready" || votingId || loadingMore) return;
    // 同一卡片的编辑/改状态/删除在途时不点赞：那些响应整条覆盖卡片，会把更新的
    // 票数与本人点赞状态盖回旧值（latest 排序下没有对齐重拉来纠正）。
    if (statusPendingId === item.id || deletingId === item.id || (savingEdit && edit?.id === item.id)) return;
    const visibleCount = state.items.length;
    const visiblePageSize = state.pageSize;
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
        // 点赞后的窗口刷新同样守护游标契约：登记为本代际的对齐，这样它在途时落地的
        // 编辑/删除会把它重启，而不是作废后留下旧游标。
        // 点赞在途本身已禁用翻页（votingId），这里只登记代际、不切换 realigning 文案。
        realignGeneration.current = requestGeneration;
        try {
          const page = await loadWishWindow(filterRef.current, statusFilterRef.current, "priority", visibleCount);
          if (mounted.current && requestGeneration === listRequestGeneration.current) {
            setRealignFailed(false);
            setState((previous) => {
              if (previous.kind !== "ready") {
                return {
                  kind: "ready",
                  items: page.items,
                  total: page.total,
                  nextOffset: page.items.length,
                  pageSize: visiblePageSize,
                };
              }
              if (page.items.some((row) => row.id === item.id)) {
                return {
                  kind: "ready",
                  items: page.items,
                  total: page.total,
                  nextOffset: page.items.length,
                  pageSize: previous.pageSize,
                };
              }
              const votedItem = previous.items.find((row) => row.id === item.id);
              if (!votedItem) {
                return {
                  kind: "ready",
                  items: page.items,
                  total: page.total,
                  nextOffset: page.items.length,
                  pageSize: previous.pageSize,
                };
              }
              const serverItems = page.items.slice(0, Math.max(0, visibleCount - 1));
              return {
                kind: "ready",
                items: [...serverItems, votedItem],
                total: page.total,
                nextOffset: serverItems.length,
                pageSize: previous.pageSize,
              };
            });
          }
        } catch {
          if (mounted.current && requestGeneration === listRequestGeneration.current) {
            // 与 reloadPriorityWindow 同一套游标恢复：旧游标不再用于翻页，改为重试对齐。
            realignRetry.current = { itemId: item.id, visibleCount };
            setRealignFailed(true);
            setState((previous) => previous.kind === "loading" ? {
              kind: "error",
              notice: `${result.voted ? "已点赞" : "已取消"}，但许愿墙刷新失败，请重试`,
            } : previous);
            setVoteNotice((previous) => ({
              ...previous,
              [item.id]: `${result.voted ? "已点赞" : "已取消"}，但排序暂未刷新；请稍后重试或刷新页面`,
            }));
          }
        } finally {
          if (mounted.current && realignGeneration.current === requestGeneration) {
            realignGeneration.current = -1;
            setRealigning(false);
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

  function beginEdit(item: WishItem) {
    if (savingEdit) return;
    setConfirmDeleteId("");
    setEditNotice("");
    setEdit({ id: item.id, kind: item.kind, title: item.title, content: item.content });
  }

  function cancelEdit() {
    if (savingEdit) return;
    setEdit(null);
    setEditNotice("");
  }

  async function saveEdit(original: WishItem) {
    // 同一卡片的编辑与改状态串行：两条响应都会整条覆盖卡片，并发时后到的旧响应
    // 会盖掉先落地的新内容（latest 排序下没有对齐重拉来纠正）。
    if (!edit || edit.id !== original.id || savingEdit || statusPendingId === original.id || votingId === original.id) return;
    const nextTitle = trimWishValue(edit.title);
    const nextContent = trimWishValue(edit.content);
    const problem = validateWishText(nextTitle, nextContent);
    if (problem) {
      setEditNotice(problem);
      return;
    }
    const changes: { kind?: WishKind; title?: string; content?: string } = {};
    if (edit.kind !== original.kind) changes.kind = edit.kind;
    if (nextTitle !== original.title) changes.title = nextTitle;
    if (nextContent !== original.content) changes.content = nextContent;
    if (Object.keys(changes).length === 0) {
      setEdit(null);
      setEditNotice("");
      showCardNotice(original.id, "ok", "内容没有变化");
      return;
    }
    setSavingEdit(true);
    setEditNotice("");
    try {
      const updated = await updateWish(original.id, changes);
      if (!mounted.current) return;
      setEdit(null);
      applyServerItem(updated, changes.kind !== undefined);
      showCardNotice(original.id, "ok", "已保存修改");
    } catch (error) {
      if (!mounted.current) return;
      setEditNotice(toUserMessage(error, "保存失败，请重试"));
    } finally {
      if (mounted.current) setSavingEdit(false);
    }
  }

  async function confirmDelete(item: WishItem) {
    if (deletingId) return;
    setDeletingId(item.id);
    try {
      await deleteWish(item.id);
      if (!mounted.current) return;
      setConfirmDeleteId("");
      if (edit?.id === item.id) setEdit(null);
      window.clearTimeout(cardNoticeTimers.current[item.id]);
      delete cardNoticeTimers.current[item.id];
      removeItem(item.id);
    } catch (error) {
      if (!mounted.current) return;
      setConfirmDeleteId("");
      showCardNotice(item.id, "error", toUserMessage(error, "删除失败，请重试"));
    } finally {
      if (mounted.current) setDeletingId("");
    }
  }

  async function changeStatus(item: WishItem, next: WishStatus) {
    if (statusPendingId || next === item.status) return;
    if (votingId === item.id || (savingEdit && edit?.id === item.id)) return;
    setStatusPendingId(item.id);
    try {
      const updated = await setWishStatus(item.id, next);
      if (!mounted.current) return;
      applyServerItem(updated, true);
      showCardNotice(item.id, "ok", `已标记为${WISH_STATUS_LABELS[updated.status]}`);
    } catch (error) {
      if (!mounted.current) return;
      showCardNotice(item.id, "error", toUserMessage(error, "标记失败，请重试"));
    } finally {
      if (mounted.current) setStatusPendingId("");
    }
  }

  async function loadMore() {
    if (state.kind !== "ready" || loadingMore || votingId || realigning) return;
    if (realignFailed) {
      // 旧游标不可用：先重试整窗对齐，成功后按钮恢复为正常翻页。
      const retry = realignRetry.current;
      if (retry) void reloadPriorityWindow(retry.itemId, Math.max(1, state.items.length));
      return;
    }
    const requestGeneration = ++listRequestGeneration.current;
    const existingItems = state.items;
    const nextOffset = state.nextOffset;
    setLoadingMore(true);
    try {
      const page = await listWishes({
        kind: filter || undefined,
        status: statusFilter || undefined,
        sort,
        offset: nextOffset,
        limit: state.pageSize,
      });
      if (requestGeneration !== listRequestGeneration.current) return;
      const existingIds = new Set(existingItems.map((item) => item.id));
      setState({
        kind: "ready",
        items: [...existingItems, ...page.items.filter((item) => !existingIds.has(item.id))],
        total: page.total,
        nextOffset: nextOffset + page.items.length,
        pageSize: state.pageSize,
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

  function changeStatusFilter(next: WishStatus | "") {
    if (next === statusFilterRef.current) return;
    statusFilterRef.current = next;
    ++listRequestGeneration.current;
    setLoadingMore(false);
    setStatusFilter(next);
  }

  function changeSort(next: WishSort) {
    if (next === sortRef.current) return;
    sortRef.current = next;
    ++listRequestGeneration.current;
    setLoadingMore(false);
    setSort(next);
  }

  function renderEditor(item: WishItem) {
    if (!edit || edit.id !== item.id) return null;
    const editTitle = trimWishValue(edit.title);
    const editContent = trimWishValue(edit.content);
    const editableKinds = allowedKinds.includes(item.kind) ? allowedKinds : [item.kind, ...allowedKinds];
    return (
      <div className="wish-edit" aria-label="编辑内容">
        <div className="wish-kind-picker" role="group" aria-label="修改内容类型">
          {editableKinds.map((kind) => {
            const Icon = KIND_ICONS[kind];
            return (
              <button key={kind} type="button" className={edit.kind === kind ? "active" : ""} disabled={savingEdit} onClick={() => setEdit({ ...edit, kind })}>
                <Icon size={15} />{WISH_KIND_LABELS[kind]}
              </button>
            );
          })}
        </div>
        <label className="wish-field">
          <span>标题</span>
          <input aria-label="修改标题" value={edit.title} aria-invalid={codePointLength(editTitle) > WISH_TITLE_MAX_CHARS} disabled={savingEdit} onChange={(event) => setEdit({ ...edit, title: event.target.value })} />
          <small>{codePointLength(editTitle)}/{WISH_TITLE_MAX_CHARS}</small>
        </label>
        <label className="wish-field">
          <span>详细说明</span>
          <textarea aria-label="修改详细说明" value={edit.content} aria-invalid={codePointLength(editContent) > WISH_CONTENT_MAX_CHARS} disabled={savingEdit} rows={5} onChange={(event) => setEdit({ ...edit, content: event.target.value })} />
          <small>{codePointLength(editContent)}/{WISH_CONTENT_MAX_CHARS}</small>
        </label>
        <div className="wish-submit-row">
          <button type="button" className="wish-submit" disabled={savingEdit || statusPendingId === item.id || votingId === item.id} onClick={() => { void saveEdit(item); }}>
            <Check size={16} />{savingEdit ? "保存中…" : "保存修改"}
          </button>
          <button type="button" className="wish-ghost" disabled={savingEdit} onClick={cancelEdit}>
            <X size={15} />取消
          </button>
          {editNotice && <span className="wish-inline-notice error" role="alert">{editNotice}</span>}
        </div>
      </div>
    );
  }

  function renderActions(item: WishItem) {
    const canManage = Boolean(user) && (isAdmin || user?.id === item.author_id);
    if (!canManage) return null;
    const editing = edit?.id === item.id;
    const deleting = deletingId === item.id;
    const confirming = confirmDeleteId === item.id;
    const statusPending = statusPendingId === item.id;
    const notice = cardNotice[item.id];
    return (
      <div className="wish-card-actions">
        {isAdmin && (
          <label className="wish-status-control">
            <span>处理状态</span>
            <select aria-label="处理状态" value={item.status} disabled={Boolean(statusPendingId) || deleting || votingId === item.id || (editing && savingEdit)} onChange={(event) => { void changeStatus(item, event.target.value as WishStatus); }}>
              {WISH_STATUS_ORDER.map((status) => (
                <option key={status} value={status}>{WISH_STATUS_LABELS[status]}</option>
              ))}
            </select>
            {statusPending && <small>标记中…</small>}
          </label>
        )}
        {!editing && (
          <button type="button" className="wish-ghost" disabled={savingEdit || deleting} onClick={() => beginEdit(item)}>
            <Pencil size={14} />编辑
          </button>
        )}
        {confirming ? (
          <span className="wish-delete-confirm" role="group" aria-label="确认删除">
            <span>确定删除这条内容？</span>
            <button type="button" className="wish-ghost danger" disabled={Boolean(deletingId)} onClick={() => { void confirmDelete(item); }}>
              <Trash2 size={14} />{deleting ? "删除中…" : "确认删除"}
            </button>
            <button type="button" className="wish-ghost" disabled={Boolean(deletingId)} onClick={() => setConfirmDeleteId("")}>取消</button>
          </span>
        ) : (
          <button type="button" className="wish-ghost danger" disabled={Boolean(deletingId) || savingEdit} onClick={() => { setConfirmDeleteId(item.id); }}>
            <Trash2 size={14} />删除
          </button>
        )}
        {notice && <small className={`wish-card-notice ${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>{notice.text}</small>}
      </div>
    );
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
            <div className="wish-toolbar-selects">
              <label className="wish-sort-label">状态
                <select aria-label="按处理状态筛选" value={statusFilter} onChange={(event) => changeStatusFilter(event.target.value as WishStatus | "")}>
                  <option value="">全部状态</option>
                  {WISH_STATUS_ORDER.map((status) => (
                    <option key={status} value={status}>{WISH_STATUS_LABELS[status]}</option>
                  ))}
                </select>
              </label>
              <label className="wish-sort-label">排序
                <select value={sort} onChange={(event) => changeSort(event.target.value as WishSort)}>
                  <option value="priority">优先级</option>
                  <option value="latest">最新发布</option>
                </select>
              </label>
            </div>
          </div>

          {state.kind === "loading" && <div className="wish-state">正在加载许愿墙…</div>}
          {state.kind === "error" && <div className="wish-state error"><span>{state.notice}</span><button type="button" onClick={() => { void load(); }}>重试</button></div>}
          {state.kind === "ready" && state.items.length === 0 && <div className="wish-state"><strong>这里还没有内容</strong><span>{filter || statusFilter ? "换个筛选条件试试。" : "成为第一个分享想法的人吧。"}</span></div>}
          {state.kind === "ready" && (
            <div className="wish-list">
              {state.items.map((item) => {
                const Icon = KIND_ICONS[item.kind];
                const pending = votingId === item.id;
                const editing = edit?.id === item.id;
                return (
                  <article key={item.id} className={`wish-card wish-card-${item.kind}${CLOSED_STATUSES.has(item.status) ? " wish-card-closed" : ""}`}>
                    <div className="wish-card-main">
                      <div className="wish-card-meta">
                        <span className={`wish-kind wish-kind-${item.kind}`}><Icon size={14} />{WISH_KIND_LABELS[item.kind]}</span>
                        {item.status !== "open" && <span className={`wish-status wish-status-${item.status}`}>{WISH_STATUS_LABELS[item.status]}</span>}
                        <span>{item.author_name}</span>
                        <span>{formattedTime(item.created_at)}</span>
                      </div>
                      {editing ? renderEditor(item) : (
                        <>
                          <h2>{item.title}</h2>
                          <p>{item.content}</p>
                        </>
                      )}
                      {renderActions(item)}
                    </div>
                    {item.kind !== "plan" && (
                      <div className="wish-vote-area">
                        <button type="button" className={`wish-vote${item.voted_by_me ? " active" : ""}`} disabled={Boolean(votingId) || loadingMore || statusPendingId === item.id || deletingId === item.id || (savingEdit && editing)} aria-pressed={item.voted_by_me} onClick={() => { void vote(item); }}>
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
                <button type="button" className="wish-load-more" disabled={loadingMore || realigning || Boolean(votingId)} onClick={() => { void loadMore(); }}>
                  {loadingMore ? "加载中…" : realigning ? "正在刷新排序…" : realignFailed ? "排序刷新失败，点击重试后再加载" : `加载更多（还有 ${state.total - state.items.length} 条）`}
                </button>
              )}
              {state.items.length > state.pageSize && <span className="wish-result-count">已显示 {state.items.length} 条</span>}
            </div>
          )}
        </section>
      </main>
    </>
  );
}
