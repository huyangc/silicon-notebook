// knowhow-manage.tsx / knowhow-import.tsx（改造后）的纯逻辑单测。两个 .tsx 均含
// JSX，Node 原生 TS 类型剥离不支持 .tsx（仅 .ts/.mts/.cts），因此可测纯逻辑
// （建表向导表头状态机 / 内容类型选项与提示语 / 行标题列选择器文案 / 导入向导
// 猜测预选映射 / 管理动作 payload）一律抽到 knowhow-manage-logic.ts（无 JSX）
// 导出，本文件直接 import 该文件测试（镜像 knowhow-panel.test.mjs 对
// knowhow-panel-logic.ts 的拆分方式）。
import test from "node:test";
import assert from "node:assert/strict";

import {
  KIND_HINTS,
  KIND_OPTIONS,
  ANCHOR_SELECTOR_LABEL,
  ANCHOR_NONE_LABEL,
  ANCHOR_SET_HINT,
  ANCHOR_NONE_HINT,
  anchorHint,
  initialWizardState,
  addWizardColumn,
  updateWizardColumn,
  removeWizardColumn,
  moveWizardColumn,
  setWizardAnchor,
  createValidationError,
  canSubmitCreate,
  assembleCreatePayload,
  kindFromGuess,
  deriveImportSelection,
  assembleImportColumnKinds,
  COLUMN_DELETE_CONFIRM,
  ROW_DELETE_CONFIRM,
  HISTORY_PRUNE_CONFIRM,
  DEFAULT_HISTORY_PRUNE_DAYS,
  parseHistoryPruneDays,
  tableMetaPatch,
  hasMetaChanges,
} from "./knowhow-manage-logic.ts";
import { KIND_LABELS } from "./knowhow-model.ts";

// --- KIND_OPTIONS / KIND_HINTS（规格①：三个内容类型 + 各一行提示语）---------------

test("KIND_OPTIONS: 恰好三项，顺序与 KIND_LABELS 一致（方法步骤/工具/事物/普通）", () => {
  assert.deepStrictEqual(
    KIND_OPTIONS.map((option) => option.value),
    ["procedure", "entity", "attribute"],
  );
});

test("KIND_OPTIONS: 不含 anchor（行标题不进列内容类型下拉，走表级选择器）", () => {
  assert.ok(KIND_OPTIONS.every((option) => option.value !== "anchor"));
});

test("KIND_OPTIONS: label 逐字取自 KIND_LABELS", () => {
  for (const option of KIND_OPTIONS) {
    assert.strictEqual(option.label, KIND_LABELS[option.value]);
  }
});

test("KIND_HINTS: 三条提示语与规格①逐字一致", () => {
  assert.deepStrictEqual(KIND_HINTS, {
    procedure: "写做法/流程的列，自动识别有序步骤",
    entity: "列出的名称自动归并：工具、命令、文档等",
    attribute: "仅作为内容参与检索",
  });
});

test("KIND_OPTIONS: hint 逐字取自 KIND_HINTS（下拉 title 与图例共用同一来源）", () => {
  for (const option of KIND_OPTIONS) {
    assert.strictEqual(option.hint, KIND_HINTS[option.value]);
  }
});

// --- 行标题列选择器文案（规格①逐字）-----------------------------------------------

test("行标题列选择器：标签与「不设置」选项文案", () => {
  assert.strictEqual(ANCHOR_SELECTOR_LABEL, "行标题列");
  assert.strictEqual(ANCHOR_NONE_LABEL, "不设置");
});

test("ANCHOR_SET_HINT: 已选行标题列的提示语与规格①逐字一致", () => {
  // 规格原文用「节点」（图论内部词），界面词改用「知识对象」；不能用「概念」——
  // knowhow 的 anchor 分组在界面上已经占了「概念」这个词，且每行进图谱后的
  // object_type 是自定义列名，不是 Concept 这一种类型。
  assert.strictEqual(ANCHOR_SET_HINT, "用作每行的标题；设置后每行作为一个知识对象进入知识图谱，对象名取自该列");
});

test("ANCHOR_NONE_HINT: 未选行标题列的提示语与规格①逐字一致", () => {
  assert.strictEqual(ANCHOR_NONE_HINT, "未选行标题列：本表仅参与问答检索，不加入知识图谱");
});

test("anchorHint: null → 未选提示；下标/列 id → 已选提示", () => {
  assert.strictEqual(anchorHint(null), ANCHOR_NONE_HINT);
  assert.strictEqual(anchorHint(0), ANCHOR_SET_HINT);
  assert.strictEqual(anchorHint(2), ANCHOR_SET_HINT);
  assert.strictEqual(anchorHint("col-9"), ANCHOR_SET_HINT);
});

// --- 建表向导：表头状态机 ----------------------------------------------------------

test("initialWizardState: 一列空名（默认普通）、未设行标题", () => {
  assert.deepStrictEqual(initialWizardState(), {
    columns: [{ name: "", kind: "attribute" }],
    anchorIndex: null,
  });
});

test("addWizardColumn: 末尾追加空名普通列，不动行标题选择", () => {
  const state = { columns: [{ name: "A", kind: "procedure" }], anchorIndex: 0 };
  assert.deepStrictEqual(addWizardColumn(state), {
    columns: [
      { name: "A", kind: "procedure" },
      { name: "", kind: "attribute" },
    ],
    anchorIndex: 0,
  });
});

test("updateWizardColumn: 按下标改名/改类型，不影响其他列", () => {
  const state = {
    columns: [
      { name: "A", kind: "attribute" },
      { name: "B", kind: "attribute" },
    ],
    anchorIndex: null,
  };
  const renamed = updateWizardColumn(state, 0, { name: "违例概念" });
  assert.strictEqual(renamed.columns[0].name, "违例概念");
  assert.strictEqual(renamed.columns[1].name, "B");
  const rekinded = updateWizardColumn(renamed, 1, { kind: "entity" });
  assert.strictEqual(rekinded.columns[1].kind, "entity");
  assert.strictEqual(rekinded.columns[0].kind, "attribute");
});

test("removeWizardColumn: 删除行标题列本身 → anchorIndex 清空", () => {
  const state = {
    columns: [
      { name: "A", kind: "attribute" },
      { name: "B", kind: "procedure" },
    ],
    anchorIndex: 0,
  };
  const out = removeWizardColumn(state, 0);
  assert.deepStrictEqual(out.columns, [{ name: "B", kind: "procedure" }]);
  assert.strictEqual(out.anchorIndex, null);
});

test("removeWizardColumn: 删除行标题列之前的列 → anchorIndex 左移补偿", () => {
  const state = {
    columns: [
      { name: "A", kind: "attribute" },
      { name: "B", kind: "attribute" },
      { name: "C", kind: "attribute" },
    ],
    anchorIndex: 2,
  };
  const out = removeWizardColumn(state, 0);
  assert.strictEqual(out.anchorIndex, 1);
  assert.deepStrictEqual(out.columns.map((column) => column.name), ["B", "C"]);
});

test("removeWizardColumn: 删除行标题列之后的列 → anchorIndex 不变", () => {
  const state = {
    columns: [
      { name: "A", kind: "attribute" },
      { name: "B", kind: "attribute" },
    ],
    anchorIndex: 0,
  };
  assert.strictEqual(removeWizardColumn(state, 1).anchorIndex, 0);
});

test("moveWizardColumn: 与相邻列交换；被移动的行标题列下标跟着走", () => {
  const state = {
    columns: [
      { name: "A", kind: "attribute" },
      { name: "B", kind: "attribute" },
    ],
    anchorIndex: 0,
  };
  const out = moveWizardColumn(state, 0, 1);
  assert.deepStrictEqual(out.columns.map((column) => column.name), ["B", "A"]);
  assert.strictEqual(out.anchorIndex, 1);
});

test("moveWizardColumn: 交换对象是行标题列时其下标反向补偿", () => {
  const state = {
    columns: [
      { name: "A", kind: "attribute" },
      { name: "B", kind: "attribute" },
    ],
    anchorIndex: 1,
  };
  const out = moveWizardColumn(state, 0, 1);
  assert.deepStrictEqual(out.columns.map((column) => column.name), ["B", "A"]);
  assert.strictEqual(out.anchorIndex, 0);
});

test("moveWizardColumn: 越界移动原样返回（首列上移/末列下移）", () => {
  const state = {
    columns: [
      { name: "A", kind: "attribute" },
      { name: "B", kind: "attribute" },
    ],
    anchorIndex: null,
  };
  assert.strictEqual(moveWizardColumn(state, 0, -1), state);
  assert.strictEqual(moveWizardColumn(state, 1, 1), state);
});

test("setWizardAnchor: 设置/清空行标题列下标", () => {
  const state = { columns: [{ name: "A", kind: "attribute" }], anchorIndex: null };
  assert.strictEqual(setWizardAnchor(state, 0).anchorIndex, 0);
  assert.strictEqual(setWizardAnchor({ ...state, anchorIndex: 0 }, null).anchorIndex, null);
});

// --- createValidationError / canSubmitCreate ---------------------------------------

test("createValidationError: 标题为空/纯空白 → 「表标题不能为空」（与后端文案一致）", () => {
  assert.strictEqual(createValidationError("", [{ name: "A", kind: "attribute" }]), "表标题不能为空");
  assert.strictEqual(createValidationError("   ", [{ name: "A", kind: "attribute" }]), "表标题不能为空");
});

test("createValidationError: 零列 → 「至少需要一列」", () => {
  assert.strictEqual(createValidationError("表", []), "至少需要一列");
});

test("createValidationError: 任一列名为空/纯空白 → 「列名不能为空」", () => {
  assert.strictEqual(
    createValidationError("表", [
      { name: "A", kind: "attribute" },
      { name: "  ", kind: "procedure" },
    ]),
    "列名不能为空",
  );
});

test("createValidationError: 列名重复（trim 后比较）→ 提示含重复名", () => {
  const message = createValidationError("表", [
    { name: "工具", kind: "entity" },
    { name: " 工具 ", kind: "attribute" },
  ]);
  assert.strictEqual(message, "列名重复：工具");
});

test("createValidationError: 合法输入返回 null（行标题列不参与校验，0..1 由选择器保证）", () => {
  assert.strictEqual(
    createValidationError("时序修复", [
      { name: "违例概念", kind: "attribute" },
      { name: "修复方法", kind: "procedure" },
    ]),
    null,
  );
});

test("canSubmitCreate: 与 createValidationError 一致的布尔视图", () => {
  const columns = [{ name: "A", kind: "attribute" }];
  assert.strictEqual(canSubmitCreate("表", columns), true);
  assert.strictEqual(canSubmitCreate("", columns), false);
  assert.strictEqual(canSubmitCreate("表", []), false);
});

// --- assembleCreatePayload -----------------------------------------------------------

test("assembleCreatePayload: 标题/列名 trim，kind 原样，anchorIndex 透传", () => {
  const payload = assembleCreatePayload("  时序修复  ", {
    columns: [
      { name: " 违例概念 ", kind: "attribute" },
      { name: "修复方法", kind: "procedure" },
    ],
    anchorIndex: 0,
  });
  assert.deepStrictEqual(payload, {
    title: "时序修复",
    columns: [
      { name: "违例概念", kind: "attribute" },
      { name: "修复方法", kind: "procedure" },
    ],
    anchorIndex: 0,
  });
});

test("assembleCreatePayload: 不设行标题时 anchorIndex 为 null", () => {
  const payload = assembleCreatePayload("旅行日志", {
    columns: [{ name: "日期", kind: "attribute" }],
    anchorIndex: null,
  });
  assert.strictEqual(payload.anchorIndex, null);
});

test("assembleCreatePayload: 不修改传入的向导状态", () => {
  const state = { columns: [{ name: " A ", kind: "entity" }], anchorIndex: 0 };
  const snapshot = JSON.parse(JSON.stringify(state));
  assembleCreatePayload("表", state);
  assert.deepStrictEqual(state, snapshot);
});

// --- kindFromGuess（legacy guessed_role → 三值 kind；T3 落地后 legacy 分支失效）----

test("kindFromGuess: 新词表三值原样透传", () => {
  assert.strictEqual(kindFromGuess("procedure"), "procedure");
  assert.strictEqual(kindFromGuess("entity"), "entity");
  assert.strictEqual(kindFromGuess("attribute"), "attribute");
});

test("kindFromGuess: legacy identify/root_cause/fix → procedure", () => {
  assert.strictEqual(kindFromGuess("identify"), "procedure");
  assert.strictEqual(kindFromGuess("root_cause"), "procedure");
  assert.strictEqual(kindFromGuess("fix"), "procedure");
});

test("kindFromGuess: legacy tool → entity，plain → attribute", () => {
  assert.strictEqual(kindFromGuess("tool"), "entity");
  assert.strictEqual(kindFromGuess("plain"), "attribute");
});

test("kindFromGuess: legacy concept 是行标题猜测，内容类型兜底为普通", () => {
  assert.strictEqual(kindFromGuess("concept"), "attribute");
});

test("kindFromGuess: 未知值兜底为普通", () => {
  assert.strictEqual(kindFromGuess("whatever"), "attribute");
  assert.strictEqual(kindFromGuess(""), "attribute");
});

// --- deriveImportSelection -----------------------------------------------------------

test("deriveImportSelection: legacy 预览（无 anchorSuggestion）——concept 列变行标题预选，其余映射三值", () => {
  const preview = {
    columns: [
      { name: "违例类型", guessedRole: "concept" },
      { name: "现象识别方法", guessedRole: "identify" },
      { name: "依赖工具", guessedRole: "tool" },
      { name: "备注", guessedRole: "plain" },
    ],
    anchorSuggestion: null,
  };
  assert.deepStrictEqual(deriveImportSelection(preview), {
    kinds: ["attribute", "procedure", "entity", "attribute"],
    anchorIndex: 0,
  });
});

test("deriveImportSelection: 显式 anchorSuggestion（T3 起）优先于 legacy concept 扫描", () => {
  const preview = {
    columns: [
      { name: "备注", guessedRole: "attribute" },
      { name: "名称", guessedRole: "attribute" },
    ],
    anchorSuggestion: 1,
  };
  assert.strictEqual(deriveImportSelection(preview).anchorIndex, 1);
});

test("deriveImportSelection: 两边都没有行标题线索 → null（无首列兜底，规格①）", () => {
  const preview = {
    columns: [
      { name: "日期", guessedRole: "plain" },
      { name: "花费", guessedRole: "plain" },
    ],
    anchorSuggestion: null,
  };
  assert.strictEqual(deriveImportSelection(preview).anchorIndex, null);
});

test("deriveImportSelection: anchorSuggestion 字段缺席（undefined）时也走 legacy 回退", () => {
  const preview = {
    columns: [{ name: "概念", guessedRole: "concept" }],
  };
  assert.strictEqual(deriveImportSelection(preview).anchorIndex, 0);
});

test("deriveImportSelection: 越界建议一律丢弃", () => {
  const preview = {
    columns: [{ name: "A", guessedRole: "plain" }],
    anchorSuggestion: 5,
  };
  assert.strictEqual(deriveImportSelection(preview).anchorIndex, null);
});

// --- assembleImportColumnKinds -------------------------------------------------
// 曾叫 assembleImportColumnsWithAnchor，把行标题列编码成 role:'anchor' 塞进
// 列定义。后端从不读 role（`_columns_with_anchor` 只读 kind，缺失静默默认
// attribute；anchor 只认独立的 anchor_index），于是行标题列 + 每列内容类型被
// 一起丢弃、整张表落库成记录型表——而当时的测试断言的正是 role:'anchor'，
// 固化了这个前端自造的假契约，全绿到真机才暴露。下面锁住真实 wire。

test("assembleImportColumnKinds: 组装 {name,kind}（后端 wire 的字段名）", () => {
  const out = assembleImportColumnKinds(
    ["违例类型", "现象识别方法", "依赖工具"],
    ["attribute", "procedure", "entity"],
  );
  assert.deepStrictEqual(out, [
    { name: "违例类型", kind: "attribute" },
    { name: "现象识别方法", kind: "procedure" },
    { name: "依赖工具", kind: "entity" },
  ]);
});

test("assembleImportColumnKinds: 不产出 role 字段、也不产出 kind='anchor'（后端会静默忽略/拒绝）", () => {
  const out = assembleImportColumnKinds(["日期", "花费"], ["attribute", "procedure"]);
  assert.ok(out.every((column) => !("role" in column)));
  assert.ok(out.every((column) => column.kind !== "anchor"));
});

test("assembleImportColumnKinds: kinds 比列名短时兜底普通", () => {
  const out = assembleImportColumnKinds(["日期", "花费"], ["procedure"]);
  assert.deepStrictEqual(out, [
    { name: "日期", kind: "procedure" },
    { name: "花费", kind: "attribute" },
  ]);
});

test("assembleImportColumnKinds: 不修改传入数组", () => {
  const names = ["A"];
  const kinds = ["entity"];
  assembleImportColumnKinds(names, kinds);
  assert.deepStrictEqual(names, ["A"]);
  assert.deepStrictEqual(kinds, ["entity"]);
});

// --- 管理面板：确认文案 / 表信息 patch -----------------------------------------------

test("破坏性操作确认文案：删列/删行提示连格子与代码附件一并删除", () => {
  assert.strictEqual(COLUMN_DELETE_CONFIRM, "删除该列？列下所有格子与代码附件将一并删除");
  assert.strictEqual(ROW_DELETE_CONFIRM, "删除该行？行内所有格子与代码附件将一并删除");
});

// --- 清理历史：确认文案 / 默认天数 / 天数校验（Task 17，规格 §8.2③ + §7.1）-------

test("HISTORY_PRUNE_CONFIRM: 两层意思都要有——①无法回退到该时间点之前 ②会释放历史引用的图片，但 head 例外", () => {
  assert.strictEqual(
    HISTORY_PRUNE_CONFIRM,
    "清理历史？将无法回退到该时间点之前，同时会释放这些历史引用的图片；但最后一次改动会一直保留，其引用的图片不受影响",
  );
  // 逐层断言，锁住两个必要子句本身（而非只锁整句字面量——防止未来编辑只改
  // 措辞时不小心把其中一层意思删没了却仍然「看起来像」一句合理的确认语）。
  assert.ok(HISTORY_PRUNE_CONFIRM.includes("无法回退到该时间点之前"), "缺少「无法回退」层");
  assert.ok(HISTORY_PRUNE_CONFIRM.includes("释放"), "缺少「释放图片」层");
  assert.ok(HISTORY_PRUNE_CONFIRM.includes("最后一次改动"), "缺少 head 例外限定");
});

test("DEFAULT_HISTORY_PRUNE_DAYS: 默认 180 天（brief 指定值）", () => {
  assert.strictEqual(DEFAULT_HISTORY_PRUNE_DAYS, 180);
});

test("parseHistoryPruneDays: 合法正整数字符串原样转数字", () => {
  assert.strictEqual(parseHistoryPruneDays("180"), 180);
  assert.strictEqual(parseHistoryPruneDays("1"), 1);
  assert.strictEqual(parseHistoryPruneDays("365"), 365);
});

test("parseHistoryPruneDays: 首尾空白 trim 后再校验", () => {
  assert.strictEqual(parseHistoryPruneDays("  30  "), 30);
  assert.strictEqual(parseHistoryPruneDays("\t7\n"), 7);
});

test("parseHistoryPruneDays: 空串/纯空白 → null", () => {
  assert.strictEqual(parseHistoryPruneDays(""), null);
  assert.strictEqual(parseHistoryPruneDays("   "), null);
});

test("parseHistoryPruneDays: 0 与负数 → null（0/负数天没有意义）", () => {
  assert.strictEqual(parseHistoryPruneDays("0"), null);
  assert.strictEqual(parseHistoryPruneDays("-1"), null);
  assert.strictEqual(parseHistoryPruneDays("-180"), null);
});

test("parseHistoryPruneDays: 小数 → null（天数粒度不支持小数）", () => {
  assert.strictEqual(parseHistoryPruneDays("1.5"), null);
  assert.strictEqual(parseHistoryPruneDays("180.0"), null);
});

test("parseHistoryPruneDays: 非数字字符/科学计数法/带正号 → null", () => {
  assert.strictEqual(parseHistoryPruneDays("abc"), null);
  assert.strictEqual(parseHistoryPruneDays("18a"), null);
  assert.strictEqual(parseHistoryPruneDays("1e2"), null);
  assert.strictEqual(parseHistoryPruneDays("+180"), null);
  assert.strictEqual(parseHistoryPruneDays("18 0"), null);
});

test("tableMetaPatch: 无变化返回空对象（hasMetaChanges=false）", () => {
  const current = { title: "表", description: "描述" };
  const patch = tableMetaPatch(current, "表", "描述");
  assert.deepStrictEqual(patch, {});
  assert.strictEqual(hasMetaChanges(patch), false);
});

test("tableMetaPatch: 只装变化字段——标题变则只有 title", () => {
  const patch = tableMetaPatch({ title: "旧", description: "d" }, "新标题", "d");
  assert.deepStrictEqual(patch, { title: "新标题" });
  assert.strictEqual(hasMetaChanges(patch), true);
});

test("tableMetaPatch: 描述变则只有 description；清空描述（空串）也算变化", () => {
  assert.deepStrictEqual(tableMetaPatch({ title: "表", description: "旧描述" }, "表", "新描述"), {
    description: "新描述",
  });
  assert.deepStrictEqual(tableMetaPatch({ title: "表", description: "旧描述" }, "表", ""), {
    description: "",
  });
});

test("tableMetaPatch: 标题/描述 trim 后比较与提交；trim 后空标题不入 patch（标题不可清空）", () => {
  assert.deepStrictEqual(tableMetaPatch({ title: "表", description: "" }, "  表 2 ", ""), { title: "表 2" });
  assert.deepStrictEqual(tableMetaPatch({ title: "表", description: "" }, "   ", ""), {});
});
