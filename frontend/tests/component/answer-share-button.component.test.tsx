import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import type { AskResponse } from "../../app/workspace-model";

// 每条回答下面的「分享到这条回答」按钮（T6）。三条钉住的性质：
//   * 它排在**复制之后**——用户按位置找按钮，顺序是产品要求的一部分；
//   * 没有 `onShare` 就不渲染（同 onSaveMemory/onFeedback 的既有惯例：写回服务端的
//     动作没有承接方就不出按钮，绝不传 noop 留一颗点了没反应的启用态按钮）；
//   * `answer_id` 为空（生成中的回答还没有答案行）时不渲染——分享水位就锚在这个 id 上，
//     渲染了点下去只会拿到一句「这条会话还没有已完成的回答」。

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
  return render(
    <AnswerView
      answer={answer}
      feedbackSent=""
      onFeedback={() => undefined}
      notebookId="nb-1"
      notebookNames={{}}
      buildingScaleIndex={false}
      memorySaved={false}
      {...props}
    />,
  );
}

test("传了 onShare 就有分享按钮，点击带回这条回答的 answer_id", async () => {
  const user = userEvent.setup();
  const onShare = vi.fn();
  renderAnswer({ onShare });

  await user.click(screen.getByRole("button", { name: "分享到这条回答" }));
  expect(onShare).toHaveBeenCalledWith("answer-1");
});

test("分享按钮排在复制之后", () => {
  renderAnswer({ onShare: vi.fn() });

  const copy = screen.getByRole("button", { name: "复制回答" });
  const share = screen.getByRole("button", { name: "分享到这条回答" });
  // DOCUMENT_POSITION_FOLLOWING：share 在 copy 之后。
  expect(copy.compareDocumentPosition(share) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("没有 onShare（只读排障视图）→ 不渲染分享按钮，而不是渲染一颗点不动的", () => {
  renderAnswer();

  expect(screen.queryByRole("button", { name: "分享到这条回答" })).toBeNull();
  // 复制仍在：它是自足动作，与本条正交。
  expect(screen.getByRole("button", { name: "复制回答" })).toBeTruthy();
});

test("生成中的回答（answer_id 为空）不渲染分享按钮", () => {
  renderAnswer({ answer: { ...answer, answer_id: "" }, onShare: vi.fn() });

  expect(screen.queryByRole("button", { name: "分享到这条回答" })).toBeNull();
});
