import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  fetchAdminUsers: vi.fn(),
  fetchChannels: vi.fn(),
  fetchDays: vi.fn(),
  fetchRecords: vi.fn(),
  fetchRecord: vi.fn(),
  fetchUserActivity: vi.fn(),
  fetchUserAskDetail: vi.fn(),
  fetchUserNotebookSources: vi.fn(),
  fetchUserNotebooks: vi.fn(),
  fetchSystemConfiguration: vi.fn(),
}));

vi.mock("./auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("./admin/usage/api.ts", () => ({
  FORBIDDEN_SENTINEL: "forbidden",
  fetchAdminUsers: mocks.fetchAdminUsers,
}));
vi.mock("./dev/logs/api.ts", () => ({
  fetchChannels: mocks.fetchChannels,
  fetchDays: mocks.fetchDays,
  fetchRecords: mocks.fetchRecords,
  fetchRecord: mocks.fetchRecord,
}));
// 页面现在多了一个「活动」视图 tab（且是默认视图）。本文件钉的是「模型调用」视图
// 的既有行为，所以只需把活动侧的取数挡住，不让它在这些用例里发真实请求。
vi.mock("./dev/logs/activity/api.ts", () => ({
  FORBIDDEN_SENTINEL: "forbidden",
  fetchUserActivity: mocks.fetchUserActivity,
  fetchUserAskDetail: mocks.fetchUserAskDetail,
  fetchUserNotebookSources: mocks.fetchUserNotebookSources,
}));
// 活动 tab 的可见性由 /system/config 下发的能力位决定;不拦这条请求的话它会走
// 真实网络、失败后按「不可用」处理,于是这些用例里的「活动」tab 根本不渲染。
vi.mock("./system-api.ts", () => ({
  fetchSystemConfiguration: mocks.fetchSystemConfiguration,
}));
vi.mock("./admin/usage/notebooks.ts", () => ({
  fetchUserNotebooks: mocks.fetchUserNotebooks,
  notebookStatusLabel: (value: string) => value,
}));

import LogsPage from "./dev/logs/page";

const users = [
  { id: "user-local", username: "admin", role: "admin" },
  { id: "user-target", username: "a00123456", role: "user" },
];

const stats = {
  total: 1,
  filtered: 1,
  by_kind: { chat: 1 },
  by_status: { ok: 1 },
  by_model: {},
  total_tokens: 3,
  latency_ms: { avg: 10, max: 10 },
  malformed_lines: 0,
  facets: { kinds: ["chat"], statuses: ["ok"], models: [] },
};

function summary(id: string, seq: number, preview: string, model: string) {
  return {
    id,
    seq,
    preview,
    model,
    ts: "2026-07-27T10:00:00+08:00",
    kind: "chat",
    status: "ok",
    latency_ms: 10,
    total_tokens: 3,
    attempt: null,
    error: null,
  };
}

function listResponse(records: ReturnType<typeof summary>[], date = "2026-07-27") {
  return {
    channel: "llm",
    date,
    file_exists: true,
    records,
    stats,
    has_more: false,
    truncated: false,
    newest_seq: records[0]?.seq ?? null,
  };
}

function detail(id: string, seq: number, model: string) {
  return {
    id,
    seq,
    model,
    ts: "2026-07-27T10:00:00+08:00",
    kind: "chat",
    status: "ok",
    request: { messages: [] },
    response: { content: "done" },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  window.history.replaceState(null, "", "/dev/logs");
  mocks.fetchMe.mockResolvedValue({ id: "user-local", username: "admin", role: "admin" });
  mocks.fetchSystemConfiguration.mockResolvedValue({
    source_upload_max_bytes: 50 * 1024 * 1024,
    source_upload_max_files_per_batch: 20,
    user_activity_view_enabled: true,
  });
  mocks.fetchAdminUsers.mockResolvedValue(users);
  mocks.fetchChannels.mockResolvedValue({ channels: [{ name: "llm", file: "llm.jsonl", exists: true }] });
  mocks.fetchDays.mockResolvedValue({ channel: "llm", days: [] });
  mocks.fetchUserNotebooks.mockResolvedValue([]);
  mocks.fetchUserActivity.mockResolvedValue({ items: [], has_more: false, next_cursor: null });
});

// 「活动」是默认视图；这些用例钉的是「模型调用」视图，先切过去。
// ⚠ 切换不只影响渲染：模型调用那一侧的取数按 view 门控（默认视图看不见的东西不该
// 花钱取），所以这一步同时是这些用例里第一次 fetchRecords/fetchChannels 的触发点。
async function showModelCalls(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(screen.getByRole("button", { name: "模型调用" }));
}

async function showActivity(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(screen.getByRole("button", { name: "活动" }));
}

function dateSelect(): HTMLSelectElement {
  return document.querySelector(".logview-date-select") as HTMLSelectElement;
}

function dateInput(): HTMLInputElement {
  return document.querySelector(".logview-date-input") as HTMLInputElement;
}

async function ownerSelect(container: HTMLElement): Promise<HTMLSelectElement> {
  await screen.findByRole("option", { name: "a00123456" });
  const select = container.querySelector<HTMLSelectElement>(".logview-owner-select");
  expect(select).not.toBeNull();
  return select as HTMLSelectElement;
}

// F7：两个视图共享同一个 date state，取值集却不同——活动视图是原生日历（日历上的
// 任何一天都能选），「模型调用」是 <select>（options 只有「今天」+ 有日志文件的那
// 几天）。带着一个下拉里没有的日期切回去，控件**不会**空白：React 的受控 <select>
// 找不到匹配 option 时会回落到第一个可选项（ReactDOMSelect.updateOptions），于是
// 下拉写着「今天」、请求发的却是那个没有日志文件的日子 → 列表恒空，而且用户点一下
// 那个已经显示着的「今天」不会产生 change 事件、根本回不去。
//
// ⚠ 判据因此钉在**请求参数**上，不能只看下拉的显示值：坏的和好的实现下它恰好都
// 是「今天」，只断言显示值这条用例会一直假绿（实测过）。显示值只作为「显示的与
// 请求的是同一天」的补充断言留着。
test("活动视图选了下拉里没有的日期后，切回「模型调用」按「今天」取数", async () => {
  mocks.fetchDays.mockResolvedValue({ channel: "llm", days: ["2026-07-26"] });
  mocks.fetchRecords.mockResolvedValue(listResponse([]));
  const user = userEvent.setup();
  render(<LogsPage />);

  // 先走一遍下拉，证明 days 确实已经进 state（否则「不在 days 里」会因为 days 还是
  // 空数组而恒真，这条用例就退化成一句无条件断言）。
  await showModelCalls(user);
  await waitFor(() => expect(dateSelect().options).toHaveLength(2));

  await showActivity(user);
  fireEvent.change(dateInput(), { target: { value: "2026-08-04" } }); // 没有日志文件的一天
  await showModelCalls(user);

  await waitFor(() => {
    expect(mocks.fetchRecords).toHaveBeenLastCalledWith(
      "llm",
      expect.objectContaining({ date: "" }),
    );
  });
  expect(dateSelect().value).toBe("");
});

// 反向对照：归一不是无条件重置。日期本来就在下拉取值集里时必须原样保留，否则
// 「看某天的模型调用 → 瞄一眼那天的活动 → 切回来」会把用户选的那天悄悄丢掉。
test("日期本来就在下拉取值集里时，切回「模型调用」保持不变", async () => {
  mocks.fetchDays.mockResolvedValue({ channel: "llm", days: ["2026-08-04"] });
  mocks.fetchRecords.mockResolvedValue(listResponse([]));
  const user = userEvent.setup();
  render(<LogsPage />);

  await showModelCalls(user);
  await waitFor(() => expect(dateSelect().options).toHaveLength(2));

  await showActivity(user);
  fireEvent.change(dateInput(), { target: { value: "2026-08-04" } });
  await showModelCalls(user);

  await waitFor(() => {
    expect(mocks.fetchRecords).toHaveBeenLastCalledWith(
      "llm",
      expect.objectContaining({ date: "2026-08-04" }),
    );
  });
  expect(dateSelect().value).toBe("2026-08-04");
});

test("管理员跨用户打开日志详情时使用该记录绑定的 owner/date/seq", async () => {
  mocks.fetchRecords.mockImplementation((_channel, params) => Promise.resolve(
    params.owner === "user-target"
      ? listResponse([summary("llm-target", 17, "target preview", "model-target")])
      : listResponse([]),
  ));
  mocks.fetchRecord.mockResolvedValue(detail("llm-target", 17, "model-target"));
  const user = userEvent.setup();
  const { container } = render(<LogsPage />);
  await showModelCalls(user);

  await user.selectOptions(await ownerSelect(container), "user-target");
  await user.click(await screen.findByRole("button", { name: /target preview/ }));

  await waitFor(() => {
    expect(mocks.fetchRecord).toHaveBeenCalledWith(
      "llm",
      "llm-target",
      "2026-07-27",
      17,
      "user-target",
    );
  });
});

test("切换用户后忽略较晚返回的旧用户列表", async () => {
  const oldList = deferred<ReturnType<typeof listResponse>>();
  mocks.fetchRecords.mockImplementation((_channel, params) => (
    params.owner === "user-target"
      ? Promise.resolve(listResponse([summary("llm-new", 22, "new owner preview", "model-new")]))
      : oldList.promise
  ));
  const user = userEvent.setup();
  const { container } = render(<LogsPage />);
  await showModelCalls(user);

  await user.selectOptions(await ownerSelect(container), "user-target");
  expect(await screen.findByText("new owner preview")).toBeInTheDocument();

  await act(async () => {
    oldList.resolve(listResponse([summary("llm-old", 11, "old owner preview", "model-old")]));
    await oldList.promise;
  });

  expect(screen.getByText("new owner preview")).toBeInTheDocument();
  expect(screen.queryByText("old owner preview")).not.toBeInTheDocument();
});

test("切换用户后忽略较晚返回的旧日志详情", async () => {
  const oldDetail = deferred<ReturnType<typeof detail>>();
  mocks.fetchRecords.mockImplementation((_channel, params) => Promise.resolve(
    params.owner === "user-target"
      ? listResponse([summary("llm-new", 22, "new detail preview", "model-new")])
      : listResponse([summary("llm-old", 11, "old detail preview", "model-old")]),
  ));
  mocks.fetchRecord.mockImplementation((_channel, id) => (
    id === "llm-old"
      ? oldDetail.promise
      : Promise.resolve(detail("llm-new", 22, "model-new"))
  ));
  const user = userEvent.setup();
  const { container } = render(<LogsPage />);
  await showModelCalls(user);

  await user.click(await screen.findByRole("button", { name: /old detail preview/ }));
  await user.selectOptions(await ownerSelect(container), "user-target");
  await user.click(await screen.findByRole("button", { name: /new detail preview/ }));
  expect(await screen.findByText("model: model-new")).toBeInTheDocument();

  await act(async () => {
    oldDetail.resolve(detail("llm-old", 11, "model-old"));
    await oldDetail.promise;
  });

  expect(screen.getByText("model: model-new")).toBeInTheDocument();
  expect(screen.queryByText("model: model-old")).not.toBeInTheDocument();
});

test("切换用户后忽略较晚返回的旧频道和日期", async () => {
  const oldChannels = deferred<{ channels: { name: string; file: string; exists: boolean }[] }>();
  const oldDays = deferred<{ channel: string; days: string[] }>();
  mocks.fetchChannels.mockImplementation((owner) => (
    owner === "user-target"
      ? Promise.resolve({ channels: [{ name: "llm", file: "llm.jsonl", exists: true }] })
      : oldChannels.promise
  ));
  mocks.fetchDays.mockImplementation((_channel, owner) => (
    owner === "user-target"
      ? Promise.resolve({ channel: "llm", days: ["2026-07-26"] })
      : oldDays.promise
  ));
  mocks.fetchRecords.mockResolvedValue(listResponse([]));
  const user = userEvent.setup();
  const { container } = render(<LogsPage />);
  await showModelCalls(user);

  await user.selectOptions(await ownerSelect(container), "user-target");
  expect(await screen.findByRole("option", { name: "2026-07-26" })).toBeInTheDocument();
  expect(await screen.findByRole("button", { name: /LLM/ })).toBeInTheDocument();

  await act(async () => {
    oldChannels.resolve({ channels: [{ name: "events", file: "events.jsonl", exists: true }] });
    oldDays.resolve({ channel: "llm", days: ["2026-07-25"] });
    await Promise.all([oldChannels.promise, oldDays.promise]);
  });

  expect(screen.getByRole("option", { name: "2026-07-26" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "2026-07-25" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /LLM/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /EVENTS/ })).not.toBeInTheDocument();
});

test("筛选刷新开始后旧批次不再提供加载更多", async () => {
  const filteredList = deferred<ReturnType<typeof listResponse>>();
  let calls = 0;
  mocks.fetchRecords.mockImplementation(() => {
    calls += 1;
    return calls === 1
      ? Promise.resolve({
          ...listResponse([summary("llm-old", 11, "old filter preview", "model-old")]),
          has_more: true,
        })
      : filteredList.promise;
  });
  const user = userEvent.setup();
  const { container } = render(<LogsPage />);
  await showModelCalls(user);

  expect(await screen.findByRole("button", { name: "加载更多" })).toBeInTheDocument();
  const kindSelect = container.querySelector<HTMLSelectElement>(".logview-filters select");
  expect(kindSelect).not.toBeNull();
  await user.selectOptions(kindSelect as HTMLSelectElement, "chat");

  await waitFor(() => {
    expect(screen.queryByRole("button", { name: "加载更多" })).not.toBeInTheDocument();
  });

  await act(async () => {
    filteredList.resolve(listResponse([]));
    await filteredList.promise;
  });
});
