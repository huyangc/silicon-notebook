// knowhow-import.tsx 的纯逻辑单测。knowhow-import.tsx 本身含 JSX，Node 原生
// TS 类型剥离不支持 .tsx（仅 .ts/.mts/.cts），因此可测纯逻辑（payload 组装/
// concept 校验/角色选项/默认标题/错误文案抽取）一律抽到 knowhow-import-logic.ts
// （无 JSX）导出，本文件直接 import 该文件测试（镜像 knowhow-panel.test.mjs
// 对 knowhow-panel-logic.ts 的拆分方式）。
import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_IMPORT_ORIENTATION,
  IMPORT_ACCEPT_EXTENSIONS,
  IMPORT_ACCEPT,
  IMPORT_ORIENTATION_OPTIONS,
  isSupportedImportFile,
  deriveDefaultTitle,
  ROLE_OPTIONS,
  assembleImportColumns,
  countConceptRoles,
  conceptValidationError,
  isBlankTitle,
  canSubmitImport,
  extractErrorMessage,
  computePreviewSpans,
  shouldApplyAnchorPreview,
  importSubmitDisabledReason,
  SUBMIT_BLANK_TITLE_HINT,
  SUBMIT_PREVIEW_REFRESHING_HINT,
  SUBMIT_PREVIEW_ERROR_HINT,
} from "./knowhow-import-logic.ts";
import { humanizedError } from "./errors.ts";
import {
  assignmentsIn,
  callSitesIn,
  findFunction,
  findFunctionIn,
  jsxElements,
  parseModule,
  variableInitializersIn,
} from "./test/semantic-source.mjs";

const importModule = await parseModule("knowhow-import.tsx");

// --- 属性排列方式 ---------------------------------------------------------------

test("IMPORT_ORIENTATION_OPTIONS: 默认属性按列，并提供双方向说明", () => {
  assert.strictEqual(DEFAULT_IMPORT_ORIENTATION, "columns");
  assert.deepStrictEqual(IMPORT_ORIENTATION_OPTIONS, [
    {
      value: "columns",
      label: "属性按列",
      description: "第一行是属性名，每一行是一条记录",
    },
    {
      value: "rows",
      label: "属性按行",
      description: "第一列是属性名，每一列是一条记录",
    },
  ]);
});

test("KnowhowImportWizard: 返回选文件时保留用户选择的属性排列方式", () => {
  const handler = findFunctionIn(
    importModule,
    "KnowhowImportWizard",
    "backToSelect",
  );
  assert.equal(
    callSitesIn(handler).some(({ target }) => target === "setOrientation"),
    false,
  );
});

// --- IMPORT_ACCEPT_EXTENSIONS / IMPORT_ACCEPT -----------------------------------

test("IMPORT_ACCEPT_EXTENSIONS: 恰好三种规格要求的扩展名", () => {
  assert.deepStrictEqual(IMPORT_ACCEPT_EXTENSIONS, [".xlsx", ".csv", ".md"]);
});

test("IMPORT_ACCEPT: 逗号拼接，供 <input accept> 直接使用", () => {
  assert.strictEqual(IMPORT_ACCEPT, ".xlsx,.csv,.md");
});

// --- isSupportedImportFile ------------------------------------------------------

test("isSupportedImportFile: 支持的扩展名返回 true", () => {
  assert.strictEqual(isSupportedImportFile("data.xlsx"), true);
  assert.strictEqual(isSupportedImportFile("data.csv"), true);
  assert.strictEqual(isSupportedImportFile("notes.md"), true);
});

test("isSupportedImportFile: 大小写不敏感", () => {
  assert.strictEqual(isSupportedImportFile("DATA.XLSX"), true);
  assert.strictEqual(isSupportedImportFile("Notes.Md"), true);
});

test("isSupportedImportFile: 不支持的扩展名返回 false", () => {
  assert.strictEqual(isSupportedImportFile("image.png"), false);
  assert.strictEqual(isSupportedImportFile("archive.zip"), false);
});

test("isSupportedImportFile: 无扩展名返回 false", () => {
  assert.strictEqual(isSupportedImportFile("no-extension"), false);
});

test("isSupportedImportFile: 空文件名返回 false", () => {
  assert.strictEqual(isSupportedImportFile(""), false);
});

// --- deriveDefaultTitle ----------------------------------------------------------

test("deriveDefaultTitle: 去掉最后一个扩展名", () => {
  assert.strictEqual(deriveDefaultTitle("report.xlsx"), "report");
  assert.strictEqual(deriveDefaultTitle("notes.md"), "notes");
});

test("deriveDefaultTitle: 只去掉最后一个点之后的部分，保留文件名中间的点", () => {
  assert.strictEqual(deriveDefaultTitle("my.data.csv"), "my.data");
});

test("deriveDefaultTitle: 无扩展名时原样返回整个文件名", () => {
  assert.strictEqual(deriveDefaultTitle("notes"), "notes");
});

test("deriveDefaultTitle: 点在开头（隐藏文件）时原样返回整个文件名", () => {
  assert.strictEqual(deriveDefaultTitle(".gitignore"), ".gitignore");
});

test("deriveDefaultTitle: 空文件名返回空串", () => {
  assert.strictEqual(deriveDefaultTitle(""), "");
});

// --- ROLE_OPTIONS ------------------------------------------------------------------
// 注：角色词表 2026-07-15 由六值收窄为四值 CellKind（行标题/方法步骤/
// 工具/事物/普通），ROLE_OPTIONS 派生自 knowhow-model.ts 的 ROLE_LABELS，随之从
// 6 项变为 4 项——这不是本文件测的逻辑本身变化，只是词表收窄的必然结果。

test("ROLE_OPTIONS: 覆盖全部四个 CellKind 值", () => {
  assert.strictEqual(ROLE_OPTIONS.length, 4);
});

test("ROLE_OPTIONS: 顺序与规格一致（行标题/方法步骤/工具/事物/普通）", () => {
  assert.deepStrictEqual(
    ROLE_OPTIONS.map((option) => option.value),
    ["anchor", "procedure", "entity", "attribute"],
  );
});

test("ROLE_OPTIONS: 每项 label 为非空中文文案", () => {
  for (const option of ROLE_OPTIONS) {
    assert.ok(typeof option.label === "string" && option.label.length > 0, `role ${option.value} 缺少文案`);
  }
});

// --- assembleImportColumns ---------------------------------------------------------

test("assembleImportColumns: 按文件列序对齐组装 name+role", () => {
  const columns = [
    { name: "违例类型", guessedRole: "anchor" },
    { name: "现象", guessedRole: "procedure" },
    { name: "备注", guessedRole: "attribute" },
  ];
  const roles = ["anchor", "procedure", "procedure"];
  assert.deepStrictEqual(assembleImportColumns(columns, roles), [
    { name: "违例类型", role: "anchor" },
    { name: "现象", role: "procedure" },
    { name: "备注", role: "procedure" },
  ]);
});

test("assembleImportColumns: roles 数组比 columns 短时缺失项兜底为该列 guessedRole", () => {
  const columns = [
    { name: "A", guessedRole: "anchor" },
    { name: "B", guessedRole: "attribute" },
  ];
  const roles = ["procedure"];
  assert.deepStrictEqual(assembleImportColumns(columns, roles), [
    { name: "A", role: "procedure" },
    { name: "B", role: "attribute" },
  ]);
});

test("assembleImportColumns: 空列数组返回空数组", () => {
  assert.deepStrictEqual(assembleImportColumns([], []), []);
});

test("assembleImportColumns: 不修改传入的 columns/roles 数组", () => {
  const columns = [{ name: "A", guessedRole: "anchor" }];
  const roles = ["anchor"];
  const columnsCopy = JSON.parse(JSON.stringify(columns));
  const rolesCopy = [...roles];
  assembleImportColumns(columns, roles);
  assert.deepStrictEqual(columns, columnsCopy);
  assert.deepStrictEqual(roles, rolesCopy);
});

// --- countConceptRoles / conceptValidationError -------------------------------------
// 注：角色词表收窄后，原 "concept" 值已改名为 "anchor"（行标题）；本节校验
// 规则本身（恰好一列）暂未改动——放宽为「至多一列」是 Task 5 随独立行标题
// 列选择器一并落地的产品决策，不在本任务范围内。

test("countConceptRoles: 统计 anchor(行标题) 角色数量", () => {
  assert.strictEqual(countConceptRoles(["anchor", "attribute", "procedure"]), 1);
  assert.strictEqual(countConceptRoles(["anchor", "anchor", "attribute"]), 2);
  assert.strictEqual(countConceptRoles(["attribute", "procedure", "entity"]), 0);
  assert.strictEqual(countConceptRoles([]), 0);
});

test("conceptValidationError: 恰好一列 anchor 时返回 null", () => {
  assert.strictEqual(conceptValidationError(["anchor", "attribute", "procedure"]), null);
});

test("conceptValidationError: 零列 anchor 时返回中文提示", () => {
  const message = conceptValidationError(["attribute", "procedure", "entity"]);
  assert.ok(typeof message === "string" && message.length > 0);
  assert.ok(message.includes("行标题"));
});

test("conceptValidationError: 多列 anchor 时返回中文提示且提及数量", () => {
  const message = conceptValidationError(["anchor", "anchor", "attribute"]);
  assert.ok(typeof message === "string" && message.length > 0);
  assert.ok(message.includes("行标题"));
  assert.ok(message.includes("2"));
});

test("conceptValidationError: 空角色数组视为零列 anchor", () => {
  assert.notStrictEqual(conceptValidationError([]), null);
});

// --- isBlankTitle / canSubmitImport -------------------------------------------------

test("isBlankTitle: 空串或纯空白视为空", () => {
  assert.strictEqual(isBlankTitle(""), true);
  assert.strictEqual(isBlankTitle("   "), true);
  assert.strictEqual(isBlankTitle("\n\t"), true);
});

test("isBlankTitle: 含非空白内容视为非空", () => {
  assert.strictEqual(isBlankTitle("我的表"), false);
  assert.strictEqual(isBlankTitle("  我的表  "), false);
});

test("canSubmitImport: 标题非空且恰好一列 anchor 时可提交", () => {
  assert.strictEqual(canSubmitImport("我的表", ["anchor", "attribute"]), true);
});

test("canSubmitImport: 标题为空时不可提交", () => {
  assert.strictEqual(canSubmitImport("   ", ["anchor", "attribute"]), false);
});

test("canSubmitImport: anchor 列数不为一时不可提交", () => {
  assert.strictEqual(canSubmitImport("我的表", ["attribute", "attribute"]), false);
  assert.strictEqual(canSubmitImport("我的表", ["anchor", "anchor"]), false);
});

// --- extractErrorMessage ------------------------------------------------------------
//
// 它现在是 errors.ts 的 toUserMessage() 的别名（knowhow 面板 ~20 个调用点共用）。
// 这批用例连同它的历史一起改写过：以前 knowhow-model 的 apiFetch() 裸抛
// `${status} ${bodyText}`，所以这里自己解析状态码前缀和 JSON detail，并且
// 「解析不出来就展示原文」——那正是把 `Internal Server Error` / `Not Found`
// 写进用户错误条的那条路径。apiFetch() 改走 throwHumanizedHttpError() 之后
// 那套解析全成了死代码，兜底规则也随之收紧：英文一律不给用户看。

// 静音 toUserMessage 兜底时写的 console.error（原始值本就该进 console，这里
// 只是不让它污染测试输出）。
function silenced(fn) {
  const original = console.error;
  console.error = () => {};
  try {
    return fn();
  } finally {
    console.error = original;
  }
}

test("extractErrorMessage: 已翻译的中文错误原样保留", () => {
  // 上游（apiFetch → throwHumanizedHttpError）给的就是人话且带品牌，不能再加工。
  assert.strictEqual(extractErrorMessage(humanizedError("列定义不能为空")), "列定义不能为空");
  assert.strictEqual(
    extractErrorMessage(humanizedError("没有权限进行这个操作")),
    "没有权限进行这个操作"
  );
});

test("extractErrorMessage: 没盖章的中文串不算「已翻译」", () => {
  // 第三轮评审:判据是品牌不是形态。裸 new Error("列定义不能为空") 可能来自
  // 后端 detail=str(exc),形态上与上面那条一模一样。
  silenced(() => {
    assert.strictEqual(extractErrorMessage(new Error("列定义不能为空")), "操作失败，请重试");
  });
});

test("extractErrorMessage: 英文技术串不进用户文案，走兜底", () => {
  // 回归：这三条以前会被原样写进错误条。
  silenced(() => {
    assert.strictEqual(extractErrorMessage(new Error("Not Found")), "操作失败，请重试");
    assert.strictEqual(extractErrorMessage(new Error("Internal Server Error")), "操作失败，请重试");
    assert.strictEqual(
      extractErrorMessage(new TypeError("Failed to fetch"), "解析文件失败，请重试"),
      "解析文件失败，请重试"
    );
  });
});

test("extractErrorMessage: 带 JSON 花括号的原文不直出（哪怕含中文）", () => {
  silenced(() => {
    assert.strictEqual(extractErrorMessage(new Error('400 {"detail":"列定义不能为空"}')), "操作失败，请重试");
  });
});

test("extractErrorMessage: 空 message 时使用默认兜底文案", () => {
  silenced(() => {
    assert.strictEqual(extractErrorMessage(new Error("")), "操作失败，请重试");
  });
});

test("extractErrorMessage: 支持调用方自定义兜底文案", () => {
  silenced(() => {
    assert.strictEqual(extractErrorMessage(new Error(""), "解析文件失败，请重试"), "解析文件失败，请重试");
  });
});

test("extractErrorMessage: 非 Error 实例（字符串）走兜底而不是原样返回", () => {
  silenced(() => {
    assert.strictEqual(extractErrorMessage("weird string"), "操作失败，请重试");
  });
});

test("extractErrorMessage: null/undefined 时使用默认兜底文案", () => {
  silenced(() => {
    assert.strictEqual(extractErrorMessage(null), "操作失败，请重试");
    assert.strictEqual(extractErrorMessage(undefined), "操作失败，请重试");
  });
});

// --- 预览合并显示（真机反馈）。转置表导入时，行标题列的兄弟行在预览里
// 显示成一串「—」，看着像数据丢了；实际落库前后端 forward_fill_column 会
// 把它们补成同一个概念。预览应当像 Excel 原表 / 导入后的主网格（G2，
// computeGridSpans）那样合并显示，做到「预览即所得」。

test("computePreviewSpans 先 forward-fill 行标题列，再合并相邻同值", () => {
  const rows = [
    ["hold和setup打架", "同一现象", "根因1"],
    ["", "同一现象", "根因2"],
    ["", "同一现象", "根因3"],
  ];
  const spans = computePreviewSpans(rows, 0);
  // 行标题列：兄弟行的空被 fill 成同值 → 合并成一个 rowSpan=3 的起始格
  assert.deepEqual(spans[0][0], { text: "hold和setup打架", rowSpan: 3 });
  assert.equal(spans[1][0].rowSpan, 0);
  assert.equal(spans[2][0].rowSpan, 0);
  // 现象列本就三行同值（Excel 里原是合并格）→ 同样合并
  assert.equal(spans[0][1].rowSpan, 3);
  // 根因列三行各不同 → 各自独立
  assert.deepEqual(spans[0][2], { text: "根因1", rowSpan: 1 });
  assert.deepEqual(spans[1][2], { text: "根因2", rowSpan: 1 });
});

test("computePreviewSpans 未选行标题列时不 forward-fill", () => {
  const rows = [
    ["A", "x"],
    ["", "x"],
  ];
  const spans = computePreviewSpans(rows, null);
  // 没有行标题列 → 空不继承 → "A" 与 "" 不同值，不合并
  assert.deepEqual(spans[0][0], { text: "A", rowSpan: 1 });
  assert.deepEqual(spans[1][0], { text: "", rowSpan: 1 });
  // 其余列仍按相邻同值合并
  assert.equal(spans[0][1].rowSpan, 2);
});

test("computePreviewSpans 首行行标题为空时不被 fill（leading-blank）", () => {
  // 首行行标题就空、上面没有可继承的值 → 保持空、独立成行，与后端
  // forward_fill_column 的 leading-blank 语义一致。
  const rows = [
    ["", "x"],
    ["A", "y"],
  ];
  const spans = computePreviewSpans(rows, 0);
  assert.deepEqual(spans[0][0], { text: "", rowSpan: 1 });
  assert.deepEqual(spans[1][0], { text: "A", rowSpan: 1 });
});

// --- shouldApplyAnchorPreview（P1：在飞重取预览的失效守卫）---------------------
//
// 缺陷：改锚定的在飞重取用请求序号自守；但「返回选择步骤 / 另选文件」此前不
// 递增序号，于是文件 A 的迟到重取响应仍匹配旧序号、把已切到的文件 B 的预览
// 覆盖成 A 的行。守卫本身很小——真正的修复是把序号递增接到那两个离开映射
// 上下文的点上（见下方源码接线守卫）；本函数只负责「响应序号还等于当前序号
// 且组件仍挂载才落地」这个判定。

test("shouldApplyAnchorPreview: 挂载中且序号匹配 → true（最新一次响应落地）", () => {
  assert.strictEqual(shouldApplyAnchorPreview(true, 3, 3), true);
});

test("shouldApplyAnchorPreview: 序号失配 → false（更晚的切文件/返回/重取已把当前序号推进，旧响应被丢弃）", () => {
  // 正是 P1 场景：文件 A 的重取 responseSeq=1，用户返回并切到文件 B 把
  // currentSeq 推到 3，A 的迟到响应到达时 1 !== 3 → 丢弃，不覆盖 B 的预览。
  assert.strictEqual(shouldApplyAnchorPreview(true, 1, 3), false);
  assert.strictEqual(shouldApplyAnchorPreview(true, 4, 3), false);
});

test("shouldApplyAnchorPreview: 已卸载 → false（即便序号匹配也不 setState 到已卸载组件）", () => {
  assert.strictEqual(shouldApplyAnchorPreview(false, 3, 3), false);
});

// --- importSubmitDisabledReason（P2：提交必须等重取预览就绪）-------------------
//
// 提交按钮在「预览正在按新锚定列重取」或「重取失败」时必须置灰——否则用户
// 会拿着旧锚定列规整的预览（或根本没刷新成功的预览）提交，commit 却按新锚定
// 列走，展示≠落库。同 optimizeCellDisabledReason：同一函数算出的原因同时驱动
// disabled 与 title，不会「置灰了但提示语对不上真实原因」。

test("importSubmitDisabledReason: 全部就绪 → null（可提交）", () => {
  assert.strictEqual(importSubmitDisabledReason("我的表", false, null), null);
});

test("importSubmitDisabledReason: 标题为空 → 提示填标题（优先级最高）", () => {
  assert.strictEqual(importSubmitDisabledReason("   ", false, null), SUBMIT_BLANK_TITLE_HINT);
  // 标题空优先于预览态：即便同时在重取，也先提示更根本的「没填标题」。
  assert.strictEqual(importSubmitDisabledReason("", true, "更新预览失败"), SUBMIT_BLANK_TITLE_HINT);
});

test("importSubmitDisabledReason: 预览重取中 → 提示稍候（挡住「拿旧预览提交」）", () => {
  assert.strictEqual(importSubmitDisabledReason("我的表", true, null), SUBMIT_PREVIEW_REFRESHING_HINT);
});

test("importSubmitDisabledReason: 预览重取失败 → 提示重选行标题列后再试（挡住「拿没刷成功的预览提交」）", () => {
  assert.strictEqual(importSubmitDisabledReason("我的表", false, "更新预览失败，请重试"), SUBMIT_PREVIEW_ERROR_HINT);
});

test("importSubmitDisabledReason: 重取中优先于重取失败（loading 时不显示上一次的 error）", () => {
  assert.strictEqual(importSubmitDisabledReason("我的表", true, "上次的错误"), SUBMIT_PREVIEW_REFRESHING_HINT);
});

// --- 接线：改选行标题列 → 重取预览（P2 全栈修复的前端一侧）--------------------
//
// 这些守卫查询 TypeScript AST 的调用、赋值、变量与 JSX 绑定，不依赖源码行号、
// 字符偏移、函数排列或格式。锁住的是「预览即所得」的业务接线。

test("接线：行标题单选的 onAnchorChange 接到 handleAnchorChange（不是裸 setAnchorIndex——否则只改分组不重取规整）", () => {
  const mapping = jsxElements(importModule, "MapStep").find(
    ({ scope }) => scope === "<module>.KnowhowImportWizard",
  );
  assert.equal(mapping?.bindings.onAnchorChange, "handleAnchorChange");
});

test("接线：handleAnchorChange 用**所选** anchor 下标重取预览（预览随所选列重新规整）", () => {
  const handler = findFunctionIn(
    importModule,
    "KnowhowImportWizard",
    "handleAnchorChange",
  );
  assert.deepEqual(
    callSitesIn(handler).find(({ target }) => target === "importKnowhowPreview"),
    {
      target: "importKnowhowPreview",
      arguments: ["notebookId", "currentFile", "orientation", "nextAnchorIndex"],
    },
  );
});

test("接线：初始预览携带用户选择的属性排列方式", () => {
  const handler = findFunctionIn(
    importModule,
    "KnowhowImportWizard",
    "handleFileSelected",
  );
  assert.deepEqual(
    callSitesIn(handler).find(({ target }) => target === "importKnowhowPreview"),
    {
      target: "importKnowhowPreview",
      arguments: ["notebookId", "selected", "orientation"],
    },
  );
});

test("接线：handleAnchorChange 带请求序号守卫（快速切换只让最新一次落地，丢弃乱序旧响应）", () => {
  const handler = findFunctionIn(
    importModule,
    "KnowhowImportWizard",
    "handleAnchorChange",
  );
  assert.deepEqual(
    variableInitializersIn(handler).find(({ name }) => name === "seq"),
    { name: "seq", initializer: "anchorReqSeqRef.current + 1" },
  );
  assert.deepEqual(assignmentsIn(handler), [
    { target: "anchorReqSeqRef.current", operator: "=", value: "seq" },
  ]);
  // 守卫经纯逻辑 shouldApplyAnchorPreview（单测另有覆盖）：响应序号仍是最新且
  // 组件仍挂载才落地——序号在切文件/返回选择步骤时被推进，旧响应在此失配丢弃。
  const guards = callSitesIn(handler).filter(
    ({ target }) => target === "shouldApplyAnchorPreview",
  );
  assert.deepEqual(
    [...new Set(guards.map(({ arguments: args }) => JSON.stringify(args)))],
    [JSON.stringify(["mountedRef.current", "seq", "anchorReqSeqRef.current"])],
  );
});

test("接线：重取失败保留旧网格（不清空 preview），只置 refreshError 提示", () => {
  // 清空 preview 会让用户改一次行标题就把整块预览打没，比原 bug 更糟。
  const calls = callSitesIn(findFunctionIn(
    importModule,
    "KnowhowImportWizard",
    "handleAnchorChange",
  ));
  assert.ok(calls.some(({ target }) => target === "setAnchorPreviewError"));
  assert.equal(
    calls.some(
      ({ target, arguments: args }) => (
        target === "setPreview" && args[0] === "null"
      ),
    ),
    false,
  );
});

test("接线：初始「猜测预选」不经 handleAnchorChange（handleFileSelected 直接 setAnchorIndex，不多取一次预览）", () => {
  // 初始预览本就是后端按猜测列规整的；若初始也走重取，等于每次开文件都多一趟往返。
  const calls = callSitesIn(findFunctionIn(
    importModule,
    "KnowhowImportWizard",
    "handleFileSelected",
  ));
  assert.deepEqual(
    calls.find(({ target }) => target === "setAnchorIndex"),
    { target: "setAnchorIndex", arguments: ["selection.anchorIndex"] },
  );
  assert.equal(calls.some(({ target }) => target === "handleAnchorChange"), false);
});

// --- 接线（P1）：离开映射上下文时作废在飞重取预览 -----------------------------
//
// 缺陷：文件 A 改锚定的在飞重取（seq=N）在用户返回并切到文件 B 后迟到返回，
// 序号仍匹配 → 把 B 的预览覆盖成 A 的行（列数相等还能就此把 B 按 A 的列导入）。
// 修复：在两个「离开映射上下文」的点递增序号——(a) 新文件初始预览开始，
// (b) 返回选择步骤——使那条迟到响应失配、被 shouldApplyAnchorPreview 丢弃。

test("接线（P1）：handleFileSelected 初始预览开始时递增序号并清掉在飞重取态（作废上一个文件仍在飞的重取响应）", () => {
  const handler = findFunctionIn(
    importModule,
    "KnowhowImportWizard",
    "handleFileSelected",
  );
  assert.deepEqual(assignmentsIn(handler), [
    { target: "anchorReqSeqRef.current", operator: "+=", value: "1" },
  ]);
  const calls = callSitesIn(handler);
  assert.ok(calls.some(
    ({ target, arguments: args }) => (
      target === "setAnchorPreviewLoading" && args[0] === "false"
    ),
  ));
  assert.ok(calls.some(
    ({ target, arguments: args }) => (
      target === "setAnchorPreviewError" && args[0] === "null"
    ),
  ));
});

test("接线（P1）：backToSelect 返回选择步骤时递增序号（作废仍在飞的重取响应，防它迟到覆盖已切走的文件）", () => {
  const handler = findFunctionIn(
    importModule,
    "KnowhowImportWizard",
    "backToSelect",
  );
  assert.deepEqual(assignmentsIn(handler), [
    { target: "anchorReqSeqRef.current", operator: "+=", value: "1" },
  ]);
  // 返回时同样清掉在飞重取态（否则旧的 refreshing/error 提示会残留）。
  const calls = callSitesIn(handler);
  assert.ok(calls.some(
    ({ target, arguments: args }) => (
      target === "setAnchorPreviewLoading" && args[0] === "false"
    ),
  ));
  assert.ok(calls.some(
    ({ target, arguments: args }) => (
      target === "setAnchorPreviewError" && args[0] === "null"
    ),
  ));
});

// --- 接线（P2）：提交必须等重取预览就绪 ---------------------------------------
//
// 缺陷：改锚定后预览还在重取（anchorPreviewLoading）或重取失败（anchorPreviewError）
// 时提交按钮仍可点——用户会拿着旧锚定列规整的（或根本没刷成功的）预览提交，
// commit 却按新锚定列走，展示≠落库。修复：这两态并入 submitDisabled，并把置灰
// 原因绑到按钮 title（置灰必有对得上的原因）。

test("接线（P2）：提交置灰原因经 importSubmitDisabledReason 计算（预览重取中/失败都并入 disabled）", () => {
  const wizard = findFunction(importModule, "KnowhowImportWizard");
  const initializers = variableInitializersIn(wizard);
  assert.deepEqual(
    initializers.find(({ name }) => name === "submitDisabledReason"),
    {
      name: "submitDisabledReason",
      initializer: "importSubmitDisabledReason(title, anchorPreviewLoading, anchorPreviewError)",
    },
  );
  // submitting 单独 OR 进 disabled（它有自己的按钮文案「导入中…」），其余原因来自 helper。
  assert.deepEqual(
    initializers.find(({ name }) => name === "submitDisabled"),
    {
      name: "submitDisabled",
      initializer: "submitting || submitDisabledReason !== null",
    },
  );
});

test("接线（P2）：提交按钮把置灰原因绑到 title（hover 能看到为什么不能点）", () => {
  const button = jsxElements(importModule, "button").find(
    ({ attributes, scope }) => (
      scope === "<module>.MapStep"
      && attributes.className === "new-pill knowhow-import-submit-button"
    ),
  );
  assert.equal(button?.bindings.disabled, "submitDisabled");
  assert.equal(button?.bindings.title, "submitDisabledReason ?? undefined");
});
