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

import { inspectPackage, validateManifest } from "../../scripts/sync-ui-plugins.mjs";
import {
  FIRST_PAGE_START,
  classifyImportReceipt,
  deselectPaper,
  foldImportedUrls,
  formatAuthors,
  mergeCatalog,
  nextPageStart,
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
const EXPECTED_CONTRIBUTION = Object.freeze({
  id: "examples.arxiv_search.panel",
  plugin_id: "examples.arxiv_search",
  version: "0.1.0",
  capability: "examples.arxiv_search.available",
  slot: "workspace.side_panel",
  permission: "source:write",
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


test("search-panel-model：导入回执三态——created / reused / rejected", () => {
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

  // 第二次导入同一批 URL（服务端仍把 a 报成 created——内容去重复用旧行）：
  // 本会话已经见过它，这次读出来是「已复用」。
  const secondReceipt = classifyImportReceipt(response, remembered);
  assert.deepEqual(secondReceipt.get("https://arxiv.org/pdf/a"), { status: "reused", title: "Paper A" });
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
  // 判据是 check_ui_vocabulary.py 那条调用**紧接着**一行 --extra-root（`\`
  // 续行——脚本里那次调用本来就是两行），不是「这两个子串各自在文件某处出现过」：
  // 光子串判据会被「调用没带参数，但别处的注释里恰好各提过一次这两个词」悄悄骗过。
  assert.match(
    content,
    /"\$ROOT_DIR\/scripts\/check_ui_vocabulary\.py" \\\n\s*--extra-root "\$ROOT_DIR\/examples\/extensions\/arxiv-search\/src"/,
    "scripts/check_contracts.sh 里 check_ui_vocabulary.py 没有紧跟着带样板插件 src 目录的 --extra-root",
  );
});
