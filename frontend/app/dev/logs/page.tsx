"use client";
import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import "./logs.css";
import type { ChannelInfo, FullRecord, Stats, Summary } from "./types";
import { fetchChannels, fetchRecord, fetchRecords } from "./api";
import { ChannelTabs } from "./components/ChannelTabs";
import { StatsBar } from "./components/StatsBar";
import { LogList } from "./components/LogList";
import { LogDetail } from "./components/LogDetail";

const PAGE = 200;
const POLL_MS = 5000;

export default function LogsPage() {
  const [channels, setChannels] = useState<ChannelInfo[]>([]);
  const channel = "llm"; // v1: fixed channel
  const [records, setRecords] = useState<Summary[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [hasMore, setHasMore] = useState(false);
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

  const filterParams = useMemo(() => ({ kind, status, model, q }), [kind, status, model, q]);

  useEffect(() => {
    fetchChannels()
      .then((r) => setChannels(r.channels))
      .catch((e) => setError(String(e)));
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const r = await fetchRecords(channel, { limit: PAGE, ...filterParams });
      setRecords(r.records);
      setStats(r.stats);
      setHasMore(r.has_more);
      setNewestSeq(r.newest_seq);
      setPending([]);
    } catch (e) {
      setError(String(e));
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
  useEffect(() => {
    if (!autoRefresh) return;
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
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [records, filterParams]);

  const select = useCallback(async (rec: Summary) => {
    setSelectedId(rec.id);
    setDetail(null);
    setDetailLoading(true);
    try {
      setDetail(await fetchRecord(channel, rec.id));
    } catch (e) {
      setError(String(e));
    } finally {
      setDetailLoading(false);
    }
  }, []);

  const facets = stats?.facets;

  return (
    <div className="logview">
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
        <label className="auto-toggle">
          <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} /> 自动刷新
        </label>
      </div>

      {error ? <div className="errorbar">{error}</div> : null}

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
