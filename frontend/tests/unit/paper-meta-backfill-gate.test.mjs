import assert from "node:assert/strict";
import test from "node:test";

import { showPaperMetaBackfill } from "../../app/workspace-model.ts";
import { callsIn, importsFrom, parseModule } from "../../test-support/semantic-source.mjs";


test("补全任务进行中时按钮保持可见(承载「补全中…」)", () => {
  assert.equal(showPaperMetaBackfill({ paper_meta_missing: false }, [], true), true);
});

test("后端判定存在缺元数据源时显示", () => {
  assert.equal(showPaperMetaBackfill({ paper_meta_missing: true }, [], false), true);
});

test("未计算(列表投影/旧后端)保持旧行为显示——隐藏只能由显式 false 触发", () => {
  assert.equal(showPaperMetaBackfill({}, [], false), true);
  assert.equal(showPaperMetaBackfill({ paper_meta_missing: null }, [], false), true);
  assert.equal(showPaperMetaBackfill(null, [], false), true);
});

test("显式 false 且可见页无 missing 来源时隐藏", () => {
  const sources = [
    { paper_meta_status: "has_meta" },
    { paper_meta_status: "not_paper" },
    { paper_meta_status: null },
  ];
  assert.equal(showPaperMetaBackfill({ paper_meta_missing: false }, sources, false), false);
  assert.equal(showPaperMetaBackfill({ paper_meta_missing: false }, [], false), false);
});

test("单库快照陈旧但可见页已有 missing 来源时仍显示(取并集)", () => {
  const sources = [{ paper_meta_status: "has_meta" }, { paper_meta_status: "missing" }];
  assert.equal(showPaperMetaBackfill({ paper_meta_missing: false }, sources, false), true);
});

test("page.tsx 的按钮门真的消费共享判据(防止接线被移除)", async () => {
  const page = await parseModule("page.tsx");
  assert.ok(
    importsFrom(page, "./workspace-model").some((item) => item.imported === "showPaperMetaBackfill"),
    "page.tsx 必须从 workspace-model import showPaperMetaBackfill",
  );
  assert.ok(
    callsIn(page).includes("showPaperMetaBackfill"),
    "page.tsx 必须实际调用 showPaperMetaBackfill(按钮显示门)",
  );
});
