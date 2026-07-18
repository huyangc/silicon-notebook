// knowhow-import.tsx 的纯逻辑单测。knowhow-import.tsx 本身含 JSX，Node 原生
// TS 类型剥离不支持 .tsx（仅 .ts/.mts/.cts），因此可测纯逻辑（payload 组装/
// concept 校验/角色选项/默认标题/错误文案抽取）一律抽到 knowhow-import-logic.ts
// （无 JSX）导出，本文件直接 import 该文件测试（镜像 knowhow-panel.test.mjs
// 对 knowhow-panel-logic.ts 的拆分方式）。
import test from "node:test";
import assert from "node:assert/strict";

import {
  IMPORT_ACCEPT_EXTENSIONS,
  IMPORT_ACCEPT,
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
} from "./knowhow-import-logic.ts";
import { humanizedError } from "./errors.ts";

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
