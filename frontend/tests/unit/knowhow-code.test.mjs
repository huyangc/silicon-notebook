// Task 11（代码附件 UI）纯逻辑测试——三态新鲜度展示映射/抽屉 chip 显示门控/
// 行级 code map 缺席补位/保存前校验/复制分支决策，全部来自 knowhow-code-
// logic.ts（无 JSX，可被 node --test 直接 import）。组件渲染（KnowhowCodeChip/
// KnowhowCodeModal 的 JSX 挂载态）由 tsc + 浏览器 QA 验证，见任务简报。

import test from "node:test";
import assert from "node:assert/strict";

import {
  CODE_STATUS_LABELS,
  CODE_STATUS_TONE,
  CODE_STATUS_EXPLANATIONS,
  CODE_EMPTY_ERROR,
  shouldShowCodeChip,
  resolveCellCodeView,
  codeProvenanceSuffix,
  codeSaveDisabledReason,
  normalizeLanguageInput,
  codeEditorIsDirty,
  resolveCopyStrategy,
} from "../../app/knowhow-code-logic.ts";

const STATUSES = ["implemented", "stale", "none"];

// --- 1. 三态展示映射 -------------------------------------------------------------

test("CODE_STATUS_LABELS: 恰好三态，文案与规格⑥-4/任务简报一致（已实现/知识已更新/未实现）", () => {
  assert.deepStrictEqual(CODE_STATUS_LABELS, {
    implemented: "已实现",
    stale: "知识已更新",
    none: "未实现",
  });
});

test("CODE_STATUS_TONE: 三态都有色调，implemented=success/stale=warning/none=neutral", () => {
  assert.deepStrictEqual(CODE_STATUS_TONE, {
    implemented: "success",
    stale: "warning",
    none: "neutral",
  });
});

test("CODE_STATUS_EXPLANATIONS: stale 说明句与规格⑥-4 原文逐字一致", () => {
  assert.strictEqual(CODE_STATUS_EXPLANATIONS.stale, "格子内容已更新，此代码可能过期");
});

test("CODE_STATUS_EXPLANATIONS: 三态都有非空说明句", () => {
  for (const status of STATUSES) {
    assert.ok(
      typeof CODE_STATUS_EXPLANATIONS[status] === "string" && CODE_STATUS_EXPLANATIONS[status].length > 0,
      `status ${status} 缺少说明句`,
    );
  }
});

test("三张映射表键集合互相一致（三态穷举，不多不少）", () => {
  const labelKeys = Object.keys(CODE_STATUS_LABELS).sort();
  const toneKeys = Object.keys(CODE_STATUS_TONE).sort();
  const explainKeys = Object.keys(CODE_STATUS_EXPLANATIONS).sort();
  assert.deepStrictEqual(labelKeys, [...STATUSES].sort());
  assert.deepStrictEqual(toneKeys, [...STATUSES].sort());
  assert.deepStrictEqual(explainKeys, [...STATUSES].sort());
});

// --- 2. chip 显示门控 -------------------------------------------------------------

test("shouldShowCodeChip: implemented 无论 canEdit 与否都显示", () => {
  assert.equal(shouldShowCodeChip("implemented", true), true);
  assert.equal(shouldShowCodeChip("implemented", false), true);
});

test("shouldShowCodeChip: stale 无论 canEdit 与否都显示", () => {
  assert.equal(shouldShowCodeChip("stale", true), true);
  assert.equal(shouldShowCodeChip("stale", false), true);
});

test("shouldShowCodeChip: none 只在 canEdit=true 时显示（安静的「添加代码」入口）", () => {
  assert.equal(shouldShowCodeChip("none", true), true);
});

test("shouldShowCodeChip: none 且 canEdit=false 时不显示（只读成员看不到任何代码入口）", () => {
  assert.equal(shouldShowCodeChip("none", false), false);
});

// --- 3. 行级 code map 缺席补位 -----------------------------------------------------

test("resolveCellCodeView: 命中时原样返回该列的代码视图", () => {
  const map = { c1: { codeText: "print(1)", language: "python", status: "implemented", updatedAt: "2026-07-15T00:00:00Z" } };
  assert.deepStrictEqual(resolveCellCodeView(map, "c1"), map.c1);
});

test("resolveCellCodeView: 缺席时合成 none 占位（codeText/language 空串、updatedAt/updatedBy=null）", () => {
  assert.deepStrictEqual(resolveCellCodeView({}, "c-missing"), {
    codeText: "",
    language: "",
    status: "none",
    updatedAt: null,
    updatedBy: null,
  });
});

test("resolveCellCodeView: map 里其它列命中不影响这一列的缺席判定", () => {
  const map = { other: { codeText: "x", language: "", status: "implemented", updatedAt: "2026-07-01T00:00:00Z" } };
  assert.strictEqual(resolveCellCodeView(map, "c1").status, "none");
});

// --- 3b. 查看态「最近更新」溯源后缀（收尾修复：updated_by 展示）--------------------

test("codeProvenanceSuffix: updatedBy 非空时返回带前导分隔符的「 · 来自 {名字}」", () => {
  assert.strictEqual(codeProvenanceSuffix("CodeAgent"), " · 来自 CodeAgent");
  assert.strictEqual(codeProvenanceSuffix("a00123456"), " · 来自 a00123456");
});

test("codeProvenanceSuffix: null/undefined/空串/纯空白一律返回空串（不合成假来源）", () => {
  assert.strictEqual(codeProvenanceSuffix(null), "");
  assert.strictEqual(codeProvenanceSuffix(undefined), "");
  assert.strictEqual(codeProvenanceSuffix(""), "");
  assert.strictEqual(codeProvenanceSuffix("   "), "");
});

test("codeProvenanceSuffix: 名字首尾空白被裁剪后再拼接", () => {
  assert.strictEqual(codeProvenanceSuffix("  agent-1  "), " · 来自 agent-1");
});

// --- 4. 保存前校验 + language 归一化 + 脏检测 --------------------------------------

test("codeSaveDisabledReason: 空串返回错误文案（与后端 put_cell_code 校验同文案）", () => {
  assert.strictEqual(codeSaveDisabledReason(""), CODE_EMPTY_ERROR);
  assert.strictEqual(CODE_EMPTY_ERROR, "代码内容不能为空");
});

test("codeSaveDisabledReason: 纯空白同样视为空", () => {
  assert.strictEqual(codeSaveDisabledReason("   \n\t  "), CODE_EMPTY_ERROR);
});

test("codeSaveDisabledReason: 非空内容返回 null（可保存）", () => {
  assert.strictEqual(codeSaveDisabledReason("print(1)"), null);
});

test("normalizeLanguageInput: 去首尾空白", () => {
  assert.strictEqual(normalizeLanguageInput("  python  "), "python");
});

test("normalizeLanguageInput: 空串/纯空白归一为空串", () => {
  assert.strictEqual(normalizeLanguageInput(""), "");
  assert.strictEqual(normalizeLanguageInput("   "), "");
});

test("codeEditorIsDirty: 代码正文与已保存值完全一致、language 也一致时不算脏", () => {
  const saved = { codeText: "print(1)", language: "python" };
  assert.equal(codeEditorIsDirty("print(1)", "python", saved), false);
});

test("codeEditorIsDirty: 代码正文变化即为脏", () => {
  const saved = { codeText: "print(1)", language: "python" };
  assert.equal(codeEditorIsDirty("print(2)", "python", saved), true);
});

test("codeEditorIsDirty: language 实质性变化即为脏", () => {
  const saved = { codeText: "print(1)", language: "python" };
  assert.equal(codeEditorIsDirty("print(1)", "tcl", saved), true);
});

test("codeEditorIsDirty: language 只是首尾多打了空格不算脏（按归一化后比较）", () => {
  const saved = { codeText: "print(1)", language: "python" };
  assert.equal(codeEditorIsDirty("print(1)", "  python  ", saved), false);
});

test("codeEditorIsDirty: 已保存值本身未归一化(旧数据带空白)时也按归一化后比较", () => {
  const saved = { codeText: "print(1)", language: "  python  " };
  assert.equal(codeEditorIsDirty("print(1)", "python", saved), false);
});

// --- 5. 复制分支决策 ---------------------------------------------------------------

test("resolveCopyStrategy: 探测到 clipboard API 时走 clipboard-api 分支", () => {
  assert.strictEqual(resolveCopyStrategy(true), "clipboard-api");
});

test("resolveCopyStrategy: 探测不到时走 execCommand-fallback 分支", () => {
  assert.strictEqual(resolveCopyStrategy(false), "execCommand-fallback");
});
