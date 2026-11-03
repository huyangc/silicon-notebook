import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, expect, test, vi } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import { MemorySaveDialog } from "../../app/memory-panel";
import { collectSavedAnswerFlags } from "../../app/memory-model";
import type {
  AskResponse,
  MemoryPreview,
  MemoryRecord,
} from "../../app/workspace-model";


afterEach(() => {
  vi.unstubAllGlobals();
});


const answer: AskResponse = {
  answer_id: "answer-1",
  conversation_id: "conversation-1",
  conclusion: "Grounded answer",
  answer: "Grounded answer",
  grounded: true,
  anchors: [],
  related_knowledge: [],
  citations: [],
  llm_mode: "deterministic",
};

function renderAnswer(props: Partial<Parameters<typeof AnswerView>[0]> = {}) {
  const base: Parameters<typeof AnswerView>[0] = { answer, feedbackSent: "", onFeedback: () => undefined, onOpenKnowledgeGraph: () => undefined, onOpenKnowhowRow: () => undefined, notebookId: "nb-1", notebookNames: {}, onBuildScaleIndex: () => undefined, buildingScaleIndex: false, onSaveMemory: vi.fn(), memorySaved: false, ...props };
  const view = render(<AnswerView {...base} />);
  return { ...view, base, rerenderAnswer: (next: Partial<Parameters<typeof AnswerView>[0]>) => view.rerender(<AnswerView {...base} {...next} />) };
}

test("未采纳建议显示有建议且保存仍可点，采纳后显示已保存并禁用", async () => {
  const signal = new AbortController().signal;
  const flags = await collectSavedAnswerFlags([["answer-1"]], async () => ({ links: {}, states: { "answer-1": { memory_id: "draft-1", status: "candidate", suggested: true } } }), signal);
  const onSaveMemory = vi.fn();
  const view = renderAnswer({ memorySaved: flags?.["answer-1"] === true, memorySuggested: flags?.["answer-1"] === "suggested", onSaveMemory });
  expect(screen.getByText("有建议")).toBeVisible();
  await userEvent.setup().click(screen.getByRole("button", { name: "保存到记忆" }));
  expect(onSaveMemory).toHaveBeenCalledWith("answer-1");
  view.rerenderAnswer({ memorySaved: true, memorySuggested: false });
  expect(screen.getByRole("button", { name: "已保存到记忆" })).toBeDisabled();
  expect(screen.queryByText("有建议")).toBeNull();
});

test("D19仅suggest用户点有用后显示句中保存动作，并复用原保存入口", async () => {
  const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
  const onSaveMemory = vi.fn();
  function Harness() {
    const [feedback, setFeedback] = useState("");
    return <AnswerView answer={answer} feedbackSent={feedback} onFeedback={setFeedback} notebookId="nb-1" notebookNames={{}} buildingScaleIndex={false} onSaveMemory={onSaveMemory} memorySaved={false} memorySuggestionsEnabled />;
  }
  render(<Harness />);
  expect(screen.queryByRole("button", { name: "存为记忆" })).toBeNull();
  await userEvent.setup().click(screen.getByRole("button", { name: "有用" }));
  const action = screen.getByRole("button", { name: "存为记忆" });
  expect(action).toHaveClass("link-button", "inline-action");
  expect(action.parentElement).toHaveTextContent("要把这条回答存为记忆吗？");
  await userEvent.setup().click(action);
  expect(onSaveMemory).toHaveBeenCalledOnce();
  expect(onSaveMemory).toHaveBeenCalledWith("answer-1");
  expect(fetchMock).not.toHaveBeenCalled();
});

test("manual回答DOM逐字不变，suggest关闭或已保存时不显示D19", () => {
  const view = renderAnswer({ feedbackSent: "useful" });
  expect(screen.queryByRole("button", { name: "存为记忆" })).toBeNull();
  const manual = view.container.innerHTML;
  view.rerenderAnswer({ feedbackSent: "useful", memorySuggestionsEnabled: false });
  expect(view.container.innerHTML).toBe(manual);
  view.rerenderAnswer({ feedbackSent: "useful", memorySuggestionsEnabled: true });
  expect(screen.getByRole("button", { name: "存为记忆" })).toBeVisible();
  view.rerenderAnswer({ feedbackSent: "useful", memorySuggestionsEnabled: false });
  expect(view.container.innerHTML).toBe(manual);
  view.rerenderAnswer({ feedbackSent: "useful", memorySuggestionsEnabled: true, memorySaved: true });
  expect(screen.queryByRole("button", { name: "存为记忆" })).toBeNull();
});


test("AnswerView exposes one executable save-to-Memory action", async () => {
  const user = userEvent.setup();
  const onSaveMemory = vi.fn();
  const { rerender } = render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      notebookId="nb-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      onSaveMemory={onSaveMemory}
      memorySaved={false}
    />,
  );

  await user.click(screen.getByRole("button", { name: "保存到记忆" }));
  expect(onSaveMemory).toHaveBeenCalledWith("answer-1");

  rerender(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      notebookId="nb-1"
      notebookNames={{}}
      onBuildScaleIndex={() => undefined}
      buildingScaleIndex={false}
      onSaveMemory={onSaveMemory}
      memorySaved
    />,
  );
  expect(screen.getByRole("button", { name: "已保存到记忆" })).toBeDisabled();
});


test("Memory save previews, permits edits, and locks dismissal while saving", async () => {
  const preview: MemoryPreview = {
    title: "Preview title",
    content_md: "Preview content",
    tags: ["one"],
    provenance_summary: {
      evidence_level: "grounded",
      citation_count: 2,
    },
    kg_extract_eligible: true,
  };
  const memory: MemoryRecord = {
    id: "memory-1",
    notebook_id: "nb-1",
    created_by: "user-1",
    origin: "ask_answer",
    status: "confirmed",
    promotion_state: "none",
    title: "Edited title",
    content_md: "Preview content",
    tags: ["one"],
    embedding_status: "pending",
    embedding_error: "",
    created_at: "2030-01-01T00:00:00Z",
    updated_at: "2030-01-01T00:00:00Z",
    provenance: {},
  };
  let resolveSave!: (response: Response) => void;
  const encoder = new TextEncoder();
  const previewStream = new Response(new ReadableStream({
    start(controller) {
      controller.enqueue(encoder.encode(`${JSON.stringify({ event: "started", stage: "memory_preview", elapsed_ms: 0 })}\n`));
      controller.enqueue(encoder.encode(`${JSON.stringify({ event: "final", stage: "memory_preview", result: preview })}\n`));
      controller.close();
    },
  }), { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
  const fetchMock = vi.fn()
    .mockResolvedValueOnce(previewStream)
    .mockImplementationOnce(() => new Promise<Response>((resolve) => {
      resolveSave = resolve;
    }));
  vi.stubGlobal("fetch", fetchMock);
  const onClose = vi.fn();
  const onSaved = vi.fn();
  const user = userEvent.setup();

  render(
    <MemorySaveDialog
      answerId="answer-1"
      notebookId="nb-1"
      onClose={onClose}
      onSaved={onSaved}
      sessionSignal={new AbortController().signal}
    />,
  );

  const title = await screen.findByDisplayValue("Preview title");
  await user.clear(title);
  await user.type(title, "Edited title");
  await user.click(screen.getByRole("button", { name: "确认保存" }));

  expect(screen.getByRole("button", { name: "关闭" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(String(fetchMock.mock.calls[1][0])).toContain(
    "/notebooks/nb-1/memories/from-answer",
  );
  expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toMatchObject({
    answer_id: "answer-1",
    title: "Edited title",
    content_md: "Preview content",
  });

  resolveSave(new Response(JSON.stringify(memory), { status: 200 }));
  await waitFor(() => expect(onSaved).toHaveBeenCalledWith(memory));
  expect(onClose).not.toHaveBeenCalled();
});
