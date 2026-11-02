"use strict";

const fs = require("node:fs");
const path = require("node:path");
let ts;
try {
  ts = require(path.join(__dirname, "../frontend/node_modules/typescript"));
} catch (error) {
  if (error.code !== "MODULE_NOT_FOUND") throw error;
  if (require.main !== module) throw new Error("TypeScript dependency missing; install frontend dependencies first");
  process.stderr.write("TypeScript dependency missing; install frontend dependencies first\n");
  process.exit(2);
}

function indexText(text, fileName, details = true) {
  const file = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true,
    fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  if (file.parseDiagnostics.length) throw new Error(`TypeScript syntax error: ${fileName}`);
  const symbols = [];
  const nodes = new Map();
  const imports = file.statements.filter(ts.isImportDeclaration);
  function walk(node, parents) {
    let name;
    let kind;
    let selected = node;
    if (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node) || ts.isInterfaceDeclaration(node)
      || ts.isTypeAliasDeclaration(node) || ts.isEnumDeclaration(node)) {
      name = node.name?.text || "default";
      kind = ts.isFunctionDeclaration(node) ? "function" : ts.isClassDeclaration(node) ? "class" : "type";
    } else if (ts.isMethodDeclaration(node) || ts.isGetAccessorDeclaration(node) || ts.isSetAccessorDeclaration(node)) {
      name = node.name.getText(file);
      kind = "method";
    } else if (ts.isConstructorDeclaration(node)) {
      name = "constructor";
      kind = "constructor";
    } else if ((ts.isVariableDeclaration(node) || ts.isPropertyDeclaration(node)) && node.initializer
      && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))) {
      name = node.name.getText(file);
      kind = "function";
      if (ts.isVariableDeclaration(node) && node.parent.declarations.length === 1) selected = node.parent.parent;
    }
    const scope = name ? [...parents, name] : parents;
    if (name) {
      const item = {
        name: scope.join("."), kind,
        line: file.getLineAndCharacterOfPosition(selected.getStart(file)).line + 1,
        end: file.getLineAndCharacterOfPosition(selected.end).line + 1,
      };
      if (details) item.source = text.slice(selected.getStart(file), selected.end);
      symbols.push(item);
      nodes.set(item, node);
    }
    ts.forEachChild(node, child => walk(child, scope));
  }
  walk(file, []);
  const declarations = new Set(symbols.map(s => s.name));
  for (const symbol of details ? symbols : []) {
    const used = new Set();
    function names(node) {
      if (ts.isIdentifier(node)) used.add(node.text);
      ts.forEachChild(node, names);
    }
    names(nodes.get(symbol));
    symbol.imports = imports.flatMap(node => {
      const bindings = node.importClause;
      if (!bindings) return [];
      const parts = [];
      if (bindings.name && used.has(bindings.name.text)) parts.push(bindings.name.text);
      const named = bindings.namedBindings;
      if (named) {
        if (ts.isNamespaceImport(named)) {
          if (used.has(named.name.text)) parts.push(`* as ${named.name.text}`);
        } else {
          const elements = named.elements.filter(e => used.has(e.name.text));
          if (elements.length) parts.push(`{ ${elements.map(e => e.getText(file)).join(", ")} }`);
        }
      }
      return parts.length ? [`import ${bindings.isTypeOnly ? "type " : ""}${parts.join(", ")} from ${node.moduleSpecifier.getText(file)};`] : [];
    });
    const scope = symbol.name.split(".");
    const dependencies = new Set();
    for (const name of used) {
      for (let size = scope.length - 1; size >= 0; size--) {
        const candidate = [...scope.slice(0, size), name].join(".");
        if (declarations.has(candidate)) {
          if (candidate !== symbol.name && !candidate.startsWith(`${symbol.name}.`)) dependencies.add(candidate);
          break;
        }
      }
    }
    symbol.dependencies = [...dependencies].sort();
  }
  const aliases = [];
  for (const statement of imports) {
    const clause = statement.importClause;
    if (!clause) continue;
    const module = statement.moduleSpecifier.text;
    const base = module.startsWith(".") ? path.resolve(path.dirname(fileName), module) : "";
    const owner = base && [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts"), path.join(base, "index.tsx")]
      .find(candidate => fs.existsSync(candidate) && fs.statSync(candidate).isFile());
    const add = (name, original) => aliases.push({ name, original, module, owner: owner || "" });
    if (clause.name) add(clause.name.text, "default");
    if (clause.namedBindings) {
      if (ts.isNamespaceImport(clause.namedBindings)) add(clause.namedBindings.name.text, "*");
      else for (const e of clause.namedBindings.elements) add(e.name.text, e.propertyName?.text || e.name.text);
    }
  }
  return { symbols, file, nodes, aliases };
}

function moduleMatches(specifier, source, target) {
  if (!specifier.startsWith(".")) return false;
  const base = path.resolve(path.dirname(source), specifier);
  return [base, `${base}.ts`, `${base}.tsx`, path.join(base, "index.ts"), path.join(base, "index.tsx")]
    .some(candidate => candidate === target);
}

function isReferenceIdentifier(node) {
  const parent = node.parent;
  if (ts.isShorthandPropertyAssignment(parent)) return true;
  // Named syntax declares a binding or a property; its spelling is not a
  // lexical read. Computed names have an intervening ComputedPropertyName.
  if (parent.name === node || ts.isImportSpecifier(parent) || ts.isImportClause(parent)
    || ts.isExportSpecifier(parent) || ts.isJsxNamespacedName(parent)) return false;
  if (ts.isBindingElement(parent) && parent.propertyName === node) return false;
  if (ts.isQualifiedName(parent) && parent.right === node) return false;
  if ((ts.isLabeledStatement(parent) || ts.isBreakStatement(parent) || ts.isContinueStatement(parent))
    && parent.label === node) return false;
  const jsxTag = (ts.isJsxOpeningElement(parent) || ts.isJsxClosingElement(parent)
    || ts.isJsxSelfClosingElement(parent)) && parent.tagName === node;
  return !jsxTag || !/^[a-z]/.test(node.text);
}

function references(text, source, target, targetName, targetDefault = false) {
  const { file } = indexText(text, source, false);
  const wanted = targetDefault ? ["default", ...targetName.split(".").slice(1)].join(".") : targetName;
  const result = [];
  const add = (node, scope, kind) => result.push({
    line: file.getLineAndCharacterOfPosition(node.getStart(file)).line + 1,
    scope: scope.join(".") || "<module>", kind,
  });
  function bindingNames(node, names) {
    if (ts.isIdentifier(node)) names.add(node.text);
    else if (ts.isObjectBindingPattern(node) || ts.isArrayBindingPattern(node)) {
      for (const e of node.elements) if (ts.isBindingElement(e)) bindingNames(e.name, names);
    }
  }
  function isScope(node) {
    return ts.isFunctionLike(node) || ts.isClassDeclaration(node) || ts.isBlock(node);
  }
  function scan(scope, parents, inherited) {
    const all = [];
    function gather(node) {
      ts.forEachChild(node, child => {
        all.push(child);
        if (!isScope(child)) gather(child);
      });
    }
    gather(scope);
    const blocked = new Set();
    for (const node of all) {
      if (ts.isVariableDeclaration(node) || ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node)) {
        if (node.name) bindingNames(node.name, blocked);
      }
    }
    for (const parameter of scope.parameters || []) bindingNames(parameter.name, blocked);
    for (const parameter of scope.typeParameters || []) bindingNames(parameter.name, blocked);
    const bindings = new Map([...inherited].filter(([name]) => !blocked.has(name)));
    for (const node of all) {
      if (source === target && (ts.isFunctionDeclaration(node) || ts.isClassDeclaration(node)) && node.name) {
        bindings.set(node.name.text, [...parents, node.name.text].join("."));
      }
      if (!ts.isImportDeclaration(node) || !moduleMatches(node.moduleSpecifier.text, source, target)) continue;
      const clause = node.importClause;
      if (!clause) continue;
      const imported = [];
      if (clause.name) imported.push([clause.name.text, "default"]);
      if (clause.namedBindings) {
        if (ts.isNamespaceImport(clause.namedBindings)) imported.push([clause.namedBindings.name.text, ""]);
        else for (const e of clause.namedBindings.elements) imported.push([e.name.text, e.propertyName?.text || e.name.text]);
      }
      for (const [name, resolved] of imported) {
        if (!blocked.has(name)) bindings.set(name, resolved);
        if (!resolved || resolved === wanted.split(".")[0]) add(node, parents, "import");
      }
    }
    for (const node of all) {
      if (isScope(node)) continue;
      let cursor = node;
      const parts = [];
      while (ts.isPropertyAccessExpression(cursor)) {
        parts.unshift(cursor.name.text);
        cursor = cursor.expression;
      }
      if (!ts.isIdentifier(cursor) || !bindings.has(cursor.text)) continue;
      if (ts.isIdentifier(node) && !isReferenceIdentifier(node)) continue;
      const resolved = [bindings.get(cursor.text), ...parts].filter(Boolean).join(".");
      if (resolved === wanted) add(node, parents, "reference");
    }
    for (const child of all.filter(isScope)) {
      let name = child.name?.getText(file);
      if (!name && (ts.isArrowFunction(child) || ts.isFunctionExpression(child)) && child.parent.name) {
        name = child.parent.name.getText(file);
      }
      scan(child, name ? [...parents, name] : parents, bindings);
    }
  }
  scan(file, [], new Map());
  return [...new Map(result.map(r => [`${r.line}:${r.scope}:${r.kind}`, r])).values()];
}

function main(request) {
  if (request.action === "index") {
    const result = indexText(request.text, request.path, request.details !== false);
    return { symbols: result.symbols, aliases: result.aliases };
  }
  const target = path.resolve(request.target);
  const targetIndex = target.endsWith(".ts") || target.endsWith(".tsx")
    ? indexText(fs.readFileSync(target, "utf8"), target) : null;
  const selected = targetIndex?.symbols.find(s => s.name === request.name);
  const targetNode = selected && targetIndex.nodes.get(selected);
  const defaultExport = Boolean(targetNode?.modifiers?.some(m => m.kind === ts.SyntaxKind.DefaultKeyword));
  return Object.fromEntries(request.files.map(source => [source,
    references(fs.readFileSync(source, "utf8"), source, target, request.name, defaultExport)]));
}

module.exports = { indexText, references, main };
if (require.main === module) {
  try {
    process.stdout.write(JSON.stringify(main(JSON.parse(fs.readFileSync(0, "utf8")))));
  } catch (error) {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 2;
  }
}
