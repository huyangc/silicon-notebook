"use client";
// 「活动」视图左栏 · 范围：该用户的笔记本清单，展开一个笔记本给出它的来源清单。
// 纯展示组件——取数、竞态与选中态都在 ActivityView 里。

import { ChevronDown, ChevronRight } from "lucide-react";

import type { AdminUserNotebook } from "../../../admin/usage/notebooks.ts";
import type { SourceSummary } from "../../../workspace-model.ts";
import { activityStatusLabel, activityTitle, activityTone } from "./format.ts";
import { SourceAnomalies, toActivitySource } from "./source-view.tsx";
import type { ActivitySource } from "./types";

export type SourceListState = {
  items: SourceSummary[];
  total: number;
  loading: boolean;
  /** 已经过 toUserMessage 的人话文案（不是后端诊断原文），字段名刻意避开 `error`。 */
  failure: string;
};

export const ALL_NOTEBOOKS = "";

export function ActivityScopePanel({
  notebooks,
  loading,
  failure,
  identityErrored,
  onRetryNotebooks,
  notebookId,
  allNotebooksLabel = "全部笔记本",
  onSelectNotebook,
  expanded,
  onToggleExpand,
  sources,
  selectedKey,
  onSelectSource,
}: {
  notebooks: AdminUserNotebook[];
  loading: boolean;
  /** 笔记本清单**自己**的失败态（已过 toUserMessage）。与「取回来了、但是空的」
   *  必须分开渲染：一次 500 之后说「这位用户还没有建过笔记本」是一句假陈述，而
   *  它偏偏是管理员据以下结论的那一句。 */
  failure: string;
  /** 用户身份本身取不到（fetchMe 失败，`userId` 因此解析不出）——与「加载中」
   *  「确定了、就是空」都不同的第三态：既不能说没有笔记本，也不算失败态自己的
   *  独立错误横幅（错误已经在页面顶部显示），单纯不下这个「空」的结论。 */
  identityErrored?: boolean;
  onRetryNotebooks: () => void;
  notebookId: string;
  /** 提问概览的全局范围还包含该用户在共享笔记本里的提交。 */
  allNotebooksLabel?: string;
  onSelectNotebook: (id: string) => void;
  expanded: string[];
  onToggleExpand: (id: string) => void;
  sources: Record<string, SourceListState>;
  selectedKey: string;
  /** ⚠ 只交出 `toActivitySource()` 折好的条目，**不再**附带服务端算好的
   *  `created_label`：右栏的时间一律由 `created_at` 按浏览器本地时区渲染，
   *  多一个覆盖口就是多一种时间格式（见 source-view.tsx 顶部说明）。 */
  onSelectSource: (source: ActivitySource) => void;
}) {
  return (
    <div className="activity-scope">
      <div className="activity-col-head">范围</div>
      <div className="activity-scope-list">
        <button
          className={`activity-nb-row${notebookId === ALL_NOTEBOOKS ? " selected" : ""}`}
          onClick={() => onSelectNotebook(ALL_NOTEBOOKS)}
          type="button"
        >
          <span className="activity-nb-name">{allNotebooksLabel}</span>
        </button>
        {loading && notebooks.length === 0 ? <div className="empty">加载中…</div> : null}
        {!loading && failure ? (
          <div className="activity-scope-failure">
            <span>{failure}</span>
            <button className="copy-btn" onClick={onRetryNotebooks} type="button">
              重试
            </button>
          </div>
        ) : null}
        {!loading && !failure && !identityErrored && notebooks.length === 0 ? (
          <div className="empty">这位用户还没有建过笔记本</div>
        ) : null}
        {notebooks.map((notebook) => {
          const open = expanded.includes(notebook.id);
          const list = sources[notebook.id];
          return (
            <div className="activity-nb" key={notebook.id}>
              <div className="activity-nb-head">
                <button
                  aria-expanded={open}
                  aria-label={`${open ? "收起" : "展开"}《${notebook.name}》的来源`}
                  className="activity-nb-toggle"
                  onClick={() => onToggleExpand(notebook.id)}
                  type="button"
                >
                  {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>
                <button
                  className={`activity-nb-row${notebookId === notebook.id ? " selected" : ""}`}
                  onClick={() => onSelectNotebook(notebook.id)}
                  title={notebook.name}
                  type="button"
                >
                  <span className="activity-nb-name">{notebook.name}</span>
                  {/* 计数只用界面词。问答用量取 questions——conversations 是仅为兼容
                      保留的会话容器数，`docs/product-and-api.md` 禁止拿它冒充提问次数。 */}
                  <span className="activity-nb-counts">
                    来源 {notebook.sources} · 提问 {notebook.questions} · 报告 {notebook.reports}
                  </span>
                </button>
              </div>
              {open ? (
                <div className="activity-src-list">
                  {list?.loading ? <div className="empty">加载中…</div> : null}
                  {list?.failure ? (
                    <div className="activity-src-error">{list.failure}</div>
                  ) : null}
                  {list && !list.loading && !list.failure && list.items.length === 0 ? (
                    <div className="empty">这个笔记本还没有来源</div>
                  ) : null}
                  {(list?.items ?? []).map((source) => {
                    const item = toActivitySource(source, notebook.id);
                    const name = activityTitle(item);
                    return (
                      <div className="activity-src" key={source.id}>
                        <button
                          className={`activity-src-row${
                            selectedKey === `source:${source.id}` ? " selected" : ""
                          }`}
                          onClick={() => onSelectSource(item)}
                          title={name}
                          type="button"
                        >
                          <span className="activity-src-name">{name}</span>
                          <span className={`badge ${activityTone(item)}`}>
                            {activityStatusLabel(item)}
                          </span>
                        </button>
                        <SourceAnomalies source={item} />
                      </div>
                    );
                  })}
                  {list && list.items.length < list.total ? (
                    <div className="activity-src-more">
                      已显示 {list.items.length} / {list.total} 个来源
                    </div>
                  ) : null}
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </div>
  );
}
