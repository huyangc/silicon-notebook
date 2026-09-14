import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({ requestJson: vi.fn() }));

vi.mock("../../app/api-client.ts", () => ({ requestJson: mocks.requestJson }));

import { AgentAccessManager } from "../../app/agent-access-manager";
import { localDateTimeToUtcIso } from "../../app/agent-token-model";
import { humanizedError } from "../../app/errors";

const notebooks = [
  { id: "nb-1", name: "工艺笔记本", purpose: "", primary_domain: "", status: "ready", counts: {}, created_label: "" },
  { id: "nb-2", name: "器件笔记本", purpose: "", primary_domain: "", status: "ready", counts: {}, created_label: "" },
];

const activeToken = {
  id: "token-1",
  agent_profile_id: "agent-1",
  profile_name: "Claude Code",
  scopes: ["knowledge:read"],
  default_notebook_id: "nb-1",
  notebook_ids: ["nb-1"],
  expires_at: "2030-01-01T00:00:00Z",
  revoked_at: null,
  last_used_at: null,
  created_at: "2026-09-01T00:00:00Z",
};

const revokedToken = { ...activeToken, id: "token-2", profile_name: "Codex", revoked_at: "2026-09-02T00:00:00Z" };

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
  // 已撤销的 token 不给修改入口。
  expect(within(tokenRow("Codex")).queryByRole("button", { name: /修改权限/ })).not.toBeInTheDocument();

  await user.click(within(tokenRow("Claude Code")).getByRole("button", { name: /修改权限/ }));
  const editor = screen.getByRole("form", { name: "修改 Claude Code 的 token 权限" });
  const save = within(editor).getByRole("button", { name: "保存" });
  expect(within(editor).getByRole("checkbox", { name: "读取知识库" })).toBeChecked();
  // 没改任何东西时不能保存。
  expect(save).toBeDisabled();

  await user.click(within(editor).getByRole("checkbox", { name: "读取已确认记忆" }));
  await user.click(within(editor).getByRole("checkbox", { name: "器件笔记本" }));
  expect(save).toBeEnabled();
  await user.click(save);

  // 在途:按钮换文案并禁用,取消与表单字段也都点不动。
  expect(within(editor).getByRole("button", { name: "保存中…" })).toBeDisabled();
  expect(within(editor).getByRole("button", { name: "取消" })).toBeDisabled();
  expect(within(editor).getByRole("checkbox", { name: "执行笔记本问答" })).toBeDisabled();
  expect(within(editor).getByLabelText("过期时间")).toBeDisabled();
  const saves = requestsTo("PUT /agent-tokens/token-1/access");
  expect(saves).toHaveLength(1);
  const body = JSON.parse(String(saves[0].body)) as Record<string, unknown>;
  expect(body).toEqual({
    scopes: ["knowledge:read", "memory:read"],
    default_notebook_id: "nb-1",
    notebook_ids: ["nb-1", "nb-2"],
    expires_at: localDateTimeToUtcIso((within(editor).getByLabelText("过期时间") as HTMLInputElement).value),
    // 编辑器打开时读到的配置,供服务端拒绝旧标签页的覆盖写。
    expected: {
      scopes: ["knowledge:read"],
      default_notebook_id: "nb-1",
      notebook_ids: ["nb-1"],
      expires_at: "2030-01-01T00:00:00Z",
    },
  });
  expect(body.expires_at).toBe("2030-01-01T00:00:00.000Z");

  const listLoadsBeforeSave = requestsTo("GET /agent-tokens").length;
  vi.useFakeTimers();
  await act(async () => {
    resolveSave({ ...activeToken, scopes: ["knowledge:read", "memory:read"], notebook_ids: ["nb-1", "nb-2"] });
  });

  const row = tokenRow("Claude Code");
  expect(screen.queryByRole("form", { name: "修改 Claude Code 的 token 权限" })).not.toBeInTheDocument();
  expect(within(row).getByRole("status")).toHaveTextContent("已保存，立即生效");
  expect(row).toHaveTextContent("读取知识库 · 读取已确认记忆");
  expect(row).toHaveTextContent("允许 2 个笔记本");
  // 成功回执本身就是新配置,不重拉列表(重拉会丢掉「加载更多」翻到的行)。
  expect(requestsTo("GET /agent-tokens")).toHaveLength(listLoadsBeforeSave);

  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(within(tokenRow("Claude Code")).queryByRole("status")).not.toBeInTheDocument();
  expect(tokenRow("Claude Code")).toHaveTextContent("读取知识库 · 读取已确认记忆");
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
  await user.click(within(editor).getByRole("checkbox", { name: "执行笔记本问答" }));
  await user.click(within(editor).getByRole("button", { name: "保存" }));

  expect(await within(editor).findByRole("alert")).toHaveTextContent("白名单里有你已无权访问的笔记本");
  expect(within(editor).getByRole("checkbox", { name: "执行笔记本问答" })).toBeChecked();
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
      if (saveAttempt === 1) return { ...secondPageToken, scopes: ["knowledge:read", "memory:read"] };
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
  await user.click(within(editor).getByRole("checkbox", { name: "读取已确认记忆" }));
  const listLoads = requestsTo("GET /agent-tokens").length;
  await user.click(within(editor).getByRole("button", { name: "保存" }));

  expect(await within(tokenRow("第二页代理")).findByRole("status")).toHaveTextContent("已保存，立即生效");
  expect(requestsTo("GET /agent-tokens")).toHaveLength(listLoads);

  await user.click(within(tokenRow("第二页代理")).getByRole("button", { name: /修改权限/ }));
  editor = screen.getByRole("form", { name: "修改 第二页代理 的 token 权限" });
  await user.click(within(editor).getByRole("checkbox", { name: "执行笔记本问答" }));
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
  await user.click(within(editor).getByRole("checkbox", { name: "读取已确认记忆" }));
  await user.click(within(editor).getByRole("button", { name: "保存" }));
  // 保存在途时翻页;这页响应里带着这一行保存前的旧副本。
  await user.click(screen.getByRole("button", { name: "加载更多 Token" }));

  await act(async () => { resolveSave({ ...edited, scopes: ["knowledge:read", "memory:read"] }); });
  expect(tokenRow("第一页代理0")).toHaveTextContent("读取知识库 · 读取已确认记忆");
  await act(async () => { resolvePage([edited, { ...activeToken, id: "token-late", profile_name: "晚到代理" }]); });

  expect(await screen.findByText("晚到代理")).toBeInTheDocument();
  expect(tokenRow("第一页代理0")).toHaveTextContent("读取知识库 · 读取已确认记忆");
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
  await user.click(within(editor).getByRole("checkbox", { name: "执行笔记本问答" }));
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
