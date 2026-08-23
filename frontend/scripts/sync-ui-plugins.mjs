#!/usr/bin/env node
/**
 * 构建期把 `SILICON_NOTEBOOK_UI_PLUGINS` 指向的仓库外私有 UI 插件包装进基座。
 *
 * 零依赖（只用 node:fs、node:fs/promises、node:path、node:url），由 package.json 的
 * `postinstall` 与五个 `pre*` 钩子调用，所以生成物总在——它们全部被 .gitignore
 * 忽略，绝不入库：已跟踪文件不受 .gitignore 影响，「提交空存根 + 脚本覆写」必然
 * 让 `git status` 变脏，唯一稳妥解就是「不入库 + 钩子保证它总在」。
 *
 * 三份产物：
 *   features/ext-<包名>/                    插件包副本（只搬 .ts/.tsx 与 manifest）
 *   features/extension-sdk/registry.local.ts 本地 contribution 清单（空态是空数组）
 *   .local/ui-extension-contract.json        部署期对账输入（内建行 + 各包 manifest 行）
 *
 * **两相位**：先把一切能失败的事做完（读内建契约、校验每个输入包、勘察
 * `features/ext-*` 的每个既有目录、渲染两份产物文本），全部通过之后才动文件树
 * （删旧副本 → 复制 → 写文件）。顺序不能反：先删后校验会在「已同步 A + 手工放了
 * 一个无标记的 ext-B」时把 A 删掉、再在 B 上抛错，而 registry.local.ts 仍指向刚被
 * 删掉的 A —— 之后每次 build 都红且不自愈。
 *
 * 本模块导出纯函数供单测直接调用；`main()` 只在它作为进程入口被运行时执行。
 */
import { realpathSync } from "node:fs";
import { copyFile, lstat, mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
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
/** 与 scripts/generate_ui_extension_contract.py 的 `_CONTRIBUTION_SORT_FIELDS` 一致。
 *  这份产物承诺的是**行集合**与后端 fixture 的内建行一致（加上各包 manifest 的行）；
 *  **行序以本脚本的排序键为准**——后端生成器当前不对写出的数组排序，对账方按集合
 *  或按同一组键重排后比对，别把它当成两侧逐行字节相同的承诺。 */
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


/**
 * 读取并校验一个插件包目录，返回 `{ name, entry, manifest, files, skipped, root }`。
 *
 * `skipped` 是包根下以 `.` 开头的**普通文件**（`.DS_Store` / `.gitignore` / 编辑器
 * 残留…）：既不复制也不报错——它们不参与构建，为它们中止整次同步只会把一台
 * Mac 上随手打开过目录的插件作者挡在门外。**子目录仍一律拒绝**（`.git` 也不例外）。
 */
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
  const skipped = [];
  for (const entry of listed) {
    if (entry.isDirectory()) {
      throw new Error(
        `ui plugin "${name}": subdirectory ${JSON.stringify(entry.name)} is not allowed. `
        + PACKAGE_SHAPE_HINT,
      );
    }
    if (entry.isFile() && entry.name.startsWith(".")) {
      skipped.push(entry.name);
      continue;
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
  skipped.sort();

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

  return { name, entry: entries[0], manifest, files, skipped, root: absolute };
}


/**
 * 生成文件里 import 用的本地别名。两个包各导出一个 `SearchEntry` 时，裸名字会让
 * `registry.local.ts` 出现两条同名 import——`node` / `tsc` 报重复标识符，整个前端
 * 构建挂在一份写着「do not edit」的生成文件上。别名按包名派生，故仍然确定。
 */
function componentAlias(packageName, component) {
  return `ext_${packageName.replaceAll("-", "_")}__${component}`;
}


function aliasesByPackage(packages) {
  const owners = new Map();
  const perPackage = new Map();
  for (const row of packages) {
    const components = [
      ...new Set(checkedContributions(row.manifest, row.name).map((item) => item.component)),
    ].sort();
    const aliases = new Map();
    for (const component of components) {
      const alias = componentAlias(row.name, component);
      const previous = owners.get(alias);
      if (previous !== undefined && previous !== row.name) {
        throw new Error(
          `ui plugin import alias is not unique: ${JSON.stringify(alias)} is produced by both `
          + `${previous} and ${row.name}`,
        );
      }
      owners.set(alias, row.name);
      aliases.set(component, alias);
    }
    perPackage.set(row.name, aliases);
  }
  return perPackage;
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
  const aliases = aliasesByPackage(packages);
  const origin = packages.length === 0
    ? "<unset>"
    : `<${packages.length} package(s): ${packages.map((row) => row.name).join(", ")}>`;
  const lines = [
    "// GENERATED by frontend/scripts/sync-ui-plugins.mjs — do not edit, do not commit.",
    `// source: SILICON_NOTEBOOK_UI_PLUGINS=${origin}`,
    "import type { WorkspaceUiContribution } from \"./contracts.ts\";",
  ];
  for (const row of packages) {
    const bindings = [...aliases.get(row.name).entries()]
      .map(([component, alias]) => `${component} as ${alias}`);
    lines.push(
      `import { ${bindings.join(", ")} } from "../ext-${row.name}/${row.entry}";`,
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
      lines.push(...registryEntryLines(item, aliases.get(row.name).get(item.component)));
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
  let parsed;
  try {
    parsed = JSON.parse(rendered);
  } catch (error) {
    throw new Error(
      `built-in UI extension contract fixture is not valid JSON: ${fixture} `
      + `(${error instanceof Error ? error.message : String(error)})`,
    );
  }
  if (!Array.isArray(parsed?.contributions)) {
    throw new Error(
      `built-in UI extension contract fixture has no "contributions" array: ${fixture}`,
    );
  }
  return parsed.contributions;
}


/** `.ui-plugin-origin` 必须是一个真实普通文件；符号链接同样不算数。 */
async function requireOriginMarker(absolute) {
  let marker;
  try {
    marker = await lstat(path.join(absolute, ORIGIN_MARKER));
  } catch {
    marker = null;
  }
  if (marker === null || !marker.isFile()) {
    throw new Error(
      `refusing to remove ${absolute}: it matches the generated "ext-*" prefix but carries `
      + `no ${ORIGIN_MARKER} marker, so it was not written by this script`,
    );
  }
}


/**
 * 勘察既有 `features/ext-*`：只有「名字匹配 + 真实目录 + 带标记」的才可以删。
 *
 * 「必须是真实目录」不是形式主义：`features/ext-foo` 若是一条指向
 * `features/extension-sdk` 的符号链接，`isDirectory()` 为假（readdir 是 lstat 语义）
 * 会让删除跳过它，随后 `mkdir(target, { recursive: true })` + `copyFile` 顺着链接
 * 写进 SDK 目录，直接覆盖受版本控制的 SDK 源码。
 *
 * 这里刻意**不再**做 `path.relative(featuresDir, …)` 的越界判断：`readdir` 回来的
 * `entry.name` 是单个路径分量，既不含分隔符也不可能是 `..`，那道闸对它不可达；
 * 真正有牙的是上面这条 lstat 检查。
 */
async function surveyGeneratedPackages(featuresDir) {
  let listed;
  try {
    listed = await readdir(featuresDir, { withFileTypes: true });
  } catch {
    throw new Error(`features directory is not readable: ${featuresDir}`);
  }
  const removals = [];
  for (const entry of listed) {
    if (!GENERATED_PACKAGE_DIR.test(entry.name)) continue;
    const absolute = path.join(featuresDir, entry.name);
    if (!entry.isDirectory() || entry.isSymbolicLink()) {
      throw new Error(
        `refusing to touch ${absolute}: it matches the generated "ext-*" prefix but is not a real `
        + "directory (copying into a symlink here would write straight through it into the "
        + "tracked extension-sdk sources)",
      );
    }
    await requireOriginMarker(absolute);
    removals.push(absolute);
  }
  removals.sort();
  return removals;
}


/** 复制前对落点再看一眼：只接受「不存在」或「带标记的真实目录」。 */
async function requireWritableTarget(target) {
  let info;
  try {
    info = await lstat(target);
  } catch (error) {
    if (error?.code === "ENOENT") return;
    throw new Error(`ui plugin copy target is not inspectable: ${target}`);
  }
  if (!info.isDirectory() || info.isSymbolicLink()) {
    throw new Error(
      `refusing to copy into ${target}: it exists but is not a real directory (a symlink here `
      + "would write straight through it into whatever it points at)",
    );
  }
  await requireOriginMarker(target);
}


/**
 * 全量勘察：读内建契约 → 逐个校验输入包 → 跨包查重 → 勘察既有副本 → 渲染两份
 * 产物文本。整个过程**只读**；任何一条失败都在文件树被动过之前抛出。
 */
async function surveySync({ frontendDir, roots }) {
  const base = path.resolve(frontendDir);
  const builtinRows = await builtinContractRows(base);

  const packages = [];
  const claimedNames = new Map();
  const claimedContributions = new Map();
  for (const root of roots) {
    const inspected = await inspectPackage(root);
    const previous = claimedNames.get(inspected.name);
    if (previous !== undefined) {
      throw new Error(
        `ui plugin package name is not unique: ${JSON.stringify(inspected.name)} is declared by `
        + `both ${previous} and ${inspected.root}`,
      );
    }
    claimedNames.set(inspected.name, inspected.root);
    for (const row of checkedContributions(inspected.manifest, inspected.name)) {
      const key = `${row.pluginId} ${row.id}`;
      const owner = claimedContributions.get(key);
      if (owner !== undefined) {
        throw new Error(
          `ui plugin contribution is not unique: plugin_id ${JSON.stringify(row.pluginId)} with `
          + `id ${JSON.stringify(row.id)} is declared by both ${owner} and ${inspected.name}`,
        );
      }
      claimedContributions.set(key, inspected.name);
    }
    packages.push(inspected);
  }
  packages.sort((left, right) => left.name.localeCompare(right.name));

  const featuresDir = path.join(base, "features");
  const removals = await surveyGeneratedPackages(featuresDir);

  const localRows = packages.flatMap((row) => validateManifest(row.manifest, row.name));
  return {
    base,
    featuresDir,
    packages,
    removals,
    registryText: renderLocalRegistry(packages),
    contractText: renderContract(builtinRows, localRows),
    rows: mergeContractRows(builtinRows, localRows),
  };
}


/** 同步一次：全量勘察（只读）→ 清理旧副本 → 复制 → 写 registry.local.ts 与契约。 */
export async function syncUiPlugins({ frontendDir, roots }) {
  const plan = await surveySync({ frontendDir, roots });

  for (const row of plan.packages) {
    if (row.skipped.length === 0) continue;
    process.stderr.write(
      `sync-ui-plugins: ui plugin "${row.name}": skipped ${row.skipped.length} dotfile(s): `
      + `${row.skipped.join(", ")}\n`,
    );
  }

  for (const absolute of plan.removals) {
    await rm(absolute, { recursive: true, force: true });
  }

  const stamp = new Date().toISOString();
  for (const row of plan.packages) {
    const target = path.join(plan.featuresDir, `ext-${row.name}`);
    await requireWritableTarget(target);
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
    path.join(plan.featuresDir, "extension-sdk", REGISTRY_LOCAL),
    plan.registryText,
    "utf8",
  );

  const contractPath = path.join(plan.base, ...CONTRACT_RELATIVE.split("/"));
  await mkdir(path.dirname(contractPath), { recursive: true });
  await writeFile(contractPath, plan.contractText, "utf8");

  return { packages: plan.packages, rows: plan.rows };
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


/**
 * 入口判定按 **realpath** 比对：`node …/sync-ui-plugins.mjs` 走一条含符号链接的路径
 * 时（共享 checkout、`npm` 的 bin 转发），`path.resolve(argv[1])` 与
 * `fileURLToPath(import.meta.url)` 是两个不同的字符串，`main()` 会静默不跑——
 * 脚本「成功」退出、什么产物都没有。
 */
function isProcessEntry() {
  const invoked = process.argv[1];
  if (typeof invoked !== "string" || invoked.length === 0) return false;
  const here = fileURLToPath(import.meta.url);
  try {
    return realpathSync(invoked) === realpathSync(here);
  } catch {
    return path.resolve(invoked) === here;
  }
}


if (isProcessEntry()) {
  await main();
}
