"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { toUserMessage } from "../../errors.ts";
import {
  fetchAnalysisIssues,
  fetchAnalysisIssueModelArtifact,
  type AdminUserUsage,
  type AnalysisIssue,
  type AnalysisIssueModelArtifact,
  type ModelAnalysisArea,
} from "./api.ts";

function categoryLabel(value: AnalysisIssue["category"]): string {
  if (value === "spreadsheet_analysis") return "Excel 专业分析";
  if (value === "model_output") return "模型回答格式";
  return "文档解析";
}

const MODEL_AREA_LABELS: Record<ModelAnalysisArea, string> = {
  ask: "问答",
  report: "深度报告",
  source: "来源处理",
  knowledge: "知识库",
  memory: "记忆与库理解",
  knowhow: "经验表格",
  retrieval: "检索优化",
};

export function AnalysisIssuesSheet({ users }: { users: AdminUserUsage[] }) {
  const requestedOwner = useMemo(() => {
    if (typeof window === "undefined") return "";
    const value = new URLSearchParams(window.location.search).get("owner") || "";
    return users.some((user) => user.id === value) ? value : "";
  }, [users]);
  const [ownerId, setOwnerId] = useState(requestedOwner);
  const [status, setStatus] = useState<"" | "open" | "resolved">("open");
  const [category, setCategory] = useState<
    "" | "source_parse" | "spreadsheet_analysis" | "model_output"
  >("");
  const [modelArea, setModelArea] = useState<"" | ModelAnalysisArea>("");
  const [items, setItems] = useState<AnalysisIssue[]>([]);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [artifact, setArtifact] = useState<AnalysisIssueModelArtifact | null>(null);
  const [artifactLoading, setArtifactLoading] = useState(false);
  const [artifactFailure, setArtifactFailure] = useState("");
  const artifactRequest = useRef(0);
  const usernames = useMemo(
    () => Object.fromEntries(users.map((user) => [user.id, user.username])),
    [users],
  );

  const load = useCallback(async () => {
    artifactRequest.current += 1;
    setSelectedId("");
    setArtifact(null);
    setArtifactLoading(false);
    setArtifactFailure("");
    setLoading(true);
    setFailure("");
    try {
      setItems(await fetchAnalysisIssues({ ownerId, status, category, modelArea }));
    } catch (cause) {
      setFailure(toUserMessage(cause, "解析问题加载失败，请重试"));
    } finally {
      setLoading(false);
    }
  }, [category, modelArea, ownerId, status]);

  useEffect(() => { void load(); }, [load]);

  async function toggleArtifact(issueId: string) {
    const requestId = ++artifactRequest.current;
    if (selectedId === issueId) {
      setSelectedId("");
      setArtifact(null);
      setArtifactFailure("");
      return;
    }
    setSelectedId(issueId);
    setArtifact(null);
    setArtifactFailure("");
    setArtifactLoading(true);
    try {
      const result = await fetchAnalysisIssueModelArtifact(issueId);
      if (artifactRequest.current === requestId) setArtifact(result);
    } catch (cause) {
      if (artifactRequest.current === requestId) {
        setArtifactFailure(toUserMessage(cause, "模型异常回答加载失败，请重试"));
      }
    } finally {
      if (artifactRequest.current === requestId) setArtifactLoading(false);
    }
  }

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
          模型功能
          <select value={modelArea} onChange={(event) => setModelArea(event.target.value as typeof modelArea)}>
            <option value="">全部功能</option>
            {Object.entries(MODEL_AREA_LABELS).map(([value, label]) => (
              <option value={value} key={value}>{label}</option>
            ))}
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
            <option value="model_output">模型回答格式</option>
          </select>
        </label>
        <button type="button" className="usage-role-button" disabled={loading} onClick={() => void load()}>
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>
      <p className="usage-analysis-note">
        本页只读。失败文件，以及所有功能中未通过 JSON 协议校验的模型请求与回答，由系统私有保存并在保留期到期后清除；管理员不能在此重试、删除或改动用户内容。
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
              <Fragment key={item.id}>
              <tr>
                <td><span className={`usage-issue-status usage-issue-status-${item.status}`}>{item.status === "open" ? "未解决" : "已解决"}</span></td>
                <td>{item.owner_id === "system"
                  ? "系统任务"
                  : (usernames[item.owner_id] || "已删除用户")}</td>
                <td>{item.category === "model_output"
                  ? `${MODEL_AREA_LABELS[item.model_area as ModelAnalysisArea] || "模型调用"} · ${item.workload_label || item.workload_id || "未分类"}`
                  : categoryLabel(item.category)}</td>
                <td><strong>{item.code}</strong><br /><span>{item.summary}</span></td>
                <td>{item.category === "model_output"
                  ? (item.notebook_name || item.notebook_id || "未关联笔记本")
                  : (item.source_deleted ? "来源已删除（已脱敏）" : (item.source_title || item.file_name || "未命名来源"))}</td>
                <td>{item.updated_at.replace("T", " ").slice(0, 16)}</td>
                <td>{item.expires_at.replace("T", " ").slice(0, 16)}</td>
                <td>
                  {item.category === "model_output" && item.artifact_available
                    ? <button
                        type="button"
                        className="usage-role-button"
                        disabled={artifactLoading && selectedId === item.id}
                        aria-expanded={selectedId === item.id}
                        onClick={() => void toggleArtifact(item.id)}
                      >
                        {artifactLoading && selectedId === item.id
                          ? "加载中…"
                          : (selectedId === item.id ? "收起记录" : "查看提问与回答")}
                      </button>
                    : item.notebook_id && item.source_id && !item.source_deleted && !item.notebook_deleted
                    ? <a href={`/dev/logs?view=activity&owner=${encodeURIComponent(item.owner_id)}&activity_type=source&notebook_id=${encodeURIComponent(item.notebook_id)}&source_id=${encodeURIComponent(item.source_id)}`}>查看来源详情</a>
                    : <span>仅保留脱敏摘要</span>}
                </td>
              </tr>
              {item.category === "model_output" && selectedId === item.id && (
                <tr className="usage-subrow">
                  <td colSpan={8}>
                    {artifactLoading && <p className="usage-subtable-status">正在加载完整记录…</p>}
                    {artifactFailure && <p className="usage-subtable-status usage-subtable-error" role="alert">{artifactFailure}</p>}
                    {artifact && (
                      <div className="usage-model-artifact">
                        <p><strong>提问或主要模型输入</strong></p>
                        <pre>{artifact.question}</pre>
                        <p><strong>模型原始回答</strong></p>
                        <pre>{artifact.response}</pre>
                        <details>
                          <summary>查看完整模型请求与 JSON 契约</summary>
                          {artifact.messages.map((message, index) => (
                            <div key={`${message.role}-${index}`}>
                              <p><strong>{message.role || "message"}</strong></p>
                              <pre>{message.content}</pre>
                            </div>
                          ))}
                          <p><strong>JSON 契约</strong></p>
                          <pre>{artifact.schema_hint}</pre>
                          <p className="usage-analysis-note">
                            功能：{MODEL_AREA_LABELS[artifact.model_area as ModelAnalysisArea] || "未分类"}；工作负载：{artifact.workload_label || artifact.workload_id || "未知"}；失败类型：{artifact.failure_kind || "未知"}；失败原因：{artifact.reason || "未知"}；支持编号：{artifact.support_id || "无"}
                          </p>
                        </details>
                      </div>
                    )}
                  </td>
                </tr>
              )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
