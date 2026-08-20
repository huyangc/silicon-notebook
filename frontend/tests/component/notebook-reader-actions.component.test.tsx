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
});

test("卡片菜单:组管理员只有「由组管理员管理」——没有编辑/删除/退出", () => {
  // ⚠ P2-T2 评审 P2-4 修正:卡片菜单的「编辑信息」打开的是那个 fused 编辑器
  // (openNotebookEditor 连挂载配置一起拉,是 owner-only 的 notebook:configure),
  // 给组管理员画出来点了会在 listMountable 上 404。所以卡片菜单对组管理员**不给**
  // 编辑信息——他改名走工作区顶栏的行内输入框(见下一条),那条是 PATCH-only。
  render(
    <NotebookMenuActions
      notebook={notebook({ granted_via: VIA_GROUP, can_manage_content: true })}
      onLeave={vi.fn()}
      onEdit={vi.fn()}
      onDelete={vi.fn()}
    />,
  );

  expect(screen.getByText("由组管理员管理")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "编辑信息" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "删除笔记本" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "退出共享" })).not.toBeInTheDocument();
});

// P2-T2 评审 P2-4:组管理员在顶栏改名(PATCH-only,notebook:manage),而不是走 fused
// 编辑器。徽章按 can_manage_content 渲染成**可编辑输入框**,并保住身份标注。
test("顶栏徽章:组管理员的标题是可编辑输入框,改名回调可用", async () => {
  const onCommit = vi.fn();
  const onChange = vi.fn();
  render(
    <ReaderNotebookBadge
      notebook={notebook({ granted_via: VIA_GROUP, can_manage_content: true })}
      leaveBusy={false}
      onLeave={vi.fn()}
      rename={{
        value: "封装工艺库",
        saving: false,
        onChange,
        onCommit,
        onReset: vi.fn(),
      }}
    />,
  );

  const input = screen.getByRole("textbox", { name: "笔记本名称" }) as HTMLInputElement;
  expect(input.value).toBe("封装工艺库");
  // 身份标注仍在(可编辑标题 + 「可管理·来自群组《X》」)。
  expect(screen.getByText("可管理 · 来自群组《封装项目》")).toBeInTheDocument();
  await userEvent.setup().type(input, "X");
  expect(onChange).toHaveBeenCalled();
  input.blur();
  expect(onCommit).toHaveBeenCalledOnce();
});

// 纯只读成员即便传了 rename 也**不**渲染成可编辑(can_manage_content 为假)——
// 门控是 can_manage_content,不是「有没有传 rename」。
test("顶栏徽章:纯只读成员的标题不可编辑,即便传了 rename", () => {
  render(
    <ReaderNotebookBadge
      notebook={notebook({ granted_via: VIA_GROUP, can_manage_content: false })}
      leaveBusy={false}
      onLeave={vi.fn()}
      rename={{
        value: "封装工艺库",
        saving: false,
        onChange: vi.fn(),
        onCommit: vi.fn(),
        onReset: vi.fn(),
      }}
    />,
  );
  expect(screen.queryByRole("textbox", { name: "笔记本名称" })).not.toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "封装工艺库" })).toBeInTheDocument();
});


// ---------------------------------------------------- 顶栏版式(2026-08-20 用户反馈)
//
// 现象:群组共享库点进去「完全看不到 notebook 名字」。原因不在数据——`notebook.name`
// 一直是对的——而在版式:徽章行原本用 `.tag-row`(flex-wrap: wrap),里面塞了标题 +
// 徽章 + 一句长说明,而 `.workspace-header` 是**固定 72px 单行**。三样东西排成三行、
// 在 72px 里垂直居中,标题那一行就被推到可视区**之上**(静态量过:innerH 141 > 72,
// 标题 top 在 header top 之上),说明文字则漏到标题栏外面盖住下方内容。
//
// jsdom 没有排版,量不了那 141px;能在这里钉住的是**成因**的三条结构性前提。
// `.reader-badge-row` 自己「不换行」由 `tests/guards/reader-badge-layout-guard.test.mjs`
// 在 CSS 里钉。

test("顶栏徽章:库名必须在(它是这一行的主角,不是徽章)", () => {
  const { container } = render(
    <ReaderNotebookBadge
      notebook={notebook({ granted_via: VIA_GROUP })}
      leaveBusy={false}
      onLeave={vi.fn()}
    />,
  );

  expect(screen.getByRole("heading", { name: "封装工艺库" })).toBeInTheDocument();
  // 恒单行的那个容器,不是会换行的 `.tag-row`。
  expect(container.querySelector(".reader-badge-row")).not.toBeNull();
  expect(container.querySelector(".tag-row")).toBeNull();
});

test("顶栏徽章:不再把长说明塞进顶栏(用户明确不需要「怎么停止访问」)", () => {
  const { container } = render(
    <ReaderNotebookBadge
      notebook={notebook({ granted_via: VIA_GROUP })}
      leaveBusy={false}
      onLeave={vi.fn()}
    />,
  );

  expect(container.querySelector(".tool-hint")).toBeNull();
  expect(screen.queryByText(/停止访问/)).toBeNull();
  expect(screen.queryByText(/联系组管理员/)).toBeNull();
  // 身份解释没有消失,只是挪进了 tooltip。
  expect(screen.getByText("只读 · 来自群组《封装项目》")).toHaveAttribute(
    "title",
    expect.stringContaining("组管理员"),
  );
});

// 徽章按 `.reader-badge-chip` 的 max-width 有界,长组名(上限 120 字符,且
// `grantedViaLabel` 会把多个组名串起来)会被省略号截掉。完整文本必须同时在 tooltip 里,
// 否则读者看得见「只读 · 来自群组《长长长…」却看不出到底是哪个组(codex #529 R1 P2)。
test("顶栏徽章:tooltip 同时带完整徽章文本与身份说明", () => {
  const long = "封装工艺与良率改善跨部门协同项目组第二阶段";
  render(
    <ReaderNotebookBadge
      notebook={notebook({ granted_via: [{ group_id: "g9", group_name: long, kind: "project" }] })}
      leaveBusy={false}
      onLeave={vi.fn()}
    />,
  );

  const chip = screen.getByText(`只读 · 来自群组《${long}》`);
  const title = chip.getAttribute("title") ?? "";
  expect(title).toContain(long);
  expect(title).toContain("组管理员");
});

test("顶栏徽章:链接共享的「退出共享」是这一行唯一的动作,必须还在", () => {
  const { container } = render(
    <ReaderNotebookBadge notebook={notebook({})} leaveBusy={false} onLeave={vi.fn()} />,
  );
  expect(screen.getByRole("button", { name: "退出共享" })).toHaveClass("reader-badge-action");
  expect(container.querySelector(".tool-hint")).toBeNull();
});
