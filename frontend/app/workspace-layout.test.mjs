import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const css = await readFile(new URL("./globals.css", import.meta.url), "utf8");

test("workspace uses a two-column source and ask layout without the inactive Studio sidebar", () => {
  assert.ok(page.includes('className="workspace-grid"'));
  assert.equal(page.includes('className="workspace-panel studio-panel"'), false);
  assert.match(css, /grid-template-columns:\s*minmax\(270px,\s*25%\)\s+minmax\(0,\s*1fr\);/);
  assert.equal(css.includes('"studio studio"'), false);
});

test("workspace top actions use a designed toolbar instead of generic sort buttons", () => {
  assert.ok(page.includes('className="workspace-toolbar"'));
  assert.ok(page.includes('className="workspace-nav-button"'));
  assert.equal(page.includes('<button className="sort-button" onClick={() => openAnalytics()'), false);
  assert.match(css, /\.workspace-nav-button\s*{/);
});
