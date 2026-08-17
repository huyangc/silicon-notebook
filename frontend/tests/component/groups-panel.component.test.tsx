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
  };
});

import {
  createGroup,
  deleteGroup,
  getGroup,
  leaveGroup,
  listGroupSharedNotebooks,
  listGroups,
  putGroupMember,
  resolveUser,
  revokeGroupSharedNotebook,
  updateGroup,
  type GroupDetail,
  type GroupSummary,
} from "../../app/group-api.ts";
import { GroupsModal } from "../../app/groups-panel.tsx";

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
  expect(screen.queryByRole("button", { name: "添加成员" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "删除群组" })).not.toBeInTheDocument();
  expect(screen.queryByText("共享给本组的知识库")).not.toBeInTheDocument();
  // 自助退出仍在:它按「我是不是成员」显示,与管理权无关。
  expect(screen.getByRole("button", { name: "退出群组" })).toBeInTheDocument();
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
