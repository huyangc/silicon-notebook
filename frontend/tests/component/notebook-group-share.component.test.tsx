import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/group-api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/group-api.ts")>();
  return {
    ...actual,
    listGroups: vi.fn(),
    listNotebookGrants: vi.fn(),
    shareNotebookToGroup: vi.fn(),
    revokeNotebookGrant: vi.fn(),
  };
});

import {
  listGroups,
  listNotebookGrants,
  revokeNotebookGrant,
  shareNotebookToGroup,
  type GroupSummary,
  type NotebookGrant,
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

beforeEach(() => {
  vi.mocked(listGroups).mockResolvedValue([ADMIN_GROUP, MEMBER_GROUP]);
  vi.mocked(listNotebookGrants).mockResolvedValue([]);
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

test("共享给群组只发一条只读授权,并让外层重取笔记本清单", async () => {
  const user = userEvent.setup();
  vi.mocked(shareNotebookToGroup).mockResolvedValue(grant({}));
  const { onChanged } = renderSection();

  await screen.findByLabelText("选择群组");
  await user.selectOptions(screen.getByLabelText("选择群组"), "g1");
  await user.click(screen.getByRole("button", { name: "共享给该群组" }));

  await waitFor(() => expect(shareNotebookToGroup).toHaveBeenCalledWith("nb1", "g1"));
  // P1 的裁决:不发第二条 group_admins 边——那条边现在没有任何效果。
  expect(shareNotebookToGroup).toHaveBeenCalledTimes(1);
  await waitFor(() => expect(onChanged).toHaveBeenCalled());
});

test("已共享的组不再出现在选择器里;撤销会把该组的两条边一起删掉", async () => {
  const user = userEvent.setup();
  vi.mocked(listNotebookGrants).mockResolvedValue([
    grant({ id: "gr1", principal_type: "group" }),
    grant({ id: "gr2", principal_type: "group_admins", role: "admin" }),
  ]);
  vi.mocked(revokeNotebookGrant).mockResolvedValue(undefined);
  renderSection();

  await screen.findByText("封装项目");
  // 唯一的组管理员身份的组已经共享过,所以连选择器都不该出现。
  expect(screen.queryByLabelText("选择群组")).not.toBeInTheDocument();
  expect(screen.getByText(/没有可选的群组/)).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "撤销共享" }));
  await waitFor(() => expect(revokeNotebookGrant).toHaveBeenCalledTimes(2));
  expect(revokeNotebookGrant).toHaveBeenCalledWith("nb1", "gr1");
  expect(revokeNotebookGrant).toHaveBeenCalledWith("nb1", "gr2");
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
