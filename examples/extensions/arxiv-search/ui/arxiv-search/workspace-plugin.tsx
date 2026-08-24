"use client";

/**
 * The browser half of the arXiv sample plugin: one `workspace.side_panel`
 * entry, `ArxivSearchEntry`. Its backend counterpart is
 * `silicon_notebook_arxiv_search.bundle.BUNDLE` — `id` / `plugin_id` /
 * `capability` / `slot` there and in `ui-plugin.json` must match character
 * for character (`test_ui_manifest_matches_the_backend_manifest`, T4).
 *
 * **Why the manifest declares `notebook:write` and not `source:write`.** The
 * permission name is resolved against a snapshot the host builds per slot
 * (`features/extension-sdk/visibility.ts::permissionAllowed`). In the
 * `workspace.side_panel` outlet the shell hard-codes `sourceRead: false` and
 * `sourceWrite: false` (`app/page.tsx`'s `workspaceExtensionPermissions` —
 * those two describe a *selected source*, and no source is selected out
 * here), so a contribution declaring `source:write` is structurally
 * unreachable: the entry button never renders, in any deployment, for any
 * user. `notebook:write` maps to `snapshot.notebookWrite` →
 * `capabilities.canWriteNotebook`, which is the browser-side mirror of the
 * backend `sources:write` capability this plugin's `/import` route actually
 * needs. Do not "correct" this back to `source:write`; the guard for it is
 * `frontend/tests/unit/arxiv-sample-ui-package.test.mjs`, which pins the
 * manifest field literally.
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
  MAX_IMPORT_URLS,
  MAX_QUERY_TERMS,
  classifyImportReceipt,
  countQueryTerms,
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
  // The query the visible page was actually fetched with, frozen at the
  // moment it was submitted. "Load more" pages forward on *this*, never on
  // the live input box — see `handleLoadMore`.
  const [executedQuery, setExecutedQuery] = useState("");
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

  // `term` is passed in rather than read from `query` so that both callers are
  // explicit about which query they mean: the submit handler means "whatever
  // is in the box now", the pager means "the one that produced what is on
  // screen". Reading state here would silently give the pager the former.
  // `importBusy` also blocks: an import in flight ends by clearing `selected`,
  // and letting a new page land underneath it would clear a selection the user
  // made against results that no longer exist.
  async function runSearch(term: string, nextStart: number) {
    // The submit button is already disabled past the cap (see
    // `overQueryTermLimit` below); this re-check is the defensive half, the
    // same shape `handleImport` uses for `MAX_IMPORT_URLS` — so a stray
    // programmatic call cannot spend a round trip on a query the plugin's
    // own route will now answer with a 400 instead of silently truncating.
    if (
      term.length === 0 ||
      searchBusy ||
      importBusy ||
      countQueryTerms(term) > MAX_QUERY_TERMS
    ) {
      return;
    }
    setSearchBusy(true);
    setSearchError(null);
    try {
      const response = await actions.api.requestJson<ArxivSearchResponse>(
        `/notebooks/${context.notebook.id}/search`,
        { query: { q: term, start: nextStart } },
      );
      setCatalog((previous) => mergeCatalog(previous, response.items));
      setVisibleIds(response.items.map((item) => item.arxiv_id));
      setStart(response.start);
      setHasMore(response.has_more);
      setExecutedQuery(term);
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
    // A fresh query starts a fresh session, and that has to mean all three
    // of these, not just the receipt: the selection and the catalog both
    // describe the previous query's results. Keeping `selected` would let a
    // paper the user can no longer see ride along on the next import, and
    // keeping `catalog` is precisely the mechanism that would still resolve
    // its id to a URL. `alreadyImported` deliberately survives — it is the
    // panel's session-long duplicate warning, not per-query state.
    setReceipt(null);
    setImportError(null);
    setSelected(EMPTY_SELECTION);
    setCatalog(EMPTY_CATALOG);
    void runSearch(query.trim(), FIRST_PAGE_START);
  }

  function handleLoadMore() {
    if (!hasMore || searchBusy || importBusy) return;
    // The frozen query, not the input box: typing a new term without
    // submitting must not make "load more" fetch that term's second page and
    // append it under the previous term's results.
    void runSearch(executedQuery, nextPageStart(start, visibleIds.length));
  }

  function toggle(arxivId: string, checked: boolean) {
    setSelected((previous) => (checked ? selectPaper(previous, arxivId) : deselectPaper(previous, arxivId)));
  }

  async function handleImport() {
    const urls = selectedImportUrls(catalog, selected);
    // The button is already disabled past the cap; this re-check is the
    // defensive half, so a stray programmatic call cannot spend a round trip
    // on a batch the plugin's own route will answer with a 400.
    if (urls.length === 0 || urls.length > MAX_IMPORT_URLS || importBusy) return;
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
  const overImportLimit = selected.size > MAX_IMPORT_URLS;
  const overQueryTermLimit = countQueryTerms(query) > MAX_QUERY_TERMS;

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
          <button
            type="submit"
            className="button"
            disabled={
              searchBusy || importBusy || query.trim().length === 0 || overQueryTermLimit
            }
          >
            {searchBusy ? "检索中…" : "检索"}
          </button>
        </form>
        {overQueryTermLimit && <p>检索词最多 {MAX_QUERY_TERMS} 个，请精简后重试。</p>}
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
        <button
          type="button"
          className="button"
          disabled={importBusy || selected.size === 0 || overImportLimit}
          onClick={() => void handleImport()}
        >
          {importBusy ? "导入中…" : `导入所选（${selected.size}）`}
        </button>
        {overImportLimit && <p>一次最多导入 {MAX_IMPORT_URLS} 篇，请先取消部分勾选。</p>}
        {importError && <p role="alert">{importError}</p>}
        {receipt && receipt.size === 0 && (
          // A non-null but empty receipt means the request succeeded and
          // accounted for nothing at all. Silently resetting would read as
          // "done" — say so instead and point at where the truth is.
          <p role="status">本次导入没有收到任何结果，请到来源列表确认。</p>
        )}
        {receipt && receipt.size > 0 && (
          <ul>
            {[...receipt.entries()].map(([url, entry]) => (
              <li key={url}>
                {entry.status === "created" && <span>已创建：{entry.title}</span>}
                {entry.status === "repeat" && (
                  <span>本次已导入过，可能已产生重复来源：{entry.title}</span>
                )}
                {entry.status === "rejected" && <span>未导入（{entry.reason}）：{url}</span>}
              </li>
            ))}
          </ul>
        )}
      </ExtensionModal>
    </>
  );
}
