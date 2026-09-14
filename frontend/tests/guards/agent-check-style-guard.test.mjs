// 勾选框样式归属守卫。
//
// 来源是一次真实回归:「Agent 接入」从私有记忆页迁到独立的 /agents 页时,`.agent-check`
// 这条勾选框样式跟着组件一起搬进了 agent-access.css。可首页上仍有三处勾选框用这个类名
// ——记忆审核与保存预览里的「同时整理进知识图谱」(memory-panel.tsx)、以及 memory-panel
// 渲染的 transfer-picker.tsx——而 agent-access.css 只随 /agents 页加载,首页永远拿不到,
// 于是这些勾选框静默丢了 flex 布局、间距与字号。testing-library 只看 DOM 与可访问名字,
// `tsc` 不检查 className 字符串,没有既有门禁会红。
//
// 判据:每个模块里 `<label>` 上出现的勾选框 class,都必须在**该模块实际随之加载**的样式
// 表里有类选择器规则。模块→样式表的对应关系写在下面的表里;新增一处勾选框用了别处的类名
// 就会在这里报红。
//
// 覆盖边界(如实说明):只核对这三个模块 `<label>` 上的静态 class 是否有规则,不检查
// 具体数值,也不覆盖别的元素或别的页面。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app");

// 模块 → 它在页面上实际随之加载的样式表。transfer-picker 没有自己的样式表,它只在
// memory-panel 里渲染,吃的是 memory-panel.css。
const MODULE_STYLESHEETS = [
  { module: "memory-panel.tsx", stylesheet: "memory-panel.css" },
  { module: "transfer-picker.tsx", stylesheet: "memory-panel.css" },
  { module: "agent-access-manager.tsx", stylesheet: "agent-access.css" },
];

async function stylesheetRules(name) {
  // 样式表没有可消费的 AST,文本是唯一诚实的输入;注释先剥掉,免得注释里写过的类名冒充规则。
  const css = (await readFile(path.join(APP_DIR, name), "utf8")).replace(/\/\*[\s\S]*?\*\//g, "");
  return (className) => new RegExp(`\\.${className}(?![\\w-])[^{}]*\\{`).test(css);
}

test("checkbox label classes are styled by the stylesheet each module actually loads", async () => {
  for (const { module, stylesheet } of MODULE_STYLESHEETS) {
    const parsed = await parseModule(module);
    const hasRule = await stylesheetRules(stylesheet);
    const classNames = new Set(
      jsxElements(parsed, "label")
        .map((element) => element.attributes.className)
        .filter((value) => typeof value === "string" && /check/.test(value))
        .flatMap((value) => value.split(/\s+/).filter(Boolean)),
    );
    assert.ok(classNames.size > 0, `${module}: expected at least one checkbox label class`);
    for (const className of classNames) {
      assert.ok(hasRule(className), `${module}: .${className} has no rule in ${stylesheet}`);
    }
  }
});
