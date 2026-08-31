import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  listWishes: vi.fn(),
  createWish: vi.fn(),
  toggleWishVote: vi.fn(),
}));

vi.mock("../../app/auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("../../app/wish-wall-api.ts", () => ({
  listWishes: mocks.listWishes,
  createWish: mocks.createWish,
  toggleWishVote: mocks.toggleWishVote,
}));

import WishWallPage from "../../app/wishes/page";

const feature = {
  id: "wish-feature",
  kind: "feature",
  title: "希望支持批量标签",
  content: "整理大量资料时可以一次选择多个来源。",
  author_id: "user-1",
  author_name: "小林",
  vote_count: 7,
  voted_by_me: false,
  created_at: "2026-08-31T10:00:00+08:00",
  updated_at: "2026-08-31T10:00:00+08:00",
};

beforeEach(() => {
  mocks.fetchMe.mockResolvedValue({ id: "user-1", username: "小林", role: "user" });
  mocks.listWishes.mockResolvedValue({ items: [feature], total: 1, offset: 0, limit: 50 });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("普通用户可以提交需求，但不能发布更新计划", async () => {
  mocks.createWish.mockResolvedValue(feature);
  const user = userEvent.setup();
  render(<WishWallPage />);

  expect(await screen.findByText("希望支持批量标签")).toBeInTheDocument();
  const composerKinds = screen.getByRole("group", { name: "内容类型" });
  expect(within(composerKinds).queryByRole("button", { name: "更新计划" })).not.toBeInTheDocument();

  await user.type(screen.getByLabelText("标题"), "  增加导出格式  ");
  await user.type(screen.getByLabelText("详细说明"), "  希望支持 Markdown 导出。  ");
  await user.click(screen.getByRole("button", { name: "提交反馈" }));

  await waitFor(() => expect(mocks.createWish).toHaveBeenCalledWith({
    kind: "feature",
    title: "增加导出格式",
    content: "希望支持 Markdown 导出。",
  }));
  expect(await screen.findByText("已提交，感谢你的反馈")).toBeInTheDocument();
});

test("许愿输入按 Unicode 字符计数且不会静默截断 emoji", async () => {
  mocks.createWish.mockResolvedValue(feature);
  const user = userEvent.setup();
  render(<WishWallPage />);
  await screen.findByText(feature.title);
  const titleInput = screen.getByLabelText("标题");
  const emojiTitle = "😀".repeat(120);
  fireEvent.change(titleInput, { target: { value: emojiTitle } });
  fireEvent.change(screen.getByLabelText("详细说明"), { target: { value: "说明" } });

  expect(titleInput).toHaveValue(emojiTitle);
  expect(screen.getByText("120/120")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "提交反馈" }));
  await waitFor(() => expect(mocks.createWish).toHaveBeenCalledWith({
    kind: "feature",
    title: emojiTitle,
    content: "说明",
  }));
});

test("Unicode 字符超限时保留原输入并给出明确提示", async () => {
  const user = userEvent.setup();
  render(<WishWallPage />);
  await screen.findByText(feature.title);
  const titleInput = screen.getByLabelText("标题");
  const overLimitTitle = "😀".repeat(121);
  fireEvent.change(titleInput, { target: { value: overLimitTitle } });
  fireEvent.change(screen.getByLabelText("详细说明"), { target: { value: "说明" } });

  await user.click(screen.getByRole("button", { name: "提交反馈" }));
  expect(await screen.findByText("标题不能超过 120 个字符")).toBeInTheDocument();
  expect(titleInput).toHaveValue(overLimitTitle);
  expect(mocks.createWish).not.toHaveBeenCalled();
});

test("许愿输入的 Unicode 空白 trim 规则与 Python 后端一致", async () => {
  mocks.createWish.mockResolvedValue(feature);
  const user = userEvent.setup();
  render(<WishWallPage />);
  await screen.findByText(feature.title);
  const effectiveTitle = "x".repeat(120);
  fireEvent.change(screen.getByLabelText("标题"), { target: { value: `${effectiveTitle}\u0085` } });
  fireEvent.change(screen.getByLabelText("详细说明"), { target: { value: "\u001c说明\u001c" } });

  expect(screen.getByText("120/120")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "提交反馈" }));
  await waitFor(() => expect(mocks.createWish).toHaveBeenCalledWith({
    kind: "feature",
    title: effectiveTitle,
    content: "说明",
  }));
});

test("点赞结果在当前卡片旁即时更新", async () => {
  const updated = { ...feature, vote_count: 8, voted_by_me: true };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 1, offset: 0, limit: 50 })
    .mockResolvedValueOnce({ items: [updated], total: 1, offset: 0, limit: 50 });
  mocks.toggleWishVote.mockResolvedValue({ wish_id: feature.id, voted: true, vote_count: 8 });
  const user = userEvent.setup();
  render(<WishWallPage />);

  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();
  await user.click(within(card as HTMLElement).getByRole("button", { name: /点赞 7/ }));

  expect(await within(card as HTMLElement).findByRole("button", { name: /已点赞 8/ })).toHaveAttribute("aria-pressed", "true");
  expect(within(card as HTMLElement).getByRole("status")).toHaveTextContent("已点赞");
});

test("优先级排序下点赞后重新加载第一页并采用服务端顺序", async () => {
  const higher = { ...feature, id: "wish-higher", title: "当前优先级更高", vote_count: 8 };
  const promoted = { ...feature, vote_count: 9, voted_by_me: true };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [higher, feature], total: 2, offset: 0, limit: 50 })
    .mockResolvedValueOnce({ items: [promoted, higher], total: 2, offset: 0, limit: 50 });
  mocks.toggleWishVote.mockResolvedValue({ wish_id: feature.id, voted: true, vote_count: 9 });
  const user = userEvent.setup();
  render(<WishWallPage />);

  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();
  await user.click(within(card as HTMLElement).getByRole("button", { name: /点赞 7/ }));

  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));
  expect(mocks.listWishes).toHaveBeenLastCalledWith({ kind: undefined, sort: "priority", offset: 0, limit: 2 });
  expect(screen.getAllByRole("heading", { level: 2 }).map((heading) => heading.textContent)).toEqual([
    feature.title,
    higher.title,
  ]);
});

test("点赞成功但排序刷新失败时保留更新后的卡片和局部反馈", async () => {
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 1, offset: 0, limit: 50 })
    .mockRejectedValueOnce(new Error("refresh failed"));
  mocks.toggleWishVote.mockResolvedValue({ wish_id: feature.id, voted: true, vote_count: 8 });
  const user = userEvent.setup();
  render(<WishWallPage />);

  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();
  await user.click(within(card as HTMLElement).getByRole("button", { name: /点赞 7/ }));

  expect(await within(card as HTMLElement).findByRole("button", { name: /已点赞 8/ })).toHaveAttribute("aria-pressed", "true");
  expect(within(card as HTMLElement).getByRole("status")).toHaveTextContent("已点赞，但排序暂未刷新；请稍后重试或刷新页面");
  expect(screen.queryByText("点赞已更新，但排序刷新失败，请重试")).not.toBeInTheDocument();
});

test("加载超过单次上限后点赞会分块刷新窗口并保留遗漏卡片与反馈", async () => {
  const firstPage = [feature, ...Array.from({ length: 49 }, (_, index) => ({
    ...feature,
    id: `wish-first-${index}`,
    title: `第一页需求 ${index}`,
    vote_count: 6,
  }))];
  const secondPage = Array.from({ length: 50 }, (_, index) => ({
    ...feature,
    id: `wish-second-${index}`,
    title: `第二页需求 ${index}`,
    vote_count: 4,
  }));
  const tail = { ...feature, id: "wish-tail", title: "第三页需求", vote_count: 1 };
  const boundary = { ...feature, id: "wish-boundary", title: "刷新窗口边界需求", vote_count: 2 };
  const updated = { ...feature, vote_count: 8, voted_by_me: true };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: firstPage, total: 101, offset: 0, limit: 50 })
    .mockResolvedValueOnce({ items: secondPage, total: 101, offset: 50, limit: 50 })
    .mockResolvedValueOnce({ items: [tail], total: 101, offset: 100, limit: 50 })
    .mockResolvedValueOnce({ items: [...firstPage.slice(1), ...secondPage, tail], total: 102, offset: 0, limit: 100 })
    .mockResolvedValueOnce({ items: [boundary], total: 102, offset: 100, limit: 1 })
    .mockResolvedValueOnce({ items: [boundary, updated], total: 102, offset: 100, limit: 50 });
  mocks.toggleWishVote.mockResolvedValue({ wish_id: feature.id, voted: true, vote_count: 8 });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.click(screen.getByRole("button", { name: /加载更多/ }));
  expect(await screen.findByText("第二页需求 49")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /加载更多/ }));
  expect(await screen.findByText(tail.title)).toBeInTheDocument();
  const card = screen.getByText(feature.title).closest("article");
  expect(card).not.toBeNull();
  await user.click(within(card as HTMLElement).getByRole("button", { name: /点赞 7/ }));

  await waitFor(() => expect(mocks.listWishes).toHaveBeenNthCalledWith(4, {
    kind: undefined,
    sort: "priority",
    offset: 0,
    limit: 100,
  }));
  expect(mocks.listWishes).toHaveBeenNthCalledWith(5, {
    kind: undefined,
    sort: "priority",
    offset: 100,
    limit: 1,
  });
  expect(screen.getByText(tail.title)).toBeInTheDocument();
  expect(await within(card as HTMLElement).findByRole("button", { name: /已点赞 8/ })).toHaveAttribute("aria-pressed", "true");
  expect(within(card as HTMLElement).getByRole("status")).toHaveTextContent("已点赞");
  expect(screen.queryByText(boundary.title)).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: /加载更多/ }));
  expect(await screen.findByText(boundary.title)).toBeInTheDocument();
  expect(mocks.listWishes).toHaveBeenLastCalledWith({
    kind: undefined,
    sort: "priority",
    offset: 100,
    limit: 50,
  });
  expect(screen.getAllByText(feature.title)).toHaveLength(1);
});

test("点赞在途切换筛选后由当前代际窗口结束加载态", async () => {
  let resolveVote!: (value: { wish_id: string; voted: boolean; vote_count: number }) => void;
  let resolveStaleFilter!: (value: { items: Array<typeof feature>; total: number; offset: number; limit: number }) => void;
  const pendingVote = new Promise<{ wish_id: string; voted: boolean; vote_count: number }>((resolve) => { resolveVote = resolve; });
  const staleFilter = new Promise<{ items: Array<typeof feature>; total: number; offset: number; limit: number }>((resolve) => { resolveStaleFilter = resolve; });
  const bug = { ...feature, id: "wish-bug", kind: "bug", title: "筛选后的问题反馈" };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 1, offset: 0, limit: 50 })
    .mockReturnValueOnce(staleFilter)
    .mockResolvedValueOnce({ items: [bug], total: 1, offset: 0, limit: 1 });
  mocks.toggleWishVote.mockReturnValue(pendingVote);
  const user = userEvent.setup();
  render(<WishWallPage />);

  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();
  await user.click(within(card as HTMLElement).getByRole("button", { name: /点赞 7/ }));
  await user.click(within(screen.getByRole("group", { name: "筛选许愿墙内容" })).getByRole("button", { name: "问题反馈" }));
  expect(await screen.findByText("正在加载许愿墙…")).toBeInTheDocument();

  await act(async () => {
    resolveVote({ wish_id: feature.id, voted: true, vote_count: 8 });
    await Promise.resolve();
  });
  expect(await screen.findByText(bug.title)).toBeInTheDocument();
  expect(mocks.listWishes).toHaveBeenLastCalledWith({ kind: "bug", sort: "priority", offset: 0, limit: 1 });

  resolveStaleFilter({ items: [{ ...bug, id: "stale-bug", title: "迟到的筛选结果" }], total: 1, offset: 0, limit: 50 });
  await act(async () => { await Promise.resolve(); });
  expect(screen.queryByText("迟到的筛选结果")).not.toBeInTheDocument();
});

test("点赞在途切换筛选且窗口失败时进入可重试错误态", async () => {
  let resolveVote!: (value: { wish_id: string; voted: boolean; vote_count: number }) => void;
  let resolveStaleFilter!: (value: { items: Array<typeof feature>; total: number; offset: number; limit: number }) => void;
  const pendingVote = new Promise<{ wish_id: string; voted: boolean; vote_count: number }>((resolve) => { resolveVote = resolve; });
  const staleFilter = new Promise<{ items: Array<typeof feature>; total: number; offset: number; limit: number }>((resolve) => { resolveStaleFilter = resolve; });
  const bug = { ...feature, id: "wish-bug-retry", kind: "bug", title: "重试后的问题反馈" };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 1, offset: 0, limit: 50 })
    .mockReturnValueOnce(staleFilter)
    .mockRejectedValueOnce(new Error("window refresh failed"))
    .mockResolvedValueOnce({ items: [bug], total: 1, offset: 0, limit: 50 });
  mocks.toggleWishVote.mockReturnValue(pendingVote);
  const user = userEvent.setup();
  render(<WishWallPage />);

  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();
  await user.click(within(card as HTMLElement).getByRole("button", { name: /点赞 7/ }));
  await user.click(within(screen.getByRole("group", { name: "筛选许愿墙内容" })).getByRole("button", { name: "问题反馈" }));
  await screen.findByText("正在加载许愿墙…");

  await act(async () => {
    resolveVote({ wish_id: feature.id, voted: true, vote_count: 8 });
    await Promise.resolve();
  });
  expect(await screen.findByText("已点赞，但许愿墙刷新失败，请重试")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByText(bug.title)).toBeInTheDocument();

  resolveStaleFilter({ items: [{ ...bug, id: "stale-retry", title: "迟到的失败筛选结果" }], total: 1, offset: 0, limit: 50 });
  await act(async () => { await Promise.resolve(); });
  expect(screen.queryByText("迟到的失败筛选结果")).not.toBeInTheDocument();
});

test("优先级排序加载更多在途时禁用点赞，完成后恢复", async () => {
  let resolveLoadMore!: (value: { items: Array<typeof feature>; total: number; offset: number; limit: number }) => void;
  const pendingLoadMore = new Promise<{ items: Array<typeof feature>; total: number; offset: number; limit: number }>((resolve) => {
    resolveLoadMore = resolve;
  });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 2, offset: 0, limit: 50 })
    .mockReturnValueOnce(pendingLoadMore);
  const user = userEvent.setup();
  render(<WishWallPage />);

  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();
  await user.click(screen.getByRole("button", { name: /加载更多/ }));
  expect(screen.getByRole("button", { name: "加载中…" })).toBeDisabled();
  const voteButton = within(card as HTMLElement).getByRole("button", { name: /点赞 7/ });
  expect(voteButton).toBeDisabled();
  await user.click(voteButton);
  expect(mocks.toggleWishVote).not.toHaveBeenCalled();
  resolveLoadMore({ items: [{ ...feature, id: "late-item", title: "迟到的分页项" }], total: 2, offset: 1, limit: 50 });
  expect(await screen.findByText("迟到的分页项")).toBeInTheDocument();
  await waitFor(() => expect(voteButton).toBeEnabled());
});

test("加载更多沿用服务端返回的默认页大小", async () => {
  const second = { ...feature, id: "wish-second", title: "第二条需求" };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 2, offset: 0, limit: 1 })
    .mockResolvedValueOnce({ items: [second], total: 2, offset: 1, limit: 1 });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.click(screen.getByRole("button", { name: /加载更多/ }));
  expect(await screen.findByText(second.title)).toBeInTheDocument();
  expect(mocks.listWishes).toHaveBeenLastCalledWith({
    kind: undefined,
    sort: "priority",
    offset: 1,
    limit: 1,
  });
});

test("最新排序加载更多在途时禁用点赞，避免迟到分页覆盖票数", async () => {
  let resolveLoadMore!: (value: { items: Array<typeof feature>; total: number; offset: number; limit: number }) => void;
  const pendingLoadMore = new Promise<{ items: Array<typeof feature>; total: number; offset: number; limit: number }>((resolve) => {
    resolveLoadMore = resolve;
  });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 2, offset: 0, limit: 50 })
    .mockResolvedValueOnce({ items: [feature], total: 2, offset: 0, limit: 50 })
    .mockReturnValueOnce(pendingLoadMore);
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.selectOptions(screen.getByRole("combobox", { name: "排序" }), "latest");
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));
  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();
  await user.click(screen.getByRole("button", { name: /加载更多/ }));
  expect(screen.getByRole("button", { name: "加载中…" })).toBeDisabled();

  const voteButton = within(card as HTMLElement).getByRole("button", { name: /点赞 7/ });
  expect(voteButton).toBeDisabled();
  await user.click(voteButton);
  expect(mocks.toggleWishVote).not.toHaveBeenCalled();

  resolveLoadMore({ items: [], total: 2, offset: 1, limit: 50 });
  await waitFor(() => expect(voteButton).toBeEnabled());
});

test("点赞后的优先级首页重载完成前不能抢跑加载更多", async () => {
  let resolvePriorityReload!: (value: { items: Array<typeof feature>; total: number; offset: number; limit: number }) => void;
  const pendingPriorityReload = new Promise<{ items: Array<typeof feature>; total: number; offset: number; limit: number }>((resolve) => {
    resolvePriorityReload = resolve;
  });
  const updated = { ...feature, vote_count: 8, voted_by_me: true };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 2, offset: 0, limit: 50 })
    .mockReturnValueOnce(pendingPriorityReload);
  mocks.toggleWishVote.mockResolvedValue({ wish_id: feature.id, voted: true, vote_count: 8 });
  const user = userEvent.setup();
  render(<WishWallPage />);

  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();
  await user.click(within(card as HTMLElement).getByRole("button", { name: /点赞 7/ }));
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));
  const loadMore = screen.getByRole("button", { name: /加载更多/ });
  expect(loadMore).toBeDisabled();
  await user.click(loadMore);
  expect(mocks.listWishes).toHaveBeenCalledTimes(2);

  resolvePriorityReload({ items: [updated], total: 2, offset: 0, limit: 50 });
  await waitFor(() => expect(screen.getByRole("button", { name: /加载更多/ })).toBeEnabled());
});

test("严格模式重挂后点赞结果仍会更新", async () => {
  const updated = { ...feature, vote_count: 8, voted_by_me: true };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 1, offset: 0, limit: 50 })
    .mockResolvedValueOnce({ items: [feature], total: 1, offset: 0, limit: 50 })
    .mockResolvedValueOnce({ items: [updated], total: 1, offset: 0, limit: 50 });
  mocks.toggleWishVote.mockResolvedValue({ wish_id: feature.id, voted: true, vote_count: 8 });
  const user = userEvent.setup();
  render(<StrictMode><WishWallPage /></StrictMode>);

  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();
  await user.click(within(card as HTMLElement).getByRole("button", { name: /点赞 7/ }));

  expect(await within(card as HTMLElement).findByRole("button", { name: /已点赞 8/ })).toHaveAttribute("aria-pressed", "true");
  expect(within(card as HTMLElement).getByRole("status")).toHaveTextContent("已点赞");
});

test("点赞失败提示会自动复位", async () => {
  mocks.toggleWishVote.mockRejectedValue(new Error("vote failed"));
  render(<WishWallPage />);
  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();
  vi.useFakeTimers();
  await act(async () => {
    fireEvent.click(within(card as HTMLElement).getByRole("button", { name: /点赞 7/ }));
    await Promise.resolve();
  });
  expect(within(card as HTMLElement).getByRole("status")).toHaveTextContent("操作失败，请重试");

  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(within(card as HTMLElement).queryByRole("status")).not.toBeInTheDocument();
});

test("页面卸载后在途点赞不会再创建提示计时器", async () => {
  let resolveVote!: (value: { wish_id: string; voted: boolean; vote_count: number }) => void;
  mocks.toggleWishVote.mockReturnValue(new Promise((resolve) => { resolveVote = resolve; }));
  const timerSpy = vi.spyOn(window, "setTimeout");
  const user = userEvent.setup();
  const view = render(<WishWallPage />);
  const card = (await screen.findByText(feature.title)).closest("article");
  expect(card).not.toBeNull();

  await user.click(within(card as HTMLElement).getByRole("button", { name: /点赞 7/ }));
  view.unmount();
  await act(async () => {
    resolveVote({ wish_id: feature.id, voted: true, vote_count: 8 });
    await Promise.resolve();
  });

  expect(timerSpy.mock.calls.some(([, delay]) => delay === 3000)).toBe(false);
});

test("管理员可以发布更新计划", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.createWish.mockResolvedValue({ ...feature, id: "wish-plan", kind: "plan" });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.click(within(screen.getByRole("group", { name: "内容类型" })).getByRole("button", { name: "更新计划" }));
  await user.type(screen.getByLabelText("标题"), "九月更新");
  await user.type(screen.getByLabelText("详细说明"), "将上线批量标签。");
  await user.click(screen.getByRole("button", { name: "发布计划" }));

  await waitFor(() => expect(mocks.createWish).toHaveBeenCalledWith({
    kind: "plan",
    title: "九月更新",
    content: "将上线批量标签。",
  }));
});

test("创建成功后列表刷新失败不会误报提交失败", async () => {
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 1, offset: 0, limit: 50 })
    .mockRejectedValueOnce(new Error("refresh failed"));
  mocks.createWish.mockResolvedValue(feature);
  const user = userEvent.setup();
  render(<WishWallPage />);
  await screen.findByText(feature.title);

  await user.type(screen.getByLabelText("标题"), "增加导出格式");
  await user.type(screen.getByLabelText("详细说明"), "希望支持 Markdown 导出。");
  await user.click(screen.getByRole("button", { name: "提交反馈" }));

  expect(await screen.findByText("已提交，但列表暂未更新；请稍后重试或刷新页面")).toBeInTheDocument();
  expect(screen.getByLabelText("标题")).toHaveValue("");
  expect(screen.queryByText("提交失败，请重试")).not.toBeInTheDocument();
});

test("迟到的旧筛选响应不会覆盖当前许愿墙结果", async () => {
  let resolveBug!: (value: { items: Array<typeof feature>; total: number; offset: number; limit: number }) => void;
  let resolveFeature!: (value: { items: Array<typeof feature>; total: number; offset: number; limit: number }) => void;
  const bugResponse = new Promise<{ items: Array<typeof feature>; total: number; offset: number; limit: number }>((resolve) => { resolveBug = resolve; });
  const featureResponse = new Promise<{ items: Array<typeof feature>; total: number; offset: number; limit: number }>((resolve) => { resolveFeature = resolve; });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature], total: 1, offset: 0, limit: 50 })
    .mockReturnValueOnce(bugResponse)
    .mockReturnValueOnce(featureResponse);
  const user = userEvent.setup();
  render(<WishWallPage />);
  await screen.findByText(feature.title);

  const filters = screen.getByRole("group", { name: "筛选许愿墙内容" });
  await user.click(within(filters).getByRole("button", { name: "问题反馈" }));
  await user.click(within(filters).getByRole("button", { name: "功能需求" }));
  resolveFeature({
    items: [{ ...feature, id: "current-feature", title: "当前筛选的功能需求" }],
    total: 1,
    offset: 0,
    limit: 50,
  });
  expect(await screen.findByText("当前筛选的功能需求")).toBeInTheDocument();

  resolveBug({
    items: [{ ...feature, id: "late-bug", kind: "bug", title: "迟到的问题反馈" }],
    total: 1,
    offset: 0,
    limit: 50,
  });
  await waitFor(() => expect(screen.queryByText("迟到的问题反馈")).not.toBeInTheDocument());
  expect(screen.getByText("当前筛选的功能需求")).toBeInTheDocument();
});

test("加载中重复选择当前筛选不会作废在途结果", async () => {
  let resolveCurrent!: (value: { items: Array<typeof feature>; total: number; offset: number; limit: number }) => void;
  const currentResponse = new Promise<{ items: Array<typeof feature>; total: number; offset: number; limit: number }>((resolve) => { resolveCurrent = resolve; });
  mocks.listWishes.mockReset();
  mocks.listWishes.mockReturnValueOnce(currentResponse);
  const user = userEvent.setup();
  render(<WishWallPage />);
  expect(await screen.findByText("正在加载许愿墙…")).toBeInTheDocument();

  await user.click(within(screen.getByRole("group", { name: "筛选许愿墙内容" })).getByRole("button", { name: "全部" }));
  resolveCurrent({ items: [feature], total: 1, offset: 0, limit: 50 });

  expect(await screen.findByText(feature.title)).toBeInTheDocument();
  expect(mocks.listWishes).toHaveBeenCalledTimes(1);
});
