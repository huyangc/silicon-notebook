import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import postcss from "postcss";

const css = await readFile(new URL("../../app/globals.css", import.meta.url), "utf8");
const stylesheet = postcss.parse(css);

const visibleSelector = ".workspace-grid:has(> .workspace-extension-outlet-workspace-side_panel)";
const collapsedSelector = ".workspace-grid.sources-collapsed:has(> .workspace-extension-outlet-workspace-side_panel)";

function exactRules(container, selectors, direct = false) {
  const rules = [];
  const consider = (rule) => {
    if (
      rule.selectors.length === selectors.length
      && rule.selectors.every((selector, index) => selector === selectors[index])
    ) rules.push(rule);
  };
  if (direct) {
    for (const node of container.nodes ?? []) {
      if (node.type === "rule") consider(node);
    }
  } else {
    container.walkRules(consider);
  }
  return rules;
}

function exactRule(container, selectors, direct = false) {
  const matches = exactRules(container, selectors, direct);
  assert.equal(matches.length, 1, `expected one exact CSS rule for ${selectors.join(", ")}`);
  return matches[0];
}

function declarationMap(rule) {
  const declarations = rule.nodes?.filter((node) => node.type === "decl") ?? [];
  assert.equal(declarations.length, rule.nodes?.length ?? 0, "layout rule must contain declarations only");
  return Object.fromEntries(declarations.map((declaration) => [declaration.prop, declaration.value]));
}

function sidePanelMedia(maxWidth) {
  const matches = [];
  for (const atRule of stylesheet.nodes ?? []) {
    if (atRule.type !== "atrule" || atRule.name !== "media") continue;
    const containsVisibleSelector = (atRule.nodes ?? []).some((node) => (
      node.type === "rule" && node.selectors.includes(visibleSelector)
    ));
    if (atRule.params === `(max-width: ${maxWidth}px)` && containsVisibleSelector) {
      matches.push(atRule);
    }
  }
  assert.equal(matches.length, 1, `expected one side-panel media block at ${maxWidth}px`);
  return matches[0];
}

test("visible side contribution gets an explicit third desktop column", () => {
  assert.deepEqual(declarationMap(exactRule(stylesheet, [visibleSelector], true)), {
    "grid-template-columns": "minmax(270px, 25%) minmax(190px, 18%) minmax(0, 1fr)",
  });
  assert.deepEqual(declarationMap(exactRule(stylesheet, [collapsedSelector], true)), {
    "grid-template-columns": "0 minmax(190px, 18%) minmax(0, 1fr)",
  });
});

test("mobile contribution layout collapses both exact selectors back to one column", () => {
  const mobile = sidePanelMedia(760);
  assert.deepEqual(declarationMap(exactRule(mobile, [visibleSelector, collapsedSelector], true)), {
    "grid-template-columns": "1fr",
  });
});

test("medium workspace preserves all three visible and collapsed columns", () => {
  const medium = sidePanelMedia(1100);
  assert.deepEqual(declarationMap(exactRule(medium, [visibleSelector], true)), {
    "grid-template-columns": "250px 180px minmax(0, 1fr)",
  });
  assert.deepEqual(declarationMap(exactRule(medium, [collapsedSelector], true)), {
    "grid-template-columns": "0 180px minmax(0, 1fr)",
  });
});
