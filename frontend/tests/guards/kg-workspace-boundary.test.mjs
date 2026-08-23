// KG workspace ownership boundary.
//
// The owner is no longer one file: `use-kg-workspace.ts` is a thin composition
// layer over three independently testable domain owners
// (`use-kg-knowledge.ts` / `use-kg-schema.ts` / `use-kg-graph.ts`) sharing one
// actor + notebook + generation gate (`use-kg-owner.ts`). The contract this
// guard pins is unchanged by that split — it just has to hold per file now:
//
//  - page.tsx composes exactly one KG owner and owns no Knowledge/graph HTTP;
//  - every KG file has a positive import allowlist, and the three domains
//    cannot see each other or the composition (cross-domain coordination is
//    only ever the composition layer routing the authority's fan-out into a
//    domain's own **named command**);
//  - no file hands back a raw setter, and the domains' internal fan-out
//    commands never leak out to page.tsx;
//  - opening a notebook reads no Knowledge / schema / unified-graph content —
//    only the maintenance-state recovery probe the graph domain owns;
//  - late visible commits are refused unless the live actor, the live
//    notebook and the exact owner generation all still match;
//  - review recovery and every provider-side write stay policy gated.
//
// Judgement is semantic (AST) — no line numbers (this repo's
// static-source-policy guard forbids AST position queries in test code).
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { callsIn, findFunction, findFunctionIn, importsIn, parseModule } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const composition = await parseModule("use-kg-workspace.ts");
const ownerGate = await parseModule("use-kg-owner.ts");
const knowledge = await parseModule("use-kg-knowledge.ts");
const schema = await parseModule("use-kg-schema.ts");
const graph = await parseModule("use-kg-graph.ts");

// (module, exported hook function name) for every file the split produced.
// Anything that has to hold "for the KG owner" is asserted across this whole
// roster, so a rule can't be satisfied by one file while another drops it.
const KG_MODULES = [
  { file: "use-kg-workspace.ts", module: composition, hook: "useKgWorkspace" },
  { file: "use-kg-owner.ts", module: ownerGate, hook: "useKgOwner" },
  { file: "use-kg-knowledge.ts", module: knowledge, hook: "useKgKnowledge" },
  { file: "use-kg-schema.ts", module: schema, hook: "useKgSchema" },
  { file: "use-kg-graph.ts", module: graph, hook: "useKgGraph" },
];

function callCount(module, name) {
  let count = 0;
  function visit(child) {
    if (ts.isCallExpression(child) && ts.isIdentifier(child.expression)
      && child.expression.text === name) count += 1;
    ts.forEachChild(child, visit);
  }
  visit(module);
  return count;
}

function returnedPropertyNames(module, functionName) {
  const names = [];
  function visit(child) {
    if (ts.isFunctionDeclaration(child) && child.name?.text === functionName) {
      function findReturns(inner) {
        if (ts.isReturnStatement(inner) && inner.expression
          && ts.isObjectLiteralExpression(inner.expression)) {
          for (const property of inner.expression.properties) {
            if (ts.isShorthandPropertyAssignment(property)
              || ts.isPropertyAssignment(property)
              || ts.isMethodDeclaration(property)) names.push(property.name.getText(module));
          }
        }
        ts.forEachChild(inner, findReturns);
      }
      findReturns(child);
      return;
    }
    ts.forEachChild(child, visit);
  }
  visit(module);
  return names;
}

// The property names of the hook's own top-level `return {...}` (not the
// nested returns `returnedPropertyNames` also sweeps up). That is the surface
// a consumer actually sees, which is what the "internals stay internal"
// assertions below need.
function publicSurface(module, functionName) {
  const hookNode = findFunction(module, functionName);
  const names = [];
  function visit(child) {
    if (ts.isFunctionDeclaration(child) || ts.isFunctionExpression(child)
      || ts.isArrowFunction(child) || ts.isMethodDeclaration(child)) return;
    if (ts.isReturnStatement(child) && child.expression
      && ts.isObjectLiteralExpression(child.expression)) {
      for (const property of child.expression.properties) {
        if (ts.isShorthandPropertyAssignment(property)
          || ts.isPropertyAssignment(property)
          || ts.isMethodDeclaration(property)) names.push(property.name.getText(module));
      }
    }
    ts.forEachChild(child, visit);
  }
  ts.forEachChild(hookNode.body, visit);
  return names;
}

// The `fanOut: { ... }` object literal passed to `useKgOwner(...)` inside
// `useKgWorkspace` — the composition layer's one legitimate spot for calling
// a domain's named command. Used both to check every `KgDomains` key is
// routed (below) and to bound where a domain call is allowed to appear at
// all (the "confined to the fan-out routing spots" test).
function findFanOutObject(module, functionName) {
  const hookNode = findFunction(module, functionName);
  let match;
  function visit(node) {
    if (match) return;
    if (
      ts.isPropertyAssignment(node)
      && node.name.getText(module) === "fanOut"
      && ts.isObjectLiteralExpression(node.initializer)
    ) {
      match = node.initializer;
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(hookNode.body);
  if (!match) {
    throw new Error(`fanOut object literal not found in ${functionName} — parsing broke`);
  }
  return match;
}

// The property names declared on the `KgDomains` type literal — the roster
// of domains the fan-out must sweep. Read from the type, not hand-copied,
// so a fourth domain key is picked up automatically.
function kgDomainKeys(module) {
  let keys;
  function visit(node) {
    if (keys) return;
    if (
      ts.isTypeAliasDeclaration(node)
      && node.name.text === "KgDomains"
      && ts.isTypeLiteralNode(node.type)
    ) {
      keys = node.type.members
        .filter((member) => ts.isPropertySignature(member) && member.name)
        .map((member) => member.name.getText(module));
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(module);
  if (!keys) {
    throw new Error("KgDomains type literal not found — parsing broke");
  }
  return keys;
}

// Recognizes `for (const <var> of Object.values(domains)) { ... }` inside a
// fan-out property's body — the loop must walk `domains` itself (not a
// hand-picked subset dressed up as one) for the sweep guarantee to hold.
function domainsValuesLoop(node, module) {
  let loop;
  function visit(child) {
    if (loop) return;
    if (
      ts.isForOfStatement(child)
      && child.expression.getText(module).replace(/\s+/g, "") === "Object.values(domains)"
    ) {
      loop = child;
      return;
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return loop;
}

test("page composes one KG workspace owner and no longer owns Knowledge or graph HTTP", () => {
  assert.equal(callCount(page, "useKgWorkspace"), 1);
  assert.equal(importsIn(page).some(({ module }) => module === "./knowledge-api"), false);
  for (const name of [
    "listKnowledge",
    "listKnowledgeTypes",
    "fetchUnifiedGraph",
    "fetchPendingMerges",
    "fetchNodeContext",
    "fetchKgSearch",
    "fetchConceptDetail",
    "fetchKgNeighbors",
    "fetchMergeReviewJob",
    "reviewMerges",
    "confirmMerge",
    "rejectMerge",
    "rebuildUnifiedKg",
    "relinkKg",
    "buildKg",
    "rebuildKg",
  ]) assert.equal(callCount(page, name), 0, name);
});

test("every KG file has a positive dependency allowlist and no cross-workspace owner", () => {
  const allowed = new Map([
    ["use-kg-workspace.ts", new Set([
      "react",
      "./kg-workspace-model",
      "./use-kg-graph",
      "./use-kg-knowledge",
      "./use-kg-owner",
      "./use-kg-schema",
      "./workspace-model",
    ])],
    // The shared gate owns identity only: no API module, no domain hook.
    ["use-kg-owner.ts", new Set([
      "react",
      "./kg-workspace-model",
      "./workspace-model",
    ])],
    ["use-kg-knowledge.ts", new Set([
      "react",
      "./kg-workspace-model",
      "./knowledge-api",
      "../features/kg-maintenance/kg-api",
      "./use-kg-owner",
      "./workspace-model",
    ])],
    ["use-kg-schema.ts", new Set([
      "react",
      "./kg-workspace-model",
      "./knowledge-api",
      "./schema-manager",
      "./use-kg-owner",
      "./workspace-model",
    ])],
    ["use-kg-graph.ts", new Set([
      "react",
      "./errors",
      "./kg-focus",
      "./kg-workspace-model",
      "./kg-merge-model",
      "./in-progress-resume",
      "../features/kg-maintenance/kg-api",
      "../features/kg-maintenance/kg-rebuild-status",
      "../features/kg-maintenance/kg-relink-status",
      "./kg-build-status",
      "./use-kg-owner",
      "./workspace-model",
    ])],
  ]);

  for (const { file, module } of KG_MODULES) {
    const imports = importsIn(module).map((item) => item.module);
    assert.ok(imports.length > 0, `${file}: 一个 import 都没扫到 —— 解析失效`);
    assert.deepEqual(
      imports.filter((specifier) => !allowed.get(file).has(specifier)),
      [],
      file,
    );
    for (const forbidden of [
      "source-api",
      "ask-api",
      "report-api",
      "memory",
      "notebook-api",
      "page",
      "use-source-library",
      "use-ask-session",
      "use-report-workspace",
    ]) {
      assert.equal(
        imports.some((specifier) => specifier.includes(forbidden)),
        false,
        `${file} → ${forbidden}`,
      );
    }
  }
});

test("the three KG domains cannot see each other or the composition layer", () => {
  for (const file of ["use-kg-knowledge.ts", "use-kg-schema.ts", "use-kg-graph.ts"]) {
    const entry = KG_MODULES.find((candidate) => candidate.file === file);
    const imports = importsIn(entry.module).map((item) => item.module);
    for (const sibling of ["use-kg-knowledge", "use-kg-schema", "use-kg-graph", "use-kg-workspace"]) {
      assert.equal(
        imports.some((specifier) => specifier.includes(sibling)),
        false,
        `${file} 直接 import 了 ${sibling} —— 跨域协调只能走组合层的具名 command`,
      );
    }
  }
});

test("every KG hook exposes readonly views and named commands, never raw setters", () => {
  for (const { file, module, hook } of KG_MODULES) {
    const names = returnedPropertyNames(module, hook);
    assert.ok(names.length > 0, `${file}: 一个 return 字段都没扫到 —— 解析失效`);
    assert.equal(names.some((name) => name.startsWith("set")), false, `${file}: ${names.join(",")}`);
  }

  const surface = publicSurface(composition, "useKgWorkspace");
  for (const name of [
    "knowledge",
    "schema",
    "graph",
    "enterKnowledge",
    "openSchemas",
    "openGraph",
    "beginNotebookTransition",
    "finishNotebookTransition",
    "leaveWorkspace",
  ]) assert.ok(surface.includes(name), name);

  // The domains' fan-out commands are internal to the composition layer:
  // page.tsx sees three namespaced views plus flat commands, nothing else.
  for (const internal of ["view", "clearVisibleState", "invalidate", "adoptOwner"]) {
    assert.equal(
      surface.includes(internal),
      false,
      `组合层把领域内部命令 ${internal} 泄露给了 page.tsx`,
    );
  }
});

test("every KgDomains key is routed through clearVisibleState and invalidate", () => {
  const domainKeys = kgDomainKeys(composition);
  assert.ok(domainKeys.length >= 3, `KgDomains 键数异常: ${domainKeys.join(",")}`);

  const fanOut = findFanOutObject(composition, "useKgWorkspace");
  const clearProperty = fanOut.properties.find(
    (property) => property.name?.getText(composition) === "clearVisibleState",
  );
  const invalidateProperty = fanOut.properties.find(
    (property) => property.name?.getText(composition) === "invalidate",
  );
  assert.ok(clearProperty && invalidateProperty, "fanOut 缺少 clearVisibleState/invalidate 属性");

  // Judgement is structural: if the fan-out is a loop over
  // `Object.values(domains)`, the loop body must invoke `.<command>()` on
  // the loop variable (walking `domains` itself, not a hand-picked subset —
  // `domainsValuesLoop` only matches the exact iterable). If a future
  // refactor reverts to an explicit per-key list, every `KgDomains` key must
  // have its own routed call instead.
  function assertRoutesAllDomains(property, command) {
    const initializer = property.initializer;
    const loop = domainsValuesLoop(initializer, composition);
    if (loop) {
      const declarations = ts.isVariableDeclarationList(loop.initializer)
        ? loop.initializer.declarations
        : [];
      assert.equal(declarations.length, 1, `${command}: for-of 循环变量解析失败`);
      const loopVar = declarations[0].name.getText(composition);
      assert.ok(
        callsIn(loop.statement).includes(`${loopVar}.${command}`),
        `${command}: Object.values(domains) 循环体没有调用 .${command}()`,
      );
      return;
    }
    const initializerText = initializer.getText(composition);
    for (const key of domainKeys) {
      assert.match(
        initializerText,
        new RegExp(`domains\\.${key}\\.${command}\\(`),
        `${command} 没有路由 KgDomains 的 "${key}" 键（既不是 Object.values(domains) 循环，也没有显式 domains.${key}.${command}()）`,
      );
    }
  }

  assertRoutesAllDomains(clearProperty, "clearVisibleState");
  assertRoutesAllDomains(invalidateProperty, "invalidate");
});

test("domain command calls are confined to the fan-out routing spots", () => {
  const compose = findFunction(composition, "useKgWorkspace");
  const fanOut = findFanOutObject(composition, "useKgWorkspace");

  // The allowed regions are the fan-out object literal's own property
  // initializers (arrow functions) — this covers both an explicit per-key
  // list and a loop over `Object.values(domains)`, since either form is
  // written inside one of these initializers. Containment is judged
  // structurally: a single descent from the composition function body
  // carries an `allowed` flag that flips true only on entering one of these
  // exact node objects (identity, not a source-position/order query — this
  // repo's static-source-policy guard forbids `.pos`/`.end`/`getStart`/
  // `getEnd` in test code).
  const allowedRoots = new Set(fanOut.properties.map((property) => property.initializer));

  // A "domain command call" is any call whose callee text is rooted at an
  // identifier starting with "domain" and reaches a member through at least
  // one dot — this catches the explicit form (`domains.knowledge.invalidate(`),
  // the ref form (`domainsRef.current?.graph.adoptOwner(`), and the loop
  // form (`domain.clearVisibleState(`) regardless of which one production
  // code currently uses.
  const offenders = [];
  function visit(node, allowed) {
    const nowAllowed = allowed || allowedRoots.has(node);
    if (ts.isCallExpression(node)) {
      const calleeText = node.expression.getText(composition);
      if (/^domain/i.test(calleeText) && calleeText.includes(".") && !nowAllowed) {
        offenders.push(calleeText);
      }
    }
    ts.forEachChild(node, (child) => visit(child, nowAllowed));
  }
  visit(compose.body, false);

  assert.deepEqual(
    offenders,
    [],
    `组合层在扇出路由位置之外调用了领域命令: ${offenders.join(", ")}`,
  );
});

test("the actor/notebook/generation gate is declared once, in the shared owner", () => {
  const gate = findFunction(ownerGate, "useKgOwner").getText(ownerGate);
  assert.match(gate, /const ownerRef = useRef<KgWorkspaceOwner \| null>\(null\)/);
  assert.match(gate, /pendingNotebookSnapshotRef/);
  for (const { file, module, hook } of KG_MODULES) {
    if (file === "use-kg-owner.ts") continue;
    const text = findFunction(module, hook).getText(module);
    for (const duplicated of ["ownerRef", "actorIdRef", "notebookIdRef", "generationRef"]) {
      assert.equal(
        text.includes(`const ${duplicated} =`),
        false,
        `${file} 自己又声明了一份 ${duplicated} —— 门必须只有一个定义点`,
      );
    }
  }
});

test("Knowledge, schemas, and unified graph remain explicit lazy commands", () => {
  assert.match(findFunction(knowledge, "enterKnowledge").getText(knowledge), /^enterKnowledge = async \(\) =>/);
  assert.match(findFunction(schema, "openSchemas").getText(schema), /^openSchemas = \(\) =>/);
  assert.match(findFunction(graph, "openGraph").getText(graph), /^openGraph = async \(/);
  // Opening a notebook runs the graph domain's `adoptOwner` recovery probe and
  // nothing else; the shared gate cannot even reach a content reader (its
  // import allowlist above has no API module).
  const adopt = findFunction(graph, "adoptOwner").getText(graph);
  for (const forbidden of ["listKnowledge(", "listKnowledgeTypes(", "listNotebookObjectSchemas(", "fetchUnifiedGraph("]) {
    assert.equal(adopt.includes(forbidden), false, forbidden);
  }
  for (const forbidden of ["listKnowledge", "listKnowledgeTypes", "listNotebookObjectSchemas", "fetchUnifiedGraph"]) {
    assert.equal(callCount(ownerGate, forbidden), 0, forbidden);
  }
});

test("workspace transition and both authentication paths bind KG authority", () => {
  const text = page.getText(page);
  assert.match(text, /kgWorkspace\.beginNotebookTransition\(\)/);
  assert.match(text, /kgWorkspace\.finishNotebookTransition\(kgTransition, opened \? openedNotebook : null\)/);
  assert.match(text, /kgWorkspace\.leaveWorkspace\(\)/);
  // kgWorkspace.activateActor 现在只经共享函数 activateWorkspaceOwners 间接调用；
  // 守卫 workspace-owner-transition-guard 钉住「只能从那里发出」。原断言钉「两个
  // 认证站点各出现一次 kgWorkspace.activateActor(u.id) 且紧邻 setCurrentUser」，
  // 现拆成两半：共享函数体内恰好调用一次，加上两个认证站点各自
  // activateWorkspaceOwners(u.id) 紧邻 setCurrentUser(u)。
  assert.ok(
    callsIn(findFunctionIn(page, "Home", "activateWorkspaceOwners")).includes("kgWorkspace.activateActor"),
    "activateWorkspaceOwners must activate the KG workspace owner",
  );
  assert.equal(
    (text.match(/activateWorkspaceOwners\(u\.id\);[\s\S]{0,220}setCurrentUser\(u\)/g) ?? []).length,
    2,
  );
  assert.match(
    text,
    /listBases\(notebookId\)[\s\S]{0,300}workspaceActorIdRef\.current === actorId[\s\S]{0,180}activeNotebookIdRef\.current === notebookId[\s\S]{0,180}workspaceEpochRef\.current === workspaceEpoch/,
  );
});

test("visible commits compare live actor and notebook before exact owner generation", () => {
  const gate = ownerGate.getText(ownerGate);
  assert.match(
    gate,
    /actorIdRef\.current === owner\.actorId[\s\S]{0,180}notebookIdRef\.current === owner\.notebookId[\s\S]{0,180}sameKgOwner\(ownerRef\.current, owner\)/,
  );
  assert.match(gate, /pendingNotebookSnapshotRef/);
  // The gate carries the frozen notebook snapshot; the graph domain is what
  // interprets it, and only for the notebook the new owner actually names.
  assert.match(
    findFunction(graph, "adoptOwner").getText(graph),
    /notebookSnapshot\?\.id === owner\.notebookId/,
  );
  const openGraph = findFunction(graph, "openGraph").getText(graph);
  assert.equal(openGraph.includes("ownerRef.current ="), false);
});

test("read-only review recovery and every provider-side write are policy gated", () => {
  const text = graph.getText(graph);
  assert.match(text, /if \(policyRef\.current\.canWriteKg\) \{[\s\S]{0,180}fetchMergeReviewJob/);
  for (const name of ["reviewPendingMerges", "reviewAllMerges", "decideMerge", "startRelink", "launchRebuild", "startKgBuild"]) {
    const at = text.indexOf(`const ${name}`);
    assert.ok(at >= 0, name);
    assert.match(text.slice(at, at + 900), /policyRef\.current\.canWriteKg/);
  }
  const knowledgeText = knowledge.getText(knowledge);
  for (const name of ["updateKnowledge", "findDuplicates", "mergeKnowledge"]) {
    const at = knowledgeText.indexOf(`const ${name} =`);
    assert.ok(at >= 0, name);
    assert.match(knowledgeText.slice(at, at + 900), /policyRef\.current\.canGovernKnowledge/);
  }
  const schemaText = schema.getText(schema);
  for (const name of ["selectSchemaView", "mutateSchema", "induceSchemas"]) {
    const at = schemaText.indexOf(`const ${name} =`);
    assert.ok(at >= 0, name);
    assert.match(
      schemaText.slice(at, at + 900),
      /policyRef\.current\.canManage(Notebook|Global)Schemas|canWriteSchema\(view\)/,
      name,
    );
  }
  const pageText = page.getText(page);
  assert.match(pageText, /!readOnlyWorkspace && \([\s\S]{0,400}onClick=\{reviewPendingMerges\}/);
  assert.match(pageText, /!readOnlyWorkspace && <span className="kg-merge-actions">/);
});
