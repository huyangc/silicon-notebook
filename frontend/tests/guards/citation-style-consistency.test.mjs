import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import postcss from "postcss";

const stylesheet = postcss.parse(await readFile(new URL("../../app/globals.css", import.meta.url), "utf8"));

test("公开原文与笔记本引用卡的操作按钮共用外观规则", () => {
  const sharedProperties = new Set();
  stylesheet.walkRules((rule) => {
    if (!rule.selectors.includes(".cite-detail-action") || !rule.selectors.includes(".cite-detail-head button")) return;
    rule.walkDecls((declaration) => sharedProperties.add(declaration.prop));
  });
  for (const property of ["border", "border-radius", "background", "color", "font-size", "padding"]) {
    assert.ok(sharedProperties.has(property), `${property} 应来自相同的引用操作规则`);
  }
});

test("公开证据区域引用的设计变量都已定义", () => {
  const defined = new Set();
  stylesheet.walkDecls((declaration) => {
    if (declaration.prop.startsWith("--")) defined.add(declaration.prop);
  });
  let checked = 0;
  stylesheet.walkRules((rule) => {
    if (!rule.selectors.some((selector) => /\.(?:public-evidence|cite-detail-context)/.test(selector))) return;
    rule.walkDecls((declaration) => {
      for (const [, variable] of declaration.value.matchAll(/var\(\s*(--[\w-]+)/g)) {
        checked++;
        assert.ok(defined.has(variable), `${rule.selector} 使用了未定义的 ${variable}`);
      }
    });
  });
  assert.ok(checked > 0);
});
