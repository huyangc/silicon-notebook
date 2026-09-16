import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";
import type { NotebookQuestionSuggestions as Result } from "../../app/notebook-api";
import type { NotebookSummary } from "../../app/workspace-model";

const api = vi.hoisted(() => ({ fetchNotebookQuestionSuggestions: vi.fn() }));
vi.mock("../../app/notebook-api.ts", () => api);
import { NotebookQuestionSuggestions } from "../../app/notebook-question-suggestions";

const notebook: NotebookSummary = {
  id: "notebook-a", name: "论文", purpose: "", primary_domain: "", status: "active",
  counts: {}, created_label: "", ask_available: true,
};
const fallbackPrompts: Array<[string, string]> = [["核心论断", "请列出核心论断并给出证据。"]];
const ready: Result = {
  status: "ready", sampled: true,
  questions: [{ label: "循环深度的作用", question: "循环深度如何扩展推理计算？请基于来源说明。" }],
};
const props = {
  actorId: "actor-a", workspaceEpoch: 1, notebook, contentRevision: 1, sourceTotal: 1,
  fallbackPrompts, onSubmit: vi.fn(),
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

beforeEach(() => { vi.resetAllMocks(); });

test("templates remain usable while loading; generated chips submit full questions and disclose projection", async () => {
  const pending = deferred<Result>();
  api.fetchNotebookQuestionSuggestions.mockReturnValue(pending.promise);
  render(<NotebookQuestionSuggestions {...props} />);
  expect(screen.getByRole("status")).toHaveTextContent("正在根据来源整理问题");
  fireEvent.click(screen.getByRole("button", { name: "核心论断" }));
  expect(props.onSubmit).toHaveBeenLastCalledWith(fallbackPrompts[0][1]);
  await act(async () => pending.resolve(ready));
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "核心论断" })).not.toBeInTheDocument();
  expect(screen.getByText("根据部分来源内容推荐")).toBeInTheDocument();
  const chip = screen.getByRole("button", { name: ready.questions[0].label });
  expect(chip).toHaveAttribute("title", ready.questions[0].question);
  fireEvent.click(chip);
  expect(props.onSubmit).toHaveBeenLastCalledWith(ready.questions[0].question);
});

test.each(["transport", "fallback", "empty"])("%s failure retains templates without blocking Ask", async (kind) => {
  if (kind === "transport") api.fetchNotebookQuestionSuggestions.mockRejectedValue(new Error("offline"));
  else api.fetchNotebookQuestionSuggestions.mockResolvedValue({ ...ready, status: kind === "fallback" ? "fallback" : "ready", questions: [] });
  await act(async () => { render(<NotebookQuestionSuggestions {...props} />); });
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "核心论断" }));
  expect(props.onSubmit).toHaveBeenCalledWith(fallbackPrompts[0][1]);
});

test("manual questions take priority and empty, blocked or signed-out notebooks do no model I/O", () => {
  const manual = { ...notebook, expected_questions: ["我的问题"] };
  const view = render(<NotebookQuestionSuggestions {...props} notebook={manual} fallbackPrompts={[["我的问题", "我的完整问题"]]} />);
  fireEvent.click(screen.getByRole("button", { name: "我的问题" }));
  expect(props.onSubmit).toHaveBeenCalledWith("我的完整问题");
  view.rerender(<NotebookQuestionSuggestions {...props} sourceTotal={0} />);
  view.rerender(<NotebookQuestionSuggestions {...props} actorId={null} />);
  view.rerender(<NotebookQuestionSuggestions {...props} notebook={{ ...notebook, ask_available: false }} />);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(api.fetchNotebookQuestionSuggestions).not.toHaveBeenCalled();
});

test.each(["actor", "notebook", "workspace", "source"])("a changed %s hides prior chips and ignores its late response", async (kind) => {
  const first = deferred<Result>();
  const second = deferred<Result>();
  api.fetchNotebookQuestionSuggestions.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
  const view = render(<NotebookQuestionSuggestions {...props} />);
  const signal = api.fetchNotebookQuestionSuggestions.mock.calls[0][1] as AbortSignal;
  const nextProps = {
    ...props,
    ...(kind === "actor" ? { actorId: "actor-b" } : {}),
    ...(kind === "notebook" ? { notebook: { ...notebook, id: "notebook-b" } } : {}),
    ...(kind === "workspace" ? { workspaceEpoch: 2 } : {}),
    ...(kind === "source" ? { contentRevision: 2 } : {}),
  };
  view.rerender(<NotebookQuestionSuggestions {...nextProps} />);
  expect(signal.aborted).toBe(true);
  await act(async () => second.resolve(ready));
  await act(async () => first.resolve({ ...ready, questions: [{ label: "旧问题", question: "已过期的问题" }] }));
  expect(screen.queryByRole("button", { name: "旧问题" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: ready.questions[0].label })).toBeInTheDocument();
});

test("stable snapshots reuse the current result; a new source invalidates it immediately", async () => {
  api.fetchNotebookQuestionSuggestions.mockResolvedValueOnce(ready).mockReturnValueOnce(new Promise(() => {}));
  const view = render(<NotebookQuestionSuggestions {...props} />);
  await act(async () => {});
  view.rerender(<NotebookQuestionSuggestions {...props} notebook={{ ...notebook }} />);
  expect(api.fetchNotebookQuestionSuggestions).toHaveBeenCalledTimes(1);
  view.rerender(<NotebookQuestionSuggestions {...props} sourceTotal={2} />);
  expect(screen.queryByRole("button", { name: ready.questions[0].label })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "核心论断" })).toBeInTheDocument();
  expect(api.fetchNotebookQuestionSuggestions).toHaveBeenCalledTimes(2);
});

test("adding a manual question suppresses an already generated result", async () => {
  api.fetchNotebookQuestionSuggestions.mockResolvedValueOnce(ready);
  const view = render(<NotebookQuestionSuggestions {...props} />);
  await act(async () => {});
  view.rerender(<NotebookQuestionSuggestions {...props} notebook={{ ...notebook, expected_questions: ["自定义"] }} fallbackPrompts={[["自定义", "自定义问题"]]} />);
  expect(screen.queryByRole("button", { name: ready.questions[0].label })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "自定义" })).toBeInTheDocument();
  expect(api.fetchNotebookQuestionSuggestions).toHaveBeenCalledTimes(1);
});
