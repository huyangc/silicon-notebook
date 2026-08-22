import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { appSourceModules, findFunction, importsIn, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";


test("page composes one availability owner and exactly the two canonical outlets", async () => {
  const page = await parseModule("page.tsx");
  const outlets = jsxElements(page, "WorkspaceExtensionOutlet");
  assert.equal(outlets.length, 2);
  assert.deepEqual(outlets.map((row) => row.attributes.slot).sort(), [
    "source.detail_section", "workspace.side_panel",
  ]);

  let workspacePermissions;
  let sourceDetailWindow;
  const contexts = new Map();
  const outletNodes = new Map();
  const ownerCalls = [];
  function visit(node) {
    if (
      ts.isCallExpression(node)
      && node.expression.getText(page) === "useWorkspaceExtensions"
    ) ownerCalls.push(node.arguments.map((argument) => argument.getText(page)));
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
      ) {
        contexts.set(slot.text, context.expression);
        outletNodes.set(slot.text, node);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(page);

  assert.deepEqual(
    ownerCalls,
    [["currentUser?.id ?? null", "currentNotebookId"]],
    "the availability owner must bind the live actor and visible notebook exactly once",
  );

  assert.ok(sourceDetailWindow, "source extension slot must remain inside SourceDetailWindow");
  let workspaceOutletParent = outletNodes.get("workspace.side_panel")?.parent;
  while (workspaceOutletParent && !ts.isJsxElement(workspaceOutletParent)) {
    workspaceOutletParent = workspaceOutletParent.parent;
  }
  assert.ok(
    workspaceOutletParent
    && workspaceOutletParent.openingElement.tagName.getText(page) === "section",
    "workspace side outlet must remain a direct JSX child of the workspace section",
  );
  const workspaceClass = workspaceOutletParent.openingElement.attributes.properties.find((attribute) => (
    ts.isJsxAttribute(attribute) && attribute.name.getText(page) === "className"
  ));
  assert.ok(
    workspaceClass
    && workspaceClass.initializer?.getText(page) === '{`workspace-grid${sourcesCollapsed ? " sources-collapsed" : ""}`}',
    "workspace side outlet direct parent must be the CSS-gated workspace grid",
  );
  let sourceOutletAncestor = outletNodes.get("source.detail_section")?.parent;
  while (sourceOutletAncestor && sourceOutletAncestor !== sourceDetailWindow) {
    sourceOutletAncestor = sourceOutletAncestor.parent;
  }
  assert.equal(
    sourceOutletAncestor,
    sourceDetailWindow,
    "source extension slot must remain a semantic descendant of SourceDetailWindow",
  );
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

  function exactObjectProperty(object, name, expected) {
    const entry = property(object, name);
    assert.ok(
      entry
      && ts.isPropertyAssignment(entry)
      && ts.isObjectLiteralExpression(entry.initializer),
      `${name} must remain an explicit readonly summary object`,
    );
    assert.equal(entry.initializer.properties.length, Object.keys(expected).length);
    assert.ok(
      entry.initializer.properties.every(ts.isPropertyAssignment),
      `${name} must not spread or shorthand any wider domain object`,
    );
    assert.deepEqual(Object.fromEntries(entry.initializer.properties
      .map((item) => [item.name.getText(page), item.initializer.getText(page)])), expected);
  }

  assert.ok(workspacePermissions, "workspace extension permission snapshot must remain explicit");
  assert.equal(workspacePermissions.properties.length, 6);
  assert.ok(workspacePermissions.properties.every(ts.isPropertyAssignment));
  assert.deepEqual(Object.fromEntries(workspacePermissions.properties
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
    assert.deepEqual(context.properties.map((entry) => {
      if (ts.isPropertyAssignment(entry)) return entry.name.getText(page);
      if (ts.isShorthandPropertyAssignment(entry)) return `shorthand:${entry.name.getText(page)}`;
      return `forbidden:${ts.SyntaxKind[entry.kind]}`;
    }), ["slot", "actor", "notebook", "source", "shorthand:uiMode", "permissions"]);
    const mode = property(context, "uiMode");
    assert.ok(
      mode && ts.isShorthandPropertyAssignment(mode) && mode.name.getText(page) === "uiMode",
      `${slot} must receive the shell's normalized uiMode identifier`,
    );
    exactObjectProperty(context, "actor", {
      id: "currentUser.id",
      username: "currentUser.username",
      displayName: "currentUser.display_name",
    });
    exactObjectProperty(context, "notebook", {
      id: "currentNotebook.id",
      name: "currentNotebook.name",
    });
  }
  const workspaceSource = property(contexts.get("workspace.side_panel"), "source");
  assert.ok(
    workspaceSource
    && ts.isPropertyAssignment(workspaceSource)
    && workspaceSource.initializer.kind === ts.SyntaxKind.NullKeyword,
    "workspace slot must receive no source identity",
  );
  exactObjectProperty(contexts.get("source.detail_section"), "source", {
    id: "sourceDetail.id",
    notebookId: "sourceDetail.notebook_id",
    title: "sourceDetail.title",
  });
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
    || row.path === "features/agent-profile/workspace-plugin.ts"
    || row.path === "use-workspace-extensions.ts"
  ));
  const forbidden = /\b(fetch\s*\(|import\s*\(|setInterval\s*\(|setTimeout\s*\(|WebSocket\b|EventSource\b|page\.tsx|useSourceLibrary|useAskSession|useReportWorkspace|useKgWorkspace|useNotebookCollection)\b/;
  for (const { path, module } of modules) {
    assert.doesNotMatch(module.getText(module), forbidden, path);
  }
  const registry = modules.find((row) => row.path === "features/extension-sdk/registry.ts");
  assert.ok(registry);
  assert.deepEqual(importsIn(registry.module).map((row) => row.module), [
    "../agent-profile/workspace-plugin.ts",
    "./contracts.ts",
  ]);
  const plugin = modules.find((row) => row.path === "features/agent-profile/workspace-plugin.ts");
  assert.ok(plugin);
  assert.deepEqual(importsIn(plugin.module).map((row) => row.module), [
    "../extension-sdk/contracts.ts",
    "lucide-react",
    "react",
  ]);
  const owner = modules.find((row) => row.path === "use-workspace-extensions.ts");
  assert.ok(owner);
  const ownerFunction = findFunction(owner.module, "useWorkspaceExtensions");
  const effectBodies = [];
  function visitOwner(node) {
    if (
      ts.isCallExpression(node)
      && node.expression.getText(owner.module) === "useEffect"
    ) {
      const callback = node.arguments[0];
      if (
        callback
        && (ts.isArrowFunction(callback) || ts.isFunctionExpression(callback))
        && ts.isBlock(callback.body)
      ) effectBodies.push(callback.body);
    }
    ts.forEachChild(node, visitOwner);
  }
  visitOwner(ownerFunction);
  assert.equal(effectBodies.length, 1, "availability owner must retain one explicit effect");
  const emptyGuard = effectBodies[0].statements[0];
  assert.ok(
    emptyGuard
    && ts.isIfStatement(emptyGuard)
    && emptyGuard.expression.getText(owner.module) === "registry.length === 0 || !actorId || !visibleOwner"
    && ts.isReturnStatement(emptyGuard.thenStatement),
    "empty registry must be the effect's first semantic statement and return before side effects",
  );
});


test("extension owner is wired into authentication and workspace transitions", async () => {
  const page = await parseModule("page.tsx");
  const calls = [];
  function visit(node) {
    if (ts.isCallExpression(node)) {
      const target = node.expression.getText(page);
      if (target.startsWith("workspaceExtensions.")) {
        calls.push({ target, arguments: node.arguments.map((argument) => argument.getText(page)) });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.equal(calls.filter((row) => row.target === "workspaceExtensions.activateActor").length, 2);
  assert.equal(calls.filter((row) => row.target === "workspaceExtensions.beginNotebookTransition").length, 1);
  assert.ok(calls.some((row) => row.target === "workspaceExtensions.finishNotebookTransition"
    && row.arguments.join("|") === "workspaceExtensionTransition|opened"));
  assert.ok(calls.filter((row) => row.target === "workspaceExtensions.leaveWorkspace").length >= 2);
  assert.equal(calls.filter((row) => row.target === "workspaceExtensions.owns").length, 0);
  const actions = [];
  function visitActions(node) {
    if (
      ts.isVariableDeclaration(node)
      && node.name.getText(page) === "workspaceExtensionActions"
      && node.initializer
      && ts.isCallExpression(node.initializer)
    ) actions.push(node.initializer);
    ts.forEachChild(node, visitActions);
  }
  visitActions(page);
  assert.equal(actions.length, 1);
  assert.equal(actions[0].expression.getText(page), "createOwnedWorkspaceExtensionActions");
  assert.deepEqual(actions[0].arguments.map((argument) => argument.getText(page)), [
    "workspaceExtensions.owner",
    "workspaceExtensions.owns",
    '() => {\n      rootModals.open("understanding", rootModals.captureWorkspaceOwner());\n    }',
  ]);
  const outlets = jsxElements(page, "WorkspaceExtensionOutlet");
  for (const outlet of outlets) {
    assert.equal(outlet.bindings.ownerKey, "workspaceExtensions.ownerKey");
    assert.equal(outlet.bindings.actions, "workspaceExtensionActions");
  }
});
