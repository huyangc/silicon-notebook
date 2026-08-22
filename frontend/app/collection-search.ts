import { requestJson } from "./api-client.ts";
import type { SearchHit } from "./workspace-model.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

// Collection search fans out once per visible notebook.  The permit pool is
// module-scoped so rapid query generations share one server-side concurrency
// ceiling instead of each claiming a fresh allowance.
export const SEARCH_FANOUT_LIMIT = 4;

let searchPermitsHeld = 0;
const searchPermitWaiters: (() => void)[] = [];

async function acquireSearchPermit(): Promise<void> {
  if (searchPermitsHeld < SEARCH_FANOUT_LIMIT) {
    searchPermitsHeld += 1;
    return;
  }
  await new Promise<void>((resolve) => searchPermitWaiters.push(resolve));
}

function releaseSearchPermit(): void {
  const next = searchPermitWaiters.shift();
  if (next) next();
  else searchPermitsHeld -= 1;
}

// Deliberately no AbortSignal: the synchronous backend search keeps running
// after a browser disconnect, so returning the permit on client abort would
// undercount real database work.  The signal below gates only requests that
// have not yet been issued.
export const searchNotebook = (id: string, query: string) =>
  requestJson<{ hits: SearchHit[] }>(
    `/notebooks/${id}/search?q=${encodeURIComponent(query)}`,
    options,
  );

export async function searchNotebooksBounded(
  notebookIds: readonly string[],
  query: string,
  signal?: AbortSignal,
  search: (id: string, query: string) => Promise<{ hits: SearchHit[] }> = searchNotebook,
): Promise<Record<string, SearchHit[]>> {
  const hits: Record<string, SearchHit[]> = {};
  await Promise.all(notebookIds.map(async (id) => {
    await acquireSearchPermit();
    try {
      signal?.throwIfAborted();
      hits[id] = (await search(id, query)).hits;
    } finally {
      releaseSearchPermit();
    }
  }));
  return hits;
}
