/**
 * Pure state-transition logic for the arXiv search panel — no React, no DOM,
 * no network. Everything the panel does with its own local state (checkbox
 * selection, pagination, author formatting, and turning an import response
 * into a per-item receipt) is a function here, so it can be exercised by
 * `node --test` without a browser or a mounted component. The `.tsx` entry
 * only wires these to `useState` and to the two HTTP calls.
 *
 * **Why "reused" cannot come from the wire.** The plugin's own `/import`
 * route (`routes.py::import_papers`) forwards straight to core's
 * `PluginUrlSourceImportPort`, whose `created` rows
 * (`PluginImportedSource{source_id, title, url}`) carry no flag saying
 * whether a row is a brand-new source or an existing one core's own
 * content-hash de-duplication handed back unchanged — core deliberately
 * does not surface that distinction to a plugin. So "已复用" here is a
 * *client-side* fact, not a claim about what core did: it means "this
 * session already saw this exact URL come back as `created` once before".
 * `foldImportedUrls` is the one place that memory is updated, and
 * `classifyImportReceipt` is the one place it is read — a URL is graded
 * `"reused"` only on its second-or-later appearance in *this panel's*
 * accumulated memory, never on a guess about the backend's own de-dup.
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
  | { status: "reused"; title: string }
  | { status: "rejected"; reason: string }
>;

/** The `start` value a fresh query (as opposed to "load more") begins at. */
export const FIRST_PAGE_START = 0;

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
 * See the module docstring for why `"reused"` is read from `alreadyImported`
 * (this panel's own memory) rather than from anything the response says.
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
        ? { status: "reused", title: row.title }
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
