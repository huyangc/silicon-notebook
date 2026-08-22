import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const css = await readFile(new URL("../../app/globals.css", import.meta.url), "utf8");

function declarationsFor(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = [...css.matchAll(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, "g"))];
  return matches.map((match) => match[1].replace(/\s+/g, " ").trim());
}

test("visible side contribution gets an explicit third desktop column", () => {
  const visible = declarationsFor(
    ".workspace-grid:has(> .workspace-extension-outlet-workspace-side_panel)",
  );
  assert.ok(visible.some((body) => /grid-template-columns:[^;]*25%[^;]*18%[^;]*1fr/.test(body)));
  const collapsed = declarationsFor(
    ".workspace-grid.sources-collapsed:has(> .workspace-extension-outlet-workspace-side_panel)",
  );
  assert.ok(collapsed.some((body) => /grid-template-columns:\s*0[^;]*18%[^;]*1fr/.test(body)));
});

test("mobile contribution layout collapses back to one column", () => {
  const mobile = css.match(/@media \(max-width: 760px\) \{([\s\S]*)$/)?.[1] ?? "";
  assert.match(mobile, /workspace-extension-outlet-workspace-side_panel[\s\S]*grid-template-columns:\s*1fr/);
});
