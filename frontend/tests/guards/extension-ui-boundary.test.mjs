import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { appSourceModules, importsIn, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";


test("page composes one availability owner and exactly the two canonical outlets", async () => {
  const page = await parseModule("page.tsx");
  const text = page.getText(page);
  assert.equal((text.match(/useWorkspaceExtensions\(/g) ?? []).length, 1);
  const outlets = jsxElements(page, "WorkspaceExtensionOutlet");
  assert.equal(outlets.length, 2);
  assert.deepEqual(outlets.map((row) => row.attributes.slot).sort(), [
    "source.detail_section", "workspace.side_panel",
  ]);
  assert.equal(text.includes("source_detail_section"), false);
  assert.equal(text.includes('slot="side_panel"'), false);
  const sourceWindowStart = text.indexOf("<SourceDetailWindow");
  const sourceOutlet = text.indexOf('slot="source.detail_section"');
  const sourceWindowEnd = text.indexOf("</SourceDetailWindow>", sourceWindowStart);
  assert.ok(sourceWindowStart < sourceOutlet && sourceOutlet < sourceWindowEnd);
  const sourceWriteInitializers = [];
  function visit(node) {
    if (
      ts.isPropertyAssignment(node)
      && node.name.getText(page) === "sourceWrite"
    ) sourceWriteInitializers.push(node.initializer.getText(page));
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.deepEqual(
    sourceWriteInitializers.sort(),
    ["!sourceDetailBaseId && capabilities.canWriteNotebook", "false"].sort(),
    "source:write must be false by default and require both local-source ownership and live core write authority",
  );
});


test("extension SDK remains static, narrow, and free of domain owners or remote loaders", async () => {
  const modules = (await appSourceModules()).filter((row) => (
    row.path.startsWith("features/extension-sdk/")
    || row.path === "use-workspace-extensions.ts"
  ));
  const forbidden = /\b(fetch\s*\(|import\s*\(|setInterval\s*\(|setTimeout\s*\(|WebSocket\b|EventSource\b|page\.tsx|useSourceLibrary|useAskSession|useReportWorkspace|useKgWorkspace|useNotebookCollection)\b/;
  for (const { path, module } of modules) {
    assert.doesNotMatch(module.getText(module), forbidden, path);
  }
  const registry = modules.find((row) => row.path === "features/extension-sdk/registry.ts");
  assert.ok(registry);
  assert.deepEqual(importsIn(registry.module).map((row) => row.module), ["./contracts.ts"]);
});
