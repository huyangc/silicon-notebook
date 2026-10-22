// 主界面外壳 `.app` 的列定义与笔记本列表工具条的换行守卫。
//
// 起因(2026-10-22):笔记本列表页在 1024、768、375 宽下都横向溢出到 1102px。`.app` 是
// `display: grid`,只写了行、没写列,于是唯一的一列是隐式的 `auto`。`auto` 列不会窄于
// 子项的 min-content;而 `.notebook-grid` 是 `repeat(auto-fill, minmax(250px, 1fr))` 加
// `max-width: 1200px`,算固有尺寸时 auto-fill 按 1200 定出 4 条轨道,min-content 就是
// 4×250 + 3×18 = 1054px。外壳列被撑到 1054 + 48(`.page` 左右留白)= 1102px,顶栏右侧的
// 状态灯/铃铛/账户和工具条上的「新建」被推出视口。卡片里的长名称早有
// `overflow-wrap: anywhere`,不是元凶(Browser pane 实测:注入 minmax(0, 1fr) 后三个宽度
// 都是 scrollWidth == clientWidth)。
//
// 外壳列一旦按视口宽度排版,工具条和窄屏顶栏就第一次真正「放不下」,于是另外两处一直被
// 1102px 掩盖的版式问题露了出来,本守卫一并钉住:
//   - 761~850px(以及搜索框展开时)工具条一行放不下,按钮被挤到字折行(「全/部」「最/近」);
//   - ≤760px 顶栏本来就设计成可换行,但外壳第一行定死 56px,换到第二行的入口溢出顶栏、
//     压在页面上(群组页上直接压住「返回主页」和「新建群组」)。
//
// 判据全部是语义的(PostCSS 规则/声明),不含行号;jsdom 不做 grid/flex 布局,量出来的
// rect 恒为 0,所以这些约束只能在样式表里钉。覆盖边界:本文件只证明形态,像素结果由
// Browser pane 实测(PR 描述里有三个宽度的前后数字)。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import postcss from "postcss";

const css = await readFile(new URL("../../app/globals.css", import.meta.url), "utf8");
const stylesheet = postcss.parse(css);

const NARROW_MEDIA = "(max-width: 760px)";

function normalize(value) {
  return value.replace(/\s+/g, " ").trim();
}

/** 规则所在的作用域:顶层为 "",@media 内为其条件。 */
function scopeOf(rule) {
  return rule.parent?.type === "atrule" ? normalize(rule.parent.params) : "";
}

/** 选择器列表里含 `selector` 这一项的全部规则。 */
function rulesFor(selector) {
  const found = [];
  stylesheet.walkRules((rule) => {
    if (rule.selectors.map(normalize).includes(selector)) found.push(rule);
  });
  return found;
}

/** 某作用域下某选择器最终声明的某属性值(同作用域内后写的赢)。 */
function declared(selector, property, scope = "") {
  let value;
  for (const rule of rulesFor(selector)) {
    if (scopeOf(rule) !== scope) continue;
    rule.walkDecls(property, (decl) => { value = normalize(decl.value); });
  }
  return value;
}

test(".app 的唯一一列写成 minmax(0, 1fr),不让子项的 min-content 撑宽外壳", () => {
  assert.equal(
    declared(".app", "grid-template-columns"),
    "minmax(0, 1fr)",
    "隐式 auto 列会被 .notebook-grid 的 1054px min-content 撑到 1102px,整页横向溢出",
  );
});

test("任何作用域里都不许把 .app 的列改回不以 0 为下限的写法", () => {
  // 例如在某个 @media 里写 `grid-template-columns: 1fr` —— `1fr` 的下限是 auto,
  // 等于把同一个 bug 换个断点放回来。
  stylesheet.walkRules((rule) => {
    if (!rule.selectors.map(normalize).some((one) => one === ".app" || one.startsWith(".app."))) return;
    rule.walkDecls("grid-template-columns", (decl) => {
      assert.match(
        normalize(decl.value),
        /^minmax\(0(?:px)?, /,
        `${rule.selector}${scopeOf(rule) ? ` @media ${scopeOf(rule)}` : ""} 的列没有 0 下限`,
      );
    });
  });
});

test("不靠在外壳上裁掉溢出来掩盖问题", () => {
  // 修的是根因(列的下限)。在 html/body/.app/.page 上写 overflow-x: hidden 或 clip,
  // 真正放不下的内容会被静默裁掉、用户连滚动都滚不到。
  for (const selector of ["html", "body", ".app", ".page", ".collection-view"]) {
    for (const rule of rulesFor(selector)) {
      rule.walkDecls(/^overflow(?:-x)?$/, (decl) => {
        assert.doesNotMatch(
          normalize(decl.value),
          /\b(?:hidden|clip)\b/,
          `${selector} 上的 ${decl.prop}: ${decl.value} 会把溢出裁掉而不是修好`,
        );
      });
    }
  }
});

test("工具条放不下一行时整组换行,而不是把按钮挤到字折行", () => {
  assert.equal(declared(".library-toolbar", "flex-wrap"), "wrap");
  // flex-basis 为 0:换行判定按动作组的 min-content(搜索框取下限),1024 宽展开搜索
  // 时仍留在同一行;同一行时占满剩余宽度、按钮贴右,与原先 space-between 观感一致。
  assert.equal(declared(".library-actions", "flex"), "1 1 0");
  assert.equal(declared(".library-actions", "justify-content"), "flex-end");
  assert.equal(declared(".library-toolbar .tab", "white-space"), "nowrap");
  assert.equal(declared(".library-actions button", "white-space"), "nowrap");
  // 三个标签在 320px 这种极窄屏上可以换到第二行,但不许溢出。
  assert.equal(declared(".tabs", "flex-wrap"), "wrap");
});

test("窄屏堆叠布局里动作组靠左,不继承宽屏的贴右", () => {
  assert.equal(declared(".library-toolbar", "display", NARROW_MEDIA), "grid");
  assert.equal(declared(".library-actions", "justify-content", NARROW_MEDIA), "flex-start");
});

test("窄屏顶栏换行时外壳第一行跟着长高,入口不溢出顶栏", () => {
  // 前提:窄屏顶栏本身是 height: auto + 可换行。前提变了,这一条要跟着重估。
  assert.equal(declared(".topbar", "height", NARROW_MEDIA), "auto");
  assert.equal(declared(".topbar", "flex-wrap", NARROW_MEDIA), "wrap");
  const rows = declared(".app", "grid-template-rows", NARROW_MEDIA);
  assert.ok(rows, "窄屏缺 .app 的行定义,第一行仍是定死的 56px");
  assert.match(rows, /^minmax\(56px, auto\) /, "第一行必须能随顶栏换行长高");
  // 工作区不显示顶栏,它的单行定义必须仍然压过上面那条(两类选择器 > 一类)。
  assert.equal(declared(".app.workspace-mode", "grid-template-rows"), "1fr");
});
