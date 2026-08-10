import test from "node:test";
import assert from "node:assert/strict";

import {
  autoModeAskPlaceholder,
  isAdvanced,
  isAutoMode,
  normalizeUiMode,
} from "../../app/ui-mode.ts";

test("normalizeUiMode: 合法值原样保留", () => {
  assert.equal(normalizeUiMode("auto"), "auto");
  assert.equal(normalizeUiMode("advanced"), "advanced");
});

test("normalizeUiMode: 缺省/非法值一律落回 auto（产品默认，不能被解释成用户选了高级）", () => {
  assert.equal(normalizeUiMode(undefined), "auto");
  assert.equal(normalizeUiMode(null), "auto");
  assert.equal(normalizeUiMode(""), "auto");
  assert.equal(normalizeUiMode("Advanced"), "auto"); // 大小写不宽松归一
  assert.equal(normalizeUiMode("expert"), "auto");
  assert.equal(normalizeUiMode(0), "auto");
  assert.equal(normalizeUiMode(true), "auto");
});

test("isAdvanced / isAutoMode 互斥且覆盖两个合法值", () => {
  assert.equal(isAdvanced("advanced"), true);
  assert.equal(isAdvanced("auto"), false);
  assert.equal(isAutoMode("auto"), true);
  assert.equal(isAutoMode("advanced"), false);
});

test("autoModeAskPlaceholder: 0 篇处理中 → 回退既有兜底文案", () => {
  assert.equal(
    autoModeAskPlaceholder(0, "请先添加来源或挂载参考库，再开始对话"),
    "请先添加来源或挂载参考库，再开始对话",
  );
});

test("autoModeAskPlaceholder: N 篇处理中 → 带计数的处理中文案", () => {
  assert.equal(
    autoModeAskPlaceholder(3, "请先添加来源或挂载参考库，再开始对话"),
    "3 篇文档处理中，完成后即可提问",
  );
  assert.equal(
    autoModeAskPlaceholder(1, "fallback"),
    "1 篇文档处理中，完成后即可提问",
  );
});

test("autoModeAskPlaceholder: 计数不可信（null）→ 不带数字的通用处理中文案，不落回 fallback", () => {
  assert.equal(
    autoModeAskPlaceholder(null, "请先添加来源或挂载参考库，再开始对话"),
    "文档处理中，完成后即可提问",
  );
  // 即便 fallback 本身写的是更强的否定结论（如「请先添加来源」），计数不可信时也不能
  // 采信它——那正是「按当前筛选/分页数出 0」会误判成的结论。
  assert.equal(
    autoModeAskPlaceholder(null, "请先添加来源，再开始对话"),
    "文档处理中，完成后即可提问",
  );
});
