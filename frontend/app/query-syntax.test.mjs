import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_QUOTED_PHRASES,
  MIN_PHRASE_CHARS,
  analyzeQuotedPhrases,
  quotedPhraseHint,
  quotedPhrases,
} from "./query-syntax.ts";
import { appSourceModules, importsFrom } from "./test/semantic-source.mjs";


// 跨端契约:这份表与 backend/tests/test_query_syntax.py 的 PARITY_CASES 逐条相同。
// 两侧规则一旦分叉,提问框就会承诺一个检索侧不认的约束(或反过来)。
const PARITY_CASES = [
  ['什么是 "static timing analysis" 的原理', ["static timing analysis"]],
  ['比较 "OCV" 与 "AOCV 分析" 的差异', ["OCV", "AOCV 分析"]],
  ["没有引号的普通问题", []],
  ['单个未闭合的 " 引号', []],
  ['他说"这个"和"那个"的区别', []],
  ['"Set DB" 与 "set db" 有区别吗', ["Set DB"]],
  ['"  static   timing  " 是什么', ["static timing"]],
  ['{"a":"aaa","b":"bbb","c":"ccc","d":"ddd","e":"eee"}', []],
];


test("quoted phrase parsing matches the backend case for case", () => {
  for (const [text, expected] of PARITY_CASES) {
    assert.deepEqual(quotedPhrases(text), expected, text);
  }
});


test("the shared limits are the backend's limits", () => {
  // 这两个数字是跨端契约的一部分:后端按它们决定「识别/不识别」,这里按它们解释原因。
  assert.equal(MAX_QUOTED_PHRASES, 4);
  assert.equal(MIN_PHRASE_CHARS, 3);
});


test("too many spans disables the syntax instead of taking a prefix", () => {
  const text = Array.from({ length: MAX_QUOTED_PHRASES + 1 }, (_v, i) => `"phrase${i}"`).join(" ");
  const analysis = analyzeQuotedPhrases(text);
  assert.deepEqual(analysis.phrases, []);
  assert.equal(analysis.tooMany, true);
});


test("at the limit the syntax still applies", () => {
  const text = Array.from({ length: MAX_QUOTED_PHRASES }, (_v, i) => `"phrase${i}"`).join(" ");
  assert.equal(analyzeQuotedPhrases(text).phrases.length, MAX_QUOTED_PHRASES);
});


test("typographic quotes are not the syntax", () => {
  assert.deepEqual(quotedPhrases("他说“静态时序分析”很重要"), []);
});


test("the hint stays silent until the user types a straight quote", () => {
  // 没写引号 = 不渲染:这行提示是回执,不是常驻广告。
  assert.equal(quotedPhraseHint(""), null);
  assert.equal(quotedPhraseHint("普通提问"), null);
  assert.equal(quotedPhraseHint("他说“静态时序分析”很重要"), null);
});


test("the quote parse exists only in the shared module", async () => {
  // 「移动」变异守卫:任何界面自己写一遍 `"(...)"` 的解析,这里就报红。
  // 光断言 query-syntax.ts 存在挡不住「一边 import 共享规则、一边在页面里另抄
  // 一套判断」——那正是两端规则分叉的入口,而分叉在界面上表现为一句假承诺。
  const OWNER = "query-syntax.ts";
  const offenders = [];
  for (const { path, module } of await appSourceModules()) {
    if (path === OWNER) continue;
    const text = module.getFullText();
    // 双引号捕获组的正则字面量:这是这条规则的唯一形状。
    if (/\/"\(?\[\^/.test(text) || /new RegExp\(\s*['"`]"/.test(text)) {
      offenders.push(path);
    }
  }
  assert.deepEqual(offenders, []);

  // 两个消费端确实走的是共享模块,而且真的把结果**渲染**出来了——只 import 不渲染
  // 等于回执从未上屏,而回执正是这条语法唯一的可发现入口。
  const consumers = ["page.tsx", "report-view.tsx"];
  const modules = new Map((await appSourceModules()).map((item) => [item.path, item.module]));
  for (const consumer of consumers) {
    const module = modules.get(consumer);
    const imported = importsFrom(module, "./query-syntax").map((item) => item.imported);
    assert.ok(imported.includes("quotedPhraseHint"), `${consumer} 未引入共享回执`);
    const rendered = module.getFullText().match(/quotedPhraseHint\(/g) ?? [];
    assert.ok(rendered.length >= 1, `${consumer} 引入了却没调用`);
    // 回执落在 JSX 里(而不是只算给某个变量再丢掉)。
    assert.match(
      module.getFullText(),
      /\{\s*(askQuotedPhraseHint|quotedPhraseHint\(question\))/,
      `${consumer} 未把回执渲染进 JSX`,
    );
  }
});


test("the hint confirms what was recognised, and explains what was not", () => {
  assert.match(quotedPhraseHint('"static timing analysis" 的原理'), /static timing analysis/);
  // 写了引号却没识别 → 必须说清为什么,否则就是一次静默失败。
  assert.match(quotedPhraseHint('他说"这个"很重要'), /未识别到精确短语/);
  const dense = Array.from({ length: MAX_QUOTED_PHRASES + 1 }, (_v, i) => `"phrase${i}"`).join(" ");
  assert.match(quotedPhraseHint(dense), /引号超过/);
});
