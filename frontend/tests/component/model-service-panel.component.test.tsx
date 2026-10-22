import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { ModelServicePanel, ModelServiceSummaryButton } from "../../app/model-service-panel";
import type { ModelServiceStatusItem } from "../../app/model-services";


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

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function servicesWithSupportId(supportId: string) {
  return {
    services: services.services.map((item) => item.service_id === "reasoner-next"
      ? { ...item, support_id: supportId }
      : item),
  };
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


test("focuses the highlighted service, traps focus, and closes", async () => {
  const opener = document.createElement("button");
  document.body.append(opener);
  opener.focus();
  const onClose = vi.fn();
  const user = userEvent.setup();
  const panel = renderPanel({ highlightedServiceId: "reasoner-next", onClose });
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


test("a stale copy completion cannot change the next support id copy state", async () => {
  const copyA = deferred<void>();
  const copyB = deferred<void>();
  const writeText = vi.spyOn(navigator.clipboard, "writeText")
    .mockImplementationOnce(() => copyA.promise)
    .mockImplementationOnce(() => copyB.promise);
  const user = userEvent.setup();
  const panel = renderPanel({ status: servicesWithSupportId("mdl-A") });

  await user.click(screen.getByRole("button", { name: "复制支持编号" }));
  expect(writeText).toHaveBeenLastCalledWith("mdl-A");

  panel.rerender(<ModelServicePanel {...panel.props} status={servicesWithSupportId("mdl-B")} />);
  const reasoner = screen.getByRole("group", { name: "推理服务" });
  expect(within(reasoner).getByRole("button", { name: "复制支持编号" })).toBeEnabled();
  await user.click(within(reasoner).getByRole("button", { name: "复制支持编号" }));
  expect(writeText).toHaveBeenLastCalledWith("mdl-B");
  expect(within(reasoner).getByRole("button", { name: "复制中…" })).toBeDisabled();

  await act(async () => { copyA.resolve(); });
  expect(within(reasoner).queryByRole("status")).not.toBeInTheDocument();
  expect(within(reasoner).getByRole("button", { name: "复制中…" })).toBeDisabled();

  await act(async () => { copyB.reject(new Error("B denied")); });
  expect(await within(reasoner).findByRole("status"))
    .toHaveTextContent("复制失败，请手动选择支持编号");
});


test("an unmounted support id copy ignores its deferred completion", async () => {
  const copy = deferred<void>();
  vi.spyOn(navigator.clipboard, "writeText").mockImplementationOnce(() => copy.promise);
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const user = userEvent.setup();
  const panel = renderPanel({ status: servicesWithSupportId("mdl-unmount") });

  await user.click(screen.getByRole("button", { name: "复制支持编号" }));
  panel.unmount();
  await act(async () => { copy.resolve(); });

  expect(consoleError).not.toHaveBeenCalled();
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


const gaps = {
  unbound_workloads: [
    { id: "agent_profile_consolidate", label: "库理解整理", feature_enabled: true },
    { id: "kg_glean", label: "知识补充分析", feature_enabled: false },
  ],
  stale_binding_ids: ["ask_anwser"],
  stale_binding_hidden_count: 2,
};


test("admins see unbound workloads and ignored bindings above the service cards", () => {
  renderPanel({ isAdmin: true, status: { ...services, ...gaps } });
  const block = screen.getByRole("region", { name: "未绑定与已忽略的绑定" });
  expect(within(block).getByText("库理解整理（功能已开启）")).toHaveClass("is-feature-enabled");
  // 特性开关关着的只说未绑定，不冒充「功能已开启」。
  expect(within(block).getByText("知识补充分析")).not.toHaveClass("is-feature-enabled");
  expect(within(block).getByText("ask_anwser")).toBeInTheDocument();
  expect(within(block).getByText("另有 2 项无法显示")).toBeInTheDocument();
  expect(block).toHaveTextContent("未绑定的功能当前不可用");
  // 使用位置按中文名显示；内部 id 只在「已忽略的绑定」里出现（那是配置文件里的原样键名）。
  expect(within(block).queryByText(/agent_profile_consolidate|kg_glean/)).not.toBeInTheDocument();
  // 每服务卡片照常渲染，块在卡片列表之前。
  const card = screen.getByRole("group", { name: "通用问答服务" });
  expect(block.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});


test("only ignored bindings: the block says they were ignored and lists no unbound row", () => {
  renderPanel({
    isAdmin: true,
    status: {
      ...services,
      unbound_workloads: [],
      stale_binding_ids: ["graph_chain_verify"],
      stale_binding_hidden_count: 0,
    },
  });
  const block = screen.getByRole("region", { name: "未绑定与已忽略的绑定" });
  expect(within(block).queryByText("未绑定")).not.toBeInTheDocument();
  expect(within(block).getByText("graph_chain_verify")).toBeInTheDocument();
  expect(block).toHaveTextContent("已被忽略");
  expect(within(block).queryByText(/另有/)).not.toBeInTheDocument();
});


test("no gaps renders no block at all, for admins and in the offline mode", () => {
  const complete = renderPanel({
    isAdmin: true,
    status: { ...services, unbound_workloads: [], stale_binding_ids: [], stale_binding_hidden_count: 0 },
  });
  expect(screen.queryByRole("region", { name: "未绑定与已忽略的绑定" })).not.toBeInTheDocument();
  expect(document.querySelector(".model-service-binding-gaps")).toBeNull();
  complete.unmount();

  // 旧后端不带这几个字段：同样不渲染。
  const legacy = renderPanel({ isAdmin: true });
  expect(document.querySelector(".model-service-binding-gaps")).toBeNull();
  legacy.unmount();

  // 离线模式：后端不报缺口，面板只剩「暂无模型服务」。
  renderPanel({ isAdmin: true, status: { services: [], unbound_workloads: [], stale_binding_ids: [] } });
  expect(screen.getByText("暂无模型服务")).toBeInTheDocument();
  expect(document.querySelector(".model-service-binding-gaps")).toBeNull();
});


test("regular users never see the block even if a payload carried gaps", () => {
  renderPanel({ isAdmin: false, status: { ...services, ...gaps } });
  expect(screen.queryByRole("region", { name: "未绑定与已忽略的绑定" })).not.toBeInTheDocument();
  expect(screen.queryByText(/功能已开启|已忽略的绑定/)).not.toBeInTheDocument();
});
