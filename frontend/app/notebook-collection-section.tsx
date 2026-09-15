import { type MouseEvent, type ReactNode } from "react";
import { User } from "lucide-react";

import { grantedViaLabel, isGroupGranted } from "./group-api";
import { notebookRoleText } from "./workspace-transitions";
import { Pagination } from "./Pagination";
import { useClientPagination, type PaginationResetKey } from "./use-client-pagination.ts";
import type { NotebookSummary, SearchHit } from "./workspace-model";

/** 笔记本卡片/列表行每页的条数。接口一次性返回整份笔记本清单,分页只发生在界面
 * (集合页「我的笔记本」「群组」两个分区各自独立分页)。 */
const NOTEBOOK_COLLECTION_PAGE_SIZE = 24;

function cardTone(index: number): string {
  return ["tone-green", "tone-cream", "tone-lavender", "tone-rose", "tone-cream", "tone-blue"][index % 6];
}

function cardIcon(index: number, notebook: NotebookSummary): string {
  if (notebook.primary_domain.toLowerCase().includes("esd")) return "▣";
  return ["◇", "📒", "📈", "▤", "▧"][index % 5];
}

function SearchHits({ hits, compact }: { hits: SearchHit[]; compact: boolean }) {
  if (!hits.length) return null;
  if (compact) {
    const hit = hits[0];
    return <small>{hit.scope} · {hit.text}</small>;
  }
  return (
    <div className="card-search-hits">
      {hits.slice(0, 3).map((hit, index) => (
        <div key={`${hit.scope}-${index}`}>
          <span>{hit.scope}</span>
          <p>{hit.text}</p>
        </div>
      ))}
    </div>
  );
}

function NotebookList({
  entries,
  roleText,
  openingNotebookId,
  openNotebook,
  openMemory,
  openMenu
}: {
  entries: Array<{ notebook: NotebookSummary; index: number; hits: SearchHit[] }>;
  /** 覆盖「角色」列的文案。「群组」分区传「群组成员」;省略时按每行的 access 判。 */
  roleText?: string;
  /**
   * 正在打开的笔记本 id。列表视图与网格卡片是同一个动作的两种外观,忙碌反馈必须
   * 同权:命中行的四个「打开」单元格立刻禁用并显示旋转指示,否则用户在列表里连点
   * 依旧会叠出多组并行请求(网格那边已经挡住了,这边没挡就是同一个缺陷的另一半)。
   */
  openingNotebookId: string | null;
  openNotebook: (id: string) => void;
  openMemory: (id: string) => void;
  openMenu: (id: string, event: MouseEvent<HTMLButtonElement>) => void;
}) {
  // 全部库都落在「群组」分区时,主区一行都没有——只剩一排孤零零的表头,读起来像
  // 「这里本该有东西但没加载出来」。没有行就整段不渲染。
  if (entries.length === 0) return null;
  return (
    <section className="notebook-list">
      <div className="notebook-list-header">
        <span>标题</span><span>来源</span><span>记忆</span><span>创建日期</span><span>角色</span><span />
      </div>
      {entries.map(({ notebook, index, hits }) => {
        const opening = openingNotebookId === notebook.id;
        return (
        <article className="notebook-list-row" key={notebook.id}>
          <button
            className={`notebook-list-title${opening ? " is-opening" : ""}`}
            aria-busy={opening || undefined}
            disabled={opening}
            onClick={() => openNotebook(notebook.id)}
          >
            <span className="list-icon">{cardIcon(index, notebook)}</span>
            <span>
              <strong>{notebook.name}</strong>
              {opening && (
                <small className="notebook-list-open-status">
                  <span className="notebook-card-open-spinner" aria-hidden="true" />
                  打开中…
                </small>
              )}
              {isGroupGranted(notebook) && <small>{grantedViaLabel(notebook)}</small>}
              <SearchHits hits={hits} compact />
            </span>
          </button>
          {/* 三个数据格与标题同一个动作(打开这本库),所以同禁用。⋮ 菜单与「N 条记忆」
              是另外的动作,不禁用。 */}
          <button className="notebook-list-cell" disabled={opening} onClick={() => openNotebook(notebook.id)}>{notebook.counts.sources ?? 0} 个来源</button>
          <button className="notebook-list-cell notebook-memory-link" onClick={() => openMemory(notebook.id)}>{notebook.counts.memories ?? 0} 条</button>
          <button className="notebook-list-cell" disabled={opening} onClick={() => openNotebook(notebook.id)}>{notebook.created_label}</button>
          <button className="notebook-list-cell role-cell" disabled={opening} onClick={() => openNotebook(notebook.id)}>{notebookRoleText(notebook, roleText)}</button>
          <button className="list-row-menu" onClick={(event) => openMenu(notebook.id, event)} title="笔记本操作">⋮</button>
        </article>
        );
      })}
    </section>
  );
}

/**
 * 笔记本集合页一个分区(「我的笔记本」或「群组」)的卡片/列表渲染 + 分页。两个分区共用
 * 同一种卡片(复制一份 JSX 必然分叉),各自一份页码状态,互不影响对方翻到的页。
 */
export function NotebookCollectionSection({
  entries,
  viewMode,
  roleText,
  label,
  leadingCard,
  trailingGridItem,
  openingNotebookId,
  openNotebook,
  openMemory,
  openMenu,
  resetKey,
}: {
  entries: Array<{ notebook: NotebookSummary; index: number; hits: SearchHit[] }>;
  viewMode: string;
  /** 覆盖列表视图「角色」列文案,原样透传给 NotebookList。 */
  roleText?: string;
  label: string;
  /** 网格视图里排在卡片最前面的「新建笔记本」按钮;列表视图不渲染它。 */
  leadingCard?: ReactNode;
  /** 空态提示,渲染在 .notebook-grid 内部(与分页前的原位置一致,占一个 grid 单元)。 */
  trailingGridItem?: ReactNode;
  openingNotebookId: string | null;
  openNotebook: (id: string) => void;
  openMemory: (id: string) => void;
  openMenu: (id: string, event: MouseEvent<HTMLButtonElement>) => void;
  resetKey?: PaginationResetKey;
}) {
  const page = useClientPagination(entries, NOTEBOOK_COLLECTION_PAGE_SIZE, resetKey);
  return (
    <>
      <section className={`notebook-grid view-${viewMode}`}>
        {viewMode === "list" ? (
          <NotebookList
            entries={page.pageItems}
            roleText={roleText}
            openingNotebookId={openingNotebookId}
            openNotebook={openNotebook}
            openMemory={openMemory}
            openMenu={openMenu}
          />
        ) : (
          <>
            {leadingCard}
            {page.pageItems.map(({ notebook, hits }, i) => {
              // 翻页不改变每张卡片的色调/图标:用绝对位置(当前页起点 + 页内偏移)而不是
              // 页内下标,否则第二页会从头复刷一遍第一页用过的色调循环。
              const index = page.page * NOTEBOOK_COLLECTION_PAGE_SIZE + i;
              return (
                <article key={notebook.id} className={`notebook-card ${cardTone(index)}`}>
                  <button className="card-menu" onClick={(event) => openMenu(notebook.id, event)} title="笔记本操作">⋮</button>
                  {/* 动作发出后立即禁用 + 忙碌指示(docs/development.md「长任务控件」硬约束)。
                      禁用后基线 :active 反馈按设计不再适用——spinner + 「打开中…」就是反馈。
                      ⋮ 菜单与「N 条记忆」是另外的动作,不在这颗按钮里,照旧可点。 */}
                  <button
                    className={`notebook-card-main${openingNotebookId === notebook.id ? " is-opening" : ""}`}
                    aria-busy={openingNotebookId === notebook.id || undefined}
                    disabled={openingNotebookId === notebook.id}
                    onClick={() => openNotebook(notebook.id)}
                  >
                    <div className="card-icon">{cardIcon(index, notebook)}</div>
                    <div>
                      <h2>{notebook.name}</h2>
                      <p>{notebook.purpose || "No purpose set yet."}</p>
                      {openingNotebookId === notebook.id && (
                        <p className="notebook-card-open-status">
                          <span className="notebook-card-open-spinner" aria-hidden="true" />
                          打开中…
                        </p>
                      )}
                      {isGroupGranted(notebook) && (
                        <p className="notebook-card-meta">{grantedViaLabel(notebook)}</p>
                      )}
                    </div>
                    <SearchHits hits={hits} compact={false} />
                  </button>
                  <div className="notebook-card-footer">
                    <p className="notebook-card-meta">{notebook.created_label} · {notebook.counts.sources ?? 0} 个来源</p>
                    <div className="notebook-card-footer-actions">
                      {notebook.access !== "reader" && notebook.is_shared && (
                        <span className="notebook-shared-badge" title="已分享" aria-label="已分享"><User size={14} /></span>
                      )}
                      <button
                        type="button"
                        className="notebook-memory-link"
                        onClick={() => openMemory(notebook.id)}
                      >{notebook.counts.memories ?? 0} 条记忆</button>
                    </div>
                  </div>
                </article>
              );
            })}
          </>
        )}
        {trailingGridItem}
      </section>
      <Pagination page={page.page} pageSize={NOTEBOOK_COLLECTION_PAGE_SIZE} total={page.total} onPage={page.setPage} label={label} />
    </>
  );
}
