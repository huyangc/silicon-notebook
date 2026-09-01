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

test("轮播加载失败可就地重试且不冒充生成任务失败", async () => {
  mocks.listWishes.mockRejectedValueOnce(new Error("offline"));
  const user = userEvent.setup();
  render(<WaitingWishCarousel />);

  expect(await screen.findByText("许愿墙暂时加载失败")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "重试" }));
  expect(await screen.findByText(feature.title)).toBeInTheDocument();
});
