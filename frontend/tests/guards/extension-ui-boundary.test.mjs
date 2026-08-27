import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { appSourceModules, findFunction, findFunctionIn, importsIn, jsxElements, parseModule, parseText, scopedCalls } from "../../test-support/semantic-source.mjs";
// 判据不住在任何一个守卫里：插件包的 import 白名单与侧信道判据只能有**一份**拼写，
// 而它的两个消费者（本文件的逐插件白名单检查、包边界守卫的真值表与真包扫描）如果
// 互相 import，node 泳道单跑任一文件都会把另一份的全部用例一起加载并再跑一遍。
// 放进 `test-support/` 反而不对——那里是跨泳道共享的 setup/adapter，这份判据只服务
// 这两个守卫，所以它住在 `tests/guards/` 的一个非测试入口模块里。
import {
  pluginPackageImportOffenders,
  pluginPackageSideChannelOffenders,
  samePluginPackageSpecifier,
} from "./_plugin-import-predicate.mjs";


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
  // The side outlet is an entry row inside the source panel's **non-scrolling**
  // area, not a third workspace column (that column left an empty 190px strip and
  // squeezed the chat panel). Its direct JSX parent must therefore stay the
  // sources body. `.source-list` is the only scrolling container (it owns
  // `flex:1 1 auto/overflow:auto`), so nesting the outlet *inside* it would make the
  // entry scroll away with the source rows; pinning the direct parent to the sources
  // body is the load-bearing half of that invariant (the layout guard only pins the
  // `.sources-panel` subtree and rejects the old `.workspace-grid` direct child).
  let workspaceOutletParent = outletNodes.get("workspace.side_panel")?.parent;
  while (workspaceOutletParent && !ts.isJsxElement(workspaceOutletParent)) {
    workspaceOutletParent = workspaceOutletParent.parent;
  }
  assert.ok(
    workspaceOutletParent
    && workspaceOutletParent.openingElement.tagName.getText(page) === "div",
    "workspace side outlet must remain a direct JSX child of the sources body",
  );
  const workspaceClass = workspaceOutletParent.openingElement.attributes.properties.find((attribute) => (
    ts.isJsxAttribute(attribute) && attribute.name.getText(page) === "className"
  ));
  assert.ok(
    workspaceClass
    && workspaceClass.initializer?.getText(page) === '"workspace-panel-body sources-body"',
    "workspace side outlet direct parent must be the fixed sources body, above the scrolling source list",
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
    && sourceDetailGate.left.getText(page) === 'rootModals.view("source-detail").open && sourceDetail',
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


/**
 * 插件模块按路径约定认（features/<feature>/workspace-plugin.ts），不手抄清单：
 * 加第二个插件不该改这份守卫，但每个插件仍要过同一套 import 白名单。
 */
const PLUGIN_MODULE = /^features\/[a-z0-9-]+\/workspace-plugin\.tsx?$/;
/** 仓库外插件包的落点：`features/ext-<包名>/`（同步脚本的 `GENERATED_PACKAGE_DIR`）。 */
const EXT_PACKAGE_MODULE = /^features\/ext-[a-z0-9-]+\//;
/** 仓库自己的 SDK 模块里不许出现的字样（远端装载器与领域 owner）。 */
const FORBIDDEN_SDK_TEXT = /\b(fetch\s*\(|import\s*\(|setInterval\s*\(|setTimeout\s*\(|WebSocket\b|EventSource\b|page\.tsx|useSourceLibrary|useAskSession|useReportWorkspace|useKgWorkspace|useNotebookCollection)\b/;

/**
 * 文本正则的扫描面：**仓库自己写的**那些模块，不含仓库外插件包（`features/ext-<包名>/`）。
 *
 * 那是一条**文本**正则，注释与字符串一起数。SDK 与内建插件是我们写的，一句
 * 「本模块不得 setTimeout(」的注释可以顺手改掉；插件包不是——它由同步脚本从内网复制
 * 进来，谁也不会为了让基座的正则高兴去改它的注释，而一次注释误报会把整棵装了插件的
 * 树判红，逼下一个人去放宽正则。
 *
 * 覆盖没有变窄，只是换了判据：包里的**每个**模块（入口与兄弟）由
 * `pluginPackageSideChannelOffenders` 的 AST 扫描认真调用，`fetch(` 由
 * `api-boundary.test.mjs` 对全部模块普查。下面那条用例把这两半钉在一起。
 */
function textScanTargets(rows) {
  return rows.filter((row) => !EXT_PACKAGE_MODULE.test(row.path));
}


test("extension SDK remains static, narrow, and free of domain owners or remote loaders", async () => {
  const modules = (await appSourceModules()).filter((row) => (
    row.path.startsWith("features/extension-sdk/")
    || PLUGIN_MODULE.test(row.path)
    || row.path === "use-workspace-extensions.ts"
  ));
  const forbidden = FORBIDDEN_SDK_TEXT;
  const textScanned = textScanTargets(modules);
  assert.ok(textScanned.length > 0, "文本扫描面被过滤空了（守卫失效）");
  for (const { path, module } of textScanned) {
    assert.doesNotMatch(module.getText(module), forbidden, path);
  }
  // registry 现在是三个模块，import 规则各不相同、必须分开钉：
  //  · registry.ts            —— node --test 泳道直接 import 它，闭包必须全是 .ts，
  //                              因此**不许** import ./registry.local.ts（生成物会拉进插件的 .tsx 入口）。
  //  · workspace-registry.ts  —— 唯一的合并点，只许碰这三份 SDK 内部模块。
  //  · registry.local.ts      —— 同步脚本的生成物，只许 import 合同与 features/ext-*/ 下的插件入口。
  const registry = modules.find((row) => row.path === "features/extension-sdk/registry.ts");
  assert.ok(registry);
  // api 端口是插件唯一的 HTTP 出口，所以它自己的依赖面必须窄到可以一眼看完：核心
  // api 客户端（唯一允许发请求的模块）、共享请求任务流消费者、错误人性化（插件
  // 读不得 `error.message`——errors-guard 是精确计数普查，仓库外的包登记不进那份
  // 清单）、以及 SDK 合同。
  // 多出任何一条——尤其是 `../../app/source-api.ts` 这种领域 API——就等于让插件端口
  // 顺带成为某个领域能力的转发器，而它本该只会拼路径。
  const port = modules.find((row) => row.path === "features/extension-sdk/api.ts");
  assert.ok(port, "features/extension-sdk/api.ts must remain the plugin HTTP port");
  assert.deepEqual(
    [...new Set(importsIn(port.module).map((row) => row.module))].sort(),
    [
      "../../app/api-client.ts",
      "../../app/errors.ts",
      "../../app/request-task-stream.ts",
      "./contracts.ts",
    ],
    "api.ts may import only the core api client, task stream, error humanizer and SDK contracts",
  );
  const merged = modules.find((row) => row.path === "features/extension-sdk/workspace-registry.ts");
  assert.ok(merged, "workspace-registry.ts must remain the single merged registry module");
  const local = modules.find((row) => row.path === "features/extension-sdk/registry.local.ts");
  if (!local) assert.fail("registry.local.ts 缺失——先跑 npm run sync:ui-plugins");
  // registry.ts 只能 import 合同与插件组件模块（按路径形状认，不钉死插件清单）；
  // 它的插件 import 数必须等于**内建**登记条目数——漏登记或多 import 都红。这条
  // 一一对应只对手写的内建目录成立，本地那半另有判据（见下面那段注释）。
  const registryImports = importsIn(registry.module).map((row) => row.module);
  const builtinPluginImports = registryImports.filter((module) => /^\.\.\/[a-z0-9-]+\/workspace-plugin\.tsx?$/.test(module));
  // 按**前缀**判而不是等值判 `./registry.local.ts`：`allowImportingTsExtensions`
  // 是仓库惯例而不是打包器的要求，无扩展名的 `./registry.local` 解析到同一个
  // 生成文件，等值判据会整条放过它。
  assert.deepEqual(
    registryImports.filter((module) => module.startsWith("./registry.local")),
    [],
    "registry.ts must not import the generated local registry: the node --test lane imports"
    + " registry.ts directly and Node cannot load the plugin .tsx entries it pulls in",
  );
  assert.deepEqual(
    registryImports.filter((module) => module !== "./contracts.ts" && !builtinPluginImports.includes(module)),
    [],
    "registry.ts may import only ./contracts.ts and features/*/workspace-plugin modules",
  );
  assert.deepEqual(
    [...new Set(importsIn(merged.module).map((row) => row.module))].sort(),
    ["./contracts.ts", "./registry.local.ts", "./registry.ts"],
    "workspace-registry.ts may merge only the builtin registry and the generated local one",
  );
  // 合并结果必须**再过一次** defineWorkspaceUiRegistry：本地条目与内建条目共用同一套
  // 元数据校验、重复 id 拒绝、稳定排序与冻结。直接拼数组（`[...builtin, ...local]`）在
  // 零插件部署上看不出任何差别，却让仓库外插件包的重复 id、非法 slot 或不是函数的
  // Component 静默走到 host，并让合并结果不再冻结。
  let mergedInitializer;
  for (const top of merged.module.statements) {
    if (!ts.isVariableStatement(top)) continue;
    for (const declared of top.declarationList.declarations) {
      if (declared.name.getText(merged.module) !== "WORKSPACE_UI_CONTRIBUTIONS") continue;
      mergedInitializer = declared.initializer;
    }
  }
  assert.ok(
    mergedInitializer
    && ts.isCallExpression(mergedInitializer)
    && mergedInitializer.expression.getText(merged.module) === "defineWorkspaceUiRegistry",
    "workspace-registry.ts must re-validate the merged registry through defineWorkspaceUiRegistry",
  );
  const mergedArgument = mergedInitializer.arguments[0];
  assert.ok(
    mergedArgument && ts.isArrayLiteralExpression(mergedArgument),
    "the merged registry must be validated as one explicit array literal of both sources",
  );
  // 钉的是**形状**：合并点恰好是两条 spread、没有第三个来源、也没有就地拼装的
  // 条目。spread 的先后本身不承重——`defineWorkspaceUiRegistry` 按 id 排序后冻结，
  // 两种顺序产出逐字相同的数组；固定写法只是让这一行读起来与审计起来都只有一种
  // 形态（重复 id 由 defineWorkspaceUiRegistry 直接拒绝，不存在「谁覆盖谁」）。
  assert.deepEqual(
    mergedArgument.elements.map((element) => element.getText(merged.module)),
    ["...BUILTIN_WORKSPACE_UI_CONTRIBUTIONS", "...LOCAL_WORKSPACE_UI_CONTRIBUTIONS"],
    "merged registry must be exactly the builtin registry and the generated local one, spread in that fixed shape",
  );
  const LOCAL_PLUGIN_SPECIFIER = /^\.\.\/ext-[a-z0-9-]+\/workspace-plugin\.tsx?$/;
  const localImportRows = importsIn(local.module);
  const localImports = localImportRows.map((row) => row.module);
  const localPluginImports = localImports.filter((module) => LOCAL_PLUGIN_SPECIFIER.test(module));
  assert.deepEqual(
    localImports.filter((module) => module !== "./contracts.ts" && !localPluginImports.includes(module)),
    [],
    "registry.local.ts may import only ./contracts.ts and features/ext-*/workspace-plugin modules",
  );
  let registeredEntries = 0;
  function countEntries(entry) {
    if (
      ts.isCallExpression(entry)
      && entry.expression.getText(registry.module) === "defineWorkspaceUiRegistry"
      && entry.arguments[0] && ts.isArrayLiteralExpression(entry.arguments[0])
    ) registeredEntries += entry.arguments[0].elements.filter(ts.isObjectLiteralExpression).length;
    ts.forEachChild(entry, countEntries);
  }
  countEntries(registry.module);
  assert.ok(registeredEntries > 0, "registry must register at least one contribution (guard would be vacuous)");
  // 内建目录是手写的，一个 contribution 配一个插件模块仍然是它的形状——这条同时
  // 是本守卫的空转保护（少了它，插件解析函数整个失效时下面每条断言都比较空集合）。
  assert.equal(
    builtinPluginImports.length,
    registeredEntries,
    "registry.ts: every builtin contribution imports exactly one plugin module and vice versa",
  );
  const plugins = modules.filter((row) => PLUGIN_MODULE.test(row.path));
  // **本地条目与插件模块不是一一对应的**，所以这里刻意不数「条目数 == 模块数」也不数
  // 「条目数 == import 绑定数」：§1.5 的 `contributions` 是数组而每个包恰好一个入口
  // 文件（一包两条 → 模块少一个），同步脚本又按组件名去重 import 绑定（两条复用同一个
  // 组件 → 绑定少一个）。两条计数都会把完全合法的包判红。改钉两条与「一文件一条目」
  // 无关的结构性质：
  //  ① registry.local.ts 引用的入口集合 == 磁盘上 features/ext-*/ 的入口集合；
  //  ② 每条本地条目的 Component 都绑定到本文件某条 import 的名字。
  const LOCAL_PLUGIN_PATH = /^features\/ext-[a-z0-9-]+\/workspace-plugin\.tsx?$/;
  const importedEntryPaths = [...new Set(localPluginImports.map(
    (module) => `features/${module.replace("../", "")}`,
  ))].sort();
  const presentEntryPaths = plugins
    .map((row) => row.path)
    .filter((modulePath) => LOCAL_PLUGIN_PATH.test(modulePath))
    .sort();
  const orphanEntryModules = presentEntryPaths.filter((modulePath) => !importedEntryPaths.includes(modulePath));
  const danglingEntryImports = importedEntryPaths.filter((modulePath) => !presentEntryPaths.includes(modulePath));
  assert.deepEqual(
    orphanEntryModules,
    [],
    `plugin package entry present under features/ext-*/ but never imported by registry.local.ts: ${
      orphanEntryModules.join(", ")
    } — run npm run sync:ui-plugins, or remove the hand-placed package`,
  );
  assert.deepEqual(
    danglingEntryImports,
    [],
    `registry.local.ts imports a plugin entry that is not on disk: ${
      danglingEntryImports.join(", ")
    } — the generated registry and the copied packages must be written by the same sync run`,
  );
  // 本地条目住在 LOCAL_WORKSPACE_UI_CONTRIBUTIONS 那个顶层数组字面量里（生成物没有
  // defineWorkspaceUiRegistry 调用，校验发生在合并处）。按导出名认，不按「随便哪个
  // 数组字面量」认：workspace-registry.ts 消费的就是这个名字。
  const localEntryObjects = [];
  let localArrayDeclared = false;
  for (const top of local.module.statements) {
    if (!ts.isVariableStatement(top)) continue;
    for (const declared of top.declarationList.declarations) {
      if (declared.name.getText(local.module) !== "LOCAL_WORKSPACE_UI_CONTRIBUTIONS") continue;
      const initialized = declared.initializer;
      if (!initialized || !ts.isArrayLiteralExpression(initialized)) continue;
      localArrayDeclared = true;
      localEntryObjects.push(...initialized.elements.filter(ts.isObjectLiteralExpression));
    }
  }
  assert.ok(
    localArrayDeclared,
    "registry.local.ts must export LOCAL_WORKSPACE_UI_CONTRIBUTIONS as one explicit array literal",
  );
  const localImportNames = new Set(localImportRows.map((row) => row.local));
  function declaredPropertyOf(item, name) {
    return item.properties.find((property) => (
      ts.isPropertyAssignment(property) && property.name.getText(local.module) === name
    ));
  }
  for (const item of localEntryObjects) {
    const declaredProperty = (name) => declaredPropertyOf(item, name);
    const declaredId = declaredProperty("id");
    const label = declaredId && ts.isStringLiteral(declaredId.initializer)
      ? declaredId.initializer.text
      : "<unnamed entry>";
    const component = declaredProperty("Component");
    assert.ok(
      component
      && ts.isIdentifier(component.initializer)
      && localImportNames.has(component.initializer.text),
      `registry.local.ts entry ${label}: Component must bind an identifier this file imports`
      + " (the aliased local name), so every local contribution is backed by a real plugin module",
    );
  }
  // 每个插件组件模块只许依赖 SDK 合同、react、图标库与**同包兄弟模块**——同一张白名单
  // 对所有插件生效。判据整份住在 `./_plugin-import-predicate.mjs`（`pluginPackageImportOffenders`），
  // 这里只是它的一个消费点：白名单在两处各写一份拼写，就会在一份改了另一份没改时互相
  // 认同一个陈旧值，同时与真实边界脱节。包边界守卫的真值表逐条钉住这份判据的形状，包括
  // `api.ts` 被单独点名拒绝（`createWorkspaceExtensionApi(pluginId)` 一旦对插件可见，
  // 插件 A 就能给自己造一条绑定插件 B 的端口，路径限定当场失去意义）。
  //
  // 内建插件由 registry.ts 自己点名；其余插件模块只能是同步脚本复制进来的仓库外包，
  // 落点固定 features/ext-<name>/。手写一个 features/notmyext/workspace-plugin.ts 并从
  // registry.local.ts 引用它会在这里与上面的 local import 规则同时报红。
  const builtinPluginPaths = new Set(builtinPluginImports.map(
    (module) => `features/${module.replace("../", "")}`,
  ));
  // **内建插件走更窄的一档（`{ builtin: true }`）：没有 `ui.tsx`。** 它在 registry.ts 的
  // 模块闭包里，而那条闭包被 `node --test` 直接 import——Node 对 `.tsx` 报
  // `Unknown file extension`，整条 node 泳道会在 import 阶段炸掉。module-graph 守卫也会
  // 红，但那条说的是「闭包里有 .tsx」；这条说的是「内建插件不许 import 共享 UI」，两句话
  // 都要有人讲：只留 module-graph 那条，读到失败的人只知道闭包脏了，不知道自己踩的是
  // 白名单分档。
  for (const plugin of plugins) {
    const builtin = builtinPluginPaths.has(plugin.path);
    assert.deepEqual(
      pluginPackageImportOffenders(
        plugin.path,
        importsIn(plugin.module).map((row) => row.module),
        { builtin },
      ),
      [],
      `${plugin.path} imports outside the plugin allowlist`
      + (builtin
        ? " (builtin plugins live in the node-lane closure and may not import the shared .tsx UI)"
        : ""),
    );
  }
  assert.deepEqual(
    plugins.map((row) => row.path).filter((modulePath) => (
      !builtinPluginPaths.has(modulePath) && !/^features\/ext-[a-z0-9-]+\//.test(modulePath)
    )),
    [],
    "plugin modules the builtin registry does not own must live under features/ext-*/",
  );
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


test("the plugin sibling allowance is decided after normalization and never leaves the package", () => {
  const entry = "features/ext-alpha/workspace-plugin.tsx";
  for (const specifier of [
    "./labels.ts",
    "./model.tsx",
    // 归一化后仍落在包内。判据是「在本包目录之下」而不是「就在同一层」：包是扁平的
    // （同步脚本拒绝子目录），所以更宽的那半在真实包上不可达，但它让归一化成为承重
    // 的一步——按同一层判的实现会因为多出的 `/` 顺手挡住越界写法，越界那条就再也
    // 证明不了归一化有没有被做。
    "./sub/../labels.ts",
    "./sub/model.ts",
  ]) assert.equal(samePluginPackageSpecifier(entry, specifier), true, specifier);
  for (const specifier of [
    // 拼起来以 `features/ext-alpha/` 开头，归一化之后落在 app/ 里：前缀字符串比较
    // 会整条放过它，插件因此可以一串 `..` 直接 import 壳层模块。
    "./sub/../../app/page.tsx",
    "../ext-other/labels.ts",
    "../extension-sdk/api.ts",
    "../extension-sdk/contracts.ts",
    "../../app/api-client.ts",
    "./",
    ".",
    "react",
    "lucide-react",
  ]) assert.equal(samePluginPackageSpecifier(entry, specifier), false, specifier);
  // 内建插件不住在 ext-* 包里，没有兄弟豁免：它的 import 集合另有更窄的钉法（T6）。
  assert.equal(
    samePluginPackageSpecifier("features/agent-profile/workspace-plugin.ts", "./labels.ts"),
    false,
  );
});


test("plugin packages answer to the AST side-channel scan, not to this file's text regex", () => {
  // 这条用例把「文本正则不扫插件包」与「AST 判据仍然扫它」两半钉在一起。少了它，
  // 把 ext-* 摘出文本扫描面就只是一次单方面放宽，没人证明覆盖还在。
  const entry = "features/ext-probe/workspace-plugin.tsx";
  // 一份**只在注释和字符串里**出现禁用字样的插件入口。这正是文本正则的误报形态：
  // 插件作者在自己的包里解释「本模块不开 setTimeout( 轮询、也不用 WebSocket」，
  // 基座的正则却把这两个词当成实现。
  const commented = [
    "// 本插件不做轮询：既不 setTimeout( 也不开 WebSocket，全部 I/O 走注入的 api 端口。",
    'export const NOTE = "no setTimeout( / WebSocket here";',
    "export function Panel() { return null; }",
  ].join("\n");
  assert.match(commented, FORBIDDEN_SDK_TEXT, "夹具必须真的能触发那条文本正则（否则本用例空转）");
  assert.deepEqual(
    textScanTargets([{ path: entry, module: null }]),
    [],
    "仓库外插件包不进文本正则的扫描面",
  );
  assert.deepEqual(
    pluginPackageSideChannelOffenders(parseText(commented, entry)),
    [],
    "只出现在注释与字符串里的字样不是侧信道",
  );
  // 反方向：同一份源码真的调用了它，AST 判据必须红——覆盖是换了判据，不是被放弃。
  const calling = `${commented}\nexport const beat = () => setTimeout(() => undefined, 1000);`;
  assert.deepEqual(
    pluginPackageSideChannelOffenders(parseText(calling, entry)),
    ["setTimeout"],
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
  // workspaceExtensions.activateActor 现在只经共享函数 activateWorkspaceOwners
  // 间接调用；守卫 workspace-owner-transition-guard 钉住「只能从那里发出」。原断言
  // 钉「两个认证站点各出现一次」，现拆成两半：字面只出现一次（在共享函数体内），
  // 且该共享函数本身被两个认证站点各调用一次。
  assert.equal(calls.filter((row) => row.target === "workspaceExtensions.activateActor").length, 1);
  const sharedCalls = scopedCalls(page);
  assert.ok(
    sharedCalls.some((entry) => (
      entry.scope === "<module>.Home.activateWorkspaceOwners"
      && entry.target === "workspaceExtensions.activateActor"
    )),
    "workspaceExtensions.activateActor must be called from activateWorkspaceOwners",
  );
  const activateOwnersInvocations = sharedCalls
    .filter((entry) => entry.target === "activateWorkspaceOwners")
    .reduce((sum, entry) => sum + entry.count, 0);
  assert.equal(
    activateOwnersInvocations,
    2,
    "activateWorkspaceOwners must be invoked from the two authentication call sites",
  );
  assert.equal(calls.filter((row) => row.target === "workspaceExtensions.beginNotebookTransition").length, 1);
  // 结构项 F3：begin/settle 现在只在 `notebookTransitionSteps` 的 step 列表里声明，
  // settle 的第二个实参由 `opened` 布尔换成「本次 transition 有没有产出 outcome」。
  // 语义逐字不变（`outcome !== null` 就是旧的 `opened`），判据搬进了编排器。
  assert.ok(calls.some((row) => row.target === "workspaceExtensions.finishNotebookTransition"
    && row.arguments.join("|") === "ticket|outcome !== null"));
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
  // 第 4 实参（X5 T4）：`refreshSources` 只用 `sourceLibrary` 自己的具名 command
  // （`loadSourcesPage(currentPageRequest())`）——不是页面级重新派生的
  // `loadSourcesPage(currentNotebookId ?? "", {})` 之类的形态，那会绕开
  // `currentPageRequest()` 保存的分页/搜索状态。
  //
  // 语义判据排在下面那条文本 deepEqual **之前**，因为它才是这条不变量的内容：箭头
  // 体里只有那**一条**调用链，没有第二个调用。它替下了此前两条全局计数断言
  // （page.tsx 的 `useEffect` 总数 == 29、`sourceLibrary.loadSourcesPage` 调用点
  // == 2）——那两条对 page.tsx 任何无关改动都会误报，把一条关于第 4 实参的约束伪装
  // 成了对整个文件的冻结。"不新增 effect / 不新增领域 I/O" 这半条由本断言直接兑现：
  // 箭头体里只允许出现这两个调用目标，`useEffect(...)`、`reloadCheckup()`、
  // `loadNotebookCollection()` 一律在此报红。
  const refreshArgument = actions[0].arguments[3];
  assert.ok(refreshArgument, "createOwnedWorkspaceExtensionActions must receive a 4th argument");
  const refreshCallTargets = [];
  function collectRefreshCalls(node) {
    if (ts.isCallExpression(node)) refreshCallTargets.push(node.expression.getText(page));
    ts.forEachChild(node, collectRefreshCalls);
  }
  collectRefreshCalls(refreshArgument);
  assert.deepEqual(
    refreshCallTargets.sort(),
    ["sourceLibrary.currentPageRequest", "sourceLibrary.loadSourcesPage"],
    "the refreshSources argument must be exactly one call chain:"
    + " sourceLibrary.loadSourcesPage(sourceLibrary.currentPageRequest()) and nothing else",
  );
  // 第 5/6 实参（codex #578 R1 P2、R7 P2）：插件弹窗接入 root-dialog 裁决的两条接线，
  // 逐字钉住，因为它们的每一处都在挡一种**静默**失败：
  //  · `openDialog` 必须先经 `rootModals.open` 拿到 lease **再**记持有者——反过来会
  //    留下一个永远等不到 lease 的持有者（那一格在界面上"归它了"、弹窗却不会开）。
  //  · 记的是 `contributionId` 而不是 pluginId——同一个插件的多条 contribution 会
  //    一起挂出弹窗。
  //  · 持有者**换人**时必须先 `requestClose` 再 `open`（R7 P2）——owner 不变时
  //    `rootModals.open()` 会把旧持有者的 lease 原样递回来，那份 lease 捕获的
  //    `returnFocus` 还是旧持有者的触发按钮；反过来直接 `open` 会让新弹窗关闭后
  //    焦点跑回旧持有者而不是刚点开它的按钮。
  //  · `closeDialog` 必须按**当时**的持有者判（读 ref，不读渲染闭包里的 state），
  //    否则一次迟到的关闭会关掉别人刚开的弹窗。
  //  · 持有者的**清空**只有 `handleRootModalClosed` 那一条出口（下一条断言）。
  assert.deepEqual(actions[0].arguments.map((argument) => argument.getText(page)), [
    "workspaceExtensions.owner",
    "workspaceExtensions.owns",
    '() => {\n      rootModals.open("understanding", rootModals.captureWorkspaceOwner());\n    }',
    "() => sourceLibrary.loadSourcesPage(sourceLibrary.currentPageRequest())",
    '(contributionId: string) => {\n'
    + '      if (extensionDialogHolderRef.current !== null && extensionDialogHolderRef.current !== contributionId) {\n'
    + '        rootModals.requestClose("extension", "button");\n'
    + '      }\n'
    + '      if (rootModals.open("extension", rootModals.captureWorkspaceOwner())) {\n'
    + "        extensionDialogHolderRef.current = contributionId;\n"
    + "        setExtensionDialogHolder(contributionId);\n"
    + "      }\n"
    + "    }",
    "(contributionId, reason) => {\n"
    + "      if (extensionDialogHolderRef.current !== contributionId) return;\n"
    + '      rootModals.requestClose("extension", reason);\n'
    + "    }",
  ]);
  // 释放那一半：`extension` 的持有者只在协调器的 close sink 里清空。协调器只知道
  // "那一格开着"，谁认领了它只有这里记着——两者必须同生共死，否则一次冲突/切库之后
  // 会留下一个没有 lease 的持有者。这条是 mutation (d) 的守卫（行为上不可观测：没有
  // lease 时 host 本来就不渲染，所以只能按源码语义钉）。
  const closeSink = findFunctionIn(page, "Home", "handleRootModalClosed").getText(page);
  assert.match(
    closeSink,
    /case "extension":[\s\S]{0,200}?extensionDialogHolderRef\.current = null;[\s\S]{0,80}?setExtensionDialogHolder\(null\);[\s\S]{0,40}?return;/,
    "the extension dialog holder must be released through the coordinator close sink",
  );
  const outlets = jsxElements(page, "WorkspaceExtensionOutlet");
  for (const outlet of outlets) {
    assert.equal(outlet.bindings.ownerKey, "workspaceExtensions.ownerKey");
    assert.equal(outlet.bindings.actions, "workspaceExtensionActions");
    // 两个 outlet 都必须透传同一格的 view 与同一个持有者——只接一个的话，另一个 slot
    // 里的 contribution 会永远拿不到 open 的 dialog view（静默不可见）。
    assert.equal(outlet.bindings.dialog, 'rootModals.view("extension")');
    assert.equal(outlet.bindings.dialogHolder, "extensionDialogHolder");
  }
});


test("the api port and the builtin registry validate plugin ids with the identical pattern", async () => {
  // `api.ts` 的 `PLUGIN_ID` 与 `registry.ts` 的 `STABLE_ID` 是同一条判据的两处拼写：
  // 端口拿来限定路径前缀的那个 id，就是 registry 登记时校验过的那个 id。两边漂移不会
  // 报任何错，只会安静地打开或关上一道门——放宽 registry 而不放宽端口，一条合法登记的
  // contribution 每次请求都同步抛 TypeError；反过来放宽端口，端口开始接受 registry
  // 根本发不出来的 id 形状，路径限定的论证（"前缀就是登记时那个 id"）当场失去前提。
  //
  // 判据走 AST 而不是读裸文本：拿的是那个变量的初始化器节点、并要求它是正则字面量，
  // 所以改名/挪位置/加注释都不报红，改**取值**才报红。
  const modules = await appSourceModules();
  function regexLiteral(modulePath, name) {
    const entry = modules.find((row) => row.path === modulePath);
    assert.ok(entry, `${modulePath} not found（路径约定变了？守卫失效）`);
    let found;
    function visit(node) {
      if (
        ts.isVariableDeclaration(node)
        && node.name.getText(entry.module) === name
        && node.initializer
      ) found = node.initializer;
      ts.forEachChild(node, visit);
    }
    visit(entry.module);
    assert.ok(found, `${modulePath} 里没有 ${name}（改名或删除？守卫失效）`);
    assert.equal(
      found.kind,
      ts.SyntaxKind.RegularExpressionLiteral,
      `${modulePath} 的 ${name} 必须仍是一条正则字面量`,
    );
    return found.getText(entry.module);
  }
  assert.equal(
    regexLiteral("features/extension-sdk/api.ts", "PLUGIN_ID"),
    regexLiteral("features/extension-sdk/registry.ts", "STABLE_ID"),
    "the api port's plugin id pattern must stay byte-identical to the registry's stable id pattern",
  );
});
