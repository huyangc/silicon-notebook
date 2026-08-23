import test from "node:test";
import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import { appSourceModules, importsIn, parseText } from "../../test-support/semantic-source.mjs";
// 判据住在自己的模块里（不是测试入口，node 泳道按后缀收不到它）。此前它住在
// `extension-ui-boundary.test.mjs`、由本文件 import 过去，于是单跑任一守卫都会把
// 另一份的全部用例一起加载并再跑一遍。
import {
  BUILTIN_UI_REASON,
  pluginPackageImportOffenders,
  pluginPackageSideChannelOffenders,
} from "./_plugin-import-predicate.mjs";


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
      'export const legacy = () => new XMLHttpRequest();',
      // 卸载时仍会送达的一次性外发：最方便的「静默把数据带出去」形态。裸拼写与
      // `navigator.` 拼写是同一件事（`const { sendBeacon } = navigator` 之后就是裸的）。
      'export const beacon = () => navigator.sendBeacon("https://example.invalid", "x");',
      'export const lazy = () => import("./model.ts");',
    ].join("\n"),
    "features/ext-probe/model.ts",
  );
  assert.deepEqual(pluginPackageSideChannelOffenders(probe), [
    "import",
    "navigator.sendBeacon",
    "new EventSource",
    "new WebSocket",
    "new XMLHttpRequest",
    "setInterval",
    "window.setTimeout",
  ]);
  const barehanded = parseText(
    'const { sendBeacon } = navigator;\nexport const ping = () => sendBeacon("https://example.invalid", "x");',
    "features/ext-probe/beacon.ts",
  );
  assert.deepEqual(pluginPackageSideChannelOffenders(barehanded), ["sendBeacon"]);
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
