"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { toUserMessage } from "../../errors.ts";
import {
  fetchAnalysisIssues,
  type AdminUserUsage,
  type AnalysisIssue,
} from "./api.ts";

function categoryLabel(value: AnalysisIssue["category"]): string {
  return value === "spreadsheet_analysis" ? "Excel 专业分析" : "文档解析";
}

export function AnalysisIssuesSheet({ users }: { users: AdminUserUsage[] }) {
  const requestedOwner = useMemo(() => {
    if (typeof window === "undefined") return "";
    const value = new URLSearchParams(window.location.search).get("owner") || "";
    return users.some((user) => user.id === value) ? value : "";
  }, [users]);
  const [ownerId, setOwnerId] = useState(requestedOwner);
  const [status, setStatus] = useState<"" | "open" | "resolved">("open");
  const [category, setCategory] = useState<"" | "source_parse" | "spreadsheet_analysis">("");
  const [items, setItems] = useState<AnalysisIssue[]>([]);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState("");
  const usernames = useMemo(
    () => Object.fromEntries(users.map((user) => [user.id, user.username])),
    [users],
  );

  const load = useCallback(async () => {
    setLoading(true);
    setFailure("");
    try {
      setItems(await fetchAnalysisIssues({ ownerId, status, category }));
    } catch (cause) {
      setFailure(toUserMessage(cause, "解析问题加载失败，请重试"));
    } finally {
      setLoading(false);
    }
  }, [category, ownerId, status]);

  useEffect(() => { void load(); }, [load]);

  function selectOwner(value: string) {
    setOwnerId(value);
    const params = new URLSearchParams(window.location.search);
    params.set("sheet", "issues");
    if (value) params.set("owner", value);
    else params.delete("owner");
    window.history.replaceState(null, "", `?${params.toString()}`);
  }

  return (
    <section className="usage-analysis-sheet" aria-label="解析问题">
      <div className="usage-analysis-toolbar">
        <label>
          用户
          <select value={ownerId} onChange={(event) => selectOwner(event.target.value)}>
            <option value="">全部用户</option>
            {users.map((user) => <option value={user.id} key={user.id}>{user.username}</option>)}
          </select>
        </label>
        <label>
          状态
          <select value={status} onChange={(event) => setStatus(event.target.value as typeof status)}>
            <option value="open">未解决</option>
            <option value="resolved">已解决</option>
            <option value="">全部</option>
          </select>
        </label>
        <label>
          类型
          <select value={category} onChange={(event) => setCategory(event.target.value as typeof category)}>
            <option value="">全部</option>
            <option value="source_parse">文档解析</option>
            <option value="spreadsheet_analysis">Excel 专业分析</option>
          </select>
        </label>
        <button type="button" className="usage-role-button" disabled={loading} onClick={() => void load()}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>
      <p className="usage-analysis-note">
        本页只读。失败文件由系统自动隔离、成功后自动清理副本，并在保留期到期后清除记录；管理员不能在此重试、删除或改动用户来源。
      </p>
      {failure && <div className="usage-role-notice usage-role-notice-error" role="alert">{failure}</div>}
      <div className="usage-table-wrap">
        <table className="usage-table usage-issues-table">
          <thead>
            <tr>
              <th>状态</th><th>用户</th><th>类型</th><th>问题</th><th>来源</th><th>记录时间</th><th>自动清理时间</th><th>定位</th>
            </tr>
          </thead>
          <tbody>
            {!loading && items.length === 0 && (
              <tr><td colSpan={8} className="usage-analysis-empty">当前筛选范围内没有解析问题。</td></tr>
            )}
            {items.map((item) => (
              <tr key={item.id}>
                <td><span className={`usage-issue-status usage-issue-status-${item.status}`}>{item.status === "open" ? "未解决" : "已解决"}</span></td>
                <td>{usernames[item.owner_id] || "已删除用户"}</td>
                <td>{categoryLabel(item.category)}</td>
                <td><strong>{item.code}</strong><br /><span>{item.summary}</span></td>
                <td>{item.source_deleted ? "来源已删除（已脱敏）" : (item.source_title || item.file_name || "未命名来源")}</td>
                <td>{item.updated_at.replace("T", " ").slice(0, 16)}</td>
                <td>{item.expires_at.replace("T", " ").slice(0, 16)}</td>
                <td>
                  {item.notebook_id && item.source_id && !item.source_deleted && !item.notebook_deleted
                    ? <a href={`/dev/logs?view=activity&owner=${encodeURIComponent(item.owner_id)}&activity_type=source&notebook_id=${encodeURIComponent(item.notebook_id)}&source_id=${encodeURIComponent(item.source_id)}`}>查看来源详情</a>
                    : <span>仅保留脱敏摘要</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
