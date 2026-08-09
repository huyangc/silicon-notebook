import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "../../app/answer-panel";
import type { AskResponse } from "../../app/workspace-model";


function renderAnswer(
  answer: AskResponse,
  overrides: Partial<React.ComponentProps<typeof AnswerView>> = {},
) {
  return render(
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
      onSaveMemory={() => undefined}
      memorySaved={false}
      {...overrides}
    />,
  );
}

const answer = {
  answer_id: "answer-model-error",
  conversation_id: "conversation-1",
  conclusion: "Partial answer",
  answer: "Partial answer",
  grounded: true,
  anchors: [],
  related_knowledge: [],
  citations: [],
  llm_mode: "deterministic",
  model_errors: [
    {
      service_id: "reasoner-next",
      service_name: "推理服务",
      workload_id: "ask_reasoning",
      workload_label: "逐步推理",
      stage: "answer",
      model: "runtime-reasoner",
      message: "upstream_error",
      support_id: "mdl-ask_789",
    },
    {
      service_id: "embed-next",
      service_name: "",
      workload_id: "chunk_embed",
      workload_label: "来源向量化",
      stage: "embed",
      model: "runtime-embed",
      message: "upstream_error",
      support_id: "",
    },
    {
      service_id: "reasoner-next",
      service_name: "推理服务",
      workload_id: "ask_reasoning",
      workload_label: "逐步推理",
      stage: "answer",
      model: "runtime-reasoner",
      message: "upstream_error",
      support_id: "mdl-ask_789",
    },
  ],
} as AskResponse;


test("renders safe dynamic names, support ids, and deduplicates failures", () => {
  renderAnswer(answer);
  expect(screen.getAllByText("推理服务 runtime-reasoner 调用失败，本次回答可能不完整。"))
    .toHaveLength(1);
  expect(screen.getByText("模型服务 runtime-embed 调用失败，本次回答可能不完整。"))
    .toBeInTheDocument();
  expect(screen.getByText("支持编号：")).toBeInTheDocument();
  expect(screen.getByText("mdl-ask_789")).toBeInTheDocument();
  expect(screen.queryByText(/reasoner-next|embed-next/)).not.toBeInTheDocument();
});


test("查看模型状态 highlights the failing service id", async () => {
  const onOpenModelStatus = vi.fn();
  const user = userEvent.setup();
  renderAnswer(answer, { onOpenModelStatus });
  await user.click(screen.getAllByRole("button", { name: "查看模型状态" })[1]);
  expect(onOpenModelStatus).toHaveBeenCalledWith("embed-next");
});


test("copies the returned support id", async () => {
  const user = userEvent.setup();
  const writeText = vi.spyOn(navigator.clipboard, "writeText");
  renderAnswer(answer);
  await user.click(screen.getByRole("button", { name: "复制支持编号" }));
  expect(writeText).toHaveBeenCalledWith("mdl-ask_789");
  expect(await screen.findByRole("status")).toHaveTextContent("已复制");
  writeText.mockRestore();
});


test("admin-only probe callback is absent unless the page provides it", () => {
  const withoutProbe = renderAnswer(answer);
  expect(screen.queryByRole("button", { name: "测试此模型" })).not.toBeInTheDocument();
  withoutProbe.unmount();

  renderAnswer(answer, { onTestModel: vi.fn(async () => null) });
  expect(screen.getAllByRole("button", { name: "测试此模型" })).toHaveLength(2);
});


test("rejects unsafe streamed service/support metadata before rendering or actions", async () => {
  const malicious = {
    ...answer,
    model_errors: [{
      service_id: "general/../../admin",
      service_name: "推理服务",
      workload_id: "ask_reasoning",
      workload_label: "逐步推理",
      stage: "answer",
      model: "runtime-reasoner",
      message: "upstream_error",
      support_id: "Bearer SECRET diagnostic",
    }],
  } as AskResponse;
  const onTestModel = vi.fn(async () => null);
  const onOpenModelStatus = vi.fn();
  const user = userEvent.setup();
  renderAnswer(malicious, { onTestModel, onOpenModelStatus });

  expect(screen.queryByText(/Bearer SECRET|general\/\.\./)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "复制支持编号" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "测试此模型" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "查看模型状态" }));
  expect(onOpenModelStatus).toHaveBeenCalledWith("");
});


test("a false execCommand fallback reports failure without an unhandled rejection", async () => {
  const user = userEvent.setup();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  const execCommand = vi.fn(() => false);
  Object.defineProperty(document, "execCommand", { configurable: true, value: execCommand });
  renderAnswer(answer);

  await user.click(screen.getByRole("button", { name: "复制支持编号" }));
  expect(await screen.findByRole("status"))
    .toHaveTextContent("复制失败，请手动选择支持编号");
  expect(screen.getByText("mdl-ask_789")).toBeInTheDocument();
  expect(execCommand).toHaveBeenCalledWith("copy");
  Object.defineProperty(document, "execCommand", { configurable: true, value: undefined });
});
