import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import {
  NotebookMenuActions,
  ReaderNotebookBadge,
} from "../../app/notebook-reader-actions.tsx";
import type { NotebookSummary } from "../../app/workspace-model.ts";

// 后端真实形状:`GET /notebooks/{id}` 的详情投影现在回填 access / shared_from /
// granted_via 三个字段(P1-T4 修的长期缺口——此前 access 恒为模型默认的 "owner",
// 这整段 reader 界面从来没有为真过)。用例按那份形状构造。
function notebook(over: Partial<NotebookSummary>): NotebookSummary {
  return {
    id: "nb1",
    name: "封装工艺库",
    purpose: "",
    primary_domain: "",
    status: "ready",
    counts: {},
    created_label: "2026年8月17日",
    access: "reader",
    shared_from: "carol",
    ...over,
  } as NotebookSummary;
}

const VIA_GROUP = [{ group_id: "g1", group_name: "封装项目", kind: "project" }];

test("只读共享进来的库:显示「来自 X」并给退出入口", async () => {
  const user = userEvent.setup();
  const onLeave = vi.fn();
  render(<ReaderNotebookBadge notebook={notebook({})} leaveBusy={false} onLeave={onLeave} />);

  expect(screen.getByText("只读 · 来自 carol")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "退出共享" }));
  expect(onLeave).toHaveBeenCalledOnce();
});

// 核心不变量:那个按钮打的是只读成员表,对群组授权边是空操作——点了会弹「已退出」
// 而库还在列表里,是一个必然发生的假失败。
test("经群组共享进来的库:标注来源群组,且**没有**退出入口", () => {
  render(
    <ReaderNotebookBadge
      notebook={notebook({ granted_via: VIA_GROUP })}
      leaveBusy={false}
      onLeave={vi.fn()}
    />,
  );

  expect(screen.getByText("只读 · 来自群组《封装项目》")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "退出共享" })).not.toBeInTheDocument();
  expect(screen.getByText(/由组管理员管理/)).toBeInTheDocument();
});

test("退出在飞时按钮禁用并换成进行态文案", () => {
  render(<ReaderNotebookBadge notebook={notebook({})} leaveBusy onLeave={vi.fn()} />);
  const button = screen.getByRole("button", { name: "退出中…" });
  expect(button).toBeDisabled();
});

test("卡片菜单:只读共享给退出、群组共享只说明由谁管理、owner 是编辑/删除", async () => {
  const user = userEvent.setup();
  const onLeave = vi.fn();
  const { rerender } = render(
    <NotebookMenuActions
      notebook={notebook({})}
      onLeave={onLeave}
      onEdit={vi.fn()}
      onDelete={vi.fn()}
    />,
  );
  await user.click(screen.getByRole("button", { name: "退出共享" }));
  expect(onLeave).toHaveBeenCalledOnce();

  rerender(
    <NotebookMenuActions
      notebook={notebook({ granted_via: VIA_GROUP })}
      onLeave={onLeave}
      onEdit={vi.fn()}
      onDelete={vi.fn()}
    />,
  );
  expect(screen.queryByRole("button", { name: "退出共享" })).not.toBeInTheDocument();
  expect(screen.getByText("由组管理员管理")).toBeInTheDocument();

  rerender(
    <NotebookMenuActions
      notebook={notebook({ access: "owner", shared_from: "" })}
      onLeave={onLeave}
      onEdit={vi.fn()}
      onDelete={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "编辑信息" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "删除笔记本" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "退出共享" })).not.toBeInTheDocument();
});

// P3-3 交叉态:同时经 share-token 加入**又**被共享给我所在的群组。列表投影按
// 「成员行优先」去重,`granted_via` 为空 —— 于是他仍然拿得到「退出共享」,那个动作
// 对他也确实有效(它删的正是那条成员行)。这条钉的是「前端不按 access 猜来源」。
test("交叉态(既是只读成员又在授权群组里):成员行优先,退出共享仍然可用", () => {
  render(
    <ReaderNotebookBadge
      notebook={notebook({ granted_via: [] })}
      leaveBusy={false}
      onLeave={vi.fn()}
    />,
  );
  expect(screen.getByRole("button", { name: "退出共享" })).toBeInTheDocument();
  expect(screen.getByText("只读 · 来自 carol")).toBeInTheDocument();
});

// ---------------------------------------------------------------- P2:组管理员
//
// `access` 仍是 "reader"(权限档没有新增枚举值,裁决 P2-3),但 `can_manage_content`
// 为真时后端六个内容管理能力全部放行。界面必须跟上,否则组管理员对着一个 API 允许、
// 按钮全藏的只读工作区;反过来写着「只读」而整屏写入口都亮着,也是一句自相矛盾的话。

test("组管理员打开共享库:徽章说「可管理」而不是「只读」", () => {
  render(
    <ReaderNotebookBadge
      notebook={notebook({ granted_via: VIA_GROUP, can_manage_content: true })}
      leaveBusy={false}
      onLeave={vi.fn()}
    />,
  );

  expect(screen.getByText("可管理 · 来自群组《封装项目》")).toBeInTheDocument();
  expect(screen.queryByText(/^只读 ·/)).not.toBeInTheDocument();
  // 说明改成「你是该群组的管理员」——原来那句「由组管理员管理」是说给**别人**听的。
  expect(screen.getByText(/你是该群组的管理员/)).toBeInTheDocument();
  // 他的访问来自授权边,「退出共享」打的是成员表,对他仍是空操作。
  expect(screen.queryByRole("button", { name: "退出共享" })).not.toBeInTheDocument();
});

test("同一本库、只差 can_manage_content:纯只读成员仍写「只读」", () => {
  render(
    <ReaderNotebookBadge
      notebook={notebook({ granted_via: VIA_GROUP, can_manage_content: false })}
      leaveBusy={false}
      onLeave={vi.fn()}
    />,
  );
  expect(screen.getByText("只读 · 来自群组《封装项目》")).toBeInTheDocument();
  expect(screen.getByText(/由组管理员管理/)).toBeInTheDocument();
});

test("卡片菜单:组管理员有「编辑信息」但**没有**「删除笔记本」", async () => {
  const onEdit = vi.fn();
  render(
    <NotebookMenuActions
      notebook={notebook({ granted_via: VIA_GROUP, can_manage_content: true })}
      onLeave={vi.fn()}
      onEdit={onEdit}
      onDelete={vi.fn()}
    />,
  );

  await userEvent.setup().click(screen.getByRole("button", { name: "编辑信息" }));
  expect(onEdit).toHaveBeenCalledOnce();
  // `notebook:delete` 恒 owner(裁决 P2-1):画出来只会 404,而它是不可撤销的动作。
  expect(screen.queryByRole("button", { name: "删除笔记本" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "退出共享" })).not.toBeInTheDocument();
  expect(screen.getByText("由组管理员管理")).toBeInTheDocument();
});
