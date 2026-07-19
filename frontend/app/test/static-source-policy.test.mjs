import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import { STATIC_SOURCE_CONTRACTS } from "./static-source-contracts.mjs";


const APP_DIR = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const DIRECT_READ_ALLOWLIST = new Set([
  // Reads committed cross-language golden data, never production source text.
  "knowhow-normalize.test.mjs",
  "test/semantic-source.mjs",
  "test/static-source-policy.test.mjs",
  "test-runner-config.test.mjs",
]);
const POSITION_QUERY_ALLOWLIST = new Set([
  // Contains mutation examples for this policy; it does not inspect production.
  "test/static-source-policy.test.mjs",
]);


function isSourceModule(relative) {
  return (
    relative.endsWith(".mjs")
    || relative.endsWith(".ts")
    || relative.endsWith(".tsx")
  );
}


function isTestEntrypoint(relative) {
  return (
    relative.endsWith(".test.mjs")
    || relative.endsWith(".test.ts")
    || relative.endsWith(".test.tsx")
  );
}


function scriptKind(relative) {
  if (relative.endsWith(".tsx")) return ts.ScriptKind.TSX;
  if (relative.endsWith(".ts")) return ts.ScriptKind.TS;
  return ts.ScriptKind.JS;
}


async function sourceModules(directory = APP_DIR) {
  const entries = await readdir(directory, { withFileTypes: true });
  const modules = [];
  for (const entry of entries) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) {
      modules.push(...await sourceModules(absolute));
    } else if (isSourceModule(
      path.relative(APP_DIR, absolute).replaceAll(path.sep, "/"),
    )) {
      modules.push(absolute);
    }
  }
  return modules.sort();
}


function relativeImports(relative, source, moduleNames) {
  const sourceFile = ts.createSourceFile(
    relative,
    source,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(relative),
  );
  const imports = [];
  function resolve(specifier) {
    if (!specifier.startsWith(".")) return undefined;
    const base = path.posix.normalize(
      path.posix.join(path.posix.dirname(relative), specifier),
    );
    for (const candidate of [
      base,
      `${base}.ts`,
      `${base}.tsx`,
      `${base}.mjs`,
      `${base}/index.ts`,
      `${base}/index.tsx`,
      `${base}/index.mjs`,
    ]) {
      if (moduleNames.has(candidate)) return candidate;
    }
    return undefined;
  }
  function visit(node) {
    if (
      (
        ts.isImportDeclaration(node)
        || ts.isExportDeclaration(node)
      )
      && node.moduleSpecifier
      && ts.isStringLiteral(node.moduleSpecifier)
    ) {
      const resolved = resolve(node.moduleSpecifier.text);
      if (resolved) imports.push(resolved);
    }
    if (
      ts.isCallExpression(node)
      && node.expression.kind === ts.SyntaxKind.ImportKeyword
      && node.arguments.length === 1
      && ts.isStringLiteralLike(node.arguments[0])
    ) {
      const resolved = resolve(node.arguments[0].text);
      if (resolved) imports.push(resolved);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return imports;
}


function reachableModules(roots, importsByModule) {
  const reached = new Set();
  const pending = [...roots];
  while (pending.length > 0) {
    const relative = pending.pop();
    if (reached.has(relative)) continue;
    reached.add(relative);
    pending.push(...(importsByModule.get(relative) ?? []));
  }
  return reached;
}


function policyRelativeModules(moduleSources) {
  const moduleNames = new Set(moduleSources.keys());
  const importsByModule = new Map(
    [...moduleSources].map(([relative, source]) => [
      relative,
      relativeImports(relative, source, moduleNames),
    ]),
  );
  const testEntrypoints = [...moduleNames].filter(isTestEntrypoint);
  const productionEntrypoints = [...moduleNames].filter(
    (relative) => /(?:^|\/)(?:page|layout|route)\.tsx?$/.test(relative),
  );
  const testReachable = reachableModules(testEntrypoints, importsByModule);
  const productionReachable = reachableModules(
    productionEntrypoints,
    importsByModule,
  );
  return new Set(
    [...moduleNames].filter((relative) => (
      isTestEntrypoint(relative)
      || relative.startsWith("test/")
      || (
        testReachable.has(relative)
        && !productionReachable.has(relative)
      )
    )),
  );
}


async function policyModules() {
  const sources = new Map();
  for (const absolute of await sourceModules()) {
    const relative = path.relative(APP_DIR, absolute).replaceAll(path.sep, "/");
    sources.set(relative, await readFile(absolute, "utf8"));
  }
  return [...policyRelativeModules(sources)]
    .sort()
    .map((relative) => path.join(APP_DIR, relative));
}


function isDefinitelyArrayType(type) {
  if (!type) return false;
  if (ts.isArrayTypeNode(type) || ts.isTupleTypeNode(type)) return true;
  if (ts.isParenthesizedTypeNode(type)) {
    return isDefinitelyArrayType(type.type);
  }
  if (
    ts.isTypeOperatorNode(type)
    && type.operator === ts.SyntaxKind.ReadonlyKeyword
  ) {
    return isDefinitelyArrayType(type.type);
  }
  if (ts.isUnionTypeNode(type)) {
    return type.types.every(isDefinitelyArrayType);
  }
  return (
    ts.isTypeReferenceNode(type)
    && ts.isIdentifier(type.typeName)
    && [
      "Array",
      "ReadonlyArray",
      "ArrayBuffer",
      "SharedArrayBuffer",
      "Buffer",
      "Int8Array",
      "Uint8Array",
      "Uint8ClampedArray",
      "Int16Array",
      "Uint16Array",
      "Int32Array",
      "Uint32Array",
      "Float32Array",
      "Float64Array",
      "BigInt64Array",
      "BigUint64Array",
    ].includes(type.typeName.text)
  );
}


function hasPositionOrOrderQuery(relative, source) {
  if (POSITION_QUERY_ALLOWLIST.has(relative)) return false;
  const sourceFile = ts.createSourceFile(
    relative,
    source,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(relative),
  );
  let structuralPositionQuery = false;
  let textPositionMethod = false;
  function scanPositionSyntax(node) {
    if (
      ts.isPropertyAccessExpression(node)
      && ["pos", "end"].includes(node.name.text)
    ) {
      structuralPositionQuery = true;
      return;
    }
    if (
      ts.isElementAccessExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && ["statements", "members", "properties"].includes(
        node.expression.name.text,
      )
    ) {
      structuralPositionQuery = true;
      return;
    }
    if (
      (
        ts.isVariableDeclaration(node)
        && ts.isArrayBindingPattern(node.name)
        && node.initializer
        && ts.isPropertyAccessExpression(node.initializer)
        && ["statements", "members", "properties"].includes(
          node.initializer.name.text,
        )
      )
      || (
        ts.isBinaryExpression(node)
        && ts.isArrayLiteralExpression(node.left)
        && ts.isPropertyAccessExpression(node.right)
        && ["statements", "members", "properties"].includes(
          node.right.name.text,
        )
      )
    ) {
      structuralPositionQuery = true;
      return;
    }
    if (
      ts.isCallExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
    ) {
      const method = node.expression.name.text;
      if (["getStart", "getFullStart", "getEnd"].includes(method)) {
        structuralPositionQuery = true;
        return;
      }
      if (
        method === "at"
        && ts.isPropertyAccessExpression(node.expression.expression)
        && ["statements", "members", "properties"].includes(
          node.expression.expression.name.text,
        )
      ) {
        structuralPositionQuery = true;
        return;
      }
      if (["indexOf", "slice", "substring"].includes(method)) {
        textPositionMethod = true;
      }
    }
    ts.forEachChild(node, scanPositionSyntax);
  }
  scanPositionSyntax(sourceFile);
  if (structuralPositionQuery) return true;
  if (!textPositionMethod) return false;

  let found = false;
  const SOURCE_INPUT_NAME = /^(?:source|sourceText|productionSource|content|text|payload)$/i;
  const FILE_READ_EXPORTS = new Set(["readFile", "readFileSync"]);
  const FILE_READ_EXPORT_BITS = new Map([
    ["readFile", 1],
    ["readFileSync", 2],
  ]);
  const ALL_FILE_READ_EXPORTS = 3;
  const fileReadImports = [];
  const fileReadNamespaces = [];
  const throwCollectors = [];
  let nextBindingId = 1;

  for (const statement of sourceFile.statements) {
    if (
      !ts.isImportDeclaration(statement)
      || !ts.isStringLiteral(statement.moduleSpecifier)
      || !/^node:fs(?:\/promises)?$/.test(statement.moduleSpecifier.text)
      || !statement.importClause
    ) {
      continue;
    }
    if (statement.importClause.name) {
      fileReadNamespaces.push(statement.importClause.name.text);
    }
    const bindings = statement.importClause.namedBindings;
    if (bindings && ts.isNamespaceImport(bindings)) {
      fileReadNamespaces.push(bindings.name.text);
    }
    if (bindings && ts.isNamedImports(bindings)) {
      for (const element of bindings.elements) {
        const imported = element.propertyName?.text ?? element.name.text;
        if (FILE_READ_EXPORTS.has(imported)) {
          fileReadImports.push(element.name.text);
        }
      }
    }
  }

  function newScope(parent = undefined, kind = "block") {
    return { parent, kind, bindings: new Map() };
  }

  function newBinding(
    value,
    { fileRead = false, fileReadNamespace = 0 } = {},
  ) {
    return {
      id: nextBindingId++,
      value,
      fileRead,
      fileReadNamespace,
    };
  }

  function resolvedBinding(scope, name) {
    for (let current = scope; current; current = current.parent) {
      if (current.bindings.has(name)) {
        return current.bindings.get(name);
      }
    }
    return undefined;
  }

  function defineBinding(scope, name, value, traits = {}) {
    const existing = scope.bindings.get(name);
    if (existing) {
      existing.value = value;
      existing.fileRead = traits.fileRead ?? false;
      existing.fileReadNamespace = traits.fileReadNamespace ?? 0;
      return existing;
    }
    const created = newBinding(value, traits);
    scope.bindings.set(name, created);
    return created;
  }

  function assignBinding(scope, name, value, traits = {}) {
    const existing = resolvedBinding(scope, name);
    if (existing) {
      existing.value = value;
      existing.fileRead = traits.fileRead ?? false;
      existing.fileReadNamespace = traits.fileReadNamespace ?? 0;
    } else {
      defineBinding(scope, name, value, traits);
    }
  }

  function nearestFunctionScope(scope) {
    let current = scope;
    while (current.parent && current.kind !== "function") {
      current = current.parent;
    }
    return current;
  }

  function cloneScope(scope) {
    const chain = [];
    for (let current = scope; current; current = current.parent) {
      chain.push(current);
    }
    let clonedParent;
    for (const original of chain.reverse()) {
      const cloned = newScope(clonedParent, original.kind);
      for (const [name, entry] of original.bindings) {
        cloned.bindings.set(
          name,
          {
            id: entry.id,
            value: entry.value,
            fileRead: entry.fileRead,
            fileReadNamespace: entry.fileReadNamespace,
          },
        );
      }
      clonedParent = cloned;
    }
    return clonedParent;
  }

  function bindingStates(scope) {
    const states = new Map();
    for (let current = scope; current; current = current.parent) {
      for (const entry of current.bindings.values()) {
        if (!states.has(entry.id)) {
          states.set(entry.id, {
            value: entry.value,
            fileRead: entry.fileRead,
            fileReadNamespace: entry.fileReadNamespace,
          });
        }
      }
    }
    return states;
  }

  function joinScopes(target, branches) {
    const branchStates = branches.map(bindingStates);
    for (let current = target; current; current = current.parent) {
      for (const entry of current.bindings.values()) {
        entry.value = branchStates.some(
          (states) => states.get(entry.id)?.value ?? entry.value,
        );
        entry.fileRead = branchStates.some(
          (states) => (
            states.get(entry.id)?.fileRead
            ?? entry.fileRead
          ),
        );
        entry.fileReadNamespace = branchStates.reduce(
          (mask, states) => (
            mask
            | (
              states.get(entry.id)?.fileReadNamespace
              ?? entry.fileReadNamespace
            )
          ),
          0,
        );
      }
    }
  }

  function sameBindingValues(left, right) {
    const leftStates = bindingStates(left);
    const rightStates = bindingStates(right);
    if (leftStates.size !== rightStates.size) return false;
    return [...leftStates].every(
      ([id, value]) => {
        const other = rightStates.get(id);
        return (
          other?.value === value.value
          && other.fileRead === value.fileRead
          && other.fileReadNamespace === value.fileReadNamespace
        );
      },
    );
  }

  function normalCompletion() {
    return { normal: true, abrupt: [] };
  }

  function abruptCompletion(kind, scope, label = undefined) {
    return {
      normal: false,
      abrupt: [{ kind, label, scope: cloneScope(scope) }],
    };
  }

  function analyzeLoop(
    scope,
    statement,
    afterIteration,
    canSkip,
    canExitAfterIteration,
    label = undefined,
  ) {
    const baseline = cloneScope(scope);
    let state = cloneScope(baseline);
    const exits = canSkip ? [cloneScope(baseline)] : [];
    const propagated = [];
    const maxIterations = bindingStates(scope).size + 2;
    for (let index = 0; index < maxIterations; index += 1) {
      const iteration = cloneScope(state);
      const result = visit(statement, iteration);
      const backEdges = result.normal ? [cloneScope(iteration)] : [];
      for (const completion of result.abrupt) {
        const matches = (
          completion.label === undefined
          || completion.label === label
        );
        if (completion.kind === "continue" && matches) {
          backEdges.push(cloneScope(completion.scope));
        } else if (completion.kind === "break" && matches) {
          exits.push(cloneScope(completion.scope));
        } else {
          propagated.push(completion);
        }
      }
      for (const backEdge of backEdges) afterIteration(backEdge);
      if (canExitAfterIteration) {
        exits.push(...backEdges.map((backEdge) => cloneScope(backEdge)));
      }
      if (backEdges.length === 0) break;
      const next = cloneScope(baseline);
      joinScopes(next, [baseline, ...backEdges]);
      if (sameBindingValues(state, next)) break;
      state = next;
    }
    if (exits.length > 0) joinScopes(scope, exits);
    return {
      normal: exits.length > 0,
      abrupt: propagated,
    };
  }

  function collectPossibleThrow(scope) {
    throwCollectors.at(-1)?.(scope);
  }

  function bindingIdentifiers(name, identifiers = []) {
    if (ts.isIdentifier(name)) {
      identifiers.push(name.text);
      return identifiers;
    }
    for (const element of name.elements) {
      if (!ts.isOmittedExpression(element)) {
        bindingIdentifiers(element.name, identifiers);
      }
    }
    return identifiers;
  }

  function collectFunctionVarNames(node) {
    const names = new Set();
    function collect(current, root = false) {
      if (!root && ts.isFunctionLike(current)) return;
      if (
        ts.isVariableDeclarationList(current)
        && !(current.flags & ts.NodeFlags.BlockScoped)
      ) {
        for (const declaration of current.declarations) {
          if (ts.isIdentifier(declaration.name)) {
            names.add(declaration.name.text);
          }
        }
      }
      ts.forEachChild(current, (child) => collect(child));
    }
    collect(node, true);
    return names;
  }

  function predeclareFunctionVars(node, scope) {
    for (const name of collectFunctionVarNames(node)) {
      if (!scope.bindings.has(name)) {
        scope.bindings.set(name, newBinding(false));
      }
    }
  }

  function predeclareStatements(statements, scope) {
    for (const statement of statements) {
      if (
        (
          ts.isFunctionDeclaration(statement)
          || ts.isClassDeclaration(statement)
        )
        && statement.name
      ) {
        defineBinding(scope, statement.name.text, false);
      }
      if (
        ts.isVariableStatement(statement)
        && (statement.declarationList.flags & ts.NodeFlags.BlockScoped)
      ) {
        for (const declaration of statement.declarationList.declarations) {
          for (const name of bindingIdentifiers(declaration.name)) {
            defineBinding(scope, name, false);
          }
        }
      }
    }
  }

  function predeclareBlockBindings(node, scope) {
    predeclareStatements(node.statements, scope);
  }

  function unwrapExpression(expression) {
    let current = expression;
    while (
      ts.isAwaitExpression(current)
      || ts.isParenthesizedExpression(current)
      || ts.isAsExpression(current)
      || ts.isTypeAssertionExpression(current)
      || ts.isNonNullExpression(current)
    ) {
      current = current.expression;
    }
    return current;
  }

  function staticStringValue(expression) {
    const current = unwrapExpression(expression);
    if (ts.isStringLiteralLike(current)) return current.text;
    if (
      ts.isBinaryExpression(current)
      && current.operatorToken.kind === ts.SyntaxKind.PlusToken
    ) {
      const left = staticStringValue(current.left);
      const right = staticStringValue(current.right);
      if (left !== undefined && right !== undefined) return left + right;
    }
    return undefined;
  }

  function staticPropertyName(name) {
    if (ts.isIdentifier(name) || ts.isStringLiteralLike(name)) {
      return name.text;
    }
    if (
      ts.isComputedPropertyName(name)
    ) {
      return staticStringValue(name.expression);
    }
    return undefined;
  }

  function objectBindingTargets(pattern) {
    const targets = [];
    const entries = ts.isObjectBindingPattern(pattern)
      ? pattern.elements
      : pattern.properties;
    for (const entry of entries) {
      if (
        ts.isBindingElement(entry)
        && ts.isIdentifier(entry.name)
      ) {
        targets.push({
          defaultInitializer: entry.initializer,
          property: entry.propertyName
            ? staticPropertyName(entry.propertyName)
            : entry.name.text,
          rest: Boolean(entry.dotDotDotToken),
          target: entry.name.text,
        });
      } else if (
        ts.isPropertyAssignment(entry)
        && ts.isIdentifier(entry.initializer)
      ) {
        targets.push({
          defaultInitializer: undefined,
          property: staticPropertyName(entry.name),
          rest: false,
          target: entry.initializer.text,
        });
      } else if (
        ts.isPropertyAssignment(entry)
        && ts.isBinaryExpression(entry.initializer)
        && entry.initializer.operatorToken.kind === ts.SyntaxKind.EqualsToken
        && ts.isIdentifier(entry.initializer.left)
      ) {
        targets.push({
          defaultInitializer: entry.initializer.right,
          property: staticPropertyName(entry.name),
          rest: false,
          target: entry.initializer.left.text,
        });
      } else if (ts.isShorthandPropertyAssignment(entry)) {
        targets.push({
          defaultInitializer: entry.objectAssignmentInitializer,
          property: entry.name.text,
          rest: false,
          target: entry.name.text,
        });
      } else if (
        ts.isSpreadAssignment(entry)
        && ts.isIdentifier(entry.expression)
      ) {
        targets.push({
          defaultInitializer: undefined,
          property: undefined,
          rest: true,
          target: entry.expression.text,
        });
      }
    }
    return targets;
  }

  function bindObjectCapabilities(pattern, traits, scope, define) {
    const targets = objectBindingTargets(pattern);
    const excludedMask = targets.reduce(
      (mask, { property, rest }) => (
        rest
          ? mask
          : mask | (FILE_READ_EXPORT_BITS.get(property) ?? 0)
      ),
      0,
    );
    for (
      const {
        defaultInitializer,
        property,
        rest,
        target,
      } of targets
    ) {
      const propertyBit = FILE_READ_EXPORT_BITS.get(property) ?? 0;
      const defaultReachable = Boolean(
        defaultInitializer
        && !(traits.fileReadNamespace & propertyBit),
      );
      if (defaultReachable) visit(defaultInitializer, scope);
      const defaultTraits = defaultReachable
        ? fileReadTraits(defaultInitializer, scope)
        : {};
      const targetTraits = rest
        ? {
          fileReadNamespace: (
            traits.fileReadNamespace & ~excludedMask
          ),
        }
        : {
          fileRead: Boolean(
            defaultTraits.fileRead
            || (traits.fileReadNamespace & propertyBit)
          ),
          fileReadNamespace: defaultTraits.fileReadNamespace ?? 0,
        };
      const value = defaultReachable
        ? isTextFlow(defaultInitializer, scope)
        : false;
      if (define) {
        defineBinding(scope, target, value, targetTraits);
      } else {
        assignBinding(scope, target, value, targetTraits);
      }
    }
  }

  function isFileRead(expression, scope) {
    const current = unwrapExpression(expression);
    if (!ts.isCallExpression(current)) return false;
    return Boolean(fileReadTraits(current.expression, scope).fileRead);
  }

  function isTextFlow(expression, scope) {
    const current = unwrapExpression(expression);
    if (ts.isIdentifier(current)) {
      const resolved = resolvedBinding(scope, current.text);
      return resolved === undefined
        ? SOURCE_INPUT_NAME.test(current.text)
        : resolved.value;
    }
    if (isFileRead(current, scope)) return true;
    if (ts.isTemplateExpression(current)) {
      return current.templateSpans.some(
        (span) => isTextFlow(span.expression, scope),
      );
    }
    if (ts.isConditionalExpression(current)) {
      return (
        isTextFlow(current.whenTrue, scope)
        || isTextFlow(current.whenFalse, scope)
      );
    }
    if (
      ts.isBinaryExpression(current)
      && current.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
      && current.operatorToken.kind <= ts.SyntaxKind.LastAssignment
    ) {
      if (current.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
        return isTextFlow(current.right, scope);
      }
      return [
        ts.SyntaxKind.PlusEqualsToken,
        ts.SyntaxKind.AmpersandAmpersandEqualsToken,
        ts.SyntaxKind.BarBarEqualsToken,
        ts.SyntaxKind.QuestionQuestionEqualsToken,
      ].includes(current.operatorToken.kind)
        ? (
          isTextFlow(current.left, scope)
          || isTextFlow(current.right, scope)
        )
        : false;
    }
    if (
      ts.isBinaryExpression(current)
      && current.operatorToken.kind === ts.SyntaxKind.CommaToken
    ) {
      return isTextFlow(current.right, scope);
    }
    return (
      ts.isBinaryExpression(current)
      && [
        ts.SyntaxKind.PlusToken,
        ts.SyntaxKind.AmpersandAmpersandToken,
        ts.SyntaxKind.BarBarToken,
        ts.SyntaxKind.QuestionQuestionToken,
      ].includes(current.operatorToken.kind)
      && (
        isTextFlow(current.left, scope)
        || isTextFlow(current.right, scope)
      )
    );
  }

  function fileReadTraits(expression, scope) {
    const current = unwrapExpression(expression);
    if (ts.isIdentifier(current)) {
      const resolved = resolvedBinding(scope, current.text);
      return resolved
        ? {
          fileRead: resolved.fileRead,
          fileReadNamespace: resolved.fileReadNamespace,
        }
        : {};
    }
    if (
      ts.isPropertyAccessExpression(current)
      && FILE_READ_EXPORTS.has(current.name.text)
      && ts.isIdentifier(current.expression)
      && (
        (
          resolvedBinding(scope, current.expression.text)
            ?.fileReadNamespace
          ?? 0
        )
        & (FILE_READ_EXPORT_BITS.get(current.name.text) ?? 0)
      )
    ) {
      return { fileRead: true };
    }
    if (
      ts.isElementAccessExpression(current)
      && ts.isIdentifier(current.expression)
      && FILE_READ_EXPORTS.has(
        staticStringValue(current.argumentExpression),
      )
      && (
        (
          resolvedBinding(scope, current.expression.text)
            ?.fileReadNamespace
          ?? 0
        )
        & (
          FILE_READ_EXPORT_BITS.get(
            staticStringValue(current.argumentExpression),
          )
          ?? 0
        )
      )
    ) {
      return { fileRead: true };
    }
    if (ts.isConditionalExpression(current)) {
      const whenTrue = fileReadTraits(current.whenTrue, scope);
      const whenFalse = fileReadTraits(current.whenFalse, scope);
      return {
        fileRead: (
          Boolean(whenTrue.fileRead)
          || Boolean(whenFalse.fileRead)
        ),
        fileReadNamespace: (
          (whenTrue.fileReadNamespace ?? 0)
          | (whenFalse.fileReadNamespace ?? 0)
        ),
      };
    }
    if (
      ts.isBinaryExpression(current)
      && [
        ts.SyntaxKind.AmpersandAmpersandToken,
        ts.SyntaxKind.BarBarToken,
        ts.SyntaxKind.QuestionQuestionToken,
      ].includes(current.operatorToken.kind)
    ) {
      const left = fileReadTraits(current.left, scope);
      const right = fileReadTraits(current.right, scope);
      return {
        fileRead: Boolean(left.fileRead) || Boolean(right.fileRead),
        fileReadNamespace: (
          (left.fileReadNamespace ?? 0)
          | (right.fileReadNamespace ?? 0)
        ),
      };
    }
    if (
      ts.isBinaryExpression(current)
      && current.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
      && current.operatorToken.kind <= ts.SyntaxKind.LastAssignment
    ) {
      if (current.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
        return fileReadTraits(current.right, scope);
      }
      if ([
        ts.SyntaxKind.AmpersandAmpersandEqualsToken,
        ts.SyntaxKind.BarBarEqualsToken,
        ts.SyntaxKind.QuestionQuestionEqualsToken,
      ].includes(current.operatorToken.kind)) {
        const left = fileReadTraits(current.left, scope);
        const right = fileReadTraits(current.right, scope);
        return {
          fileRead: Boolean(left.fileRead) || Boolean(right.fileRead),
          fileReadNamespace: (
            (left.fileReadNamespace ?? 0)
            | (right.fileReadNamespace ?? 0)
          ),
        };
      }
      return {};
    }
    if (
      ts.isBinaryExpression(current)
      && current.operatorToken.kind === ts.SyntaxKind.CommaToken
    ) {
      return fileReadTraits(current.right, scope);
    }
    return {};
  }

  function parameterIsTextFlow(parameter) {
    if (
      (
        parameter.initializer
        && ts.isArrayLiteralExpression(parameter.initializer)
      )
      || isDefinitelyArrayType(parameter.type)
    ) {
      return false;
    }
    return (
      ts.isIdentifier(parameter.name)
      && SOURCE_INPUT_NAME.test(parameter.name.text)
    );
  }

  function isLoopStatement(node) {
    return (
      ts.isDoStatement(node)
      || ts.isWhileStatement(node)
      || ts.isForStatement(node)
      || ts.isForInStatement(node)
      || ts.isForOfStatement(node)
    );
  }

  function isAlwaysTrue(expression) {
    return unwrapExpression(expression).kind === ts.SyntaxKind.TrueKeyword;
  }

  function visitLoop(node, scope, label = undefined) {
    if (ts.isDoStatement(node)) {
      return analyzeLoop(
        scope,
        node.statement,
        (iteration) => visit(node.expression, iteration),
        false,
        !isAlwaysTrue(node.expression),
        label,
      );
    }
    if (ts.isWhileStatement(node)) {
      visit(node.expression, scope);
      const canTerminate = !isAlwaysTrue(node.expression);
      return analyzeLoop(
        scope,
        node.statement,
        (iteration) => visit(node.expression, iteration),
        canTerminate,
        canTerminate,
        label,
      );
    }
    const loopScope = newScope(scope);
    if (ts.isForStatement(node)) {
      if (node.initializer) visit(node.initializer, loopScope);
      if (node.condition) visit(node.condition, loopScope);
      const canTerminate = Boolean(
        node.condition && !isAlwaysTrue(node.condition),
      );
      return analyzeLoop(
        loopScope,
        node.statement,
        (iteration) => {
          if (node.incrementor) visit(node.incrementor, iteration);
          if (node.condition) visit(node.condition, iteration);
        },
        canTerminate,
        canTerminate,
        label,
      );
    }
    visit(node.expression, loopScope);
    visit(node.initializer, loopScope);
    return analyzeLoop(
      loopScope,
      node.statement,
      () => {},
      true,
      true,
      label,
    );
  }

  function visit(node, scope) {
    if (found) return normalCompletion();
    if (ts.isContinueStatement(node)) {
      return abruptCompletion(
        "continue",
        scope,
        node.label?.text,
      );
    }
    if (ts.isBreakStatement(node)) {
      return abruptCompletion("break", scope, node.label?.text);
    }
    if (ts.isReturnStatement(node)) {
      if (node.expression) visit(node.expression, scope);
      return abruptCompletion("return", scope);
    }
    if (ts.isLabeledStatement(node)) {
      if (isLoopStatement(node.statement)) {
        return visitLoop(node.statement, scope, node.label.text);
      }
      const result = visit(node.statement, scope);
      const exits = result.abrupt.filter(
        (completion) => (
          completion.kind === "break"
          && completion.label === node.label.text
        ),
      );
      const propagated = result.abrupt.filter(
        (completion) => !exits.includes(completion),
      );
      if (exits.length > 0) {
        const branches = result.normal ? [cloneScope(scope)] : [];
        branches.push(...exits.map((completion) => completion.scope));
        joinScopes(scope, branches);
      }
      return {
        normal: result.normal || exits.length > 0,
        abrupt: propagated,
      };
    }
    if (ts.isFunctionLike(node)) {
      const functionScope = newScope(scope, "function");
      throwCollectors.push(() => {});
      try {
        for (const parameter of node.parameters) {
          if (ts.isIdentifier(parameter.name)) {
            if (parameter.initializer) {
              visit(parameter.initializer, functionScope);
            }
            const value = Boolean(
              parameterIsTextFlow(parameter)
              || (
                parameter.initializer
                && isTextFlow(parameter.initializer, functionScope)
              )
            );
            const traits = parameter.initializer
              ? fileReadTraits(parameter.initializer, functionScope)
              : {};
            defineBinding(
              functionScope,
              parameter.name.text,
              value,
              traits,
            );
          } else if (parameter.initializer) {
            visit(parameter.initializer, functionScope);
          }
        }
        if (node.body) predeclareFunctionVars(node.body, functionScope);
        if (node.body) visit(node.body, functionScope);
      } finally {
        throwCollectors.pop();
      }
      return normalCompletion();
    }
    if (ts.isBlock(node)) {
      const blockScope = newScope(scope);
      predeclareBlockBindings(node, blockScope);
      const abrupt = [];
      for (const statement of node.statements) {
        const result = visit(statement, blockScope);
        abrupt.push(...result.abrupt);
        if (!result.normal || found) {
          return { normal: false, abrupt };
        }
      }
      return { normal: true, abrupt };
    }
    if (ts.isCatchClause(node)) {
      const catchScope = newScope(scope);
      if (
        node.variableDeclaration
        && ts.isIdentifier(node.variableDeclaration.name)
      ) {
        defineBinding(
          catchScope,
          node.variableDeclaration.name.text,
          false,
        );
      }
      return visit(node.block, catchScope);
    }
    if (ts.isIfStatement(node)) {
      visit(node.expression, scope);
      const thenScope = cloneScope(scope);
      const thenResult = visit(node.thenStatement, thenScope);
      const elseScope = cloneScope(scope);
      const elseResult = node.elseStatement
        ? visit(node.elseStatement, elseScope)
        : normalCompletion();
      const branches = [];
      if (thenResult.normal) branches.push(thenScope);
      if (elseResult.normal) branches.push(elseScope);
      if (branches.length > 0) joinScopes(scope, branches);
      return {
        normal: branches.length > 0,
        abrupt: [...thenResult.abrupt, ...elseResult.abrupt],
      };
    }
    if (ts.isTryStatement(node)) {
      const tryScope = cloneScope(scope);
      const possibleThrows = [];
      throwCollectors.push(
        (throwScope) => possibleThrows.push(cloneScope(throwScope)),
      );
      const tryResult = visit(node.tryBlock, tryScope);
      throwCollectors.pop();
      let normalBranches = tryResult.normal ? [tryScope] : [];
      const explicitThrows = tryResult.abrupt.filter(
        (completion) => completion.kind === "throw",
      );
      let abrupt = node.catchClause
        ? tryResult.abrupt.filter(
          (completion) => completion.kind !== "throw",
        )
        : [
          ...tryResult.abrupt,
          ...possibleThrows.map((throwScope) => ({
            kind: "throw",
            label: undefined,
            scope: cloneScope(throwScope),
          })),
        ];
      if (node.catchClause) {
        const catchInputs = [
          ...possibleThrows,
          ...explicitThrows.map((completion) => completion.scope),
        ];
        const catchReachable = catchInputs.length > 0;
        const catchScope = cloneScope(scope);
        joinScopes(
          catchScope,
          catchReachable
            ? catchInputs
            : [cloneScope(scope)],
        );
        const catchResult = visit(node.catchClause, catchScope);
        if (catchReachable) {
          if (catchResult.normal) normalBranches.push(catchScope);
          abrupt.push(...catchResult.abrupt);
        }
      }
      if (node.finallyBlock) {
        const finalNormal = [];
        const finalAbrupt = [];
        for (const branch of normalBranches) {
          const finalScope = cloneScope(branch);
          const finalResult = visit(node.finallyBlock, finalScope);
          if (finalResult.normal) finalNormal.push(finalScope);
          finalAbrupt.push(...finalResult.abrupt);
        }
        for (const completion of abrupt) {
          const finalScope = cloneScope(completion.scope);
          const finalResult = visit(node.finallyBlock, finalScope);
          if (finalResult.normal) {
            finalAbrupt.push({ ...completion, scope: finalScope });
          }
          finalAbrupt.push(...finalResult.abrupt);
        }
        normalBranches = finalNormal;
        abrupt = finalAbrupt;
      }
      if (normalBranches.length > 0) {
        joinScopes(scope, normalBranches);
      }
      return {
        normal: normalBranches.length > 0,
        abrupt,
      };
    }
    if (isLoopStatement(node)) return visitLoop(node, scope);
    if (ts.isSwitchStatement(node)) {
      visit(node.expression, scope);
      const clauses = node.caseBlock.clauses;
      const switchScope = newScope(scope);
      predeclareStatements(
        clauses.flatMap((clause) => [...clause.statements]),
        switchScope,
      );
      function visitCaseComparisons(branch, entry) {
        const limit = ts.isDefaultClause(clauses[entry])
          ? clauses.length - 1
          : entry;
        for (let index = 0; index <= limit; index += 1) {
          if (ts.isCaseClause(clauses[index])) {
            visit(clauses[index].expression, branch);
          }
        }
      }
      const branches = [];
      if (!clauses.some(ts.isDefaultClause)) {
        const noMatch = cloneScope(switchScope);
        for (const clause of clauses) {
          if (ts.isCaseClause(clause)) {
            visit(clause.expression, noMatch);
          }
        }
        branches.push(noMatch);
      }
      const propagated = [];
      for (let entry = 0; entry < clauses.length; entry += 1) {
        const branch = cloneScope(switchScope);
        visitCaseComparisons(branch, entry);
        let stopped = false;
        for (
          let clauseIndex = entry;
          clauseIndex < clauses.length && !stopped;
          clauseIndex += 1
        ) {
          for (const statement of clauses[clauseIndex].statements) {
            const result = visit(statement, branch);
            for (const completion of result.abrupt) {
              if (
                completion.kind === "break"
                && completion.label === undefined
              ) {
                branches.push(completion.scope);
              } else {
                propagated.push(completion);
              }
            }
            if (!result.normal) {
              stopped = true;
              break;
            }
          }
        }
        if (!stopped) branches.push(branch);
      }
      if (branches.length > 0) joinScopes(scope, branches);
      return {
        normal: branches.length > 0,
        abrupt: propagated,
      };
    }
    if (ts.isConditionalExpression(node)) {
      visit(node.condition, scope);
      const whenTrue = cloneScope(scope);
      visit(node.whenTrue, whenTrue);
      const whenFalse = cloneScope(scope);
      visit(node.whenFalse, whenFalse);
      joinScopes(scope, [whenTrue, whenFalse]);
      return normalCompletion();
    }
    if (
      ts.isBinaryExpression(node)
      && [
        ts.SyntaxKind.AmpersandAmpersandToken,
        ts.SyntaxKind.BarBarToken,
        ts.SyntaxKind.QuestionQuestionToken,
      ].includes(node.operatorToken.kind)
    ) {
      visit(node.left, scope);
      const skipped = cloneScope(scope);
      const evaluated = cloneScope(scope);
      visit(node.right, evaluated);
      joinScopes(scope, [skipped, evaluated]);
      return normalCompletion();
    }
    if (ts.isVariableDeclaration(node)) {
      const declarationList = node.parent;
      const declarationScope = (
        ts.isVariableDeclarationList(declarationList)
        && !(declarationList.flags & ts.NodeFlags.BlockScoped)
      )
        ? nearestFunctionScope(scope)
        : scope;
      if (node.initializer) visit(node.initializer, scope);
      const value = node.initializer
        ? isTextFlow(node.initializer, scope)
        : false;
      const traits = node.initializer
        ? fileReadTraits(node.initializer, scope)
        : {};
      if (ts.isObjectBindingPattern(node.name)) {
        bindObjectCapabilities(
          node.name,
          traits,
          declarationScope,
          true,
        );
        return normalCompletion();
      }
      if (!ts.isIdentifier(node.name)) return normalCompletion();
      if (
        ts.isVariableDeclarationList(declarationList)
        && !(declarationList.flags & ts.NodeFlags.BlockScoped)
      ) {
        if (node.initializer) {
          assignBinding(
            declarationScope,
            node.name.text,
            value,
            traits,
          );
        }
      } else {
        defineBinding(
          declarationScope,
          node.name.text,
          value,
          traits,
        );
      }
      return normalCompletion();
    }
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isObjectLiteralExpression(node.left)
    ) {
      visit(node.right, scope);
      bindObjectCapabilities(
        node.left,
        fileReadTraits(node.right, scope),
        scope,
        false,
      );
      return normalCompletion();
    }
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
      && node.operatorToken.kind <= ts.SyntaxKind.LastAssignment
      && ts.isIdentifier(node.left)
    ) {
      const existing = resolvedBinding(scope, node.left.text);
      const existingValue = Boolean(existing?.value);
      const existingFileRead = Boolean(existing?.fileRead);
      const existingFileReadNamespace = (
        existing?.fileReadNamespace ?? 0
      );
      const preservesValueLeft = [
        ts.SyntaxKind.PlusEqualsToken,
        ts.SyntaxKind.AmpersandAmpersandEqualsToken,
        ts.SyntaxKind.BarBarEqualsToken,
        ts.SyntaxKind.QuestionQuestionEqualsToken,
      ].includes(node.operatorToken.kind);
      const preservesCapabilityLeft = [
        ts.SyntaxKind.AmpersandAmpersandEqualsToken,
        ts.SyntaxKind.BarBarEqualsToken,
        ts.SyntaxKind.QuestionQuestionEqualsToken,
      ].includes(node.operatorToken.kind);
      const plainAssignment = (
        node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      );
      visit(node.right, scope);
      const rightValue = isTextFlow(node.right, scope);
      const rightTraits = fileReadTraits(node.right, scope);
      const value = plainAssignment
        ? rightValue
        : (
          preservesValueLeft
            ? existingValue || rightValue
            : false
        );
      const traits = plainAssignment
        ? rightTraits
        : (
          preservesCapabilityLeft
            ? {
              fileRead: (
                existingFileRead
                || Boolean(rightTraits.fileRead)
              ),
              fileReadNamespace: (
                existingFileReadNamespace
                | (rightTraits.fileReadNamespace ?? 0)
              ),
            }
            : {}
      );
      assignBinding(scope, node.left.text, value, traits);
      return normalCompletion();
    }
    if (ts.isThrowStatement(node)) {
      visit(node.expression, scope);
      collectPossibleThrow(scope);
      return abruptCompletion("throw", scope);
    }
    if (ts.isNewExpression(node)) {
      visit(node.expression, scope);
      for (const argument of node.arguments ?? []) {
        visit(argument, scope);
      }
      collectPossibleThrow(scope);
      return normalCompletion();
    }
    if (
      ts.isPropertyAccessExpression(node)
      && ["pos", "end"].includes(node.name.text)
    ) {
      found = true;
      return normalCompletion();
    }
    if (
      ts.isElementAccessExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && ["statements", "members", "properties"].includes(
        node.expression.name.text,
      )
    ) {
      found = true;
      return normalCompletion();
    }
    if (
      (
        ts.isVariableDeclaration(node)
        && ts.isArrayBindingPattern(node.name)
        && node.initializer
        && ts.isPropertyAccessExpression(node.initializer)
        && ["statements", "members", "properties"].includes(
          node.initializer.name.text,
        )
      )
      || (
        ts.isBinaryExpression(node)
        && ts.isArrayLiteralExpression(node.left)
        && ts.isPropertyAccessExpression(node.right)
        && ["statements", "members", "properties"].includes(
          node.right.name.text,
        )
      )
    ) {
      found = true;
      return normalCompletion();
    }
    if (ts.isCallExpression(node)) {
      const property = ts.isPropertyAccessExpression(node.expression)
        ? node.expression
        : undefined;
      const textReceiver = (
        property
        && ["indexOf", "slice", "substring"].includes(
          property.name.text,
        )
      )
        ? property.expression
        : undefined;
      if (textReceiver && isTextFlow(textReceiver, scope)) {
        found = true;
        return normalCompletion();
      }
      visit(node.expression, scope);
      for (const argument of node.arguments) visit(argument, scope);
      if (textReceiver && isTextFlow(textReceiver, scope)) {
        found = true;
        return normalCompletion();
      }
      collectPossibleThrow(scope);
      return normalCompletion();
    }
    const abrupt = [];
    let normal = true;
    ts.forEachChild(node, (child) => {
      if (!normal) return;
      const result = visit(child, scope);
      abrupt.push(...result.abrupt);
      normal = result.normal;
    });
    return { normal, abrupt };
  }

  const rootScope = newScope(undefined, "function");
  for (const name of fileReadImports) {
    defineBinding(rootScope, name, false, { fileRead: true });
  }
  for (const name of fileReadNamespaces) {
    defineBinding(
      rootScope,
      name,
      false,
      { fileReadNamespace: ALL_FILE_READ_EXPORTS },
    );
  }
  predeclareFunctionVars(sourceFile, rootScope);
  visit(sourceFile, rootScope);
  return found;
}


function modulePolicyOffenders(relative, source) {
  const offenders = [];
  const readsFiles = (
    /from\s+["']node:fs(?:\/promises)?["']/.test(source)
    || /import\(\s*["']node:fs(?:\/promises)?["']\s*\)/.test(source)
  );
  if (readsFiles && !DIRECT_READ_ALLOWLIST.has(relative)) {
    offenders.push(`${relative}: direct production-source read`);
  }
  if (
    hasPositionOrOrderQuery(relative, source)
  ) {
    offenders.push(`${relative}: source-position or source-order query`);
  }
  if (/(?:frontend|backend|scripts)\/[^:'"\n]+:\d+\b/.test(source)) {
    offenders.push(`${relative}: path-line identity`);
  }
  if (
    /\b(?:test|it|describe)\.(?:skip|only|todo)\s*\(/.test(source)
    || /\bskip\s*:\s*(?:true|["'])/.test(source)
  ) {
    offenders.push(`${relative}: disabled or exclusive test`);
  }
  return offenders;
}


async function policyOffenders() {
  const offenders = [];
  for (const absolute of await policyModules()) {
    const relative = path.relative(APP_DIR, absolute).replaceAll(path.sep, "/");
    const source = await readFile(absolute, "utf8");
    offenders.push(...modulePolicyOffenders(relative, source));
  }
  return offenders.sort();
}


test("static source scans are registered with a category and reason", () => {
  for (const [name, contract] of Object.entries(STATIC_SOURCE_CONTRACTS)) {
    assert.ok(contract.category, name);
    assert.ok(contract.reason, name);
    assert.ok(contract.roots.length > 0, name);
  }
});


test("source policy rejects position identities without banning ordinary arrays", () => {
  assert.deepEqual(
    modulePolicyOffenders(
      "example.test.mjs",
      "const start = node.getStart();",
    ),
    ["example.test.mjs: source-position or source-order query"],
  );
  for (const mutation of [
    "const start = node.getFullStart();",
    "const finish = node.end;",
    "const first = sourceFile.statements[0];",
    "const first = sourceFile.members.at(0);",
    "const [first] = sourceFile.statements;",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders("example.test.mjs", mutation),
      ["example.test.mjs: source-position or source-order query"],
      mutation,
    );
  }
  assert.deepEqual(
    modulePolicyOffenders(
      "example.test.mjs",
      "import { readFile } from 'node:fs/promises';\n"
        + "const source = await readFile(path, 'utf8');\n"
        + "source.slice(source.indexOf('a'));",
    ),
    [
      "example.test.mjs: direct production-source read",
      "example.test.mjs: source-position or source-order query",
    ],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/source-helper.mjs",
      "export function fragment(payload) { return payload.slice(1); }",
    ),
    ["test/source-helper.mjs: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/array-helper.ts",
      "export function clone(values: string[]) { return values.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/local-array-helper.mjs",
      "export function clone() { const values = [1]; return values.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/readonly-array-helper.ts",
      "export function clone(values: readonly string[]) {"
        + " return values.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/scoped-array-helper.ts",
      "function fragment(payload: string) { return payload.slice(1); }\n"
        + "function clone(payload: string[]) { return payload.slice(); }",
    ),
    ["test/scoped-array-helper.ts: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/unpoisoned-array-helper.ts",
      "function inspect(payload: string) { return payload.length; }\n"
        + "function clone(payload: string[]) { return payload.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/ordinary-string-helper.ts",
      "function trimPrefix(value: string) { return value.slice(1); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/typed-array-helper.ts",
      "function clone(payload: Uint8Array) { return payload.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "example.test.mjs",
      "const copy = values.slice().sort();",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "feature.test.mjs",
      "function fragment(payload) { return payload.slice(1); }",
    ),
    ["feature.test.mjs: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFile } from 'node:fs/promises';\n"
        + "const fragment = (await readFile(path, 'utf8')).slice(1);",
    ),
    ["test/semantic-source.mjs: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import fs from 'node:fs';\n"
        + "let payload;\npayload = fs.readFileSync(path, 'utf8');\n"
        + "payload.substring(1);",
    ),
    ["test/semantic-source.mjs: source-position or source-order query"],
  );
  for (const mutation of [
    "import { readFileSync } from 'node:fs';\n"
      + "let data = readFileSync(path, 'utf8');\n"
      + "if (flag) { data = []; }\ndata.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data;\nif (flag) { data = readFileSync(path, 'utf8'); }"
      + " else { data = []; }\ndata.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nwhile (flag) {"
      + " data = readFileSync(path, 'utf8'); }\ndata.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nwhile (flag) {"
      + " data.slice(1); data = readFileSync(path, 'utf8'); }",
    "import { readFileSync } from 'node:fs';\n"
      + "let data;\ntry { data = readFileSync(path, 'utf8'); }"
      + " catch { data = []; }\ndata.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\ntry {"
      + " data = readFileSync(path, 'utf8'); risky();"
      + " } catch { data.slice(1); }",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\ntry {"
      + " risky(data = readFileSync(path, 'utf8'));"
      + " } catch { data.slice(1); }",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\ntry {"
      + " throw (data = readFileSync(path, 'utf8'));"
      + " } catch { data.slice(1); }",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nswitch (kind) {"
      + " case 1: data = readFileSync(path, 'utf8');"
      + " case 2: data.slice(1); break; }",
    "import { readFileSync } from 'node:fs';\n"
      + "if (flag) { var data = readFileSync(path, 'utf8'); }\n"
      + "data.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = readFileSync(path, 'utf8');\n"
      + "try {} catch (data) {}\ndata.slice(1);",
    "import { readFile as load } from 'node:fs/promises';\n"
      + "const data = await load(path, 'utf8');\ndata.slice(1);",
    "import { readFile } from 'node:fs/promises';\n"
      + "const load = readFile;\n"
      + "const data = await load(path, 'utf8');\ndata.slice(1);",
    "import { readFile } from 'node:fs/promises';\n"
      + "let load;\nif (flag) { load = readFile; }"
      + " else { load = other; }\n"
      + "const data = await load(path, 'utf8');\ndata.slice(1);",
    "import fs from 'node:fs';\n"
      + "const load = fs.readFileSync;\n"
      + "const data = load(path, 'utf8');\ndata.slice(1);",
    "import * as fs from 'node:fs';\n"
      + "const { readFileSync: load } = fs;\n"
      + "const data = load(path, 'utf8');\ndata.slice(1);",
    "import * as fs from 'node:fs';\n"
      + "const { ['readFileSync']: load } = fs;\n"
      + "const data = load(path, 'utf8');\ndata.slice(1);",
    "import * as fs from 'node:fs';\n"
      + "const load = fs['readFileSync'];\n"
      + "const data = load(path, 'utf8');\ndata.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "const load = flag ? readFileSync : other;\n"
      + "const data = load(path, 'utf8');\ndata.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\ndata += readFileSync(path, 'utf8');\n"
      + "data.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let part = [];\nlet data = [];\n"
      + "data += (part = readFileSync(path, 'utf8'), part);\n"
      + "data.slice(1);",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nwhile (next()) { switch (kind) {"
      + " case 1: data = readFileSync(path, 'utf8'); continue;"
      + " case 2: data.slice(1); break; } }",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nwhile (next()) { switch (kind) {"
      + " case 1: if (flag) {"
      + " data = readFileSync(path, 'utf8'); continue; } break;"
      + " case 2: data.slice(1); break; } }",
    "import { readFileSync } from 'node:fs';\n"
      + "function f() { let data = []; try {"
      + " data = readFileSync(path, 'utf8'); return;"
      + " } finally { data.slice(); } }",
    "import { readFileSync } from 'node:fs';\n"
      + "function f() { let data = []; try {"
      + " data = readFileSync(path, 'utf8'); throw error;"
      + " } finally { data.slice(); } }",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nwhile (next()) {"
      + " data = readFileSync(path, 'utf8'); break; data = []; }\n"
      + "data.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nouter: while (next()) {"
      + " while (inner()) { data = readFileSync(path, 'utf8');"
      + " continue outer; } data = []; }\ndata.slice();",
    "import * as fs from 'node:fs';\n"
      + "let load;\n({ readFileSync: load } = fs);\n"
      + "const data = load(path, 'utf8'); data.slice();",
    "import * as fs from 'node:fs';\n"
      + "const { ...rest } = fs;\n"
      + "const data = rest.readFileSync(path, 'utf8'); data.slice();",
    "import * as fs from 'node:fs';\n"
      + "const { readFileSync: omit, ...rest } = fs;\n"
      + "const data = await rest.readFile(path, 'utf8'); data.slice();",
    "import * as fs from 'node:fs';\n"
      + "const { ['read' + 'FileSync']: load } = fs;\n"
      + "const data = load(path, 'utf8'); data.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let part = [];\nlet data = [];\n"
      + "data += (0, part = readFileSync(path, 'utf8'));\n"
      + "data.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nfor (;;) {"
      + " data = readFileSync(path, 'utf8'); break; }\ndata.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nswitch (k) {"
      + " case (data = readFileSync(path, 'utf8'), 1): break;"
      + " default: data.slice(); }",
    "import { readFileSync } from 'node:fs';\n"
      + "const { missing: load = readFileSync } = {};\n"
      + "const data = load(path, 'utf8'); data.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "const { missing: data = readFileSync(path, 'utf8') } = {};\n"
      + "data.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = readFileSync(path, 'utf8');\n"
      + "const copy = (data += 'x'); copy.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let load = readFileSync;\nconst alias = (load ||= other);\n"
      + "const data = alias(path, 'utf8'); data.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = readFileSync(path, 'utf8');\n"
      + "const copy = (0, data += 'x'); copy.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = readFileSync(path, 'utf8');\ntry {"
      + " try { throw err; } finally {}"
      + " } catch { data.slice(); }",
    "function fragment(payload, alias = payload) {"
      + " return alias.slice(1); }",
    "import { readFileSync } from 'node:fs';\n"
      + "const data = `${readFileSync(path, 'utf8')}`;\n"
      + "data.slice(1);",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders("test/semantic-source.mjs", mutation),
      ["test/semantic-source.mjs: source-position or source-order query"],
      mutation,
    );
  }
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFileSync } from 'node:fs';\n"
        + "let data = readFileSync(path, 'utf8');\n"
        + "data = [];\ndata.slice(1);",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFileSync } from 'node:fs';\n"
        + "let data = [];\nfunction inspect() {"
        + " switch (kind) { case 1:"
        + " data = readFileSync(path, 'utf8'); return;"
        + " case 2: data.slice(); } }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFileSync } from 'node:fs';\n"
        + "let data = [];\ntry {"
        + " data = readFileSync(path, 'utf8');"
        + " } catch { data.slice(1); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFileSync } from 'node:fs';\n"
        + "let data = readFileSync(path, 'utf8');\n"
        + "do { data = []; } while (flag);\ndata.slice(1);",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFile } from 'node:fs/promises';\n"
        + "function clone(readFile: () => number[]) {"
        + " const data = readFile(); return data.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import fs from 'node:fs';\n"
        + "function clone(fs: { readFileSync(): number[] }) {"
        + " const data = fs.readFileSync(); return data.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "function readFile() { return []; }\n"
        + "const data = readFile(); data.slice();",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFileSync } from 'node:fs';\n"
        + "function f() { function readFileSync() { return []; }"
        + " const data = readFileSync(); data.slice(); }",
    ),
    [],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFileSync } from 'node:fs';\n"
        + "function f() { const data = readFileSync();"
        + " function readFileSync() { return []; } data.slice(); }",
    ),
    [],
  );
  for (const mutation of [
    "import { readFileSync } from 'node:fs';\n"
      + "function f() { let data = []; while (next()) {"
      + " data = readFileSync(path, 'utf8'); return; }"
      + " data.slice(); }",
    "import { readFileSync } from 'node:fs';\n"
      + "function f() { let data = []; while (next()) {"
      + " data = readFileSync(path, 'utf8'); throw error; }"
      + " data.slice(); }",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = [];\nwhile (next()) { try {"
      + " data = readFileSync(path, 'utf8'); continue;"
      + " } finally { data = []; } }\ndata.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "switch (k) { case 1: {"
      + " const data = readFileSync(); data.slice(); break; }"
      + " case 2: const readFileSync = () => []; break; }",
    "import { readFileSync } from 'node:fs';\n"
      + "function f() { let data = readFileSync();"
      + " while (true) { return; } data.slice(); }",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = readFileSync(path, 'utf8');\ntry {"
      + " try { throw err; } catch { data = []; }"
      + " } catch {}\ndata.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = readFileSync(path, 'utf8');\ntry {"
      + " try { risky(); data = []; } catch { data = []; }"
      + " } catch {}\ndata.slice();",
    "import * as fs from 'node:fs';\n"
      + "const { readFileSync: omit, ...rest } = fs;\n"
      + "const data = rest.readFileSync(path, 'utf8'); data.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let data = readFileSync(path, 'utf8');\ntry {"
      + " function helper() { risky(); }\ndata = [];"
      + " } catch {}\ndata.slice();",
    "import * as fs from 'node:fs';\n"
      + "let data = fs.readFileSync(path, 'utf8');\ntry {"
      + " const { readFileSync: load = risky() } = fs;\ndata = [];"
      + " } catch {}\ndata.slice();",
    "import { readFileSync } from 'node:fs';\n"
      + "let load = readFileSync;\nconst alias = (load += 'x');\n"
      + "const data = alias(path, 'utf8'); data.slice();",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders("test/semantic-source.mjs", mutation),
      [],
      mutation,
    );
  }
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFile } from 'node:fs/promises';\n"
        + "let load = readFile;\nload = other;\n"
        + "const data = load(); data.slice();",
    ),
    [],
  );
});


test("source policy follows test imports to root TypeScript helper modules", () => {
  const modules = new Map([
    [
      "feature.test.mjs",
      "import { fragment } from './feature-helper.ts'; void fragment;",
    ],
    [
      "feature-helper.ts",
      "export function fragment(text: string) { return text.slice(1); }",
    ],
    ["page.tsx", "export default function Page() { return null; }"],
  ]);
  assert.deepEqual(
    [...policyRelativeModules(modules)].sort(),
    ["feature-helper.ts", "feature.test.mjs"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "feature-helper.ts",
      modules.get("feature-helper.ts"),
    ),
    ["feature-helper.ts: source-position or source-order query"],
  );
});


test("source policy follows dynamic imports to test-only helpers", () => {
  const modules = new Map([
    [
      "feature.test.mjs",
      "const helper = await import('./dynamic-helper.ts'); void helper;",
    ],
    [
      "dynamic-helper.ts",
      "export function fragment(payload: string) { return payload.slice(1); }",
    ],
    ["page.tsx", "export default function Page() { return null; }"],
  ]);
  assert.deepEqual(
    [...policyRelativeModules(modules)].sort(),
    ["dynamic-helper.ts", "feature.test.mjs"],
  );

  modules.set(
    "feature.test.mjs",
    "const helper = await import(`./dynamic-helper.ts`); void helper;",
  );
  assert.deepEqual(
    [...policyRelativeModules(modules)].sort(),
    ["dynamic-helper.ts", "feature.test.mjs"],
  );
});


test("source policy includes semantic helper modules, not only test entrypoints", async () => {
  const relative = (await policyModules()).map(
    (absolute) => path.relative(APP_DIR, absolute).replaceAll(path.sep, "/"),
  );
  assert.ok(relative.includes("test/semantic-source.mjs"));
  assert.ok(relative.includes("test/setup.ts"));
});


test("frontend tests do not inspect production source layout directly", async () => {
  assert.deepEqual(await policyOffenders(), []);
});
