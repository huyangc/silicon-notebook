import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, test, vi } from "vitest";

vi.mock("../../app/group-api.ts", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/group-api.ts")>();
  return {
    ...actual,
    listGroups: vi.fn(),
    getGroup: vi.fn(),
    getGroupInvite: vi.fn(),
    createGroupInvite: vi.fn(),
    listGroupSharedNotebooks: vi.fn(),
    listGroupShareRequests: vi.fn(),
    listMyPendingShareRequests: vi.fn(),
    shareNotebookToGroup: vi.fn(),
    transferGroupOwner: vi.fn(),
    leaveGroup: vi.fn(),
  };
});

import {
  createGroupInvite,
  getGroup,
  getGroupInvite,
  listGroupShareRequests,
  listGroupSharedNotebooks,
  listGroups,
  listMyPendingShareRequests,
  leaveGroup,
  shareNotebookToGroup,
  transferGroupOwner,
  type GroupDetail,
} from "../../app/group-api.ts";
import { GroupsPage } from "../../app/groups-page.tsx";
import type { NotebookSummary } from "../../app/workspace-model.ts";

const OWNER_DETAIL: GroupDetail = {
  id: "g1",
  name: "先进封装项目",
  kind: "project",
  description: "跨团队共享封装工艺资料",
  owner_id: "u1",
  my_role: "admin",
  member_count: 2,
  created_at: "",
  members: [
    { id: "u1", username: "alice", display_name: "爱丽丝", role: "admin" },
    { id: "u2", username: "bob", display_name: "", role: "member" },
  ],
};

const NOTEBOOKS: NotebookSummary[] = [
  {
    id: "nb-owned",
    name: "先进封装工艺",
    purpose: "项目资料",
    primary_domain: "Packaging",
    status: "ready",
    counts: {},
    created_label: "刚刚",
    access: "owner",
  },
  {
    id: "nb-reader",
    name: "只读参考库",
    purpose: "",
    primary_domain: "",
    status: "ready",
    counts: {},
    created_label: "昨天",
    access: "reader",
  },
];

beforeEach(() => {
  vi.mocked(listGroups).mockResolvedValue([OWNER_DETAIL]);
  vi.mocked(getGroup).mockResolvedValue(OWNER_DETAIL);
  vi.mocked(getGroupInvite).mockResolvedValue({ active: false, token: "", created_at: null });
  vi.mocked(listGroupSharedNotebooks).mockResolvedValue([
    { notebook_id: "nb-shared", name: "共享可靠性资料", owner_username: "carol", roles: ["viewer"] },
  ]);
  vi.mocked(listGroupShareRequests).mockResolvedValue([]);
  vi.mocked(listMyPendingShareRequests).mockResolvedValue([]);
});

function renderPage(detail: GroupDetail = OWNER_DETAIL) {
  vi.mocked(listGroups).mockResolvedValue([detail]);
  vi.mocked(getGroup).mockResolvedValue(detail);
  const onChanged = vi.fn();
  const onNavigate = vi.fn();
  render(
    <GroupsPage
      currentUserId="u1"
      isSystemAdmin={false}
      notebooks={NOTEBOOKS}
      initialGroupId={detail.id}
      onBack={vi.fn()}
      onChanged={onChanged}
      onOpenNotebook={vi.fn()}
      onNavigate={onNavigate}
    />,
  );
  return { onChanged, onNavigate };
}

test("独立页面集中展示群组知识库，并只列出当前用户可管理的待添加 Notebook", async () => {
  const user = userEvent.setup();
  renderPage();

  expect(await screen.findByRole("heading", { name: "先进封装项目" })).toBeInTheDocument();
  expect(screen.getByText("共享可靠性资料")).toBeInTheDocument();
  expect(screen.getByText("先进封装工艺")).toBeInTheDocument();
  expect(screen.queryByText("只读参考库")).not.toBeInTheDocument();

  vi.mocked(shareNotebookToGroup).mockResolvedValue({
    id: "grant-1", principal_type: "group", principal_id: "g1", role: "viewer",
    principal_name: "先进封装项目", principal_kind: "project", created_at: "",
  });
  await user.click(screen.getByRole("checkbox", { name: /先进封装工艺/ }));
  await user.click(screen.getByRole("button", { name: "添加已选（1）" }));
  await waitFor(() => expect(shareNotebookToGroup).toHaveBeenCalledWith("nb-owned", "g1", { manage: false }));
});

test("owner 转让需要二次确认，新 owner 由服务端返回且旧 owner 保留管理员提示", async () => {
  const user = userEvent.setup();
  const transferred: GroupDetail = {
    ...OWNER_DETAIL,
    owner_id: "u2",
    my_role: "admin",
    members: OWNER_DETAIL.members.map((member) => member.id === "u2" ? { ...member, role: "admin" } : member),
  };
  vi.mocked(transferGroupOwner).mockResolvedValue(transferred);
  renderPage();

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.selectOptions(screen.getByLabelText("选择新的群组所有者"), "u2");
  await user.click(screen.getByRole("button", { name: "转让群组" }));
  expect(transferGroupOwner).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "确认转让" }));

  await waitFor(() => expect(transferGroupOwner).toHaveBeenCalledWith("g1", "u2"));
  expect(await screen.findByText("群组所有权已转让，你仍是组管理员。")).toBeInTheDocument();
});

test("组管理员可在成员页生成邀请链接", async () => {
  const user = userEvent.setup();
  vi.mocked(createGroupInvite).mockResolvedValue({
    active: true,
    token: "gri_test-token",
    created_at: "2026-08-21T00:00:00+00:00",
  });
  renderPage();

  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "成员" }));
  await user.click(screen.getByRole("button", { name: "生成邀请链接" }));

  await waitFor(() => expect(createGroupInvite).toHaveBeenCalledWith("g1"));
  expect(screen.getByDisplayValue(/group_invite=gri_test-token/)).toBeInTheDocument();
});

test("普通成员仍可查看群组知识库，但不加载审批队列且设置中只能退出", async () => {
  const user = userEvent.setup();
  const memberDetail: GroupDetail = { ...OWNER_DETAIL, owner_id: "u2", my_role: "member" };
  renderPage(memberDetail);

  expect(await screen.findByText("共享可靠性资料")).toBeInTheDocument();
  expect(listGroupSharedNotebooks).toHaveBeenCalledWith("g1");
  expect(listGroupShareRequests).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "设置" }));
  expect(screen.getByRole("button", { name: "退出群组" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "删除群组" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "转让群组" })).not.toBeInTheDocument();
});

test("退出后更新 hash 并自动选择剩余群组", async () => {
  const user = userEvent.setup();
  const memberDetail: GroupDetail = { ...OWNER_DETAIL, owner_id: "u2", my_role: "member" };
  const remaining: GroupDetail = {
    ...OWNER_DETAIL,
    id: "g2",
    name: "剩余群组",
    owner_id: "u2",
    my_role: "member",
  };
  vi.mocked(leaveGroup).mockResolvedValue(undefined);
  const { onNavigate } = renderPage(memberDetail);

  await screen.findByRole("heading", { name: "先进封装项目" });
  vi.mocked(listGroups).mockResolvedValue([remaining]);
  vi.mocked(getGroup).mockResolvedValue(remaining);
  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "退出群组" }));
  await user.click(screen.getByRole("button", { name: "确认退出" }));

  await waitFor(() => expect(onNavigate).toHaveBeenCalledWith("g2", "notebooks"));
  expect(await screen.findByRole("heading", { name: "剩余群组" })).toBeInTheDocument();
});

test("退出最后一个群组时立即失效旧 hash 请求且不把 404 写回空态", async () => {
  const user = userEvent.setup();
  const memberDetail: GroupDetail = { ...OWNER_DETAIL, owner_id: "u2", my_role: "member" };
  vi.mocked(leaveGroup).mockResolvedValue(undefined);
  const { onNavigate } = renderPage(memberDetail);

  await screen.findByRole("heading", { name: "先进封装项目" });
  let releaseList!: (groups: GroupDetail[]) => void;
  vi.mocked(listGroups).mockImplementationOnce(() => new Promise((resolve) => {
    releaseList = resolve;
  }));
  vi.mocked(getGroup).mockRejectedValueOnce(new Error("群组不存在"));

  await user.click(screen.getByRole("button", { name: "设置" }));
  await user.click(screen.getByRole("button", { name: "退出群组" }));
  await user.click(screen.getByRole("button", { name: "确认退出" }));

  await waitFor(() => expect(onNavigate).toHaveBeenCalledWith("", "notebooks"));
  expect(getGroup).toHaveBeenCalledTimes(1);
  releaseList([]);
  await waitFor(() => expect(screen.getByText("还没有群组")).toBeInTheDocument());
  expect(screen.queryByText("群组详情加载失败")).not.toBeInTheDocument();
});

// 复制邀请链接:结果必须画在按钮自身上。
//
// 缺陷来源:唯一的反馈曾经是页面**顶部**那条 notice 横幅——在长页面上它常常滚出视口,
// 用户看到的是「按钮纹丝不动」,于是判定没生效。按下态由 globals.css 的
// `button:...:active` 负责(那条没有 AST,由 tests/guards/button-press-feedback-guard
// 钉住),这里钉的是它管不到的另一半:**结果**态与它的自动还原。
const ACTIVE_INVITE = {
  active: true,
  token: "gri_copy-token",
  created_at: "2026-08-21T00:00:00+00:00",
} as const;

async function openInviteCard(user: ReturnType<typeof userEvent.setup>) {
  vi.mocked(getGroupInvite).mockResolvedValue({ ...ACTIVE_INVITE });
  renderPage();
  await screen.findByRole("heading", { name: "先进封装项目" });
  await user.click(screen.getByRole("button", { name: "成员" }));
  return await screen.findByRole("button", { name: "复制" });
}

test("复制成功时按钮自己变成「已复制」并换成成功配色，随后自动还原", async () => {
  const user = userEvent.setup();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  try {
    const copy = await openInviteCard(user);
    await user.click(copy);

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(
      expect.stringContaining("group_invite=gri_copy-token"),
    ));
    const copied = await screen.findByRole("button", { name: "已复制" });
    expect(copied).toHaveClass("copy-result-copied");

    // 结果态是 JS 状态,不像 :active 那样松手自动还原——忘了摘掉就会一直挂着,
    // 下一次点击反而看不出有没有点上。
    const restored = await screen.findByRole("button", { name: "复制" }, { timeout: 4000 });
    expect(restored).not.toHaveClass("copy-result-copied");
    expect(restored).not.toHaveClass("copy-result-failed");
  } finally {
    Reflect.deleteProperty(navigator, "clipboard");
  }
});

test("复制失败时按钮说「复制失败」，并把链接选中好让用户自己复制", async () => {
  // 剪贴板 API 存在却被拒(权限/非安全上下文/文档失焦),DOM 兜底的 execCommand 在 jsdom
  // 里又不存在——就是那台真的复制不了的浏览器。此时最要紧的不是那句提示,而是让用户当场
  // 就能 ⌘C。⚠ 不能靠「jsdom 没有剪贴板」来造这个场景:userEvent.setup() 自己会装一份
  // 可用的 navigator.clipboard,不打掉它这条用例会反过来测成功路径(实测过)。
  const user = userEvent.setup();
  vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("denied"));
  const copy = await openInviteCard(user);
  await user.click(copy);

  const failed = await screen.findByRole("button", { name: "复制失败" });
  expect(failed).toHaveClass("copy-result-failed");
  const link = screen.getByDisplayValue(/group_invite=gri_copy-token/);
  expect(document.activeElement).toBe(link);
  expect((link as HTMLInputElement).selectionEnd).toBe((link as HTMLInputElement).value.length);
});
