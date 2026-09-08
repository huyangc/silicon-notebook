"use client";
import { ExternalLink, FileText, Loader2, Search, Trash2 } from "lucide-react";

import { AnomalyBadge } from "./anomaly-badge";
import { sourceAnomalies } from "./anomaly-severity";
import { Pagination } from "./Pagination";
import { sourceKgBadge } from "./source-kg-badge.ts";
import { sourceIsSelected, type SourceScopeSelection } from "./source-scope";
import { compactSourceTitle } from "./source-title.ts";
import { isAdvanced, type UiMode } from "./ui-mode.ts";
import { SOURCES_PAGE_SIZE, type SourceSummary } from "./workspace-model.ts";

/**
 * 来源栏里「本库来源」那一段：标题 + 搜索表单 + 滚动的来源列表 + 分页。
 *
 * 返回 Fragment 而不是包一层 wrapper —— 两个 div 必须仍是 `.sources-body` 的**直接
 * 子节点**：`.source-list` 的滚动算术（`.sources-body` 上的 flex:1 1 auto /
 * min-height:0 / overflow:auto）与 workspace.side_panel 扩展点「排在滚动列表之前的
 * 固定区」那条不变量都靠这层直接父子关系。
 *
 * 状态与在途请求纪律（AbortController / owner 世代守卫 / sourcesPageLoading 的开合）
 * 全部留在 use-source-library.ts；这里只呈现，命令一律经显式回调交回工作区编排层。
 */
export function SourceListPanel({
  sources,
  uiMode,
  notebookId,
  sourceQuery,
  sourcesPage,
  sourcesTotal,
  sourcesPageLoading,
  sourceScopeSelection,
  deletingSourceIds,
  askInFlight,
  readOnlyWorkspace,
  kgReady,
  onQueryChange,
  onSubmitSearch,
  onToggleSource,
  onOpenSource,
  onDeleteSource,
  onPage,
}: {
  sources: SourceSummary[];
  uiMode: UiMode;
  notebookId: string | null;
  sourceQuery: string;
  sourcesPage: number;
  sourcesTotal: number;
  sourcesPageLoading: boolean;
  sourceScopeSelection: SourceScopeSelection;
  deletingSourceIds: ReadonlySet<string>;
  askInFlight: boolean;
  readOnlyWorkspace: boolean;
  kgReady: boolean;
  onQueryChange: (value: string) => void;
  onSubmitSearch: () => void;
  onToggleSource: (sourceId: string) => void;
  onOpenSource: (source: SourceSummary) => void;
  onDeleteSource: (source: SourceSummary) => void;
  onPage: (page: number) => void;
}) {
  return (
    <>
      {/* 标题 + 搜索框成组，但**不**把 .source-list 包进来：那个列表靠
          .sources-body 上的 flex:1 1 auto / min-height:0 / overflow:auto 拿到
          剩余高度并自己滚动，套一层就得把这套算术原样复制一遍。列表用
          aria-labelledby 挂回标题，分组语义不丢。
          搜索框是从整个面板最顶上挪下来的：它过去悬在参考库之上，让人误以为
          能一并搜到参考库里的内容，而它只查当前笔记本。 */}
      <div className="scope-group">
        <h3 className="scope-group-title" id="local-source-scope-title">本库来源</h3>
        {/* 整个表单是一只带边框的搜索框：左侧放大镜、中间无边框输入、右侧「搜索」
            做成与上方「全选/清空」同款的蓝色文字按钮。之前输入框和按钮各自裸用
            浏览器默认外观（深色内嵌边框 + 灰色方块按钮），与侧栏其余控件（圆角
            细边框、蓝色文字动作、行内图标按钮）明显不是一套。
            在途反馈仍落在控件自身：放大镜换成转圈，按钮文案变「搜索中…」并禁用。 */}
        <form className="source-search-form" role="search" onSubmit={(event) => {
          event.preventDefault();
          onSubmitSearch();
        }}>
          {sourcesPageLoading
            ? <Loader2 size={15} className="busy-spin source-search-icon" aria-hidden="true" />
            : <Search size={15} className="source-search-icon" aria-hidden="true" />}
          <input
            className="source-search"
            type="search"
            placeholder="搜索标题/作者/文件名"
            aria-label="搜索本库来源"
            value={sourceQuery}
            onChange={(e) => onQueryChange(e.target.value)}
          />
          <button type="submit" className="source-search-submit" disabled={!notebookId || sourcesPageLoading}>
            {sourcesPageLoading ? "搜索中…" : "搜索"}
          </button>
        </form>
      </div>
      {/* role="group" 是 aria-labelledby 的生效条件：挂在无 role 的通用 div
          上，辅助技术基本会忽略这条标注，分组语义等于没接。它不影响布局
          （这个 div 的滚动算术在 .sources-body 上，见上面那段注释）。 */}
      <div className="source-list" role="group" aria-labelledby="local-source-scope-title">
        {sources.length === 0 ? (
          <article className="source-empty">
            <div>▧</div>
            <strong>已保存的来源将显示在此处</strong>
            <p>点击上方的“添加来源”导入 PDF、Markdown、DOCX 或 PPTX。</p>
          </article>
        ) : (
          sources.map((source) => {
            const deletingSource = deletingSourceIds.has(source.id);
            const kgBadge = sourceKgBadge(source);
            return (
            <div
              key={source.id}
              className={`source-row compact-source-row${isAdvanced(uiMode) ? "" : " source-row--no-select"}${deletingSource ? " source-row--deleting" : ""}`}
              title={source.title}
              aria-busy={deletingSource || undefined}
            >
              {isAdvanced(uiMode) && (
                <label
                  className="source-scope-check"
                  title={sourceIsSelected(sourceScopeSelection, source.id)
                    ? "此来源会参与问答与深度报告检索"
                    : "此来源不会参与问答与深度报告检索"}
                >
                  <input
                    type="checkbox"
                    checked={sourceIsSelected(sourceScopeSelection, source.id)}
                    disabled={deletingSource || askInFlight}
                    aria-label={`检索来源：${source.title}`}
                    onChange={() => onToggleSource(source.id)}
                  />
                </label>
              )}
              <button
                className="source-row-main"
                disabled={deletingSource}
                onClick={() => onOpenSource(source)}
              >
                <FileText className="source-file-icon" size={20} />
                <span className="source-title-short">{compactSourceTitle(source)}</span>
                <span className="source-row-status">
                  <span className={`source-status-dot status-${source.parse_status || source.status}`} />
                  {sourceAnomalies(source).filter((a) => a.severity !== "info").map((anomaly, i) => (
                    <AnomalyBadge key={`${anomaly.severity}-${i}`} anomaly={anomaly} />
                  ))}
                </span>
              </button>
              <div className="source-row-actions">
                {kgReady && (
                  <span
                    className={kgBadge.className}
                    title={kgBadge.title}
                  >
                    {kgBadge.label}
                  </span>
                )}
                {source.agent_created && (
                  <span
                    className="source-agent-badge"
                    title="由 Agent 通过接入通道添加的来源"
                  >
                    Agent 添加
                  </span>
                )}
                {source.source_url ? (
                  <a
                    className="source-link-button"
                    href={source.source_url}
                    target="_blank"
                    rel="noreferrer"
                    title={source.source_url}
                    aria-label="打开原始链接"
                    aria-disabled={deletingSource || undefined}
                    tabIndex={deletingSource ? -1 : undefined}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (deletingSource) e.preventDefault();
                    }}
                  >
                    <ExternalLink size={13} />
                  </a>
                ) : null}
                {!readOnlyWorkspace && (
                  <button
                    className="source-delete-button"
                    disabled={deletingSource}
                    title="删除来源"
                    aria-label={deletingSource ? `正在删除来源：${source.title}` : `删除来源：${source.title}`}
                    onClick={() => onDeleteSource(source)}
                  >
                    {deletingSource
                      ? <Loader2 size={15} className="busy-spin" aria-hidden="true" />
                      : <Trash2 size={15} />}
                  </button>
                )}
              </div>
            </div>
            );
          })
        )}
        <Pagination
          page={sourcesPage}
          pageSize={SOURCES_PAGE_SIZE}
          total={sourcesTotal}
          busy={sourcesPageLoading}
          onPage={onPage}
        />
      </div>
    </>
  );
}
