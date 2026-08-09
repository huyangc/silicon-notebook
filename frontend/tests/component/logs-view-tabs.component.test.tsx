import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { dayRange } from "../../app/dev/logs/date.ts";

const mocks = vi.hoisted(() => ({
  fetchChannels: vi.fn(),
  fetchDays: vi.fn(),
  fetchRecord: vi.fn(),
  fetchRecords: vi.fn(),
  fetchMe: vi.fn(),
  fetchAdminUsers: vi.fn(),
  fetchSystemConfiguration: vi.fn(),
  fetchUserActivity: vi.fn(),
  fetchUserAskDetail: vi.fn(),
  fetchUserNotebookSources: vi.fn(),
  fetchUserNotebooks: vi.fn(),
}));

vi.mock("../../app/dev/logs/api.ts", () => ({
  fetchChannels: mocks.fetchChannels,
  fetchDays: mocks.fetchDays,
  fetchRecord: mocks.fetchRecord,
  fetchRecords: mocks.fetchRecords,
}));
vi.mock("../../app/auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("../../app/system-api.ts", () => ({
  fetchSystemConfiguration: mocks.fetchSystemConfiguration,
}));
vi.mock("../../app/admin/usage/api.ts", () => ({
  FORBIDDEN_SENTINEL: "forbidden",
  fetchAdminUsers: mocks.fetchAdminUsers,
}));
vi.mock("../../app/dev/logs/activity/api.ts", () => ({
  FORBIDDEN_SENTINEL: "forbidden",
  fetchUserActivity: mocks.fetchUserActivity,
  fetchUserAskDetail: mocks.fetchUserAskDetail,
  fetchUserNotebookSources: mocks.fetchUserNotebookSources,
}));
vi.mock("../../app/admin/usage/notebooks.ts", () => ({
  fetchUserNotebooks: mocks.fetchUserNotebooks,
  notebookStatusLabel: (value: string) => value,
}));

import LogsPage from "../../app/dev/logs/page";

function primeMocks(days: string[] = [], newestSeq: number | null = null) {
  mocks.fetchMe.mockResolvedValue({
    id: "user-1", email: "", display_name: "", role: "user", username: "a00123456",
  });
  mocks.fetchSystemConfiguration.mockResolvedValue({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    user_activity_view_enabled: true,
  });
  mocks.fetchChannels.mockResolvedValue({ channels: [{ name: "llm", count: 0 }] });
  mocks.fetchDays.mockResolvedValue({ channel: "llm", days });
  mocks.fetchRecords.mockResolvedValue({
    channel: "llm",
    date: "",
    file_exists: true,
    records: [],
    stats: {
      total: 0,
      filtered: 0,
      by_kind: {},
      by_status: {},
      by_model: {},
      total_tokens: 0,
      latency_ms: { avg: 0, max: 0 },
      malformed_lines: 0,
      facets: { kinds: [], statuses: [], models: [] },
    },
    has_more: false,
    truncated: false,
    newest_seq: newestSeq,
  });
  mocks.fetchUserNotebooks.mockResolvedValue([]);
  mocks.fetchUserActivity.mockResolvedValue({ items: [], has_more: false, next_cursor: null });
}

function dateSelect(): HTMLSelectElement {
  return document.querySelector(".logview-date-select") as HTMLSelectElement;
}

function dateInput(): HTMLInputElement {
  return document.querySelector(".logview-date-input") as HTMLInputElement;
}

// 页面把 `?view=` 写回地址栏，挂载时又会读回来。不重置的话，上一条用例点过
// 「模型调用」之后，下一条会从 llm 视图起步——而那正是本文件要区分的东西。
beforeEach(() => {
  window.history.replaceState(null, "", "/dev/logs");
});

afterEach(() => {
  vi.useRealTimers();
});


test("默认进「活动」视图，日期为空即「全部时间」，且不下发时间区间", async () => {
  primeMocks();
  const { container } = render(<LogsPage />);

  await waitFor(() => expect(mocks.fetchUserActivity).toHaveBeenCalled());
  expect(container.querySelector(".activity-scope")).not.toBeNull();
  expect(container.querySelector(".activity-stream")).not.toBeNull();
  expect(container.querySelector(".activity-detail")).not.toBeNull();
  // 活动视图用原生日期选择器（取值来自库里的 created_at，不是日志文件分天）；
  // 空值旁边写明它读作「全部时间」。
  expect(dateSelect()).toBeNull();
  expect(dateInput().value).toBe("");
  expect(screen.getByText("全部时间")).toBeInTheDocument();
  expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith("user-1", {
    notebookId: undefined,
    since: undefined,
    until: undefined,
    limit: 50,
  });
});


// F3（效率是一等约束）：「模型调用」那一侧的取数按 view 门控。默认视图是「活动」，
// 挂载时那套请求（含带全量 stats 聚合的 fetchRecords）用户一眼看不到。
// ⚠ 门控只影响**什么时候发**：切过去时该发的一次照发（本用例后半段就是正向对照，
// 少了它，「永远不发」这种把门控做过头的实现同样能让前半段通过）。
test("停在「活动」视图时不为看不见的模型调用视图取数", async () => {
  const user = userEvent.setup();
  primeMocks();
  render(<LogsPage />);

  await waitFor(() => expect(mocks.fetchUserActivity).toHaveBeenCalled());
  expect(mocks.fetchRecords).not.toHaveBeenCalled();
  expect(mocks.fetchChannels).not.toHaveBeenCalled();

  await user.click(screen.getByRole("button", { name: "模型调用" }));

  await waitFor(() => expect(mocks.fetchRecords).toHaveBeenCalledTimes(1));
  expect(mocks.fetchChannels).toHaveBeenCalledTimes(1);
});


// 同一条门控的第二个入口：改日期是活动视图里最常做的事，它不该顺带再发一遍
// 模型调用那一侧的请求。
test("在「活动」视图改日期不会触发模型调用侧的取数", async () => {
  primeMocks();
  render(<LogsPage />);
  await waitFor(() => expect(mocks.fetchUserActivity).toHaveBeenCalled());

  fireEvent.change(dateInput(), { target: { value: "2026-08-04" } });

  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith("user-1", {
      notebookId: undefined,
      // 边界带浏览器 UTC 偏移(F1)。这里不写死 `+08:00`:偏移随跑测的机器时区变,
      // UTC 的 CI 与 UTC+8 的开发机期望值不同。dayRange 自己的语义(含偏移拼写、
      // 跨月/闰年进位、非法日期回环)由 date.test.mjs 在**固定时区**下逐条钉住;
      // 这条用例钉的是**接线** —— 页面确实把选中的那天交给了 dayRange,并把结果
      // 原样下发。
      ...dayRange("2026-08-04"),
      limit: 50,
    });
  });
  expect(mocks.fetchRecords).not.toHaveBeenCalled();
});


// 现有排障流程零回归:切到「模型调用」后，facet 下拉 / 搜索框 / 刷新 / 自动刷新
// 一件不少，日期下拉回到按天分文件的取值集，空值也读回「今天」。
test("切到「模型调用」保留既有的过滤、搜索与自动刷新控件", async () => {
  const user = userEvent.setup();
  primeMocks();
  const { container } = render(<LogsPage />);

  await user.click(screen.getByRole("button", { name: "模型调用" }));
  await waitFor(() => expect(mocks.fetchRecords).toHaveBeenCalled());

  expect(container.querySelector(".activity-scope")).toBeNull();
  expect(container.querySelector(".logview-filters")).not.toBeNull();
  expect(screen.getByPlaceholderText("搜索 prompt / response / error…")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /刷新/ })).toBeInTheDocument();
  expect(screen.getByRole("checkbox")).toBeInTheDocument();
  expect(container.querySelectorAll(".logview-filters select")).toHaveLength(3);
  expect(dateInput()).toBeNull();
  expect(dateSelect().options[0].textContent).toBe("今天");
});


// 「历史(未分天)」是日志文件的分区，活动流读的是库里的 created_at，没有这个概念。
// 切过去时归一成空值（= 全部时间），日期选择器留空；切回来仍是「今天」。
test("从「模型调用」的 legacy 切到「活动」时归一成全部时间", async () => {
  const user = userEvent.setup();
  primeMocks(["legacy", "2026-08-01"]);
  render(<LogsPage />);

  await user.click(screen.getByRole("button", { name: "模型调用" }));
  await waitFor(() => expect(dateSelect().options).toHaveLength(3));
  await user.selectOptions(dateSelect(), "legacy");
  expect(dateSelect().value).toBe("legacy");

  await user.click(screen.getByRole("button", { name: "活动" }));

  expect(dateInput().value).toBe("");
  expect(screen.getByText("全部时间")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "模型调用" }));
  expect(dateSelect().value).toBe("");
});


// F4：活动视图的日期取值集不再跟日志文件分天共用。日历上的任何一天都能选——
// 包括「今天传了 5 份来源、0 次模型调用」那种今天没有日志文件的日子。
test("在「活动」视图选日历上的某天时下发半开区间，与是否有日志文件无关", async () => {
  primeMocks([]); // 一天日志文件都没有
  render(<LogsPage />);
  await waitFor(() => expect(mocks.fetchUserActivity).toHaveBeenCalled());

  fireEvent.change(dateInput(), { target: { value: "2026-08-04" } });

  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith("user-1", {
      notebookId: undefined,
      // 边界带浏览器 UTC 偏移(F1)。这里不写死 `+08:00`:偏移随跑测的机器时区变,
      // UTC 的 CI 与 UTC+8 的开发机期望值不同。dayRange 自己的语义(含偏移拼写、
      // 跨月/闰年进位、非法日期回环)由 date.test.mjs 在**固定时区**下逐条钉住;
      // 这条用例钉的是**接线** —— 页面确实把选中的那天交给了 dayRange,并把结果
      // 原样下发。
      ...dayRange("2026-08-04"),
      limit: 50,
    });
  });

  // 清除回「全部时间」。
  fireEvent.click(screen.getByRole("button", { name: "全部时间" }));
  await waitFor(() => {
    expect(mocks.fetchUserActivity).toHaveBeenLastCalledWith("user-1", {
      notebookId: undefined,
      since: undefined,
      until: undefined,
      limit: 50,
    });
  });
});


// F3 的第二半：自动刷新的复选框只长在「模型调用」的过滤条上。留着勾选态切到活动
// 视图，轮询会继续每 5 秒打日志接口，而**用户没有任何办法关掉它**（控件已经不在
// 屏幕上）。前半段是正向对照：不先证明轮询真的在跑，后半段的「不再跑」毫无意义。
test("切到「活动」后停止轮询模型调用日志，且不留下关不掉的勾选态", async () => {
  // ⚠ 假定时器必须在轮询的 setInterval 建立**之前**装好，否则那个 interval 是真的、
  // 推进假时钟不会碰它。整条用例因此全程假时钟，`waitFor` 换成显式推进 0ms 冲刷
  // 微任务（waitFor 自己的轮询在假时钟下永远不会到点）。
  vi.useFakeTimers();
  primeMocks([], 42);
  // ⚠ 交互一律走 fireEvent：userEvent 的指针序列在假时钟下会挂住（它自己等的那个
  // setTimeout 永远不到点，`delay: null` 也救不了）。这里要证的是定时器行为，
  // 用同步事件把交互这一半彻底移出等式。
  const flush = () => act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  const clickTab = async (name: string) => {
    fireEvent.click(screen.getByRole("button", { name }));
    await flush();
  };

  render(<LogsPage />);
  await flush();
  await clickTab("模型调用");
  expect(mocks.fetchRecords).toHaveBeenCalled();
  fireEvent.click(screen.getByRole("checkbox"));
  await flush();

  const beforePoll = mocks.fetchRecords.mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(6000);
  });
  expect(mocks.fetchRecords.mock.calls.length).toBeGreaterThan(beforePoll);

  await clickTab("活动");
  const afterSwitch = mocks.fetchRecords.mock.calls.length;
  await act(async () => {
    await vi.advanceTimersByTimeAsync(30000);
  });
  expect(mocks.fetchRecords.mock.calls.length).toBe(afterSwitch);

  await clickTab("模型调用");
  expect(screen.getByRole("checkbox")).not.toBeChecked();
});


// P2-1(codex 第 3 轮):USER_ACTIVITY_VIEW_ENABLED=false 时前端不该默认进一个
// 三个端点全 404 的视图。能力位经 /system/config 下发,页面必须据此隐藏「活动」
// tab 并把默认视图落回「模型调用」。
test("能力位关闭时不渲染「活动」tab，默认视图落在「模型调用」", async () => {
  primeMocks();
  mocks.fetchSystemConfiguration.mockResolvedValue({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    user_activity_view_enabled: false,
  });
  render(<LogsPage />);

  await waitFor(() => expect(mocks.fetchRecords).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: "活动" })).toBeNull();
  expect(screen.getByRole("button", { name: "模型调用" })).toHaveClass("active");
  // 活动视图自己的三个端点一次都不该被打——不是「隐藏了 tab 但还是取了数」。
  expect(mocks.fetchUserActivity).not.toHaveBeenCalled();
  expect(mocks.fetchUserNotebooks).not.toHaveBeenCalled();
});


// 深链 `?view=activity` 是手工构造或历史书签,不能因为部署关掉了能力位就打开
// 一个三个端点全 404 的视图——必须归一到「模型调用」，且改写回地址栏。
test("能力位关闭时?view=activity深链被归一成模型调用视图", async () => {
  window.history.replaceState(null, "", "/dev/logs?view=activity");
  primeMocks();
  mocks.fetchSystemConfiguration.mockResolvedValue({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    user_activity_view_enabled: false,
  });
  render(<LogsPage />);

  await waitFor(() => expect(mocks.fetchRecords).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: "活动" })).toBeNull();
  expect(mocks.fetchUserActivity).not.toHaveBeenCalled();
  await waitFor(() => {
    expect(new URLSearchParams(window.location.search).get("view")).toBe("llm");
  });
});


// 能力位缺失或取不到(旧后端、网络错误)时必须按 true 处理——「活动」tab 仍在,
// 不能因为一次瞬时失败就藏掉一个其实可用的视图。system-api.test.mjs 已经钉住
// fetchSystemConfiguration 自己对「字段缺失」的归一(defaults to true);这里补的
// 是页面对「这次调用整个失败/挂起」的兜底路径,同一份默认 state 覆盖两种情形。
test("拿不到能力位时按不可用处理，不挂载活动视图", async () => {
  // 「问不出来」与「没有这个特性」在可用性上是同一件事:能力位与三个活动端点
  // 是同一次改动一起上线的,旧后端两样都没有。按 true 兜底会默认打开一个请求
  // 全 404 的 tab —— 与开关显式关闭时的失败形态一字不差。
  primeMocks();
  mocks.fetchSystemConfiguration.mockRejectedValue(new Error("network"));
  render(<LogsPage />);

  // 等「模型调用」侧真的取过数,证明页面已经渲染完、不是还停在挂载前——否则
  // 下面两条断言会因为"什么都还没发生"而通过。
  await waitFor(() => expect(mocks.fetchRecords).toHaveBeenCalled());
  expect(mocks.fetchUserActivity).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "活动" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "模型调用" })).toHaveClass("active");
});
