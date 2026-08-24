/**
 * Pure state-transition logic for the arXiv search panel — no React, no DOM,
 * no network. Everything the panel does with its own local state (checkbox
 * selection, pagination, author formatting, and turning an import response
 * into a per-item receipt) is a function here, so it can be exercised by
 * `node --test` without a browser or a mounted component. The `.tsx` entry
 * only wires these to `useState` and to the two HTTP calls.
 *
 * **What the third receipt state actually means — and what it must not
 * claim.** Core's URL import path (`source_ingestion.py::add_url_sources`)
 * does **not** de-duplicate by content: every URL that passes the PDF probe
 * gets an unconditional `insert_source` with an empty `file_hash`, followed
 * by a full parse. Importing the same PDF URL twice therefore creates two
 * separate sources and parses both. (Content-hash de-duplication does exist
 * in this product, but on the browser *upload* path, keyed by
 * `(notebook_id, file_hash)`; the URL importer never reaches it.) The
 * plugin's own `/import` route (`routes.py::import_papers`) forwards
 * straight to that path through core's `PluginUrlSourceImportPort`, and its
 * `created` rows (`PluginImportedSource{source_id, title, url}`) are always
 * brand-new rows.
 *
 * So this panel cannot report "core reused an existing source" — that event
 * does not occur. What it *can* report honestly is its own memory: "this
 * panel already sent this exact URL earlier in this session, so a duplicate
 * source has probably just been created." That is the `"repeat"` state, and
 * it is a **warning**, not a reassurance; the earlier `"reused"` spelling
 * asserted a backend behaviour that does not exist. `foldImportedUrls` is
 * the one place that memory is written and `classifyImportReceipt` the one
 * place it is read.
 */

export type ArxivSearchResultItem = Readonly<{
  arxiv_id: string;
  title: string;
  authors: readonly string[];
  published: string;
  summary: string;
  pdf_url: string;
  abs_url: string;
}>;

export type ArxivSearchResponse = Readonly<{
  items: readonly ArxivSearchResultItem[];
  start: number;
  has_more: boolean;
}>;

export type ArxivImportCreatedRow = Readonly<{ source_id: string; title: string; url: string }>;
export type ArxivImportRejectedRow = Readonly<{ url: string; reason: string }>;
export type ArxivImportResponse = Readonly<{
  created: readonly ArxivImportCreatedRow[];
  rejected: readonly ArxivImportRejectedRow[];
}>;

export type ImportReceiptEntry = Readonly<
  | { status: "created"; title: string }
  | { status: "repeat"; title: string }
  | { status: "rejected"; reason: string }
>;

/** The `start` value a fresh query (as opposed to "load more") begins at. */
export const FIRST_PAGE_START = 0;

/**
 * The most URLs one import request may carry.
 *
 * The **authoritative** bound is the plugin's own server-side
 * `routes.py::MAX_IMPORT_URLS`, which rejects an over-long batch with a 400.
 * This constant exists so the panel can disable the button and say why,
 * rather than letting someone tick thirty papers and learn the limit from an
 * error banner.
 *
 * The number is spelled twice, once per language, and drift would be safe in
 * only one direction: if the server raises its limit and this stays low the
 * panel is merely stricter than it must be, whereas if the server lowers its
 * limit the panel still lets the request go and the 400 is surfaced verbatim.
 * That asymmetry is precisely what makes drift easy to miss, so the two are
 * reconciled by
 * `backend/tests/test_arxiv_sample_plugin.py::test_the_ui_package_import_cap_matches_the_route_cap`
 * — five lines of regex, in the G1 lane. Keep the `export const
 * MAX_IMPORT_URLS = <n>;` shape it matches on.
 */
export const MAX_IMPORT_URLS = 20;

/**
 * The most whitespace-split words a search query may carry.
 *
 * The **authoritative** bound is the plugin's own server-side
 * `routes.py::search`, which rejects an over-limit query with a 400 —
 * silently truncating user-edited input (what `client.py::build_query_url`'s
 * `[:MAX_QUERY_TERMS]` slice used to do, with no warning) is a red line the
 * server-side fix closes. This constant exists for the same reason
 * {@link MAX_IMPORT_URLS} does: so the panel can disable its own submit
 * button and say why, rather than letting someone submit a ten-word query
 * and learn the limit from an error banner.
 *
 * The number is spelled twice, once per language, and drift is safe in only
 * one direction (a panel stricter than the server is merely conservative; a
 * panel looser than the server surfaces the 400 verbatim) — the same
 * asymmetry {@link MAX_IMPORT_URLS}'s doc comment explains, and the same
 * reason the two are reconciled by a regex test:
 * `backend/tests/test_arxiv_sample_plugin.py::test_the_ui_package_query_term_cap_matches_the_route_cap`,
 * in the G1 lane. Keep the `export const MAX_QUERY_TERMS = <n>;` shape it
 * matches on.
 */
export const MAX_QUERY_TERMS = 8;

/**
 * The number of whitespace-split words in `query`, using the exact same
 * splitting rule the server does (`client.py::build_query_url`'s bare
 * `query.split()`, which Python defines as "split on runs of whitespace,
 * ignoring leading/trailing whitespace, drop empty pieces"). A blank or
 * whitespace-only query counts as zero words, matching Python's
 * `"".split() == []` and `"   ".split() == []` rather than JavaScript's
 * `"".split(/\s+/)`, which would otherwise report one (empty) word.
 */
export function countQueryTerms(query: string): number {
  const trimmed = query.trim();
  return trimmed.length === 0 ? 0 : trimmed.split(/\s+/).length;
}

/**
 * Add `arxivId` to the selection. Idempotent: selecting an already-selected
 * id returns the very same `selected` reference rather than a new Set, so a
 * caller that stores this in `useState` does not trigger an extra render on
 * a repeated selection.
 */
export function selectPaper(selected: ReadonlySet<string>, arxivId: string): ReadonlySet<string> {
  if (selected.has(arxivId)) return selected;
  const next = new Set(selected);
  next.add(arxivId);
  return next;
}

/** The inverse of {@link selectPaper}: idempotent on an already-absent id. */
export function deselectPaper(selected: ReadonlySet<string>, arxivId: string): ReadonlySet<string> {
  if (!selected.has(arxivId)) return selected;
  const next = new Set(selected);
  next.delete(arxivId);
  return next;
}

/**
 * The `start` to request for the next page, given how many records the last
 * page actually returned (not a fixed page size — the server may return
 * fewer than requested on the last page). A `returnedCount` of `0` or less
 * leaves `start` unchanged: there is nothing to advance past.
 */
export function nextPageStart(start: number, returnedCount: number): number {
  return start + Math.max(0, returnedCount);
}

/** Join non-blank author names with the CJK enumeration comma; never truncates. */
export function formatAuthors(authors: readonly string[]): string {
  return authors
    .map((name) => name.trim())
    .filter((name) => name.length > 0)
    .join("、");
}

/**
 * Fold one page of results into the panel's running catalog (keyed by
 * `arxiv_id`), so a paper selected on an earlier page is still resolvable
 * to its `pdf_url` after the user pages forward. A no-op call (empty
 * `items`) returns the same `catalog` reference.
 */
export function mergeCatalog(
  catalog: ReadonlyMap<string, ArxivSearchResultItem>,
  items: readonly ArxivSearchResultItem[],
): ReadonlyMap<string, ArxivSearchResultItem> {
  if (items.length === 0) return catalog;
  const next = new Map(catalog);
  for (const item of items) next.set(item.arxiv_id, item);
  return next;
}

/**
 * The PDF URLs for the currently-selected papers, in catalog (== first-seen)
 * order — the same order `/import`'s request body will carry them in, so a
 * receipt list built by walking the response stays in a stable, predictable
 * order for the user.
 */
export function selectedImportUrls(
  catalog: ReadonlyMap<string, ArxivSearchResultItem>,
  selected: ReadonlySet<string>,
): readonly string[] {
  const urls: string[] = [];
  for (const [arxivId, item] of catalog) {
    if (selected.has(arxivId)) urls.push(item.pdf_url);
  }
  return urls;
}

/**
 * Turn one `/import` response into a per-URL receipt. Every URL the server
 * accounted for (in either `created` or `rejected`) gets exactly one entry;
 * a URL the panel asked about but the server did not mention is not present
 * — the caller decides whether that is worth surfacing (it should not
 * happen against a conforming server, so silently dropping it here is safe).
 *
 * See the module docstring for why `"repeat"` is read from `alreadyImported`
 * (this panel's own memory) rather than from anything the response says, and
 * why it warns about a likely duplicate instead of claiming a reuse.
 */
export function classifyImportReceipt(
  response: ArxivImportResponse,
  alreadyImported: ReadonlySet<string>,
): ReadonlyMap<string, ImportReceiptEntry> {
  const receipt = new Map<string, ImportReceiptEntry>();
  for (const row of response.created) {
    receipt.set(
      row.url,
      alreadyImported.has(row.url)
        ? { status: "repeat", title: row.title }
        : { status: "created", title: row.title },
    );
  }
  for (const row of response.rejected) {
    receipt.set(row.url, { status: "rejected", reason: row.reason });
  }
  return receipt;
}

/**
 * Extend the "seen as created" memory with every non-rejected URL in
 * `receipt`. Idempotent: folding the same receipt in twice (or a receipt
 * whose URLs are already all present) returns the same `alreadyImported`
 * reference.
 */
export function foldImportedUrls(
  alreadyImported: ReadonlySet<string>,
  receipt: ReadonlyMap<string, ImportReceiptEntry>,
): ReadonlySet<string> {
  let mutated: Set<string> | null = null;
  for (const [url, entry] of receipt) {
    if (entry.status === "rejected") continue;
    if (alreadyImported.has(url)) continue;
    if (mutated === null) mutated = new Set(alreadyImported);
    mutated.add(url);
  }
  return mutated ?? alreadyImported;
}
