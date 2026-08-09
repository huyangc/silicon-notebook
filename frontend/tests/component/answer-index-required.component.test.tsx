import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import type { AskResponse } from "../../app/workspace-model";


const historicalDegradedAnswer: AskResponse = {
  answer_id: "answer-index-required",
  conversation_id: "conversation-1",
  conclusion: "No evidence was available at answer time.",
  answer: "No evidence was available at answer time.",
  grounded: false,
  anchors: [],
  related_knowledge: [],
  citations: [],
  llm_mode: "deterministic",
  index_required: true,
};


function view(
  scaleIndexStatus: { exists: boolean; building: boolean; state?: "queued" | "building" } | null,
  onBuildScaleIndex = () => undefined,
) {
  return (
    <AnswerView
      answer={historicalDegradedAnswer}
      feedbackSent=""
      onFeedback={() => undefined}
      onOpenKnowledgeGraph={() => undefined}
      onOpenKnowhowRow={() => undefined}
      notebookId="nb-1"
      notebookNames={{}}
      onBuildScaleIndex={onBuildScaleIndex}
      buildingScaleIndex={false}
      scaleIndexStatus={scaleIndexStatus}
      onSaveMemory={() => undefined}
      memorySaved={false}
    />
  );
}


test("a published live index removes a historical answer's degraded banner", () => {
  const { rerender } = render(view({ exists: false, building: false }));
  expect(screen.getByText(/尚未建立索引/)).toBeInTheDocument();

  rerender(view({ exists: true, building: false }));
  expect(screen.queryByText(/尚未建立索引/)).not.toBeInTheDocument();
});


test("a queued degraded answer can upgrade the off-peak request immediately", async () => {
  const user = userEvent.setup();
  const onBuildScaleIndex = vi.fn();
  render(view(
    { exists: false, building: false, state: "queued" },
    onBuildScaleIndex,
  ));

  await user.click(screen.getByRole("button", { name: "立即构建" }));
  expect(onBuildScaleIndex).toHaveBeenCalledWith("nb-1");
});
