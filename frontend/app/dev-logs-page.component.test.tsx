import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  fetchAdminUsers: vi.fn(),
  fetchChannels: vi.fn(),
  fetchDays: vi.fn(),
  fetchRecords: vi.fn(),
  fetchRecord: vi.fn(),
}));

vi.mock("./auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("./admin/usage/api.ts", () => ({ fetchAdminUsers: mocks.fetchAdminUsers }));
vi.mock("./dev/logs/api.ts", () => ({
  fetchChannels: mocks.fetchChannels,
  fetchDays: mocks.fetchDays,
  fetchRecords: mocks.fetchRecords,
  fetchRecord: mocks.fetchRecord,
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
  mocks.fetchAdminUsers.mockResolvedValue(users);
  mocks.fetchChannels.mockResolvedValue({ channels: [{ name: "llm", file: "llm.jsonl", exists: true }] });
  mocks.fetchDays.mockResolvedValue({ channel: "llm", days: [] });
});

async function ownerSelect(container: HTMLElement): Promise<HTMLSelectElement> {
  await screen.findByRole("option", { name: "a00123456" });
  const select = container.querySelector<HTMLSelectElement>(".logview-owner-select");
  expect(select).not.toBeNull();
  return select as HTMLSelectElement;
}

test("管理员跨用户打开日志详情时使用该记录绑定的 owner/date/seq", async () => {
  mocks.fetchRecords.mockImplementation((_channel, params) => Promise.resolve(
    params.owner === "user-target"
      ? listResponse([summary("llm-target", 17, "target preview", "model-target")])
      : listResponse([]),
  ));
  mocks.fetchRecord.mockResolvedValue(detail("llm-target", 17, "model-target"));
  const user = userEvent.setup();
  const { container } = render(<LogsPage />);

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
