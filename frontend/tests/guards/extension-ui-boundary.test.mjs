import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { appSourceModules, findFunction, importsIn, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";


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

  let workspacePermissions;
  let sourceDetailWindow;
  const contexts = new Map();
  function visit(node) {
    if (
      ts.isJsxElement(node)
      && node.openingElement.tagName.getText(page) === "SourceDetailWindow"
    ) sourceDetailWindow = node;
    if (
      ts.isVariableDeclaration(node)
      && node.name.getText(page) === "workspaceExtensionPermissions"
      && node.initializer
    ) {
      const initializer = ts.isAsExpression(node.initializer)
        ? node.initializer.expression
        : node.initializer;
      if (ts.isObjectLiteralExpression(initializer)) workspacePermissions = initializer;
    }
    if (
      ts.isJsxSelfClosingElement(node)
      && node.tagName.getText(page) === "WorkspaceExtensionOutlet"
    ) {
      const attributes = new Map(node.attributes.properties
        .filter(ts.isJsxAttribute)
        .map((attribute) => [attribute.name.getText(page), attribute]));
      const slot = attributes.get("slot")?.initializer;
      const context = attributes.get("context")?.initializer;
      if (
        slot && ts.isStringLiteral(slot)
        && context && ts.isJsxExpression(context)
        && context.expression && ts.isObjectLiteralExpression(context.expression)
      ) contexts.set(slot.text, context.expression);
    }
    ts.forEachChild(node, visit);
  }
  visit(page);

  assert.ok(sourceDetailWindow, "source extension slot must remain inside SourceDetailWindow");
  const sourceDetailGate = sourceDetailWindow.parent?.parent;
  assert.ok(
    sourceDetailGate
    && ts.isBinaryExpression(sourceDetailGate)
    && sourceDetailGate.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
    && sourceDetailGate.left.getText(page) === "sourceDetailModal.open && sourceDetail",
    "source detail and its extension slot must be synchronously hidden by the coordinator open lease",
  );

  function property(object, name) {
    return object?.properties.find((entry) => (
      (ts.isPropertyAssignment(entry) || ts.isShorthandPropertyAssignment(entry))
      && entry.name.getText(page) === name
    ));
  }

  assert.ok(workspacePermissions, "workspace extension permission snapshot must remain explicit");
  assert.deepEqual(Object.fromEntries(workspacePermissions.properties
    .filter(ts.isPropertyAssignment)
    .map((entry) => [entry.name.getText(page), entry.initializer.getText(page)])), {
    notebookRead: "Boolean(currentNotebookId && currentNotebook)",
    notebookWrite: "capabilities.canWriteNotebook",
    notebookConfigure: "capabilities.canConfigureNotebook",
    sourceRead: "false",
    sourceWrite: "false",
    systemAdmin: "capabilities.canManageGlobalSchemas",
  });
  for (const slot of ["workspace.side_panel", "source.detail_section"]) {
    const context = contexts.get(slot);
    assert.ok(context, `missing semantic context for ${slot}`);
    const mode = property(context, "uiMode");
    assert.ok(
      mode && ts.isShorthandPropertyAssignment(mode) && mode.name.getText(page) === "uiMode",
      `${slot} must receive the shell's normalized uiMode identifier`,
    );
  }
  const sidePermissions = property(contexts.get("workspace.side_panel"), "permissions");
  assert.ok(
    sidePermissions
    && ts.isPropertyAssignment(sidePermissions)
    && sidePermissions.initializer.getText(page) === "workspaceExtensionPermissions",
    "workspace slot must consume the core-owned permission snapshot",
  );
  const sourcePermissions = property(contexts.get("source.detail_section"), "permissions");
  assert.ok(
    sourcePermissions
    && ts.isPropertyAssignment(sourcePermissions)
    && ts.isObjectLiteralExpression(sourcePermissions.initializer),
    "source slot must narrow the core-owned permission snapshot explicitly",
  );
  const sourcePermissionObject = sourcePermissions.initializer;
  assert.deepEqual(sourcePermissionObject.properties.map((entry) => {
    if (ts.isSpreadAssignment(entry)) return `...${entry.expression.getText(page)}`;
    if (ts.isPropertyAssignment(entry)) {
      return `${entry.name.getText(page)}:${entry.initializer.getText(page)}`;
    }
    return entry.getText(page);
  }), [
    "...workspaceExtensionPermissions",
    "sourceRead:true",
    "sourceWrite:!sourceDetailBaseId && capabilities.canWriteNotebook",
  ]);
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
  const owner = modules.find((row) => row.path === "use-workspace-extensions.ts");
  assert.ok(owner);
  const ownerFunction = findFunction(owner.module, "useWorkspaceExtensions");
  let emptyGuard = -1;
  const controllers = [];
  function visitOwner(node) {
    if (
      ts.isIfStatement(node)
      && node.expression.getText(owner.module) === "registry.length === 0 || !actorId"
    ) emptyGuard = node.getStart(owner.module);
    if (
      ts.isNewExpression(node)
      && node.expression.getText(owner.module) === "AbortController"
    ) controllers.push(node.getStart(owner.module));
    ts.forEachChild(node, visitOwner);
  }
  visitOwner(ownerFunction);
  assert.ok(emptyGuard >= 0, "empty registry must retain an explicit zero-side-effect return");
  assert.ok(
    controllers.every((position) => emptyGuard < position),
    "empty registry must return before constructing an AbortController",
  );
});
