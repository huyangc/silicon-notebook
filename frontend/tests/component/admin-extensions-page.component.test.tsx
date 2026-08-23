import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  fetchLoadedExtensions: vi.fn(),
}));

vi.mock("../../app/auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("../../app/admin/extensions/api.ts", () => ({
  KNOWN_API_VERSION: "1",
  fetchLoadedExtensions: mocks.fetchLoadedExtensions,
}));

import AdminExtensionsPage from "../../app/admin/extensions/page";
import { humanizedError } from "../../app/errors";


// 合成的十行内建拓扑(与后端 test_admin_extensions_routes.py 里钉的 10 个内建
// 扩展同量级)。第一行带完整的两类接入明细,其余只需要身份与计数。
const AGENT_PROFILE = {
  id: "builtin.ask_agent_profile",
  displayName: "Ask agent-profile completion",
  version: "1.0.0",
  trust: "builtin" as const,
  contributions: [{
    id: "builtin.ask_agent_profile",
    point: "ask.completed_observer",
    kind: "observer",
  }],
  uiContributions: [{
    id: "builtin.ask_agent_profile.workspace_panel",
    slot: "workspace.side_panel",
    capability: "ui.agent_profile.available",
  }],
};

const OTHER_IDS = [
  "builtin.ask_retrieval_experience",
  "builtin.ask_search_profile",
  "builtin.generated_question",
  "builtin.report_agent_profile",
  "builtin.report_markdown_exporter",
  "builtin.selected_source_graph",
  "builtin.source_parser_chain",
  "builtin.http_plugin_router",
  "builtin.report_completion",
];

function topology(extensions = [
  AGENT_PROFILE,
  ...OTHER_IDS.map((id) => ({
    id,
    displayName: id,
    version: "1.0.0",
    trust: "builtin" as const,
    contributions: [{ id, point: "retrieval.contributor", kind: "contributor" }],
    uiContributions: [],
  })),
]) {
  return { apiVersion: "1", versionRecognized: true, extensions };
}


test("管理员看到已加载的十个扩展,展开一行显示它的接入明细", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology());
  const user = userEvent.setup();

  render(<AdminExtensionsPage />);

  expect(await screen.findByText("Ask agent-profile completion")).toBeInTheDocument();
  const expanders = screen.getAllByRole("button", { name: /^展开 / });
  expect(expanders).toHaveLength(10);

  // 折叠态不渲染明细:接入的稳定 id 与扩展点都还看不到。
  expect(screen.queryByText("ask.completed_observer")).not.toBeInTheDocument();

  const target = expanders[0];
  expect(target).toHaveAttribute("aria-expanded", "false");
  await user.click(target);

  expect(target).toHaveAttribute("aria-expanded", "true");
  const contributionRow = screen.getByText("ask.completed_observer").closest("tr");
  expect(contributionRow).not.toBeNull();
  expect(within(contributionRow as HTMLTableRowElement).getByText("builtin.ask_agent_profile"))
    .toBeInTheDocument();
  // kind 出的是界面词,不是后端的英文枚举 id。
  expect(within(contributionRow as HTMLTableRowElement).getByText("完成后通知")).toBeInTheDocument();
  expect(screen.queryByText("observer")).not.toBeInTheDocument();
  expect(screen.getByText("workspace.side_panel")).toBeInTheDocument();
  expect(screen.getByText("ui.agent_profile.available")).toBeInTheDocument();

  // 只展开了这一行:另外九行仍是折叠态。
  const stillCollapsed = screen.getAllByRole("button", { name: /^展开 / });
  expect(stillCollapsed).toHaveLength(9);
  for (const other of stillCollapsed) expect(other).toHaveAttribute("aria-expanded", "false");
});


test("普通用户看到无权限文案,一次拓扑都不取", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "u1", role: "user" });

  render(<AdminExtensionsPage />);

  expect(await screen.findByText("无权限：仅管理员可查看已加载的扩展。")).toBeInTheDocument();
  expect(mocks.fetchLoadedExtensions).not.toHaveBeenCalled();
});


test("端点回 403 时同样落到无权限文案,而不是一句原始错误", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockRejectedValue(
    humanizedError("没有权限进行这个操作", 403),
  );

  render(<AdminExtensionsPage />);

  expect(await screen.findByText("无权限：仅管理员可查看已加载的扩展。")).toBeInTheDocument();
});


test("加载失败经人话层出文案,不直出异常原文", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockRejectedValue(new TypeError("Failed to fetch"));

  render(<AdminExtensionsPage />);

  expect(await screen.findByText(/加载失败：/)).toBeInTheDocument();
  expect(screen.queryByText(/Failed to fetch/)).not.toBeInTheDocument();
});


test("清单版本认不出时只挂一条提示,内容照常渲染", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue({
    ...topology(),
    apiVersion: "2",
    versionRecognized: false,
  });

  render(<AdminExtensionsPage />);

  expect(await screen.findByRole("status")).toHaveTextContent("下面的内容可能不完整");
  expect(screen.getByText("Ask agent-profile completion")).toBeInTheDocument();
});


test("部署装入的扩展带自己的信任档位徽标", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([{
    id: "corp.sample",
    displayName: "",
    version: "",
    trust: "deployment" as const,
    contributions: [],
    uiContributions: [],
  }]));
  const user = userEvent.setup();

  render(<AdminExtensionsPage />);

  expect(await screen.findByText("部署装入")).toBeInTheDocument();
  // 没有显示名时退回稳定 id,不留一格空白。
  const row = screen.getByRole("row", { name: /corp\.sample/ });
  expect(within(row).getAllByText("corp.sample")).toHaveLength(2);

  await user.click(screen.getByRole("button", { name: /^展开 / }));
  expect(screen.getByText("没有服务端接入。")).toBeInTheDocument();
  expect(screen.getByText("没有界面入口。")).toBeInTheDocument();
});


test("空清单给出可操作的文案而不是一张空表", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([]));

  render(<AdminExtensionsPage />);

  expect(await screen.findByText(/没有读到任何扩展/)).toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});
