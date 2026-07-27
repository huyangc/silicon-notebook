"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import "./logs.css";
import type { ChannelInfo, FullRecord, Stats, Summary } from "./types";
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

export default function LogsPage() {
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const channel = "llm"; // v1: fixed channel
  const [records, setRecords] = useState<Summary[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [truncated, setTruncated] = useState(false);
  const [newestSeq, setNewestSeq] = useState<number | null>(null);
  const [pending, setPending] = useState<Summary[]>([]);
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

  const selectOwner = useCallback((next: string) => {
    setOwner(next);
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (next) params.set("owner", next);
    else params.delete("owner");
    const qs = params.toString();
    window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
  }, []);

  const selectDate = useCallback((next: string) => {
    setDate(next);
    if (next !== TODAY_VALUE) setAutoRefresh(false); // 仅当天可实时；切到历史天时关闭自动刷新，避免勾选态和禁用态不一致
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    if (next) params.set("date", next);
    else params.delete("date");
    const qs = params.toString();
    window.history.replaceState(null, "", qs ? `?${qs}` : window.location.pathname);
  }, []);

  useEffect(() => {
    fetchChannels(owner)
      .then((r) => setChannels(r.channels))
      .catch((e) => setError(toUserMessage(e, "日志加载失败，请重试")));
  }, [owner]);

  useEffect(() => {
    fetchDays(channel, owner)
      .then((r) => setDays(r.days))
      .catch(() => undefined);
  }, [owner]);

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await fetchRecords(channel, { limit: PAGE, ...filterParams });
      setRecords(r.records);
      setStats(r.stats);
      setHasMore(r.has_more);
      setTruncated(r.truncated);
      setNewestSeq(r.newest_seq);
      setPending([]);
    } catch (e) {
      setError(toUserMessage(e, "日志加载失败，请重试"));
    } finally {
      setLoading(false);
    }
  }, [filterParams]);

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
      try {
        const r = await fetchRecords(channel, { since: newestSeq, ...filterParams });
        if (r.records.length) {
          setPending((prev) => {
            const seen = new Set(prev.map((x) => x.seq));
            const fresh = r.records.filter((x) => !seen.has(x.seq));
            return [...fresh, ...prev];
          });
          if (r.newest_seq != null) setNewestSeq(r.newest_seq);
          setStats(r.stats);
        }
      } catch {
        /* ignore transient poll errors */
      }
    }, POLL_MS);
    return () => clearInterval(t);
  }, [autoRefresh, newestSeq, filterParams]);

  const showNew = useCallback(() => {
    setRecords((prev) => {
      const seen = new Set(prev.map((x) => x.seq));
      return [...pending.filter((x) => !seen.has(x.seq)), ...prev];
    });
    setPending([]);
  }, [pending]);

  const loadMore = useCallback(async () => {
    if (!records.length) return;
    const oldest = records[records.length - 1].seq;
    setLoading(true);
    try {
      const r = await fetchRecords(channel, { before: oldest, limit: PAGE, ...filterParams });
      setRecords((prev) => [...prev, ...r.records]);
      setHasMore(r.has_more);
    } catch (e) {
      setError(toUserMessage(e, "日志加载失败，请重试"));
    } finally {
      setLoading(false);
    }
  }, [records, filterParams]);

  const select = useCallback(
    async (rec: Summary) => {
      setSelectedId(rec.id);
      setDetail(null);
      setDetailLoading(true);
      try {
        setDetail(await fetchRecord(channel, rec.id, date || undefined, rec.seq, owner || undefined));
      } catch (e) {
        setError(toUserMessage(e, "日志加载失败，请重试"));
      } finally {
        setDetailLoading(false);
      }
    },
    [date, owner],
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
          hasMore={hasMore}
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
