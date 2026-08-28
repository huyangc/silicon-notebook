import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { SchemaManager, type SchemaWriteOutcome } from "../../app/schema-manager.tsx";
import type { ObjectSchema } from "../../app/workspace-model.ts";

function item(overrides: Partial<ObjectSchema> = {}): ObjectSchema {
  return {
    object_type: "claim",
    plural: "claims",
    fields: ["statement", "condition"],
    primary: "statement",
    description: "可验证结论",
    label: "结论",
    list_fields: [],
    source: "global",
    status: "active",
    rationale: "",
    notebook_id: "nb-1",
    scope: "global",
    inherited: true,
    overrides_global: false,
    can_edit: true,
    ...overrides,
  };
}

function mount(overrides: Partial<Parameters<typeof SchemaManager>[0]> = {}) {
  // overrides 先合进 props 再取回：直接返回默认那一份会让「传了自己的 onPatch」的用例
  // 断言到一个从未被接线的 spy 上，永远看到 0 次调用。
  const props = {
    schemas: [item()],
    busy: false,
    view: "notebook" as const,
    canEdit: true,
    canManageGlobal: false,
    onView: vi.fn(),
    onPatch: vi.fn().mockResolvedValue("confirmed"),
    onCreate: vi.fn().mockResolvedValue("confirmed"),
    onDelete: vi.fn().mockResolvedValue("confirmed"),
    onInduce: vi.fn(),
    ...overrides,
  };
  const view = render(<SchemaManager {...props} />);
  return { ...props, ...view };
}

/** 只读定义里某一行的值。标题在栏头也出现一次，所以按 dt 定位它自己那一格。 */
function definitionOf(term: string) {
  return within(screen.getByLabelText("类型定义")).getByText(term).parentElement;
}

/** 清单行的可及名字里同时有类型标识、显示名与元信息，用标识做锚点最稳。 */
function row(objectType: string) {
  return screen.getByRole("button", { name: new RegExp(objectType) });
}

function openEditor(objectType: string) {
  fireEvent.click(row(objectType));
  fireEvent.click(screen.getByRole("button", { name: /^(编辑|继续编辑)$/ }));
}

// --- 清单：三组各自成段，右栏默认空着 ----------------------------------------

test("清单按候选、生效中、已停用分组，右栏在选中之前只给指引", () => {
  mount({
    schemas: [
      item(),
      item({ object_type: "process_window", label: "工艺窗口", inherited: false, scope: "notebook" }),
      item({ object_type: "old_type", status: "disabled", inherited: false, scope: "notebook" }),
      item({ object_type: "failure_mode", status: "proposed", inherited: false, scope: "notebook", rationale: "反复出现的失效归因" }),
    ],
  });
  const list = screen.getByLabelText("类型清单");
  for (const [title, count] of [["待批准的候选", "1"], ["生效中", "2"], ["已停用", "1"]] as const) {
    expect(within(list).getByText(title).parentElement).toHaveTextContent(count);
  }
  expect(screen.getByText("从左边选一个类型，这里显示它的完整定义。")).toBeInTheDocument();
  // 选中之前右栏没有任何写控件——「编辑」不该在没有对象的时候先亮出来。
  expect(screen.queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
});

test("选中一个类型先给只读定义，按下编辑才换成表单", () => {
  mount();
  fireEvent.click(row("claim"));
  const detail = screen.getByLabelText("类型定义");
  expect(within(detail).getByText("全局继承")).toBeInTheDocument();
  expect(within(detail).getByText("已启用")).toBeInTheDocument();
  expect(within(detail).getByText("可验证结论")).toBeInTheDocument();
  expect(screen.queryByLabelText("显示名")).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  expect(screen.getByLabelText("显示名")).toHaveValue("结论");
});

// --- 写入：copy-on-write 的措辞与载荷 ----------------------------------------

test("当前笔记本把继承类型标为全局继承，保存走当前笔记本的写回调", () => {
  const callbacks = mount();
  fireEvent.click(row("claim"));
  expect(screen.getByText("全局继承")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "停用并建立覆盖" })).toBeInTheDocument();

  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  fireEvent.change(screen.getByLabelText("显示名"), { target: { value: "项目结论" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并建立覆盖" }));
  expect(callbacks.onPatch).toHaveBeenCalledWith("claim", expect.objectContaining({ label: "项目结论" }));
});

test("字段替换可以同步修改主字段、列表字段和复数名称", async () => {
  const callbacks = mount();
  openEditor("claim");
  fireEvent.change(screen.getByLabelText("字段（逗号分隔，按顺序）"), { target: { value: "title, evidence" } });
  fireEvent.change(screen.getByLabelText("主字段"), { target: { value: "title" } });
  fireEvent.change(screen.getByLabelText("列表字段（逗号分隔，可留空）"), { target: { value: "evidence" } });
  fireEvent.change(screen.getByLabelText("复数名称"), { target: { value: "findings" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并建立覆盖" }));
  await waitFor(() => expect(callbacks.onPatch).toHaveBeenCalledWith("claim", expect.objectContaining({
    fields: ["title", "evidence"],
    primary: "title",
    list_fields: ["evidence"],
    plural: "findings",
  })));
});

test("保存成功后退回只读态并显示写回来的那一版", async () => {
  const saved = item({ label: "项目结论" });
  const callbacks = mount();
  openEditor("claim");
  fireEvent.change(screen.getByLabelText("显示名"), { target: { value: "项目结论" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并建立覆盖" }));
  await waitFor(() => expect(callbacks.onPatch).toHaveBeenCalled());
  // 服务端回来的新一版由上层重新下发；面板应当已经回到只读态在显示它。
  callbacks.rerender(
    <SchemaManager
      schemas={[saved]}
      busy={false}
      view="notebook"
      canEdit
      canManageGlobal={false}
      onView={callbacks.onView}
      onPatch={callbacks.onPatch}
      onCreate={callbacks.onCreate}
      onDelete={callbacks.onDelete}
      onInduce={callbacks.onInduce}
    />,
  );
  await waitFor(() => expect(screen.queryByLabelText("显示名")).not.toBeInTheDocument());
  expect(definitionOf("显示名")).toHaveTextContent("项目结论");
});

test("保存没拿到回执时留在编辑态、保留输入并说明原因", async () => {
  const callbacks = mount({ onPatch: vi.fn().mockResolvedValue("failed") });
  openEditor("claim");
  fireEvent.change(screen.getByLabelText("显示名"), { target: { value: "项目结论" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并建立覆盖" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("当前输入已保留"));
  expect(screen.getByLabelText("显示名")).toHaveValue("项目结论");
  expect(callbacks.onPatch).toHaveBeenCalledOnce();
});

// --- 草稿：切到别的类型不该把打进去的字丢掉 ----------------------------------

test("编辑中切到另一个类型，草稿留在原处并在清单上标记未保存", () => {
  mount({ schemas: [item(), item({ object_type: "process_window", label: "工艺窗口", inherited: false, scope: "notebook" })] });
  openEditor("claim");
  fireEvent.change(screen.getByLabelText("显示名"), { target: { value: "项目结论" } });

  fireEvent.click(row("process_window"));
  expect(within(screen.getByLabelText("类型定义")).getByText("process_window")).toBeInTheDocument();
  expect(within(row("claim")).getByTitle("有还没保存的修改")).toBeInTheDocument();

  // 切回来落在只读态：右栏说清草稿还在，「继续编辑」把它原样接回来。
  fireEvent.click(row("claim"));
  expect(screen.queryByLabelText("显示名")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("这个类型有一份还没保存的修改。");
  fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
  expect(screen.getByLabelText("显示名")).toHaveValue("项目结论");

  fireEvent.click(screen.getByRole("button", { name: "放弃修改" }));
  expect(screen.queryByLabelText("显示名")).not.toBeInTheDocument();
  expect(within(row("claim")).queryByTitle("有还没保存的修改")).not.toBeInTheDocument();
});

// --- 新增：入口、护栏与失败保留 ----------------------------------------------

test("新增校验失败或请求失败时显示原因并保留输入", async () => {
  const onCreate = vi.fn().mockResolvedValue("failed");
  mount({ schemas: [], onCreate });
  fireEvent.click(screen.getByRole("button", { name: "新增类型" }));
  const typeInput = screen.getByLabelText("类型标识（snake_case）");
  const fieldsInput = screen.getByLabelText("字段（逗号分隔，按顺序）");
  fireEvent.change(typeInput, { target: { value: "Bad-Type" } });
  fireEvent.change(fieldsInput, { target: { value: "title" } });
  fireEvent.click(screen.getByRole("button", { name: "创建类型" }));
  expect(screen.getByRole("alert")).toHaveTextContent("类型标识须以小写字母开头");
  expect(onCreate).not.toHaveBeenCalled();

  fireEvent.change(typeInput, { target: { value: "lab_recipe" } });
  fireEvent.click(screen.getByRole("button", { name: "创建类型" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("当前输入已保留"));
  expect(typeInput).toHaveValue("lab_recipe");
  expect(fieldsInput).toHaveValue("title");
  expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
    object_type: "lab_recipe",
    plural: "lab_recipes",
    primary: "title",
    list_fields: [],
  }));
});

test("新增表单在请求前执行名称与文本长度护栏", () => {
  const onCreate = vi.fn().mockResolvedValue("confirmed");
  mount({ schemas: [], onCreate });
  fireEvent.click(screen.getByRole("button", { name: "新增类型" }));
  fireEvent.change(screen.getByLabelText("类型标识（snake_case）"), { target: { value: `a${"b".repeat(80)}` } });
  fireEvent.change(screen.getByLabelText("字段（逗号分隔，按顺序）"), { target: { value: "title" } });
  fireEvent.click(screen.getByRole("button", { name: "创建类型" }));
  expect(screen.getByRole("alert")).toHaveTextContent("类型标识不能超过 80 个字符");
  expect(onCreate).not.toHaveBeenCalled();
});

test("新增拿到回执后清空表单，下一次打开是干净的", async () => {
  const onCreate = vi.fn().mockResolvedValue("confirmed");
  mount({ schemas: [], onCreate });
  fireEvent.click(screen.getByRole("button", { name: "新增类型" }));
  fireEvent.change(screen.getByLabelText("类型标识（snake_case）"), { target: { value: "lab_recipe" } });
  fireEvent.change(screen.getByLabelText("字段（逗号分隔，按顺序）"), { target: { value: "title" } });
  fireEvent.click(screen.getByRole("button", { name: "创建类型" }));
  await waitFor(() => expect(onCreate).toHaveBeenCalledOnce());
  fireEvent.click(screen.getByRole("button", { name: "新增类型" }));
  expect(screen.getByLabelText("类型标识（snake_case）")).toHaveValue("");
});

// --- 删除：覆盖恢复继承，自建真的删掉 ----------------------------------------

test("当前笔记本覆盖提供恢复全局，而本库自建条目提供删除", () => {
  const callbacks = mount({ schemas: [item({ inherited: false, overrides_global: true, scope: "notebook" })] });
  fireEvent.click(row("claim"));
  expect(screen.getByText("当前笔记本覆盖")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "恢复全局" }));
  expect(callbacks.onDelete).toHaveBeenCalledWith("claim");

  const selfOwned = mount({ schemas: [item({ object_type: "project_note", inherited: false, overrides_global: false, scope: "notebook" })] });
  fireEvent.click(row("project_note"));
  expect(screen.getByText("当前笔记本自建")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "删除" }));
  expect(selfOwned.onDelete).toHaveBeenCalledWith("project_note");
});

// --- 候选：理由与批准/拒绝都在右栏，不混进生效类型 ---------------------------

test("候选类型在右栏给出归纳理由与批准、拒绝", () => {
  const callbacks = mount({
    schemas: [item({ object_type: "failure_mode", status: "proposed", inherited: false, scope: "notebook", rationale: "反复出现的失效归因" })],
  });
  fireEvent.click(row("failure_mode"));
  const detail = screen.getByLabelText("类型定义");
  expect(within(detail).getByText("归纳候选")).toBeInTheDocument();
  expect(within(detail).getByText("待批准")).toBeInTheDocument();
  expect(within(detail).getByText("反复出现的失效归因")).toBeInTheDocument();
  // 候选还没被采纳，右栏不给「编辑」——先批准，再当作生效类型来改。
  expect(within(detail).queryByRole("button", { name: "编辑" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "批准并启用" }));
  expect(callbacks.onPatch).toHaveBeenCalledWith("failure_mode", { status: "active" });
});

test("同名候选与继承行是两条可分别选中的行，候选的审批控件够得着", async () => {
  // 后端刻意两行都返回（候选在批准前不遮蔽继承类型），并把 active 排在 proposed 前面。
  // 只按 object_type 认行时，find 必然命中继承那一行：两行同时高亮，而候选的归纳理由
  // 与批准/拒绝按钮永远点不到——审批这条路整条断掉。
  const callbacks = mount({
    schemas: [
      item({ object_type: "failure_mode" }),
      item({ object_type: "failure_mode", status: "proposed", inherited: false, scope: "notebook", rationale: "反复出现的失效归因" }),
    ],
  });
  const list = screen.getByLabelText("类型清单");
  const rows = within(list).getAllByRole("button", { name: /failure_mode/ });
  expect(rows).toHaveLength(2);
  // 按徽章认行，不按下标：分组顺序是版式取舍，不该被断言钉死。
  const proposalRow = rows.find((one) => within(one).queryByText("候选"))!;
  const inheritedRow = rows.find((one) => within(one).queryByText("继承"))!;

  fireEvent.click(proposalRow);
  const detail = screen.getByLabelText("类型定义");
  expect(within(detail).getByText("反复出现的失效归因")).toBeInTheDocument();
  expect(rows.filter((one) => one.getAttribute("aria-current") === "true")).toEqual([proposalRow]);

  fireEvent.click(inheritedRow);
  expect(within(detail).getByText("全局继承")).toBeInTheDocument();
  expect(rows.filter((one) => one.getAttribute("aria-current") === "true")).toEqual([inheritedRow]);

  fireEvent.click(proposalRow);
  fireEvent.click(screen.getByRole("button", { name: "批准并启用" }));
  await waitFor(() => expect(callbacks.onPatch).toHaveBeenCalledWith("failure_mode", { status: "active" }));
});

test("批准成功后选中项跟着换到生效那一行，右栏不会空掉", async () => {
  const callbacks = mount({
    schemas: [item({ object_type: "failure_mode", status: "proposed", inherited: false, scope: "notebook", rationale: "反复出现的失效归因" })],
  });
  fireEvent.click(row("failure_mode"));
  fireEvent.click(screen.getByRole("button", { name: "批准并启用" }));
  await waitFor(() => expect(callbacks.onPatch).toHaveBeenCalled());
  callbacks.rerender(
    <SchemaManager
      schemas={[item({ object_type: "failure_mode", inherited: false, overrides_global: false, scope: "notebook" })]}
      busy={false}
      view="notebook"
      canEdit
      canManageGlobal={false}
      onView={callbacks.onView}
      onPatch={callbacks.onPatch}
      onCreate={callbacks.onCreate}
      onDelete={callbacks.onDelete}
      onInduce={callbacks.onInduce}
    />,
  );
  await waitFor(() => expect(screen.getByRole("button", { name: "编辑" })).toBeInTheDocument());
  expect(screen.queryByText("从左边选一个类型，这里显示它的完整定义。")).not.toBeInTheDocument();
});

test("状态类写动作拿不到回执时在按钮紧邻处说明原因", async () => {
  mount({ onPatch: vi.fn().mockResolvedValue("failed"), schemas: [item({ inherited: false, scope: "notebook" })] });
  fireEvent.click(row("claim"));
  fireEvent.click(screen.getByRole("button", { name: "停用" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("切换启用状态失败"));
});

// --- 在飞期间导航：结果不能落到用户此刻看的那一行上 ---------------------------

test("保存在飞时切到别的类型，完成回调既不把人拽回去，也不把失败挂到别人身上", async () => {
  // 清单行**刻意**在写入在飞期间仍然可点（只读浏览不该被一次写入冻住），代价就是
  // 完成回调会晚于用户的下一次导航到达。不核对身份就会做两件都错的事：把人从他刚
  // 点开的类型拽回原来那一行，以及把失败提示挂在另一个类型旁边。
  let settle: (outcome: SchemaWriteOutcome) => void = () => {};
  const onPatch = vi.fn().mockImplementation(() => new Promise<SchemaWriteOutcome>((resolve) => { settle = resolve; }));
  mount({
    onPatch,
    schemas: [item(), item({ object_type: "process_window", label: "工艺窗口", inherited: false, scope: "notebook" })],
  });
  openEditor("claim");
  fireEvent.change(screen.getByLabelText("显示名"), { target: { value: "项目结论" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并建立覆盖" }));
  await waitFor(() => expect(onPatch).toHaveBeenCalled());

  fireEvent.click(row("process_window"));
  await act(async () => { settle("failed"); });

  // 人留在自己刚点开的那一行，失败提示没有跟过来。
  expect(within(screen.getByLabelText("类型定义")).getByText("process_window")).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  // 而那份没保存成功的草稿仍在原处等着，圆点还亮着。
  expect(within(row("claim")).getByTitle("有还没保存的修改")).toBeInTheDocument();
});

test("保存在飞时切走再切回来，成功那一版仍然是只读态且草稿已清", async () => {
  let settle: (outcome: SchemaWriteOutcome) => void = () => {};
  const onPatch = vi.fn().mockImplementation(() => new Promise<SchemaWriteOutcome>((resolve) => { settle = resolve; }));
  mount({
    onPatch,
    schemas: [item(), item({ object_type: "process_window", label: "工艺窗口", inherited: false, scope: "notebook" })],
  });
  openEditor("claim");
  fireEvent.change(screen.getByLabelText("显示名"), { target: { value: "项目结论" } });
  fireEvent.click(screen.getByRole("button", { name: "保存并建立覆盖" }));
  await waitFor(() => expect(onPatch).toHaveBeenCalled());
  fireEvent.click(row("process_window"));
  await act(async () => { settle("confirmed"); });

  // 落库的草稿无条件丢掉——它已经不是「用户还没提交的输入」了。
  expect(within(row("claim")).queryByTitle("有还没保存的修改")).not.toBeInTheDocument();
  fireEvent.click(row("claim"));
  expect(screen.queryByLabelText("显示名")).not.toBeInTheDocument();
});

test("写已提交但没能确认时，不说失败、也不劝重试", async () => {
  // 写落库了、只是清单没读回来。照着说「失败，请重试」会把用户引去撞重名 409——
  // 而第一次其实成功了；删除同理会留下一行删不掉的陈旧条目（codex #614 R4 P2）。
  const onCreate = vi.fn().mockResolvedValue("unconfirmed" satisfies SchemaWriteOutcome);
  mount({ schemas: [], onCreate });
  fireEvent.click(screen.getByRole("button", { name: "新增类型" }));
  fireEvent.change(screen.getByLabelText("类型标识（snake_case）"), { target: { value: "lab_recipe" } });
  fireEvent.change(screen.getByLabelText("字段（逗号分隔，按顺序）"), { target: { value: "title" } });
  fireEvent.click(screen.getByRole("button", { name: "创建类型" }));

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("改动可能已经生效");
  expect(alert).toHaveTextContent("不要直接重试");
  expect(alert).not.toHaveTextContent("新增失败");
  // 表单原样留着，用户确认之后自己决定怎么办。
  expect(screen.getByLabelText("类型标识（snake_case）")).toHaveValue("lab_recipe");
});

// --- 权限：只读成员看得到同一份定义，但没有任何写控件 -------------------------

test("只读成员可以查看，但不会渲染任何写入控件", () => {
  mount({ canEdit: false, schemas: [item({ can_edit: false })] });
  expect(screen.getByText("你拥有只读权限，可以查看当前笔记本实际采用的类型。")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "从当前笔记本归纳候选类型" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "新增类型" })).not.toBeInTheDocument();

  fireEvent.click(row("claim"));
  expect(within(screen.getByLabelText("类型定义")).getByText("可验证结论")).toBeInTheDocument();
  for (const name of ["编辑", "停用并建立覆盖", "删除", "恢复全局"]) {
    expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
  }
});

// --- 作用范围：管理员的第二份注册表 ------------------------------------------

test("管理员可切换全局基线，并看到它影响未覆盖笔记本的说明", () => {
  const callbacks = mount({ canManageGlobal: true, view: "global", canEdit: true, schemas: [item({ scope: "global", inherited: false, source: "custom" })] });
  expect(screen.getByText("全局基线会影响所有尚未建立当前笔记本覆盖的笔记本。")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "全局基线" })).toHaveAttribute("aria-pressed", "true");
  // 全局视图里没有「归纳候选」——候选只从当前笔记本的内容里来。
  expect(screen.queryByRole("button", { name: "从当前笔记本归纳候选类型" })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "当前笔记本" }));
  expect(callbacks.onView).toHaveBeenCalledWith("notebook");
});

test("已经停在某个范围时再点同一颗页签不会重复拉清单", () => {
  const callbacks = mount({ canManageGlobal: true });
  fireEvent.click(screen.getByRole("button", { name: "当前笔记本" }));
  expect(callbacks.onView).not.toHaveBeenCalled();
});

test("切换作用范围会清掉选中项与草稿，不把另一份注册表的状态带过去", () => {
  const callbacks = mount({ canManageGlobal: true });
  openEditor("claim");
  fireEvent.change(screen.getByLabelText("显示名"), { target: { value: "项目结论" } });

  callbacks.rerender(
    <SchemaManager
      schemas={[item({ scope: "global", inherited: false, source: "custom" })]}
      busy={false}
      view="global"
      canEdit
      canManageGlobal
      onView={callbacks.onView}
      onPatch={callbacks.onPatch}
      onCreate={callbacks.onCreate}
      onDelete={callbacks.onDelete}
      onInduce={callbacks.onInduce}
    />,
  );
  expect(screen.getByText("从左边选一个类型，这里显示它的完整定义。")).toBeInTheDocument();
  fireEvent.click(row("claim"));
  fireEvent.click(screen.getByRole("button", { name: "编辑" }));
  expect(screen.getByLabelText("显示名")).toHaveValue("结论");
});

// --- 忙碌位：写动作在飞时整排写控件不可点 -------------------------------------

test("面板忙碌时写控件全部禁用，只读浏览不受影响", () => {
  mount({ busy: true, schemas: [item({ inherited: false, scope: "notebook" })] });
  expect(screen.getByRole("button", { name: "新增类型" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "从当前笔记本归纳候选类型" })).toBeDisabled();
  fireEvent.click(row("claim"));
  expect(within(screen.getByLabelText("类型定义")).getByText("可验证结论")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "编辑" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "删除" })).toBeDisabled();
});

test("加载中不渲染工作区", () => {
  mount({ schemas: null });
  expect(screen.getByText("加载中…")).toBeInTheDocument();
  expect(screen.queryByLabelText("类型清单")).not.toBeInTheDocument();
});
