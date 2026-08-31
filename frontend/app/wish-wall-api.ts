import { requestJson } from "./api-client.ts";
import { WISH_PAGE_MAX, type WishItem, type WishKind, type WishPage, type WishSort, type WishVoteResult } from "./wish-wall-model.ts";

export async function listWishes(options: {
  kind?: WishKind;
  sort: WishSort;
  offset?: number;
  limit?: number;
}): Promise<WishPage> {
  if (options.limit !== undefined && (
    !Number.isInteger(options.limit) || options.limit < 1 || options.limit > WISH_PAGE_MAX
  )) {
    throw new RangeError(`每页数量必须是 1 到 ${WISH_PAGE_MAX} 之间的整数`);
  }
  const query = new URLSearchParams({
    sort: options.sort,
    offset: String(options.offset ?? 0),
  });
  if (options.kind) query.set("kind", options.kind);
  if (options.limit !== undefined) query.set("limit", String(options.limit));
  return requestJson<WishPage>(`/wishes?${query.toString()}`, { tag: "wish-wall" });
}

export async function createWish(input: {
  kind: WishKind;
  title: string;
  content: string;
}): Promise<WishItem> {
  return requestJson<WishItem>("/wishes", {
    tag: "wish-wall",
    method: "POST",
    body: JSON.stringify(input),
  });
}

export async function toggleWishVote(wishId: string): Promise<WishVoteResult> {
  return requestJson<WishVoteResult>(`/wishes/${encodeURIComponent(wishId)}/vote`, {
    tag: "wish-wall",
    method: "POST",
  });
}
