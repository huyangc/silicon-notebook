// 问答的「关联报告」：来源栏里的分组（选择器 + 标签）、在途轮次的报告名（linked-report-picker.tsx），
// 以及它们接上真实的 useAskSession 之后的挂 / 换 / 摘与标签旁的冲突说明。
import { useState } from "react";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { ReportSummaryT } from "../../app/report-model";

const reportApi = vi.hoisted(() => ({ listReports: vi.fn() }));
vi.mock("../../app/report-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/report-api.ts")>()),
  ...reportApi,
}));

const askApi = vi.hoisted(() => ({
  checkReportLink: vi.fn(),
  fetchAskModes: vi.fn(),
  listConversations: vi.fn(),
}));
vi.mock("../../app/ask-api.ts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/ask-api.ts")>()),
  ...askApi,
}));

import { AskComposer } from "../../app/ask-composer";
import {
  LINKED_REPORT_PICKER_PAGE_SIZE,
  LinkedReportChip,
  LinkedReportPicker,
  LinkedReportTurnTag,
} from "../../app/linked-report-picker";
import { useAskSession } from "../../app/use-ask-session";

afterEach(cleanup);

beforeEach(() => {
  for (const mock of [...Object.values(reportApi), ...Object.values(askApi)]) mock.mockReset();
  askApi.fetchAskModes.mockResolvedValue([]);
  askApi.listConversations.mockResolvedValue([]);
  askApi.checkReportLink.mockImplementation(async (_nb: string, body: { report_id: string }) => ({
    report_id: body.report_id,
    title: "",
    readable: true,
  }));
});


function summary(id: string, question: string, status = "done"): ReportSummaryT {
  return {
    id,
    question,
    status,
    progress: "",
    section_count: 4,
    created_at: "2026-10-20T00:00:00Z",
    updated_at: "2026-10-20T01:00:00Z",
    created_by: "user-a",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function renderPicker(props: Partial<Parameters<typeof LinkedReportPicker>[0]> = {}) {
  const onSelect = vi.fn();
  const view = render(
    <LinkedReportPicker
      notebookId="notebook-a"
      linked={null}
      disabled={false}
      onSelect={onSelect}
      {...props}
    />,
  );
  return { ...view, onSelect };
}

/** 分组标题行上的「选择 / 更换」：没挂报告时叫「选择」，挂着时叫「更换」。 */
function openButton() {
  return screen.getByRole("button", { name: /^(选择|更换)关联报告$/ });
}


test("只列本笔记本里已完成的报告，读取中就地说明，选中即挂上并收起", async () => {
  const user = userEvent.setup();
  const pending = deferred<ReportSummaryT[]>();
  reportApi.listReports.mockReturnValue(pending.promise);
  const { onSelect } = renderPicker();

  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();
  await user.click(openButton());
  const popover = screen.getByRole("region", { name: "可关联的报告" });
  expect(openButton()).toHaveAttribute("aria-expanded", "true");
  expect(within(popover).getByText("正在读取报告…")).toBeVisible();
  expect(reportApi.listReports).toHaveBeenCalledWith("notebook-a");

  await act(async () => {
    pending.resolve([
      summary("r-done", "电池安全综述"),
      summary("r-running", "还在生成的报告", "generating"),
      summary("r-failed", "失败的报告", "failed"),
      summary("r-done-2", "储能政策梳理"),
    ]);
  });
  const options = within(popover).getAllByRole("button", { pressed: false });
  expect(options.map((option) => option.getAttribute("title"))).toEqual(["电池安全综述", "储能政策梳理"]);
  expect(within(popover).queryByText("还在生成的报告")).toBeNull();
  expect(within(popover).queryByText("失败的报告")).toBeNull();

  await user.click(within(popover).getByRole("button", { name: /储能政策梳理/ }));
  expect(onSelect).toHaveBeenCalledWith({ id: "r-done-2", title: "储能政策梳理" });
  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();
  expect(openButton()).toHaveAttribute("aria-expanded", "false");
});

test("当前已挂的那份标为已选", async () => {
  const user = userEvent.setup();
  reportApi.listReports.mockResolvedValue([summary("r-1", "电池安全综述"), summary("r-2", "储能政策梳理")]);
  renderPicker({ linked: { id: "r-2", title: "储能政策梳理" } });
  await user.click(openButton());
  const selected = await screen.findByRole("button", { pressed: true });
  expect(selected).toHaveAttribute("title", "储能政策梳理");
  expect(within(selected).getByText(/已选/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /电池安全综述/ })).toHaveAttribute("aria-pressed", "false");
});

test("没有已完成的报告时就地说明", async () => {
  const user = userEvent.setup();
  reportApi.listReports.mockResolvedValue([summary("r-running", "还在生成的报告", "generating")]);
  renderPicker();
  await user.click(openButton());
  expect(await screen.findByText("这个笔记本里还没有你生成完成的报告")).toBeVisible();
});

test("读取失败时在展开的清单里就地说明，重试再读一次", async () => {
  const user = userEvent.setup();
  reportApi.listReports
    .mockRejectedValueOnce(new Error("network down"))
    .mockResolvedValueOnce([summary("r-1", "电池安全综述")]);
  renderPicker();
  await user.click(openButton());
  const popover = screen.getByRole("region", { name: "可关联的报告" });
  expect(await within(popover).findByText("暂时无法读取报告列表，请重试")).toBeVisible();

  await user.click(within(popover).getByRole("button", { name: "重试" }));
  expect(await within(popover).findByRole("button", { name: /电池安全综述/ })).toBeVisible();
  expect(reportApi.listReports).toHaveBeenCalledTimes(2);
});

test("清单超过一页时分页，每次打开都从第一页开始、重新读一遍", async () => {
  const user = userEvent.setup();
  const reports = Array.from({ length: 25 }, (_, index) => summary(`r-${index + 1}`, `报告${String(index + 1).padStart(2, "0")}`));
  reportApi.listReports.mockResolvedValue(reports);
  renderPicker();
  await user.click(openButton());
  const popover = screen.getByRole("region", { name: "可关联的报告" });
  await within(popover).findByRole("button", { name: /报告01/ });
  expect(within(popover).getAllByRole("listitem")).toHaveLength(LINKED_REPORT_PICKER_PAGE_SIZE);
  const pager = within(popover).getByRole("navigation", { name: "关联报告分页" });
  await user.click(within(pager).getByRole("button", { name: "下一页" }));
  expect(within(popover).getAllByRole("listitem")).toHaveLength(5);
  expect(within(popover).getByRole("button", { name: /报告21/ })).toBeVisible();

  await user.click(openButton());
  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();
  await user.click(openButton());
  const reopened = screen.getByRole("region", { name: "可关联的报告" });
  expect(await within(reopened).findByRole("button", { name: /报告01/ })).toBeVisible();
  expect(reportApi.listReports).toHaveBeenCalledTimes(2);
});

test("Esc 与点外部都会收起清单", async () => {
  const user = userEvent.setup();
  reportApi.listReports.mockResolvedValue([]);
  renderPicker();
  await user.click(openButton());
  expect(screen.getByRole("region", { name: "可关联的报告" })).toBeInTheDocument();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();

  await user.click(openButton());
  expect(screen.getByRole("region", { name: "可关联的报告" })).toBeInTheDocument();
  await user.click(document.body);
  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();
});

// 键盘用户:持焦点的节点随清单卸载时,焦点不能掉到 body。
test("在清单里按 Esc 收起，焦点回到「选择」按钮", async () => {
  const user = userEvent.setup();
  reportApi.listReports.mockResolvedValue([summary("r-1", "电池安全综述")]);
  renderPicker();
  await user.tab();
  expect(openButton()).toHaveFocus();
  await user.keyboard("{Enter}");
  const option = await screen.findByRole("button", { name: /电池安全综述/ });
  await user.tab();
  expect(option).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();
  expect(openButton()).toHaveFocus();
});

test("点外部收起不抢焦点", async () => {
  const user = userEvent.setup();
  reportApi.listReports.mockResolvedValue([]);
  render(
    <>
      <input aria-label="别处的输入框" />
      <LinkedReportPicker notebookId="notebook-a" linked={null} disabled={false} onSelect={vi.fn()} />
    </>,
  );
  await user.click(openButton());
  await user.click(screen.getByRole("textbox", { name: "别处的输入框" }));
  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();
  expect(screen.getByRole("textbox", { name: "别处的输入框" })).toHaveFocus();
});

test("按「重试」后重试键被替换，焦点留在清单里", async () => {
  const user = userEvent.setup();
  const retried = deferred<ReportSummaryT[]>();
  reportApi.listReports
    .mockRejectedValueOnce(new Error("network down"))
    .mockReturnValueOnce(retried.promise);
  renderPicker();
  await user.click(openButton());
  const popover = screen.getByRole("region", { name: "可关联的报告" });
  const retry = await within(popover).findByRole("button", { name: "重试" });
  retry.focus();
  await user.keyboard("{Enter}");
  expect(within(popover).getByText("正在读取报告…")).toBeVisible();
  expect(within(popover).queryByRole("button", { name: "重试" })).toBeNull();
  expect(popover).toHaveFocus();
  await act(async () => {
    retried.resolve([summary("r-1", "电池安全综述")]);
  });
  expect(within(popover).getByRole("button", { name: /电池安全综述/ })).toBeVisible();
});

test("提交中（禁用）时按钮不可点，已展开的清单随之收起", async () => {
  const user = userEvent.setup();
  reportApi.listReports.mockResolvedValue([summary("r-1", "电池安全综述")]);
  const { rerender, onSelect } = renderPicker();
  await user.click(openButton());
  await screen.findByRole("button", { name: /电池安全综述/ });

  rerender(<LinkedReportPicker notebookId="notebook-a" linked={null} disabled onSelect={onSelect} />);
  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();
  expect(openButton()).toBeDisabled();

  // 解除禁用后不会自己弹回来。
  rerender(<LinkedReportPicker notebookId="notebook-a" linked={null} disabled={false} onSelect={onSelect} />);
  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();
});

test("换了笔记本，旧笔记本展开的清单与迟到的结果一起作废", async () => {
  const user = userEvent.setup();
  const pending = deferred<ReportSummaryT[]>();
  reportApi.listReports.mockReturnValue(pending.promise);
  const { rerender, onSelect } = renderPicker();
  await user.click(openButton());
  rerender(<LinkedReportPicker notebookId="notebook-b" linked={null} disabled={false} onSelect={onSelect} />);
  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();
  await act(async () => {
    pending.resolve([summary("r-a", "A 库的报告")]);
  });
  expect(screen.queryByText("A 库的报告")).toBeNull();
});


test("分组是来源栏的一组：标题「关联报告」，标签排在标题行与展开的清单之间，挂着报告时按钮叫「更换」", async () => {
  const user = userEvent.setup();
  reportApi.listReports.mockResolvedValue([]);
  const { rerender, onSelect } = renderPicker({ children: <div data-testid="tag">标签</div> });
  const group = screen.getByRole("region", { name: "关联报告" });
  expect(group).toHaveClass("scope-group");
  expect(within(group).getByRole("heading", { name: "关联报告" })).toBeInTheDocument();
  expect(openButton()).toHaveAccessibleName("选择关联报告");
  expect(openButton()).toHaveTextContent("选择");

  await user.click(openButton());
  const list = screen.getByRole("region", { name: "可关联的报告" });
  const tag = screen.getByTestId("tag");
  expect(tag.previousElementSibling).toContainElement(openButton());
  expect(tag.nextElementSibling).toBe(list);

  rerender(
    <LinkedReportPicker notebookId="notebook-a" linked={{ id: "r-1", title: "电池安全综述" }} disabled={false} onSelect={onSelect} />,
  );
  expect(openButton()).toHaveAccessibleName("更换关联报告");
  expect(openButton()).toHaveTextContent("更换");
});

test("标签只显示题目（分组标题已说了「关联报告」），题目整串放进 title，移除键调回调", async () => {
  const user = userEvent.setup();
  const onRemove = vi.fn();
  const title = "一个很长很长的研究问题".repeat(12);
  render(<LinkedReportChip report={{ id: "r-1", title }} verdict={null} note="" onRemove={onRemove} />);
  const text = screen.getByText(title);
  expect(text).toHaveAttribute("title", title);
  expect(text.closest(".linked-report-chip")).not.toHaveClass("unreadable");
  await user.click(screen.getByRole("button", { name: "移除关联报告" }));
  expect(onRemove).toHaveBeenCalledOnce();
});

test("题目为空时如实说没有题目，不编一个", () => {
  render(<LinkedReportChip report={{ id: "r-1", title: "" }} verdict={null} note="" onRemove={vi.fn()} />);
  expect(screen.getByText("（无题目）")).toBeInTheDocument();
});

test("判词说这一轮不读报告时，标签置灰、原因紧挨着标签显示", () => {
  const reason = "本轮不读取这份报告：它引用的 2 份资料不在当前检索范围内";
  render(
    <LinkedReportChip
      report={{ id: "r-1", title: "电池安全综述" }}
      verdict={{ readable: false, reason }}
      note=""
      onRemove={vi.fn()}
    />,
  );
  const chip = screen.getByText("电池安全综述").closest(".linked-report-chip");
  expect(chip).toHaveClass("unreadable");
  const status = screen.getByRole("status");
  expect(status).toHaveTextContent(reason);
  expect(status.parentElement).toBe(chip!.parentElement);
});

test("判词可读时不多一句话", () => {
  render(
    <LinkedReportChip
      report={{ id: "r-1", title: "电池安全综述" }}
      verdict={{ readable: true, reason: "" }}
      note=""
      onRemove={vi.fn()}
    />,
  );
  expect(screen.queryByRole("status")).toBeNull();
});

test("只剩一句就地说明（标签已被移除）时照样显示；什么都没有时不渲染", () => {
  const { container, rerender } = render(
    <LinkedReportChip report={null} verdict={null} note="关联的报告已不存在，已移除" onRemove={vi.fn()} />,
  );
  expect(screen.getByRole("status")).toHaveTextContent("关联的报告已不存在，已移除");
  expect(screen.queryByRole("button", { name: "移除关联报告" })).toBeNull();
  rerender(<LinkedReportChip report={null} verdict={null} note="" onRemove={vi.fn()} />);
  expect(container).toBeEmptyDOMElement();
});

test("移除键在禁用时不可点", () => {
  render(
    <LinkedReportChip report={{ id: "r-1", title: "电池安全综述" }} verdict={null} note="" onRemove={vi.fn()} removeDisabled />,
  );
  expect(screen.getByRole("button", { name: "移除关联报告" })).toBeDisabled();
});

test("在途轮次的报告名：题目放进 title，空题目用兜底文案", () => {
  const { rerender } = render(<LinkedReportTurnTag report={{ id: "r-1", title: "电池安全综述" }} />);
  expect(screen.getByText("关联报告：电池安全综述")).toBeInTheDocument();
  rerender(<LinkedReportTurnTag report={{ id: "r-1", title: "" }} />);
  expect(screen.getByText("关联报告")).toBeInTheDocument();
});


// ---------------------------------------------------------------------------
// 接上真实的 useAskSession：选择器挂 / 换、标签摘、就地预检的冲突说明落在标签旁。
// 组件按 page.tsx 的接法组合（page 一侧由 report-followup-wiring 守卫钉住）。
// ---------------------------------------------------------------------------

type AskPolicy = Parameters<typeof useAskSession>[0]["policy"];

const POLICY: AskPolicy = {
  advanced: true,
  askUnavailable: false,
  scopeBlocked: false,
  kgAvailable: true,
  sourceScope: { mode: "exclude", source_ids: [] },
  baseScope: { mode: "exclude", notebook_ids: [] },
};

let session: ReturnType<typeof useAskSession> | null = null;

function AskBox() {
  const [question, setQuestion] = useState("");
  const [focusSignal, setFocusSignal] = useState(0);
  session = useAskSession({
    actorId: "user-a",
    notebookId: "notebook-a",
    policy: POLICY,
    effects: { notify: vi.fn(), reportError: vi.fn(), ensureAskVisible: vi.fn() },
  });
  return (
    <>
      <aside className="sources-panel">
        <LinkedReportPicker
          notebookId="notebook-a"
          linked={session.linkedReport}
          disabled={false}
          onSelect={(report) => {
            session!.linkReport(report);
            setFocusSignal((value) => value + 1);
          }}
        >
          <LinkedReportChip
            report={session.linkedReport}
            verdict={session.linkedReportCheck}
            note={session.linkedReportNote}
            onRemove={session.unlinkReport}
          />
        </LinkedReportPicker>
      </aside>
      <AskComposer
        value={question}
        placeholder="提问"
        onChange={setQuestion}
        onSubmit={() => undefined}
        onAbort={() => undefined}
        running={false}
        focusSignal={focusSignal}
      />
    </>
  );
}

function openNotebook() {
  act(() => {
    const owner = session!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-a", workspaceEpoch: 1 });
    session!.finishNotebookTransition(owner!);
  });
}

async function pick(user: ReturnType<typeof userEvent.setup>, name: RegExp) {
  await user.click(openButton());
  await user.click(await screen.findByRole("button", { name }));
}

test("选择器挂上一份报告、换成另一份、再从标签上摘掉", async () => {
  const user = userEvent.setup();
  reportApi.listReports.mockResolvedValue([summary("r-1", "电池安全综述"), summary("r-2", "储能政策梳理")]);
  render(<AskBox />);
  openNotebook();
  expect(document.querySelector(".linked-report-chip")).toBeNull();
  expect(openButton()).toHaveAccessibleName("选择关联报告");

  await pick(user, /电池安全综述/);
  expect(screen.getByText("电池安全综述")).toBeInTheDocument();
  // 标签住在来源栏的「关联报告」分组里，不在问答输入框里。
  const bar = screen.getByText("电池安全综述").closest(".linked-report-bar")!;
  expect(bar.closest(".sources-panel")).toContainElement(openButton());
  expect(bar.closest(".chat-input-bar")).toBeNull();
  expect(openButton()).toHaveAccessibleName("更换关联报告");

  await pick(user, /储能政策梳理/);
  expect(screen.queryByText("电池安全综述")).toBeNull();
  expect(screen.getByText("储能政策梳理")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "移除关联报告" }));
  expect(document.querySelector(".linked-report-chip")).toBeNull();
  expect(session!.linkedReport).toBeNull();
  expect(openButton()).toHaveAccessibleName("选择关联报告");
});

test("范围冲突时，这一轮不读报告的原因就地显示在标签旁", async () => {
  const user = userEvent.setup();
  const reason = "本轮不读取这份报告：它引用的参考库《A》没有勾选";
  askApi.checkReportLink.mockResolvedValue({ report_id: "r-1", title: "电池安全综述", readable: false, reason });
  reportApi.listReports.mockResolvedValue([summary("r-1", "电池安全综述")]);
  render(<AskBox />);
  openNotebook();
  await pick(user, /电池安全综述/);

  // 去抖之后发预检；判词回来，标签置灰、原因紧挨着它。
  const status = await screen.findByRole("status");
  expect(status).toHaveTextContent(reason);
  const chip = screen.getByText("电池安全综述").closest(".linked-report-chip")!;
  expect(chip).toHaveClass("unreadable");
  expect(status.parentElement).toBe(chip.parentElement);
  await waitFor(() => expect(askApi.checkReportLink).toHaveBeenCalledTimes(1));
  expect(askApi.checkReportLink.mock.calls[0]?.[1]).toMatchObject({ report_id: "r-1" });
});

test("用键盘选中报告：标签挂上，焦点送进输入框", async () => {
  const user = userEvent.setup();
  reportApi.listReports.mockResolvedValue([summary("r-1", "电池安全综述")]);
  render(<AskBox />);
  openNotebook();
  openButton().focus();
  await user.keyboard("{Enter}");
  const option = await screen.findByRole("button", { name: /电池安全综述/ });
  option.focus();
  await user.keyboard("{Enter}");
  expect(screen.queryByRole("region", { name: "可关联的报告" })).toBeNull();
  expect(screen.getByText("电池安全综述")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "提问" })).toHaveFocus();
});
