// 回归门:任何把 rehype-katex 接进 Markdown 管线的模块,必须自己带上 KaTeX 样式表。
//
// 真机踩过(公开分享页 `app/r/[token]/page.tsx`):它是应用里唯一不经 `app/page.tsx`
// 的界面,漏了 `import "katex/dist/katex.min.css"` 之后没有任何报错——rehype-katex
// 照常产出 `.katex-mathml` + `.katex-html` 两份标记,而裁掉 MathML 那一份的规则在
// 样式表里。结果是每条公式在页面上出现两遍,其中一遍是逐字符散开的 MathML 文本。
//
// 判据刻意只钉 rehype-katex(Markdown 管线 = 一个自成一体的渲染面),不钉直接调用
// `katex.renderToString` 的组件(`answer-panel.tsx` / `formula-view.tsx`):后者永远
// 挂在已经引入样式表的 `app/page.tsx` 之下,要求它们再引一次没有信息量。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { appSourceModules } from "./test/semantic-source.mjs";

const KATEX_STYLESHEET = "katex/dist/katex.min.css";

/** 模块里全部 import 声明的说明符——含 `import "x.css"` 这种无绑定的副作用导入。 */
function moduleSpecifiers(sourceFile) {
  return sourceFile.statements
    .filter((statement) => (
      ts.isImportDeclaration(statement)
      && ts.isStringLiteral(statement.moduleSpecifier)
    ))
    .map((statement) => statement.moduleSpecifier.text);
}

test("接入 rehype-katex 的模块都自带 KaTeX 样式表", async () => {
  const surfaces = [];
  const missing = [];
  for (const { path, module } of await appSourceModules()) {
    const specifiers = moduleSpecifiers(module);
    if (!specifiers.includes("rehype-katex")) continue;
    surfaces.push(path);
    if (!specifiers.includes(KATEX_STYLESHEET)) missing.push(path);
  }

  // 守卫自身的空转保护:一旦没有任何模块被扫到,上面的循环恒过。
  assert.ok(surfaces.length >= 3, `扫到的 KaTeX 渲染面太少: ${surfaces.join(", ")}`);
  assert.deepEqual(missing, []);
});
