import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    testingRoles: {},
    draftTestingRoles: {},
    allTesting: false,
    ...overrides,
  };
  const view = render(<ModelServicePanel {...props} />);
  return { props, ...view };
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


test("renders the page-owned all-model lock until the orchestrator releases it", async () => {
  let resolveAll!: () => void;
  const onTestAll = vi.fn(() => new Promise<void>((resolve) => { resolveAll = resolve; }));
  const user = userEvent.setup();
  const { props, rerender } = renderPanel({ onTestAll });

  const button = screen.getByRole("button", { name: "测试当前使用的全部模型" });
  await user.click(button);
  expect(onTestAll).toHaveBeenCalledTimes(1);

  rerender(<ModelServicePanel {...props} allTesting />);
  expect(screen.getByRole("button", { name: "正在测试全部模型…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();

  resolveAll();
  rerender(<ModelServicePanel {...props} allTesting={false} />);
  expect(screen.getByRole("button", { name: "测试当前使用的全部模型" })).toBeEnabled();
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


test("traps Tab focus within the modal", async () => {
  const user = userEvent.setup();
  renderPanel();

  const close = screen.getByRole("button", { name: "关闭模型服务" });
  const save = screen.getByRole("button", { name: "保存" });
  expect(close).toHaveFocus();

  await user.tab({ shift: true });
  expect(save).toHaveFocus();
  await user.tab();
  expect(close).toHaveFocus();
});


test("keeps Tab and Shift+Tab inside the dialog when saving disables every control", () => {
  const outside = document.createElement("button");
  outside.textContent = "outside target";
  document.body.append(outside);
  renderPanel({ saving: true });
  const dialog = screen.getByRole("dialog", { name: "模型服务" });

  outside.focus();
  const tab = fireEvent.keyDown(window, { key: "Tab" });
  expect(tab).toBe(false);
  expect(dialog).toHaveFocus();

  outside.focus();
  const shiftTab = fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
  expect(shiftTab).toBe(false);
  expect(dialog).toHaveFocus();
  outside.remove();
});


test("restores focus to the exact opener when the modal unmounts", () => {
  const opener = document.createElement("button");
  opener.textContent = "Ask opener";
  document.body.append(opener);
  opener.focus();

  const panel = renderPanel({ returnFocusTo: opener });
  panel.unmount();

  expect(opener).toHaveFocus();
  opener.remove();
});


test("checked text distinguishes an observed failure from a manual test", () => {
  renderPanel({
    status: {
      services: [
        status("llm", "runtime-primary", {
          status: "error",
          code: "upstream_error",
          trigger: "observed_failure",
        }),
        status("reasoning_llm", "runtime-reasoner", { trigger: "manual_test" }),
      ],
    },
  });

  expect(screen.getByRole("group", { name: "主模型" })).toHaveTextContent("最近失败");
  expect(screen.getByRole("group", { name: "推理模型" })).toHaveTextContent("上次测试");
});


test("draft activity blocks save and save activity disables editable inputs", () => {
  const first = renderPanel({ draftTestingRoles: { llm: true } });
  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  first.unmount();

  renderPanel({ saving: true });
  for (const input of screen.getAllByRole("textbox")) {
    expect(input).toBeDisabled();
  }
  expect(screen.getAllByPlaceholderText("未改动则保留原 key")[0]).toBeDisabled();
});


test("save and draft locks do not claim a saved-current test is running", () => {
  const draft = renderPanel({ draftTestingRoles: { llm: true } });
  const draftRow = screen.getByRole("group", { name: "主模型" });
  expect(within(draftRow).getByRole("button", { name: "测试当前使用" })).toBeDisabled();
  expect(within(draftRow).queryByRole("button", { name: "测试中…" })).not.toBeInTheDocument();
  draft.unmount();

  renderPanel({ saving: true });
  const savingRow = screen.getByRole("group", { name: "主模型" });
  expect(within(savingRow).getByRole("button", { name: "测试当前使用" })).toBeDisabled();
  expect(within(savingRow).queryByRole("button", { name: "测试中…" })).not.toBeInTheDocument();
});


test("renders page-owned per-role locks and preserves them across panel remount", async () => {
  let resolveTest!: () => void;
  const onTestCurrent = vi.fn(() => new Promise<void>((resolve) => { resolveTest = resolve; }));
  const user = userEvent.setup();
  const { props, rerender, unmount } = renderPanel({ onTestCurrent });

  await user.click(screen.getAllByRole("button", { name: "测试当前使用" })[0]);
  expect(onTestCurrent).toHaveBeenCalledTimes(1);

  rerender(<ModelServicePanel {...props} testingRoles={{ llm: true }} />);

  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "测试当前使用的全部模型" })).toBeDisabled();
  expect(screen.getByRole("group", { name: "主模型" }))
    .toHaveTextContent("测试中…");
  expect(within(screen.getByRole("group", { name: "主模型" }))
    .getByRole("button", { name: "测试中…" })).toBeDisabled();

  unmount();
  const reopened = render(<ModelServicePanel {...props} testingRoles={{ llm: true }} />);
  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();

  resolveTest();
  reopened.rerender(<ModelServicePanel {...props} testingRoles={{}} />);
  expect(screen.getByRole("button", { name: "保存" })).toBeEnabled();
});


test("handles a rejected current-model callback without an unhandled rejection", async () => {
  const onTestCurrent = vi.fn(async () => {
    throw new Error("diagnostic-only failure");
  });
  const user = userEvent.setup();
  renderPanel({ onTestCurrent });

  await user.click(screen.getAllByRole("button", { name: "测试当前使用" })[0]);

  expect(onTestCurrent).toHaveBeenCalledWith("llm");
  await waitFor(() => {
    expect(screen.getAllByRole("button", { name: "测试当前使用" })[0]).toBeEnabled();
  });
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
