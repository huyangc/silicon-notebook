// 档位控件的样式隔离守卫。
//
// 背景(这是实际发生过的回归,不是假想):EffortPicker 同时渲染在深度报告生成区和问答
// 输入行两处。问答那侧的祖先带 `.chat-input-bar span { color; font-size }` —— 一个类
// 加一个元素,比控件自己的单类规则更具体,于是 chip/popover 里那几个 <span> 的蓝色和
// 字号被压掉,「同一个控件」在两处长得不一样。控件复用的意义正是两处一致,所以这条
// 必须有机器守卫,而不是靠下次评审再抓一遍。
//
// 为什么不用 jsdom 量计算样式:jsdom 的 getComputedStyle 不实现特异性级联(实测让单类
// 规则压过了 `.a span`),据此写断言会得到一个稳定的假绿 —— 比没有守卫更糟。所以改为
// 静态比较选择器特异性。
//
// 断言:effort-picker.tsx 里渲染成 <span> 的每个类,凡是在 globals.css 里声明了 color
// 或 font-size 的规则,特异性都必须**严格高于** `.chat-input-bar span`。span 是关键
// 限定 —— 那条祖先规则只命中 <span>,chip 本身是 <button>、说明文字是 <p>,都不在射程
// 内,把它们一起卷进来只会逼出无意义的选择器加权。
//
// 覆盖边界(如实说明):本守卫只钉住「已知的那条祖先规则」这一个量级。日后若有人在
// composer 里加一条更具体的祖先规则(比如 `.chat-input-controls .effort-picker span`),
// 它同样能压过控件而本守卫不会报红。那种情况的兜底是 docs/product-and-api*.md
// 的共享控件约定与人工评审。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { jsxElements, parseModule } from "../../test-support/semantic-source.mjs";


const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app");
const CSS = await readFile(path.join(APP_DIR, "globals.css"), "utf8");

// 控件被嵌进问答输入行后所处的祖先规则。要压过它,控件规则必须比 (0,1,1) 更具体。
const AMBIENT_SELECTOR = ".chat-input-bar span";


/**
 * 计算一条选择器的特异性 [id, class, type]。只识别本仓库实际用到的形态;遇到无法
 * 归类的 token 就抛错,而不是当作 0 —— 静默低估会让守卫变成假绿。
 */
function specificity(selector) {
  let id = 0;
  let cls = 0;
  let type = 0;
  const tokens = selector
    .trim()
    .split(/[\s>+~]+/)
    .filter(Boolean);
  for (const token of tokens) {
    // 伪元素(::before)按 type 计,伪类(:hover/:disabled/:not 的括号先剥掉)按 class 计。
    let rest = token.replace(/::[a-z-]+/g, () => { type += 1; return ""; });
    rest = rest.replace(/:not\(([^)]*)\)/g, (_, inner) => {
      const [, innerCls, innerType] = specificity(inner);
      cls += innerCls;
      type += innerType;
      return "";
    });
    rest = rest.replace(/:[a-z-]+(\([^)]*\))?/g, () => { cls += 1; return ""; });
    rest = rest.replace(/\[[^\]]*\]/g, () => { cls += 1; return ""; });
    rest = rest.replace(/#[A-Za-z0-9_-]+/g, () => { id += 1; return ""; });
    rest = rest.replace(/\.[A-Za-z0-9_-]+/g, () => { cls += 1; return ""; });
    rest = rest.replace(/^\*/, "");
    if (rest === "") continue;
    assert.match(rest, /^[A-Za-z][A-Za-z0-9-]*$/, `unparsed selector token in "${selector}": "${rest}"`);
    type += 1;
  }
  return [id, cls, type];
}


function outranks(left, right) {
  for (let i = 0; i < 3; i += 1) {
    if (left[i] !== right[i]) return left[i] > right[i];
  }
  return false;
}


/** 粗粒度切出顶层规则块:本文件没有 @media 嵌套包裹档位控件,够用。 */
function cssRules(css) {
  const rules = [];
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(withoutComments)) !== null) {
    const selectorList = match[1].trim();
    if (selectorList.startsWith("@")) continue;
    rules.push({ selectors: selectorList.split(",").map((s) => s.trim()), body: match[2] });
  }
  return rules;
}


const picker = await parseModule("effort-picker.tsx");
const SPAN_CLASSES = new Set(
  jsxElements(picker, "span")
    .map((element) => element.attributes?.className)
    .filter((name) => typeof name === "string" && name.length > 0),
);


test("the guard sees the ambient composer rule it is defending against", () => {
  // 哨兵:祖先规则若被改名/删除,下面的断言会变成空转真空过。
  const ambient = cssRules(CSS).filter((rule) => rule.selectors.includes(AMBIENT_SELECTOR));
  assert.equal(ambient.length, 1, `expected exactly one "${AMBIENT_SELECTOR}" rule`);
  assert.match(ambient[0].body, /color\s*:/);
  assert.match(ambient[0].body, /font-size\s*:/);
  assert.deepEqual(specificity(AMBIENT_SELECTOR), [0, 1, 1]);
});


test("picker span rules outrank the composer ambient rule", () => {
  assert.ok(SPAN_CLASSES.size >= 3, `expected the picker to render spans, saw ${SPAN_CLASSES.size}`);
  const ambient = specificity(AMBIENT_SELECTOR);
  const checked = [];

  for (const rule of cssRules(CSS)) {
    if (!/(?:^|[\s;])(?:color|font-size)\s*:/.test(rule.body)) continue;
    for (const selector of rule.selectors) {
      const last = selector.split(/[\s>+~]+/).filter(Boolean).pop() ?? "";
      const target = [...SPAN_CLASSES].find((name) => last.includes(`.${name}`));
      if (!target) continue;
      checked.push(selector);
      assert.ok(
        outranks(specificity(selector), ambient),
        `"${selector}" (${specificity(selector)}) must outrank "${AMBIENT_SELECTOR}" (${ambient})`
        + " — otherwise the Ask composer repaints the shared picker and the two call sites diverge",
      );
    }
  }

  // 正向下限:上面的循环若因类名改动而一条都没匹配上,断言会全部跳过而静默通过。
  assert.ok(checked.length >= 4, `expected to check picker span rules, checked ${checked.length}`);
});
