// arxiv-sample-ui-package.test.mjs — X9 PR-B T3
//
// Exercises the arXiv sample plugin's front-end half
// (`examples/extensions/arxiv-search/ui/arxiv-search/`) two ways:
//
//  1. Package-shape conformance, by calling the *real* build-time tooling
//     (`frontend/scripts/sync-ui-plugins.mjs`'s exported pure functions) on
//     the real package directory — never a copy, never a mock manifest.
//     This is deliberately not just JSON-schema validation: `inspectPackage`
//     is the exact function `npm run sync:ui-plugins` runs against every
//     `SILICON_NOTEBOOK_UI_PLUGINS` entry, so a failure here is a failure a
//     deployment configuring this sample would actually hit.
//  2. `search-panel-model.ts`'s pure state-transition logic, imported
//     directly — Node's native TypeScript type-stripping runs it exactly
//     the way `features/extension-sdk/registry.ts` already relies on being
//     importable by this same `node --test` lane (see that file's own
//     header comment).
//
// This file does **not** touch the file tree (no copy into
// `frontend/features/ext-arxiv-search/`, no env var) — the sample package
// deliberately never enters the default `npm run test` tree (CLAUDE.md
// "Workspace UI registry": `extension-ui-host.component.test.tsx`'s
// "length 1 with zero plugins" must stay green). The G2-only lane that
// actually synchronizes it and exercises the five `extension-*.test.mjs`
// guards against it lives in `scripts/check_sample_plugin.sh`.
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import { parseText } from "../../test-support/semantic-source.mjs";
import { inspectPackage, validateManifest } from "../../scripts/sync-ui-plugins.mjs";
import {
  FIRST_PAGE_START,
  MAX_IMPORT_URLS,
  MAX_QUERY_TERMS,
  QUERY_MAX_CHARS,
  classifyImportReceipt,
  countQueryTerms,
  deselectPaper,
  foldImportedUrls,
  formatAuthors,
  mergeCatalog,
  nextPageStart,
  queryExceedsCharLimit,
  selectPaper,
  selectedImportUrls,
} from "../../../examples/extensions/arxiv-search/ui/arxiv-search/search-panel-model.ts";


const REPO_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../..",
);
const PACKAGE_DIR = path.join(
  REPO_DIR, "examples", "extensions", "arxiv-search", "ui", "arxiv-search",
);

// The backend manifest's authoritative values — bundle.py's `PLUGIN_ID`,
// `_PANEL` and `ExtensionManifest(version=...)`. Kept as one literal object
// here (rather than importing Python) so a one-character drift in either
// file's copy is caught by a plain string comparison; the cross-language
// round trip through a running backend belongs to T4's
// `test_ui_manifest_matches_the_backend_manifest`.
//
// ⚠ `permission` is **not** one of the mirrored backend values — the backend's
// `UiContributionDeclaration` has only `id`/`slot`/`capability`, so this field
// exists on the front-end manifest alone and T4's parity test will never see
// it. It must stay `notebook:write`: in the `workspace.side_panel` outlet the
// shell hard-codes `sourceRead: false` / `sourceWrite: false`
// (`app/page.tsx`'s `workspaceExtensionPermissions`, because those two describe
// a *selected source* and none is selected out here), so a contribution
// declaring `source:write` is structurally unreachable — the entry button would
// never render for anyone. `notebook:write` maps to `snapshot.notebookWrite` →
// `capabilities.canWriteNotebook`, the browser mirror of the backend
// `sources:write` capability the plugin's `/import` route needs. This assertion
// is what stops a future edit from "tidying" it back.
const EXPECTED_CONTRIBUTION = Object.freeze({
  id: "examples.arxiv_search.panel",
  plugin_id: "examples.arxiv_search",
  version: "0.1.0",
  capability: "examples.arxiv_search.available",
  slot: "workspace.side_panel",
  permission: "notebook:write",
  mode: "all",
  component: "ArxivSearchEntry",
});


test("arXiv 样板 UI 包过真实的 inspectPackage/validateManifest（不复制文件树）", async () => {
  const inspected = await inspectPackage(PACKAGE_DIR);

  // 包目录名本身必须过 sync-ui-plugins.mjs 的 PACKAGE_NAME 校验且不以 ext- 开头
  // ——inspectPackage 若名字不合法会直接抛错，能走到这里就已经证明了这条；
  // 显式再断言一次基名，让这条意图在测试里可读。
  assert.equal(inspected.name, "arxiv-search");
  assert.equal(inspected.entry, "workspace-plugin.tsx");

  // 反向断言：包内文件集合恰好是三个预期文件，没有多余文件混进去（变异①：塞一个
  // styles.css 会先在 inspectPackage 内部因「不是允许的包文件」直接抛错，走不到
  // 这条断言；两条防线独立生效)。
  assert.deepEqual(inspected.files, [
    "search-panel-model.ts",
    "ui-plugin.json",
    "workspace-plugin.tsx",
  ]);
  assert.deepEqual(inspected.skipped, []);

  const [row] = inspected.manifest.contributions;
  assert.ok(row, "ui-plugin.json 的 contributions 数组为空（清单被改坏？）");
  // 逐字段核对 §1.8 声明的原始清单形状（id/plugin_id/version/capability/slot/
  // permission/mode/component）——变异②(改一个字)会让下面某一行断言红。
  for (const [field, expected] of Object.entries(EXPECTED_CONTRIBUTION)) {
    assert.equal(row[field], expected, `ui-plugin.json 的 "${field}" 字段与后端 manifest 不一致`);
  }

  // 再走一遍真实的 validateManifest（同步脚本合并 .local/ui-extension-contract.json
  // 用的正是这个函数），核对它派生出的契约行同样吻合。
  const [contractRow] = validateManifest(inspected.manifest, inspected.name);
  assert.deepEqual(contractRow, {
    plugin_id: EXPECTED_CONTRIBUTION.plugin_id,
    version: EXPECTED_CONTRIBUTION.version,
    contribution_id: EXPECTED_CONTRIBUTION.id,
    slot: EXPECTED_CONTRIBUTION.slot,
    capability: EXPECTED_CONTRIBUTION.capability,
  });
});


test("search-panel-model：勾选/取消勾选按同一 id 幂等", () => {
  const empty = new Set();
  const selectedOnce = selectPaper(empty, "2401.00001");
  assert.deepEqual([...selectedOnce], ["2401.00001"]);
  const selectedTwice = selectPaper(selectedOnce, "2401.00001");
  // 幂等的可观察证据是引用相等，不只是内容相等——否则调用方用 useState 存它时，
  // 重复勾选同一条会白触发一次多余渲染。
  assert.equal(selectedTwice, selectedOnce);

  const deselectedOnce = deselectPaper(selectedOnce, "2401.00001");
  assert.deepEqual([...deselectedOnce], []);
  const deselectedTwice = deselectPaper(deselectedOnce, "2401.00001");
  assert.equal(deselectedTwice, deselectedOnce);

  // 取消一个从未勾选过的 id 同样幂等（返回同一个空集合引用）。
  assert.equal(deselectPaper(empty, "not-there"), empty);
});


test("search-panel-model：start 翻页按上一页真实返回条数推进", () => {
  assert.equal(FIRST_PAGE_START, 0);
  assert.equal(nextPageStart(0, 10), 10);
  assert.equal(nextPageStart(10, 7), 17);
  // 零条返回（例如最后一页恰好取完）不推进——没有下一页可翻。
  assert.equal(nextPageStart(10, 0), 10);
});


test("search-panel-model：作者串按顿号拼接，折叠空白项，空表给空串", () => {
  assert.equal(formatAuthors(["Alice Smith", "Bob Lee"]), "Alice Smith、Bob Lee");
  assert.equal(formatAuthors(["  Carol  ", "", "  "]), "Carol");
  assert.equal(formatAuthors([]), "");
});


test("search-panel-model：一次导入的条数上限与插件路由同值", () => {
  // 服务端真源是 routes.py::MAX_IMPORT_URLS（超限 400）。这里刻意是**手抄**一份
  // 而不是去读那个 .py：样板插件不带跨语言契约测试（与上面 EXPECTED_CONTRIBUTION
  // 同一条口径，真正的跨语言对账属于 T4）。抄写方向是安全的——服务端调高而这里
  // 没跟上只是更严，调低了前端照发、400 原样上屏。
  assert.equal(MAX_IMPORT_URLS, 20);
});


test("search-panel-model：检索词条数上限与插件路由同值", () => {
  // 服务端真源是 client.py::MAX_QUERY_TERMS（routes.py::search 现在超限即以
  // 400 拒绝，不再静默截断）。与上面 MAX_IMPORT_URLS 同一条口径——手抄一份而不
  // 读 .py，抄写方向安全：服务端调高而这里没跟上只是更保守，调低了前端照发、
  // 400 原样上屏。跨语言对账属于 backend 侧的
  // test_the_ui_package_query_term_cap_matches_the_route_cap。
  assert.equal(MAX_QUERY_TERMS, 8);
});


test("search-panel-model：countQueryTerms 按空白分词计数，空串/纯空白记零", () => {
  // 与服务端 `client.py::build_query_url` 的裸 `query.split()` 同一条分词
  // 口径：按任意空白游程切分，忽略首尾空白，空/纯空白记零个词（Python
  // `"".split() == []`，而不是 JS 原生 `"".split(/\s+/)` 会给出的长度为 1）。
  assert.equal(countQueryTerms(""), 0);
  assert.equal(countQueryTerms("   "), 0);
  assert.equal(countQueryTerms("diffusion"), 1);
  assert.equal(countQueryTerms("diffusion model"), 2);
  // 词间/首尾的多重空白（含制表符、换行）折叠——按词数而不是按空白字符数计。
  assert.equal(countQueryTerms("  a\tb\n\nc   "), 3);

  const atLimit = Array.from({ length: MAX_QUERY_TERMS }, (_, i) => `t${i}`).join(" ");
  assert.equal(countQueryTerms(atLimit), MAX_QUERY_TERMS);
  assert.equal(countQueryTerms(`${atLimit} one-too-many`), MAX_QUERY_TERMS + 1);
});


test("search-panel-model：检索关键词字符上限与插件路由同值", () => {
  // 服务端真源是 routes.py::search 里的 QUERY_MAX_CHARS（超限即以 400 拒绝，
  // 且这道闸排在词数检查**之前**运行）。与上面两条上限同一条口径——手抄一份而
  // 不读 .py，抄写方向安全：服务端调高而这里没跟上只是更保守，调低了前端照发、
  // 400 原样上屏。跨语言对账属于 backend 侧的
  // test_the_ui_package_query_char_cap_matches_the_route_cap。
  assert.equal(QUERY_MAX_CHARS, 200);
});


test("search-panel-model：queryExceedsCharLimit 先按服务端同款裁边，再按 Unicode 码点数判断（P2-3）", () => {
  // 裁边对齐服务端 `routes.py::search` 的 `(q or "").strip()`：首尾空白不计入
  // 长度，判据是 `>`（恰好等于上限不算超限）。
  const padded = ` ${"a".repeat(QUERY_MAX_CHARS)} `;
  assert.equal(queryExceedsCharLimit(padded), false);
  assert.equal(queryExceedsCharLimit(`${padded}b`), true);
  assert.equal(queryExceedsCharLimit("a".repeat(QUERY_MAX_CHARS)), false);
  assert.equal(queryExceedsCharLimit("a".repeat(QUERY_MAX_CHARS + 1)), true);

  // 按 Unicode **码点**数而非 UTF-16 code unit 数计——服务端 `len(str)` 数的是
  // 码点（Python 字符串本就是码点序列）。这里用一个代理对 emoji（一个码点、两
  // 个 UTF-16 code unit）钉住方向：QUERY_MAX_CHARS 个这样的字符恰好在码点上限，
  // 但若误按 `.length` 算会得到 QUERY_MAX_CHARS*2，被错误判成超限。
  const emoji = "\u{1F4A1}"; // 💡
  assert.equal(emoji.length, 2, "夹具本身必须是一个代理对字符，否则这条测不出方向");
  const atLimitEmoji = emoji.repeat(QUERY_MAX_CHARS);
  assert.equal(queryExceedsCharLimit(atLimitEmoji), false);
  assert.equal(queryExceedsCharLimit(`${atLimitEmoji}${emoji}`), true);
});


test("search-panel-model：导入回执三态——created / repeat / rejected", () => {
  const catalog = mergeCatalog(new Map(), [
    { arxiv_id: "a", title: "Paper A", authors: [], published: "", summary: "", pdf_url: "https://arxiv.org/pdf/a", abs_url: "https://arxiv.org/abs/a" },
    { arxiv_id: "b", title: "Paper B", authors: [], published: "", summary: "", pdf_url: "https://arxiv.org/pdf/b", abs_url: "https://arxiv.org/abs/b" },
  ]);
  const selected = selectPaper(selectPaper(new Set(), "a"), "b");
  assert.deepEqual(
    selectedImportUrls(catalog, selected),
    ["https://arxiv.org/pdf/a", "https://arxiv.org/pdf/b"],
  );

  const response = {
    created: [{ source_id: "src-1", title: "Paper A", url: "https://arxiv.org/pdf/a" }],
    rejected: [{ url: "https://arxiv.org/pdf/b", reason: "已存在同名来源" }],
  };

  // 第一次导入：a 从未在本会话见过，判「已创建」；b 被拒并带原因。
  const firstReceipt = classifyImportReceipt(response, new Set());
  assert.deepEqual(firstReceipt.get("https://arxiv.org/pdf/a"), { status: "created", title: "Paper A" });
  assert.deepEqual(
    firstReceipt.get("https://arxiv.org/pdf/b"),
    { status: "rejected", reason: "已存在同名来源" },
  );

  const remembered = foldImportedUrls(new Set(), firstReceipt);
  assert.deepEqual([...remembered], ["https://arxiv.org/pdf/a"]);
  // 被拒的 URL 不进「已见过」记忆——它没有被创建，下次可以重试而不是被误判为复用。
  assert.ok(!remembered.has("https://arxiv.org/pdf/b"));

  // 折同一份回执两次是幂等的（第二次没有新增，返回同一个引用）。
  assert.equal(foldImportedUrls(remembered, firstReceipt), remembered);

  // 第二次导入同一批 URL。服务端**没有**内容去重（add_url_sources 无条件
  // insert_source + 完整解析），所以 a 又被报成 created，且库里此刻多半真的多了
  // 一份重复来源。第三态因此是本会话记忆给出的**警告**（repeat），不是「服务端
  // 复用了旧行」那种保证——后者根本不会发生。
  const secondReceipt = classifyImportReceipt(response, remembered);
  assert.deepEqual(secondReceipt.get("https://arxiv.org/pdf/a"), { status: "repeat", title: "Paper A" });
});


/**
 * 从 `./search-panel-model[.ts]` import 进来的名字（`{ imported, local }`）。
 *
 * 两种后缀写法都收：仓库里插件包写的是带 `.ts` 的说明符，但「有没有写后缀」不是
 * 这条守卫要管的事，钉死一种写法只会在无关的改写上假红。type-only import 也照收
 * ——它们本来就不会出现在「被调用的函数」那一侧，收进来不影响 ⊇ 判定。
 */
function importedFromModel(sourceFile) {
  const names = [];
  for (const statement of sourceFile.statements) {
    if (
      !ts.isImportDeclaration(statement)
      || !ts.isStringLiteral(statement.moduleSpecifier)
    ) {
      continue;
    }
    const specifier = statement.moduleSpecifier.text.replace(/\.ts$/, "");
    if (specifier !== "./search-panel-model") continue;
    const bindings = statement.importClause?.namedBindings;
    if (!bindings || !ts.isNamedImports(bindings)) continue;
    for (const element of bindings.elements) {
      names.push({
        imported: element.propertyName?.text ?? element.name.text,
        local: element.name.text,
      });
    }
  }
  return names;
}


/** `search-panel-model.ts` 导出的**函数**名（不含 type / 常量）。 */
function exportedModelFunctions(sourceFile) {
  const names = new Set();
  for (const statement of sourceFile.statements) {
    const exported = ts.getCombinedModifierFlags(statement) & ts.ModifierFlags.Export;
    if (!exported) continue;
    if (ts.isFunctionDeclaration(statement) && statement.name) {
      names.add(statement.name.text);
    }
  }
  return names;
}


/** 入口 .tsx 里本地声明的函数名（函数声明 + 箭头/函数表达式赋给的变量）。 */
function locallyDeclaredFunctions(sourceFile) {
  const names = new Set();
  function visit(node) {
    if (ts.isFunctionDeclaration(node) && node.name) {
      names.add(node.name.text);
    }
    if (
      ts.isVariableDeclaration(node)
      && node.name && ts.isIdentifier(node.name)
      && node.initializer
      && (ts.isArrowFunction(node.initializer) || ts.isFunctionExpression(node.initializer))
    ) {
      names.add(node.name.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return names;
}


/** 整个 .tsx 里出现的调用目标名（只收裸标识符调用）。 */
function calledIdentifiers(sourceFile) {
  const names = new Set();
  function visit(node) {
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
      names.add(node.expression.text);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return names;
}


test("样板入口真的在用 search-panel-model，而不是自己内联一份分叉", async () => {
  // 这条钉的是 T3 评审登记的那个缺口：纯函数层有单测、组件层没有（完整组件级行为
  // 测试要能 mount .tsx，而这个包刻意不进默认测试树，登记给 T4）。中间地带是——
  // 至少证明**组件确实消费这个模块**。否则「把模型模块原样留着当摆设、在 .tsx 里
  // 内联一份分叉实现」会让上面那些纯函数用例全绿，而用户看到的是另一份代码。
  const modelSource = parseText(
    await readFile(path.join(PACKAGE_DIR, "search-panel-model.ts"), "utf8"),
    "search-panel-model.ts",
  );
  const entrySource = parseText(
    await readFile(path.join(PACKAGE_DIR, "workspace-plugin.tsx"), "utf8"),
    "workspace-plugin.tsx",
  );

  const modelFunctions = exportedModelFunctions(modelSource);
  // 非空性下限：模型模块被清空 / 判据挑不出东西时，下面两条都会对空集恒真。
  assert.ok(
    modelFunctions.size >= 8,
    `search-panel-model.ts 的导出函数只剩 ${modelFunctions.size} 个（判据失效？）`,
  );

  const importedNames = new Set(
    importedFromModel(entrySource).map((row) => row.imported),
  );
  const usedModelFunctions = [...calledIdentifiers(entrySource)]
    .filter((name) => modelFunctions.has(name));

  // ① 用到的每个模型函数都必须是从模型模块 import 进来的。
  for (const name of usedModelFunctions) {
    assert.ok(
      importedNames.has(name),
      `workspace-plugin.tsx 调用了模型函数 ${name}，却不是从 ./search-panel-model.ts import 的`,
    );
  }

  // ①' 反过来，模型导出的每个函数都必须真的被入口调用到。① 只挡「分叉还叫原来
  //     的名字」，改个名字（内联 formatAuthorsLocal 并删掉 import）就绕过去了——
  //     那时 used 只是少一个，光靠下限阈值看不出来。按**集合相等**判就没有缝：
  //     任何一个模型函数被分叉替换掉，它立刻变成没人用的导出而报红。
  //     代价是模型模块不得留只给测试用的导出函数；这是个三文件样板，那种死代码
  //     本来也该删，所以这条同时是它的看门人。
  assert.deepEqual(
    usedModelFunctions.sort(),
    [...modelFunctions].sort(),
    "search-panel-model.ts 的导出函数与 workspace-plugin.tsx 实际调用的那批对不上"
      + "（入口内联了一份分叉？还是模型里留了没人用的死导出？）",
  );

  // ② 入口不得本地声明一个与模型导出同名的函数——那正是「内联分叉」的形状，且
  //    改个名字就能绕过 ① 的那种变异也被这条挡住。
  for (const name of locallyDeclaredFunctions(entrySource)) {
    assert.ok(
      !modelFunctions.has(name),
      `workspace-plugin.tsx 本地声明了 ${name}，与 search-panel-model.ts 的导出同名（内联分叉？）`,
    );
  }
});


test("门禁接线：check_extended.sh 挂了样板 UI 守卫 lane", async () => {
  const content = await readFile(
    path.join(REPO_DIR, "scripts", "check_extended.sh"), "utf8",
  );
  // 判据是**真正的调用行**，不是随便哪里出现这个文件名的字样——脚本头顶那句
  // 解释性注释里本来就提到了 `check_sample_plugin.sh` 这个名字，只按子串判会把
  // 「删掉调用、留着注释」误判成 lane 还在(真实踩过一次的假阴性)。
  assert.match(
    content,
    /bash "\$ROOT_DIR\/scripts\/check_sample_plugin\.sh"/,
    "scripts/check_extended.sh 里没有真正调用 check_sample_plugin.sh（lane 没挂上或被删了，"
      + "光提到文件名的注释不算）",
  );
});


test("门禁接线：check_contracts.sh 给 check_ui_vocabulary.py 传了样板包的 --extra-root", async () => {
  const content = await readFile(
    path.join(REPO_DIR, "scripts", "check_contracts.sh"), "utf8",
  );
  // 判据是**那一次调用自己的参数里**含这个 --extra-root，不是「文件某处出现过这
  // 两个子串」（注释里各提一次就能骗过），也不是原先那条把 `\` 续行连同缩进一起
  // 钉死的正则——那等于把脚本的**排版**写进断言：把两行并成一行、或在中间插一个
  // 别的参数，守卫就假红，而接线其实一个字都没变。
  //
  // 于是从脚本路径起，只允许跨过「同一条逻辑命令」的字符——普通非换行字符，或
  // `\` 续行——直到读到那个 --extra-root。单行写法与续行写法都过，同一条命令里
  // 重排参数也过，而调用被删掉或参数被摘掉就一定不匹配（非空性由此自带：这条正则
  // 本身就要求那次调用存在）。裸换行不在允许集里，所以别处另一条命令的参数不会被
  // 误算进来。
  //
  // ⚠ 刻意不先把文件 split 成行再挑：`static-source-policy` 禁止对源文本用
  // split/slice/indexOf 这类按位置取材的方法（本次真红过一轮），整段正则才是这个
  // 仓库里对 .sh 断言的合法形状。
  assert.match(
    content,
    /check_ui_vocabulary\.py"(?:\\\n|[^\n])*--extra-root\s+"\$ROOT_DIR\/examples\/extensions\/arxiv-search\/src"/,
    "scripts/check_contracts.sh 调用 check_ui_vocabulary.py 时没带样板插件 src 目录的 --extra-root",
  );
});
