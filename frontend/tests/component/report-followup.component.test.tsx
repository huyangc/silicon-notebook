// 报告详情的「就此追问」：按钮本身（只对已完成报告出现、在飞禁用、失败画在按钮上并自己复原），
// 以及一次完整的「点按钮 → 切到问答页签 → 新会话挂上报告标签并聚焦输入框」。
//
// 后半段的外壳按 page.tsx 的 followUpReport 组合同一组具名命令（先取范围、再开会话切页签、
// 高级模式切范围、最后聚焦）；page 自己的接线与顺序由 tests/guards/report-followup-wiring
// 守卫按源码语义钉住——Home() 太大，没有能单独渲染的接缝。
import { useState } from "react";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/waiting-wish-carousel", () => ({
  WaitingWishCarousel: () => <div aria-label="测试许愿轮播" />,
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
import { COPY_RESULT_HOLD_MS } from "../../app/copy-result";
import { followupScopeSelections } from "../../app/linked-report";
import { LinkedReportChip, LinkedReportPicker } from "../../app/linked-report-picker";
import type { ReportFollowupScopeT } from "../../app/report-model";
import { ReportsPanel, type ReportDetailT } from "../../app/report-view";
import {
  defaultSourceScopeSelection,
  type SourceScopeSelection,
} from "../../app/source-scope";
import { useAskSession } from "../../app/use-ask-session";
import { reportWorkspaceFixture } from "./report-workspace-fixture";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

beforeEach(() => {
  for (const mock of Object.values(askApi)) mock.mockReset();
  askApi.fetchAskModes.mockResolvedValue([]);
  askApi.listConversations.mockResolvedValue([]);
  askApi.checkReportLink.mockImplementation(async (_nb: string, body: { report_id: string }) => ({
    report_id: body.report_id,
    title: "",
    readable: true,
  }));
});


function report(overrides: Partial<ReportDetailT> = {}): ReportDetailT {
  return {
    id: "rep-1",
    question: "电池热失控的防护方案有哪些",
    status: "done",
    progress: "",
    section_count: 3,
    created_at: "2026-10-20T00:00:00Z",
    created_by: "user-a",
    outline: [],
    sections: [],
    section_status: [],
    gaps: [],
    content_md: "## 一\n\n正文",
    references: [],
    error: "",
    understanding: {},
    ...overrides,
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

function renderDetail(active: ReportDetailT, onFollowUp?: (report: ReportDetailT) => Promise<void>) {
  return render(
    <ReportsPanel
      notebookId="notebook-a"
      workspace={reportWorkspaceFixture({ active })}
      setToast={vi.fn()}
      onFollowUp={onFollowUp}
    />,
  );
}


test("已完成的报告有「就此追问」，未完成的没有；没有承接方时也不渲染", () => {
  const onFollowUp = vi.fn(async () => undefined);
  const view = renderDetail(report(), onFollowUp);
  expect(screen.getByRole("button", { name: "就此追问" })).toBeEnabled();

  for (const status of ["generating", "planning", "outline_ready", "intent_ready", "failed", "cancelled"]) {
    view.rerender(
      <ReportsPanel
        notebookId="notebook-a"
        workspace={reportWorkspaceFixture({ active: report({ status }) })}
        setToast={vi.fn()}
        onFollowUp={onFollowUp}
      />,
    );
    expect(screen.queryByRole("button", { name: "就此追问" }), status).toBeNull();
  }

  view.rerender(
    <ReportsPanel notebookId="notebook-a" workspace={reportWorkspaceFixture({ active: report() })} setToast={vi.fn()} />,
  );
  expect(screen.queryByRole("button", { name: "就此追问" })).toBeNull();
});

test("只读成员也能就此追问（追问是读）", () => {
  render(
    <ReportsPanel
      notebookId="notebook-a"
      workspace={reportWorkspaceFixture({ active: report() })}
      setToast={vi.fn()}
      readOnly
      onFollowUp={vi.fn(async () => undefined)}
    />,
  );
  expect(screen.getByRole("button", { name: "就此追问" })).toBeEnabled();
});

test("点下即禁用直到打开结束，在飞期间重复点击不会再发一次", async () => {
  const user = userEvent.setup();
  const pending = deferred<void>();
  const onFollowUp = vi.fn(() => pending.promise);
  const active = report();
  renderDetail(active, onFollowUp);

  await user.click(screen.getByRole("button", { name: "就此追问" }));
  expect(onFollowUp).toHaveBeenCalledWith(active);
  const busy = screen.getByRole("button", { name: "正在打开…" });
  expect(busy).toBeDisabled();
  await user.click(busy);
  expect(onFollowUp).toHaveBeenCalledOnce();

  await act(async () => {
    pending.resolve();
  });
  expect(screen.getByRole("button", { name: "就此追问" })).toBeEnabled();
});

test("打开失败时结果画在按钮自己身上，并由自身计时器复原", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const onFollowUp = vi.fn(async () => {
    throw new Error("scope read failed");
  });
  renderDetail(report(), onFollowUp);

  await user.click(screen.getByRole("button", { name: "就此追问" }));
  const failed = await screen.findByRole("button", { name: "打开失败" });
  expect(failed).toHaveClass("copy-result-failed");
  expect(failed).toBeEnabled();

  await act(async () => {
    await vi.advanceTimersByTimeAsync(COPY_RESULT_HOLD_MS);
  });
  const idle = screen.getByRole("button", { name: "就此追问" });
  expect(idle).not.toHaveClass("copy-result-failed");
});


// ---------------------------------------------------------------------------
// 一次完整的「就此追问」：外壳按 page.tsx 的 followUpReport 组合同一组命令。
// ---------------------------------------------------------------------------

type AskPolicy = Parameters<typeof useAskSession>[0]["policy"];

let session: ReturnType<typeof useAskSession> | null = null;

function Shell({
  advanced,
  readFollowupScope,
}: {
  advanced: boolean;
  readFollowupScope: (id: string) => Promise<ReportFollowupScopeT | null>;
}) {
  const [chatMode, setChatMode] = useState<"reports" | "ask">("reports");
  const [focusSignal, setFocusSignal] = useState(0);
  const [question, setQuestion] = useState("");
  const [sourceSelection, setSourceSelection] = useState<SourceScopeSelection>(defaultSourceScopeSelection);
  const policy: AskPolicy = {
    advanced,
    askUnavailable: false,
    scopeBlocked: false,
    kgAvailable: true,
    sourceScope: sourceSelection.allSelected
      ? { mode: "exclude", source_ids: [...sourceSelection.ids] }
      : { mode: "include", source_ids: [...sourceSelection.ids] },
    baseScope: { mode: "exclude", notebook_ids: [] },
  };
  session = useAskSession({
    actorId: "user-a",
    notebookId: "notebook-a",
    policy,
    effects: { notify: vi.fn(), reportError: vi.fn(), ensureAskVisible: () => setChatMode("ask") },
  });

  async function followUp(target: ReportDetailT) {
    let scope: ReturnType<typeof followupScopeSelections> | null = null;
    if (advanced) {
      const frozen = await readFollowupScope(target.id);
      if (!frozen) return;
      scope = followupScopeSelections(frozen, []);
    }
    if (!session!.beginReportFollowup({ id: target.id, title: target.question }, 2, scope?.note ?? "")) {
      throw new Error("not opened");
    }
    setChatMode("ask");
    if (scope) setSourceSelection(scope.source);
    setFocusSignal((value) => value + 1);
  }

  return (
    <>
      <p data-testid="tab">{chatMode}</p>
      <p data-testid="source-scope">{JSON.stringify(policy.sourceScope)}</p>
      {chatMode === "reports" ? (
        <ReportsPanel
          notebookId="notebook-a"
          workspace={reportWorkspaceFixture({ active: report() })}
          setToast={vi.fn()}
          onFollowUp={followUp}
        />
      ) : (
        <>
          <LinkedReportPicker
            notebookId="notebook-a"
            linked={session.linkedReport}
            disabled={false}
            onSelect={session.linkReport}
          >
            <LinkedReportChip
              report={session.linkedReport}
              verdict={session.linkedReportCheck}
              note={session.linkedReportNote}
              onRemove={session.unlinkReport}
            />
          </LinkedReportPicker>
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
      )}
    </>
  );
}

function openNotebook() {
  act(() => {
    const owner = session!.beginNotebookTransition({ actorId: "user-a", notebookId: "notebook-a", workspaceEpoch: 1 });
    session!.finishNotebookTransition(owner!);
  });
}

function frozenScope(overrides: Partial<ReportFollowupScopeT> = {}): ReportFollowupScopeT {
  return {
    report_id: "rep-1",
    title: "电池热失控的防护方案有哪些",
    source_scope: { mode: "include", source_ids: ["s-1", "s-2"] },
    base_scope: { mode: "exclude", notebook_ids: [] },
    dropped_sources: 1,
    dropped_libraries: 0,
    fell_back_to_all: false,
    ...overrides,
  };
}

test("高级模式：按钮 → 取范围 → 切到问答页签，新会话挂上报告、按报告范围勾选并聚焦输入框", async () => {
  const user = userEvent.setup();
  const readFollowupScope = vi.fn(async () => frozenScope());
  render(<Shell advanced readFollowupScope={readFollowupScope} />);
  openNotebook();

  await user.click(screen.getByRole("button", { name: "就此追问" }));
  expect(readFollowupScope).toHaveBeenCalledWith("rep-1");
  expect(screen.getByTestId("tab")).toHaveTextContent("ask");
  expect(screen.getByText("电池热失控的防护方案有哪些")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "提问" })).toHaveFocus();
  expect(JSON.parse(screen.getByTestId("source-scope").textContent!)).toEqual({
    mode: "include",
    source_ids: ["s-1", "s-2"],
  });
  // 过滤掉的部分就地说在标签旁。
  expect(screen.getByRole("status")).toHaveTextContent("已按报告生成时的检索范围勾选；其中 1 份资料现在无法勾选，已略去");
  expect(session!.conversationId).toBeNull();
});

test("自动模式：不取也不切检索范围，照样开新会话、挂报告、聚焦", async () => {
  const user = userEvent.setup();
  const readFollowupScope = vi.fn(async () => frozenScope());
  render(<Shell advanced={false} readFollowupScope={readFollowupScope} />);
  openNotebook();

  await user.click(screen.getByRole("button", { name: "就此追问" }));
  expect(readFollowupScope).not.toHaveBeenCalled();
  expect(screen.getByTestId("tab")).toHaveTextContent("ask");
  expect(screen.getByText("电池热失控的防护方案有哪些")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "提问" })).toHaveFocus();
  expect(JSON.parse(screen.getByTestId("source-scope").textContent!)).toEqual({ mode: "exclude", source_ids: [] });
  // 自动模式也能从标签上摘掉。
  await user.click(screen.getByRole("button", { name: "移除关联报告" }));
  expect(screen.queryByText("电池热失控的防护方案有哪些")).toBeNull();
});

test("取范围失败：留在报告页签，按钮自己显示「打开失败」，不挂标签", async () => {
  const user = userEvent.setup();
  const readFollowupScope = vi.fn(async () => {
    throw new Error("409");
  });
  render(<Shell advanced readFollowupScope={readFollowupScope} />);
  openNotebook();

  await user.click(screen.getByRole("button", { name: "就此追问" }));
  expect(await screen.findByRole("button", { name: "打开失败" })).toBeInTheDocument();
  expect(screen.getByTestId("tab")).toHaveTextContent("reports");
  expect(session!.linkedReport).toBeNull();
});

test("取范围期间归属变了（读回 null）：什么都不做，按钮回到可点", async () => {
  const user = userEvent.setup();
  const readFollowupScope = vi.fn(async () => null);
  render(<Shell advanced readFollowupScope={readFollowupScope} />);
  openNotebook();

  await user.click(screen.getByRole("button", { name: "就此追问" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "就此追问" })).toBeEnabled());
  expect(screen.getByTestId("tab")).toHaveTextContent("reports");
  expect(session!.linkedReport).toBeNull();
});
