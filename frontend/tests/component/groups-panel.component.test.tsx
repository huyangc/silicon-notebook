import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/group-api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/group-api.ts")>();
  return {
    ...actual,
    listGroups: vi.fn(),
    getGroup: vi.fn(),
    createGroup: vi.fn(),
    deleteGroup: vi.fn(),
    putGroupMember: vi.fn(),
    removeGroupMember: vi.fn(),
    updateGroup: vi.fn(),
    leaveGroup: vi.fn(),
    resolveUser: vi.fn(),
    listGroupSharedNotebooks: vi.fn(),
    revokeGroupSharedNotebook: vi.fn(),
    listGroupShareRequests: vi.fn(),
    approveShareRequest: vi.fn(),
    rejectShareRequest: vi.fn(),
    listMyPendingShareRequests: vi.fn(),
    withdrawShareRequest: vi.fn(),
  };
});

import {
  approveShareRequest,
  createGroup,
  deleteGroup,
  getGroup,
  leaveGroup,
  listGroupShareRequests,
  listGroupSharedNotebooks,
  listGroups,
  listMyPendingShareRequests,
  putGroupMember,
  rejectShareRequest,
  removeGroupMember,
  resolveUser,
  revokeGroupSharedNotebook,
  updateGroup,
  withdrawShareRequest,
  type GroupDetail,
  type ShareRequest,
  type GroupSummary,
} from "../../app/group-api.ts";
import { GroupsModal } from "../../app/groups-panel.tsx";

const shareRequest = (over: Partial<ShareRequest>): ShareRequest => ({
  id: "sr1", notebook_id: "nb9", notebook_name: "候选库", group_id: "g1", group_name: "封装项目",
  requested_by: "u7", requested_by_username: "erin", status: "pending",
  decided_by: null, decided_at: null, created_at: "", ...over,
});

const ADMIN_GROUP: GroupSummary = {
  id: "g1",
  name: "封装项目",
  kind: "project",
  description: "封装工艺相关的项目组",
  my_role: "admin",
  member_count: 2,
  created_at: "",
};

const MEMBER_GROUP: GroupSummary = {
  ...ADMIN_GROUP,
  id: "g2",
  name: "工艺部",
  kind: "department",
  my_role: "member",
  member_count: 5,
};

const ADMIN_DETAIL: GroupDetail = {
  ...ADMIN_GROUP,
  members: [
    { id: "u1", username: "alice", display_name: "爱丽丝", role: "admin", added_at: "" },
    { id: "u2", username: "bob", display_name: "", role: "member", added_at: "" },
  ],
};

beforeEach(() => {
  vi.mocked(listGroups).mockResolvedValue([ADMIN_GROUP, MEMBER_GROUP]);
  vi.mocked(getGroup).mockResolvedValue(ADMIN_DETAIL);
  vi.mocked(listGroupSharedNotebooks).mockResolvedValue([]);
  vi.mocked(listGroupShareRequests).mockResolvedValue([]);
  vi.mocked(listMyPendingShareRequests).mockResolvedValue([]);
});

function renderModal(isSystemAdmin = false) {
  const onChanged = vi.fn();
  const onClose = vi.fn();
  render(<GroupsModal isSystemAdmin={isSystemAdmin} onChanged={onChanged} onClose={onClose} />);
  return { onChanged, onClose };
}

test("清单按分类与角色标注,普通用户只看到「项目」这一档建组选项", async () => {
  renderModal();

  await screen.findByText("封装项目");
  expect(screen.getByText("工艺部")).toBeInTheDocument();
  expect(screen.getByText("项目")).toBeInTheDocument();
  expect(screen.getByText("部门")).toBeInTheDocument();
  expect(screen.getByText("组管理员")).toBeInTheDocument();
  // 分类选择器只在有多个可选分类时出现;普通用户只能建项目组。
  expect(screen.queryByLabelText("群组分类")).not.toBeInTheDocument();
  expect(screen.getByText(/部门与领域群组由管理员创建/)).toBeInTheDocument();
  // 「全部群组」是系统管理员的运维视图,普通用户看不到。
  expect(screen.queryByRole("tab", { name: "全部群组" })).not.toBeInTheDocument();
});

test("系统管理员能选分类建组,并有「全部群组」视图", async () => {
  const user = userEvent.setup();
  vi.mocked(createGroup).mockResolvedValue({ ...ADMIN_DETAIL, id: "g9", name: "先进封装", kind: "domain" });
  renderModal(true);

  await screen.findByText("封装项目");
  const kindPicker = screen.getByLabelText("群组分类");
  await user.selectOptions(kindPicker, "domain");
  await user.type(screen.getByLabelText("群组名称"), "先进封装");
  await user.click(screen.getByRole("button", { name: "创建群组" }));

  await waitFor(() => expect(createGroup).toHaveBeenCalledWith("先进封装", "domain", ""));

  await user.click(screen.getByRole("tab", { name: "全部群组" }));
  await waitFor(() => expect(listGroups).toHaveBeenCalledWith("all"));
});

test("组管理员可加人、改角色、撤销共享给本组的知识库", async () => {
  const user = userEvent.setup();
  vi.mocked(listGroupSharedNotebooks).mockResolvedValue([
    { notebook_id: "nb1", name: "封装工艺库", owner_username: "carol", roles: ["viewer"] },
  ]);
  vi.mocked(resolveUser).mockResolvedValue({ id: "u3", username: "dave", display_name: "" });
  vi.mocked(putGroupMember).mockResolvedValue(ADMIN_DETAIL);
  vi.mocked(revokeGroupSharedNotebook).mockResolvedValue(undefined);
  const { onChanged } = renderModal();

  await screen.findByText("封装项目");
  await user.click(screen.getAllByRole("button", { name: "查看" })[0]);

  // 成员清单:显示名与用户名一起给出,免得两个 display_name 相同的人分不开。
  await screen.findByText("爱丽丝（alice）");
  expect(screen.getByText("bob")).toBeInTheDocument();

  await user.type(screen.getByLabelText("要添加的用户名"), "dave");
  await user.click(screen.getByRole("button", { name: "添加成员" }));
  await waitFor(() => expect(resolveUser).toHaveBeenCalledWith("dave"));
  expect(putGroupMember).toHaveBeenCalledWith("g1", "u3", "member");

  await user.selectOptions(screen.getByLabelText("bob 的角色"), "admin");
  await waitFor(() => expect(putGroupMember).toHaveBeenCalledWith("g1", "u2", "admin"));

  await user.click(screen.getByRole("button", { name: "撤销共享" }));
  await waitFor(() => expect(revokeGroupSharedNotebook).toHaveBeenCalledWith("g1", "nb1"));
  expect(onChanged).toHaveBeenCalled();
});

test("普通成员没有管理入口,也不去读「共享给本组的知识库」(那条清单对他 404)", async () => {
  const user = userEvent.setup();
  vi.mocked(getGroup).mockResolvedValue({
    ...ADMIN_DETAIL,
    id: "g2",
    name: "工艺部",
    kind: "department",
    my_role: "member",
  });
  renderModal();

  await screen.findByText("工艺部");
  await user.click(screen.getAllByRole("button", { name: "查看" })[1]);

  await screen.findByText("工艺部 · 部门");
  expect(listGroupSharedNotebooks).not.toHaveBeenCalled();
  // 「待审批申请」也只属于组管理员这一层——普通成员连查都不查。
  expect(listGroupShareRequests).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "添加成员" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "删除群组" })).not.toBeInTheDocument();
  expect(screen.queryByText("共享给本组的知识库")).not.toBeInTheDocument();
  expect(screen.queryByText("待审批申请")).not.toBeInTheDocument();
  // 自助退出仍在:它按「我是不是成员」显示,与管理权无关。
  expect(screen.getByRole("button", { name: "退出群组" })).toBeInTheDocument();
});

// 新建的组:创建者恒是组管理员,所以「待审批申请」区必然渲染;不初始化 shareRequests
// 就会把 null 渲染成「加载中…」并永久卡在那里,直到关掉重开(codex #519 R2 P2-2)。
test("新建群组后审批区显示空态而不是永久「加载中」", async () => {
  const user = userEvent.setup();
  vi.mocked(createGroup).mockResolvedValue({ ...ADMIN_DETAIL, id: "g9", name: "新组" });
  renderModal();

  await screen.findByText("封装项目");
  await user.type(screen.getByLabelText("群组名称"), "新组");
  await user.click(screen.getByRole("button", { name: "创建群组" }));

  await screen.findByText("待审批申请");
  expect(await screen.findByText("没有待审批的共享申请。")).toBeInTheDocument();
  expect(screen.queryByText("加载中…")).not.toBeInTheDocument();
  // 新组的待审批集合可证明为空,不必多发一次必然返回 [] 的请求。
  expect(listGroupShareRequests).not.toHaveBeenCalled();
});

test("组管理员看到「待审批申请」区,可批准(写边、刷新)或驳回", async () => {
  const user = userEvent.setup();
  vi.mocked(listGroupShareRequests)
    .mockResolvedValueOnce([
      shareRequest({ id: "sr1", notebook_name: "候选库甲", requested_by_username: "erin" }),
      shareRequest({ id: "sr2", notebook_name: "候选库乙", requested_by_username: "frank" }),
    ])
    .mockResolvedValue([shareRequest({ id: "sr2", notebook_name: "候选库乙" })]);
  vi.mocked(approveShareRequest).mockResolvedValue(shareRequest({ id: "sr1", status: "approved" }));
  const { onChanged } = renderModal();

  await screen.findByText("封装项目");
  await user.click(screen.getAllByRole("button", { name: "查看" })[0]);

  await screen.findByText("待审批申请");
  expect(screen.getByText("候选库甲")).toBeInTheDocument();
  expect(screen.getByText("申请人 erin")).toBeInTheDocument();

  // 批准第一条 → 写边、刷新申请队列与共享清单、让外层重取。
  await user.click(screen.getAllByRole("button", { name: "批准" })[0]);
  await waitFor(() => expect(approveShareRequest).toHaveBeenCalledWith("g1", "sr1"));
  await waitFor(() => expect(listGroupSharedNotebooks).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

test("驳回申请不写边,只刷新审核队列", async () => {
  const user = userEvent.setup();
  vi.mocked(listGroupShareRequests)
    .mockResolvedValueOnce([shareRequest({ id: "sr1" })])
    .mockResolvedValue([]);
  vi.mocked(rejectShareRequest).mockResolvedValue(shareRequest({ id: "sr1", status: "rejected" }));
  renderModal();

  await screen.findByText("封装项目");
  await user.click(screen.getAllByRole("button", { name: "查看" })[0]);
  await screen.findByText("待审批申请");

  await user.click(screen.getByRole("button", { name: "驳回" }));
  await waitFor(() => expect(rejectShareRequest).toHaveBeenCalledWith("g1", "sr1"));
  // 驳回不发授权边。
  expect(approveShareRequest).not.toHaveBeenCalled();
});

test("删除群组是两步确认,并说清共享会被一并收回", async () => {
  const user = userEvent.setup();
  vi.mocked(deleteGroup).mockResolvedValue(undefined);
  renderModal();

  await screen.findByText("封装项目");
  await user.click(screen.getAllByRole("button", { name: "查看" })[0]);
  await screen.findByText("封装项目 · 项目");

  await user.click(screen.getByRole("button", { name: "删除群组" }));
  expect(deleteGroup).not.toHaveBeenCalled();
  expect(screen.getByText(/共享给这个群组的知识库会一并收回/)).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "确认删除" }));
  await waitFor(() => expect(deleteGroup).toHaveBeenCalledWith("g1"));
});

test("退出被后端拒绝(最后一名组管理员是 409)时必须上屏,不能静默什么都没发生", async () => {
  const user = userEvent.setup();
  vi.mocked(leaveGroup).mockRejectedValue(
    new Error("你是这个群组唯一的组管理员，请先指定其他组管理员再退出"),
  );
  renderModal();

  await screen.findByText("封装项目");
  await user.click(screen.getAllByRole("button", { name: "查看" })[0]);
  await screen.findByText("封装项目 · 项目");
  await user.click(screen.getByRole("button", { name: "退出群组" }));
  await user.click(screen.getByRole("button", { name: "确认退出" }));

  // 经 `user_error()` 盖过章的后端文案会由 errors.ts 原样透传;本用例在模块边界打桩,
  // 拿到的是未盖章的裸 Error,所以看到的是兜底文案 —— 两条路都必须出现一句「没成功」。
  const banner = await screen.findByText("退出群组失败");
  expect(banner).toHaveClass("error");
  // 失败之后确认态保留、组仍在:界面不能假装退出成功。
  expect(screen.getByRole("button", { name: "确认退出" })).toBeInTheDocument();
  expect(screen.getByText("封装项目 · 项目")).toBeInTheDocument();
});


// P3-2 全栈对等的小缺口:后端 PATCH 一直支持 description,界面却既不显示也不给编辑。
test("组说明可显示与编辑,保存后让外层重取(组名进笔记本卡片的来源标注)", async () => {
  const user = userEvent.setup();
  vi.mocked(updateGroup).mockResolvedValue({
    ...ADMIN_DETAIL, name: "封装项目 2026", description: "改过的说明",
  });
  const { onChanged } = renderModal();

  await screen.findByText("封装项目");
  await user.click(screen.getAllByRole("button", { name: "查看" })[0]);

  const description = await screen.findByLabelText("群组说明");
  expect(description).toHaveValue("封装工艺相关的项目组");
  // 名字没改、说明改了,也要能保存 —— 保存闸不能只看名字。
  await user.clear(description);
  await user.type(description, "改过的说明");
  await user.click(screen.getByRole("button", { name: "保存群组信息" }));

  await waitFor(() => expect(updateGroup).toHaveBeenCalledWith("g1", "封装项目", "改过的说明"));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

test("普通成员只读到说明,没有编辑框", async () => {
  const user = userEvent.setup();
  vi.mocked(getGroup).mockResolvedValue({
    ...ADMIN_DETAIL, my_role: "member", description: "只读的说明",
  });
  renderModal();

  await screen.findByText("封装项目");
  await user.click(screen.getAllByRole("button", { name: "查看" })[0]);

  await screen.findByText("只读的说明");
  expect(screen.queryByLabelText("群组说明")).not.toBeInTheDocument();
});

// 串组事故:详情请求失败时,如果不先清空,屏幕上会留着上一个组的成员与「共享给本组
// 的知识库」,而标题接的是刚点开的那个组 —— 一排「撤销共享」打的是新组、列的却是旧
// 组的库。
test("打开另一个组失败时不残留上一个组的成员与共享清单", async () => {
  const user = userEvent.setup();
  vi.mocked(listGroupSharedNotebooks).mockResolvedValue([
    { notebook_id: "nb1", name: "甲组的库", owner_username: "carol", roles: ["viewer"] },
  ]);
  renderModal();

  await screen.findByText("封装项目");
  await user.click(screen.getAllByRole("button", { name: "查看" })[0]);
  await screen.findByText("甲组的库");

  vi.mocked(getGroup).mockRejectedValue(new Error("boom"));
  await user.click(screen.getAllByRole("button", { name: "查看" })[1]);

  await screen.findByText("群组详情加载失败");
  expect(screen.queryByText("甲组的库")).not.toBeInTheDocument();
  expect(screen.queryByText("爱丽丝（alice）")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "撤销共享" })).not.toBeInTheDocument();
});


// 移出成员会当场撤掉这个人经**本组**拿到的全部知识库访问——爆炸半径与删组同量级,
// 不该一击即发(组件自述是两步确认,实现却少了这一半)。
test("移出成员是两步确认,第一下不发请求", async () => {
  const user = userEvent.setup();
  vi.mocked(removeGroupMember).mockResolvedValue(undefined);
  const { onChanged } = renderModal();

  await screen.findByText("封装项目");
  await user.click(screen.getAllByRole("button", { name: "查看" })[0]);
  await screen.findByText("爱丽丝（alice）");

  await user.click(screen.getAllByRole("button", { name: "移出群组" })[1]);
  expect(removeGroupMember).not.toHaveBeenCalled();
  expect(screen.getByText(/他将看不到共享给本组的知识库/)).toBeInTheDocument();

  // 取消回到原状,仍然没发请求。
  await user.click(screen.getByRole("button", { name: "取消" }));
  expect(removeGroupMember).not.toHaveBeenCalled();
  expect(screen.getAllByRole("button", { name: "移出群组" })).toHaveLength(2);

  await user.click(screen.getAllByRole("button", { name: "移出群组" })[1]);
  await user.click(screen.getByRole("button", { name: "确认移出" }));
  await waitFor(() => expect(removeGroupMember).toHaveBeenCalledWith("g1", "u2"));
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

// 数值上限红线:前端显示同一护栏(敲不进去),API 超限明确拒绝。
test("组名与说明的长度护栏在前端同显,快到上限时出声", async () => {
  const user = userEvent.setup();
  renderModal();

  await screen.findByText("封装项目");
  expect(screen.getByLabelText("群组名称")).toHaveAttribute("maxlength", "120");
  expect(screen.getByLabelText("新群组的说明")).toHaveAttribute("maxlength", "1000");

  // 还早的时候一个字都不渲染(不给常驻计数噪音)。
  await user.type(screen.getByLabelText("群组名称"), "短名字");
  expect(screen.queryByText(/还可输入/)).not.toBeInTheDocument();

  await user.clear(screen.getByLabelText("群组名称"));
  await user.paste("x".repeat(115));
  expect(screen.getByText("群组名称还可输入 5 个字")).toBeInTheDocument();

  await user.click(screen.getAllByRole("button", { name: "查看" })[0]);
  await screen.findByLabelText("群组说明");
  expect(screen.getByLabelText("群组新名称")).toHaveAttribute("maxlength", "120");
  expect(screen.getByLabelText("群组说明")).toHaveAttribute("maxlength", "1000");
});

// --- 我发起的共享申请:失权申请人的全局撤回入口(codex #519 R11 P1) --------------

test("我发起的待审批申请出现在群组面板顶层,且撤回不依赖任何笔记本权限", async () => {
  const user = userEvent.setup();
  // 舞台:他已经失去那本库的管理权,所以笔记本工作区里那块 UI 对他不存在。这一节是
  // 裁决 P2-7 留的口子在界面上的落点——入口必须在工作区之外。
  vi.mocked(listMyPendingShareRequests)
    .mockResolvedValueOnce([shareRequest({ id: "sr-mine", notebook_id: "nb-lost", notebook_name: "Alice 的库" })])
    .mockResolvedValue([]);
  vi.mocked(withdrawShareRequest).mockResolvedValue(undefined);
  renderModal();

  await screen.findByText("我发起的共享申请");
  expect(screen.getByText("Alice 的库 → 封装项目")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "撤回" }));
  // 撤回打的是申请所属的那本笔记本 —— 全局清单必须把 notebook_id 带回来,否则这个
  // 动作根本发不出去。
  await waitFor(() => expect(withdrawShareRequest).toHaveBeenCalledWith("nb-lost", "sr-mine"));
  await waitFor(() => expect(screen.queryByText("我发起的共享申请")).not.toBeInTheDocument());
});

test("撤回成功但列表刷新失败:不报失败、行不残留、不诱导重试", async () => {
  const user = userEvent.setup();
  // 与批准/驳回**同一种形状**,所以必须走同一条两段式:撤回已经生效,重取失败只是
  // 这一屏落后了。报成「撤回申请失败」会让人再点一次,而那一次必然 404——申请行
  // 已经被整行删掉了(裁决 P2-2:撤回不是第三个状态,是 DELETE)。
  vi.mocked(listMyPendingShareRequests)
    .mockResolvedValueOnce([shareRequest({ id: "sr-mine", notebook_id: "nb-lost", notebook_name: "Alice 的库" })])
    .mockRejectedValue(new Error("network"));   // 对账那次失败
  vi.mocked(withdrawShareRequest).mockResolvedValue(undefined);
  renderModal();

  await screen.findByText("我发起的共享申请");
  await user.click(screen.getByRole("button", { name: "撤回" }));

  await waitFor(() => expect(withdrawShareRequest).toHaveBeenCalledWith("nb-lost", "sr-mine"));
  await waitFor(() => expect(screen.getByText(/列表没能刷新/)).toBeInTheDocument());
  expect(screen.queryByText("撤回申请失败")).not.toBeInTheDocument();
  // 那条已撤回的申请不能残留(本地摘行必须发生在 mutate 里,不能只靠对账)。
  expect(screen.queryByRole("button", { name: "撤回" })).not.toBeInTheDocument();
});

test("撤回本身失败时仍然报失败,行留着让人重试", async () => {
  const user = userEvent.setup();
  vi.mocked(listMyPendingShareRequests)
    .mockResolvedValue([shareRequest({ id: "sr-mine", notebook_id: "nb-lost", notebook_name: "Alice 的库" })]);
  vi.mocked(withdrawShareRequest).mockRejectedValue(new Error("nope"));
  renderModal();

  await screen.findByText("我发起的共享申请");
  await user.click(screen.getByRole("button", { name: "撤回" }));

  await screen.findByText("撤回申请失败");
  expect(screen.queryByText(/列表没能刷新/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "撤回" })).toBeInTheDocument();
});

test("没有待审批申请时不渲染这一节,也不因它加载失败而盖住整个面板", async () => {
  vi.mocked(listMyPendingShareRequests).mockRejectedValue(new Error("boom"));
  renderModal();

  await screen.findByText("群组清单");
  expect(screen.queryByText("我发起的共享申请")).not.toBeInTheDocument();
  // 它是补救入口,自己挂了不该把群组面板整块报成错误。
  expect(screen.queryByText(/加载失败/)).not.toBeInTheDocument();
});

// --- 批准/驳回:改动成功与对账失败必须分开报(codex #519 R11 P2) ----------------

test("批准成功但列表刷新失败:不报失败、行不残留、不诱导重试", async () => {
  const user = userEvent.setup();
  vi.mocked(listGroupShareRequests)
    .mockResolvedValueOnce([shareRequest({ id: "sr-ok" })])
    .mockRejectedValue(new Error("network"));   // 对账那次失败
  vi.mocked(approveShareRequest).mockResolvedValue(shareRequest({ id: "sr-ok", status: "approved" }));
  renderModal();

  await user.click((await screen.findAllByRole("button", { name: "查看" }))[0]);
  await screen.findByText("待审批申请");
  await user.click(await screen.findByRole("button", { name: "批准" }));

  await waitFor(() => expect(approveShareRequest).toHaveBeenCalledWith("g1", "sr-ok"));
  // ① 不报「批准申请失败」——批准**成功了**,报失败会把人骗去重试一个已完成的动作
  //    (重试必然 404:它已经不是待审批状态)。
  await waitFor(() => expect(screen.getByText(/列表没能刷新/)).toBeInTheDocument());
  expect(screen.queryByText("批准申请失败")).not.toBeInTheDocument();
  // ② 那条已批准的行不能残留在待审批清单里。
  expect(screen.queryByRole("button", { name: "批准" })).not.toBeInTheDocument();
});

test("批准本身失败时仍然报失败(分离不能变成把错误吞掉)", async () => {
  const user = userEvent.setup();
  vi.mocked(listGroupShareRequests).mockResolvedValue([shareRequest({ id: "sr-bad" })]);
  vi.mocked(approveShareRequest).mockRejectedValue(new Error("nope"));
  renderModal();

  await user.click((await screen.findAllByRole("button", { name: "查看" }))[0]);
  await user.click(await screen.findByRole("button", { name: "批准" }));

  await screen.findByText("批准申请失败");
  expect(screen.queryByText(/列表没能刷新/)).not.toBeInTheDocument();
  // 改动没生效,行必须留着让人重试。
  expect(screen.getByRole("button", { name: "批准" })).toBeInTheDocument();
});
