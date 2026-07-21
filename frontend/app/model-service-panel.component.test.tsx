import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ModelServicePanel, ModelServiceSummaryButton } from "./model-service-panel";
import {
  MODEL_ROLES,
  type ModelRole,
  type ModelServiceStatusItem,
  type ServiceForm,
  type StatusModelRole,
} from "./model-settings";


function form(): ServiceForm {
  return { base_url: "https://models.example/v1", model: "draft-model", api_key: "", keyDirty: false };
}

const forms = Object.fromEntries(MODEL_ROLES.map((role) => [role, form()])) as Record<ModelRole, ServiceForm>;

function status(
  service: StatusModelRole,
  model: string,
  overrides: Partial<ModelServiceStatusItem> = {},
): ModelServiceStatusItem {
  return {
    service,
    model,
    source: "system",
    kind: service === "embedding" ? "embedding" : service === "rerank" ? "rerank" : "llm",
    configured: true,
    required: service === "llm",
    status: "ok",
    latency_ms: 42,
    checked_at: "2030-01-02T03:04:05Z",
    trigger: "manual_test",
    code: "",
    ...overrides,
  };
}

const services = {
  services: [
    status("llm", "runtime-primary"),
    status("reasoning_llm", "runtime-reasoner", { status: "error", code: "upstream_error" }),
    status("rewrite_llm", "runtime-rewrite"),
    status("kg_llm", "runtime-kg"),
    status("rerank", "runtime-rerank"),
    status("embedding", "runtime-embed"),
  ],
};

function renderPanel(overrides: Partial<React.ComponentProps<typeof ModelServicePanel>> = {}) {
  const props: React.ComponentProps<typeof ModelServicePanel> = {
    forms,
    status: services,
    highlightedRole: null,
    draftTestResults: {},
    onFormChange: vi.fn(),
    onTestDraft: vi.fn(),
    onTestCurrent: vi.fn(async () => undefined),
    onTestAll: vi.fn(async () => undefined),
    onClose: vi.fn(),
    onSave: vi.fn(),
    ...overrides,
  };
  render(<ModelServicePanel {...props} />);
  return props;
}


test("renders dynamic saved model status and a read-only embedding row without probing", () => {
  const onTestCurrent = vi.fn(async () => undefined);
  const onTestAll = vi.fn(async () => undefined);
  renderPanel({ onTestCurrent, onTestAll });

  expect(screen.getByText("runtime-primary")).toBeInTheDocument();
  expect(screen.getByText("runtime-reasoner")).toBeInTheDocument();
  expect(screen.getAllByText(/上次测试/).length).toBeGreaterThan(0);

  const embedding = screen.getByRole("group", { name: "嵌入模型" });
  expect(within(embedding).getByText("runtime-embed")).toBeInTheDocument();
  expect(within(embedding).queryByRole("textbox")).not.toBeInTheDocument();
  expect(within(embedding).getByText("由管理员维护系统配置")).toBeInTheDocument();
  expect(onTestCurrent).not.toHaveBeenCalled();
  expect(onTestAll).not.toHaveBeenCalled();
});


test("all-model test runs once and locks the action until completion", async () => {
  let resolveAll!: () => void;
  const onTestAll = vi.fn(() => new Promise<void>((resolve) => { resolveAll = resolve; }));
  const user = userEvent.setup();
  renderPanel({ onTestAll });

  const button = screen.getByRole("button", { name: "测试当前使用的全部模型" });
  await user.click(button);
  await user.click(button);

  expect(onTestAll).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "正在测试全部模型…" })).toBeDisabled();

  resolveAll();
  expect(await screen.findByRole("button", { name: "测试当前使用的全部模型" })).toBeEnabled();
});


test("marks and focuses the affected role after the panel mounts", () => {
  renderPanel({ highlightedRole: "reasoning_llm" });

  const row = screen.getByRole("group", { name: "推理模型" });
  expect(row).toHaveClass("is-highlighted");
  expect(row).toHaveAttribute("aria-current", "true");
  expect(row).toHaveFocus();
});


test("focuses a dialog control by default and closes on Escape", async () => {
  const onClose = vi.fn();
  const user = userEvent.setup();
  renderPanel({ onClose });

  expect(screen.getByRole("button", { name: "关闭模型服务" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);
});


test("locks saved-configuration mutation while an individual current test is running", async () => {
  let resolveTest!: () => void;
  const onTestCurrent = vi.fn(() => new Promise<void>((resolve) => { resolveTest = resolve; }));
  const user = userEvent.setup();
  renderPanel({ onTestCurrent });

  await user.click(screen.getAllByRole("button", { name: "测试当前使用" })[0]);

  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "测试当前使用的全部模型" })).toBeDisabled();

  resolveTest();
  expect(await screen.findByRole("button", { name: "保存" })).toBeEnabled();
});


test("collection summary is a button that only opens the provided panel", async () => {
  const onOpen = vi.fn();
  const user = userEvent.setup();
  render(
    <ModelServiceSummaryButton
      text="API 正常 · 1 个模型异常"
      tone="bad"
      title="推理模型 runtime-reasoner 异常"
      onOpen={onOpen}
    />,
  );

  await user.click(screen.getByRole("button", { name: "API 正常 · 1 个模型异常" }));
  expect(onOpen).toHaveBeenCalledTimes(1);
});
