import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/group-api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/group-api.ts")>();
  return { ...actual, resolveUser: vi.fn() };
});
vi.mock("../../app/notebook-bases.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/notebook-bases.ts")>();
  return { ...actual, listBases: vi.fn() };
});
vi.mock("../../app/notebook-collaborators-api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/notebook-collaborators-api.ts")>();
  return {
    ...actual,
    listCollaborators: vi.fn(),
    addEditor: vi.fn(),
    removeCollaborator: vi.fn(),
  };
});

import { humanizedError } from "../../app/errors.ts";
import { resolveUser } from "../../app/group-api.ts";
import { listBases, type MountedBase } from "../../app/notebook-bases.ts";
import {
  addEditor,
  listCollaborators,
  removeCollaborator,
  type CollaboratorItem,
} from "../../app/notebook-collaborators-api.ts";
import {
  COLLABORATOR_RESULT_HOLD_MS,
  EDITOR_CLOSES_BASES_WARNING,
  LEGACY_ADMIN_DOWNGRADE_NOTE,
  NotebookCollaboratorsShare,
} from "../../app/notebook-collaborators-share.tsx";

const row = (over: Partial<CollaboratorItem>): CollaboratorItem => ({
  id: "gr-1", user_id: "u-li", username: "lisi", display_name: "李四", role: "editor",
  granted_by_username: "owner", created_at: "", ...over,
});

const base = (over: Partial<MountedBase>): MountedBase => ({
  id: "b1", name: "借来的库", tier: "personal", active: true, inactive_reason: "", ...over,
});

const ZHANG = { id: "u-zhang", username: "zhangsan", display_name: "张三" };

let collaborators: CollaboratorItem[] = [];

beforeEach(() => {
  collaborators = [];
  vi.mocked(listCollaborators).mockImplementation(async () => ({ view: "owner", items: collaborators, editors: [] }));
  vi.mocked(listBases).mockResolvedValue([]);
  vi.mocked(resolveUser).mockResolvedValue(ZHANG);
  vi.mocked(addEditor).mockImplementation(async (_nb, userId) => {
    const added = row({ id: "gr-new", user_id: userId, username: ZHANG.username, display_name: ZHANG.display_name });
    collaborators = [...collaborators.filter((item) => item.user_id !== userId), added];
    return { grant: added, changed: true, previous_role: null };
  });
  vi.mocked(removeCollaborator).mockResolvedValue(undefined);
});

afterEach(() => {
  vi.useRealTimers();
});

function renderSection(canManageCollaborators = true) {
  const onChanged = vi.fn();
  const view = render(
    <NotebookCollaboratorsShare canManageCollaborators={canManageCollaborators} notebookId="nb1" onChanged={onChanged} />,
  );
  return { onChanged, ...view };
}

/** 名单里某个人的那一行(`.group-row`)。 */
function personRow(name: RegExp | string) {
  const node = screen.getAllByText(name).map((el) => el.closest(".group-row")).find(Boolean);
  if (!node) throw new Error(`没有找到「${String(name)}」那一行`);
  return node as HTMLElement;
}

async function lookUp(user: ReturnType<typeof userEvent.setup>, username = "zhangsan") {
  await user.type(screen.getByLabelText("要添加的用户名"), username);
  await user.click(screen.getByRole("button", { name: "查找" }));
  return screen.findByRole("button", { name: "添加为编辑者" });
}

test("非库主(canManageCollaborators=false)整节不渲染,也不发任何请求", () => {
  const { container } = renderSection(false);
  expect(container).toBeEmptyDOMElement();
  expect(screen.queryByText("点名共享")).toBeNull();
  expect(listCollaborators).not.toHaveBeenCalled();
  expect(listBases).not.toHaveBeenCalled();
});

test("名单列出全部点名授权并标角色;只读点名行同样可移除,「已移除」落在原按钮位置", async () => {
  const user = userEvent.setup();
  collaborators = [
    row({ user_id: "u-li", username: "lisi", display_name: "李四", role: "editor" }),
    row({ id: "gr-2", user_id: "u-wang", username: "wangwu", display_name: "王五", role: "viewer" }),
  ];
  const { onChanged } = renderSection();

  await screen.findByText("李四（lisi）");
  expect(within(personRow("李四（lisi）")).getByText("可编辑")).toBeInTheDocument();
  const viewer = personRow("王五（wangwu）");
  expect(within(viewer).getByText("只读")).toBeInTheDocument();

  await user.click(within(viewer).getByRole("button", { name: "移除" }));
  await waitFor(() => expect(removeCollaborator).toHaveBeenCalledWith("nb1", "u-wang"));
  expect(await within(personRow("王五（wangwu）")).findByRole("status")).toHaveTextContent("已移除");
  expect(within(personRow("王五（wangwu）")).queryByRole("button")).toBeNull();
  expect(onChanged).toHaveBeenCalledTimes(1);
  // 另一行不受影响。
  expect(within(personRow("李四（lisi）")).getByRole("button", { name: "移除" })).toBeEnabled();
});

test("有会失效的参考库时:只列 closes_when_shared=true 的那些,并要求二次确认后才添加", async () => {
  const user = userEvent.setup();
  vi.mocked(listBases).mockResolvedValue([
    base({ id: "b-borrowed", name: "借来的封装库", closes_when_shared: true }),
    base({ id: "b-mine", name: "我自己的库", closes_when_shared: false }),
    base({ id: "b-public", name: "公共工艺库", tier: "base" }),
    base({ id: "b-dead", name: "已失效的库", active: false, inactive_reason: "x", closes_when_shared: false }),
  ]);
  const { onChanged } = renderSection();
  await screen.findByText("还没有点名共享给任何人。");

  await user.click(await lookUp(user));
  const confirm = await screen.findByRole("group", { name: "确认添加编辑者" });
  expect(within(confirm).getByText("借来的封装库")).toBeInTheDocument();
  expect(within(confirm).queryByText("我自己的库")).toBeNull();
  expect(within(confirm).queryByText("公共工艺库")).toBeNull();
  expect(within(confirm).queryByText("已失效的库")).toBeNull();
  expect(within(confirm).getByText(EDITOR_CLOSES_BASES_WARNING)).toBeInTheDocument();
  // 第一次点只把后果摆出来,还没添加。
  expect(addEditor).not.toHaveBeenCalled();

  // 「先不添加」什么都不做。
  await user.click(within(confirm).getByRole("button", { name: "先不添加" }));
  expect(screen.queryByRole("group", { name: "确认添加编辑者" })).toBeNull();
  expect(addEditor).not.toHaveBeenCalled();

  await user.click(screen.getByRole("button", { name: "添加为编辑者" }));
  await user.click(within(await screen.findByRole("group", { name: "确认添加编辑者" })).getByRole("button", { name: "确认添加" }));
  await waitFor(() => expect(addEditor).toHaveBeenCalledWith("nb1", "u-zhang"));
  expect(addEditor).toHaveBeenCalledTimes(1);
  expect(await screen.findByText("已添加")).toBeInTheDocument();
  expect(onChanged).toHaveBeenCalledTimes(1);
  // 新编辑者进了名单。
  expect(within(personRow("张三（zhangsan）")).getAllByText("可编辑").length).toBeGreaterThan(0);
});

test("没有会失效的参考库时一次点击直接添加,不出确认区", async () => {
  const user = userEvent.setup();
  renderSection();
  await screen.findByText("还没有点名共享给任何人。");

  await user.click(await lookUp(user));
  await waitFor(() => expect(addEditor).toHaveBeenCalledWith("nb1", "u-zhang"));
  expect(screen.queryByRole("group", { name: "确认添加编辑者" })).toBeNull();
  expect(listBases).toHaveBeenCalledWith("nb1");
});

test("结果落在按钮旁并按自己的计时复原(fake timers)", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  collaborators = [row({ id: "gr-2", user_id: "u-wang", username: "wangwu", display_name: "王五", role: "viewer" })];
  renderSection();
  await screen.findByText("王五（wangwu）");

  // 添加:「已添加」挨着「添加为编辑者」那一行,到点消失。
  await user.click(await lookUp(user));
  const added = await screen.findByText("已添加");
  // 加完之后这个人已是编辑者,按钮就地变成「已是编辑者」;结果与它同在一格。
  const settled = await screen.findByRole("button", { name: "已是编辑者" });
  expect(settled).toBeDisabled();
  expect(added.closest(".group-row-actions")).toBe(settled.closest(".group-row-actions"));
  await act(async () => { await vi.advanceTimersByTimeAsync(COLLABORATOR_RESULT_HOLD_MS + 50); });
  expect(screen.queryByText("已添加")).toBeNull();

  // 移除:行在计时内保留并写「已移除」,到点从名单里摘掉。
  await user.click(within(personRow("王五（wangwu）")).getByRole("button", { name: "移除" }));
  expect(await within(personRow("王五（wangwu）")).findByText("已移除")).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(COLLABORATOR_RESULT_HOLD_MS + 50); });
  expect(screen.queryByText("王五（wangwu）")).toBeNull();
  expect(screen.queryByText("已移除")).toBeNull();
});

test("移除后立即重新添加：旧定时器不能隐藏恢复的编辑者，仍可再次移除", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  collaborators = [row({ user_id: ZHANG.id, username: ZHANG.username, display_name: ZHANG.display_name })];
  vi.mocked(removeCollaborator).mockImplementation(async (_nb, userId) => {
    collaborators = collaborators.filter((item) => item.user_id !== userId);
  });
  renderSection();
  await screen.findByText("张三（zhangsan）");
  await user.click(screen.getByRole("button", { name: "移除" }));
  expect(await screen.findByText("已移除")).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(COLLABORATOR_RESULT_HOLD_MS / 4); });

  await user.click(await lookUp(user));
  expect(await screen.findByText("已添加")).toBeInTheDocument();
  expect(collaborators).toHaveLength(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(COLLABORATOR_RESULT_HOLD_MS * 3 / 4 + 50); });

  // 第一次移除的期限已到，新添加的结果仍在它自己的期限内。
  expect(screen.getByText("已添加")).toBeInTheDocument();
  const revoke = screen.getByRole("button", { name: "移除" });
  expect(revoke).toBeEnabled();
  expect(screen.queryByText("还没有点名共享给任何人。")).toBeNull();
  await user.click(revoke);
  expect(removeCollaborator).toHaveBeenCalledTimes(2);
  expect(await screen.findByText("已移除")).toBeInTheDocument();
});

test("同一人重新添加后再次移除：第一次定时器不能提前清掉第二次移除结果", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  collaborators = [row({ user_id: ZHANG.id, username: ZHANG.username, display_name: ZHANG.display_name })];
  vi.mocked(removeCollaborator).mockImplementation(async (_nb, userId) => {
    collaborators = collaborators.filter((item) => item.user_id !== userId);
  });
  renderSection();
  await screen.findByText("张三（zhangsan）");
  await user.click(screen.getByRole("button", { name: "移除" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(COLLABORATOR_RESULT_HOLD_MS / 4); });
  await user.click(await lookUp(user));
  await screen.findByText("已添加");
  await user.click(screen.getByRole("button", { name: "移除" }));
  await act(async () => { await vi.advanceTimersByTimeAsync(COLLABORATOR_RESULT_HOLD_MS * 3 / 4 + 50); });
  expect(screen.getByText("已移除")).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(COLLABORATOR_RESULT_HOLD_MS / 2); });
  expect(screen.queryByText("已移除")).toBeNull();
  expect(screen.queryByRole("button", { name: "移除" })).toBeNull();
});

test("旧移除计时在添加请求中到期：重新添加成功后即使重取失败也保留可撤销的新授权", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  collaborators = [row({ user_id: ZHANG.id, username: ZHANG.username, display_name: ZHANG.display_name })];
  vi.mocked(removeCollaborator).mockImplementation(async (_nb, userId) => {
    collaborators = collaborators.filter((item) => item.user_id !== userId);
  });
  let finishAdd!: () => void;
  vi.mocked(addEditor).mockImplementationOnce((_nb, userId) => new Promise((resolve) => {
    finishAdd = () => {
      const grant = row({ id: "gr-restored", user_id: userId, username: ZHANG.username, display_name: ZHANG.display_name });
      collaborators = [grant];
      resolve({ grant, changed: true, previous_role: null });
    };
  }));
  renderSection();
  await screen.findByText("张三（zhangsan）");
  await user.click(screen.getByRole("button", { name: "移除" }));
  await user.click(await lookUp(user));
  expect(screen.getByRole("button", { name: "添加中…" })).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(COLLABORATOR_RESULT_HOLD_MS + 50); });
  vi.mocked(listCollaborators).mockRejectedValueOnce(new Error("reload unavailable"));
  await act(async () => { finishAdd(); });
  expect(screen.getByText("已添加")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "移除" })).toBeEnabled();
  expect(screen.queryByText("还没有点名共享给任何人。")).toBeNull();
});

test("切换笔记本后旧移除回包和定时器不能影响同名的新授权", async () => {
  const user = userEvent.setup();
  collaborators = [row({ user_id: ZHANG.id, username: ZHANG.username, display_name: ZHANG.display_name })];
  let finishRemove!: () => void;
  vi.mocked(removeCollaborator).mockImplementationOnce(() => new Promise<void>((resolve) => {
    finishRemove = resolve;
  }));
  const { rerender, onChanged } = renderSection();
  await screen.findByText("张三（zhangsan）");
  await user.click(screen.getByRole("button", { name: "移除" }));
  expect(screen.getByRole("button", { name: "移除中…" })).toBeDisabled();

  collaborators = [row({ id: "gr-new-notebook", user_id: ZHANG.id, username: ZHANG.username, display_name: ZHANG.display_name })];
  const nextChanged = vi.fn();
  rerender(<NotebookCollaboratorsShare canManageCollaborators notebookId="nb2" onChanged={nextChanged} />);
  await waitFor(() => expect(listCollaborators).toHaveBeenLastCalledWith("nb2"));
  await act(async () => { finishRemove(); });
  expect(screen.getByRole("button", { name: "移除" })).toBeEnabled();
  expect(screen.queryByText("已移除")).toBeNull();
  expect(onChanged).not.toHaveBeenCalled();
  expect(nextChanged).not.toHaveBeenCalled();
});

test("卸载点名共享时清理仍在等待的移除定时器", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  collaborators = [row({})];
  const { unmount } = renderSection();
  await screen.findByText("李四（lisi）");
  await user.click(screen.getByRole("button", { name: "移除" }));
  expect(await screen.findByText("已移除")).toBeInTheDocument();
  expect(vi.getTimerCount()).toBeGreaterThan(0);
  unmount();
  expect(vi.getTimerCount()).toBe(0);
});

test("A→B→A 后旧添加回包不能改写新访问的授权或触发旧刷新", async () => {
  const user = userEvent.setup();
  let finishAdd!: () => void;
  vi.mocked(addEditor).mockImplementationOnce((_nb, userId) => new Promise((resolve) => {
    finishAdd = () => resolve({
      grant: row({ id: "gr-old-add", user_id: userId, username: ZHANG.username, display_name: ZHANG.display_name }),
      changed: true, previous_role: null,
    });
  }));
  const { rerender, onChanged } = renderSection();
  await screen.findByText("还没有点名共享给任何人。");
  await user.click(await lookUp(user));
  expect(screen.getByRole("button", { name: "添加中…" })).toBeDisabled();

  collaborators = [row({ id: "gr-current", user_id: ZHANG.id, username: ZHANG.username, display_name: ZHANG.display_name, role: "viewer" })];
  const nextChanged = vi.fn();
  rerender(<NotebookCollaboratorsShare canManageCollaborators notebookId="nb2" onChanged={nextChanged} />);
  await screen.findByText("只读");
  rerender(<NotebookCollaboratorsShare canManageCollaborators notebookId="nb1" onChanged={nextChanged} />);
  await screen.findByText("只读");
  await act(async () => { finishAdd(); });
  expect(screen.getByText("只读")).toBeInTheDocument();
  expect(screen.queryByText("可编辑")).toBeNull();
  expect(screen.getByRole("button", { name: "移除" })).toBeEnabled();
  expect(screen.queryByText("已添加")).toBeNull();
  expect(onChanged).not.toHaveBeenCalled();
  expect(nextChanged).not.toHaveBeenCalled();
});

test.each(["空名单", "失败"] as const)("添加后的新名单已经返回时，初次加载的旧%s不能改写新结果", async (result) => {
  const user = userEvent.setup();
  let finishInitial!: () => void;
  vi.mocked(listCollaborators).mockImplementationOnce(() => new Promise((resolve, reject) => {
    finishInitial = () => {
      if (result === "失败") reject(new Error("older list failed"));
      else resolve({ view: "owner", items: [], editors: [] });
    };
  }));
  renderSection();
  await screen.findByText("加载中…");
  await user.click(await lookUp(user));
  await waitFor(() => expect(screen.getByRole("button", { name: "移除" })).toBeEnabled());
  await act(async () => { finishInitial(); });
  expect(screen.getByRole("button", { name: "移除" })).toBeEnabled();
  expect(screen.queryByText("还没有点名共享给任何人。")).toBeNull();
  expect(screen.queryByText("点名共享名单加载失败")).toBeNull();
});

test("查无此人:后端写好的文案落在「查找」按钮旁,到点消失", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  vi.mocked(resolveUser).mockRejectedValueOnce(humanizedError("没有找到该用户名对应的用户", 404));
  renderSection();
  await screen.findByText("还没有点名共享给任何人。");

  await user.type(screen.getByLabelText("要添加的用户名"), "nobody");
  const find = screen.getByRole("button", { name: "查找" });
  await user.click(find);
  const failed = await screen.findByText("没有找到该用户名对应的用户");
  // 紧邻:与「查找」按钮同在一行表单里。
  expect(failed.closest(".group-form-row")).toBe(find.closest(".group-form-row"));
  expect(screen.queryByRole("button", { name: "添加为编辑者" })).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(COLLABORATOR_RESULT_HOLD_MS + 50); });
  expect(screen.queryByText("没有找到该用户名对应的用户")).toBeNull();
});

test("请求进行中整节按钮禁用,按钮换成进行态文案", async () => {
  const user = userEvent.setup();
  collaborators = [row({ user_id: "u-li", username: "lisi", display_name: "李四", role: "editor" })];
  let finish: () => void = () => {};
  vi.mocked(addEditor).mockImplementationOnce(() => new Promise((resolve) => {
    finish = () => resolve({ grant: row({ user_id: ZHANG.id }), changed: true, previous_role: null });
  }));
  renderSection();
  await screen.findByText("李四（lisi）");

  await user.click(await lookUp(user));
  const pending = await screen.findByRole("button", { name: "添加中…" });
  expect(pending).toBeDisabled();
  expect(within(personRow("李四（lisi）")).getByRole("button", { name: "移除" })).toBeDisabled();
  expect(screen.getByLabelText("要添加的用户名")).toBeDisabled();
  expect(screen.getByRole("button", { name: "查找" })).toBeDisabled();
  // 重复点不会再发一次。
  await user.click(pending);
  expect(addEditor).toHaveBeenCalledTimes(1);

  await act(async () => { finish(); });
  await waitFor(() => expect(within(personRow("李四（lisi）")).getByRole("button", { name: "移除" })).toBeEnabled());
});

test("遗留的管理权限行:标出来并说明;添加按钮旁写明会改为可编辑,且要确认", async () => {
  const user = userEvent.setup();
  collaborators = [row({ id: "gr-3", user_id: ZHANG.id, username: ZHANG.username, display_name: ZHANG.display_name, role: "admin" })];
  vi.mocked(addEditor).mockImplementation(async (_nb, userId) => {
    collaborators = [row({ id: "gr-3", user_id: userId, username: ZHANG.username, display_name: ZHANG.display_name, role: "editor" })];
    return { grant: collaborators[0], changed: true, previous_role: "admin" };
  });
  renderSection();

  await screen.findByText("张三（zhangsan）");
  const legacy = personRow("张三（zhangsan）");
  expect(within(legacy).getByText("可管理")).toBeInTheDocument();
  expect(legacy).toHaveTextContent("早先留下的管理权限");
  // 说明里提到的动作是句中可点的控件,不只是一句话。
  const inline = within(legacy).getByRole("button", { name: "改为可编辑" });
  expect(inline).toHaveClass("inline-action");
  // 同一行照样能移除。
  expect(within(legacy).getByRole("button", { name: "移除" })).toBeEnabled();

  // 从查找入口走:按钮旁写明降级,第一次点只进确认区。
  await lookUp(user);
  expect(screen.getAllByText(LEGACY_ADMIN_DOWNGRADE_NOTE).length).toBeGreaterThan(0);
  await user.click(screen.getByRole("button", { name: "添加为编辑者" }));
  const confirm = await screen.findByRole("group", { name: "确认添加编辑者" });
  expect(confirm).toHaveTextContent(LEGACY_ADMIN_DOWNGRADE_NOTE);
  expect(addEditor).not.toHaveBeenCalled();
  await user.click(within(confirm).getByRole("button", { name: "确认添加" }));
  await waitFor(() => expect(addEditor).toHaveBeenCalledWith("nb1", ZHANG.id));
  expect(await screen.findByText("已改为可编辑")).toBeInTheDocument();
});

test("名单行里的「改为可编辑」把结果落在那一行", async () => {
  const user = userEvent.setup();
  collaborators = [row({ id: "gr-3", user_id: "u-zhao", username: "zhaoliu", display_name: "赵六", role: "admin" })];
  vi.mocked(addEditor).mockImplementation(async (_nb, userId) => {
    collaborators = [row({ id: "gr-3", user_id: userId, username: "zhaoliu", display_name: "赵六", role: "editor" })];
    return { grant: collaborators[0], changed: true, previous_role: "admin" };
  });
  renderSection();

  await screen.findByText("赵六（zhaoliu）");
  await user.click(within(personRow("赵六（zhaoliu）")).getByRole("button", { name: "改为可编辑" }));
  await waitFor(() => expect(addEditor).toHaveBeenCalledWith("nb1", "u-zhao"));
  const rowNow = personRow("赵六（zhaoliu）");
  expect(await within(rowNow).findByText("已改为可编辑")).toBeInTheDocument();
  expect(within(rowNow).getByText("可编辑")).toBeInTheDocument();
});

test("结果与忙碌都只在本节内联:组件对外只有 onChanged 一个回调,不碰页面状态条", async () => {
  const user = userEvent.setup();
  vi.mocked(addEditor).mockRejectedValueOnce(humanizedError("公共知识库对所有人只读，不能添加编辑者", 409));
  const { onChanged, container } = renderSection();
  await screen.findByText("还没有点名共享给任何人。");

  await user.click(await lookUp(user));
  const failed = await screen.findByText("公共知识库对所有人只读，不能添加编辑者");
  // 落在本节内、挨着被按的按钮。
  expect(container.contains(failed)).toBe(true);
  expect(failed.closest(".group-row-actions")).toBe(
    screen.getByRole("button", { name: "添加为编辑者" }).closest(".group-row-actions"),
  );
  // 失败不触发外层刷新;外层能拿到的回调只有 onChanged(props 里没有 statusText/toast 通道)。
  expect(onChanged).not.toHaveBeenCalled();
});
