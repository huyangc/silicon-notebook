import test from "node:test";
import assert from "node:assert/strict";

import {
  deleteActionLabel,
  draftFromSchema,
  draftIsDirty,
  groupSchemas,
  managedRowKey,
  placementBadge,
  placementLabel,
  removable,
  resolveCreatePrimary,
  saveActionLabel,
  schemaRowKey,
  statusLabel,
  statusTone,
  toggleActionLabel,
  validateCreateDraft,
  validateDraft,
} from "../../app/schema-manager-model.ts";

function schema(overrides = {}) {
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

// --- 分组：清单三段的成员判据 -------------------------------------------------

test("三组把候选、生效中、已停用分开，组内保持服务端顺序", () => {
  const rows = [
    schema({ object_type: "concept" }),
    schema({ object_type: "draft_type", status: "proposed" }),
    schema({ object_type: "old_type", status: "disabled" }),
    schema({ object_type: "claim" }),
  ];
  const groups = groupSchemas(rows);
  assert.deepEqual(groups.map((group) => group.key), ["proposed", "active", "disabled"]);
  assert.deepEqual(groups[0].rows.map((row) => row.object_type), ["draft_type"]);
  assert.deepEqual(groups[1].rows.map((row) => row.object_type), ["concept", "claim"]);
  assert.deepEqual(groups[2].rows.map((row) => row.object_type), ["old_type"]);
});

test("未知状态归入已停用，而不是凭空消失在三组之外", () => {
  const groups = groupSchemas([schema({ status: "retired" })]);
  assert.deepEqual(groups[2].rows.map((row) => row.object_type), ["claim"]);
});

// --- 行的身份：同名候选与继承行必须分得开 -------------------------------------

test("同一个类型的继承行与还没批准的同名候选是两行，身份不同", () => {
  // 后端刻意两行都返回：候选在批准前不遮蔽继承类型，且 active 排在 proposed 前面。
  // 只按类型名认行，候选那一行永远选不中，审批那条路直接断掉。
  const inherited = schema({ object_type: "failure_mode" });
  const proposal = schema({ object_type: "failure_mode", status: "proposed", inherited: false });
  assert.notEqual(schemaRowKey(inherited), schemaRowKey(proposal));
  assert.equal(schemaRowKey(inherited), managedRowKey("failure_mode"));
});

test("启用与停用是同一行的两个状态，身份不随之变化", () => {
  assert.equal(
    schemaRowKey(schema({ status: "active" })),
    schemaRowKey(schema({ status: "disabled" })),
  );
});

// --- 草稿：脏判据同时喂给保存按钮与清单上那颗圆点 -----------------------------

test("草稿与服务端定义逐字相同就不算脏", () => {
  const row = schema();
  assert.equal(draftIsDirty(row, draftFromSchema(row)), false);
});

test("六个可编辑字段里任意一个不同都算脏", () => {
  const row = schema();
  const base = draftFromSchema(row);
  for (const patch of [
    { label: "论断" },
    { plural: "findings" },
    { fieldsText: "statement" },
    { primary: "condition" },
    { listFieldsText: "condition" },
    { description: "" },
  ]) {
    assert.equal(draftIsDirty(row, { ...base, ...patch }), true, JSON.stringify(patch));
  }
});

// --- 标签：从哪来 / 写下去落到哪 ---------------------------------------------

test("候选先判，不会被说成当前笔记本自建", () => {
  const proposal = schema({ status: "proposed", inherited: false, overrides_global: false });
  assert.equal(placementLabel(proposal, "notebook"), "归纳候选");
  assert.equal(placementBadge(proposal, "notebook"), "候选");
});

test("当前笔记本视图区分继承、覆盖与自建", () => {
  assert.equal(placementLabel(schema(), "notebook"), "全局继承");
  assert.equal(placementLabel(schema({ inherited: false, overrides_global: true }), "notebook"), "当前笔记本覆盖");
  assert.equal(placementLabel(schema({ inherited: false, overrides_global: false }), "notebook"), "当前笔记本自建");
});

test("全局视图区分内置与自定义基线", () => {
  assert.equal(placementLabel(schema({ source: "builtin" }), "global"), "内置类型");
  assert.equal(placementLabel(schema({ source: "custom" }), "global"), "全局基线");
});

test("状态文案与配色分档取值一一对应", () => {
  assert.deepEqual(
    ["active", "proposed", "disabled"].map((status) => [statusLabel(status), statusTone(status)]),
    [["已启用", "is-on"], ["待批准", "is-pending"], ["已停用", "is-off"]],
  );
});

// --- 动作标签：copy-on-write 必须提前说清 ------------------------------------

test("改继承类型的按钮说明它会建立覆盖", () => {
  assert.equal(saveActionLabel(schema(), "notebook"), "保存并建立覆盖");
  assert.equal(toggleActionLabel(schema(), "notebook"), "停用并建立覆盖");
  assert.equal(saveActionLabel(schema({ inherited: false }), "notebook"), "保存");
  assert.equal(toggleActionLabel(schema({ inherited: false, status: "disabled" }), "notebook"), "启用");
  assert.equal(saveActionLabel(schema(), "global"), "保存");
});

test("删掉覆盖等于恢复全局；继承项不可删，内置基线不可删", () => {
  assert.equal(deleteActionLabel(schema({ inherited: false, overrides_global: true }), "notebook"), "恢复全局");
  assert.equal(deleteActionLabel(schema({ inherited: false }), "notebook"), "删除");
  assert.equal(removable(schema(), "notebook"), false);
  assert.equal(removable(schema({ inherited: false }), "notebook"), true);
  assert.equal(removable(schema({ source: "builtin" }), "global"), false);
  assert.equal(removable(schema({ source: "custom" }), "global"), true);
});

// --- 校验：与后端同一组护栏，浏览器先挡一遍 -----------------------------------

function draft(overrides = {}) {
  return {
    label: "工艺窗口",
    plural: "process_windows",
    fieldsText: "title, condition",
    primary: "title",
    listFieldsText: "",
    description: "",
    ...overrides,
  };
}

test("编辑校验覆盖标识、字段集合与主字段归属", () => {
  assert.equal(validateDraft("process_window", draft()), "");
  assert.match(validateDraft("process_window", draft({ fieldsText: "" })), /至少填写一个字段/);
  assert.match(validateDraft("process_window", draft({ fieldsText: "title, Title" })), /小写字母/);
  assert.match(validateDraft("process_window", draft({ fieldsText: "title, title" })), /字段不能重复/);
  assert.match(validateDraft("process_window", draft({ primary: "missing" })), /主字段必须包含/);
  assert.match(validateDraft("process_window", draft({ listFieldsText: "missing" })), /列表字段必须包含/);
  assert.match(validateDraft("process_window", draft({ description: "x".repeat(2001) })), /2000/);
});

test("新增校验先要标识，主字段留空时默认取第一个字段", () => {
  assert.match(validateCreateDraft({ ...draft(), objectType: "  " }), /请填写类型标识/);
  assert.match(validateCreateDraft({ ...draft(), objectType: "Bad-Type" }), /类型标识须以小写字母开头/);
  assert.match(validateCreateDraft({ ...draft(), objectType: `a${"b".repeat(80)}` }), /不能超过 80 个字符/);
  assert.equal(validateCreateDraft({ ...draft({ primary: "" }), objectType: "process_window" }), "");
  assert.equal(resolveCreatePrimary({ ...draft({ primary: "" }), objectType: "process_window" }), "title");
  assert.equal(resolveCreatePrimary({ ...draft({ primary: "condition" }), objectType: "process_window" }), "condition");
});
