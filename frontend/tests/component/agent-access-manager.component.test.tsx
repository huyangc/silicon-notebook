import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ requestJson: vi.fn() }));

vi.mock("../../app/api-client.ts", () => ({ requestJson: mocks.requestJson }));

import { AgentAccessManager } from "../../app/agent-access-manager";
import { humanizedError } from "../../app/errors";

const notebooks = [
  { id: "nb-1", name: "工艺笔记本", purpose: "", primary_domain: "", status: "ready", counts: {}, created_label: "", access: "owner" },
  { id: "nb-2", name: "器件笔记本", purpose: "", primary_domain: "", status: "ready", counts: {}, created_label: "", access: "owner" },
];

const activeToken = {
  id: "token-1",
  agent_profile_id: "agent-1",
  profile_name: "Claude Code",
  scopes: ["read"],
  default_notebook_id: "nb-1",
  notebook_ids: ["nb-1"],
  expires_at: "2030-01-01T00:00:00Z",
  revoked_at: null,
  last_used_at: null,
  created_at: "2026-09-01T00:00:00Z",
  copyable: true,
};

const revokedToken = { ...activeToken, id: "token-2", profile_name: "Codex", revoked_at: "2026-09-02T00:00:00Z", copyable: false };

type Route = (options: RequestInit, path: string) => unknown;

function routeRequests(routes: Record<string, Route>) {
  mocks.requestJson.mockImplementation(async (path: string, options: RequestInit = {}) => {
    const method = options.method ?? "GET";
    const key = `${method} ${path.split("?")[0]}`;
    const route = routes[key];
    if (!route) throw new Error(`unexpected request ${key}`);
    return route(options, path);
  });
}

function baseRoutes(extra: Record<string, Route> = {}): Record<string, Route> {
  return {
    "GET /agent-profiles": () => [],
    "GET /agent-tokens": () => [activeToken, revokedToken],
    "GET /notebooks": () => notebooks,
    ...extra,
  };
}

function tokenRow(name: string): HTMLElement {
  const row = screen.getByText(name).closest("article");
  expect(row).not.toBeNull();
  return row as HTMLElement;
}

function requestsTo(key: string): RequestInit[] {
  return mocks.requestJson.mock.calls
    .filter(([path, options]) => `${(options as RequestInit | undefined)?.method ?? "GET"} ${String(path).split("?")[0]}` === key)
    .map(([, options]) => options as RequestInit);
}

beforeEach(() => {
  mocks.requestJson.mockReset();
  // 「隐藏已撤销和已过期」会写 localStorage;不同用例之间不能互相继承这个偏好。
  try { window.localStorage.clear(); } catch { /* 存储不可用时本来就不会持久化 */ }
});

afterEach(() => {
  vi.useRealTimers();
});

test("已签发 token 可以回来修改权限,保存后行内确认并按整体替换提交", async () => {
  let resolveSave!: (value: unknown) => void;
  routeRequests(baseRoutes({
    "PUT /agent-tokens/token-1/access": () => new Promise((resolve) => { resolveSave = resolve; }),
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByText("Claude Code");
  // 已撤销的 token 默认隐藏;关掉开关后能看到,但不给修改入口。
  expect(screen.queryByText("Codex")).not.toBeInTheDocument();
  await user.click(screen.getByRole("checkbox", { name: "隐藏已撤销和已过期" }));
  expect(within(tokenRow("Codex")).queryByRole("button", { name: /修改权限/ })).not.toBeInTheDocument();

  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: /修改权限/ }));
  const editor = screen.getByRole("form", { name: "修改 Claude Code 的 token 权限" });
  const save = within(editor).getByRole("button", { name: "保存" });
  expect(within(editor).getByRole("checkbox", { name: "读取" })).toBeChecked();
  // 没改任何东西时不能保存。
  expect(save).toBeDisabled();

  await user.click(within(editor).getByRole("checkbox", { name: "问答" }));
  await user.click(within(editor).getByRole("checkbox", { name: "器件笔记本" }));
  expect(save).toBeEnabled();
  await user.click(save);

  // 在途:按钮换文案并禁用,取消与表单字段也都点不动。
  expect(within(editor).getByRole("button", { name: "保存中…" })).toBeDisabled();
  expect(within(editor).getByRole("button", { name: "取消" })).toBeDisabled();
  expect(within(editor).getByRole("checkbox", { name: "问答" })).toBeDisabled();
  expect(within(editor).getByLabelText("过期时间")).toBeDisabled();
  const saves = requestsTo("PUT /agent-tokens/token-1/access");
  expect(saves).toHaveLength(1);
  const body = JSON.parse(String(saves[0].body)) as Record<string, unknown>;
  expect(body).toEqual({
    scopes: ["read", "ask"],
    default_notebook_id: "nb-1",
    notebook_ids: ["nb-1", "nb-2"],
    // 过期时间没动:原样回传存储值,而不是从只到分钟的本地时间重算。
    expires_at: "2030-01-01T00:00:00Z",
    // 编辑器打开时读到的配置,供服务端拒绝旧标签页的覆盖写。
    expected: {
      scopes: ["read"],
      default_notebook_id: "nb-1",
      notebook_ids: ["nb-1"],
      expires_at: "2030-01-01T00:00:00Z",
    },
  });

  const listLoadsBeforeSave = requestsTo("GET /agent-tokens").length;
  vi.useFakeTimers();
  await act(async () => {
    resolveSave({ ...activeToken, scopes: ["read", "ask"], notebook_ids: ["nb-1", "nb-2"] });
  });

  const row = tokenRow("Claude Code");
  expect(screen.queryByRole("form", { name: "修改 Claude Code 的 token 权限" })).not.toBeInTheDocument();
  expect(within(row).getByRole("status")).toHaveTextContent("已保存，立即生效");
  expect(row).toHaveTextContent("读取 · 问答");
  expect(row).toHaveTextContent("允许 2 个笔记本");
  // 成功回执本身就是新配置,不重拉列表(重拉会丢掉「加载更多」翻到的行)。
  expect(requestsTo("GET /agent-tokens")).toHaveLength(listLoadsBeforeSave);

  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(within(tokenRow("Claude Code")).queryByRole("status")).not.toBeInTheDocument();
  expect(tokenRow("Claude Code")).toHaveTextContent("读取 · 问答");
});

test("保存失败时错误落在编辑行内,编辑内容保留以便重试", async () => {
  routeRequests(baseRoutes({
    "PUT /agent-tokens/token-1/access": () => {
      throw humanizedError("白名单里有你已无权访问的笔记本，请取消勾选后再保存", 422);
    },
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByText("Claude Code");
  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: /修改权限/ }));
  const editor = screen.getByRole("form", { name: "修改 Claude Code 的 token 权限" });
  await user.click(within(editor).getByRole("checkbox", { name: "提交" }));
  await user.click(within(editor).getByRole("button", { name: "保存" }));

  expect(await within(editor).findByRole("alert")).toHaveTextContent("白名单里有你已无权访问的笔记本");
  expect(within(editor).getByRole("checkbox", { name: "提交" })).toBeChecked();
  expect(within(editor).getByRole("button", { name: "保存" })).toBeEnabled();
});

function pageOffset(path: string): number {
  return Number(new URLSearchParams(path.split("?")[1]).get("offset"));
}

function pagedTokens() {
  const firstPage = Array.from({ length: 25 }, (_, index) => ({
    ...activeToken,
    id: `token-p1-${index}`,
    profile_name: `第一页代理${index}`,
  }));
  const secondPageToken = { ...activeToken, id: "token-p2", profile_name: "第二页代理" };
  return { firstPage, secondPageToken };
}

test("「加载更多」翻到的 token 保存成功或撞 409 后仍留在列表里,反馈也留在该行", async () => {
  const { firstPage, secondPageToken } = pagedTokens();
  let saveAttempt = 0;
  routeRequests(baseRoutes({
    "GET /agent-tokens": (_options, path) => {
      const offset = pageOffset(path);
      return offset === 0 ? firstPage : [secondPageToken];
    },
    "PUT /agent-tokens/token-p2/access": () => {
      saveAttempt += 1;
      if (saveAttempt === 1) return { ...secondPageToken, scopes: ["read", "ask"] };
      throw humanizedError("这个 Token 的权限刚在别处被修改过，请取消后重新打开再改", 409);
    },
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByText("第一页代理0");
  await user.click(screen.getByRole("button", { name: "加载更多 Token" }));
  await screen.findByText("第二页代理");

  await user.click(within(tokenRow("第二页代理")).getByRole("button", { name: /修改权限/ }));
  let editor = screen.getByRole("form", { name: "修改 第二页代理 的 token 权限" });
  await user.click(within(editor).getByRole("checkbox", { name: "问答" }));
  const listLoads = requestsTo("GET /agent-tokens").length;
  await user.click(within(editor).getByRole("button", { name: "保存" }));

  expect(await within(tokenRow("第二页代理")).findByRole("status")).toHaveTextContent("已保存，立即生效");
  expect(requestsTo("GET /agent-tokens")).toHaveLength(listLoads);

  await user.click(within(tokenRow("第二页代理")).getByRole("button", { name: /修改权限/ }));
  editor = screen.getByRole("form", { name: "修改 第二页代理 的 token 权限" });
  await user.click(within(editor).getByRole("checkbox", { name: "提交" }));
  await user.click(within(editor).getByRole("button", { name: "保存" }));

  expect(await within(editor).findByRole("alert")).toHaveTextContent("这个 Token 的权限刚在别处被修改过");
  // 按已加载深度重拉(第一页 + 第二页),而不是只回到第一页。
  await waitFor(() => expect(requestsTo("GET /agent-tokens")).toHaveLength(listLoads + 2));
  expect(screen.getByRole("form", { name: "修改 第二页代理 的 token 权限" })).toBeInTheDocument();
  expect(within(tokenRow("第二页代理")).getByRole("form")).toHaveTextContent("这个 Token 的权限刚在别处被修改过");
});

test("保存之前就已发出的翻页请求晚到时,不会把刚保存的行覆盖回旧配置", async () => {
  const { firstPage } = pagedTokens();
  const edited = firstPage[0];
  let resolveSave!: (value: unknown) => void;
  let resolvePage!: (value: unknown) => void;
  routeRequests(baseRoutes({
    "GET /agent-tokens": (_options, path) => {
      const offset = pageOffset(path);
      if (offset === 0) return firstPage;
      return new Promise((resolve) => { resolvePage = resolve; });
    },
    [`PUT /agent-tokens/${edited.id}/access`]: () => new Promise((resolve) => { resolveSave = resolve; }),
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByText("第一页代理0");
  await user.click(within(tokenRow("第一页代理0")).getByRole("button", { name: /修改权限/ }));
  const editor = screen.getByRole("form", { name: "修改 第一页代理0 的 token 权限" });
  await user.click(within(editor).getByRole("checkbox", { name: "问答" }));
  await user.click(within(editor).getByRole("button", { name: "保存" }));
  // 保存在途时翻页;这页响应里带着这一行保存前的旧副本。
  await user.click(screen.getByRole("button", { name: "加载更多 Token" }));

  await act(async () => { resolveSave({ ...edited, scopes: ["read", "ask"] }); });
  expect(tokenRow("第一页代理0")).toHaveTextContent("读取 · 问答");
  await act(async () => { resolvePage([edited, { ...activeToken, id: "token-late", profile_name: "晚到代理" }]); });

  expect(await screen.findByText("晚到代理")).toBeInTheDocument();
  expect(tokenRow("第一页代理0")).toHaveTextContent("读取 · 问答");
});

test("409 冲突时错误留在编辑行内并重拉列表,以服务端现状为准", async () => {
  routeRequests(baseRoutes({
    "PUT /agent-tokens/token-1/access": () => {
      throw humanizedError("这个 Token 的权限刚在别处被修改过，请取消后重新打开再改", 409);
    },
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByText("Claude Code");
  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: /修改权限/ }));
  const editor = screen.getByRole("form", { name: "修改 Claude Code 的 token 权限" });
  await user.click(within(editor).getByRole("checkbox", { name: "提交" }));
  const listLoads = requestsTo("GET /agent-tokens").length;
  await user.click(within(editor).getByRole("button", { name: "保存" }));

  expect(await within(editor).findByRole("alert")).toHaveTextContent("这个 Token 的权限刚在别处被修改过");
  await waitFor(() => expect(requestsTo("GET /agent-tokens").length).toBeGreaterThan(listLoads));
});

test("白名单里已无法访问的笔记本照样列出,可以取消勾选", async () => {
  routeRequests(baseRoutes({
    "GET /agent-tokens": () => [{ ...activeToken, notebook_ids: ["nb-1", "nb-gone"] }],
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByText("Claude Code");
  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: /修改权限/ }));
  const editor = screen.getByRole("form", { name: "修改 Claude Code 的 token 权限" });
  const gone = within(editor).getByRole("checkbox", { name: "无法访问的笔记本" });
  expect(gone).toBeChecked();

  await user.click(gone);
  await waitFor(() => expect(within(editor).getByRole("button", { name: "保存" })).toBeEnabled());
});

test("撤销要在行内再确认一次,放弃则什么都不发", async () => {
  let revoked = false;
  routeRequests(baseRoutes({
    "GET /agent-tokens": () => [revoked ? { ...activeToken, revoked_at: "2026-09-03T00:00:00Z" } : activeToken],
    "DELETE /agent-tokens/token-1": () => {
      revoked = true;
      return { ...activeToken, revoked_at: "2026-09-03T00:00:00Z" };
    },
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByText("Claude Code");
  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: "撤销" }));
  expect(tokenRow("Claude Code")).toHaveTextContent("撤销后立即失效，且不能恢复");
  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: "不撤销" }));
  expect(requestsTo("DELETE /agent-tokens/token-1")).toHaveLength(0);
  expect(within(tokenRow("Claude Code")).getByRole("button", { name: /修改权限/ })).toBeInTheDocument();

  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: "撤销" }));
  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: "确认撤销" }));
  expect(await within(tokenRow("Claude Code")).findByText("已撤销")).toBeInTheDocument();
  expect(requestsTo("DELETE /agent-tokens/token-1")).toHaveLength(1);
});

test("Profile 已停用的 token 不提供修改入口", async () => {
  routeRequests(baseRoutes({
    "GET /agent-profiles": () => [{
      id: "agent-1", owner_id: "user-1", name: "Claude Code", description: "", status: "revoked",
      created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-02T00:00:00Z",
    }],
    "GET /agent-tokens": () => [activeToken],
  }));
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  const row = (await screen.findAllByText("Claude Code"))
    .map((node) => node.closest("article"))
    .find((node): node is HTMLElement => node !== null);
  expect(row).toBeDefined();
  expect(within(row as HTMLElement).getByText("Profile 已停用")).toBeInTheDocument();
  expect(within(row as HTMLElement).queryByRole("button", { name: /修改权限/ })).not.toBeInTheDocument();
});

const profileOne = {
  id: "agent-1", owner_id: "user-1", name: "Claude Code", description: "", status: "active",
  created_at: "2026-09-01T00:00:00Z", updated_at: "2026-09-02T00:00:00Z",
};

function issueForm(): HTMLElement {
  return screen.getByRole("heading", { name: "签发 Token" }).closest("form") as HTMLElement;
}

test("只有一个启用中的 Profile 时自动选中;权限五档带说明,默认只勾读取", async () => {
  routeRequests(baseRoutes({ "GET /agent-profiles": () => [profileOne] }));
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  const form = await waitFor(() => {
    const node = issueForm();
    expect(within(node).getByRole("combobox", { name: "Profile" })).toHaveValue("agent-1");
    return node;
  });
  const names = ["读取", "问答", "提交", "管理", "删除"];
  for (const name of names) expect(within(form).getByRole("checkbox", { name })).toBeInTheDocument();
  expect(within(form).getByRole("checkbox", { name: "读取" })).toBeChecked();
  expect(within(form).getByRole("checkbox", { name: "问答" })).not.toBeChecked();
  expect(within(form).getByRole("checkbox", { name: "管理" })).toHaveAccessibleDescription(/仅对你拥有的笔记本生效/);
  expect(within(form).getByRole("checkbox", { name: "删除" })).toHaveAccessibleDescription(/不能恢复/);
});

test("多个启用中的 Profile 不替用户挑", async () => {
  routeRequests(baseRoutes({ "GET /agent-profiles": () => [profileOne, { ...profileOne, id: "agent-2", name: "Codex" }] }));
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);

  await screen.findByRole("heading", { name: "签发 Token" });
  await waitFor(() => expect(mocks.requestJson).toHaveBeenCalled());
  await waitFor(() => expect(within(issueForm()).getByRole("combobox", { name: "Profile" })).toHaveValue(""));
});

test("权限与笔记本白名单各有全选/取消全选和已选计数;取消全选保留默认笔记本", async () => {
  routeRequests(baseRoutes({ "GET /agent-profiles": () => [profileOne] }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);
  await screen.findByRole("heading", { name: "签发 Token" });
  const scopes = await waitFor(() => {
    const node = within(issueForm()).getByText("权限", { selector: "legend" }).closest("fieldset") as HTMLElement;
    expect(within(node).getByRole("checkbox", { name: "读取" })).toBeChecked();
    return node;
  });
  const books = within(issueForm()).getByText("笔记本白名单", { selector: "legend" }).closest("fieldset") as HTMLElement;

  expect(within(scopes).getByText("已选 1 / 5")).toBeInTheDocument();
  await user.click(within(scopes).getByRole("button", { name: "全选" }));
  expect(within(scopes).getByText("已选 5 / 5")).toBeInTheDocument();
  expect(within(scopes).getByRole("button", { name: "全选" })).toBeDisabled();
  await user.click(within(scopes).getByRole("button", { name: "取消全选" }));
  expect(within(scopes).getByText("已选 0 / 5")).toBeInTheDocument();

  expect(within(books).getByText("已选 1 / 2")).toBeInTheDocument();
  await user.click(within(books).getByRole("button", { name: "全选" }));
  expect(within(books).getByText("已选 2 / 2")).toBeInTheDocument();
  await user.click(within(books).getByRole("button", { name: "取消全选" }));
  expect(within(books).getByText("已选 1 / 2")).toBeInTheDocument();
  expect(within(books).getByRole("checkbox", { name: "工艺笔记本" })).toBeChecked();
});

test("白名单里没有自己拥有的笔记本时,管理/删除禁用并说明原因,全选不含它们", async () => {
  const shared = [{ ...notebooks[0], access: "reader" }, { ...notebooks[1], access: "reader" }];
  routeRequests(baseRoutes({
    "GET /agent-profiles": () => [profileOne],
    "GET /notebooks": () => shared,
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);
  await screen.findByRole("heading", { name: "签发 Token" });
  const form = issueForm();
  await waitFor(() => expect(within(form).getByRole("checkbox", { name: "读取" })).toBeChecked());

  expect(within(form).getByRole("checkbox", { name: "管理" })).toBeDisabled();
  expect(within(form).getByRole("checkbox", { name: "删除" })).toBeDisabled();
  expect(within(form).getAllByText(/白名单里没有你拥有的笔记本/).length).toBeGreaterThan(0);
  const scopes = within(form).getByText("权限", { selector: "legend" }).closest("fieldset") as HTMLElement;
  await user.click(within(scopes).getByRole("button", { name: "全选" }));
  expect(within(form).getByRole("checkbox", { name: "提交" })).toBeChecked();
  expect(within(form).getByRole("checkbox", { name: "管理" })).not.toBeChecked();
});

test("勾了管理后把白名单里自己拥有的笔记本都取消,提交被拦下并说明", async () => {
  const mixed = [{ ...notebooks[0], access: "reader" }, { ...notebooks[1], access: "owner" }];
  routeRequests(baseRoutes({
    "GET /agent-profiles": () => [profileOne],
    "GET /notebooks": () => mixed,
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);
  await screen.findByRole("heading", { name: "签发 Token" });
  const form = issueForm();
  await waitFor(() => expect(within(form).getByRole("checkbox", { name: "读取" })).toBeChecked());

  await user.click(within(form).getByRole("checkbox", { name: "器件笔记本" }));
  await user.click(within(form).getByRole("checkbox", { name: "管理" }));
  expect(within(form).getByRole("button", { name: /签发 Token/ })).toBeEnabled();
  // 取消主人那本:管理失去落脚点。
  await user.click(within(form).getByRole("checkbox", { name: "器件笔记本" }));
  expect(within(form).getByRole("button", { name: /签发 Token/ })).toBeDisabled();
  expect(within(form).getByRole("status")).toHaveTextContent("只对你拥有的笔记本生效");
  // 已勾选的管理仍可取消,不会把用户卡死。
  expect(within(form).getByRole("checkbox", { name: "管理" })).toBeEnabled();
});

test("笔记本超过 8 个才出现筛选框,全选只作用于当前可见项", async () => {
  const many = Array.from({ length: 10 }, (_, index) => ({ ...notebooks[0], id: `nb-m${index}`, name: index < 3 ? `电路${index}` : `材料${index}` }));
  routeRequests(baseRoutes({
    "GET /agent-profiles": () => [profileOne],
    "GET /notebooks": () => many,
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);
  await screen.findByRole("heading", { name: "签发 Token" });
  const form = issueForm();
  const filter = await within(form).findByRole("searchbox", { name: "按名称筛选笔记本" });
  await user.type(filter, "电路");
  const books = within(form).getByText("笔记本白名单", { selector: "legend" }).closest("fieldset") as HTMLElement;
  expect(within(books).queryByRole("checkbox", { name: "材料5" })).not.toBeInTheDocument();
  await user.click(within(books).getByRole("button", { name: "全选" }));
  // 默认笔记本 + 3 个电路笔记本(默认笔记本若是电路0则重叠)。
  expect(within(books).getByText(/已选 [34] \/ 10/)).toBeInTheDocument();
  await user.clear(filter);
  expect(within(books).getByRole("checkbox", { name: "材料5" })).not.toBeChecked();
});

test("过期时间快捷项填入输入框并呈选中态", async () => {
  routeRequests(baseRoutes({ "GET /agent-profiles": () => [profileOne] }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);
  await screen.findByRole("heading", { name: "签发 Token" });
  const form = issueForm();
  const input = await waitFor(() => {
    const node = within(form).getByLabelText("过期时间") as HTMLInputElement;
    expect(node.value).not.toBe("");
    return node;
  });
  expect(within(form).getByRole("button", { name: "30 天" })).toHaveAttribute("aria-pressed", "true");
  const before = input.value;
  await user.click(within(form).getByRole("button", { name: "7 天" }));
  expect(input.value).not.toBe(before);
  expect(within(form).getByRole("button", { name: "7 天" })).toHaveAttribute("aria-pressed", "true");
  expect(within(form).getByRole("button", { name: "30 天" })).toHaveAttribute("aria-pressed", "false");
});

test("复制 token:按钮自身显示复制中/已复制,明文只经 secret 端点取得", async () => {
  let resolveSecret!: (value: unknown) => void;
  routeRequests(baseRoutes({
    "GET /agent-tokens/token-1/secret": () => new Promise((resolve) => { resolveSecret = resolve; }),
  }));
  const user = userEvent.setup();
  const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);
  await screen.findByText("Claude Code");

  const row = tokenRow("Claude Code");
  await user.click(within(row).getByRole("button", { name: "复制 token" }));
  expect(within(row).getByRole("button", { name: "复制中…" })).toBeDisabled();
  await act(async () => { resolveSecret({ token: "sk-secret-1" }); });
  expect(await within(row).findByRole("button", { name: "已复制" })).toBeInTheDocument();
  expect(writeText).toHaveBeenCalledWith("sk-secret-1");
  expect(document.body.textContent).not.toContain("sk-secret-1");
});

test("复制 token 失败:错误落在该行;剪贴板不可用时在紧邻处给出可选中的明文", async () => {
  let call = 0;
  routeRequests(baseRoutes({
    "GET /agent-tokens/token-1/secret": () => {
      call += 1;
      if (call === 1) throw humanizedError("这个 token 签发于旧版本，无法再次复制，如需请重新签发", 409);
      return { token: "sk-secret-2" };
    },
  }));
  const user = userEvent.setup();
  Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
  Object.defineProperty(document, "execCommand", { value: () => false, configurable: true });
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);
  await screen.findByText("Claude Code");
  const row = tokenRow("Claude Code");

  await user.click(within(row).getByRole("button", { name: "复制 token" }));
  expect(await within(row).findByRole("alert")).toHaveTextContent("无法再次复制");

  await user.click(within(row).getByRole("button", { name: "复制 token" }));
  const manual = await within(row).findByRole("textbox", { name: "Claude Code 的明文 token" });
  expect(manual).toHaveValue("sk-secret-2");
  expect(within(row).getByText(/自动复制失败/)).toBeInTheDocument();
});

test("旧版本签发的 token 显示说明而不是复制按钮;已撤销的行不显示", async () => {
  routeRequests(baseRoutes({
    "GET /agent-tokens": () => [{ ...activeToken, copyable: false }, revokedToken],
  }));
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);
  await screen.findByText("Claude Code");
  expect(within(tokenRow("Claude Code")).queryByRole("button", { name: "复制 token" })).not.toBeInTheDocument();
  expect(within(tokenRow("Claude Code")).getByText("旧版本签发，无法再复制")).toBeInTheDocument();

  await user.click(screen.getByRole("checkbox", { name: "隐藏已撤销和已过期" }));
  const revoked = tokenRow("Codex");
  expect(revoked).toHaveTextContent("已撤销");
  expect(within(revoked).queryByText("旧版本签发，无法再复制")).not.toBeInTheDocument();
});

test("列表行显示状态、白名单笔记本名(超过 3 个折叠可展开);隐藏开关写 localStorage 且不可用时照常工作", async () => {
  const four = ["nb-1", "nb-2", "nb-3", "nb-4"];
  const fourBooks = four.map((id, index) => ({ ...notebooks[0], id, name: `库${index + 1}` }));
  const expired = { ...activeToken, id: "token-3", profile_name: "旧代理", expires_at: "2020-01-01T00:00:00Z" };
  routeRequests(baseRoutes({
    "GET /notebooks": () => fourBooks,
    "GET /agent-tokens": () => [{ ...activeToken, default_notebook_id: "nb-1", notebook_ids: four }, expired],
  }));
  const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("blocked"); });
  const user = userEvent.setup();
  render(<AgentAccessManager sessionSignal={new AbortController().signal} />);
  await screen.findByText("Claude Code");

  const row = tokenRow("Claude Code");
  expect(row).toHaveTextContent("有效");
  expect(row).toHaveTextContent("库1、库2、库3 等 4 个");
  await user.click(within(row).getByRole("button", { name: "展开" }));
  expect(row).toHaveTextContent("库1、库2、库3、库4");
  // 已过期的默认隐藏;关掉开关后出现并带状态标签。存储抛错也不影响开关。
  expect(screen.queryByText("旧代理")).not.toBeInTheDocument();
  await user.click(screen.getByRole("checkbox", { name: "隐藏已撤销和已过期" }));
  expect(tokenRow("旧代理")).toHaveTextContent("已过期");
  setItem.mockRestore();
});
