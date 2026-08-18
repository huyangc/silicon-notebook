import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/group-api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/group-api.ts")>();
  return {
    ...actual,
    listGroups: vi.fn(),
    listNotebookGrants: vi.fn(),
    listMyShareRequests: vi.fn(),
    shareNotebookToGroup: vi.fn(),
    submitShareRequest: vi.fn(),
    withdrawShareRequest: vi.fn(),
    revokeNotebookGrant: vi.fn(),
  };
});

import {
  listGroups,
  listMyShareRequests,
  listNotebookGrants,
  revokeNotebookGrant,
  shareNotebookToGroup,
  submitShareRequest,
  withdrawShareRequest,
  type GroupSummary,
  type NotebookGrant,
  type ShareRequest,
} from "../../app/group-api.ts";
import { NotebookGroupShare } from "../../app/notebook-group-share.tsx";

const ADMIN_GROUP: GroupSummary = {
  id: "g1", name: "封装项目", kind: "project", description: "",
  my_role: "admin", member_count: 3, created_at: "",
};
const MEMBER_GROUP: GroupSummary = { ...ADMIN_GROUP, id: "g2", name: "工艺部", kind: "department", my_role: "member" };

const grant = (over: Partial<NotebookGrant>): NotebookGrant => ({
  id: "gr1", principal_type: "group", principal_id: "g1", role: "viewer",
  principal_name: "封装项目", principal_kind: "project", created_at: "", ...over,
});

const shareRequest = (over: Partial<ShareRequest>): ShareRequest => ({
  id: "sr1", notebook_id: "nb1", notebook_name: "库", group_id: "g2", group_name: "工艺部",
  requested_by: "me", requested_by_username: "me", status: "pending",
  decided_by: null, decided_at: null, created_at: "", ...over,
});

beforeEach(() => {
  vi.mocked(listGroups).mockResolvedValue([ADMIN_GROUP, MEMBER_GROUP]);
  vi.mocked(listNotebookGrants).mockResolvedValue([]);
  vi.mocked(listMyShareRequests).mockResolvedValue([]);
});

function renderSection() {
  const onChanged = vi.fn();
  render(<NotebookGroupShare notebookId="nb1" onChanged={onChanged} />);
  return { onChanged };
}

test("只列我担任组管理员的组,并在共享前说清借来的参考库会暂停参与检索", async () => {
  renderSection();

  await screen.findByText("还没有共享给任何群组。");
  // 未共享门(设计 §6.1)的提示必须在动作之前给,不是事后在失效边上解释。
  expect(screen.getByText(/借来的参考库会暂停参与检索/)).toBeInTheDocument();

  const picker = screen.getByLabelText("选择群组");
  expect(picker).toHaveTextContent("封装项目");
  expect(picker).not.toHaveTextContent("工艺部");
});

test("共享给群组默认只发只读授权(manage 未勾),并让外层重取笔记本清单", async () => {
  const user = userEvent.setup();
  vi.mocked(shareNotebookToGroup).mockResolvedValue(grant({}));
  const { onChanged } = renderSection();

  await screen.findByLabelText("选择群组");
  await user.selectOptions(screen.getByLabelText("选择群组"), "g1");
  await user.click(screen.getByRole("button", { name: "共享给该群组" }));

  // 默认不勾「组管理员可管理」:manage:false,不追加 group_admins 边。
  await waitFor(() => expect(shareNotebookToGroup).toHaveBeenCalledWith("nb1", "g1", { manage: false }));
  expect(shareNotebookToGroup).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

test("勾选「组管理员可管理」后共享带 manage:true(追加 group_admins/admin 边)", async () => {
  const user = userEvent.setup();
  vi.mocked(shareNotebookToGroup).mockResolvedValue(grant({}));
  renderSection();

  await screen.findByLabelText("选择群组");
  await user.selectOptions(screen.getByLabelText("选择群组"), "g1");
  await user.click(screen.getByLabelText("组管理员可管理这本笔记本"));
  await user.click(screen.getByRole("button", { name: "共享给该群组" }));

  await waitFor(() => expect(shareNotebookToGroup).toHaveBeenCalledWith("nb1", "g1", { manage: true }));
});

test("已共享的组(含 group_admins 边)折成一项、标注可管理;撤销把两条边一起删掉", async () => {
  const user = userEvent.setup();
  // 只有一个我担任组管理员的组,且它已经共享过(两条边)——没有别的组可选/可申请。
  vi.mocked(listGroups).mockResolvedValue([ADMIN_GROUP]);
  vi.mocked(listNotebookGrants).mockResolvedValue([
    grant({ id: "gr1", principal_type: "group" }),
    grant({ id: "gr2", principal_type: "group_admins", role: "admin" }),
  ]);
  vi.mocked(revokeNotebookGrant).mockResolvedValue(undefined);
  renderSection();

  await screen.findByText("封装项目");
  // 已共享过,直接共享选择器不该出现;没有别的组,兜底文案出现。
  expect(screen.queryByLabelText("选择群组")).not.toBeInTheDocument();
  expect(screen.getByText(/没有可共享的群组/)).toBeInTheDocument();
  // 带 group_admins 边 → 折成一项并标注「组管理员可管理」。
  expect(screen.getByText("组管理员可管理")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "撤销共享" }));
  await waitFor(() => expect(revokeNotebookGrant).toHaveBeenCalledTimes(2));
  expect(revokeNotebookGrant).toHaveBeenCalledWith("nb1", "gr1");
  expect(revokeNotebookGrant).toHaveBeenCalledWith("nb1", "gr2");
});

test("撤销把给管理权的那条边**最后**删——先删它就把自己的删除权删掉了", async () => {
  const user = userEvent.setup();
  vi.mocked(listGroups).mockResolvedValue([ADMIN_GROUP]);
  // 标准共享模板先发 (group_admins, admin) 后发 (group, viewer),后端按 created_at 返回,
  // 所以 admin 边**排在前**——这是主路径。照单顺序删,第一次 DELETE 就撤掉了调用者自己的
  // 管理权(三个 grant 端点都是 admin 档能力,组管理员也能进这个面板),第二次拿 404,
  // 只读边留下、共享仍然生效,而界面已经报了「撤销」(codex #519 R7 P2)。
  vi.mocked(listNotebookGrants).mockResolvedValue([
    grant({ id: "gr-admin", principal_type: "group_admins", role: "admin" }),
    grant({ id: "gr-view", principal_type: "group", role: "viewer" }),
  ]);
  vi.mocked(revokeNotebookGrant).mockResolvedValue(undefined);
  renderSection();

  await screen.findByText("封装项目");
  await user.click(screen.getByRole("button", { name: "撤销共享" }));

  await waitFor(() => expect(revokeNotebookGrant).toHaveBeenCalledTimes(2));
  // 断言的是**调用顺序**,不只是「两条都删了」——后者对着 bug 也是绿的。
  expect(vi.mocked(revokeNotebookGrant).mock.calls.map(([, id]) => id)).toEqual([
    "gr-view",
    "gr-admin",
  ]);
});

test("孤儿边标成失效并给删除入口,不显示一条没有名字的共享", async () => {
  vi.mocked(listNotebookGrants).mockResolvedValue([
    grant({ id: "gr9", principal_id: "gone", principal_name: "", principal_kind: "missing" }),
  ]);
  renderSection();

  await screen.findByText("已失效的群组共享");
  expect(screen.getByText(/该群组已不存在/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "撤销共享" })).toBeInTheDocument();
});

test("只读共享(user)与公共知识库(everyone)的授权不混进「共享给群组」", async () => {
  vi.mocked(listNotebookGrants).mockResolvedValue([
    grant({ id: "gr-u", principal_type: "user", principal_id: "u1", principal_name: "", principal_kind: "" }),
    grant({ id: "gr-e", principal_type: "everyone", principal_id: "", principal_name: "", principal_kind: "" }),
  ]);
  renderSection();

  await screen.findByText("还没有共享给任何群组。");
  expect(screen.queryByRole("button", { name: "撤销共享" })).not.toBeInTheDocument();
});

test("撤销只让**那一条**显示进行态,不是整段一起变灰", async () => {
  const user = userEvent.setup();
  vi.mocked(listNotebookGrants).mockResolvedValue([
    grant({ id: "gr1", principal_id: "g1", principal_name: "封装项目" }),
    grant({ id: "gr2", principal_id: "g2", principal_name: "工艺部", principal_kind: "department" }),
  ]);
  let release: () => void = () => undefined;
  vi.mocked(revokeNotebookGrant).mockReturnValue(new Promise<void>((resolve) => { release = resolve; }));
  renderSection();

  await screen.findByText("封装项目");
  const buttons = screen.getAllByRole("button", { name: "撤销共享" });
  await user.click(buttons[1]);

  // 忙碌位记的是「哪一条在忙」,所以只有被点的那条换文案(两条都禁用是对的:
  // 清单会整份重取,期间不该再发第二次撤销)。
  await screen.findByRole("button", { name: "撤销中…" });
  expect(screen.getAllByRole("button", { name: "撤销中…" })).toHaveLength(1);
  expect(screen.getByRole("button", { name: "撤销共享" })).toBeDisabled();
  release();
});

test("撤销中途失败也要重取清单——不留一个「删了一半」的旧视图", async () => {
  const user = userEvent.setup();
  vi.mocked(listNotebookGrants).mockResolvedValue([
    grant({ id: "gr1", principal_type: "group" }),
    grant({ id: "gr2", principal_type: "group_admins", role: "admin" }),
  ]);
  vi.mocked(revokeNotebookGrant)
    .mockResolvedValueOnce(undefined)                  // 第一条删掉了
    .mockRejectedValueOnce(new Error("boom"));         // 第二条炸了
  renderSection();

  await screen.findByText("封装项目");
  vi.mocked(listNotebookGrants).mockClear();
  await user.click(screen.getByRole("button", { name: "撤销共享" }));

  await screen.findByText("撤销共享失败");
  // 失败分支也重取:否则界面还按发起前那份清单渲染,用户看不出删掉了哪几条。
  await waitFor(() => expect(listNotebookGrants).toHaveBeenCalled());
});

test("我只是普通成员的组走「提交共享申请」而不是直接发边", async () => {
  const user = userEvent.setup();
  vi.mocked(submitShareRequest).mockResolvedValue(shareRequest({}));
  const { onChanged } = renderSection();

  await screen.findByText(/你不是这些群组的组管理员/);
  const picker = screen.getByLabelText("选择要申请的群组");
  // 我只是成员的 g2 出现在申请选择器里;我是组管理员的 g1 不在这里(它走直接共享)。
  expect(picker).toHaveTextContent("工艺部");
  expect(picker).not.toHaveTextContent("封装项目");

  await user.selectOptions(picker, "g2");
  await user.click(screen.getByRole("button", { name: "提交共享申请" }));
  await waitFor(() => expect(submitShareRequest).toHaveBeenCalledWith("nb1", "g2"));
  // 直接共享的入口没被误触发。
  expect(shareNotebookToGroup).not.toHaveBeenCalled();
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

test("我发起的申请回显状态:待审批可撤回,已驳回只读且不再出现在申请选择器", async () => {
  const user = userEvent.setup();
  vi.mocked(listGroups).mockResolvedValue([MEMBER_GROUP]); // 只有一个我是成员的组 g2
  vi.mocked(listMyShareRequests).mockResolvedValue([
    shareRequest({ id: "sr-pending", group_id: "g2", group_name: "工艺部", status: "pending" }),
  ]);
  vi.mocked(withdrawShareRequest).mockResolvedValue(undefined);
  renderSection();

  await screen.findByText("我的共享申请");
  expect(screen.getByText("待审批")).toBeInTheDocument();
  // 已有 pending 申请的组不再出现在申请选择器里(避免让人以为能提交第二份)。
  expect(screen.queryByLabelText("选择要申请的群组")).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "撤回申请" }));
  await waitFor(() => expect(withdrawShareRequest).toHaveBeenCalledWith("nb1", "sr-pending"));
});

test("已驳回的申请只读回显,没有撤回按钮", async () => {
  vi.mocked(listGroups).mockResolvedValue([MEMBER_GROUP]);
  vi.mocked(listMyShareRequests).mockResolvedValue([
    shareRequest({ id: "sr-rej", group_id: "g2", group_name: "工艺部", status: "rejected" }),
  ]);
  renderSection();

  await screen.findByText("我的共享申请");
  expect(screen.getByText("已驳回")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "撤回申请" })).not.toBeInTheDocument();
});
