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
  // Central semantic AST helper: callers consume nodes, never text positions.
  "test/semantic-source.mjs",
  // Contains this policy's mutation strings, not production source inspection.
  "test/static-source-policy.test.mjs",
  // Reads package/config metadata only.
  "test-runner-config.test.mjs",
]);
const STRICT_TEXT_READER_ALLOWLIST = new Set([
  // This helper owns production source text and must expose only AST semantics.
  "test/semantic-source.mjs",
]);
const SOURCE_INPUT_NAME = (
  /^(?:source|sourceText|productionSource|content|text|payload)$/i
);
const TEXT_POSITION_METHODS = new Set([
  "at",
  "charAt",
  "charCodeAt",
  "codePointAt",
  "indexOf",
  "slice",
  "split",
  "substring",
]);
const AST_COLLECTIONS = new Set([
  "members",
  "properties",
  "statements",
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


function unwrapExpression(expression) {
  let current = expression;
  while (
    ts.isParenthesizedExpression(current)
    || ts.isAsExpression(current)
    || ts.isTypeAssertionExpression(current)
    || ts.isNonNullExpression(current)
    || ts.isAwaitExpression(current)
  ) {
    current = current.expression;
  }
  return current;
}


function staticPropertyName(expression) {
  if (ts.isPropertyAccessExpression(expression)) {
    return expression.name.text;
  }
  if (
    ts.isElementAccessExpression(expression)
    && expression.argumentExpression
    && ts.isStringLiteralLike(
      unwrapExpression(expression.argumentExpression),
    )
  ) {
    return unwrapExpression(expression.argumentExpression).text;
  }
  return undefined;
}


function staticModuleCallSpecifier(node) {
  if (!ts.isCallExpression(node) || node.arguments.length === 0) {
    return undefined;
  }
  const first = unwrapExpression(node.arguments[0]);
  if (!ts.isStringLiteralLike(first)) return undefined;
  if (node.expression.kind === ts.SyntaxKind.ImportKeyword) {
    return first.text;
  }
  if (
    ts.isIdentifier(node.expression)
    && node.expression.text === "require"
  ) {
    return first.text;
  }
  if (
    (
      ts.isPropertyAccessExpression(node.expression)
      || ts.isElementAccessExpression(node.expression)
    )
    && staticPropertyName(node.expression) === "require"
    && ts.isIdentifier(node.expression.expression)
    && node.expression.expression.text === "module"
  ) {
    return first.text;
  }
  return undefined;
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
      ts.isImportEqualsDeclaration(node)
      && ts.isExternalModuleReference(node.moduleReference)
      && node.moduleReference.expression
      && ts.isStringLiteralLike(node.moduleReference.expression)
    ) {
      const resolved = resolve(node.moduleReference.expression.text);
      if (resolved) imports.push(resolved);
    }
    const callSpecifier = staticModuleCallSpecifier(node);
    if (callSpecifier !== undefined) {
      const resolved = resolve(callSpecifier);
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


function moduleReadsFiles(sourceFile) {
  let readsFiles = false;
  const isFsSpecifier = (value) => (
    /^(?:node:)?fs(?:\/promises)?$/.test(value)
  );
  function visit(node) {
    if (
      (
        ts.isImportDeclaration(node)
        || ts.isExportDeclaration(node)
      )
      && node.moduleSpecifier
      && ts.isStringLiteral(node.moduleSpecifier)
      && isFsSpecifier(node.moduleSpecifier.text)
    ) {
      readsFiles = true;
      return;
    }
    if (
      ts.isImportEqualsDeclaration(node)
      && ts.isExternalModuleReference(node.moduleReference)
      && node.moduleReference.expression
      && ts.isStringLiteralLike(node.moduleReference.expression)
      && isFsSpecifier(node.moduleReference.expression.text)
    ) {
      readsFiles = true;
      return;
    }
    const callSpecifier = staticModuleCallSpecifier(node);
    if (callSpecifier !== undefined && isFsSpecifier(callSpecifier)) {
      readsFiles = true;
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return readsFiles;
}


function hasPositionOrOrderQuery(relative, source) {
  const sourceFile = ts.createSourceFile(
    relative,
    source,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(relative),
  );
  const readsFiles = moduleReadsFiles(sourceFile);
  const registeredReader = (
    readsFiles && STRICT_TEXT_READER_ALLOWLIST.has(relative)
  );
  let found = false;

  function newScope(parent = undefined) {
    return { parent, bindings: new Map() };
  }

  function bindingKind(scope, name) {
    for (let current = scope; current; current = current.parent) {
      if (current.bindings.has(name)) {
        return current.bindings.get(name);
      }
    }
    return SOURCE_INPUT_NAME.test(name) ? "source" : "unknown";
  }

  function objectPropertyType(type, property) {
    if (!type || !ts.isTypeLiteralNode(type)) return undefined;
    for (const member of type.members) {
      if (
        ts.isPropertySignature(member)
        && member.name
        && (
          (
            ts.isIdentifier(member.name)
            || ts.isStringLiteralLike(member.name)
          )
          && member.name.text === property
        )
      ) {
        return member.type;
      }
    }
    return undefined;
  }

  function isRawReadCall(expression) {
    const current = unwrapExpression(expression);
    return Boolean(
      registeredReader
      && ts.isCallExpression(current)
      && ["readFile", "readFileSync"].includes(
        staticPropertyName(current.expression)
        ?? (
          ts.isIdentifier(current.expression)
            ? current.expression.text
            : ""
        ),
      )
    );
  }

  function expressionKind(expression, scope) {
    if (!expression) return "unknown";
    const current = unwrapExpression(expression);
    if (ts.isIdentifier(current)) {
      return bindingKind(scope, current.text);
    }
    if (ts.isArrayLiteralExpression(current)) return "array";
    if (isRawReadCall(current)) return "source";
    if (
      ts.isTemplateExpression(current)
      && current.templateSpans.some(
        (span) => expressionKind(span.expression, scope) === "source",
      )
    ) {
      return "source";
    }
    if (
      ts.isBinaryExpression(current)
      && current.operatorToken.kind === ts.SyntaxKind.PlusToken
      && (
        expressionKind(current.left, scope) === "source"
        || expressionKind(current.right, scope) === "source"
      )
    ) {
      return "source";
    }
    if (
      ts.isCallExpression(current)
      && ts.isIdentifier(current.expression)
      && current.expression.text === "String"
      && current.arguments.some(
        (argument) => expressionKind(argument, scope) === "source",
      )
    ) {
      return "source";
    }
    if (
      (
        ts.isPropertyAccessExpression(current)
        || ts.isElementAccessExpression(current)
      )
      && AST_COLLECTIONS.has(staticPropertyName(current))
    ) {
      return "ast-collection";
    }
    if (
      ts.isCallExpression(current)
      && (
        ts.isPropertyAccessExpression(current.expression)
        || ts.isElementAccessExpression(current.expression)
      )
    ) {
      return expressionKind(current.expression.expression, scope);
    }
    return "unknown";
  }

  function declarePattern(name, type, initializer, scope) {
    if (ts.isIdentifier(name)) {
      const initializerKind = expressionKind(initializer, scope);
      const kind = (
        isDefinitelyArrayType(type)
        || initializerKind === "array"
      )
        ? "array"
        : initializerKind !== "unknown"
          ? initializerKind
          : SOURCE_INPUT_NAME.test(name.text)
            ? "source"
            : "unknown";
      scope.bindings.set(name.text, kind);
      return;
    }
    if (ts.isObjectBindingPattern(name)) {
      for (const element of name.elements) {
        if (element.dotDotDotToken) {
          declarePattern(element.name, undefined, undefined, scope);
          continue;
        }
        const property = element.propertyName
          ? (
            ts.isIdentifier(element.propertyName)
            || ts.isStringLiteralLike(element.propertyName)
          )
            ? element.propertyName.text
            : undefined
          : ts.isIdentifier(element.name)
            ? element.name.text
            : undefined;
        declarePattern(
          element.name,
          objectPropertyType(type, property),
          undefined,
          scope,
        );
      }
      return;
    }
    for (const element of name.elements) {
      if (!ts.isOmittedExpression(element)) {
        declarePattern(element.name, undefined, undefined, scope);
      }
    }
  }

  function predeclareStatements(statements, scope) {
    for (const statement of statements) {
      if (!ts.isVariableStatement(statement)) continue;
      for (const declaration of statement.declarationList.declarations) {
        declarePattern(
          declaration.name,
          declaration.type,
          declaration.initializer,
          scope,
        );
      }
    }
  }

  function textReceiverKind(expression, scope) {
    return expressionKind(expression, scope);
  }

  function visit(node, scope) {
    if (found) return;
    if (ts.isSourceFile(node)) {
      predeclareStatements(node.statements, scope);
      for (const statement of node.statements) visit(statement, scope);
      return;
    }
    if (ts.isFunctionLike(node)) {
      const functionScope = newScope(scope);
      for (const parameter of node.parameters) {
        declarePattern(
          parameter.name,
          parameter.type,
          parameter.initializer,
          functionScope,
        );
      }
      if (node.body) visit(node.body, functionScope);
      return;
    }
    if (ts.isBlock(node)) {
      const blockScope = newScope(scope);
      predeclareStatements(node.statements, blockScope);
      for (const statement of node.statements) visit(statement, blockScope);
      return;
    }
    if (ts.isVariableDeclaration(node)) {
      declarePattern(node.name, node.type, node.initializer, scope);
      if (
        (
          ts.isArrayBindingPattern(node.name)
          || ts.isObjectBindingPattern(node.name)
        )
        && expressionKind(node.initializer, scope) === "source"
      ) {
        found = true;
        return;
      }
    }
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isIdentifier(node.left)
    ) {
      const rightKind = expressionKind(node.right, scope);
      if (rightKind !== "unknown") {
        for (let current = scope; current; current = current.parent) {
          if (current.bindings.has(node.left.text)) {
            current.bindings.set(node.left.text, rightKind);
            break;
          }
        }
      }
    }
    if (
      (
        ts.isPropertyAccessExpression(node)
        || ts.isElementAccessExpression(node)
      )
      && ["pos", "end"].includes(staticPropertyName(node))
    ) {
      found = true;
      return;
    }
    if (
      (
        ts.isPropertyAccessExpression(node)
        || ts.isElementAccessExpression(node)
      )
      && staticPropertyName(node) === "length"
      && textReceiverKind(node.expression, scope) === "source"
    ) {
      found = true;
      return;
    }
    if (
      ts.isElementAccessExpression(node)
      && textReceiverKind(node.expression, scope) === "source"
    ) {
      found = true;
      return;
    }
    if (
      ts.isElementAccessExpression(node)
      && expressionKind(node.expression, scope) === "ast-collection"
    ) {
      found = true;
      return;
    }
    if (
      ts.isVariableDeclaration(node)
      && ts.isArrayBindingPattern(node.name)
      && node.initializer
      && expressionKind(node.initializer, scope) === "ast-collection"
    ) {
      found = true;
      return;
    }
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.EqualsToken
      && ts.isArrayLiteralExpression(node.left)
      && [
        "ast-collection",
        "source",
      ].includes(expressionKind(node.right, scope))
    ) {
      found = true;
      return;
    }
    if (
      ts.isCallExpression(node)
      && (
        ts.isPropertyAccessExpression(node.expression)
        || ts.isElementAccessExpression(node.expression)
      )
    ) {
      const method = staticPropertyName(node.expression);
      const receiver = node.expression.expression;
      if (["getStart", "getFullStart", "getEnd"].includes(method)) {
        found = true;
        return;
      }
      if (
        method === "at"
        && expressionKind(receiver, scope) === "ast-collection"
      ) {
        found = true;
        return;
      }
      if (
        TEXT_POSITION_METHODS.has(method)
        && textReceiverKind(receiver, scope) === "source"
      ) {
        found = true;
        return;
      }
    }
    ts.forEachChild(node, (child) => visit(child, scope));
  }
  visit(sourceFile, newScope());
  return found;
}


function modulePolicyOffenders(relative, source) {
  const sourceFile = ts.createSourceFile(
    relative,
    source,
    ts.ScriptTarget.Latest,
    true,
    scriptKind(relative),
  );
  const offenders = [];
  const readsFiles = moduleReadsFiles(sourceFile);
  if (readsFiles && !DIRECT_READ_ALLOWLIST.has(relative)) {
    offenders.push(`${relative}: direct production-source read`);
  }
  if (hasPositionOrOrderQuery(relative, source)) {
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


test("source policy rejects layout identities with bounded syntax rules", () => {
  for (const mutation of [
    "const start = node.getStart();",
    "const start = node.getFullStart();",
    "const finish = node.end;",
    "const first = sourceFile.statements[0];",
    "const first = sourceFile.members.at(0);",
    "const [first] = sourceFile.statements;",
    "[first] = sourceFile.properties;",
    "const firstLine = source.split('\\n')[0];",
    "const firstCharacter = source[0];",
    "const sourceSize = source.length;",
    "node['pos'];",
    "node['getStart']();",
    "sourceFile['statements'][0];",
    "const items = sourceFile.statements; items[0];",
    "(source).slice(1);",
    "(source as string).slice(1);",
    "source.trim().split('\\n');",
    "source.at(0);",
    "const [first] = source;",
    "const body = source; body.slice(1);",
    "let body; body = source; body.slice(1);",
    "String(source).slice(1);",
    "`${source}`.slice(1);",
    "(source + '').slice(1);",
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
      "example.test.ts",
      "function bad(source: string) { return source.slice(1); }\n"
        + "function good(source: string[]) { return source.slice(); }",
    ),
    ["example.test.ts: source-position or source-order query"],
  );
  for (const source of [
    "import fs from 'fs'; void fs;",
    "const fs = require('node:fs'); void fs;",
    "const fs = await import('fs/promises'); void fs;",
    "const fs = await import('node:fs', {}); void fs;",
    "const fs = require('fs', undefined); void fs;",
    "const fs = module.require('node:fs'); void fs;",
    "import fs = require('node:fs'); void fs;",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders("example.test.mjs", source),
      ["example.test.mjs: direct production-source read"],
      source,
    );
  }
  assert.deepEqual(
    modulePolicyOffenders(
      "test/semantic-source.mjs",
      "import { readFile } from 'node:fs/promises';\n"
        + "const body = await readFile(path, 'utf8');\nbody.slice();",
    ),
    ["test/semantic-source.mjs: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/source-helper.ts",
      "export function fragment(text: string) { return text.slice(1); }",
    ),
    ["test/source-helper.ts: source-position or source-order query"],
  );
  assert.deepEqual(
    modulePolicyOffenders(
      "test/static-source-policy.test.mjs",
      "const source = 'production'; source.slice(1);",
    ),
    ["test/static-source-policy.test.mjs: source-position or source-order query"],
  );
  for (const operation of [
    "const body = await readFile(url, 'utf8'); body[0];",
    "const body = await readFile(url, 'utf8'); body.length;",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders(
        "test/semantic-source.mjs",
        "import { readFile } from 'node:fs/promises';\n" + operation,
      ),
      ["test/semantic-source.mjs: source-position or source-order query"],
      operation,
    );
  }
});


test("source policy leaves ordinary array operations alone", () => {
  for (const source of [
    "const values = [1]; values.slice().sort();",
    "function clone(values: string[]) { return values.slice(); }",
    "function clone(payload: readonly string[]) { return payload.slice(); }",
    "function clone(payload: Uint8Array) { return payload.slice(); }",
    "function trimPrefix(value: string) { return value.slice(1); }",
    "function good({ source }: { source: string[] }) {"
      + " return source.slice(); }",
  ]) {
    assert.deepEqual(
      modulePolicyOffenders("example.test.ts", source),
      [],
      source,
    );
  }
});


test("source policy stays bounded for repeated callable syntax", () => {
  const repeatedCalls = Array.from(
    { length: 1_000 },
    () => "f();",
  ).join("\n");
  const source = [
    "const a = () => [];",
    "const b = () => [];",
    "let f = flag ? a : b;",
    repeatedCalls,
  ].join("\n");
  assert.deepEqual(modulePolicyOffenders("example.test.mjs", source), []);
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

  for (const entrypoint of [
    "const helper = await import('./dynamic-helper.ts', {}); void helper;",
    "const helper = require('./dynamic-helper.ts'); void helper;",
    "const helper = module.require('./dynamic-helper.ts'); void helper;",
  ]) {
    modules.set("feature.test.mjs", entrypoint);
    assert.deepEqual(
      [...policyRelativeModules(modules)].sort(),
      ["dynamic-helper.ts", "feature.test.mjs"],
      entrypoint,
    );
  }

  const typescriptModules = new Map([
    [
      "feature.test.ts",
      "import helper = require('./dynamic-helper.ts'); void helper;",
    ],
    ["dynamic-helper.ts", "export const helper = true;"],
  ]);
  assert.deepEqual(
    [...policyRelativeModules(typescriptModules)].sort(),
    ["dynamic-helper.ts", "feature.test.ts"],
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
