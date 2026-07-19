import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";


const printer = ts.createPrinter({
  newLine: ts.NewLineKind.LineFeed,
  removeComments: true,
});


function semanticText(node) {
  return printer
    .printNode(ts.EmitHint.Unspecified, node, node.getSourceFile())
    .replace(/\s+/g, " ")
    .trim();
}


export function parseText(source, fileName) {
  return ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    fileName.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
  );
}


export async function parseModule(relativePath) {
  const url = new URL(`../${relativePath}`, import.meta.url);
  return parseText(await readFile(url, "utf8"), relativePath);
}


export async function appSourceModules() {
  const appDirectory = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
  const files = [];

  async function visit(directory) {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        await visit(absolute);
        continue;
      }
      if (
        !/\.(?:ts|tsx)$/.test(entry.name)
        || entry.name.endsWith(".d.ts")
        || entry.name.includes(".test.")
      ) {
        continue;
      }
      const relative = path.relative(appDirectory, absolute).replaceAll(
        path.sep,
        "/",
      );
      files.push({
        path: relative,
        module: parseText(await readFile(absolute, "utf8"), relative),
      });
    }
  }

  await visit(appDirectory);
  return files.sort((left, right) => left.path.localeCompare(right.path));
}


function declaredName(node, sourceFile) {
  if (
    ts.isFunctionDeclaration(node)
    || ts.isMethodDeclaration(node)
    || ts.isClassDeclaration(node)
  ) {
    return node.name?.getText(sourceFile);
  }
  if (
    ts.isVariableDeclaration(node)
    && node.initializer
    && (
      ts.isArrowFunction(node.initializer)
      || ts.isFunctionExpression(node.initializer)
    )
  ) {
    return node.name.getText(sourceFile);
  }
  return undefined;
}


export function findFunction(sourceFile, name) {
  let match;
  function visit(node) {
    if (
      (
        ts.isFunctionDeclaration(node)
        || ts.isMethodDeclaration(node)
        || (
          ts.isVariableDeclaration(node)
          && node.initializer
          && (
            ts.isArrowFunction(node.initializer)
            || ts.isFunctionExpression(node.initializer)
          )
        )
      )
      && node.name?.getText(sourceFile) === name
    ) {
      match = node;
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  if (!match) {
    throw new Error(`function not found: ${name}`);
  }
  return match;
}


export function findFunctionIn(sourceFile, parentName, name) {
  const scopes = [];
  let match;
  function visit(node) {
    const nodeName = declaredName(node, sourceFile);
    if (!match && nodeName === name && scopes.includes(parentName)) {
      match = node;
    }
    if (nodeName) {
      scopes.push(nodeName);
    }
    ts.forEachChild(node, visit);
    if (nodeName) {
      scopes.pop();
    }
  }
  visit(sourceFile);
  if (!match) {
    throw new Error(`function not found: ${parentName}.${name}`);
  }
  return match;
}


export function callsIn(node) {
  const calls = [];
  function visit(child) {
    if (ts.isCallExpression(child)) {
      calls.push(child.expression.getText(child.getSourceFile()));
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return calls.sort();
}


export function callSitesIn(node) {
  const calls = [];
  function visit(child) {
    if (ts.isCallExpression(child)) {
      calls.push({
        target: semanticText(child.expression),
        arguments: child.arguments.map(semanticText),
      });
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return calls.sort((left, right) => (
    left.target.localeCompare(right.target)
    || JSON.stringify(left.arguments).localeCompare(JSON.stringify(right.arguments))
  ));
}


export function assignmentsIn(node) {
  const assignments = [];
  function visit(child) {
    if (
      ts.isBinaryExpression(child)
      && child.operatorToken.kind >= ts.SyntaxKind.FirstAssignment
      && child.operatorToken.kind <= ts.SyntaxKind.LastAssignment
    ) {
      assignments.push({
        target: semanticText(child.left),
        operator: child.operatorToken.getText(child.getSourceFile()),
        value: semanticText(child.right),
      });
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return assignments.sort((left, right) => (
    left.target.localeCompare(right.target)
    || left.operator.localeCompare(right.operator)
    || left.value.localeCompare(right.value)
  ));
}


export function ifConditionsIn(node) {
  const conditions = [];
  function visit(child) {
    if (ts.isIfStatement(child)) {
      conditions.push(semanticText(child.expression));
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return conditions.sort();
}


function containsReturn(node) {
  let found = false;
  function visit(child) {
    if (found) {
      return;
    }
    if (ts.isReturnStatement(child)) {
      found = true;
      return;
    }
    if (child !== node && ts.isFunctionLike(child)) {
      return;
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return found;
}


export function ifBranchesIn(node) {
  const branches = [];
  function visit(child) {
    if (ts.isIfStatement(child)) {
      branches.push({
        condition: semanticText(child.expression),
        thenCalls: callsIn(child.thenStatement),
        elseCalls: child.elseStatement ? callsIn(child.elseStatement) : [],
        thenReturns: containsReturn(child.thenStatement),
        elseReturns: child.elseStatement
          ? containsReturn(child.elseStatement)
          : false,
      });
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return branches.sort((left, right) => (
    left.condition.localeCompare(right.condition)
    || JSON.stringify(left).localeCompare(JSON.stringify(right))
  ));
}


export function variableInitializersIn(node) {
  const initializers = [];
  function visit(child) {
    if (ts.isVariableDeclaration(child) && child.initializer) {
      initializers.push({
        name: semanticText(child.name),
        initializer: semanticText(child.initializer),
      });
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return initializers.sort((left, right) => (
    left.name.localeCompare(right.name)
    || left.initializer.localeCompare(right.initializer)
  ));
}


export function scopedCalls(sourceFile) {
  const scopes = ["<module>"];
  const counts = new Map();

  function visit(node) {
    const name = declaredName(node, sourceFile);
    if (name) {
      scopes.push(name);
    }
    if (ts.isCallExpression(node)) {
      const key = JSON.stringify({
        scope: scopes.join("."),
        target: node.expression.getText(sourceFile),
      });
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    ts.forEachChild(node, visit);
    if (name) {
      scopes.pop();
    }
  }
  visit(sourceFile);

  return [...counts.entries()]
    .map(([key, count]) => ({ ...JSON.parse(key), count }))
    .sort((left, right) => (
      left.scope.localeCompare(right.scope)
      || left.target.localeCompare(right.target)
    ));
}


export function importsFrom(sourceFile, modulePath) {
  const imports = [];
  for (const statement of sourceFile.statements) {
    if (
      !ts.isImportDeclaration(statement)
      || !ts.isStringLiteral(statement.moduleSpecifier)
      || statement.moduleSpecifier.text !== modulePath
    ) {
      continue;
    }
    const clause = statement.importClause;
    if (!clause) {
      continue;
    }
    if (clause.name) {
      imports.push({ imported: "default", local: clause.name.text });
    }
    if (clause.namedBindings && ts.isNamespaceImport(clause.namedBindings)) {
      imports.push({
        imported: "*",
        local: clause.namedBindings.name.text,
      });
    } else if (
      clause.namedBindings
      && ts.isNamedImports(clause.namedBindings)
    ) {
      for (const element of clause.namedBindings.elements) {
        imports.push({
          imported: element.propertyName?.text ?? element.name.text,
          local: element.name.text,
        });
      }
    }
  }
  return imports.sort((left, right) => (
    left.imported.localeCompare(right.imported)
    || left.local.localeCompare(right.local)
  ));
}


export function importsIn(sourceFile) {
  const findings = [];
  for (const statement of sourceFile.statements) {
    if (
      !ts.isImportDeclaration(statement)
      || !ts.isStringLiteral(statement.moduleSpecifier)
    ) {
      continue;
    }
    for (const item of importsFrom(sourceFile, statement.moduleSpecifier.text)) {
      findings.push({
        module: statement.moduleSpecifier.text,
        ...item,
      });
    }
  }
  return findings.sort((left, right) => (
    left.module.localeCompare(right.module)
    || left.imported.localeCompare(right.imported)
    || left.local.localeCompare(right.local)
  ));
}


function staticJsxAttributes(attributes, sourceFile) {
  const result = {};
  for (const attribute of attributes.properties) {
    if (!ts.isJsxAttribute(attribute)) {
      continue;
    }
    const name = attribute.name.getText(sourceFile);
    const initializer = attribute.initializer;
    if (!initializer) {
      result[name] = true;
    } else if (ts.isStringLiteral(initializer)) {
      result[name] = initializer.text;
    } else if (
      ts.isJsxExpression(initializer)
      && initializer.expression
      && (
        ts.isStringLiteral(initializer.expression)
        || ts.isNumericLiteral(initializer.expression)
      )
    ) {
      result[name] = initializer.expression.text;
    }
  }
  return result;
}


function dynamicJsxBindings(attributes, sourceFile) {
  const result = {};
  for (const attribute of attributes.properties) {
    if (!ts.isJsxAttribute(attribute)) {
      continue;
    }
    const initializer = attribute.initializer;
    if (
      !initializer
      || ts.isStringLiteral(initializer)
      || !ts.isJsxExpression(initializer)
      || !initializer.expression
      || ts.isStringLiteral(initializer.expression)
      || ts.isNumericLiteral(initializer.expression)
    ) {
      continue;
    }
    result[attribute.name.getText(sourceFile)] = semanticText(
      initializer.expression,
    );
  }
  return result;
}


export function jsxElements(sourceFile, elementName) {
  const scopes = ["<module>"];
  const elements = [];

  function visit(node) {
    const name = declaredName(node, sourceFile);
    if (name) {
      scopes.push(name);
    }
    if (
      (
        ts.isJsxOpeningElement(node)
        || ts.isJsxSelfClosingElement(node)
      )
      && node.tagName.getText(sourceFile) === elementName
    ) {
      const element = {
        scope: scopes.join("."),
        attributes: staticJsxAttributes(node.attributes, sourceFile),
      };
      const bindings = dynamicJsxBindings(node.attributes, sourceFile);
      if (Object.keys(bindings).length > 0) {
        element.bindings = bindings;
      }
      elements.push(element);
    }
    ts.forEachChild(node, visit);
    if (name) {
      scopes.pop();
    }
  }
  visit(sourceFile);

  return elements;
}


export function stringLiterals(sourceFile) {
  const values = [];
  function visit(node) {
    if (
      ts.isStringLiteral(node)
      || ts.isNoSubstitutionTemplateLiteral(node)
      || ts.isTemplateHead(node)
      || ts.isTemplateMiddle(node)
      || ts.isTemplateTail(node)
    ) {
      values.push(node.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return values.sort();
}


export function propertyAccesses(sourceFile) {
  const values = [];
  function visit(node) {
    if (ts.isPropertyAccessExpression(node)) {
      values.push(node.getText(sourceFile));
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return values.sort();
}


export function declarations(sourceFile) {
  const scopes = ["<module>"];
  const findings = [];

  function visit(node) {
    let kind;
    let name;
    if (ts.isFunctionDeclaration(node)) {
      kind = "function";
      name = node.name?.text;
    } else if (ts.isClassDeclaration(node)) {
      kind = "class";
      name = node.name?.text;
    } else if (ts.isMethodDeclaration(node)) {
      kind = "method";
      name = node.name.getText(sourceFile);
    } else if (ts.isTypeAliasDeclaration(node)) {
      kind = "type";
      name = node.name.text;
    } else if (ts.isInterfaceDeclaration(node)) {
      kind = "interface";
      name = node.name.text;
    } else if (ts.isVariableDeclaration(node)) {
      kind = "variable";
      name = node.name.getText(sourceFile);
    }
    if (kind && name) {
      findings.push({
        scope: scopes.join("."),
        kind,
        name,
        exported: node.modifiers?.some(
          (modifier) => modifier.kind === ts.SyntaxKind.ExportKeyword,
        ) ?? false,
      });
    }
    const scopeName = declaredName(node, sourceFile);
    if (scopeName) {
      scopes.push(scopeName);
    }
    ts.forEachChild(node, visit);
    if (scopeName) {
      scopes.pop();
    }
  }

  visit(sourceFile);
  return findings;
}


export function scopedStringLiterals(sourceFile) {
  const scopes = ["<module>"];
  const counts = new Map();

  function visit(node) {
    const scopeName = declaredName(node, sourceFile);
    if (scopeName) {
      scopes.push(scopeName);
    }
    if (
      ts.isStringLiteral(node)
      || ts.isNoSubstitutionTemplateLiteral(node)
    ) {
      const key = JSON.stringify({
        scope: scopes.join("."),
        value: node.text,
      });
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    ts.forEachChild(node, visit);
    if (scopeName) {
      scopes.pop();
    }
  }

  visit(sourceFile);
  return [...counts.entries()]
    .map(([key, count]) => ({ ...JSON.parse(key), count }))
    .sort((left, right) => (
      left.scope.localeCompare(right.scope)
      || left.value.localeCompare(right.value)
    ));
}


export function jsxTextValues(sourceFile) {
  const values = [];
  function visit(node) {
    if (ts.isJsxText(node)) {
      const value = node.getText(sourceFile).replace(/\s+/g, " ").trim();
      if (value) {
        values.push(value);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return values.sort();
}
