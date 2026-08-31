"use client";
// 「活动」视图的编排层：取数、请求竞态、三栏之间的选中态。

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchUserNotebooks, type AdminUserNotebook } from "../../../admin/usage/notebooks.ts";
import { toUserMessage } from "../../../errors.ts";
import {
  FORBIDDEN_SENTINEL,
  fetchUserActivity,
  fetchUserAskDetail,
  fetchUserNotebookSource,
  fetchUserNotebookSources,
} from "./api.ts";
import { mergeActivityPages } from "./format.ts";
import { ActivityDetail } from "./ActivityDetail.tsx";
import { ActivityScopePanel, ALL_NOTEBOOKS, type SourceListState } from "./ActivityScopePanel.tsx";
import { ActivityStream, activityKey } from "./ActivityStream.tsx";
import type {
  ActivityCursor,
  ActivityItem,
  ActivitySource,
  ActivityTypeFilter,
  AskDetail,
} from "./types";

const PAGE = 50;
const SOURCE_PAGE = 50;

function initialActivityType(): ActivityTypeFilter {
  if (typeof window === "undefined") return "";
  const value = new URLSearchParams(window.location.search).get("activity_type");
  return value === "ask" || value === "source" || value === "report" ? value : "";
}

function initialSourceTarget(): {
  ownerId: string;
  notebookId: string;
  sourceId: string;
} {
  if (typeof window === "undefined") {
    return { ownerId: "", notebookId: "", sourceId: "" };
  }
  const params = new URLSearchParams(window.location.search);
  return {
    ownerId: params.get("owner") || "",
    notebookId: params.get("notebook_id") || "",
    sourceId: params.get("source_id") || "",
  };
}

/**
 * 403 是「换个人看」而不是「再试一次」。
 *
 * activity/api.ts 的 FORBIDDEN_SENTINEL 是**不带文案**的控制流标识（诊断已经先落
 * console 了），toUserMessage 认不出它，只会兜成「…请重试」——于是普通用户打开
 * `/dev/logs?owner=<别人的id>&view=activity` 会一直重试一个永远不会成功的请求。
 * 这里按标识分流到一句固定文案，措辞里不带任何重试暗示（镜像
 * admin/usage/page.tsx 的同款分流）。
 */
const FORBIDDEN_COPY = "没有权限查看这位用户的活动记录。";

function isForbidden(cause: unknown): boolean {
  return cause instanceof Error && cause.message === FORBIDDEN_SENTINEL;
}

export function ActivityView({
  userId,
  userPending,
  userError,
  scopeKey,
  since,
  until,
  now,
  fixedActivityType,
}: {
  /** 已解析成具体 id 的被查看用户（顶部范围条选「我自己」时是当前用户自己的 id）。 */
  userId: string;
  /** 身份还没确定（当前用户尚未回来）。⚠ 与「确定了但这位用户没有活动」是两回事：
   *  混为一谈会让页面在挂载瞬间先说一句「这个范围里没有活动记录」，一个 RTT 之后
   *  才改口说「加载中」。 */
  userPending?: boolean;
  /** 身份**取不到**（当前用户接口失败）。已经过人话层，直接上屏。 */
  userError?: string;
  /** 页面级范围键：[视图 tab, owner, 日期]。本组件再叠上 notebook_id。 */
  scopeKey: string;
  since?: string;
  until?: string;
  /** 只为测试注入确定的“现在”；生产不传，时间格式化件各自用 new Date()。 */
  now?: Date;
  /** 嵌入专用分析页时固定类型，并隐藏会把用户带离该页用途的类型切换。 */
  fixedActivityType?: Exclude<ActivityTypeFilter, "">;
}) {
  const [notebooks, setNotebooks] = useState<AdminUserNotebook[]>([]);
  const [notebooksLoading, setNotebooksLoading] = useState(false);
  // 笔记本清单有**自己**的失败态，不与活动流的 error 共用一个 slot：共用时 500 会
  // 让顶部说「加载失败」、左栏同时说「这位用户还没有建过笔记本」（互相矛盾），
  // 随手改个日期触发一次 reload 又会把 error 清掉、只剩那句假陈述。
  const [notebooksFailure, setNotebooksFailure] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const [notebookId, setNotebookId] = useState(ALL_NOTEBOOKS);
  const [activityType, setActivityType] = useState<ActivityTypeFilter>(
    fixedActivityType ?? initialActivityType,
  );
  const sourceTarget = useMemo(initialSourceTarget, []);
  const sourceTargetKeyRef = useRef("");
  const [expanded, setExpanded] = useState<string[]>([]);
  const [sources, setSources] = useState<Record<string, SourceListState>>({});

  const [items, setItems] = useState<ActivityItem[]>([]);
  const [cursor, setCursor] = useState<ActivityCursor | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const [selected, setSelected] = useState<ActivityItem | null>(null);
  const [askDetail, setAskDetail] = useState<AskDetail | null>(null);
  const [askDetailLoading, setAskDetailLoading] = useState(false);
  const [askDetailError, setAskDetailError] = useState("");

  // 请求范围键。**notebook_id 与视图 tab 都在里面**（后者随页面传进来的 scopeKey
  // 带上）：mergeActivityPages 是纯函数，看不见 scope，也就不会替我们拦住迟到的
  // 响应——管理员对笔记本 A 点了「加载更多」、响应未回时切到 B，那一页必须在这里
  // 被丢掉，否则会被静默拼进 B 的列表。
  const streamScopeKey = useMemo(
    () => JSON.stringify([scopeKey, userId, notebookId, activityType]),
    [scopeKey, userId, notebookId, activityType],
  );
  const streamScopeRef = useRef(streamScopeKey);
  const userIdRef = useRef(userId);
  const expandedRef = useRef(expanded);
  const sourcesRef = useRef(sources);
  streamScopeRef.current = streamScopeKey;
  userIdRef.current = userId;
  expandedRef.current = expanded;
  sourcesRef.current = sources;

  const streamGenerationRef = useRef(0);
  const detailGenerationRef = useRef(0);
  const notebooksGenerationRef = useRef(0);
  const sourcesGenerationRef = useRef<Record<string, number>>({});

  const notebookNames = useMemo(
    () => Object.fromEntries(notebooks.map((notebook) => [notebook.id, notebook.name])),
    [notebooks],
  );

  // 换用户 = 换一整棵范围树：笔记本选择、展开态与已取回的来源清单全部作废。
  // 无权限态同样按用户重置——换一个人看不该继承上一个人的 403。
  useEffect(() => {
    setNotebookId(ALL_NOTEBOOKS);
    setExpanded([]);
    setSources({});
    setForbidden(false);
    sourcesGenerationRef.current = {};
  }, [userId]);

  useEffect(() => {
    if (fixedActivityType) setActivityType(fixedActivityType);
  }, [fixedActivityType]);

  const loadNotebooks = useCallback(() => {
    if (!userId) {
      setNotebooks([]);
      setNotebooksFailure("");
      return;
    }
    const generation = ++notebooksGenerationRef.current;
    setNotebooksLoading(true);
    setNotebooksFailure("");
    setNotebooks([]);
    fetchUserNotebooks(userId)
      .then((rows) => {
        if (generation !== notebooksGenerationRef.current) return;
        setNotebooks(rows);
      })
      .catch((cause) => {
        if (generation !== notebooksGenerationRef.current) return;
        if (isForbidden(cause)) {
          setForbidden(true);
          return;
        }
        setNotebooksFailure(toUserMessage(cause, "笔记本清单加载失败，请重试"));
      })
      .finally(() => {
        if (generation !== notebooksGenerationRef.current) return;
        setNotebooksLoading(false);
      });
  }, [userId]);

  useEffect(() => {
    loadNotebooks();
  }, [loadNotebooks]);

  const reload = useCallback(async () => {
    const generation = ++streamGenerationRef.current;
    const requestedScopeKey = streamScopeRef.current;
    detailGenerationRef.current += 1;
    setItems([]);
    setCursor(null);
    setHasMore(false);
    setSelected(null);
    setAskDetail(null);
    setAskDetailError("");
    setAskDetailLoading(false);
    setError("");
    if (!userId) {
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const page = await fetchUserActivity(userId, {
        ...(activityType ? { activityType } : {}),
        notebookId: notebookId || undefined,
        since,
        until,
        limit: PAGE,
      });
      if (
        generation !== streamGenerationRef.current
        || requestedScopeKey !== streamScopeRef.current
      ) return;
      setItems(page.items);
      setHasMore(page.has_more);
      setCursor(page.next_cursor);
    } catch (cause) {
      if (
        generation !== streamGenerationRef.current
        || requestedScopeKey !== streamScopeRef.current
      ) return;
      if (isForbidden(cause)) setForbidden(true);
      else setError(toUserMessage(cause, "活动记录加载失败，请重试"));
    } finally {
      if (
        generation === streamGenerationRef.current
        && requestedScopeKey === streamScopeRef.current
      ) setLoading(false);
    }
  }, [userId, notebookId, activityType, since, until]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const loadMore = useCallback(async () => {
    if (loading || !hasMore || !cursor || !userId) return;
    const generation = ++streamGenerationRef.current;
    const requestedScopeKey = streamScopeRef.current;
    setLoading(true);
    try {
      const page = await fetchUserActivity(userId, {
        ...(activityType ? { activityType } : {}),
        notebookId: notebookId || undefined,
        since,
        until,
        before: cursor,
        limit: PAGE,
      });
      if (
        generation !== streamGenerationRef.current
        || requestedScopeKey !== streamScopeRef.current
      ) return;
      setItems((previous) => mergeActivityPages(previous, page.items));
      setHasMore(page.has_more);
      setCursor(page.next_cursor);
    } catch (cause) {
      if (
        generation !== streamGenerationRef.current
        || requestedScopeKey !== streamScopeRef.current
      ) return;
      if (isForbidden(cause)) setForbidden(true);
      else setError(toUserMessage(cause, "活动记录加载失败，请重试"));
    } finally {
      if (
        generation === streamGenerationRef.current
        && requestedScopeKey === streamScopeRef.current
      ) setLoading(false);
    }
  }, [loading, hasMore, cursor, userId, notebookId, activityType, since, until]);

  const selectActivityType = useCallback((next: ActivityTypeFilter) => {
    // 失败后当前筛选仍保持选中；重复点击它应当兑现错误文案里的「请重试」，
    // 而不是因为 state 值未变化而静默无事发生。
    if (next === activityType && error) {
      void reload();
      return;
    }
    setActivityType(next);
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (next) params.set("activity_type", next);
    else params.delete("activity_type");
    const query = params.toString();
    window.history.replaceState(
      null,
      "",
      query ? `?${query}` : window.location.pathname,
    );
  }, [activityType, error, reload]);

  const toggleExpand = useCallback((id: string) => {
    const open = expandedRef.current.includes(id);
    setExpanded((previous) => (
      open
        ? previous.filter((value) => value !== id)
        : previous.includes(id) ? previous : [...previous, id]
    ));
    if (open || !userIdRef.current) return;
    // ⚠ 复用的判据是「**已有成功结果**」，不是「这个键有没有值」：失败态对象同样
    // truthy，按 truthy 判会让收起再展开永远不再发请求，而那条文案写的正是「请重试」。
    // 进行中（loading）照旧不重发。
    const cached = sourcesRef.current[id];
    if (cached && (cached.loading || !cached.failure)) return;
    const requestedUserId = userIdRef.current;
    const generation = (sourcesGenerationRef.current[id] ?? 0) + 1;
    sourcesGenerationRef.current[id] = generation;
    setSources((previous) => ({
      ...previous,
      [id]: { items: [], total: 0, loading: true, failure: "" },
    }));
    const fresh = () => sourcesGenerationRef.current[id] === generation
      && requestedUserId === userIdRef.current;
    fetchUserNotebookSources(requestedUserId, id, { limit: SOURCE_PAGE })
      .then((page) => {
        if (!fresh()) return;
        setSources((previous) => ({
          ...previous,
          [id]: {
            items: page.items,
            total: page.total_count,
            loading: false,
            failure: "",
          },
        }));
      })
      .catch((cause) => {
        if (!fresh()) return;
        if (isForbidden(cause)) {
          setForbidden(true);
          return;
        }
        setSources((previous) => ({
          ...previous,
          [id]: {
            items: [],
            total: 0,
            loading: false,
            failure: toUserMessage(cause, "来源清单加载失败，请重试"),
          },
        }));
      });
  }, []);

  const selectItem = useCallback((item: ActivityItem) => {
    setSelected(item);
  }, []);

  // 左栏与中栏选中的是同一种形状（source-view.tsx::toActivitySource 收敛），右栏因此
  // 不需要任何补充参数——尤其不需要服务端算好的日历日字符串，那会让同一份来源在两个
  // 入口显示两种时间格式、乃至两个日期。
  const selectSource = useCallback((source: ActivitySource) => {
    setSelected(source);
  }, []);

  useEffect(() => {
    const targetKey = [
      userId,
      sourceTarget.ownerId,
      sourceTarget.notebookId,
      sourceTarget.sourceId,
    ].join("\u0000");
    if (
      !userId
      || sourceTarget.ownerId !== userId
      || !sourceTarget.notebookId
      || !sourceTarget.sourceId
      || sourceTargetKeyRef.current === targetKey
    ) return;
    if (notebookId !== sourceTarget.notebookId) {
      setNotebookId(sourceTarget.notebookId);
      return;
    }
    sourceTargetKeyRef.current = targetKey;
    fetchUserNotebookSource(
      userId, sourceTarget.notebookId, sourceTarget.sourceId,
    )
      .then((source) => {
        if (sourceTargetKeyRef.current === targetKey) setSelected(source);
      })
      .catch((cause) => {
        if (sourceTargetKeyRef.current !== targetKey) return;
        if (isForbidden(cause)) setForbidden(true);
        else setError(toUserMessage(cause, "来源详情加载失败，请重试"));
      });
  }, [notebookId, sourceTarget, userId]);

  useEffect(() => {
    if (!selected || selected.type !== "ask" || !userId) {
      setAskDetail(null);
      setAskDetailLoading(false);
      setAskDetailError("");
      return;
    }
    const generation = ++detailGenerationRef.current;
    const requestedScopeKey = streamScopeRef.current;
    const jobId = selected.id;
    setAskDetail(null);
    setAskDetailError("");
    setAskDetailLoading(true);
    const fresh = () => generation === detailGenerationRef.current
      && requestedScopeKey === streamScopeRef.current;
    fetchUserAskDetail(userId, jobId)
      .then((detail) => {
        if (!fresh()) return;
        setAskDetail(detail);
      })
      .catch((cause) => {
        if (!fresh()) return;
        if (isForbidden(cause)) {
          setForbidden(true);
          return;
        }
        setAskDetailError(toUserMessage(cause, "问答详情加载失败，请重试"));
      })
      .finally(() => {
        if (!fresh()) return;
        setAskDetailLoading(false);
      });
  }, [selected, userId]);

  const selectedKey = selected ? activityKey(selected) : "";

  if (forbidden) {
    return <div className="activity-notice">{FORBIDDEN_COPY}</div>;
  }

  // 身份未确定 = 加载态，绝不是「这位用户没有活动」。两栏都要跟着，否则左栏会先
  // 闪一句「这位用户还没有建过笔记本」。
  const pending = Boolean(userPending);
  // 身份出错(fetchMe 失败)是第三态：既不是「加载中」也不是「确定了、就是空」。
  // userId 此时必为空串（page.tsx 的 activityUserError 只在 activityUserId 为空
  // 时才置值），左右两栏因此都在「无 userId」分支提前返回空结果——不加这个标志，
  // 就会把「未知」渲染成「确定为空」，与顶部错误横幅同屏、自相矛盾。
  const identityErrored = Boolean(userError);

  return (
    <>
      {userError ? <div className="errorbar">{userError}</div> : null}
      <div className="logview-body logview-activity-body">
        <ActivityScopePanel
          allNotebooksLabel={activityType === "ask" ? "全部提问（含共享笔记本）" : undefined}
          expanded={expanded}
          failure={notebooksFailure}
          identityErrored={identityErrored}
          loading={pending || notebooksLoading}
          notebookId={notebookId}
          notebooks={notebooks}
          onRetryNotebooks={loadNotebooks}
          onSelectNotebook={setNotebookId}
          onSelectSource={selectSource}
          onToggleExpand={toggleExpand}
          selectedKey={selectedKey}
          sources={sources}
        />
        <ActivityStream
          activityFailure={error}
          activityType={activityType}
          hasMore={hasMore}
          identityErrored={identityErrored}
          items={items}
          loading={pending || loading}
          now={now}
          onLoadMore={() => void loadMore()}
          onRetryActivity={() => void reload()}
          onActivityTypeChange={selectActivityType}
          onSelect={selectItem}
          selectedKey={selectedKey}
          showTypeFilter={!fixedActivityType}
        />
        <ActivityDetail
          askDetail={askDetail}
          askDetailError={askDetailError}
          askDetailLoading={askDetailLoading}
          item={selected}
          notebookNames={notebookNames}
          now={now}
        />
      </div>
    </>
  );
}
