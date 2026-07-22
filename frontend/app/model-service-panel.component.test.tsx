import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ModelServicePanel, ModelServiceSummaryButton } from "./model-service-panel";
import type { ModelServiceStatusItem } from "./model-services";


function status(
  serviceId: string,
  displayName: string,
  state: ModelServiceStatusItem["status"],
  overrides: Partial<ModelServiceStatusItem> = {},
): ModelServiceStatusItem {
  return {
    service_id: serviceId,
    display_name: displayName,
    kind: "chat",
    model: "runtime-model",
    workloads: [{ id: "ask_answer", label: "问答生成" }],
    status: state,
    active: 2,
    maximum: 4,
    queued: 3,
    oldest_wait_ms: 780,
    latency_ms: 42,
    checked_at: "2030-01-02T03:04:05Z",
    trigger: "manual_test",
    code: "",
    support_id: "",
    ...overrides,
  };
}

const services = {
  services: [
    status("chat-west", "通用问答服务", "busy"),
    status("reasoner-next", "推理服务", "circuit_open", {
      active: 0, queued: 0, oldest_wait_ms: 0,
      code: "provider_unavailable", support_id: "mdl-support_456",
      trigger: "observed_failure",
    }),
    status("future-embed", "", "half_open", {
      kind: "embedding", model: "embed-model", workloads: [{ id: "chunk_embed", label: "来源向量化" }],
    }),
  ],
};

function renderPanel(overrides: Partial<React.ComponentProps<typeof ModelServicePanel>> = {}) {
  const props: React.ComponentProps<typeof ModelServicePanel> = {
    status: services,
    highlightedServiceId: null,
    isAdmin: false,
    onTestOne: vi.fn(async () => undefined),
    onTestAll: vi.fn(async () => undefined),
    onClose: vi.fn(),
    testingServiceIds: {},
    allTesting: false,
    ...overrides,
  };
  const view = render(<ModelServicePanel {...props} />);
  return { props, ...view };
}


test("renders a read-only card for every dynamic backend row", () => {
  renderPanel();
  expect(screen.getByRole("group", { name: "通用问答服务" })).toHaveTextContent("2 / 4");
  expect(screen.getByRole("group", { name: "通用问答服务" })).toHaveTextContent("排队 3");
  expect(within(screen.getByRole("group", { name: "通用问答服务" })).getByText("最久等待"))
    .toBeInTheDocument();
  expect(within(screen.getByRole("group", { name: "通用问答服务" })).getByText("780ms"))
    .toBeInTheDocument();
  expect(screen.getByRole("group", { name: "推理服务" })).toHaveTextContent("熔断中");
  expect(screen.getByRole("group", { name: "推理服务" })).toHaveTextContent("支持编号：mdl-support_456");
  expect(screen.getByRole("group", { name: "模型服务" })).toHaveTextContent("恢复探测中");
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /保存/ })).not.toBeInTheDocument();
  expect(screen.queryByText(/chat-west|reasoner-next|future-embed/)).not.toBeInTheDocument();
});


test("regular users have no probe controls while admins can test one or all", async () => {
  const regular = renderPanel();
  expect(screen.queryByRole("button", { name: "测试服务" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "测试全部" })).not.toBeInTheDocument();
  regular.unmount();

  const onTestOne = vi.fn(async () => undefined);
  const onTestAll = vi.fn(async () => undefined);
  const user = userEvent.setup();
  renderPanel({ isAdmin: true, onTestOne, onTestAll });
  await user.click(within(screen.getByRole("group", { name: "推理服务" }))
    .getByRole("button", { name: "测试服务" }));
  expect(onTestOne).toHaveBeenCalledWith("reasoner-next");
  await user.click(screen.getByRole("button", { name: "测试全部" }));
  expect(onTestAll).toHaveBeenCalledTimes(1);
});


test("copies support id without exposing raw diagnostics", async () => {
  const user = userEvent.setup();
  const writeText = vi.spyOn(navigator.clipboard, "writeText");
  renderPanel();
  await user.click(within(screen.getByRole("group", { name: "推理服务" }))
    .getByRole("button", { name: "复制支持编号" }));
  expect(writeText).toHaveBeenCalledWith("mdl-support_456");
  expect(await screen.findByRole("status")).toHaveTextContent("已复制");
  writeText.mockRestore();
});


test("focuses the highlighted service, traps focus, closes, and restores opener", async () => {
  const opener = document.createElement("button");
  document.body.append(opener);
  opener.focus();
  const onClose = vi.fn();
  const user = userEvent.setup();
  const panel = renderPanel({ highlightedServiceId: "reasoner-next", onClose, returnFocusTo: opener });
  const card = screen.getByRole("group", { name: "推理服务" });
  expect(card).toHaveClass("is-highlighted");
  expect(card).toHaveFocus();

  const close = screen.getByRole("button", { name: "关闭模型服务" });
  close.focus();
  await user.tab();
  expect(within(card).getByRole("button", { name: "复制支持编号" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(onClose).toHaveBeenCalledTimes(1);

  panel.unmount();
  expect(opener).toHaveFocus();
  opener.remove();
});


test("keeps Tab and Shift+Tab inside when the highlighted read-only card has no actions", async () => {
  const outside = document.createElement("button");
  outside.textContent = "outside";
  document.body.append(outside);
  const user = userEvent.setup();
  renderPanel({
    status: { services: [status("plain-chat", "普通模型服务", "ok")] },
    highlightedServiceId: "plain-chat",
    isAdmin: false,
  });

  const card = screen.getByRole("group", { name: "普通模型服务" });
  const close = screen.getByRole("button", { name: "关闭模型服务" });
  expect(card).toHaveFocus();
  await user.tab();
  expect(close).toHaveFocus();
  expect(outside).not.toHaveFocus();

  card.focus();
  await user.tab({ shift: true });
  expect(close).toHaveFocus();
  expect(outside).not.toHaveFocus();
  outside.remove();
});


test("clipboard rejection is contained and leaves accessible manual-copy feedback", async () => {
  const user = userEvent.setup();
  const writeText = vi.spyOn(navigator.clipboard, "writeText")
    .mockRejectedValueOnce(new Error("clipboard denied"));
  renderPanel();

  const reasoner = screen.getByRole("group", { name: "推理服务" });
  await user.click(within(reasoner).getByRole("button", { name: "复制支持编号" }));
  expect(await within(reasoner).findByRole("status"))
    .toHaveTextContent("复制失败，请手动选择支持编号");
  expect(reasoner).toHaveTextContent("mdl-support_456");
  writeText.mockRestore();
});


test("backdrop closes the panel", () => {
  const onClose = vi.fn();
  renderPanel({ onClose });
  fireEvent.click(screen.getByRole("dialog", { name: "模型服务状态" }));
  expect(onClose).toHaveBeenCalledTimes(1);
});


test("collection summary only opens the provided status panel", async () => {
  const onOpen = vi.fn();
  const user = userEvent.setup();
  render(<ModelServiceSummaryButton text="API 正常 · 1 个模型异常" tone="bad" title="模型异常" onOpen={onOpen} />);
  await user.click(screen.getByRole("button", { name: "API 正常 · 1 个模型异常" }));
  expect(onOpen).toHaveBeenCalledTimes(1);
});
