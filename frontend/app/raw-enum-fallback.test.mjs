// 守卫:「兜底即原值」—— 查表查不到时退回原始 key,等于把后端的英文 enum id
// 直接渲染给用户。vocabulary.ts 的 `label(map, value, fallback)` 就是为了让这个
// bug 写不出来(签名强制传一个中性兜底词);本文件负责拦住绕开它的写法。
//
// ── 为什么从正则换成 TypeScript AST(#296 第二轮评审阻塞 3)────────────────
// 上一版是 Python 侧的正则。评审用守卫自己的函数跑了五条最小探针,同时暴露漏报
// 和误报:
//
//   写法                                     正则    应当
//   STATUS_LABELS?.[s] ?? s                  放行    抓(optional chaining 漏报)
//   vocabulary.label(STATUS_LABELS, s, s)    放行    抓(兜底传原值,废掉 label 签名)
//   getLabels()[s] ?? s                      放行    抓(调用表达式漏报)
//   ALIASES[value] ?? value                  抓      放行(纯内部归一化,误报)
//   STATUS_LABELS[s] ?? s                    抓      抓(基线)
//
// 前三条是正则写法枚举不全,还能补;第四条补不了——`ALIASES[v] ?? v` 与
// `STATUS_LABELS[s] ?? s` 在**语法形状上完全一致**,区别只在「这个值会不会上屏」。
// 正则看不见上下文,所以这不是把正则写好一点的问题。AST 能拿到上下文,故改用
// typescript 的 compiler API(已在 devDependencies)。
//
// ── 三条规则 ────────────────────────────────────────────────────────────
// R1 无条件抓 `label(m, x, x)`:label() 本身就是词汇边界,把取值表达式原样当兜底
//    传进去,是在明确地废掉那个签名。与上下文无关。
// R2 无条件抓 `M[x] ?? x`,当 M 的名字里含 label(大小写不敏感):一张自称
//    「…LABELS」的表按定义就是显示词表,它的兜底不该是原值。`getLabels()[s]` 走
//    被调函数名,`vocab.STATUS_LABELS[s]` 走属性名。
//    ——评审建议只留 JSX 上下文一条;但那样基线行 `STATUS_LABELS[s] ?? s` 写在
//    JSX 外就会被放行,比现状还弱。R2 补上这个缺口,同时不碰 ALIASES 这类名字。
// R3 在**用户可见的渲染位置**抓 `M[x] ?? x`(任意 M):表达式位于 JSX 子表达式
//    `{…}`,或位于用户可见的 JSX 属性里。属性侧用「内部属性反白名单」而非
//    「可见属性白名单」:可见的属性列不全(自定义组件的 tooltip / hint / emptyText
//    都会上屏),而 className / key / style / data-* / on* 这些**确定不上屏**的
//    是可枚举的。宁可偶尔误报(加个中性兜底词即可消解),不要漏报。
//
// 取值形状统一按 ElementAccessExpression 认,所以 `M[x]` / `M?.[x]` / `f()[x]`
// 天然同款,不必逐个枚举写法。`M.x ?? x` 不在内:那是静态属性,不是枚举查表。
//
// ── 覆盖不到的,如实承认(不假装全覆盖)────────────────────────────────
//  * 先在 helper / 变量里算好再渲染:`const t = M[x] ?? x; return <span>{t}</span>`。
//    要抓它得做跨语句数据流分析,超出 lint 的范畴。
//  * 非 JSX 的上屏出口:`alert(M[x] ?? x)`、`setToast(...)`、`window.confirm(...)`。
//    枚举 sink 会变成另一份永远追不齐的白名单。
//  * 运行时才拼出来的键。
// 这几条与评审这轮接受的「裸节点/边不做全局 lint」是同一判断:诚实标注 >
// 强行匹配制造假红。真正的兜底是 label() 的签名本身——它让正确写法比错误写法更
// 顺手,而不是靠 lint 抓完所有绕过。
//
// ── 为什么落在 .test.mjs 而不是 Python 守卫里 ──────────────────────────
// AST 在 JS 侧(typescript 包),Python 侧要用就得 spawn node 子进程,于是
// `python scripts/check_ui_vocabulary.py` 这条独立命令会多出 node +
// frontend/node_modules 两个前置依赖——它现在是零依赖的。
// `npm run test` 用 `find app -name '*.test.mjs'` 递归收集,scripts/check.sh 跑
// `npm run test`,所以这个文件照样是 check.sh 的硬门。分家后两侧职责也更清楚:
// Python 守卫管**渲染文本**里的词,这里管**代码形状**。
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ts = require("typescript");

const APP_DIR = path.dirname(fileURLToPath(import.meta.url));

// 确定**不会**被用户读到的 JSX 属性。见上文 R3:这里是反白名单,列不全的那一侧
// 是「可见属性」,所以枚举不可见的这一侧。aria-* 刻意不在内(读屏会念出来)。
const INTERNAL_ATTRS = new Set([
  "className", "class", "key", "ref", "style", "id", "htmlFor",
  "href", "src", "srcSet", "type", "role", "name", "target", "rel",
  "method", "action", "form", "tabIndex", "width", "height",
  "color", "fill", "stroke", "viewBox", "d", "transform", "xmlns",
]);

const isInternalAttr = (attr) =>
  attr.startsWith("data-") || /^on[A-Z]/.test(attr) || INTERNAL_ATTRS.has(attr);

function unwrap(node) {
  while (node && ts.isParenthesizedExpression(node)) node = node.expression;
  return node;
}

/** 标识符 / 属性名 / 被调函数名 —— 拿不到就是 null。 */
function nameOf(expr) {
  const e = unwrap(expr);
  if (!e) return null;
  if (ts.isIdentifier(e)) return e.text;
  if (ts.isPropertyAccessExpression(e)) return e.name.text;
  if (ts.isCallExpression(e)) return nameOf(e.expression);
  return null;
}

const norm = (node, sf) => node.getText(sf).replace(/\s+/g, "");

/**
 * 表达式所处的用户可见渲染位置,不在渲染位置则 null。
 *
 * 往上找最近的 JsxExpression:作为 JSX 子表达式 `{…}` 一律算渲染;作为属性值则
 * 看属性名。刻意**不**在函数边界停下——`{items.map((i) => M[i] ?? i)}` 里箭头
 * 函数的返回值确实会上屏。
 */
function renderPosition(node, sf) {
  for (let n = node.parent; n; n = n.parent) {
    if (!ts.isJsxExpression(n)) continue;
    const parent = n.parent;
    if (parent && ts.isJsxAttribute(parent)) {
      const attr = parent.name.getText(sf);
      return isInternalAttr(attr) ? null : `JSX 属性 ${attr}`;
    }
    return "JSX 文本";
  }
  return null;
}

/** 源码里所有「兜底即原值」的站点。 */
export function scanRawEnumFallback(code, fileName = "sample.tsx") {
  const sf = ts.createSourceFile(
    fileName,
    code,
    ts.ScriptTarget.Latest,
    /* setParentNodes */ true,
    fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
  const hits = [];
  const record = (node, rule) => {
    const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
    hits.push({ line: line + 1, rule, snippet: node.getText(sf).replace(/\s+/g, " ") });
  };

  const visit = (node) => {
    // R1 —— label(map, x, x)
    if (
      ts.isCallExpression(node) &&
      nameOf(node.expression) === "label" &&
      node.arguments.length === 3 &&
      norm(node.arguments[1], sf) === norm(node.arguments[2], sf)
    ) {
      record(node, "label() 的兜底传了取值表达式本身");
    }

    // R2 / R3 —— M[x] ?? x、M[x] || x(?. 与 f() 同款,都是 ElementAccess)
    if (
      ts.isBinaryExpression(node) &&
      (node.operatorToken.kind === ts.SyntaxKind.QuestionQuestionToken ||
        node.operatorToken.kind === ts.SyntaxKind.BarBarToken)
    ) {
      const left = unwrap(node.left);
      if (left && ts.isElementAccessExpression(left) && left.argumentExpression) {
        const sameKey = norm(left.argumentExpression, sf) === norm(unwrap(node.right), sf);
        if (sameKey) {
          const mapName = nameOf(left.expression) ?? "";
          const where = renderPosition(node, sf);
          if (/label/i.test(mapName)) {
            record(node, `显示词表 ${mapName} 的兜底即原值`);
          } else if (where) {
            record(node, `${where} 里的查表兜底即原值`);
          }
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return hits;
}

const scan = (code, name) => scanRawEnumFallback(code, name).map((h) => h.rule);

// --------------------------------------------------------------------------
// 1. 评审那五条探针 —— 必须逐条对上「应当」列
// --------------------------------------------------------------------------

const PROBE = [
  ["STATUS_LABELS?.[s] ?? s", true, "optional chaining"],
  ["vocabulary.label(STATUS_LABELS, s, s)", true, "兜底传原值,废掉 label 签名"],
  ["getLabels()[s] ?? s", true, "调用表达式"],
  ["ALIASES[value] ?? value", false, "纯内部归一化,不上屏"],
  ["STATUS_LABELS[s] ?? s", true, "基线,证明检查非恒空"],
];

for (const [expr, shouldFlag, why] of PROBE) {
  test(`探针:${expr} → ${shouldFlag ? "抓" : "放行"}(${why})`, () => {
    const hits = scan(`const x = ${expr};`);
    assert.equal(
      hits.length > 0,
      shouldFlag,
      `期望${shouldFlag ? "抓到" : "放行"},实际 ${JSON.stringify(hits)}`,
    );
  });
}

// --------------------------------------------------------------------------
// 2. 正例
// --------------------------------------------------------------------------

test("旧正则已能抓到的写法一条都不许退化", () => {
  // 这批来自上一版 Python 自测,换实现后必须继续红。
  for (const code of [
    "const a = RELATION_LABELS[edge.edge_type] ?? edge.edge_type;",
    "const b = FIELD_LABELS[key] ?? key;",
    "const c = STATUS_LABELS[s] || s;",
    "const d = label(TIER, tier, tier);",
    "const e = <span>{MAP[item.status] ?? item.status}</span>;",
    "const f = vocab.STATUS_LABELS[s] ?? s;",
  ]) {
    assert.ok(scan(code).length > 0, `没抓到:${code}`);
  }
});

test("渲染位置:JSX 子表达式与用户可见属性都抓,任意表名", () => {
  for (const code of [
    "const a = <span>{MAP[s] ?? s}</span>;",
    'const b = <span title={MAP[s] ?? s} />;',
    "const c = <input placeholder={MAP[s] ?? s} />;",
    'const d = <div aria-label={MAP[s] ?? s} />;',
    // 自定义组件的文案 prop 列不全,故按反白名单放行不了它 —— 这正是要的行为
    "const e = <Badge tooltip={MAP[s] ?? s} />;",
    // 箭头函数的返回值照样上屏,往上找不因函数边界停下
    "const f = <ul>{list.map((i) => <li key={i}>{MAP[i] ?? i}</li>)}</ul>;",
  ]) {
    assert.ok(scan(code, "x.tsx").length > 0, `没抓到:${code}`);
  }
});

test("空白差异不影响判等 —— 别靠逐字相同蒙混", () => {
  assert.ok(scan("const a = <b>{MAP[ item.s ] ?? item.s}</b>;", "x.tsx").length > 0);
  assert.ok(scan("const b = label(TIER, o.tier , o.tier);").length > 0);
});

// --------------------------------------------------------------------------
// 3. 反例
// --------------------------------------------------------------------------

test("正当兜底不误报", () => {
  for (const code of [
    // 退到中性词而非原值 —— 这就是 label() 想要的写法
    'const a = RELATION_LABELS[edge.edge_type] ?? "关联";',
    'const b = label(TIER, tier, "未知来源");',
    'const c = MAP[key] ?? "";',
    // 经评审的透出路径:自定义 object_type 原样显示(用户自己起的名字)。
    // 写成 Object.hasOwn 三元而非 ??,顺带免疫原型链白屏。
    "const d = Object.hasOwn(KG_TYPE_LABELS, type) ? KG_TYPE_LABELS[type] : type;",
    "const e = Object.hasOwn(FIELD_LABELS, key) ? FIELD_LABELS[key] : key;",
    // 键与兜底不是同一表达式
    "const f = MAP[a] ?? b;",
    "const g = label(TIER, tier, other);",
    // 注释里的反面教材不该让构建失败(AST 天然不把注释当表达式)
    "// 别写 MAP[x] ?? x\nconst h = 1;",
    "/** @example STATUS_LABELS[s] ?? s */\nconst i = 2;",
    // 静态属性不是枚举查表
    "const j = obj.status ?? status;",
    // 参数个数不对,不是 label(map, value, fallback)
    "const k = label(TIER, tier);",
  ]) {
    assert.deepEqual(scan(code), [], `误报:${code}`);
  }
});

test("纯内部逻辑不误报 —— 误报正是上一版最难的那条", () => {
  for (const code of [
    // 归一化:查不到就用原值,是正确语义,而且从不上屏
    "const a = ALIASES[value] ?? value;",
    "function canon(t) { return TYPE_ALIASES[t] ?? t; }",
    "const b = OVERRIDES[id] || id;",
    // 内部属性:className / key / style / data-* / on* 都不上屏
    "const c = <div className={STYLE_MAP[t] ?? t} />;",
    "const d = <div key={KEY_MAP[t] ?? t} />;",
    "const e = <div data-kind={MAP[t] ?? t} />;",
    "const f = <button onClick={() => pick(MAP[t] ?? t)} />;",
  ]) {
    assert.deepEqual(scan(code, "x.tsx"), [], `误报:${code}`);
  }
});

test("含 label 的表名在任何位置都抓,哪怕不在 JSX 里", () => {
  // R2 存在的理由:只按 JSX 上下文判,基线行写在 helper 里就漏了。
  assert.ok(scan("function f(s) { return STATUS_LABELS[s] ?? s; }").length > 0);
  assert.deepEqual(scan("function g(v) { return ALIASES[v] ?? v; }"), []);
});

// --------------------------------------------------------------------------
// 4. 端到端:真实 frontend/app 是干净的
// --------------------------------------------------------------------------

function appSources() {
  return readdirSync(APP_DIR, { recursive: true, encoding: "utf8" })
    .filter((rel) => /\.tsx?$/.test(rel) && !rel.includes(".test.") && !rel.endsWith(".d.mts"))
    .map((rel) => path.join(APP_DIR, rel));
}

test("真实 frontend/app 源码没有兜底即原值", () => {
  const files = appSources();
  // 非空性:递归收集一旦写坏(比如 recursive 选项失效),扫 0 个文件也会「全绿」。
  assert.ok(files.length > 40, `只收集到 ${files.length} 个源文件,扫描面塌了`);
  const offenders = [];
  for (const file of files) {
    for (const hit of scanRawEnumFallback(readFileSync(file, "utf8"), file)) {
      offenders.push(`${path.relative(APP_DIR, file)}:${hit.line}  ${hit.rule}  ${hit.snippet}`);
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "查表兜底成了原值,后端每加一个枚举值就把英文 id 渲染给用户。\n" +
      "改用 vocabulary.ts 的 label(MAP, value, '中性兜底')——签名强制传兜底词。\n" +
      offenders.join("\n"),
  );
});

test("扫描器在真实源码上确实跑到了 JSX 与 label() 两条路径", () => {
  // 防「实现整体失效但恰好全绿」:拿真实 page.tsx 做变异,注入一处必须被抓到。
  const page = readFileSync(path.join(APP_DIR, "page.tsx"), "utf8");
  assert.deepEqual(scanRawEnumFallback(page, "page.tsx"), []);
  const mutated = page.replace(
    "<span title={UNINDEXED_SCOPE_HINT}>",
    "<span title={PARSE_STATUS[s.parse_status] ?? s.parse_status}>",
  );
  assert.notEqual(mutated, page, "变异锚点没找到,page.tsx 结构变了");
  assert.ok(
    scanRawEnumFallback(mutated, "page.tsx").length > 0,
    "往真实 page.tsx 注入兜底即原值后仍不报 —— 扫描器在真实文件上没生效",
  );
});
