import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  listWishes: vi.fn(),
  toggleWishVote: vi.fn(),
}));

vi.mock("../../app/wish-wall-api.ts", () => ({
  listWishes: mocks.listWishes,
  toggleWishVote: mocks.toggleWishVote,
}));

import {
  WAITING_WISH_KIND_LIMIT,
  WAITING_WISH_ROTATION_MS,
  WaitingWishCarousel,
} from "../../app/waiting-wish-carousel";

const feature = {
  id: "wish-feature",
  kind: "feature" as const,
  title: "希望支持批量标签",
  content: "完整说明",
  author_id: "user-1",
  author_name: "小林",
  status: "open" as const,
  vote_count: 7,
  voted_by_me: false,
  created_at: "2026-08-31T10:00:00+08:00",
  updated_at: "2026-08-31T10:00:00+08:00",
};

const bug = {
  ...feature,
  id: "wish-bug",
  kind: "bug" as const,
  title: "修复表格滚动问题",
  vote_count: 3,
  created_at: "2026-08-30T10:00:00+08:00",
};

beforeEach(() => {
  mocks.listWishes.mockImplementation(({ kind }: { kind: string }) => Promise.resolve({
    items: kind === "bug" ? [bug] : [feature],
    total: 1,
    offset: 0,
    limit: 50,
  }));
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

test("等待轮播分别读取可投票类型并按优先级展示", async () => {
  render(<WaitingWishCarousel />);

  expect(await screen.findByText(feature.title)).toBeInTheDocument();
  expect(mocks.listWishes).toHaveBeenCalledTimes(2);
  expect(mocks.listWishes).toHaveBeenCalledWith({
    kind: "bug",
    sort: "priority",
    limit: WAITING_WISH_KIND_LIMIT,
  });
  expect(mocks.listWishes).toHaveBeenCalledWith({
    kind: "feature",
    sort: "priority",
    limit: WAITING_WISH_KIND_LIMIT,
  });
  expect(screen.getByRole("link", { name: "查看完整说明" })).toHaveAttribute("href", "/wishes");
  expect(screen.queryByText(feature.content)).not.toBeInTheDocument();
});

test("轮播自动切换，悬停与焦点重叠时持续暂停且支持手动切换", async () => {
  vi.useFakeTimers();
  render(<WaitingWishCarousel />);
  await act(async () => {});
  expect(screen.getByText(feature.title)).toBeInTheDocument();

  act(() => { vi.advanceTimersByTime(WAITING_WISH_ROTATION_MS); });
  expect(screen.getByText(bug.title)).toBeInTheDocument();

  const carousel = screen.getByRole("region", { name: "等待时浏览许愿墙" });
  fireEvent.mouseEnter(carousel);
  act(() => { vi.advanceTimersByTime(WAITING_WISH_ROTATION_MS); });
  expect(screen.getByText(bug.title)).toBeInTheDocument();

  const previous = screen.getByRole("button", { name: "上一个愿望" });
  fireEvent.focus(previous);
  fireEvent.mouseLeave(carousel);
  act(() => { vi.advanceTimersByTime(WAITING_WISH_ROTATION_MS); });
  expect(screen.getByText(bug.title)).toBeInTheDocument();

  fireEvent.click(previous);
  expect(screen.getByText(feature.title)).toBeInTheDocument();

  fireEvent.blur(previous);
  act(() => { vi.advanceTimersByTime(WAITING_WISH_ROTATION_MS); });
  expect(screen.getByText(bug.title)).toBeInTheDocument();
});

test("运行中切换减少动态效果会立即停止或恢复轮播", async () => {
  vi.useFakeTimers();
  let reduced = false;
  let preferenceListener: (() => void) | null = null;
  vi.stubGlobal("matchMedia", vi.fn(() => ({
    get matches() { return reduced; },
    media: "(prefers-reduced-motion: reduce)",
    onchange: null,
    addEventListener: (_type: string, listener: () => void) => { preferenceListener = listener; },
    removeEventListener: () => { preferenceListener = null; },
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })));
  render(<WaitingWishCarousel />);
  await act(async () => {});

  act(() => { vi.advanceTimersByTime(WAITING_WISH_ROTATION_MS); });
  expect(screen.getByText(bug.title)).toBeInTheDocument();

  reduced = true;
  act(() => { preferenceListener?.(); });
  act(() => { vi.advanceTimersByTime(WAITING_WISH_ROTATION_MS); });
  expect(screen.getByText(bug.title)).toBeInTheDocument();

  reduced = false;
  act(() => { preferenceListener?.(); });
  act(() => { vi.advanceTimersByTime(WAITING_WISH_ROTATION_MS); });
  expect(screen.getByText(feature.title)).toBeInTheDocument();
});

test("等待卡投票会调用真实许愿接口并在控件旁显示结果", async () => {
  mocks.toggleWishVote.mockResolvedValue({
    wish_id: feature.id,
    voted: true,
    vote_count: 8,
  });
  const user = userEvent.setup();
  render(<WaitingWishCarousel />);
  await screen.findByText(feature.title);

  await user.click(screen.getByRole("button", { name: /赞同.*当前 7 人赞同/ }));

  await waitFor(() => expect(mocks.toggleWishVote).toHaveBeenCalledWith(feature.id));
  expect(screen.getByRole("button", { name: /取消赞同.*当前 8 人赞同/ })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("status")).toHaveTextContent("已赞同");
});

test("投票改变优先级后重新排序并保持当前愿望可见", async () => {
  mocks.toggleWishVote.mockResolvedValue({
    wish_id: bug.id,
    voted: true,
    vote_count: 9,
  });
  const user = userEvent.setup();
  render(<WaitingWishCarousel />);
  await screen.findByText(feature.title);

  await user.click(screen.getByRole("button", { name: "下一个愿望" }));
  await user.click(screen.getByRole("button", { name: /赞同.*当前 3 人赞同/ }));

  expect(await screen.findByRole("button", { name: /取消赞同.*当前 9 人赞同/ })).toBeInTheDocument();
  expect(screen.getByText(bug.title)).toBeInTheDocument();
  expect(screen.getByText("1 / 2")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "下一个愿望" }));
  expect(screen.getByText(feature.title)).toBeInTheDocument();
});

function mockWishLists(bugs: unknown[], features: unknown[]) {
  mocks.listWishes.mockImplementation(({ kind }: { kind: string }) => {
    const items = kind === "bug" ? bugs : features;
    return Promise.resolve({ items, total: items.length, offset: 0, limit: WAITING_WISH_KIND_LIMIT });
  });
}

const closedBug = {
  ...bug,
  id: "wish-bug-done",
  title: "已修好的导出问题",
  status: "done" as const,
  vote_count: 40,
};

const inProgressFeature = {
  ...feature,
  id: "wish-feature-progress",
  title: "正在做的目录导航",
  status: "in_progress" as const,
  vote_count: 5,
};

const declinedFeature = {
  ...feature,
  id: "wish-feature-declined",
  title: "不采纳的皮肤切换",
  status: "declined" as const,
  vote_count: 30,
};

test("轮播只出现待处理与进行中的愿望，已完成和不采纳即使票数更高也不出现且不多发请求", async () => {
  vi.useFakeTimers();
  const fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
  mockWishLists([closedBug, bug], [declinedFeature, feature, inProgressFeature]);
  render(<WaitingWishCarousel />);
  await act(async () => {});

  const seen: string[] = [];
  for (let step = 0; step < 7; step += 1) {
    const title = screen.getByRole("article").querySelector("strong[title]")?.getAttribute("title");
    seen.push(title ?? "");
    expect(screen.getByText(`${(step % 3) + 1} / 3`)).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(WAITING_WISH_ROTATION_MS); });
  }

  expect(seen).toEqual([
    feature.title, inProgressFeature.title, bug.title,
    feature.title, inProgressFeature.title, bug.title,
    feature.title,
  ]);
  expect(screen.queryByText(closedBug.title)).not.toBeInTheDocument();
  expect(screen.queryByText(declinedFeature.title)).not.toBeInTheDocument();
  expect(mocks.listWishes).toHaveBeenCalledTimes(2);
  expect(fetchSpy).not.toHaveBeenCalled();
});

test("全部愿望都已完成或不采纳时显示空态，不起计时器也不多发请求", async () => {
  vi.useFakeTimers();
  const fetchSpy = vi.fn();
  vi.stubGlobal("fetch", fetchSpy);
  mockWishLists([closedBug], [declinedFeature]);
  render(<WaitingWishCarousel />);
  await act(async () => {});

  expect(screen.getByText("暂无待处理或处理中的愿望，欢迎去许愿墙分享想法。")).toBeInTheDocument();
  expect(screen.queryByRole("article")).not.toBeInTheDocument();
  expect(screen.queryByRole("navigation", { name: "切换愿望" })).not.toBeInTheDocument();
  expect(vi.getTimerCount()).toBe(0);
  act(() => { vi.advanceTimersByTime(WAITING_WISH_ROTATION_MS * 3); });
  expect(screen.getByText("暂无待处理或处理中的愿望，欢迎去许愿墙分享想法。")).toBeInTheDocument();
  expect(mocks.listWishes).toHaveBeenCalledTimes(2);
  expect(fetchSpy).not.toHaveBeenCalled();
});

test("重试加载后过滤只剩一条时直接展示且不出现切换", async () => {
  mocks.listWishes.mockRejectedValueOnce(new Error("offline"));
  const user = userEvent.setup();
  render(<WaitingWishCarousel />);
  expect(await screen.findByText("许愿墙暂时加载失败")).toBeInTheDocument();

  mockWishLists([closedBug, bug], [declinedFeature]);
  await user.click(screen.getByRole("button", { name: "重试" }));

  expect(await screen.findByText(bug.title)).toBeInTheDocument();
  expect(screen.queryByRole("navigation", { name: "切换愿望" })).not.toBeInTheDocument();
  expect(screen.queryByText(closedBug.title)).not.toBeInTheDocument();
  expect(screen.queryByText(declinedFeature.title)).not.toBeInTheDocument();
});

test("轮播加载失败可就地重试且不冒充生成任务失败", async () => {
  mocks.listWishes.mockRejectedValueOnce(new Error("offline"));
  const user = userEvent.setup();
  render(<WaitingWishCarousel />);

  expect(await screen.findByText("许愿墙暂时加载失败")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByText(feature.title)).toBeInTheDocument();
});
