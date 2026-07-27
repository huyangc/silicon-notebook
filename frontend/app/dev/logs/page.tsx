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
import { PageHeader } from "../../components/PageHeader.tsx";
import { toUserMessage } from "../../errors.ts";
import { usernameForOwner } from "./owner";
import { dayLabel, TODAY_VALUE } from "./date.ts";

const PAGE = 200;
const POLL_MS = 5000;

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
  const [adminUsers, setAdminUsers] = useState<AdminUserUsage[]>([]);
  const [owner, setOwner] = useState("");
  const [date, setDate] = useState(TODAY_VALUE);
  const [days, setDays] = useState<string[]>([]);

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

  // read ?owner= and ?date= from the URL once on mount (admin drill-down / deep-link entry points)
  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const o = params.get("owner");
    if (o) setOwner(o);
    const d = params.get("date");
    if (d) setDate(d);
  }, []);

  // current user + (admin-only) user list for the drill-down dropdown
  useEffect(() => {
    fetchMe()
      .then((u) => {
        setMe(u);
        if (u.role === "admin") {
          fetchAdminUsers()
            .then(setAdminUsers)
            .catch(() => undefined);
        }
      })
      .catch(() => undefined);
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

  useEffect(() => {
    clearScopedResults();
  }, [clearScopedResults, requestScopeKey]);

  useEffect(() => {
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
  }, [owner]);

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
    void reload();
  }, [reload]);

  // debounce search box -> q
  useEffect(() => {
    const t = setTimeout(() => setQ(qInput.trim()), 300);
    return () => clearTimeout(t);
  }, [qInput]);

  // auto-refresh polling: pull records newer than newestSeq into `pending`
  // (only meaningful for "today" — a fixed historical day has no new records to poll for)
  useEffect(() => {
    if (!autoRefresh || date !== TODAY_VALUE) return;
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
  }, [autoRefresh, newestSeq, filterKey, filterParams, requestScopeKey]);

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
      </div>

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
    </div>
  );
}
