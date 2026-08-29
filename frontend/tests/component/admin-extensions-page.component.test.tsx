import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  fetchLoadedExtensions: vi.fn(),
  setExtensionRuntimeEnabled: vi.fn(),
}));

vi.mock("../../app/auth.ts", () => ({ fetchMe: mocks.fetchMe }));
vi.mock("../../app/admin/extensions/api.ts", () => ({
  KNOWN_API_VERSION: "1",
  fetchLoadedExtensions: mocks.fetchLoadedExtensions,
  setExtensionRuntimeEnabled: mocks.setExtensionRuntimeEnabled,
}));

import AdminExtensionsPage from "../../app/admin/extensions/page";
import type { LoadedExtension } from "../../app/admin/extensions/api";
import { humanizedError } from "../../app/errors";


// 合成的十行内建拓扑,与后端那份运维视图契约测试所钉的内建扩展条数同量级。
// 第一行带完整的两类接入明细,其余只需要身份与计数。
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
  // builtin 只读:三个运行时字段恒 null。
  runtimeEnabled: null,
  runtimeUpdatedBy: null,
  runtimeUpdatedAt: null,
};

/** deployment 行的合成 fixture,默认「无开关行 = 启用」。 */
function deploymentExtension(overrides: Partial<LoadedExtension> = {}): LoadedExtension {
  return {
    id: "corp.sample",
    displayName: "",
    version: "",
    trust: "deployment",
    contributions: [],
    uiContributions: [],
    runtimeEnabled: true,
    runtimeUpdatedBy: null,
    runtimeUpdatedAt: null,
    ...overrides,
  };
}

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

beforeEach(() => {
  mocks.fetchMe.mockReset();
  mocks.fetchLoadedExtensions.mockReset();
  mocks.setExtensionRuntimeEnabled.mockReset();
});

// ⚠ 参数**必须**显式标注 `readonly LoadedExtension[]`。不标注时 TypeScript 从默认值
// 推断参数类型,而默认值里每一行的 `trust` 都是 `"builtin" as const`——推断出的形参
// 类型因此是 `trust: "builtin"`,任何传 `"deployment"` 的调用点直接 TS2322。这不是
// 风格问题:`npm run lint` / `npx tsc --noEmit` 会因此整条红。
function topology(extensions: readonly LoadedExtension[] = [
  AGENT_PROFILE,
  ...OTHER_IDS.map((id) => ({
    id,
    displayName: id,
    version: "1.0.0",
    trust: "builtin" as const,
    contributions: [{ id, point: "retrieval.contributor", kind: "contributor" }],
    uiContributions: [],
    runtimeEnabled: null,
    runtimeUpdatedBy: null,
    runtimeUpdatedAt: null,
  })),
]) {
  return { apiVersion: "1", versionRecognized: true, extensions };
}

/** 与 page.tsx 里 `formatRuntimeTimestamp` **同规格、独立实现**的本地时间文本
 *  计算——不 import 生产代码里那个未导出的私有函数,这样断言才不会在生产实现被
 *  错误地改成恒定空串(或任何其它退化)时,连着期望值一起垮成同一个假象而继续
 *  测出「绿」。 */
function localTimeText(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
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
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([deploymentExtension()]));
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


// --------------------------------------------------------------------------
// 运行时开关(T5):builtin 无控件、deployment 有控件、行级忙碌/成功/失败态、
// 多行互不影响、两种 updated_at 后端形状都能渲染。
// --------------------------------------------------------------------------

test("内置扩展显示「始终启用」,没有任何开关控件", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology());

  render(<AdminExtensionsPage />);

  expect(await screen.findByText("Ask agent-profile completion")).toBeInTheDocument();
  expect(screen.getAllByText("始终启用")).toHaveLength(10);
  // 可访问名是「动作 + 插件名」(见 page.tsx ExtensionRuntimeCell),前缀匹配即可,
  // 不必关心后面缀的是哪个插件名。
  expect(screen.queryByRole("button", { name: /^(启用|停用|启用中…|停用中…)/ })).not.toBeInTheDocument();
});


test("部署插件行带启停按钮和当前状态徽标", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([deploymentExtension()]));

  render(<AdminExtensionsPage />);

  const row = await screen.findByRole("row", { name: /corp\.sample/ });
  expect(within(row).getByText("已启用")).toBeInTheDocument();
  // 可访问名带插件名消歧(WCAG 2.5.3 Label in Name:可见文案「停用」是可访问名
  // 的前缀,不是被 aria-label 整个盖掉)。
  expect(within(row).getByRole("button", { name: "停用 corp.sample" })).toBeInTheDocument();
});


test("已停用的部署插件整行视觉弱化(ext-row-runtime-disabled)", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([
    deploymentExtension({ id: "corp.disabled", runtimeEnabled: false }),
  ]));

  render(<AdminExtensionsPage />);

  const row = await screen.findByRole("row", { name: /corp\.disabled/ });
  expect(row).toHaveClass("ext-row-runtime-disabled");
});


test("启用中的部署插件整行不带弱化 class", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([
    deploymentExtension({ id: "corp.enabled", runtimeEnabled: true }),
  ]));

  render(<AdminExtensionsPage />);

  const row = await screen.findByRole("row", { name: /corp\.enabled/ });
  expect(row).not.toHaveClass("ext-row-runtime-disabled");
});


test("点击停用:按钮立即进入忙碌态(禁用+忙碌文案),服务端响应前行状态不变", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([deploymentExtension()]));
  let resolvePatch: (value: unknown) => void = () => {};
  mocks.setExtensionRuntimeEnabled.mockImplementation(
    () => new Promise((resolve) => { resolvePatch = resolve; }),
  );
  const user = userEvent.setup();

  render(<AdminExtensionsPage />);
  const row = await screen.findByRole("row", { name: /corp\.sample/ });
  await user.click(within(row).getByRole("button", { name: "停用 corp.sample" }));

  const busyButton = within(row).getByRole("button", { name: "停用中… corp.sample" });
  expect(busyButton).toBeDisabled();
  expect(busyButton).toHaveAttribute("aria-busy", "true");
  // 不做乐观更新:响应回来之前徽标仍是「已启用」。
  expect(within(row).getByText("已启用")).toBeInTheDocument();
  expect(mocks.setExtensionRuntimeEnabled).toHaveBeenCalledWith("corp.sample", false);

  resolvePatch({
    pluginId: "corp.sample",
    runtimeEnabled: false,
    runtimeUpdatedBy: "user-admin-1",
    runtimeUpdatedAt: "2026-08-29T10:00:00",
  });

  expect(await within(row).findByText("已停用")).toBeInTheDocument();
  const restoredButton = within(row).getByRole("button", { name: "启用 corp.sample" });
  expect(restoredButton).not.toBeDisabled();
  // 空闲态:`aria-busy={pending || undefined}`(仓库既有写法,见 groups-page.tsx/
  // page.tsx 里另外三处)让这个属性直接不出现,不是留一个字符串 "false"。
  expect(restoredButton).not.toHaveAttribute("aria-busy");
  // 审计微文案紧邻着落在这一行,不是页面顶部横幅——真实的时间文本(见 P1 修复),
  // 不是只有 updated_by 那半句。
  expect(within(row).getByText(`${localTimeText("2026-08-29T10:00:00")} · 由 user-admin-1 更新`))
    .toBeInTheDocument();
  expect(within(row).queryByText(/Invalid Date/)).not.toBeInTheDocument();
});


test("失败:行内错误文案(经 toUserMessage),不出现全局横幅,按钮恢复可用", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([deploymentExtension()]));
  mocks.setExtensionRuntimeEnabled.mockRejectedValue(humanizedError("扩展停用失败,请稍后重试", 500));
  const user = userEvent.setup();

  render(<AdminExtensionsPage />);
  const row = await screen.findByRole("row", { name: /corp\.sample/ });
  await user.click(within(row).getByRole("button", { name: "停用 corp.sample" }));

  // 真正的「不发全局横幅」守卫:错误文案必须能在**这一行的作用域内**找到——
  // `within(row)` 找不到就会抛,不是一条摆设断言。
  expect(await within(row).findByText(/操作没成功：扩展停用失败,请稍后重试/)).toBeInTheDocument();
  // 失败没有翻转状态:按钮回到「停用」而不是卡在忙碌文案或翻成「启用」。
  const button = within(row).getByRole("button", { name: "停用 corp.sample" });
  expect(button).not.toBeDisabled();
  expect(within(row).getByText("已启用")).toBeInTheDocument();
});


test("两个部署插件各自独立:一行 pending 时另一行按钮仍可点", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([
    deploymentExtension({ id: "corp.alpha" }),
    deploymentExtension({ id: "corp.beta" }),
  ]));
  let resolveAlpha: (value: unknown) => void = () => {};
  mocks.setExtensionRuntimeEnabled.mockImplementation((pluginId: string) => {
    if (pluginId === "corp.alpha") return new Promise((resolve) => { resolveAlpha = resolve; });
    return Promise.resolve({
      pluginId: "corp.beta",
      runtimeEnabled: false,
      runtimeUpdatedBy: "user-admin-2",
      runtimeUpdatedAt: "2026-08-29T11:00:00",
    });
  });
  const user = userEvent.setup();

  render(<AdminExtensionsPage />);
  const alphaRow = await screen.findByRole("row", { name: /corp\.alpha/ });
  const betaRow = await screen.findByRole("row", { name: /corp\.beta/ });

  await user.click(within(alphaRow).getByRole("button", { name: "停用 corp.alpha" }));
  expect(within(alphaRow).getByRole("button", { name: "停用中… corp.alpha" })).toBeDisabled();
  // beta 这一行完全没被 alpha 的 pending 状态影响。
  expect(within(betaRow).getByRole("button", { name: "停用 corp.beta" })).not.toBeDisabled();

  await user.click(within(betaRow).getByRole("button", { name: "停用 corp.beta" }));
  expect(await within(betaRow).findByText("已停用")).toBeInTheDocument();
  // alpha 仍卡在忙碌态,没有被 beta 的成功响应带偏。
  expect(within(alphaRow).getByRole("button", { name: "停用中… corp.alpha" })).toBeDisabled();

  resolveAlpha({
    pluginId: "corp.alpha",
    runtimeEnabled: false,
    runtimeUpdatedBy: "user-admin-1",
    runtimeUpdatedAt: "2026-08-29T10:00:00",
  });
  expect(await within(alphaRow).findByText("已停用")).toBeInTheDocument();
});


// P1 修复:上一版这里只断言 `getByText(/user-admin-1/)`,而降级句「由 user-admin-1
// 更新」同样含这个子串——`formatRuntimeTimestamp` 被改成恒返回 "" 之后,整套用例
// 照样绿,时间部分从没被真正断言过。现在断言渲染出的完整时间文本本身(用一份
// **独立**实现的 `localTimeText` 重算期望值,不 import 生产的私有函数),外加一条
// 专门覆盖「时间戳解析失败」降级路径的用例。
test("updated_at 的两种后端形状(SQLite 裸本地 ISO / PG 带偏移)都渲染出真实的时间文本", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  const sqliteIso = "2026-08-29T10:00:00";
  const pgIso = "2026-08-29T10:00:00+00:00";
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([
    deploymentExtension({
      id: "corp.sqlite-shape",
      runtimeEnabled: false,
      runtimeUpdatedBy: "user-admin-1",
      runtimeUpdatedAt: sqliteIso,
    }),
    deploymentExtension({
      id: "corp.pg-shape",
      runtimeEnabled: false,
      runtimeUpdatedBy: "user-admin-2",
      runtimeUpdatedAt: pgIso,
    }),
  ]));

  render(<AdminExtensionsPage />);

  const sqliteRow = await screen.findByRole("row", { name: /corp\.sqlite-shape/ });
  const pgRow = await screen.findByRole("row", { name: /corp\.pg-shape/ });
  // 精确匹配整句审计文案,不是只匹配 updated_by 那半句:两行各自的时间文本必须
  // 真的出现在页面上,不能是空串或被吞掉。
  expect(within(sqliteRow).getByText(`${localTimeText(sqliteIso)} · 由 user-admin-1 更新`))
    .toBeInTheDocument();
  expect(within(pgRow).getByText(`${localTimeText(pgIso)} · 由 user-admin-2 更新`))
    .toBeInTheDocument();
  expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
});


test("updated_at 解析不出来时降级为只报「由谁更新」,不泄漏 Invalid Date", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([
    deploymentExtension({
      id: "corp.bad-timestamp",
      runtimeEnabled: false,
      runtimeUpdatedBy: "user-admin-1",
      runtimeUpdatedAt: "not-a-date",
    }),
  ]));

  render(<AdminExtensionsPage />);

  const row = await screen.findByRole("row", { name: /corp\.bad-timestamp/ });
  // 降级句里没有任何时间文本,只剩「由谁更新」——不是把 "Invalid Date" 拼进去。
  expect(within(row).getByText("由 user-admin-1 更新")).toBeInTheDocument();
  expect(within(row).queryByText(/Invalid Date/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
});


// P2-5 修复:行 key 全程用请求 pluginId,不用响应体回声的 result.pluginId。这里
// 让 corp.alpha 的写请求回声一个完全不相关的 id(模拟畸形响应/契约漂移),断言
// 用户实际点击的那一行(corp.alpha)照样被正确更新,而没被点过的 corp.beta 分毫
// 不受影响——如果生产代码退回按 `result.pluginId` 查找,这条用例会失败:要么两行
// 都没更新,要么 beta 被错误地写上 alpha 这次操作的数据。
test("服务端回声的 plugin_id 与请求不一致时,仍按用户实际点击的那一行更新", async () => {
  mocks.fetchMe.mockResolvedValue({ id: "user-local", role: "admin" });
  mocks.fetchLoadedExtensions.mockResolvedValue(topology([
    deploymentExtension({ id: "corp.alpha" }),
    deploymentExtension({ id: "corp.beta" }),
  ]));
  mocks.setExtensionRuntimeEnabled.mockResolvedValue({
    pluginId: "corp.totally-unrelated-id", // 畸形回声,不等于请求的 corp.alpha
    runtimeEnabled: false,
    runtimeUpdatedBy: "user-admin-1",
    runtimeUpdatedAt: "2026-08-29T10:00:00",
  });
  const user = userEvent.setup();

  render(<AdminExtensionsPage />);
  const alphaRow = await screen.findByRole("row", { name: /corp\.alpha/ });
  const betaRow = await screen.findByRole("row", { name: /corp\.beta/ });

  await user.click(within(alphaRow).getByRole("button", { name: "停用 corp.alpha" }));

  // 用户点的是 alpha,更新的必须是 alpha——不是响应体里那个不相关的 id。
  expect(await within(alphaRow).findByText("已停用")).toBeInTheDocument();
  expect(within(alphaRow).getByRole("button", { name: "启用 corp.alpha" })).toBeInTheDocument();
  // beta 完全没被这次回声异常牵连:还是启用态,还是它自己的按钮名。
  expect(within(betaRow).getByText("已启用")).toBeInTheDocument();
  expect(within(betaRow).getByRole("button", { name: "停用 corp.beta" })).toBeInTheDocument();
});
