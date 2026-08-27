import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import { MemorySaveDialog } from "../../app/memory-panel";
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
