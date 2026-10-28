// 网页导出 / 导入的界面文案不说「向量 / embedding / chunk」(docs/ui-vocabulary.md:界面说
// 「检索数据」「段」)。`scripts/check_ui_vocabulary.py` 的黑名单只拦「向量检索索引」这类复合
// 词,不拦裸「向量」,所以这几个模块单独钉一条:它们写给用户的每一个字符串字面量(含模板串
// 的每一段)与 JSX 文本都不许出现这三个词。
//
// 唯一的例外是**引用**来的那一个按钮名:`CHECKUP_FIX.backfill_vectors`(「补齐向量」)是看板
// 「来源状态」里那颗修复按钮现在的字。导入后补全检索数据没完成时,文案要告诉用户去点哪颗
// 按钮——照实说出它的名字,用户才找得到;把它改写成别的词,就是指一颗不存在的按钮。它只经
// 导入 `CHECKUP_FIX` 进来(不抄一份),所以下面只放行这一个来源:拼好的那句话里去掉这个
// 按钮名之后,不许再有任何一个禁用词。
import test from "node:test";
import assert from "node:assert/strict";

import { jsxTextValues, parseModule, stringLiterals } from "../../test-support/semantic-source.mjs";
import { CHECKUP_FIX } from "../../app/vocabulary.ts";
import {
  REEMBED_NOTICE,
  REPAIR_HINT,
  importReportNotes,
  parseTransferJob,
  transferFailureText,
} from "../../app/notebook-transfer-api.ts";

const BANNED = /向量|embedding|chunk/i;
// 不上屏的内部标识:`notebook_reembed` 的腿名(与作业信息里的腿逐字比较,从不显示)。
const INTERNAL_IDENTIFIERS = new Set(["chunks"]);
const MODULES = [
  "notebook-transfer-api.ts",
  "notebook-transfer-phases.ts",
  "notebook-import-upload.ts",
  "use-notebook-transfer.ts",
  "notebook-transfer-panel.tsx",
  "pending-actions.ts",
  "pending-center.tsx",
];

test("导出/导入模块的字面量与 JSX 文本里没有向量 / embedding / chunk", async () => {
  const offenders = [];
  for (const name of MODULES) {
    const module = await parseModule(name);
    for (const value of stringLiterals(module)) {
      if (BANNED.test(value) && !INTERNAL_IDENTIFIERS.has(value)) offenders.push(`${name}: ${value}`);
    }
    for (const value of jsxTextValues(module)) {
      if (BANNED.test(value)) offenders.push(`${name}: ${value}`);
    }
  }
  assert.deepEqual(offenders, []);
});

test("拼出来的文案里,禁用词只来自引用的那颗按钮名", () => {
  const button = `「${CHECKUP_FIX.backfill_vectors}」`;
  assert.ok(REPAIR_HINT.includes(button), "补全提示要说出那颗按钮的真名");
  const failed = (code, over = {}) => parseTransferJob({
    id: "x", kind: "import", state: "failed", phase: "reembed", notebook_id: "nb", error: { code }, ...over,
  });
  const texts = [
    REEMBED_NOTICE,
    REPAIR_HINT,
    ...["import_followup_failed", "import_followup_interrupted", "import_reaped", "archive_too_new",
      "invalid_archive", "too_large", "future_code"].map((code) => transferFailureText(failed(code))),
    ...importReportNotes(parseTransferJob({
      id: "y", kind: "import", state: "succeeded", phase: "done", notebook_id: "nb",
      report: { sources_reset: 2, mount_edges_dropped: 1 }, reembed: { state: "failed" },
    })),
  ];
  for (const text of texts) {
    assert.ok(!BANNED.test(text.replaceAll(button, "")), text);
  }
});
