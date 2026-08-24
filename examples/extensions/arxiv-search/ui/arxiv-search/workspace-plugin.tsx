"use client";

/**
 * The browser half of the arXiv sample plugin: one `workspace.side_panel`
 * entry, `ArxivSearchEntry`. Its backend counterpart is
 * `silicon_notebook_arxiv_search.bundle.BUNDLE` — `id` / `plugin_id` /
 * `capability` / `slot` there and in `ui-plugin.json` must match character
 * for character (`test_ui_manifest_matches_the_backend_manifest`, T4).
 *
 * State ownership: everything the panel needs — query text, the running
 * catalog of results, the checkbox selection, the last import receipt — lives
 * in this one component's `useState`s. There is no core-owned data owner to
 * defer to here (unlike the built-in Agent Profile entry, whose click opens
 * a *core* modal through `actions.openUnderstanding()`): this plugin's modal
 * is its own `ExtensionModal`, so its content is necessarily this plugin's
 * own state. The entry button itself never unmounts (the outlet keeps
 * rendering it for as long as the contribution is available), so this state
 * persists across closing and reopening the dialog within one workspace visit
 * — a deliberate, harmless choice: nothing here is expensive to keep, and a
 * user who closes the panel mid-selection likely wants it back the way they
 * left it.
 *
 * Import allowed here: `../extension-sdk/contracts.ts` (types),
 * `../extension-sdk/ui.tsx` (`ExtensionModal`), `react`, `lucide-react`, and
 * the sibling `./search-panel-model.ts`. Never `../extension-sdk/api.ts` —
 * the port a plugin gets is `actions.api`, injected per-`pluginId` by the
 * host; importing the module directly would let this plugin mint a port
 * bound to *any* plugin id, which is exactly what the SDK's import
 * allow-list exists to prevent.
 */
import { useState, type FormEvent } from "react";
import { Search } from "lucide-react";

import type { WorkspaceExtensionProps } from "../extension-sdk/contracts.ts";
import { ExtensionModal } from "../extension-sdk/ui.tsx";
import {
  FIRST_PAGE_START,
  classifyImportReceipt,
  deselectPaper,
  foldImportedUrls,
  formatAuthors,
  mergeCatalog,
  nextPageStart,
  selectPaper,
  selectedImportUrls,
  type ArxivImportResponse,
  type ArxivSearchResponse,
  type ArxivSearchResultItem,
  type ImportReceiptEntry,
} from "./search-panel-model.ts";

// Module-level so re-renders that do not touch these never allocate a new
// empty collection — the same "stable empty reference" discipline the owner
// hooks use elsewhere in this codebase (an inline `new Set()`/`new Map()`
// default would be a fresh object every render).
const EMPTY_CATALOG: ReadonlyMap<string, ArxivSearchResultItem> = new Map();
const EMPTY_IDS: readonly string[] = [];
const EMPTY_SELECTION: ReadonlySet<string> = new Set();

export function ArxivSearchEntry({ context, actions }: WorkspaceExtensionProps) {
  const [query, setQuery] = useState("");
  const [start, setStart] = useState(FIRST_PAGE_START);
  const [hasMore, setHasMore] = useState(false);
  const [catalog, setCatalog] = useState(EMPTY_CATALOG);
  // The current page's papers, by id, in display order — kept separate from
  // `catalog` because the catalog accumulates across pages while the visible
  // list must not (a user paging forward should see the new page, not every
  // page they have ever seen appended together).
  const [visibleIds, setVisibleIds] = useState(EMPTY_IDS);
  const [selected, setSelected] = useState(EMPTY_SELECTION);
  const [searchBusy, setSearchBusy] = useState(false);
  const [searched, setSearched] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<ReadonlyMap<string, ImportReceiptEntry> | null>(null);
  // "URLs this panel has already seen come back as `created`" — see
  // `search-panel-model.ts`'s module docstring for why this, and not
  // anything the server says, is what "已复用" means here.
  const [alreadyImported, setAlreadyImported] = useState(EMPTY_SELECTION);

  async function runSearch(nextStart: number) {
    const trimmed = query.trim();
    if (trimmed.length === 0 || searchBusy) return;
    setSearchBusy(true);
    setSearchError(null);
    try {
      const response = await actions.api.requestJson<ArxivSearchResponse>(
        `/notebooks/${context.notebook.id}/search`,
        { query: { q: trimmed, start: nextStart } },
      );
      setCatalog((previous) => mergeCatalog(previous, response.items));
      setVisibleIds(response.items.map((item) => item.arxiv_id));
      setStart(response.start);
      setHasMore(response.has_more);
      setSearched(true);
    } catch (error) {
      // A failed page load must not silently keep showing a stale one.
      setVisibleIds(EMPTY_IDS);
      setHasMore(false);
      setSearchError(actions.api.userMessage(error, "arXiv 检索暂时不可用，请稍后再试"));
    } finally {
      setSearchBusy(false);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    // A fresh query starts a fresh session: an import receipt from a
    // previous query no longer describes anything on screen.
    setReceipt(null);
    setImportError(null);
    void runSearch(FIRST_PAGE_START);
  }

  function handleLoadMore() {
    if (!hasMore || searchBusy) return;
    void runSearch(nextPageStart(start, visibleIds.length));
  }

  function toggle(arxivId: string, checked: boolean) {
    setSelected((previous) => (checked ? selectPaper(previous, arxivId) : deselectPaper(previous, arxivId)));
  }

  async function handleImport() {
    const urls = selectedImportUrls(catalog, selected);
    if (urls.length === 0 || importBusy) return;
    setImportBusy(true);
    setImportError(null);
    try {
      const response = await actions.api.requestJson<ArxivImportResponse>(
        `/notebooks/${context.notebook.id}/import`,
        { method: "POST", body: JSON.stringify({ urls }) },
      );
      const outcome = classifyImportReceipt(response, alreadyImported);
      setReceipt(outcome);
      setAlreadyImported((previous) => foldImportedUrls(previous, outcome));
      setSelected(EMPTY_SELECTION);
      // Per the SDK contract this may reject (a genuine reload failure, as
      // opposed to a stale-owner no-op) — it must be caught, not left as an
      // unhandled rejection (contracts.ts::WorkspaceExtensionActions.refreshSources).
      await actions.refreshSources().catch(() => {});
    } catch (error) {
      setImportError(actions.api.userMessage(error, "未能导入所选文献"));
    } finally {
      setImportBusy(false);
    }
  }

  const visibleItems: ArxivSearchResultItem[] = [];
  for (const id of visibleIds) {
    const item = catalog.get(id);
    if (item !== undefined) visibleItems.push(item);
  }
  const noResults = searched && !searchBusy && !searchError && visibleItems.length === 0;

  return (
    <>
      <button
        type="button"
        className="button secondary workspace-extension-entry"
        aria-label="arXiv 文献检索（样板）"
        title="按关键词检索 arXiv 论文，选择后导入这个笔记本"
        onClick={() => actions.openDialog()}
      >
        <Search size={15} aria-hidden="true" />
        <span>arXiv 文献检索</span>
      </button>
      <ExtensionModal
        context={context}
        actions={actions}
        storageKey="search"
        title="arXiv 文献检索（样板）"
        description="按关键词检索 arXiv 论文，勾选后导入这个笔记本。"
      >
        <form onSubmit={handleSubmit}>
          <input
            type="text"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="检索关键词，例如 diffusion model"
            aria-label="arXiv 检索关键词"
          />
          <button type="submit" className="button" disabled={searchBusy || query.trim().length === 0}>
            {searchBusy ? "检索中…" : "检索"}
          </button>
        </form>
        {searchError && <p role="alert">{searchError}</p>}
        {noResults && <p>没有找到相关文献，换个关键词试试。</p>}
        {visibleItems.length > 0 && (
          <ul>
            {visibleItems.map((item) => (
              <li key={item.arxiv_id}>
                <label>
                  <input
                    type="checkbox"
                    checked={selected.has(item.arxiv_id)}
                    onChange={(event) => toggle(item.arxiv_id, event.target.checked)}
                  />
                  <strong>{item.title}</strong>
                </label>
                <p>
                  {formatAuthors(item.authors)}
                  {item.published ? ` · ${item.published}` : ""}
                </p>
                {item.summary && <p>{item.summary}</p>}
              </li>
            ))}
          </ul>
        )}
        {hasMore && (
          <button type="button" className="button secondary" disabled={searchBusy} onClick={handleLoadMore}>
            {searchBusy ? "加载中…" : "加载更多"}
          </button>
        )}
        <button type="button" className="button" disabled={importBusy || selected.size === 0} onClick={() => void handleImport()}>
          {importBusy ? "导入中…" : `导入所选（${selected.size}）`}
        </button>
        {importError && <p role="alert">{importError}</p>}
        {receipt && receipt.size > 0 && (
          <ul>
            {[...receipt.entries()].map(([url, entry]) => (
              <li key={url}>
                {entry.status === "created" && <span>已创建：{entry.title}</span>}
                {entry.status === "reused" && <span>已复用：{entry.title}</span>}
                {entry.status === "rejected" && <span>未导入（{entry.reason}）：{url}</span>}
              </li>
            ))}
          </ul>
        )}
      </ExtensionModal>
    </>
  );
}
