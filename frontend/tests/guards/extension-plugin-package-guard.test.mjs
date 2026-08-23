import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import { appSourceModules, callsIn, importsIn, parseText } from "../../test-support/semantic-source.mjs";
import { samePluginPackageSpecifier } from "./extension-ui-boundary.test.mjs";


const FRONTEND_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const REPO_DIR = path.resolve(FRONTEND_DIR, "..");
const FEATURES_DIR = path.join(FRONTEND_DIR, "features");

const SYNC_SCRIPT = "node scripts/sync-ui-plugins.mjs";
const HOOK_BODY = "npm run sync:ui-plugins";
// 六个钩子：`npm ci` 触发 postinstall（生产与打包脚本都用 npm ci），其余五个覆盖
// 每一条会读取 registry.local.ts 的本地命令。少一个就有一条路径跑在缺生成物的树上。
const LIFECYCLE_HOOKS = [
  "postinstall",
  "predev",
  "prebuild",
  "prestart",
  "pretest",
  "prelint",
];
// npm 只在**同名脚本存在**时运行它的 `pre*`：`prelint` 配 `lint`、`predev` 配 `dev`……
// 任何一个基名被删掉或改名，它的 `pre*` 就静默变成一个永不触发的孤儿 key，
// 生命周期断言照样全绿而那条路径跑在缺生成物的树上。
const HOOKED_BASE_SCRIPTS = ["dev", "build", "start", "test", "lint"];

const EXT_PACKAGE_RULE = "/frontend/features/ext-*/";
const LOCAL_REGISTRY_RULE = "/frontend/features/extension-sdk/registry.local.ts";

/** 同步脚本的落点目录名（`inspectPackage` 的 `PACKAGE_NAME` 加固定 `ext-` 前缀）。 */
const EXT_PACKAGE_DIR = /^ext-[a-z][a-z0-9-]*$/;
/** `appSourceModules()` 给 feature 模块的路径前缀形态。 */
const EXT_PACKAGE_MODULE = /^features\/ext-[a-z][a-z0-9-]*\//;
/** 同步脚本写进副本目录的出处标记；`readdir` 判形状时它是合法条目之一。 */
const ORIGIN_MARKER = ".ui-plugin-origin";
const MANIFEST_FILE = "ui-plugin.json";

const SDK_CONTRACTS = "features/extension-sdk/contracts.ts";
const SDK_UI = "features/extension-sdk/ui.tsx";
const SDK_API = "features/extension-sdk/api.ts";
/** 插件只能用基座已经装好的这两个包（§1.4 白名单的裸说明符那一半）。 */
const BARE_IMPORT_ALLOWLIST = new Set(["lucide-react", "react"]);

// 四条拒绝理由各写一句「为什么」，因为读到失败的人多半是插件作者，他手上没有本仓库的
// 上下文。`api.ts` 被单独点名：它是唯一一个「看起来该给、实际绝不能给」的 SDK 模块。
const API_PORT_REASON = "api 端口必须由 host 按 contribution.pluginId 注入到 actions.api"
  + "——插件自持 createWorkspaceExtensionApi 工厂，就等于可以构造别的插件的端口，路径限定当场失效";
const BUILTIN_UI_REASON = "内建插件住在 registry.ts 的 node 泳道闭包里，而 node --test 装不下 .tsx"
  + "：共享弹窗外壳只发给仓库外的 ext-* 包";
const OUTSIDE_REASON = "插件只能 import 同包兄弟模块、extension-sdk/contracts.ts 与 extension-sdk/ui.tsx";
const BARE_REASON = "插件只能用基座已有的 react / lucide-react；新依赖走基座 PR"
  + "（插件包不得带 package.json 或 node_modules）";

/** 计时器与两条常驻连接：兄弟模块和入口一样，不得自己开一条后台通道。 */
const SIDE_CHANNEL_CALLS = new Set([
  "setInterval",
  "setTimeout",
  "window.setInterval",
  "window.setTimeout",
  "globalThis.setInterval",
  "globalThis.setTimeout",
  // 动态 import：绕过静态说明符的白名单，`callsIn` 把它记成目标 `import`。
  "import",
]);
const SIDE_CHANNEL_CONSTRUCTORS = new Set(["EventSource", "WebSocket"]);


/**
 * 「这条 import 说明符越过插件包边界了吗」——`ext-*` 包与内建插件共用的**唯一**判据。
 *
 * `extension-ui-boundary.test.mjs` 的逐插件白名单检查调它，本文件的真值表、真包扫描与
 * 内建插件那条也调它。两处白名单必须是同一份判据：写成两份拼写，一份改了另一份不改，
 * 守卫就会在互相认同一个陈旧值的同时与真实边界脱节。
 *
 * 允许集合恰好四项：
 *  · 同包兄弟模块——判据委托给 `samePluginPackageSpecifier`，它在 `path.posix.normalize`
 *    之后判「归一化后仍在本包目录之下」。**归一化是承重的**：`./sub/../../app/page.tsx`
 *    拼起来仍以包目录开头，前缀比较会整条放过它。
 *  · `features/extension-sdk/contracts.ts`——类型合同。
 *  · `features/extension-sdk/ui.tsx`——共享弹窗外壳，**只给仓库外的包**（`builtin` 选项）。
 *  · 裸 `react` / `lucide-react`。
 *
 * 其余一律违规，其中 `features/extension-sdk/api.ts` 带自己的理由：它是唯一一个插件作者
 * 会真心以为该导入的模块。
 *
 * @param {string} packagePath  模块在 `appSourceModules()` 里的路径（`features/…`）
 * @param {readonly string[]} specifiers  该模块的全部 import/export-from 说明符
 * @param {{ builtin?: boolean }} [options]  内建插件档：白名单少一项 `ui.tsx`
 * @returns {{ specifier: unknown, reason: string }[]}
 */
export function pluginPackageImportOffenders(packagePath, specifiers, options = {}) {
  const builtin = options.builtin === true;
  const offenders = [];
  for (const specifier of specifiers) {
    if (typeof specifier !== "string" || specifier.length === 0) {
      offenders.push({ specifier, reason: OUTSIDE_REASON });
      continue;
    }
    if (!specifier.startsWith(".")) {
      if (BARE_IMPORT_ALLOWLIST.has(specifier)) continue;
      offenders.push({ specifier, reason: BARE_REASON });
      continue;
    }
    if (samePluginPackageSpecifier(packagePath, specifier)) continue;
    const resolved = path.posix.normalize(
      path.posix.join(path.posix.dirname(packagePath), specifier),
    );
    if (resolved === SDK_CONTRACTS) continue;
    if (resolved === SDK_UI) {
      if (!builtin) continue;
      offenders.push({ specifier, reason: BUILTIN_UI_REASON });
      continue;
    }
    offenders.push({
      specifier,
      reason: resolved === SDK_API ? API_PORT_REASON : OUTSIDE_REASON,
    });
  }
  return offenders;
}


/**
 * 兄弟模块自开的后台通道。
 *
 * `extension-ui-boundary.test.mjs` 的禁用文本扫描只覆盖**入口**模块
 * （`features/<x>/workspace-plugin.tsx?`），所以一个包只要把轮询挪进兄弟模块就整条逃掉——
 * 这正是 T2 评审点名的缺口。`fetch(` 刻意不在这里重复：`api-boundary.test.mjs` 已经对
 * `appSourceModules()` 的每个模块（含 `features/ext-<name>/` 的兄弟）普查过它，两处各写一遍
 * 只会让将来放宽其中一处时没人发现另一处还在拦。
 */
export function pluginPackageSideChannelOffenders(parsed) {
  const offenders = callsIn(parsed).filter((target) => SIDE_CHANNEL_CALLS.has(target));
  function collect(item) {
    if (ts.isNewExpression(item) && ts.isIdentifier(item.expression)) {
      const constructed = item.expression.text;
      if (SIDE_CHANNEL_CONSTRUCTORS.has(constructed)) offenders.push(`new ${constructed}`);
    }
    ts.forEachChild(item, collect);
  }
  collect(parsed);
  return [...new Set(offenders)].sort();
}


/** 一个模块的全部 import/export-from 说明符（含副作用 import 与 type-only）。 */
function moduleSpecifiersOf(parsed) {
  const found = [];
  function collect(item) {
    if (
      (ts.isImportDeclaration(item) || ts.isExportDeclaration(item))
      && item.moduleSpecifier
      && ts.isStringLiteral(item.moduleSpecifier)
    ) {
      found.push(item.moduleSpecifier.text);
    }
    ts.forEachChild(item, collect);
  }
  collect(parsed);
  return found;
}


/** 复制进来的包里允许出现的文件名（与同步脚本 `inspectPackage` 的准入逐条对齐）。 */
function allowedPackageFile(name) {
  if (name === MANIFEST_FILE || name === ORIGIN_MARKER) return true;
  if (name.endsWith(".d.ts") || name.includes(".test.")) return false;
  return name.endsWith(".ts") || name.endsWith(".tsx");
}


/** 磁盘上的 `features/ext-<name>/` 包目录名，字典序。 */
async function copiedPackageNames() {
  const listed = await readdir(FEATURES_DIR, { withFileTypes: true });
  return listed
    .filter((entry) => entry.isDirectory() && EXT_PACKAGE_DIR.test(entry.name))
    .map((entry) => entry.name)
    .sort();
}


test("every npm lifecycle that runs or checks the frontend syncs UI plugins first", async () => {
  const packageJson = JSON.parse(
    await readFile(path.join(FRONTEND_DIR, "package.json"), "utf8"),
  );
  assert.equal(packageJson.scripts["sync:ui-plugins"], SYNC_SCRIPT);
  assert.deepEqual(
    LIFECYCLE_HOOKS.map((hook) => [hook, packageJson.scripts[hook]]),
    LIFECYCLE_HOOKS.map((hook) => [hook, HOOK_BODY]),
  );
  for (const base of HOOKED_BASE_SCRIPTS) {
    assert.equal(
      typeof packageJson.scripts[base],
      "string",
      `script "${base}" must exist, otherwise "pre${base}" is an orphan npm never runs`,
    );
  }
});


test("generated plugin artifacts are ignored by a rule that cannot swallow the SDK", async () => {
  const ignoreRules = (
    await readFile(path.join(REPO_DIR, ".gitignore"), "utf8")
  ).split("\n").map((line) => line.trim());

  assert.ok(
    ignoreRules.includes(EXT_PACKAGE_RULE),
    `.gitignore must carry the rule ${EXT_PACKAGE_RULE}`,
  );
  assert.ok(
    ignoreRules.includes(LOCAL_REGISTRY_RULE),
    `.gitignore must carry the rule ${LOCAL_REGISTRY_RULE}`,
  );

  // 方向性：`ext-*` 只吞生成的包副本，绝不能吞掉手写的 `extension-sdk/`——两者
  // 只差第 4 个字符（`-` 对 `e`），写成 `ext*` 就会让整个 SDK 悄悄脱离版本控制。
  const asRegExp = new RegExp(`^${EXT_PACKAGE_RULE.replaceAll("*", "[^/]*")}$`);
  assert.match("/frontend/features/ext-ieee-xplore/", asRegExp);
  assert.doesNotMatch("/frontend/features/extension-sdk/", asRegExp);
});


test("the plugin import allowlist is exactly four things, and the api port is refused by name", () => {
  const entry = "features/ext-ieee-xplore/workspace-plugin.tsx";
  for (const specifier of [
    // 同包兄弟模块：一个包不必把全部代码塞进入口文件。
    "./ieee-model.ts",
    "../extension-sdk/contracts.ts",
    "../extension-sdk/ui.tsx",
    "react",
    "lucide-react",
  ]) {
    assert.deepEqual(pluginPackageImportOffenders(entry, [specifier]), [], specifier);
  }
  for (const specifier of [
    // 端口工厂：给了它，插件 A 就能构造插件 B 的端口。
    "../extension-sdk/api.ts",
    // SDK 内部：registry 是宿主的组装面，插件不参与组装。
    "../extension-sdk/registry.ts",
    "../extension-sdk/workspace-registry.ts",
    // 壳层：核心 HTTP 客户端与工作区本体。
    "../../app/api-client.ts",
    "../../app/page.tsx",
    // 别的插件包 / 内建 feature 的领域 API。
    "../ext-other/x.ts",
    "../agent-profile/profile-api.ts",
    // 基座没装的裸包。
    "next/link",
    "axios",
    // 归一化之后才看得出它离开了本包：前缀比较会整条放过它。
    "./sub/../../app/page.tsx",
  ]) {
    const offenders = pluginPackageImportOffenders(entry, [specifier]);
    assert.equal(offenders.length, 1, `${specifier} must produce exactly one offender`);
    assert.equal(offenders[0].specifier, specifier);
    assert.ok(offenders[0].reason.length > 0, specifier);
  }
  // `api.ts` 的理由必须与「泛泛的越界」不同：它是唯一一个插件作者会真心以为该导入的
  // SDK 模块，读到一句通用的「不在白名单里」他只会再试一次别的路径。
  const port = pluginPackageImportOffenders(entry, ["../extension-sdk/api.ts"]);
  const generic = pluginPackageImportOffenders(entry, ["../extension-sdk/registry.ts"]);
  assert.notEqual(port[0].reason, generic[0].reason);
  assert.match(port[0].reason, /pluginId/);
  // 多条说明符一次判：违规逐条列出，合规的不出现在结果里。
  assert.deepEqual(
    pluginPackageImportOffenders(entry, ["react", "axios", "./ieee-model.ts", "next/link"])
      .map((offender) => offender.specifier),
    ["axios", "next/link"],
  );
});


test("the builtin plugin allowlist is the same predicate minus the shared .tsx UI", () => {
  const builtin = "features/agent-profile/workspace-plugin.ts";
  const outOfTree = "features/ext-ieee-xplore/workspace-plugin.tsx";
  // 唯一的分档：仓库外的包拿得到共享弹窗外壳，内建插件拿不到。
  assert.deepEqual(pluginPackageImportOffenders(outOfTree, ["../extension-sdk/ui.tsx"]), []);
  const refused = pluginPackageImportOffenders(builtin, ["../extension-sdk/ui.tsx"], { builtin: true });
  assert.equal(refused.length, 1);
  assert.equal(refused[0].reason, BUILTIN_UI_REASON);
  // 其余三项在两档下逐字相同——分档只差这一条，不是两份互不相干的白名单。
  for (const specifier of ["../extension-sdk/contracts.ts", "react", "lucide-react"]) {
    assert.deepEqual(pluginPackageImportOffenders(builtin, [specifier], { builtin: true }), [], specifier);
  }
  assert.equal(
    pluginPackageImportOffenders(builtin, ["../extension-sdk/api.ts"], { builtin: true }).length,
    1,
  );
});


test("every module of every copied plugin package stays inside the import allowlist", async () => {
  // 扫的是包内**所有** `.ts/.tsx`，不只是 `workspace-plugin` 入口：boundary 守卫的
  // 逐插件白名单按 `features/<x>/workspace-plugin.tsx?` 认模块，兄弟模块此前无人扫，
  // 一个包只要把越界 import 挪进兄弟文件就整条逃掉。
  const modules = (await appSourceModules()).filter((row) => EXT_PACKAGE_MODULE.test(row.path));
  // 分工：公网仓库里这个集合恒为空（`features/ext-*/` 是生成物，不入库），所以这条
  // 用例在这里是空转的，判据本身的非空验证由上面的真值表承担。真包上的验证发生在
  // 装载了私有插件的树上——`npm run sync:ui-plugins` 之后跑同一条命令。
  for (const { path: modulePath, module } of modules) {
    assert.deepEqual(
      pluginPackageImportOffenders(modulePath, moduleSpecifiersOf(module)),
      [],
      modulePath,
    );
  }
});


test("plugin siblings open no background channel of their own", async () => {
  // 探针先证明判据非空——公网仓库里没有 ext-* 包，少了它整条用例是空转的。
  const probe = parseText(
    [
      'export const beat = () => setInterval(() => undefined, 1000);',
      'export const later = () => window.setTimeout(() => undefined, 10);',
      'export const stream = () => new EventSource("/api/extensions/x/stream");',
      'export const socket = () => new WebSocket("wss://example.invalid");',
      'export const lazy = () => import("./model.ts");',
    ].join("\n"),
    "features/ext-probe/model.ts",
  );
  assert.deepEqual(pluginPackageSideChannelOffenders(probe), [
    "import",
    "new EventSource",
    "new WebSocket",
    "setInterval",
    "window.setTimeout",
  ]);
  const quiet = parseText(
    'export const label = (name: string) => name.trim();',
    "features/ext-probe/labels.ts",
  );
  assert.deepEqual(pluginPackageSideChannelOffenders(quiet), []);

  const modules = (await appSourceModules()).filter((row) => EXT_PACKAGE_MODULE.test(row.path));
  for (const { path: modulePath, module } of modules) {
    assert.deepEqual(pluginPackageSideChannelOffenders(module), [], modulePath);
  }
});


test("copied plugin packages stay flat and carry nothing but TS, the manifest and the origin marker", async () => {
  // 同步脚本已经在**源**包上查过这些（`inspectPackage`），这条查的是**落点**：手放进
  // `features/ext-x/` 的目录同样会被 tsconfig 的 include 吞进 next build 的类型检查，
  // 而它从来没经过脚本。带 `node_modules/`（tsconfig 的 exclude 只排除
  // `frontend/node_modules`）或 `package.json` 的包会拉进第二份 React 实例。
  for (const name of await copiedPackageNames()) {
    const entries = await readdir(path.join(FEATURES_DIR, name), { withFileTypes: true });
    assert.ok(entries.length > 0, `features/${name}/ is empty`);
    for (const entry of entries) {
      assert.ok(
        entry.isFile(),
        `features/${name}/${entry.name} must be a regular file: plugin packages are flat`
        + "（子目录会把 node_modules 一起带进类型检查）",
      );
      assert.ok(
        allowedPackageFile(entry.name),
        `features/${name}/${entry.name} is not an allowed package file`
        + "（只许 ui-plugin.json、.ui-plugin-origin 与非 .d.ts / 非 .test. 的 .ts/.tsx）",
      );
    }
    assert.ok(
      entries.some((entry) => entry.name === ORIGIN_MARKER),
      `features/${name}/ has no ${ORIGIN_MARKER}: it was not written by npm run sync:ui-plugins`,
    );
    assert.ok(
      entries.some((entry) => entry.name === MANIFEST_FILE),
      `features/${name}/ has no ${MANIFEST_FILE}`,
    );
  }
});


test("the builtin plugin imports exactly react, the icon set and the SDK contracts", async () => {
  const modules = await appSourceModules();
  const registry = modules.find((row) => row.path === "features/extension-sdk/registry.ts");
  assert.ok(registry, "features/extension-sdk/registry.ts must remain the builtin registry");
  // 内建插件按 registry.ts 自己点名的模块认，不手抄清单。
  const builtinPaths = [...new Set(
    importsIn(registry.module)
      .map((row) => row.module)
      .filter((specifier) => /^\.\.\/[a-z0-9-]+\/workspace-plugin\.tsx?$/.test(specifier))
      .map((specifier) => `features/${specifier.replace("../", "")}`),
  )].sort();
  assert.ok(builtinPaths.length > 0, "registry.ts must name at least one builtin plugin module");
  for (const modulePath of builtinPaths) {
    const builtin = modules.find((row) => row.path === modulePath);
    assert.ok(builtin, `${modulePath} is imported by registry.ts but missing on disk`);
    // 恰为这三项。它同时钉住三件事：没有 `ui.tsx`（node 泳道装不下 .tsx）、没有
    // `api.ts`（端口只经 host 注入）、也没有兄弟模块（内建插件不在 ext-* 包里，
    // 没有兄弟豁免）。将来若出现第二个不用图标的内建插件，改的是这份期望集合本身，
    // 不是把断言放宽成「子集」——那会让 ui.tsx 与 api.ts 一起溜回来。
    assert.deepEqual(
      [...new Set(moduleSpecifiersOf(builtin.module))].sort(),
      ["../extension-sdk/contracts.ts", "lucide-react", "react"],
      modulePath,
    );
  }
});
