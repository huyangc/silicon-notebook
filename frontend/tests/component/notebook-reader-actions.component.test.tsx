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
