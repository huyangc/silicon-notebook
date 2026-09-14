import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  listWishes: vi.fn(),
  createWish: vi.fn(),
  updateWish: vi.fn(),
  deleteWish: vi.fn(),
  setWishStatus: vi.fn(),
  toggleWishVote: vi.fn(),
}));

vi.mock("../../app/auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("../../app/wish-wall-api.ts", () => ({
  listWishes: mocks.listWishes,
  createWish: mocks.createWish,
  updateWish: mocks.updateWish,
  deleteWish: mocks.deleteWish,
  setWishStatus: mocks.setWishStatus,
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
  status: "open",
  vote_count: 7,
  voted_by_me: false,
  created_at: "2026-08-31T10:00:00+08:00",
  updated_at: "2026-08-31T10:00:00+08:00",
};
const quiet = {
  ...feature,
  id: "wish-quiet",
  title: "冷门需求",
  content: "少数人需要。",
  author_id: "user-2",
  author_name: "老王",
  vote_count: 2,
  created_at: "2026-08-30T10:00:00+08:00",
  updated_at: "2026-08-30T10:00:00+08:00",
};

function cardOf(title: string): HTMLElement {
  const card = screen.getByText(title).closest("article");
  expect(card).not.toBeNull();
  return card as HTMLElement;
}

beforeEach(() => {
  mocks.fetchMe.mockResolvedValue({ id: "user-1", username: "小林", role: "user" });
  mocks.listWishes.mockResolvedValue({ items: [feature, quiet], total: 2, offset: 0, limit: 50 });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("作者可以就地编辑自己的内容，结果回写卡片并在动作旁提示", async () => {
  mocks.updateWish.mockResolvedValue({ ...feature, title: "希望支持批量标签（含来源）" });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  // 只有自己的卡片有编辑/删除入口；别人的没有，也没有管理员的状态控件。
  expect(within(cardOf(quiet.title)).queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
  expect(within(cardOf(quiet.title)).queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox", { name: "处理状态" })).not.toBeInTheDocument();

  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "编辑" }));
  const titleInput = within(card).getByLabelText("修改标题");
  expect(titleInput).toHaveValue(feature.title);
  await user.clear(titleInput);
  await user.type(titleInput, "  希望支持批量标签（含来源）  ");
  await user.click(within(card).getByRole("button", { name: "保存修改" }));

  await waitFor(() => expect(mocks.updateWish).toHaveBeenCalledWith(feature.id, {
    title: "希望支持批量标签（含来源）",
  }));
  const updatedCard = await screen.findByText("希望支持批量标签（含来源）");
  const article = updatedCard.closest("article") as HTMLElement;
  expect(within(article).getByRole("status")).toHaveTextContent("已保存修改");
  expect(within(article).queryByLabelText("修改标题")).not.toBeInTheDocument();
  // 只改标题不影响排序，不重拉列表。
  expect(mocks.listWishes).toHaveBeenCalledTimes(1);
});

test("改类型会影响优先级：保存后按服务端窗口重拉并采用其顺序", async () => {
  const retyped = { ...feature, kind: "bug" as const };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 2, offset: 0, limit: 50 })
    .mockResolvedValueOnce({ items: [quiet, retyped], total: 2, offset: 0, limit: 2 });
  mocks.updateWish.mockResolvedValue(retyped);
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "编辑" }));
  await user.click(within(card).getByRole("button", { name: "问题反馈" }));
  await user.click(within(card).getByRole("button", { name: "保存修改" }));

  await waitFor(() => expect(mocks.updateWish).toHaveBeenCalledWith(feature.id, { kind: "bug" }));
  await waitFor(() => expect(mocks.listWishes).toHaveBeenLastCalledWith({
    kind: undefined,
    status: undefined,
    sort: "priority",
    offset: 0,
    limit: 2,
  }));
  await waitFor(() => expect(
    screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent),
  ).toEqual([quiet.title, feature.title]));
  expect(within(cardOf(feature.title)).getByText("问题反馈")).toBeInTheDocument();
});

test("编辑校验与保存失败都在编辑区旁提示并保留草稿", async () => {
  mocks.updateWish.mockRejectedValue(new Error("boom"));
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "编辑" }));
  const titleInput = within(card).getByLabelText("修改标题");
  await user.clear(titleInput);
  await user.click(within(card).getByRole("button", { name: "保存修改" }));
  expect(within(card).getByRole("alert")).toHaveTextContent("请填写标题和详细说明");
  expect(mocks.updateWish).not.toHaveBeenCalled();

  await user.type(titleInput, "新标题");
  await user.click(within(card).getByRole("button", { name: "保存修改" }));
  await waitFor(() => expect(within(card).getByRole("alert")).toHaveTextContent("保存失败，请重试"));
  expect(within(card).getByLabelText("修改标题")).toHaveValue("新标题");

  await user.click(within(card).getByRole("button", { name: "取消" }));
  expect(within(card).queryByLabelText("修改标题")).not.toBeInTheDocument();
  expect(screen.getByText(feature.title)).toBeInTheDocument();
});

test("删除需要在卡片内二次确认，成功后卡片消失且总数同步", async () => {
  mocks.deleteWish.mockResolvedValue(undefined);
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "删除" }));
  expect(within(card).getByRole("group", { name: "确认删除" })).toHaveTextContent("确定删除这条内容？");
  await user.click(within(card).getByRole("button", { name: "取消" }));
  expect(within(card).queryByRole("group", { name: "确认删除" })).not.toBeInTheDocument();
  expect(mocks.deleteWish).not.toHaveBeenCalled();

  await user.click(within(card).getByRole("button", { name: "删除" }));
  await user.click(within(card).getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(mocks.deleteWish).toHaveBeenCalledWith(feature.id));
  await waitFor(() => expect(screen.queryByText(feature.title)).not.toBeInTheDocument());
  expect(screen.getByText(quiet.title)).toBeInTheDocument();
});

test("删除失败时卡片保留并在动作旁给出错误", async () => {
  mocks.deleteWish.mockRejectedValue(new Error("boom"));
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "删除" }));
  await user.click(within(card).getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(within(card).getByRole("alert")).toHaveTextContent("删除失败，请重试"));
  expect(screen.getByText(feature.title)).toBeInTheDocument();
  expect(within(card).getByRole("button", { name: "删除" })).toBeEnabled();
});

test("管理员标记状态后卡片就地更新、已完成沉底并按服务端窗口对齐游标", async () => {
  const done = { ...feature, status: "done" as const };
  const unseen = { ...quiet, id: "wish-unseen", title: "原本在第二页", author_id: "user-3", vote_count: 1 };
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 3, offset: 0, limit: 2 })
    // 已完成的条目在服务端沉到第 3 位，第二页的条目挤进了当前窗口。
    .mockResolvedValueOnce({ items: [quiet, unseen], total: 3, offset: 0, limit: 2 });
  mocks.setWishStatus.mockResolvedValue(done);
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  // 管理员对任何人的内容都有编辑/删除入口。
  expect(within(cardOf(quiet.title)).getByRole("button", { name: "编辑" })).toBeInTheDocument();
  const card = cardOf(feature.title);
  await user.selectOptions(within(card).getByRole("combobox", { name: "处理状态" }), "done");

  await waitFor(() => expect(mocks.setWishStatus).toHaveBeenCalledWith(feature.id, "done"));
  await waitFor(() => expect(within(cardOf(feature.title)).getByRole("status")).toHaveTextContent("已标记为已完成"));
  const updated = cardOf(feature.title);
  expect(within(updated).getByRole("combobox", { name: "处理状态" })).toHaveValue("done");
  expect(within(updated).getByText("已完成", { selector: ".wish-status" })).toBeInTheDocument();

  // 整窗重拉：沉出窗口的条目保留在末尾，但游标只按服务端条目计数（此处为 1），
  // 因此「加载更多」会从挤进来的那条起继续，而不是把它永久跳过。
  await waitFor(() => expect(mocks.listWishes).toHaveBeenLastCalledWith({
    kind: undefined,
    status: undefined,
    sort: "priority",
    offset: 0,
    limit: 2,
  }));
  await waitFor(() => expect(
    screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent),
  ).toEqual([quiet.title, feature.title]));
  expect(screen.getByRole("button", { name: "加载更多（还有 1 条）" })).toBeInTheDocument();
  mocks.listWishes.mockResolvedValueOnce({ items: [unseen, done], total: 3, offset: 1, limit: 2 });
  await user.click(screen.getByRole("button", { name: "加载更多（还有 1 条）" }));
  await waitFor(() => expect(mocks.listWishes).toHaveBeenLastCalledWith({
    kind: undefined,
    status: undefined,
    sort: "priority",
    offset: 1,
    limit: 2,
  }));
  await waitFor(() => expect(
    screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent),
  ).toEqual([quiet.title, feature.title, unseen.title]));
});

test("标记状态失败时下拉保持服务端值并给出错误", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.setWishStatus.mockRejectedValue(new Error("boom"));
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  const card = cardOf(feature.title);
  await user.selectOptions(within(card).getByRole("combobox", { name: "处理状态" }), "declined");
  await waitFor(() => expect(within(card).getByRole("alert")).toHaveTextContent("标记失败，请重试"));
  expect(within(card).getByRole("combobox", { name: "处理状态" })).toHaveValue("open");
  expect(within(card).queryByText("不采纳", { selector: ".wish-status" })).not.toBeInTheDocument();
});

test("状态筛选会传给列表接口，状态改动后不再匹配的卡片从窗口移除", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 2, offset: 0, limit: 50 })
    .mockResolvedValueOnce({ items: [feature, quiet], total: 2, offset: 0, limit: 50 });
  mocks.setWishStatus.mockResolvedValue({ ...quiet, status: "in_progress" });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.selectOptions(screen.getByRole("combobox", { name: "按处理状态筛选" }), "open");
  await waitFor(() => expect(mocks.listWishes).toHaveBeenLastCalledWith({
    kind: undefined,
    status: "open",
    sort: "priority",
  }));
  await screen.findByText(quiet.title);

  await user.selectOptions(within(cardOf(quiet.title)).getByRole("combobox", { name: "处理状态" }), "in_progress");
  await waitFor(() => expect(mocks.setWishStatus).toHaveBeenCalledWith(quiet.id, "in_progress"));
  await waitFor(() => expect(screen.queryByText(quiet.title)).not.toBeInTheDocument());
  expect(screen.getByText(feature.title)).toBeInTheDocument();
});

test("加载更多在途时删除卡片，迟到的分页结果不会复活它", async () => {
  const third = { ...quiet, id: "wish-third", title: "第三条", author_id: "user-3" };
  let resolveMore: (page: unknown) => void = () => {};
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 3, offset: 0, limit: 2 })
    .mockImplementationOnce(() => new Promise((resolve) => { resolveMore = resolve; }));
  mocks.deleteWish.mockResolvedValue(undefined);
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.click(screen.getByRole("button", { name: "加载更多（还有 1 条）" }));
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));

  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "删除" }));
  await user.click(within(card).getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(screen.queryByText(feature.title)).not.toBeInTheDocument());

  // 删除之前捕获的窗口在删除之后才返回：整份结果作废，不能把已删卡片写回。
  resolveMore({ items: [third], total: 3, offset: 2, limit: 2 });
  await waitFor(() => expect(screen.getByRole("button", { name: "加载更多（还有 1 条）" })).toBeEnabled());
  expect(screen.queryByText(feature.title)).not.toBeInTheDocument();
  expect(screen.queryByText(third.title)).not.toBeInTheDocument();
  expect(screen.getByText(quiet.title)).toBeInTheDocument();
});

test("整窗对齐在途时「加载更多」被禁用，对齐完成后恢复", async () => {
  let resolveRealign: (page: unknown) => void = () => {};
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 3, offset: 0, limit: 2 })
    .mockImplementationOnce(() => new Promise((resolve) => { resolveRealign = resolve; }));
  mocks.setWishStatus.mockResolvedValue({ ...feature, status: "done" });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.selectOptions(within(cardOf(feature.title)).getByRole("combobox", { name: "处理状态" }), "done");
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));
  // 旧游标还没对齐：此时翻页会跳过挤进当前窗口的条目，所以按钮必须禁用。
  expect(screen.getByRole("button", { name: "正在刷新排序…" })).toBeDisabled();

  resolveRealign({ items: [quiet, { ...feature, status: "done" }], total: 3, offset: 0, limit: 2 });
  await waitFor(() => expect(screen.getByRole("button", { name: "加载更多（还有 1 条）" })).toBeEnabled());
  expect(mocks.listWishes).toHaveBeenCalledTimes(2);
});

test("整窗对齐在途时保存只改标题的编辑，会重启对齐而不是丢掉游标契约", async () => {
  let resolveFirstRealign: (page: unknown) => void = () => {};
  const done = { ...feature, status: "done" as const };
  const unseen = { ...quiet, id: "wish-unseen", title: "原本在第二页", author_id: "user-3", vote_count: 1 };
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 3, offset: 0, limit: 2 })
    .mockImplementationOnce(() => new Promise((resolve) => { resolveFirstRealign = resolve; }))
    .mockResolvedValueOnce({ items: [quiet, unseen], total: 3, offset: 0, limit: 2 });
  mocks.setWishStatus.mockResolvedValue(done);
  mocks.updateWish.mockResolvedValue({ ...done, title: "改了标题" });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.selectOptions(within(cardOf(feature.title)).getByRole("combobox", { name: "处理状态" }), "done");
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));

  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "编辑" }));
  const titleInput = within(card).getByLabelText("修改标题");
  await user.clear(titleInput);
  await user.type(titleInput, "改了标题");
  await user.click(within(card).getByRole("button", { name: "保存修改" }));
  await waitFor(() => expect(mocks.updateWish).toHaveBeenCalledWith(feature.id, { title: "改了标题" }));

  // 第一次对齐被作废，但第二次对齐随即发出；被作废的那次回来也不能解锁翻页。
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(3));
  resolveFirstRealign({ items: [feature, quiet], total: 3, offset: 0, limit: 2 });
  await waitFor(() => expect(
    screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent),
  ).toEqual([quiet.title, "改了标题"]));
  expect(screen.getByRole("button", { name: "加载更多（还有 1 条）" })).toBeEnabled();
  expect(screen.queryByText(feature.title)).not.toBeInTheDocument();
});

test("点赞后的窗口刷新在途时保存另一张卡片的编辑，会重启对齐而不是留下旧游标", async () => {
  const voted = { ...feature, vote_count: 8, voted_by_me: true };
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 3, offset: 0, limit: 2 })
    // 点赞后的窗口刷新：慢响应，在编辑落地前不返回。
    .mockImplementationOnce(() => new Promise(() => {}))
    .mockResolvedValueOnce({ items: [voted, { ...quiet, title: "改了标题" }], total: 3, offset: 0, limit: 2 });
  mocks.toggleWishVote.mockResolvedValue({ wish_id: feature.id, voted: true, vote_count: 8 });
  mocks.updateWish.mockResolvedValue({ ...quiet, title: "改了标题" });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.click(within(cardOf(feature.title)).getByRole("button", { name: /点赞 7/ }));
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));
  // 同一卡片在点赞落定前不能编辑/改状态（整条覆盖会盖掉新票数）；别的卡片可以。
  expect(within(cardOf(feature.title)).getByRole("combobox", { name: "处理状态" })).toBeDisabled();

  const card = cardOf(quiet.title);
  await user.click(within(card).getByRole("button", { name: "编辑" }));
  const titleInput = within(card).getByLabelText("修改标题");
  await user.clear(titleInput);
  await user.type(titleInput, "改了标题");
  await user.click(within(card).getByRole("button", { name: "保存修改" }));
  await waitFor(() => expect(mocks.updateWish).toHaveBeenCalledWith(quiet.id, { title: "改了标题" }));

  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(3));
  expect(mocks.listWishes).toHaveBeenLastCalledWith({ kind: undefined, status: undefined, sort: "priority", offset: 0, limit: 2 });
  expect(await screen.findByText("改了标题")).toBeInTheDocument();
  // 点赞仍在途（它的刷新已被作废但从未返回），按钮保持处理中；票数取自重启对齐带回的服务端行。
  expect(within(cardOf(feature.title)).getByRole("button", { pressed: true })).toHaveTextContent("8");
});

test("同一卡片编辑保存在途时不能改状态，保存完成后恢复", async () => {
  let resolveSave: (item: unknown) => void = () => {};
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.updateWish.mockImplementationOnce(() => new Promise((resolve) => { resolveSave = resolve; }));
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "编辑" }));
  const titleInput = within(card).getByLabelText("修改标题");
  await user.clear(titleInput);
  await user.type(titleInput, "改了标题");
  await user.click(within(card).getByRole("button", { name: "保存修改" }));
  await waitFor(() => expect(mocks.updateWish).toHaveBeenCalledTimes(1));
  expect(within(card).getByRole("combobox", { name: "处理状态" })).toBeDisabled();
  // 别的卡片不受影响。
  expect(within(cardOf(quiet.title)).getByRole("combobox", { name: "处理状态" })).toBeEnabled();

  resolveSave({ ...feature, title: "改了标题" });
  await screen.findByText("改了标题");
  expect(within(cardOf("改了标题")).getByRole("combobox", { name: "处理状态" })).toBeEnabled();
  expect(mocks.setWishStatus).not.toHaveBeenCalled();
});

test("被作废的慢对齐不会拖住翻页：替代它的对齐完成即解锁「加载更多」", async () => {
  const done = { ...feature, status: "done" as const };
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 3, offset: 0, limit: 2 })
    // 改状态触发的对齐：永远不返回（模拟卡死的请求）。
    .mockImplementationOnce(() => new Promise(() => {}))
    .mockResolvedValueOnce({ items: [quiet, { ...done, title: "改了标题" }], total: 3, offset: 0, limit: 2 });
  mocks.setWishStatus.mockResolvedValue(done);
  mocks.updateWish.mockResolvedValue({ ...done, title: "改了标题" });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.selectOptions(within(cardOf(feature.title)).getByRole("combobox", { name: "处理状态" }), "done");
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("button", { name: "正在刷新排序…" })).toBeDisabled();

  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "编辑" }));
  const titleInput = within(card).getByLabelText("修改标题");
  await user.clear(titleInput);
  await user.type(titleInput, "改了标题");
  await user.click(within(card).getByRole("button", { name: "保存修改" }));
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(3));

  await screen.findByText("改了标题");
  await waitFor(() => expect(screen.getByRole("button", { name: "加载更多（还有 1 条）" })).toBeEnabled());
});

test("同一卡片改状态在途时点赞禁用，完成后恢复", async () => {
  let resolveStatus: (item: unknown) => void = () => {};
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.setWishStatus.mockImplementationOnce(() => new Promise((resolve) => { resolveStatus = resolve; }));
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  const card = cardOf(feature.title);
  await user.selectOptions(within(card).getByRole("combobox", { name: "处理状态" }), "in_progress");
  await waitFor(() => expect(mocks.setWishStatus).toHaveBeenCalledTimes(1));
  expect(within(card).getByRole("button", { name: /点赞 7/ })).toBeDisabled();
  expect(within(cardOf(quiet.title)).getByRole("button", { name: /点赞 2/ })).toBeEnabled();
  // 改状态一次只能有一个在途：别的卡片的下拉也禁用，而不是看起来可选却静默不执行。
  expect(within(cardOf(quiet.title)).getByRole("combobox", { name: "处理状态" })).toBeDisabled();

  resolveStatus({ ...feature, status: "in_progress" });
  await waitFor(() => expect(within(cardOf(feature.title)).getByRole("status")).toHaveTextContent("已标记为处理中"));
  await waitFor(() => expect(within(cardOf(feature.title)).getByRole("button", { name: /点赞 7/ })).toBeEnabled());
  expect(mocks.toggleWishVote).not.toHaveBeenCalled();
});

test("慢对齐在途时发布新内容，发布后的整页重拉会清掉对齐状态并解锁翻页", async () => {
  const done = { ...feature, status: "done" as const };
  const fresh = { ...quiet, id: "wish-fresh", title: "刚发布的", author_id: "admin", vote_count: 0 };
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 3, offset: 0, limit: 2 })
    // 改状态触发的对齐：永远不返回。
    .mockImplementationOnce(() => new Promise(() => {}))
    // 发布后的整页重拉。
    .mockResolvedValueOnce({ items: [fresh, quiet], total: 4, offset: 0, limit: 2 });
  mocks.setWishStatus.mockResolvedValue(done);
  mocks.createWish.mockResolvedValue(fresh);
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.selectOptions(within(cardOf(feature.title)).getByRole("combobox", { name: "处理状态" }), "done");
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("button", { name: "正在刷新排序…" })).toBeDisabled();

  await user.type(screen.getByLabelText("标题"), "刚发布的");
  await user.type(screen.getByLabelText("详细说明"), "说明");
  await user.click(screen.getByRole("button", { name: "提交反馈" }));
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(3));

  await screen.findByRole("heading", { level: 2, name: "刚发布的" });
  expect(screen.getByRole("button", { name: "加载更多（还有 2 条）" })).toBeEnabled();
});

test("发布后的重拉在途时删除卡片，会重启窗口替换而不是让新发布的条目消失", async () => {
  const fresh = { ...quiet, id: "wish-fresh", title: "刚发布的", author_id: "user-1", vote_count: 0 };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 2, offset: 0, limit: 50 })
    // 发布后的重拉：慢响应，在删除落地前不返回。
    .mockImplementationOnce(() => new Promise(() => {}))
    // 删除落地后重启的窗口替换：带回新发布的条目。
    .mockResolvedValueOnce({ items: [fresh, quiet], total: 2, offset: 0, limit: 50 });
  mocks.createWish.mockResolvedValue(fresh);
  mocks.deleteWish.mockResolvedValue(undefined);
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.type(screen.getByLabelText("标题"), "刚发布的");
  await user.type(screen.getByLabelText("详细说明"), "说明");
  await user.click(screen.getByRole("button", { name: "提交反馈" }));
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));

  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "删除" }));
  await user.click(within(card).getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(3));
  expect(mocks.listWishes).toHaveBeenLastCalledWith({ kind: undefined, status: undefined, sort: "priority", offset: 0, limit: 1 });

  expect(await screen.findByRole("heading", { level: 2, name: "刚发布的" })).toBeInTheDocument();
  expect(screen.queryByText(feature.title)).not.toBeInTheDocument();
  expect(screen.getByText(quiet.title)).toBeInTheDocument();
});

test("整窗对齐失败后「加载更多」变成重试对齐，成功后才恢复按旧游标之外的位置翻页", async () => {
  const done = { ...feature, status: "done" as const };
  const unseen = { ...quiet, id: "wish-unseen", title: "原本在第二页", author_id: "user-3", vote_count: 1 };
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 3, offset: 0, limit: 2 })
    // 改状态触发的对齐：失败。
    .mockRejectedValueOnce(new Error("offline"))
    // 点击重试：成功，带回挤进窗口的条目。
    .mockResolvedValueOnce({ items: [quiet, unseen], total: 3, offset: 0, limit: 2 });
  mocks.setWishStatus.mockResolvedValue(done);
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.selectOptions(within(cardOf(feature.title)).getByRole("combobox", { name: "处理状态" }), "done");
  await waitFor(() => expect(within(cardOf(feature.title)).getByRole("alert")).toHaveTextContent("已更新，但列表排序暂未刷新"));
  const retry = screen.getByRole("button", { name: "排序刷新失败，点击重试后再加载" });
  expect(retry).toBeEnabled();

  await user.click(retry);
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(3));
  // 重试走的是整窗对齐（offset 0、limit = 可见条数），不是按旧游标翻页。
  expect(mocks.listWishes).toHaveBeenLastCalledWith({ kind: undefined, status: undefined, sort: "priority", offset: 0, limit: 2 });
  await waitFor(() => expect(
    screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent),
  ).toEqual([quiet.title, feature.title]));
  expect(screen.getByRole("button", { name: "加载更多（还有 1 条）" })).toBeEnabled();
});

test("点赞后的窗口刷新失败同样进入游标恢复：加载更多变成重试对齐", async () => {
  const unseen = { ...quiet, id: "wish-unseen", title: "原本在第二页", author_id: "user-3", vote_count: 1 };
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 3, offset: 0, limit: 2 })
    // 点赞后的窗口刷新：失败。
    .mockRejectedValueOnce(new Error("offline"))
    // 点击重试：成功。
    .mockResolvedValueOnce({ items: [{ ...feature, vote_count: 8, voted_by_me: true }, unseen], total: 3, offset: 0, limit: 2 });
  mocks.toggleWishVote.mockResolvedValue({ wish_id: feature.id, voted: true, vote_count: 8 });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.click(within(cardOf(feature.title)).getByRole("button", { name: /点赞 7/ }));
  const retry = await screen.findByRole("button", { name: "排序刷新失败，点击重试后再加载" });
  await waitFor(() => expect(retry).toBeEnabled());

  await user.click(retry);
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(3));
  expect(mocks.listWishes).toHaveBeenLastCalledWith({ kind: undefined, status: undefined, sort: "priority", offset: 0, limit: 2 });
  await waitFor(() => expect(
    screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent),
  ).toEqual([feature.title, unseen.title]));
  expect(screen.getByRole("button", { name: "加载更多（还有 1 条）" })).toBeEnabled();
});

test("改状态在途时切到它将匹配的状态筛选，落地后整窗重拉而不是留着空列表", async () => {
  let resolveStatus: (item: unknown) => void = () => {};
  const done = { ...feature, status: "done" as const };
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 2, offset: 0, limit: 50 })
    // 切到「已完成」筛选：变更还没提交，服务端返回空。
    .mockResolvedValueOnce({ items: [], total: 0, offset: 0, limit: 50 })
    // 变更落地后的整窗重拉：现在匹配了。
    .mockResolvedValueOnce({ items: [done], total: 1, offset: 0, limit: 50 });
  mocks.setWishStatus.mockImplementationOnce(() => new Promise((resolve) => { resolveStatus = resolve; }));
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  await user.selectOptions(within(cardOf(feature.title)).getByRole("combobox", { name: "处理状态" }), "done");
  await waitFor(() => expect(mocks.setWishStatus).toHaveBeenCalledTimes(1));
  await user.selectOptions(screen.getByRole("combobox", { name: "按处理状态筛选" }), "done");
  await screen.findByText("这里还没有内容");
  expect(mocks.listWishes).toHaveBeenCalledTimes(2);

  resolveStatus(done);
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(3));
  expect(mocks.listWishes).toHaveBeenLastCalledWith({ kind: undefined, status: "done", sort: "priority", offset: 0, limit: 50 });
  expect(await screen.findByRole("heading", { level: 2, name: feature.title })).toBeInTheDocument();
  expect(screen.queryByText("这里还没有内容")).not.toBeInTheDocument();
});

test("删除一次只允许一个在途：另一张卡片的删除入口同时禁用", async () => {
  let resolveDelete: () => void = () => {};
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.deleteWish.mockImplementationOnce(() => new Promise<void>((resolve) => { resolveDelete = resolve; }));
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  const other = cardOf(quiet.title);
  await user.click(within(other).getByRole("button", { name: "删除" }));
  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "删除" }));
  await user.click(within(card).getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(mocks.deleteWish).toHaveBeenCalledWith(feature.id));
  // 同一时刻只开一处二次确认；另一张卡片的「删除」入口在删除在途时禁用，而不是可点却静默不执行。
  expect(within(other).getByRole("button", { name: "删除" })).toBeDisabled();

  resolveDelete();
  await waitFor(() => expect(screen.queryByText(feature.title)).not.toBeInTheDocument());
  expect(within(cardOf(quiet.title)).getByRole("button", { name: "删除" })).toBeEnabled();
  expect(mocks.deleteWish).toHaveBeenCalledTimes(1);
});

test("被筛掉的卡片的迟到编辑响应作废了在途对齐时，同样重启对齐", async () => {
  let resolveSave: (item: unknown) => void = () => {};
  const bug1 = { ...feature, id: "wish-bug-1", kind: "bug" as const, title: "崩溃一", author_id: "user-3", vote_count: 5 };
  const bug2 = { ...feature, id: "wish-bug-2", kind: "bug" as const, title: "崩溃二", author_id: "user-3", vote_count: 3 };
  mocks.fetchMe.mockResolvedValue({ id: "admin", username: "管理员", role: "admin" });
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 2, offset: 0, limit: 50 })
    // 切到「问题反馈」筛选。
    .mockResolvedValueOnce({ items: [bug1, bug2], total: 3, offset: 0, limit: 2 })
    // bug1 改为已完成后的对齐：慢响应。
    .mockImplementationOnce(() => new Promise(() => {}))
    // 迟到的编辑响应作废上面那次后重启的对齐。
    .mockResolvedValueOnce({ items: [bug2, { ...bug1, status: "done" }], total: 3, offset: 0, limit: 2 });
  mocks.updateWish.mockImplementationOnce(() => new Promise((resolve) => { resolveSave = resolve; }));
  mocks.setWishStatus.mockResolvedValue({ ...bug1, status: "done" });
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "编辑" }));
  const titleInput = within(card).getByLabelText("修改标题");
  await user.clear(titleInput);
  await user.type(titleInput, "改了标题");
  await user.click(within(card).getByRole("button", { name: "保存修改" }));
  await waitFor(() => expect(mocks.updateWish).toHaveBeenCalledTimes(1));

  await user.click(within(screen.getByRole("group", { name: "筛选许愿墙内容" })).getByRole("button", { name: "问题反馈" }));
  await screen.findByText(bug1.title);
  await user.selectOptions(within(cardOf(bug1.title)).getByRole("combobox", { name: "处理状态" }), "done");
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(3));

  resolveSave({ ...feature, title: "改了标题" });
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(4));
  expect(mocks.listWishes).toHaveBeenLastCalledWith({ kind: "bug", status: undefined, sort: "priority", offset: 0, limit: 2 });
  await waitFor(() => expect(
    screen.getAllByRole("heading", { level: 2 }).map((node) => node.textContent),
  ).toEqual([bug2.title, bug1.title]));
  expect(screen.getByRole("button", { name: "加载更多（还有 1 条）" })).toBeEnabled();
});

test("删除在途时切换筛选，删除落地后按当前筛选重启加载而不是停在加载态", async () => {
  let resolveDelete: () => void = () => {};
  mocks.listWishes.mockReset();
  mocks.listWishes
    .mockResolvedValueOnce({ items: [feature, quiet], total: 2, offset: 0, limit: 50 })
    // 切换筛选触发的首页加载：在删除提交前发出，永远不返回（模拟慢响应）。
    .mockImplementationOnce(() => new Promise(() => {}))
    // 删除落地后重启的加载：带回变更后的结果。
    .mockResolvedValueOnce({ items: [quiet], total: 1, offset: 0, limit: 50 });
  mocks.deleteWish.mockImplementationOnce(() => new Promise<void>((resolve) => { resolveDelete = resolve; }));
  const user = userEvent.setup();
  render(<WishWallPage />);

  await screen.findByText(feature.title);
  const card = cardOf(feature.title);
  await user.click(within(card).getByRole("button", { name: "删除" }));
  await user.click(within(card).getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(mocks.deleteWish).toHaveBeenCalledWith(feature.id));

  await user.click(within(screen.getByRole("group", { name: "筛选许愿墙内容" })).getByRole("button", { name: "功能需求" }));
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(2));
  expect(screen.getByText("正在加载许愿墙…")).toBeInTheDocument();

  resolveDelete();
  await waitFor(() => expect(mocks.listWishes).toHaveBeenCalledTimes(3));
  expect(mocks.listWishes).toHaveBeenLastCalledWith({ kind: "feature", status: undefined, sort: "priority" });
  expect(await screen.findByText(quiet.title)).toBeInTheDocument();
  expect(screen.queryByText("正在加载许愿墙…")).not.toBeInTheDocument();
  expect(screen.queryByText(feature.title)).not.toBeInTheDocument();
});
