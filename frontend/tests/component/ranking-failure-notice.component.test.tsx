import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";
import { RankingFailureNotice, type RankingNoticeActions } from "../../app/ranking-failure-notice";
import type { AskResponse, RankingFailure } from "../../app/workspace-model";
import { AnswerView } from "../../app/answer-panel";

const lines: Record<RankingFailure["reason"], string> = {
  scoped_large_notebook: "没能按提及次数排行：本笔记本内容较多，只勾选部分来源时不支持这类统计。",
  not_computed: "没能按提及次数排行：本笔记本还没有排行数据。",
  analysis_unavailable: "没能按提及次数排行：本笔记本暂时不支持这类统计。",
  refresh_produced_nothing: "没能按提及次数排行：这次更新没有产出排行数据。",
  recount_budget_exhausted: "没能按提及次数排行：统计超时。",
  rank_failed: "没能按提及次数排行：统计时出错。",
  library_skipped: "有 1 个笔记本读取失败，没有计入排行。",
};
function failure(reason: RankingFailure["reason"]): RankingFailure {
  return { reason, notebook_ids: ["nb"], line: lines[reason],
    ...(reason === "not_computed" ? { fallback_line: lines.refresh_produced_nothing, produced_line: "排行数据已生成。" } : {}) };
}
function notice(reason: RankingFailure["reason"], actions?: RankingNoticeActions) {
  return <RankingFailureNotice notice={lines[reason]} failures={[failure(reason)]} actions={actions} />;
}

test.each([
  ["scoped_large_notebook", "全选本库来源并重新提问"],
  ["not_computed", "更新分析"],
  ["recount_budget_exhausted", "重新提问"],
  ["rank_failed", "重新提问"],
  ["library_skipped", "重新提问"],
] as const)("%s exposes only the applicable exit", async (reason, label) => {
  const selected = vi.fn();
  const reask = vi.fn();
  const startRebuild = vi.fn().mockResolvedValue(undefined);
  render(notice(reason, { selectAllAndReask: selected, reask, startRebuild, rebuildNotebookId: "nb" }));
  expect(screen.getAllByRole("button")).toHaveLength(1);
  await userEvent.setup().click(screen.getByRole("button", { name: label }));
  expect(label === "更新分析" ? startRebuild : label === "重新提问" ? reask : selected).toHaveBeenCalledOnce();
});

test.each(["analysis_unavailable", "refresh_produced_nothing"] as const)("%s is always plain text", (reason) => {
  render(notice(reason, { reask: vi.fn(), startRebuild: vi.fn(), rebuildNotebookId: "nb" }));
  expect(screen.getByRole("note")).toHaveTextContent(lines[reason]);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("missing callbacks and a different notebook never expose impossible exits", () => {
  const { rerender } = render(notice("scoped_large_notebook", { reask: vi.fn() }));
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  rerender(notice("not_computed", { reask: vi.fn() }));
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  rerender(notice("not_computed", { startRebuild: vi.fn(), rebuildNotebookId: "other" }));
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

test("only the submitted transition moves focus; background completion preserves focus and scroll", async () => {
  let finish!: () => void;
  const actions = { startRebuild: vi.fn(() => new Promise<void>((resolve) => { finish = resolve; })), rebuildNotebookId: "nb", reask: vi.fn() };
  const { rerender } = render(notice("not_computed", { ...actions, rebuildPhase: "confirming" }));
  expect(screen.getByRole("button", { name: "更新分析" })).toBeDisabled();
  rerender(notice("not_computed", { ...actions, rebuildPhase: "idle" }));
  const button = screen.getByRole("button", { name: "更新分析" });
  expect(button).toBeEnabled();
  const paragraph = button.closest("p")!;
  const focus = vi.spyOn(paragraph, "focus");
  await userEvent.setup().click(button);
  rerender(notice("not_computed", { ...actions, rebuildPhase: "running" }));
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("正在更新分析…");
  expect(screen.getByRole("status").closest("p")).toHaveFocus();
  expect(focus).toHaveBeenCalledExactlyOnceWith({ preventScroll: true });
  await act(async () => { finish(); });
  focus.mockClear();
  document.documentElement.scrollTop = 640;
  rerender(notice("not_computed", { ...actions, rebuildPhase: "produced" }));
  expect(screen.getByRole("note").textContent).toBe("排行数据已生成。 重新提问");
  expect(screen.queryByText(lines.not_computed)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "重新提问" })).toBeEnabled();
  expect(paragraph).toHaveFocus();
  expect(focus).not.toHaveBeenCalled();
  expect(document.documentElement.scrollTop).toBe(640);
  rerender(notice("not_computed", { ...actions, rebuildPhase: "nothing" }));
  expect(screen.getByRole("note").textContent).toBe(lines.refresh_produced_nothing);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  expect(paragraph).toHaveFocus();
  expect(focus).not.toHaveBeenCalled();
  expect(document.documentElement.scrollTop).toBe(640);
  document.documentElement.scrollTop = 0;
});

test("a rejected start restores its trigger once without scrolling; expiring feedback never moves it", async () => {
  let finish!: () => void;
  const actions = { startRebuild: vi.fn(() => new Promise<void>((resolve) => { finish = resolve; })), rebuildNotebookId: "nb" };
  const { rerender } = render(notice("not_computed", actions));
  const button = screen.getByRole("button", { name: "更新分析" });
  await userEvent.setup().click(button);
  const paragraph = button.closest("p")!;
  paragraph.focus();
  paragraph.blur();
  rerender(notice("not_computed", { ...actions, rebuildError: "更新分析未能开始，请重试。" }));
  const focus = vi.spyOn(button, "focus");
  await act(async () => { finish(); });
  expect(button).toBeEnabled();
  expect(button).toHaveFocus();
  expect(focus).toHaveBeenCalledExactlyOnceWith({ preventScroll: true });
  paragraph.focus();
  focus.mockClear();
  rerender(notice("not_computed", { ...actions, rebuildBusyReason: "图谱正在处理其他任务" }));
  rerender(notice("not_computed", actions));
  expect(paragraph).toHaveFocus();
  expect(focus).not.toHaveBeenCalled();
});

test("blurring to a null related target clears ownership before a running update", async () => {
  let finish!: () => void;
  const actions = { startRebuild: vi.fn(() => new Promise<void>((resolve) => { finish = resolve; })), rebuildNotebookId: "nb" };
  const { rerender } = render(notice("not_computed", actions));
  const button = screen.getByRole("button", { name: "更新分析" });
  await userEvent.setup().click(button);
  const paragraph = button.closest("p")!;
  paragraph.focus();
  paragraph.blur();
  const focus = vi.spyOn(paragraph, "focus");
  rerender(notice("not_computed", { ...actions, rebuildPhase: "running" }));
  await act(async () => { finish(); });
  expect(document.body).toHaveFocus();
  expect(focus).not.toHaveBeenCalled();
});

test("a pending start cannot be resubmitted and cancellation restores the button", async () => {
  let finish!: () => void;
  const startRebuild = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  render(notice("not_computed", { startRebuild, rebuildNotebookId: "nb" }));
  const user = userEvent.setup();
  const button = screen.getByRole("button", { name: "更新分析" });
  await user.click(button);
  expect(button).toBeDisabled();
  await user.click(button);
  expect(startRebuild).toHaveBeenCalledOnce();
  await act(async () => { finish(); });
  expect(button).toBeEnabled();
});

test("busy rebuild reason is inline and disabled reask exposes its reason", () => {
  const { rerender } = render(notice("not_computed", {
    startRebuild: vi.fn(), rebuildNotebookId: "nb", rebuildBusyReason: "图谱正在处理其他任务",
  }));
  expect(screen.getByRole("button", { name: "更新分析" })).toBeDisabled();
  expect(screen.getByRole("note")).toHaveTextContent("图谱正在处理其他任务");
  rerender(notice("rank_failed", { reask: vi.fn(), reaskDisabledReason: "正在回答" }));
  expect(screen.getByRole("button", { name: "重新提问" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "重新提问" })).toHaveAttribute("title", "正在回答");
});

test("readonly notices preserve every backend line with no actions or invented suffix", () => {
  const failures = Object.keys(lines).map((reason) => failure(reason as RankingFailure["reason"]));
  const disclosure = "按提及它的来源篇数统计，范围：100 篇来源。";
  render(<RankingFailureNotice notice={[...Object.values(lines), disclosure].join("\n")} failures={failures} />);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  for (const line of [...Object.values(lines), disclosure]) expect(screen.getByText(line)).toBeInTheDocument();
  expect(screen.getByRole("note")).not.toHaveTextContent("下面的回答只基于检索到的部分内容");
});

test("line matching is exact and the backend owns the fallback suffix", () => {
  const suffix = "下面的回答只基于检索到的部分内容，不是统计结果。";
  const currentFailure = { ...failure("not_computed"), line: lines.not_computed + suffix,
    fallback_line: lines.refresh_produced_nothing + suffix };
  const actions = { startRebuild: vi.fn(), rebuildNotebookId: "nb" };
  const { rerender } = render(<RankingFailureNotice notice={lines.not_computed} failures={[currentFailure]} actions={actions} />);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  rerender(<RankingFailureNotice notice={currentFailure.line} failures={[currentFailure]}
    actions={{ ...actions, rebuildPhase: "nothing" }} />);
  expect(screen.getByRole("note").textContent).toBe(currentFailure.fallback_line);
  rerender(<RankingFailureNotice notice={currentFailure.line} failures={[currentFailure]}
    actions={{ ...actions, rebuildPhase: "produced", reask: vi.fn() }} />);
  expect(screen.getByRole("note").textContent).toBe(currentFailure.produced_line + " 重新提问");
  expect(screen.getByRole("note")).not.toHaveTextContent(suffix);
});

test("AnswerView draws the exact multiline ranking notice only once before the body", () => {
  const rankingNotice = `${lines.rank_failed}\n按提及它的来源篇数统计，范围：100 篇来源。`;
  const answer = {
    answer_id: "a", conversation_id: "c", conclusion: "正文", answer: `> ${rankingNotice}\n\n正文`,
    anchors: [], citations: [], related_knowledge: [], grounded: true, llm_mode: "reasoning",
    ranking_notice: rankingNotice, ranking_failures: [failure("rank_failed")],
  } as AskResponse;
  render(<AnswerView answer={answer} notebookId="nb" notebookNames={{}} memorySaved={false}
    feedbackSent="" buildingScaleIndex={false} scaleIndexStatus={null} />);
  expect(screen.getAllByText(lines.rank_failed)).toHaveLength(1);
  expect(screen.getAllByText("按提及它的来源篇数统计，范围：100 篇来源。")).toHaveLength(1);
  expect(screen.getByText("正文")).toBeInTheDocument();
});
