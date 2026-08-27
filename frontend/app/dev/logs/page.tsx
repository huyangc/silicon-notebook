"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import "./logs.css";
import type { ChannelInfo, FullRecord, ScopedSummary, Stats, Summary } from "./types";
import { fetchChannels, fetchDays, fetchRecord, fetchRecords } from "./api";
import { ChannelTabs } from "./components/ChannelTabs";
import { StatsBar } from "./components/StatsBar";
import { LogList } from "./components/LogList";
import { LogDetail } from "./components/LogDetail";
import { fetchMe, type AuthUser } from "../../auth.ts";
import { fetchAdminUsers, type AdminUserUsage } from "../../admin/usage/api.ts";
import { fetchSystemConfiguration } from "../../system-api.ts";
import { PageHeader } from "../../components/PageHeader.tsx";
import { toUserMessage } from "../../errors.ts";
import { usernameForOwner } from "./owner";
import {
  ALL_TIME_VALUE,
  activityDayLabel,
  dayLabel,
  dayRange,
  LEGACY_VALUE,
  TODAY_VALUE,
} from "./date.ts";
import { ActivityView } from "./activity/ActivityView.tsx";

const PAGE = 200;
const POLL_MS = 5000;

// 两个视图共享顶部同一条范围条。「活动」是默认视图（用户活动才是这一页现在要
// 回答的问题）；「模型调用」是原样保留的既有排障视图。
type LogsView = "activity" | "llm";

function scopeKey(owner: string, date: string): string {
  return JSON.stringify([owner, date]);
}

function bindRecordScope(
  records: Summary[],
  owner: string,
  date: string,
  requestKey: string,
  filterKey: string,
): ScopedSummary[] {
  return records.map((record) => ({
    ...record,
    _scope: { owner, date, requestKey, filterKey },
  }));
}

function recordKey(record: Summary): string {
  return `${record.seq}\u0000${record.id}`;
}

export default function LogsPage() {
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const channel = "llm"; // v1: fixed channel
  const [records, setRecords] = useState<ScopedSummary[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [newestSeq, setNewestSeq] = useState<number | null>(null);
  const [pending, setPending] = useState<ScopedSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<FullRecord | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(false);

  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [model, setModel] = useState("");
  const [q, setQ] = useState("");
  const [qInput, setQInput] = useState("");

  const [me, setMe] = useState<AuthUser | null>(null);
  // 「当前用户还没回来」与「回来了但取不到」必须是两个可见的态：活动流的用户维度
  // 就靠 me.id 解析，把两者混进一个 null 会让页面先说「没有活动记录」，或者在
  // fetchMe 失败后**永远**这么说而不给任何提示。
  const [meResolved, setMeResolved] = useState(false);
  const [meError, setMeError] = useState("");
  const [adminUsers, setAdminUsers] = useState<AdminUserUsage[]>([]);
  const [owner, setOwner] = useState("");
  const [date, setDate] = useState(TODAY_VALUE);
  const [days, setDays] = useState<string[]>([]);
  const [view, setView] = useState<LogsView>("activity");
  // 部署开关 USER_ACTIVITY_VIEW_ENABLED 的前端镜像,经 /system/config 下发。
  // **三态**:null=还没问到,true/false=已确定。
  //
  // 不能拿 true 当初值。这个能力位与三个活动端点是同一次改动一起上线的,所以
  // 「问不到」既可能是开关关着、也可能是后端根本没有这个特性(旧后端连
  // /system/config 里的字段都没有)。先按 true 渲染就会在后一种情况下挂载活动
  // 视图、打出三个 404,与开关显式关闭时的失败形态一模一样。
  //
  // 未知期的取舍:不挂载「活动」(所以它的三个端点一次都不会被提前打出去),但
  // 「模型调用」照常立即渲染。刻意不给未知期加全页加载态——那会让「模型调用」
  // 视图也多等一个往返,而它的红线是相对改版前**零回归**。代价是能力位开着的
  // 部署会先显示「模型调用」、一个往返后「活动」tab 才出现并接管;
  // /system/config 是一次很小的认证请求,窗口极短。
  const [activityViewEnabled, setActivityViewEnabled] = useState<boolean | null>(null);
  // 渲染只认这个值:能力位未确定或已确定为关时,「活动」都不成立(收敛成
  // 「模型调用」),即使 `view` state 本身(默认值,或历史 `?view=activity` 深链)
  // 还没被下面的归一 effect 改写过来。
  const effectiveView: LogsView = view === "activity" && activityViewEnabled !== true ? "llm" : view;

  const filterParams = useMemo(
    () => ({ kind, status, model, q, owner, date }),
    [kind, status, model, q, owner, date],
  );
  const requestScopeKey = useMemo(() => scopeKey(owner, date), [owner, date]);
  const filterKey = useMemo(() => JSON.stringify(filterParams), [filterParams]);
  const currentScopeKeyRef = useRef(requestScopeKey);
  const currentFilterKeyRef = useRef(filterKey);
  const currentOwnerRef = useRef(owner);
  const listGenerationRef = useRef(0);
  const detailGenerationRef = useRef(0);
  const channelGenerationRef = useRef(0);
  const daysGenerationRef = useRef(0);
  const recordsRef = useRef(records);
  currentScopeKeyRef.current = requestScopeKey;
  currentFilterKeyRef.current = filterKey;
  currentOwnerRef.current = owner;
  recordsRef.current = records;

  const clearScopedResults = useCallback(() => {
    listGenerationRef.current += 1;
    detailGenerationRef.current += 1;
    setRecords([]);
    setPending([]);
    setStats(null);
    setHasMore(false);
    setTruncated(false);
    setNewestSeq(null);
    setSelectedId(null);
    setDetail(null);
    setDetailLoading(false);
    setLoading(false);
    setError("");
  }, []);

  // read ?owner=, ?date= and ?view= from the URL once on mount
  // (admin drill-down / deep-link entry points)
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const o = params.get("owner");
    if (o) setOwner(o);
    const d = params.get("date");
    if (d) setDate(d);
    const v = params.get("view");
    if (v === "llm" || v === "activity") setView(v);
  }, []);

  // /system/config 能力位。**失败也要落终态**(false):停在 null 会让页面永远卡在
  // 未知期的加载态,而「问不出来」与「没有这个特性」在可用性上是同一件事——旧后端
  // 既没有这个字段、也没有那三个端点。
  useEffect(() => {
    fetchSystemConfiguration()
      .then((config) => setActivityViewEnabled(config.user_activity_view_enabled))
      .catch(() => setActivityViewEnabled(false));
  }, []);

  // 确定为关之后,把已经落进 state/URL 的 `view=activity`(初始默认值,或历史
  // `?view=activity` 深链)归一成 `llm`——渲染已经用 effectiveView 挡住了内容,
  // 这里补的是 URL 与 state 本身,好让「模型调用」侧按 view 门控的取数 effect
  // 正常触发,也不留下一个手工分享出去会 404 的链接。
  //
  // 判据必须是 `=== false` 而不是 `!activityViewEnabled`:后者会把未知期的 null
  // 也算进来,在还没问到结果时就改写用户的 URL。
  useEffect(() => {
    if (activityViewEnabled !== false || view !== "activity") return;
    setView("llm");
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    params.set("view", "llm");
    window.history.replaceState(null, "", `?${params.toString()}`);
  }, [activityViewEnabled, view]);

  // current user + (admin-only) user list for the drill-down dropdown
  useEffect(() => {
    fetchMe()
      .then((u) => {
        setMe(u);
        setMeResolved(true);
        if (u.role === "admin") {
          fetchAdminUsers()
            .then(setAdminUsers)
            .catch(() => undefined);
        }
      })
      .catch((e) => {
        // ⚠ 不能吞。一次瞬时 500 会让活动视图永远解析不出用户 id，界面上却只写着
        // 「这个范围里没有活动记录」——一句无法证伪的假陈述。
        setMeResolved(true);
        setMeError(toUserMessage(e, "当前用户信息加载失败，请刷新页面重试"));
      });
  }, []);

  const selectOwner = useCallback(
    (next: string) => {
      if (next !== owner) clearScopedResults();
      setOwner(next);
      if (typeof window === "undefined") return;
      const params = new URLSearchParams(window.location.search);
      if (next) params.set("owner", next);
      else params.delete("owner");
      const qs = params.toString();
      window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
    },
    [clearScopedResults, owner],
  );

  const selectDate = useCallback(
    (next: string) => {
      if (next !== date) clearScopedResults();
      setDate(next);
      if (next !== TODAY_VALUE) setAutoRefresh(false); // 仅当天可实时；切到历史天时关闭自动刷新，避免勾选态和禁用态不一致
      if (typeof window === "undefined") return;
      const params = new URLSearchParams(window.location.search);
      if (next) params.set("date", next);
      else params.delete("date");
      const qs = params.toString();
      window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
    },
    [clearScopedResults, date],
  );

  const selectView = useCallback(
    (next: LogsView) => {
      setView(next);
      // 「模型调用」那一侧的错误横幅只在它自己的分支渲染。切到活动视图时不清掉，
      // 它会以陈旧形态在切回来时突然弹出来（而那次失败早已过去）。
      setError("");
      // 自动刷新的复选框只长在「模型调用」的过滤条上。留着勾选态切到活动视图，
      // 轮询会继续每 5 秒打日志接口，而**用户没有任何办法关掉它**——控件已经不在
      // 屏幕上了。切走即关，切回来时由用户自己重新勾。
      if (next === "activity") setAutoRefresh(false);
      // `legacy`（历史·未分天）是日志**文件**的分区，活动流读的是库里的 created_at，
      // 没有这个概念。切到活动视图时归一成空值（= 全部时间）。
      if (next === "activity" && date === LEGACY_VALUE) selectDate(TODAY_VALUE);
      // 反方向：两个视图共享同一个 date state，取值集却不同。活动视图是原生日历，
      // 日历上的任何一天都能选；「模型调用」是 <select>，options 只有「今天」（空值）
      // + 有日志文件的那几天。带着一个下拉里没有的日期切回去，控件**不会**空白——
      // React 的受控 <select> 找不到匹配 option 时会回落到第一个可选项
      // （ReactDOMSelect.updateOptions），于是下拉写着「今天」、请求发的却是那个没有
      // 日志文件的日子：列表恒空，而且用户点一下那个已经显示着的「今天」不产生
      // change 事件、根本回不去。两次点击就能复现，而「今天传了来源、零次模型调用」
      // 恰恰是活动视图换用日历控件的动机。
      // ⚠ 判据是「在不在 options 里」，不是「是不是 legacy」：`legacy` 当且仅当后端
      // 列出了那个分区（即它在 days 里）才是这个下拉的合法取值，按 days 归一两种情况
      // 自动都对；给 legacy 开一个无条件豁免反而会放过「days 里没有它」那一半。
      if (next === "llm" && date !== TODAY_VALUE && !days.includes(date)) {
        selectDate(TODAY_VALUE);
      }
      if (typeof window === "undefined") return;
      const params = new URLSearchParams(window.location.search);
      params.set("view", next);
      window.history.replaceState(null, "", `?${params.toString()}`);
    },
    [date, days, selectDate],
  );

  useEffect(() => {
    clearScopedResults();
  }, [clearScopedResults, requestScopeKey]);

  // ⚠ 「模型调用」视图的取数一律按 view 门控（本 effect、下面的 reload、以及自动
  // 刷新轮询三处）。此前它们在挂载时就发，而默认视图是「活动」——用户一眼看不到的
  // 一整套请求（含带全量 stats 聚合的 fetchRecords）在每次改 owner、改日期时都要
  // 再发一遍。`docs/development.md`：效率是一等约束，新增调用先问代价。
  // 门控只影响**什么时候发**：view 在依赖里，切回「模型调用」时该发的一次照发。
  // fetchDays 刻意不门控——它喂的日期下拉在两个视图之间共享入口位置，且是一次
  // 廉价的目录列举，提前备好可以让切过去时不再等一跳。
  useEffect(() => {
    if (view !== "llm") return;
    const generation = ++channelGenerationRef.current;
    const requestedOwner = owner;
    setChannels([]);
    fetchChannels(requestedOwner)
      .then((r) => {
        if (
          generation === channelGenerationRef.current
          && requestedOwner === currentOwnerRef.current
        ) setChannels(r.channels);
      })
      .catch((e) => {
        if (
          generation === channelGenerationRef.current
          && requestedOwner === currentOwnerRef.current
        ) setError(toUserMessage(e, "日志加载失败，请重试"));
      });
  }, [owner, view]);

  useEffect(() => {
    const generation = ++daysGenerationRef.current;
    const requestedOwner = owner;
    setDays([]);
    fetchDays(channel, requestedOwner)
      .then((r) => {
        if (
          generation === daysGenerationRef.current
          && requestedOwner === currentOwnerRef.current
        ) setDays(r.days);
      })
      .catch(() => undefined);
  }, [owner]);

  const reload = useCallback(async () => {
    const generation = ++listGenerationRef.current;
    const requestedFilterKey = filterKey;
    const requestedScopeKey = requestScopeKey;
    setLoading(true);
    setError("");
    try {
      const r = await fetchRecords(channel, { limit: PAGE, ...filterParams });
      if (
        generation !== listGenerationRef.current
        || requestedFilterKey !== currentFilterKeyRef.current
      ) return;
      setRecords(bindRecordScope(
        r.records,
        filterParams.owner,
        r.date,
        requestedScopeKey,
        requestedFilterKey,
      ));
      setStats(r.stats);
      setHasMore(r.has_more);
      setTruncated(r.truncated);
      setNewestSeq(r.newest_seq);
      setPending([]);
    } catch (e) {
      if (
        generation !== listGenerationRef.current
        || requestedFilterKey !== currentFilterKeyRef.current
      ) return;
      setError(toUserMessage(e, "日志加载失败，请重试"));
    } finally {
      if (
        generation === listGenerationRef.current
        && requestedFilterKey === currentFilterKeyRef.current
      ) setLoading(false);
    }
  }, [filterKey, filterParams, requestScopeKey]);

  useEffect(() => {
    if (view !== "llm") return;
    void reload();
  }, [reload, view]);

  // debounce search box -> q
  useEffect(() => {
    const t = setTimeout(() => setQ(qInput.trim()), 300);
    return () => clearTimeout(t);
  }, [qInput]);

  // auto-refresh polling: pull records newer than newestSeq into `pending`
  // (only meaningful for "today" — a fixed historical day has no new records to poll for)
  useEffect(() => {
    if (view !== "llm" || !autoRefresh || date !== TODAY_VALUE) return;
    const t = setInterval(async () => {
      if (newestSeq == null) return;
      const generation = listGenerationRef.current;
      const requestedFilterKey = filterKey;
      const requestedScopeKey = requestScopeKey;
      try {
        const r = await fetchRecords(channel, { since: newestSeq, ...filterParams });
        if (
          generation !== listGenerationRef.current
          || requestedFilterKey !== currentFilterKeyRef.current
        ) return;
        if (r.records.length) {
          const scopedRecords = bindRecordScope(
            r.records,
            filterParams.owner,
            r.date,
            requestedScopeKey,
            requestedFilterKey,
          );
          setPending((prev) => {
            const seen = new Set([
              ...recordsRef.current.map(recordKey),
              ...prev.map(recordKey),
            ]);
            const fresh = scopedRecords.filter((x) => !seen.has(recordKey(x)));
            return [...fresh, ...prev];
          });
          if (r.newest_seq != null) {
            setNewestSeq((previous) => Math.max(previous ?? r.newest_seq!, r.newest_seq!));
          }
          setStats(r.stats);
        }
      } catch {
        /* ignore transient poll errors */
      }
    }, POLL_MS);
    return () => clearInterval(t);
  }, [autoRefresh, newestSeq, filterKey, filterParams, requestScopeKey, view]);

  const showNew = useCallback(() => {
    setRecords((prev) => {
      const seen = new Set(prev.map((x) => x.seq));
      return [...pending.filter((x) => !seen.has(x.seq)), ...prev];
    });
    setPending([]);
  }, [pending]);

  const loadMore = useCallback(async () => {
    if (loading || !records.length) return;
    const lastRecord = records[records.length - 1];
    if (lastRecord._scope.filterKey !== currentFilterKeyRef.current) return;
    const oldest = lastRecord.seq;
    const generation = ++listGenerationRef.current;
    const requestedFilterKey = filterKey;
    const requestedScopeKey = requestScopeKey;
    setLoading(true);
    try {
      const r = await fetchRecords(channel, { before: oldest, limit: PAGE, ...filterParams });
      if (
        generation !== listGenerationRef.current
        || requestedFilterKey !== currentFilterKeyRef.current
      ) return;
      const scopedRecords = bindRecordScope(
        r.records,
        filterParams.owner,
        r.date,
        requestedScopeKey,
        requestedFilterKey,
      );
      setRecords((prev) => [...prev, ...scopedRecords]);
      setHasMore(r.has_more);
    } catch (e) {
      if (
        generation !== listGenerationRef.current
        || requestedFilterKey !== currentFilterKeyRef.current
      ) return;
      setError(toUserMessage(e, "日志加载失败，请重试"));
    } finally {
      if (
        generation === listGenerationRef.current
        && requestedFilterKey === currentFilterKeyRef.current
      ) setLoading(false);
    }
  }, [loading, records, filterKey, filterParams, requestScopeKey]);

  const select = useCallback(
    async (rec: ScopedSummary) => {
      const generation = ++detailGenerationRef.current;
      const recordScope = rec._scope;
      setSelectedId(rec.id);
      setDetail(null);
      setDetailLoading(true);
      try {
        const nextDetail = await fetchRecord(
          channel,
          rec.id,
          recordScope.date || undefined,
          rec.seq,
          recordScope.owner || undefined,
        );
        if (
          generation !== detailGenerationRef.current
          || recordScope.requestKey !== currentScopeKeyRef.current
        ) return;
        setDetail(nextDetail);
      } catch (e) {
        if (
          generation !== detailGenerationRef.current
          || recordScope.requestKey !== currentScopeKeyRef.current
        ) return;
        setError(toUserMessage(e, "日志加载失败，请重试"));
      } finally {
        if (
          generation === detailGenerationRef.current
          && recordScope.requestKey === currentScopeKeyRef.current
        ) setDetailLoading(false);
      }
    },
    [],
  );

  const facets = stats?.facets;

  // 顶部范围条选「我自己」时 owner 是空串，而活动流的端点要的是一个具体用户 id。
  // 未就绪（me 还没回来）时给空串，ActivityView 会按「尚未确定用户」跳过取数——
  // 但它同时要知道这是「还没确定」还是「确定了、就是空」，见下面两个标志。
  const activityUserId = owner || me?.id || "";
  // 显式选了 owner 时不依赖 me，因此 fetchMe 的挂起/失败都与活动视图无关。
  const activityUserPending = !activityUserId && !meResolved;
  const activityUserError = activityUserId ? "" : meError;
  // 页面级范围键：视图 tab + 用户 + 日期。ActivityView 再叠上 notebook_id。
  const activityScopeKey = useMemo(
    () => JSON.stringify([view, owner, date]),
    [view, owner, date],
  );
  const activityRange = useMemo(() => dayRange(date), [date]);
  // `<input type="date">` 只认 `YYYY-MM-DD`。合法性直接借 dayRange 判定（它已经
  // 做过 2026-02-30 这类的回环校验），不在这里另写第二份解析：空串 = 全部时间，
  // `legacy` 这类日志文件分区名同样落到空串。
  const activityDateValue = activityRange.since ? date : "";

  return (
    <div className="logview">
      <PageHeader title="日志查看" />
      <div className="logview-owner">
        <span>
          当前查看:<strong>{usernameForOwner(adminUsers, owner, me?.username ?? "")}</strong>
        </span>
        {me?.role === "admin" ? (
          <select
            className="logview-owner-select"
            value={owner}
            onChange={(e) => selectOwner(e.target.value)}
          >
            <option value="">我自己{me.username ? `(${me.username})` : ""}</option>
            {adminUsers.map((u) => (
              <option key={u.id} value={u.id}>
                {u.username}
              </option>
            ))}
          </select>
        ) : null}
        {/* 两个视图的日期取值集来自完全不同的地方，所以控件也是两个：
            · 「模型调用」按天分文件是事实，取值集 = 有日志文件的那几天 → 保留下拉。
            · 「活动」读的是库里的 created_at。拿日志文件的分天去筛它会同时错两个
              方向：某用户今天传了 5 份来源、0 次模型调用 → 今天没有 llm 文件 →
              下拉里没有今天，而空值又被「全部时间」占着，这位用户的当日活动
              **没有任何办法筛出来**；反过来，只有模型调用的那些天会出现在下拉里、
              选中后永远是空流。所以这里换成原生日期选择器，日历上的任何一天都能选。 */}
        {effectiveView === "activity" ? (
          <span className="logview-date-picker">
            <input
              aria-label="按日期筛选活动"
              className="logview-date-input"
              onChange={(e) => selectDate(e.target.value)}
              type="date"
              value={activityDateValue}
            />
            {activityDateValue ? (
              <button
                className="logview-date-clear"
                onClick={() => selectDate(ALL_TIME_VALUE)}
                type="button"
              >
                {activityDayLabel(ALL_TIME_VALUE)}
              </button>
            ) : (
              <span className="logview-date-all">{activityDayLabel(ALL_TIME_VALUE)}</span>
            )}
          </span>
        ) : (
          <select
            className="logview-date-select"
            value={date}
            onChange={(e) => selectDate(e.target.value)}
          >
            <option value={TODAY_VALUE}>{dayLabel(TODAY_VALUE)}</option>
            {days.map((d) => (
              <option key={d} value={d}>
                {dayLabel(d)}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="logview-views">
        <div className="logview-tabs">
          {/* 能力位关闭时不渲染这个 tab——不只是禁用,压根不给一个会打三个 404
              端点的入口。 */}
          {activityViewEnabled ? (
            <button
              className={`logview-tab${effectiveView === "activity" ? " active" : ""}`}
              onClick={() => selectView("activity")}
              type="button"
            >
              活动
            </button>
          ) : null}
          <button
            className={`logview-tab${effectiveView === "llm" ? " active" : ""}`}
            onClick={() => selectView("llm")}
            type="button"
          >
            模型调用
          </button>
        </div>
      </div>

      {effectiveView === "activity" ? (
        <ActivityView
          scopeKey={activityScopeKey}
          since={activityRange.since}
          until={activityRange.until}
          userError={activityUserError}
          userId={activityUserId}
          userPending={activityUserPending}
        />
      ) : (
      <>
      <div className="logview-top">
        <ChannelTabs channels={channels} active={channel} onSelect={() => undefined} />
        <StatsBar stats={stats} />
      </div>

      <div className="logview-filters">
        <select value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">kind: 全部</option>
          {(facets?.kinds ?? []).map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">status: 全部</option>
          {(facets?.statuses ?? []).map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select value={model} onChange={(e) => setModel(e.target.value)}>
          <option value="">model: 全部</option>
          {(facets?.models ?? []).map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
        <input
          className="logview-search"
          placeholder="搜索 prompt / response / error…"
          value={qInput}
          onChange={(e) => setQInput(e.target.value)}
        />
        <button className="copy-btn" onClick={() => void reload()}>
          <RefreshCw size={13} /> 刷新
        </button>
        <label
          className="auto-toggle"
          title={date !== TODAY_VALUE ? "仅当天可实时" : ""}
        >
          <input
            type="checkbox"
            checked={autoRefresh}
            disabled={date !== TODAY_VALUE}
            onChange={(e) => setAutoRefresh(e.target.checked)}
          />{" "}
          自动刷新{date !== TODAY_VALUE ? "(仅当天可实时)" : ""}
        </label>
      </div>

      {error ? <div className="errorbar">{error}</div> : null}
      {truncated ? (
        <div className="logview-trunc">
          已截断,仅显示最近 {records.length} 条,请选择具体某天或缩小范围
        </div>
      ) : null}

      <div className="logview-body">
        <LogList
          records={records}
          selectedId={selectedId}
          onSelect={select}
          hasMore={
            hasMore
            && records.length > 0
            && records[records.length - 1]._scope.filterKey === filterKey
          }
          onLoadMore={() => void loadMore()}
          newCount={pending.length}
          onShowNew={showNew}
          loading={loading}
        />
        <LogDetail record={detail} loading={detailLoading} />
      </div>
      </>
      )}
    </div>
  );
}
