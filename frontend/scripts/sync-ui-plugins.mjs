#!/usr/bin/env node
/**
 * 构建期把 `SILICON_NOTEBOOK_UI_PLUGINS` 指向的仓库外私有 UI 插件包装进基座。
 *
 * 零依赖（只用 node:fs/promises、node:path、node:url），由 package.json 的
 * `postinstall` 与五个 `pre*` 钩子调用，所以生成物总在——它们全部被 .gitignore
 * 忽略，绝不入库：已跟踪文件不受 .gitignore 影响，「提交空存根 + 脚本覆写」必然
 * 让 `git status` 变脏，唯一稳妥解就是「不入库 + 钩子保证它总在」。
 *
 * 三份产物：
 *   features/ext-<包名>/                    插件包副本（只搬 .ts/.tsx 与 manifest）
 *   features/extension-sdk/registry.local.ts 本地 contribution 清单（空态是空数组）
 *   .local/ui-extension-contract.json        部署期对账输入（内建行 + 各包 manifest 行）
 *
 * 本模块导出纯函数供单测直接调用；`main()` 只在它作为进程入口被运行时执行。
 */
import { copyFile, mkdir, readdir, readFile, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";


/** 包目录名：小写、连字符；刻意不许以 `ext-` 开头——落点是 `ext-<包名>`，
 *  允许它就会造出 `features/ext-ext-foo/` 这种自指路径。 */
const PACKAGE_NAME = /^[a-z][a-z0-9-]*$/;
/** 与 features/extension-sdk/registry.ts 的 `STABLE_ID` 同一条正则。 */
const STABLE_ID = /^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$/;
/** manifest 里的 `component` 必须是一个具名 React 导出。脚本不解析 TypeScript，
 *  所以「这个名字真的被导出了吗」由 next build 的类型检查回答。 */
const COMPONENT_NAME = /^[A-Z][A-Za-z0-9_]*$/;
/** 生成目录名，删除三重闸的第一闸。 */
const GENERATED_PACKAGE_DIR = /^ext-[a-z][a-z0-9-]*$/;
/** 与 registry.ts 的 `SLOTS` / `PERMISSIONS` / `MODES` 同一组取值。 */
const SLOTS = new Set(["workspace.side_panel", "source.detail_section"]);
const PERMISSIONS = new Set([
  "notebook:read",
  "notebook:write",
  "notebook:configure",
  "source:read",
  "source:write",
  "system:admin",
]);
const MODES = new Set(["all", "advanced"]);

const MANIFEST_FILE = "ui-plugin.json";
const ORIGIN_MARKER = ".ui-plugin-origin";
const ENTRY_FILES = ["workspace-plugin.ts", "workspace-plugin.tsx"];
const REGISTRY_LOCAL = "registry.local.ts";
const CONTRACT_RELATIVE = ".local/ui-extension-contract.json";
const BUILTIN_CONTRACT_RELATIVE = "../backend/tests/fixtures/ui_extension_contract.json";
/** 与 scripts/generate_ui_extension_contract.py 的 `_CONTRIBUTION_SORT_FIELDS` 一致：
 *  两侧排出同一个顺序，部署期对账才能逐行比对。 */
const CONTRACT_SORT_FIELDS = [
  "plugin_id",
  "version",
  "contribution_id",
  "slot",
  "capability",
];
/** 与 scripts/generate_ui_extension_contract.py 的 `API_VERSION` 一致。 */
const CONTRACT_API_VERSION = "1";

const PACKAGE_SHAPE_HINT = (
  "插件包只能是扁平的一层目录，除 ui-plugin.json 外只放 .ts/.tsx："
  + "带 package.json 或 node_modules 的包会被 tsconfig 的 exclude:[\"node_modules\"] 漏掉"
  + "（它只排除 frontend/node_modules），整个进 next build 的类型检查，还会引入第二个 React 实例；"
  + "CSS 无法参与基座样式表，视觉必须复用系统类与 :root token。"
  + "需要新依赖请走基座 PR。"
);


/** `":"` 分隔的插件包路径清单；空段丢弃，相对路径按 `cwd` 解析。 */
export function parsePluginRoots(value, cwd) {
  if (typeof value !== "string") return [];
  return value
    .split(":")
    .map((segment) => segment.trim())
    .filter((segment) => segment.length > 0)
    .map((segment) => path.resolve(cwd, segment));
}


function requireString(value, packageName, field) {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(
      `ui plugin "${packageName}": manifest field "${field}" must be a non-empty string`,
    );
  }
  return value;
}


function requireMatch(value, pattern, packageName, field) {
  const checked = requireString(value, packageName, field);
  if (!pattern.test(checked)) {
    throw new Error(
      `ui plugin "${packageName}": manifest field "${field}" is not a valid value: ${JSON.stringify(checked)}`,
    );
  }
  return checked;
}


function requireMember(value, allowed, packageName, field) {
  const checked = requireString(value, packageName, field);
  if (!allowed.has(checked)) {
    throw new Error(
      `ui plugin "${packageName}": manifest field "${field}" must be one of `
      + `${[...allowed].sort().join(", ")}, got ${JSON.stringify(checked)}`,
    );
  }
  return checked;
}


/** 校验后的完整 contribution（registry.local.ts 要用全部八个字段）。 */
function checkedContributions(manifest, packageName) {
  if (manifest === null || typeof manifest !== "object" || Array.isArray(manifest)) {
    throw new Error(`ui plugin "${packageName}": ${MANIFEST_FILE} must contain a JSON object`);
  }
  if (manifest.api_version !== CONTRACT_API_VERSION) {
    throw new Error(
      `ui plugin "${packageName}": manifest field "api_version" must be `
      + `${JSON.stringify(CONTRACT_API_VERSION)}, got ${JSON.stringify(manifest.api_version)}`,
    );
  }
  const declared = manifest.contributions;
  if (!Array.isArray(declared) || declared.length === 0) {
    throw new Error(
      `ui plugin "${packageName}": manifest field "contributions" must be a non-empty array`,
    );
  }
  return declared.map((row) => {
    if (row === null || typeof row !== "object" || Array.isArray(row)) {
      throw new Error(
        `ui plugin "${packageName}": every "contributions" entry must be a JSON object`,
      );
    }
    return {
      id: requireMatch(row.id, STABLE_ID, packageName, "id"),
      pluginId: requireMatch(row.plugin_id, STABLE_ID, packageName, "plugin_id"),
      pluginVersion: requireString(row.version, packageName, "version"),
      capability: requireMatch(row.capability, STABLE_ID, packageName, "capability"),
      slot: requireMember(row.slot, SLOTS, packageName, "slot"),
      permission: requireMember(row.permission, PERMISSIONS, packageName, "permission"),
      mode: requireMember(row.mode, MODES, packageName, "mode"),
      component: requireMatch(row.component, COMPONENT_NAME, packageName, "component"),
    };
  });
}


/** 校验一份 `ui-plugin.json`，返回它贡献的契约行；任一字段不合法即抛错。 */
export function validateManifest(manifest, packageName) {
  return checkedContributions(manifest, packageName).map((row) => ({
    plugin_id: row.pluginId,
    version: row.pluginVersion,
    contribution_id: row.id,
    slot: row.slot,
    capability: row.capability,
  }));
}


/** 读取并校验一个插件包目录，返回 `{ name, entry, manifest, files }`。 */
export async function inspectPackage(directory) {
  const absolute = path.resolve(directory);
  const name = path.basename(absolute);
  if (!PACKAGE_NAME.test(name) || name.startsWith("ext-")) {
    throw new Error(
      `ui plugin package directory name is invalid: ${JSON.stringify(name)} `
      + "(must match /^[a-z][a-z0-9-]*$/ and must not start with \"ext-\")",
    );
  }

  let listed;
  try {
    listed = await readdir(absolute, { withFileTypes: true });
  } catch {
    throw new Error(`ui plugin package directory is not readable: ${absolute}`);
  }

  const files = [];
  for (const entry of listed) {
    if (entry.isDirectory()) {
      throw new Error(
        `ui plugin "${name}": subdirectory ${JSON.stringify(entry.name)} is not allowed. `
        + PACKAGE_SHAPE_HINT,
      );
    }
    if (!entry.isFile()) {
      throw new Error(
        `ui plugin "${name}": ${JSON.stringify(entry.name)} is not a regular file. `
        + PACKAGE_SHAPE_HINT,
      );
    }
    const allowed = (
      entry.name === MANIFEST_FILE
      || (
        (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx"))
        && !entry.name.endsWith(".d.ts")
        && !entry.name.includes(".test.")
      )
    );
    if (!allowed) {
      throw new Error(
        `ui plugin "${name}": ${JSON.stringify(entry.name)} is not an allowed package file. `
        + PACKAGE_SHAPE_HINT,
      );
    }
    files.push(entry.name);
  }
  files.sort();

  if (!files.includes(MANIFEST_FILE)) {
    throw new Error(`ui plugin "${name}": ${MANIFEST_FILE} is missing`);
  }
  const entries = ENTRY_FILES.filter((candidate) => files.includes(candidate));
  if (entries.length !== 1) {
    throw new Error(
      `ui plugin "${name}": expected exactly one of ${ENTRY_FILES.join(" / ")}, found `
      + `${entries.length}`,
    );
  }

  let manifest;
  try {
    manifest = JSON.parse(await readFile(path.join(absolute, MANIFEST_FILE), "utf8"));
  } catch (error) {
    throw new Error(
      `ui plugin "${name}": ${MANIFEST_FILE} is not valid JSON (${error instanceof Error ? error.message : String(error)})`,
    );
  }
  checkedContributions(manifest, name);

  return { name, entry: entries[0], manifest, files, root: absolute };
}


function registryEntryLines(row, component) {
  return [
    "  {",
    `    id: ${JSON.stringify(row.id)},`,
    `    pluginId: ${JSON.stringify(row.pluginId)},`,
    `    pluginVersion: ${JSON.stringify(row.pluginVersion)},`,
    `    capability: ${JSON.stringify(row.capability)},`,
    `    slot: ${JSON.stringify(row.slot)},`,
    `    permission: ${JSON.stringify(row.permission)},`,
    `    mode: ${JSON.stringify(row.mode)},`,
    `    Component: ${component},`,
    "  },",
  ];
}


/** 渲染 `features/extension-sdk/registry.local.ts` 的完整文本。 */
export function renderLocalRegistry(entries) {
  const packages = [...entries].sort((left, right) => left.name.localeCompare(right.name));
  const origin = packages.length === 0
    ? "<unset>"
    : `<${packages.length} package(s): ${packages.map((row) => row.name).join(", ")}>`;
  const lines = [
    "// GENERATED by frontend/scripts/sync-ui-plugins.mjs — do not edit, do not commit.",
    `// source: SILICON_NOTEBOOK_UI_PLUGINS=${origin}`,
    "import type { WorkspaceUiContribution } from \"./contracts.ts\";",
  ];
  for (const row of packages) {
    const components = [
      ...new Set(checkedContributions(row.manifest, row.name).map((item) => item.component)),
    ].sort();
    lines.push(
      `import { ${components.join(", ")} } from "../ext-${row.name}/${row.entry}";`,
    );
  }
  lines.push("");
  if (packages.length === 0) {
    lines.push(
      "export const LOCAL_WORKSPACE_UI_CONTRIBUTIONS: readonly WorkspaceUiContribution[] = [];",
    );
    return `${lines.join("\n")}\n`;
  }
  lines.push(
    "export const LOCAL_WORKSPACE_UI_CONTRIBUTIONS: readonly WorkspaceUiContribution[] = [",
  );
  for (const row of packages) {
    for (const item of checkedContributions(row.manifest, row.name)) {
      lines.push(...registryEntryLines(item, item.component));
    }
  }
  lines.push("];");
  return `${lines.join("\n")}\n`;
}


function contractSortKey(row) {
  return CONTRACT_SORT_FIELDS.map((field) => String(row?.[field] ?? ""));
}


/** 内建行 + 本地行，按与后端生成器相同的排序键归一。 */
export function mergeContractRows(builtinRows, localRows) {
  return [...builtinRows, ...localRows].sort((left, right) => {
    const leftKey = contractSortKey(left);
    const rightKey = contractSortKey(right);
    for (let index = 0; index < CONTRACT_SORT_FIELDS.length; index += 1) {
      if (leftKey[index] === rightKey[index]) continue;
      return leftKey[index] < rightKey[index] ? -1 : 1;
    }
    return 0;
  });
}


/** 渲染 `.local/ui-extension-contract.json` 的完整文本。 */
export function renderContract(builtinRows, localRows) {
  return `${JSON.stringify({
    api_version: CONTRACT_API_VERSION,
    contributions: mergeContractRows(builtinRows, localRows),
  }, null, 2)}\n`;
}


async function builtinContractRows(frontendDir) {
  const fixture = path.resolve(frontendDir, BUILTIN_CONTRACT_RELATIVE);
  let rendered;
  try {
    rendered = await readFile(fixture, "utf8");
  } catch {
    throw new Error(
      `built-in UI extension contract fixture is missing: ${fixture}\n`
      + "  run `python3 scripts/generate_ui_extension_contract.py` to create it",
    );
  }
  const parsed = JSON.parse(rendered);
  if (!Array.isArray(parsed?.contributions)) {
    throw new Error(
      `built-in UI extension contract fixture has no "contributions" array: ${fixture}`,
    );
  }
  return parsed.contributions;
}


/**
 * 删除既有 `features/ext-*` 副本。三重闸缺一不可：目录名匹配、含
 * `.ui-plugin-origin` 标记、路径确实在 `features/` 之下。匹配前缀但没有标记的
 * 目录是人写的，抛错而不是删掉它。
 */
async function removeGeneratedPackages(featuresDir) {
  for (const entry of await readdir(featuresDir, { withFileTypes: true })) {
    if (!entry.isDirectory() || !GENERATED_PACKAGE_DIR.test(entry.name)) continue;
    const absolute = path.join(featuresDir, entry.name);
    const relative = path.relative(featuresDir, absolute);
    if (relative.length === 0 || relative.startsWith("..") || path.isAbsolute(relative)) {
      throw new Error(`refusing to remove a path outside features/: ${absolute}`);
    }
    try {
      await stat(path.join(absolute, ORIGIN_MARKER));
    } catch {
      throw new Error(
        `refusing to remove ${absolute}: it matches the generated "ext-*" prefix but carries `
        + `no ${ORIGIN_MARKER} marker, so it was not written by this script`,
      );
    }
    await rm(absolute, { recursive: true, force: true });
  }
}


/** 同步一次：校验全部包 → 清理旧副本 → 复制 → 写 registry.local.ts 与契约。 */
export async function syncUiPlugins({ frontendDir, roots }) {
  const base = path.resolve(frontendDir);
  // 先把所有包校验完再动文件树：任一包不合法就整次中止，不留半新半旧的副本。
  const packages = [];
  const claimed = new Map();
  for (const root of roots) {
    const inspected = await inspectPackage(root);
    const previous = claimed.get(inspected.name);
    if (previous !== undefined) {
      throw new Error(
        `ui plugin package name is not unique: ${JSON.stringify(inspected.name)} is declared by `
        + `both ${previous} and ${inspected.root}`,
      );
    }
    claimed.set(inspected.name, inspected.root);
    packages.push(inspected);
  }
  packages.sort((left, right) => left.name.localeCompare(right.name));

  const featuresDir = path.join(base, "features");
  await removeGeneratedPackages(featuresDir);
  const stamp = new Date().toISOString();
  for (const row of packages) {
    const target = path.join(featuresDir, `ext-${row.name}`);
    await mkdir(target, { recursive: true });
    for (const file of row.files) {
      await copyFile(path.join(row.root, file), path.join(target, file));
    }
    await writeFile(
      path.join(target, ORIGIN_MARKER),
      `${row.root}\n${stamp}\n`,
      "utf8",
    );
  }

  await writeFile(
    path.join(featuresDir, "extension-sdk", REGISTRY_LOCAL),
    renderLocalRegistry(packages),
    "utf8",
  );

  const builtinRows = await builtinContractRows(base);
  const localRows = packages.flatMap((row) => validateManifest(row.manifest, row.name));
  const contractPath = path.join(base, ...CONTRACT_RELATIVE.split("/"));
  await mkdir(path.dirname(contractPath), { recursive: true });
  await writeFile(contractPath, renderContract(builtinRows, localRows), "utf8");

  return { packages, rows: mergeContractRows(builtinRows, localRows) };
}


export async function main() {
  const frontendDir = fileURLToPath(new URL("../", import.meta.url));
  const roots = parsePluginRoots(
    process.env.SILICON_NOTEBOOK_UI_PLUGINS,
    process.cwd(),
  );
  const { packages, rows } = await syncUiPlugins({ frontendDir, roots });
  process.stdout.write(
    `sync-ui-plugins: ${packages.length} package(s), ${rows.length} contribution(s) `
    + `→ ${CONTRACT_RELATIVE}\n`,
  );
}


const invokedPath = process.argv[1];
if (invokedPath && path.resolve(invokedPath) === fileURLToPath(import.meta.url)) {
  await main();
}
