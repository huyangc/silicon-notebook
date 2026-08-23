import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";


const FRONTEND_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../..",
);
const REPO_DIR = path.resolve(FRONTEND_DIR, "..");

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
