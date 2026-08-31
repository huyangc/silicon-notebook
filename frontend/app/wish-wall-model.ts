export const WISH_TITLE_MAX_CHARS = 120;
export const WISH_CONTENT_MAX_CHARS = 4000;
export const WISH_PAGE_MAX = 100;

export type WishKind = "bug" | "feature" | "plan";
export type WishSort = "priority" | "latest";

export type WishItem = {
  id: string;
  kind: WishKind;
  title: string;
  content: string;
  author_id: string;
  author_name: string;
  vote_count: number;
  voted_by_me: boolean;
  created_at: string;
  updated_at: string;
};

export type WishPage = {
  items: WishItem[];
  total: number;
  offset: number;
  limit: number;
};

export type WishVoteResult = {
  wish_id: string;
  voted: boolean;
  vote_count: number;
};

export const WISH_KIND_LABELS: Record<WishKind, string> = {
  bug: "问题反馈",
  feature: "功能需求",
  plan: "更新计划",
};
