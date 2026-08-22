import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const css = await readFile(new URL("../../app/globals.css", import.meta.url), "utf8");

function declarationsFor(selector, source = css) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const matches = [...source.matchAll(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, "g"))];
  return matches.map((match) => match[1].replace(/\s+/g, " ").trim());
}

function mediaBodies(maxWidth) {
  const marker = `@media (max-width: ${maxWidth}px)`;
  const bodies = [];
  let cursor = 0;
  while (cursor < css.length) {
    const markerAt = css.indexOf(marker, cursor);
    if (markerAt === -1) break;
    const openAt = css.indexOf("{", markerAt + marker.length);
    assert.notEqual(openAt, -1, `missing body for ${marker}`);
    let depth = 1;
    for (let index = openAt + 1; index < css.length; index += 1) {
      if (css[index] === "{") depth += 1;
      if (css[index] === "}") depth -= 1;
      if (depth === 0) {
        bodies.push(css.slice(openAt + 1, index));
        cursor = index + 1;
        break;
      }
    }
    assert.equal(depth, 0, `unterminated ${marker}`);
  }
  assert.ok(bodies.length > 0, `missing ${marker}`);
  return bodies;
}

function sidePanelMediaBody(maxWidth) {
  const bodies = mediaBodies(maxWidth).filter((body) => (
    body.includes("workspace-extension-outlet-workspace-side_panel")
  ));
  assert.equal(bodies.length, 1, `expected one side-panel layout block at ${maxWidth}px`);
  return bodies[0];
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
  const mobile = sidePanelMediaBody(760);
  assert.match(mobile, /workspace-extension-outlet-workspace-side_panel[\s\S]*grid-template-columns:\s*1fr/);
});

test("medium workspace preserves all three visible and collapsed columns", () => {
  const medium = sidePanelMediaBody(1100);
  assert.deepEqual(declarationsFor(
    ".workspace-grid:has(> .workspace-extension-outlet-workspace-side_panel)",
    medium,
  ), ["grid-template-columns: 250px 180px minmax(0, 1fr);"]);
  assert.deepEqual(declarationsFor(
    ".workspace-grid.sources-collapsed:has(> .workspace-extension-outlet-workspace-side_panel)",
    medium,
  ), ["grid-template-columns: 0 180px minmax(0, 1fr);"]);
});
