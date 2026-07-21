import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { AnswerView } from "./answer-panel";
import type { ModelServiceStatusItem, StatusModelRole } from "./model-settings";
import type { AskResponse } from "./workspace-model";


function status(
  service: StatusModelRole,
  state: ModelServiceStatusItem["status"],
  latencyMs = 0,
): ModelServiceStatusItem {
  return {
    service,
    model: `${service}-runtime`,
    source: "system",
    kind: service === "embedding" ? "embedding" : service === "rerank" ? "rerank" : "llm",
    configured: true,
    required: service === "llm",
    status: state,
    latency_ms: latencyMs,
    checked_at: "2030-01-01T00:00:00Z",
    trigger: "manual_test",
    code: state === "error" ? "upstream_error" : "",
  };
}

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

const answer: AskResponse = {
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
      service: "reasoning_llm",
      stage: "answer",
      model: "runtime-reasoner",
      message: "secret upstream reasoner payload",
    },
    {
      service: "embedding",
      stage: "embed",
      model: "runtime-embed",
      message: "internal embed endpoint 10.0.0.8",
    },
    {
      service: "reasoning_llm",
      stage: "retry",
      model: "runtime-reasoner",
      message: "duplicate raw diagnostic",
    },
  ],
};


test("renders dynamic deduplicated failures and routes role-specific actions", async () => {
  const user = userEvent.setup();
  const onTestModel = vi.fn(async (service: StatusModelRole) => status(service, "ok", 42));
  const onOpenModelSettings = vi.fn();

  renderAnswer(answer, { onTestModel, onOpenModelSettings });

  expect(screen.getByText("推理模型 runtime-reasoner 调用失败，本次回答可能不完整。"))
    .toBeInTheDocument();
  expect(screen.getByText("嵌入模型 runtime-embed 调用失败，本次回答可能不完整。"))
    .toBeInTheDocument();
  expect(screen.getAllByRole("button", { name: "测试此模型" })).toHaveLength(2);
  expect(screen.queryByText("secret upstream reasoner payload")).not.toBeInTheDocument();
  expect(screen.queryByTitle(/secret upstream|10\.0\.0\.8|duplicate raw/)).not.toBeInTheDocument();

  await user.click(screen.getAllByRole("button", { name: "测试此模型" })[0]);
  expect(onTestModel).toHaveBeenCalledWith("reasoning_llm");
  await user.click(screen.getAllByRole("button", { name: "打开模型服务" })[1]);
  expect(onOpenModelSettings).toHaveBeenCalledWith("embedding");
});


test("shows per-service pending and stable test results", async () => {
  let resolveReasoner!: (value: ModelServiceStatusItem) => void;
  const onTestModel = vi.fn((service: StatusModelRole) => {
    if (service === "reasoning_llm") {
      return new Promise<ModelServiceStatusItem>((resolve) => { resolveReasoner = resolve; });
    }
    return Promise.resolve(status(service, "error"));
  });
  const user = userEvent.setup();

  renderAnswer(answer, { onTestModel });

  const buttons = screen.getAllByRole("button", { name: "测试此模型" });
  await user.click(buttons[0]);
  expect(screen.getByRole("button", { name: "测试中…" })).toBeDisabled();

  resolveReasoner(status("reasoning_llm", "ok", 42));
  await waitFor(() => expect(screen.getByText("正常 42ms")).toBeInTheDocument());

  await user.click(screen.getAllByRole("button", { name: "测试此模型" })[1]);
  await waitFor(() => expect(screen.getByText("失败：连接未通过")).toBeInTheDocument());
});


test("uses page-owned test locks and ignores a stale configuration completion", async () => {
  const onTestModel = vi.fn(async () => null);
  const user = userEvent.setup();
  const { rerender } = renderAnswer(answer, {
    onTestModel,
    testingModelRoles: { reasoning_llm: true },
  });

  expect(screen.getByRole("button", { name: "测试中…" })).toBeDisabled();
  expect(screen.getAllByRole("button", { name: "测试此模型" })).toHaveLength(1);

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
      onSaveMemory={() => undefined}
      memorySaved={false}
      onTestModel={onTestModel}
      testingModelRoles={{}}
    />,
  );
  await user.click(screen.getAllByRole("button", { name: "测试此模型" })[0]);

  expect(onTestModel).toHaveBeenCalledWith("reasoning_llm");
  await waitFor(() => {
    expect(screen.queryByText(/正常 \d+ms|失败：|尚未配置/)).not.toBeInTheDocument();
  });
});
