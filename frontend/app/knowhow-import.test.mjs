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
} from "./knowhow-import-logic.ts";

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

test("ROLE_OPTIONS: 覆盖全部六个角色", () => {
  assert.strictEqual(ROLE_OPTIONS.length, 6);
});

test("ROLE_OPTIONS: 顺序与规格一致（概念/现象识别/根因分析/修复方法/依赖工具/普通）", () => {
  assert.deepStrictEqual(
    ROLE_OPTIONS.map((option) => option.value),
    ["concept", "identify", "root_cause", "fix", "tool", "plain"],
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
    { name: "违例类型", guessedRole: "concept" },
    { name: "现象", guessedRole: "identify" },
    { name: "备注", guessedRole: "plain" },
  ];
  const roles = ["concept", "root_cause", "fix"];
  assert.deepStrictEqual(assembleImportColumns(columns, roles), [
    { name: "违例类型", role: "concept" },
    { name: "现象", role: "root_cause" },
    { name: "备注", role: "fix" },
  ]);
});

test("assembleImportColumns: roles 数组比 columns 短时缺失项兜底为该列 guessedRole", () => {
  const columns = [
    { name: "A", guessedRole: "concept" },
    { name: "B", guessedRole: "plain" },
  ];
  const roles = ["identify"];
  assert.deepStrictEqual(assembleImportColumns(columns, roles), [
    { name: "A", role: "identify" },
    { name: "B", role: "plain" },
  ]);
});

test("assembleImportColumns: 空列数组返回空数组", () => {
  assert.deepStrictEqual(assembleImportColumns([], []), []);
});

test("assembleImportColumns: 不修改传入的 columns/roles 数组", () => {
  const columns = [{ name: "A", guessedRole: "concept" }];
  const roles = ["concept"];
  const columnsCopy = JSON.parse(JSON.stringify(columns));
  const rolesCopy = [...roles];
  assembleImportColumns(columns, roles);
  assert.deepStrictEqual(columns, columnsCopy);
  assert.deepStrictEqual(roles, rolesCopy);
});

// --- countConceptRoles / conceptValidationError -------------------------------------

test("countConceptRoles: 统计 concept 角色数量", () => {
  assert.strictEqual(countConceptRoles(["concept", "plain", "fix"]), 1);
  assert.strictEqual(countConceptRoles(["concept", "concept", "plain"]), 2);
  assert.strictEqual(countConceptRoles(["plain", "fix", "tool"]), 0);
  assert.strictEqual(countConceptRoles([]), 0);
});

test("conceptValidationError: 恰好一列 concept 时返回 null", () => {
  assert.strictEqual(conceptValidationError(["concept", "plain", "fix"]), null);
});

test("conceptValidationError: 零列 concept 时返回中文提示", () => {
  const message = conceptValidationError(["plain", "fix", "tool"]);
  assert.ok(typeof message === "string" && message.length > 0);
  assert.ok(message.includes("概念"));
});

test("conceptValidationError: 多列 concept 时返回中文提示且提及数量", () => {
  const message = conceptValidationError(["concept", "concept", "plain"]);
  assert.ok(typeof message === "string" && message.length > 0);
  assert.ok(message.includes("概念"));
  assert.ok(message.includes("2"));
});

test("conceptValidationError: 空角色数组视为零列 concept", () => {
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

test("canSubmitImport: 标题非空且恰好一列 concept 时可提交", () => {
  assert.strictEqual(canSubmitImport("我的表", ["concept", "plain"]), true);
});

test("canSubmitImport: 标题为空时不可提交", () => {
  assert.strictEqual(canSubmitImport("   ", ["concept", "plain"]), false);
});

test("canSubmitImport: concept 列数不为一时不可提交", () => {
  assert.strictEqual(canSubmitImport("我的表", ["plain", "plain"]), false);
  assert.strictEqual(canSubmitImport("我的表", ["concept", "concept"]), false);
});

// --- extractErrorMessage ------------------------------------------------------------

test("extractErrorMessage: 抽出 FastAPI JSON detail 中的纯中文错误", () => {
  const err = new Error('400 {"detail":"列定义不能为空"}');
  assert.strictEqual(extractErrorMessage(err), "列定义不能为空");
});

test("extractErrorMessage: 非 400 状态码同样能抽出 detail", () => {
  const err = new Error('404 {"detail":"Not Found"}');
  assert.strictEqual(extractErrorMessage(err), "Not Found");
});

test("extractErrorMessage: 响应体非 JSON 时去掉状态码前缀展示原文", () => {
  const err = new Error("500 Internal Server Error");
  assert.strictEqual(extractErrorMessage(err), "Internal Server Error");
});

test("extractErrorMessage: 空 message 时使用默认兜底文案", () => {
  const err = new Error("");
  assert.strictEqual(extractErrorMessage(err), "操作失败，请重试");
});

test("extractErrorMessage: 支持调用方自定义兜底文案", () => {
  const err = new Error("");
  assert.strictEqual(extractErrorMessage(err, "解析文件失败，请重试"), "解析文件失败，请重试");
});

test("extractErrorMessage: 非 Error 实例（字符串）原样返回", () => {
  assert.strictEqual(extractErrorMessage("weird string"), "weird string");
});

test("extractErrorMessage: null/undefined 时使用默认兜底文案", () => {
  assert.strictEqual(extractErrorMessage(null), "操作失败，请重试");
  assert.strictEqual(extractErrorMessage(undefined), "操作失败，请重试");
});

test("extractErrorMessage: detail 字段非字符串（如 422 校验错误数组）时不崩溃", () => {
  const err = new Error('422 {"detail":[{"loc":["file"],"msg":"field required"}]}');
  const message = extractErrorMessage(err);
  assert.strictEqual(typeof message, "string");
  assert.ok(message.length > 0);
});
