// 「AI 对这个库的理解」面板的渲染守卫(P1-T7)。
//
// 钉的是「后端契约在界面上真的兑现了」这一层——纯逻辑那一半在
// tests/unit/agent-profile-model.test.mjs:
//   · 角色:共享那一档 `can_edit_base=false` 时**只读渲染而不是隐藏**;本人那一份
//     只读成员也能改;
//   · 编辑:保存带读回来的 `expected_revision`;超限直接禁用保存(不发必然 422 的请求);
//   · 清空:两步确认,第一下不发请求;
//   · 重新整理:点完立刻不可点并换成「整理中…」,解除**只认服务端证据**;
//   · 409:后端那句人话原样上屏,并重取一次拿到新的 revision;
//   · 总闸:关掉时入口按钮一个节点都不渲染。
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

vi.mock("../../features/agent-profile/profile-api.ts", () => ({
  fetchUnderstanding: vi.fn(),
  saveUnderstandingBlock: vi.fn(),
  clearUnderstandingBlock: vi.fn(),
  rebuildUnderstanding: vi.fn(),
  fetchAgentObservations: vi.fn(),
  clearAgentObservations: vi.fn(),
}));

import {
  clearAgentObservations,
  clearUnderstandingBlock,
  fetchAgentObservations,
  fetchUnderstanding,
  rebuildUnderstanding,
  saveUnderstandingBlock,
} from "../../features/agent-profile/profile-api.ts";
import { AgentProfilePanel } from "../../app/agent-profile-panel.tsx";
import { humanizedError } from "../../app/errors.ts";
import type {
  UnderstandingBlock,
  UnderstandingResponse,
} from "../../features/agent-profile/profile-model.ts";

function block(label: string, value: string, revision = 4): UnderstandingBlock {
  return {
    label,
    value,
    evidence: [],
    revision,
    updated_at: "2026-08-18T02:00:00+00:00",
    updated_origin: "job",
  };
}

function response(over: Partial<UnderstandingResponse> = {}): UnderstandingResponse {
  return {
    enabled: true,
    // 服务端只回写过的行 —— 这里刻意只给两块,面板要自己把剩下的补成空块。
    base: [block("corpus_shape", "封装工艺资料库")],
    mine: [block("retrieval_notes", "先写型号再问问题")],
    job: { base: null, mine: null },
    can_edit_base: true,
    ...over,
  };
}

const mockFetch = vi.mocked(fetchUnderstanding);
const mockSave = vi.mocked(saveUnderstandingBlock);
const mockClear = vi.mocked(clearUnderstandingBlock);
const mockRebuild = vi.mocked(rebuildUnderstanding);
const mockFetchObservations = vi.mocked(fetchAgentObservations);
const mockClearObservations = vi.mocked(clearAgentObservations);

beforeEach(() => {
  mockFetch.mockResolvedValue(response());
  mockSave.mockImplementation(async (_nb, label, body) => block(label, body.value, body.expected_revision + 1));
  mockClear.mockImplementation(async (_nb, label) => block(label, "", 9));
  mockRebuild.mockResolvedValue({ started: true });
  mockFetchObservations.mockResolvedValue({ enabled: true, items: [], calls_enabled: true, calls: [] });
  mockClearObservations.mockResolvedValue({ removed: 0 });
});

test("可写成员：两档都渲染，五块齐全（服务端没回的补成空块）", async () => {
  render(<AgentProfilePanel notebookId="nb1" />);

  expect(await screen.findByRole("heading", { name: "AI 对这个库的理解" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "我的检索心得" })).toBeInTheDocument();

  for (const title of ["这个库大致是什么", "反复出现的关键内容", "这个库还缺什么", "怎么查更容易找到", "常问却没找到的"]) {
    expect(screen.getByRole("textbox", { name: title })).toBeInTheDocument();
  }
  expect(screen.getByRole("textbox", { name: "这个库大致是什么" })).toHaveValue("封装工艺资料库");
  // 没写过的那块是空的,不是消失了。
  expect(screen.getByRole("textbox", { name: "这个库还缺什么" })).toHaveValue("");
});

test("只读成员：共享那一档只读渲染（内容照常显示，写入控件不渲染），自己那一份仍可改", async () => {
  mockFetch.mockResolvedValue(response({ can_edit_base: false }));
  render(<AgentProfilePanel notebookId="nb1" />);

  expect(await screen.findByText("封装工艺资料库")).toBeInTheDocument();
  // 共享那一档:没有输入框、没有「重新整理」。
  expect(screen.queryByRole("textbox", { name: "这个库大致是什么" })).not.toBeInTheDocument();
  // 共享那一档里另外两块服务端还没写过：只读态下如实说「还没有」，而不是整块消失。
  expect(screen.getAllByText("还没有整理出内容。")).toHaveLength(2);
  // 本人那一份不受影响 —— 只读成员唯一能写的就是它。
  expect(screen.getByRole("textbox", { name: "怎么查更容易找到" })).toHaveValue("先写型号再问问题");
  expect(screen.getAllByRole("button", { name: "重新整理" })).toHaveLength(1);
});

test("编辑保存：带读回来的 expected_revision，成功后重取", async () => {
  const user = userEvent.setup();
  render(<AgentProfilePanel notebookId="nb1" />);

  const field = await screen.findByRole("textbox", { name: "这个库还缺什么" });
  await user.type(field, "缺少测试报告");
  const save = within(field.closest(".item") as HTMLElement).getByRole("button", { name: "保存" });
  await user.click(save);

  await waitFor(() => expect(mockSave).toHaveBeenCalledTimes(1));
  expect(mockSave).toHaveBeenCalledWith("nb1", "corpus_gaps", {
    scope: "shared",
    value: "缺少测试报告",
    // 服务端没回过这块 —— 面板补的空块 revision 是 0,正是「创建这一行」的约定值。
    expected_revision: 0,
  });
  await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(2));
});

test("编辑保存：已经写过的块带的是服务端读回来的 revision（不是恒 0）", async () => {
  const user = userEvent.setup();
  render(<AgentProfilePanel notebookId="nb1" />);

  // "怎么查更容易找到" 对应的 mine 块由 response() 给出,revision 是 block() 的默认值 4。
  const field = await screen.findByRole("textbox", { name: "怎么查更容易找到" });
  await user.type(field, "补一句");
  await user.click(within(field.closest(".item") as HTMLElement).getByRole("button", { name: "保存" }));

  await waitFor(() => expect(mockSave).toHaveBeenCalledTimes(1));
  expect(mockSave).toHaveBeenCalledWith("nb1", "retrieval_notes", {
    scope: "mine",
    value: "先写型号再问问题补一句",
    expected_revision: 4,
  });
});

test("没改动时保存禁用（不发一次什么都没变的写请求）", async () => {
  render(<AgentProfilePanel notebookId="nb1" />);
  const field = await screen.findByRole("textbox", { name: "这个库大致是什么" });
  const save = within(field.closest(".item") as HTMLElement).getByRole("button", { name: "保存" });
  expect(save).toBeDisabled();
});

test("超限：字数提示上屏且保存禁用，一个请求都不发", async () => {
  const user = userEvent.setup();
  mockFetch.mockResolvedValue(
    response({ base: [block("corpus_shape", "封")] }),
  );
  render(<AgentProfilePanel notebookId="nb1" />);

  const field = await screen.findByRole("textbox", { name: "这个库大致是什么" });
  // 直接粘一整段超限文本(逐字符 type 400 次没有额外信息量,只是慢)。
  await user.clear(field);
  await user.click(field);
  await user.paste("字".repeat(401));

  const item = field.closest(".item") as HTMLElement;
  expect(within(item).getByText(/401 \/ 400 字/)).toBeInTheDocument();
  expect(within(item).getByText(/内容过长/)).toBeInTheDocument();
  expect(within(item).getByRole("button", { name: "保存" })).toBeDisabled();
  expect(mockSave).not.toHaveBeenCalled();
});

test("清空是两步确认：第一下只出确认按钮，确认后才发请求", async () => {
  const user = userEvent.setup();
  render(<AgentProfilePanel notebookId="nb1" />);

  const field = await screen.findByRole("textbox", { name: "这个库大致是什么" });
  const item = field.closest(".item") as HTMLElement;
  await user.click(within(item).getByRole("button", { name: "清空" }));

  expect(mockClear).not.toHaveBeenCalled();
  expect(within(item).getByRole("button", { name: "取消" })).toBeInTheDocument();

  await user.click(within(item).getByRole("button", { name: "确认清空" }));
  // codex R1 P2:清空必须带界面看到过的 revision(夹具默认 4),与保存同享乐观并发
  await waitFor(() =>
    expect(mockClear).toHaveBeenCalledWith("nb1", "corpus_shape", "shared", 4),
  );
});

test("清空可以取消：点「取消」之后既不发请求，也回到原来的按钮", async () => {
  const user = userEvent.setup();
  render(<AgentProfilePanel notebookId="nb1" />);

  const field = await screen.findByRole("textbox", { name: "这个库大致是什么" });
  const item = field.closest(".item") as HTMLElement;
  await user.click(within(item).getByRole("button", { name: "清空" }));
  await user.click(within(item).getByRole("button", { name: "取消" }));

  expect(mockClear).not.toHaveBeenCalled();
  expect(within(item).getByRole("button", { name: "清空" })).toBeInTheDocument();
});

test("重新整理：点完立刻不可点并换成「整理中…」", async () => {
  const user = userEvent.setup();
  render(<AgentProfilePanel notebookId="nb1" />);

  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  const [baseRebuild] = screen.getAllByRole("button", { name: "重新整理" });
  await user.click(baseRebuild);

  await waitFor(() => expect(mockRebuild).toHaveBeenCalledWith("nb1", "shared"));
  const busy = screen.getByRole("button", { name: "整理中…" });
  expect(busy).toBeDisabled();
  // 只有被点的那一档在忙 —— 另一档照常可点。
  expect(screen.getByRole("button", { name: "重新整理" })).toBeEnabled();
});

test("重新整理失败：忙碌位当场解除，按钮回到可点并给出人话", async () => {
  const user = userEvent.setup();
  mockRebuild.mockRejectedValue(humanizedError("正在整理，请稍候", 409));
  render(<AgentProfilePanel notebookId="nb1" />);

  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getAllByRole("button", { name: "重新整理" })[0]);

  expect(await screen.findByText("正在整理，请稍候")).toBeInTheDocument();
  await waitFor(() => expect(screen.getAllByRole("button", { name: "重新整理" })).toHaveLength(2));
});

test("服务端说还在跑时：一打开面板按钮就是「整理中…」，跑完之后才解除", async () => {
  vi.useFakeTimers();
  try {
    const running = { status: "running", pending: 2, updated_at: "", failure_reason: "" };
    mockFetch.mockResolvedValue(response({ job: { base: running, mine: null } }));
    render(<AgentProfilePanel notebookId="nb1" />);

    await vi.waitFor(() => expect(screen.getByRole("button", { name: "整理中…" })).toBeDisabled());

    // 解除只认服务端证据:轮询读到这条链不再是 running 才放开。
    mockFetch.mockResolvedValue(
      response({ job: { base: { ...running, status: "done" }, mine: null } }),
    );
    await vi.advanceTimersByTimeAsync(4000);
    await vi.waitFor(() => expect(screen.getAllByRole("button", { name: "重新整理" })).toHaveLength(2));
  } finally {
    vi.useRealTimers();
  }
});

test("卸载面板：清理轮询定时器，之后 advanceTimersByTime 不再新增 fetch 调用", async () => {
  vi.useFakeTimers();
  try {
    const running = { status: "running", pending: 1, updated_at: "", failure_reason: "" };
    mockFetch.mockResolvedValue(response({ job: { base: running, mine: null } }));
    const { unmount } = render(<AgentProfilePanel notebookId="nb1" />);
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "整理中…" })).toBeDisabled());

    const callsBeforeUnmount = mockFetch.mock.calls.length;
    unmount();
    await vi.advanceTimersByTimeAsync(4000 * 5);
    expect(mockFetch.mock.calls.length).toBe(callsBeforeUnmount);
  } finally {
    vi.useRealTimers();
  }
});

test("轮询超过尝试上限：停轮询并给中性文案，不宣布结局", async () => {
  vi.useFakeTimers();
  try {
    const running = { status: "running", pending: 1, updated_at: "", failure_reason: "" };
    mockFetch.mockResolvedValue(response({ job: { base: running, mine: null } }));
    render(<AgentProfilePanel notebookId="nb1" />);
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "整理中…" })).toBeDisabled());

    // 服务端一直如实回报「在跑」(任务卡死时正是这个形态) —— 60 次之后必须停。
    await vi.advanceTimersByTimeAsync(4000 * 61);
    await vi.waitFor(() =>
      expect(screen.getByText("整理可能仍在进行，稍后刷新查看")).toBeInTheDocument(),
    );
    const callsAfterGivingUp = mockFetch.mock.calls.length;
    await vi.advanceTimersByTimeAsync(4000 * 5);
    expect(mockFetch.mock.calls.length).toBe(callsAfterGivingUp);
  } finally {
    vi.useRealTimers();
  }
});

test("上次整理失败的原因照实上屏（后端刻意写给用户的那一句）", async () => {
  mockFetch.mockResolvedValue(
    response({
      job: {
        base: { status: "failed", pending: 5, updated_at: "", failure_reason: "服务重启，整理未完成" },
        mine: null,
      },
    }),
  );
  render(<AgentProfilePanel notebookId="nb1" />);

  expect(await screen.findByText(/服务重启，整理未完成/)).toBeInTheDocument();
});

test("保存撞 409：后端那句人话原样上屏，重取后出现陈旧草稿警示，第二次保存带刷新后的 revision", async () => {
  const user = userEvent.setup();
  mockSave.mockRejectedValueOnce(humanizedError("这段理解刚被更新过，请刷新后再改", 409));
  // 第一次 fetch 是挂载时的初始读;第二次是失败后的重取 —— 重取到的是"已经被
  // 别处改过"的新版本:revision 前进,内容也变了。
  mockFetch.mockResolvedValueOnce(response());
  mockFetch.mockResolvedValueOnce(
    response({ mine: [block("retrieval_notes", "别人整理过的新内容", 6)] }),
  );
  render(<AgentProfilePanel notebookId="nb1" />);

  const field = await screen.findByRole("textbox", { name: "怎么查更容易找到" });
  await user.type(field, "补一句");
  const item = field.closest(".item") as HTMLElement;
  await user.click(within(item).getByRole("button", { name: "保存" }));

  expect(await screen.findByText("这段理解刚被更新过，请刷新后再改")).toBeInTheDocument();
  // 重取:不重取的话用户手里还是旧 revision,再点保存只会撞同一堵墙。
  await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(2));
  // 写了一半的字不该被一次冲突吃掉。
  expect(screen.getByRole("textbox", { name: "怎么查更容易找到" })).toHaveValue("先写型号再问问题补一句");
  // 陈旧草稿的显式冲突出口:警示行,不是静默保留也不是静默覆盖。
  expect(within(item).getByRole("alert")).toHaveTextContent("内容刚被重新整理过");

  // 保存按钮仍可用 —— 这时点下去是一次知情覆盖,第二次提交必须带刷新后的 revision,
  // 不能沿用第一次那份已经作废的 4。
  mockSave.mockResolvedValueOnce(block("retrieval_notes", "先写型号再问问题补一句", 7));
  await user.click(within(item).getByRole("button", { name: "保存" }));
  await waitFor(() => expect(mockSave).toHaveBeenCalledTimes(2));
  expect(mockSave).toHaveBeenNthCalledWith(2, "nb1", "retrieval_notes", {
    scope: "mine",
    value: "先写型号再问问题补一句",
    expected_revision: 6,
  });
});

test("放弃我的修改：陈旧草稿的另一条出口——丢弃草稿，回到服务端新值", async () => {
  const user = userEvent.setup();
  mockSave.mockRejectedValueOnce(humanizedError("这段理解刚被更新过，请刷新后再改", 409));
  mockFetch.mockResolvedValueOnce(response());
  mockFetch.mockResolvedValueOnce(
    response({ mine: [block("retrieval_notes", "别人整理过的新内容", 6)] }),
  );
  render(<AgentProfilePanel notebookId="nb1" />);

  const field = await screen.findByRole("textbox", { name: "怎么查更容易找到" });
  await user.type(field, "补一句");
  const item = field.closest(".item") as HTMLElement;
  await user.click(within(item).getByRole("button", { name: "保存" }));
  expect(await screen.findByText("这段理解刚被更新过，请刷新后再改")).toBeInTheDocument();
  await waitFor(() => expect(within(item).getByRole("alert")).toBeInTheDocument());

  await user.click(within(item).getByRole("button", { name: "放弃我的修改" }));

  expect(within(item).queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "怎么查更容易找到" })).toHaveValue("别人整理过的新内容");
});

test("保存在飞时继续打字：草稿不会被吃掉，且落进陈旧提示而不是被静默覆盖", async () => {
  const user = userEvent.setup();
  let resolveSave!: (value: UnderstandingBlock) => void;
  mockSave.mockImplementationOnce(
    () => new Promise((resolve) => { resolveSave = resolve; }),
  );
  render(<AgentProfilePanel notebookId="nb1" />);

  const field = await screen.findByRole("textbox", { name: "这个库大致是什么" });
  await user.type(field, "A");
  const item = field.closest(".item") as HTMLElement;
  await user.click(within(item).getByRole("button", { name: "保存" }));
  await waitFor(() => expect(mockSave).toHaveBeenCalledTimes(1));

  // 保存还在飞的时候继续打字。
  await user.type(field, "B");
  expect(field).toHaveValue("封装工艺资料库AB");

  // 服务端确认的是提交那一刻的值(不含续打的 B);随后 load() 拿到前进了的 revision。
  mockFetch.mockResolvedValueOnce(
    response({ base: [block("corpus_shape", "封装工艺资料库A", 5)] }),
  );
  resolveSave(block("corpus_shape", "封装工艺资料库A", 5));
  await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(2));

  // 续打的字没有被丢弃 —— 这是丢字修复的核心断言。
  await waitFor(() => expect(field).toHaveValue("封装工艺资料库AB"));
  // 且落进了陈旧提示:保存完成后 revision 前进了,草稿的 baseRevision 还是提交前那版。
  expect(within(item).getByRole("alert")).toHaveTextContent("内容刚被重新整理过");
});

test("首次加载失败：role=alert 上屏人话，「重试」能再读一次并恢复正常渲染", async () => {
  const user = userEvent.setup();
  mockFetch.mockRejectedValueOnce(humanizedError("网络不太好，请稍后重试", 500));
  render(<AgentProfilePanel notebookId="nb1" />);

  expect(await screen.findByRole("alert")).toHaveTextContent("网络不太好，请稍后重试");
  const retry = screen.getByRole("button", { name: "重试" });

  mockFetch.mockResolvedValueOnce(response());
  await user.click(retry);

  expect(await screen.findByRole("heading", { name: "AI 对这个库的理解" })).toBeInTheDocument();
});

test("总闸关掉：GET 回 enabled=false 时面板不给任何编辑入口", async () => {
  mockFetch.mockResolvedValue(
    response({ enabled: false, base: [], mine: [], can_edit_base: false }),
  );
  render(<AgentProfilePanel notebookId="nb1" />);

  expect(await screen.findByText("这项功能当前未开启。")).toBeInTheDocument();
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "重新整理" })).not.toBeInTheDocument();
  // 「Agent 记录」**仍然挂着**(codex #616 R2 P2):总闸关掉的是两条理解链路,
  // 而已经记下的调用记录仍然要能看、能清——后端这一轮已经改成不论开关都回读、
  // 并放行只清调用记录的那一支,前端在这里整页早返回就等于把那份数据在浏览器
  // 里变成既看不到也删不掉的。
  expect(screen.getByText("Agent 记录")).toBeInTheDocument();
  // 仍然是折叠的:不展开就一次请求都不发,这条与开着时逐字相同。
  expect(mockFetchObservations).not.toHaveBeenCalled();
});

test("总闸关掉：Agent 记录展开后照常拉取，已经记下的调用记录看得见也清得掉", async () => {
  const user = userEvent.setup();
  mockFetch.mockResolvedValue(
    response({ enabled: false, base: [], mine: [], can_edit_base: false }),
  );
  mockFetchObservations.mockResolvedValueOnce({
    enabled: false,
    items: [],
    calls_enabled: true,
    calls: [agentCall({ capability: "ask:execute" })],
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByText("这项功能当前未开启。");
  await user.click(screen.getByText("Agent 记录"));

  expect(await screen.findByText("提问")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "清空这个 Agent 的调用记录" }),
  ).toBeEnabled();
});

// ---------------------------------------------------------------- 依据渲染
// codex #520 R2 P2-2:服务端一直在写 `evidence`,前端却把它当 opaque 透传、一个字
// 都不渲染,而设计契约要的正是「结论可点开来源」。
function evidenced(over: Partial<UnderstandingResponse> = {}): UnderstandingResponse {
  return response({
    base: [
      {
        ...block("corpus_shape", "封装工艺资料库"),
        evidence: [{ claim_index: 0, source_ids: ["src-a", "src-b"] }],
      },
    ],
    mine: [
      {
        ...block("usage_gaps", "反复找预算材料但没有"),
        evidence: [{ claim_index: 0, zero_hit_queries: 7 }],
      },
    ],
    ...over,
  });
}

test("依据来源：AI 整理出的块渲染成可点的资料 chip，点击回调带上 source id", async () => {
  const user = userEvent.setup();
  const onOpenSource = vi.fn();
  mockFetch.mockResolvedValue(evidenced());
  render(
    <AgentProfilePanel
      notebookId="nb1"
      onOpenSource={onOpenSource}
      resolveSourceTitle={(id) => (id === "src-a" ? "热设计手册" : "")}
    />,
  );

  expect(await screen.findByText("依据来源")).toBeInTheDocument();
  // 解析得到标题就显示标题;查不到退回 id(隐藏它会让依据数与实际不符)。
  await user.click(screen.getByRole("button", { name: "热设计手册" }));
  expect(onOpenSource).toHaveBeenCalledWith("src-a");

  await user.click(screen.getByRole("button", { name: "src-b" }));
  expect(onOpenSource).toHaveBeenCalledWith("src-b");
});

test("依据来源：没接 onOpenSource 时 chip 仍显示，只是不可点", async () => {
  mockFetch.mockResolvedValue(evidenced());
  render(<AgentProfilePanel notebookId="nb1" />);

  expect(await screen.findByText("依据来源")).toBeInTheDocument();
  expect(screen.getByText("src-a")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "src-a" })).not.toBeInTheDocument();
});

test("零命中依据：usage_gaps 渲染成不可点的小字，说清那个数是怎么来的", async () => {
  mockFetch.mockResolvedValue(evidenced());
  render(<AgentProfilePanel notebookId="nb1" onOpenSource={vi.fn()} />);

  expect(await screen.findByText("依据：7 次没找到结果的检索")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /7/ })).not.toBeInTheDocument();
});

test("依据只属于 AI 整理出来的块：人自己写的那段不挂来源", async () => {
  mockFetch.mockResolvedValue(
    evidenced({
      base: [
        {
          ...block("corpus_shape", "这是工具手册库"),
          updated_origin: "user",
          evidence: [{ claim_index: 0, source_ids: ["src-a"] }],
        },
      ],
    }),
  );
  render(<AgentProfilePanel notebookId="nb1" onOpenSource={vi.fn()} />);

  expect(await screen.findByRole("heading", { name: "AI 对这个库的理解" })).toBeInTheDocument();
  // 人写的那段话,依据是他自己;再挂一排来源会让人以为那是系统给他的论据。
  expect(screen.queryByRole("button", { name: "src-a" })).not.toBeInTheDocument();
});

// ---------------------------------------------------------- 「Agent 记录」(P3-T5)
//
// 与上面「理解」两条链完全独立的第二套小节:默认收起、首次展开才发第一次请求
// (效率红线——大多数用户可能永远不点开);清空是同步请求,不是后台任务,不走
// notebook-busy-set 那一套。

function observation(over: Partial<Record<string, unknown>> = {}) {
  return {
    id: "obs-1",
    agent_profile_id: "agent-1",
    agent_name: "巡检助手",
    text: "常按型号查参数表",
    created_at: new Date().toISOString(),
    ...over,
  };
}

test("Agent 记录：折叠面板默认收起，展开前一次请求都不发", async () => {
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });

  expect(screen.getByText("Agent 记录")).toBeInTheDocument();
  expect(mockFetchObservations).not.toHaveBeenCalled();
});

test("Agent 记录：展开后拉取一次，空态显示「暂无 Agent 记录」", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValue({ enabled: true, items: [], calls_enabled: true, calls: [] });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });

  await user.click(screen.getByText("Agent 记录"));

  await waitFor(() => expect(mockFetchObservations).toHaveBeenCalledWith("nb1"));
  expect(await screen.findByText("暂无 Agent 记录")).toBeInTheDocument();
});

test("Agent 记录：按 Agent 分组渲染，每条带 Agent 名与相对时间", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValue({
    calls_enabled: true,
    calls: [],
    enabled: true,
    items: [
      observation({ id: "obs-1", agent_profile_id: "agent-1", agent_name: "巡检助手", text: "常按型号查参数表" }),
      observation({ id: "obs-2", agent_profile_id: "agent-2", agent_name: "汇总助手", text: "常汇总多份来源" }),
    ],
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));

  expect(await screen.findByText("巡检助手")).toBeInTheDocument();
  expect(screen.getByText("汇总助手")).toBeInTheDocument();
  expect(screen.getByText("常按型号查参数表")).toBeInTheDocument();
  expect(screen.getByText("常汇总多份来源")).toBeInTheDocument();
  expect(screen.getAllByText("刚刚").length).toBeGreaterThan(0);
  expect(screen.getAllByRole("button", { name: "清空这个 Agent 的记录" })).toHaveLength(2);
});

test("Agent 记录：条数恰好达到服务端上限时提示「仅显示最近 20 条」，没达到时不提示", async () => {
  const user = userEvent.setup();
  const twenty = Array.from({ length: 20 }, (_, index) =>
    observation({ id: `obs-${index}`, agent_profile_id: "agent-1" }),
  );
  mockFetchObservations.mockResolvedValueOnce({ calls_enabled: true, calls: [], enabled: true, items: twenty });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));

  expect(await screen.findByText("仅显示最近 20 条")).toBeInTheDocument();
});

test("Agent 记录：条数未达服务端上限时不显示「仅显示最近」提示", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValueOnce({
    calls_enabled: true,
    calls: [],
    enabled: true,
    items: [observation({ id: "obs-1" })],
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));

  await screen.findByText("巡检助手");
  expect(screen.queryByText(/仅显示最近/)).not.toBeInTheDocument();
});

test("Agent 记录：清空单个 Agent 只清它写下的线索，不动它的调用记录", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValueOnce({
    calls_enabled: true,
    calls: [],
    enabled: true,
    items: [observation()],
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));
  await screen.findByText("巡检助手");

  mockFetchObservations.mockResolvedValueOnce({ calls_enabled: true, calls: [], enabled: true, items: [] });
  await user.click(screen.getByRole("button", { name: "清空这个 Agent 的记录" }));

  // 按 kind 收窄:这颗按钮就挨着「写下的线索」那份清单,顺手把同一个 Agent 的
  // 调用记录也删掉不是用户能预期的事(调用记录那份有自己的清空按钮)。
  await waitFor(() =>
    expect(mockClearObservations).toHaveBeenCalledWith("nb1", "agent-1", "note"),
  );
  expect(await screen.findByText("暂无 Agent 记录")).toBeInTheDocument();
});

test("Agent 记录：清空全部是两步确认，第一下只出确认按钮，确认后省略 agentProfileId 全清", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValueOnce({
    calls_enabled: true,
    calls: [],
    enabled: true,
    items: [
      observation({ id: "obs-1", agent_profile_id: "agent-1" }),
      observation({ id: "obs-2", agent_profile_id: "agent-2", agent_name: "汇总助手" }),
    ],
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));
  await screen.findByText("巡检助手");

  await user.click(screen.getByRole("button", { name: "清空全部记录" }));
  expect(mockClearObservations).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "确认清空全部记录" })).toBeInTheDocument();

  mockFetchObservations.mockResolvedValueOnce({ calls_enabled: true, calls: [], enabled: true, items: [] });
  await user.click(screen.getByRole("button", { name: "确认清空全部记录" }));

  await waitFor(() => expect(mockClearObservations).toHaveBeenCalledWith("nb1"));
  expect(await screen.findByText("暂无 Agent 记录")).toBeInTheDocument();
});

test("Agent 记录：清空全部可以取消，不发请求", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValueOnce({ calls_enabled: true, calls: [], enabled: true, items: [observation()] });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));
  await screen.findByText("巡检助手");

  await user.click(screen.getByRole("button", { name: "清空全部记录" }));
  await user.click(screen.getByRole("button", { name: "取消" }));

  expect(mockClearObservations).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "清空全部记录" })).toBeInTheDocument();
});

test("Agent 记录：读取失败时人话上屏", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockRejectedValue(humanizedError("网络不太好，请稍后重试", 500));
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));

  expect(await screen.findByText("网络不太好，请稍后重试")).toBeInTheDocument();
});


test("任一清空在飞时全部清空按钮禁用(codex #535 R5)", async () => {
  mockFetchObservations.mockResolvedValue({ calls_enabled: true, calls: [], enabled: true, items: [
    { id: "o1", agent_profile_id: "agent-a", agent_name: "A", text: "x", created_at: "2026-08-20T00:00:00+00:00" },
    { id: "o2", agent_profile_id: "agent-b", agent_name: "B", text: "y", created_at: "2026-08-20T00:00:00+00:00" },
  ] });
  let release: () => void = () => undefined;
  mockClearObservations.mockImplementation(
    () => new Promise<{ removed: number }>((resolve) => { release = () => resolve({ removed: 1 }); }),
  );
  const testUser = userEvent.setup();
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await testUser.click(screen.getByText("Agent 记录"));

  const clearButtons = await screen.findAllByText("清空这个 Agent 的记录");
  await testUser.click(clearButtons[0]);
  // 第一个清空在飞:另一组的清空与「清空全部记录」都必须禁用
  // (先发的 load 最后返回会拿旧快照复活已删记录)
  const otherClear = screen.getByText("清空这个 Agent 的记录");
  expect(otherClear.closest("button")).toBeDisabled();
  const clearAllEntry = screen.getByText("清空全部记录");
  expect(clearAllEntry.closest("button")).toBeDisabled();
  release();
});


test("后端关闸的响应不渲染成空清单(codex #535 R9)", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValue({ enabled: false, items: [], calls_enabled: true, calls: [] });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));

  expect(await screen.findByText("该功能已在此部署关闭")).toBeInTheDocument();
  expect(screen.queryByText("暂无 Agent 记录")).not.toBeInTheDocument();
});


// ------------------------------------------------------- 调用记录(kind=call)
//
// 这一组钉的是「Agent 调用这个库的记录**看得见**」——加 kind 列这件事在界面上
// 的兑现,以及两份清单在界面上同样互不侵占。

function agentCall(over: Partial<Record<string, unknown>> = {}) {
  return {
    id: "call-1",
    agent_profile_id: "agent-1",
    agent_name: "巡检助手",
    capability: "knowledge:read",
    created_at: new Date().toISOString(),
    ...over,
  };
}

test("Agent 记录：调用记录单独成一小节，能力档译成人话且连着的同档折叠计数", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValueOnce({
    enabled: true,
    items: [],
    calls_enabled: true,
    calls: [
      agentCall({ id: "c1", capability: "knowledge:read" }),
      agentCall({ id: "c2", capability: "knowledge:read" }),
      agentCall({ id: "c3", capability: "ask:execute" }),
    ],
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));

  expect(await screen.findByText("调用记录")).toBeInTheDocument();
  expect(screen.getByText("查资料")).toBeInTheDocument();
  expect(screen.getByText("×2")).toBeInTheDocument();
  expect(screen.getByText("提问")).toBeInTheDocument();
  // 协议串一个字都不上屏。
  expect(screen.queryByText(/knowledge:read/)).not.toBeInTheDocument();
  // 没有短句时,那一小节照常显示它自己的空态,而不是被调用记录顶掉。
  expect(screen.getByText("暂无 Agent 记录")).toBeInTheDocument();
});

test("Agent 记录：没有调用时说「还没有 Agent 调用过」，不是沿用短句那句空态", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValueOnce({
    enabled: true,
    items: [observation()],
    calls_enabled: true,
    calls: [],
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));

  expect(await screen.findByText("还没有 Agent 调用过这个库")).toBeInTheDocument();
});

test("Agent 记录：调用记录那把开关关掉时说清「没开」，不把空清单说成没人来过", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValueOnce({
    enabled: true,
    items: [observation()],
    calls_enabled: false,
    calls: [],
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));

  expect(await screen.findByText("这个部署没有开启调用记录")).toBeInTheDocument();
  expect(screen.queryByText("还没有 Agent 调用过这个库")).not.toBeInTheDocument();
});

test("Agent 记录：开关关掉但已经记过的行照常显示，并且还能清", async () => {
  // codex #616 R1 P2:关开关是「从现在起不记」。后端因此照常回读既有的行,
  // 面板必须跟着它——否则一份用户有权删除的数据会变成他既看不到也删不掉的。
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValueOnce({
    enabled: true,
    items: [],
    calls_enabled: false,
    calls: [agentCall({ capability: "ask:execute" })],
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));

  expect(await screen.findByText("提问")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "清空这个 Agent 的调用记录" }),
  ).toBeEnabled();
  // 「没开」那句话只在真的没有行可显示时才顶上来。
  expect(screen.queryByText("这个部署没有开启调用记录")).not.toBeInTheDocument();
});

test("Agent 记录：清空某个 Agent 的调用记录只带 call，不动它写下的线索", async () => {
  const user = userEvent.setup();
  mockFetchObservations.mockResolvedValueOnce({
    enabled: true,
    items: [observation()],
    calls_enabled: true,
    calls: [agentCall()],
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));
  await screen.findByText("调用记录");

  mockFetchObservations.mockResolvedValueOnce({
    enabled: true,
    items: [observation()],
    calls_enabled: true,
    calls: [],
  });
  await user.click(screen.getByRole("button", { name: "清空这个 Agent 的调用记录" }));

  await waitFor(() =>
    expect(mockClearObservations).toHaveBeenCalledWith("nb1", "agent-1", "call"),
  );
  expect(await screen.findByText("还没有 Agent 调用过这个库")).toBeInTheDocument();
  // 线索那一侧原样还在——这正是按 kind 收窄要保住的东西。
  expect(screen.getByText("常按型号查参数表")).toBeInTheDocument();
});

test("Agent 记录：调用取满上限时提示还有更早的没显示", async () => {
  const user = userEvent.setup();
  const twenty = Array.from({ length: 20 }, (_, index) =>
    agentCall({ id: `c-${index}`, capability: index % 2 ? "ask:execute" : "knowledge:read" }),
  );
  mockFetchObservations.mockResolvedValueOnce({
    enabled: true,
    items: [],
    calls_enabled: true,
    calls: twenty,
  });
  render(<AgentProfilePanel notebookId="nb1" />);
  await screen.findByRole("heading", { name: "AI 对这个库的理解" });
  await user.click(screen.getByText("Agent 记录"));

  expect(await screen.findByText("仅显示最近 20 次")).toBeInTheDocument();
});
