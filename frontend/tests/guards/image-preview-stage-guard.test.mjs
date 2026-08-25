import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";

// 2026-08-25 用户反馈:点开引用附图,图片贴在屏幕左上角而不是正中。
//
// 数据没问题,声明的 CSS 也「看起来」是对的——`.answer-image-preview-zoom-content`
// 本来就写着 `display:grid; place-items:center; width/height:100%`。坏在**优先级**:
// react-zoom-pan-pinch 把自己那份 CSS module(`.transform-component-module_content__*`,
// 含 `display:flex` + `width/height: fit-content`)在运行时 `head.appendChild` 进
// <head> 末尾。两边同为单类选择器、特指度相等,排在后面的库样式赢,于是内容盒退化成
// 图片自身大小;再配上库固定的 `transform-origin: 0% 0%` 与初始 translate(0,0),图片
// 就钉死在舞台左上角。真机量过(1280x720):content 盒 640x400、rect 落在 (24,24)。
//
// jsdom 没有排版引擎也不会注入那份库样式,组件测试量不到这件事,所以只能在 CSS 里钉。
// 同一条理由早就把 `.answer-image-preview-zoom` 的 width/height/overflow 写成
// !important 了——这条守卫把两处一起钉住,省得下一个人把它当「多余的 !important」清掉。

const css = readFileSync(
  fileURLToPath(new URL("../../app/globals.css", import.meta.url)),
  "utf8",
);

/** 取一条规则的声明块(只认顶层的 `选择器 { ... }`)。 */
function block(selector) {
  const start = css.indexOf(`\n${selector} {`);
  assert.notEqual(start, -1, `globals.css 里找不到 ${selector} —— 图片舞台被搬走了?`);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  assert.ok(close > open, `${selector} 的声明块没闭合`);
  return css.slice(open + 1, close);
}

test("缩放内容盒必须以 !important 铺满舞台并居中(否则图片回到左上角)", () => {
  const rule = block(".answer-image-preview-zoom-content");
  assert.match(rule, /display:\s*grid\s*!important/, "库的 display:flex 会赢");
  assert.match(rule, /width:\s*100%\s*!important/, "库的 width:fit-content 会赢");
  assert.match(rule, /height:\s*100%\s*!important/, "库的 height:fit-content 会赢");
  assert.match(rule, /place-items:\s*center/, "铺满了但不居中,图片仍旧贴在左上角");
});

test("缩放外框保持 !important 的尺寸与裁剪(舞台=可平移范围的前提)", () => {
  const rule = block(".answer-image-preview-zoom");
  assert.match(rule, /width:\s*100%\s*!important/);
  assert.match(rule, /height:\s*100%\s*!important/);
  assert.match(rule, /overflow:\s*hidden\s*!important/);
});

test("到头/到尾的切换按钮必须看得出来按不动", () => {
  // 这颗按钮自己写死了 background/color,浏览器默认的 :disabled 变灰对它无效
  // (与 .icon-button/.sort-button/.ghost-button 同一条既有理由)。
  const rule = block(".answer-image-preview-step:disabled");
  assert.match(rule, /opacity:\s*0?\.\d+/, "只挂 disabled 而不变灰,按钮看起来还是可点的");
});
