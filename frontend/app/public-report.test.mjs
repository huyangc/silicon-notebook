import assert from "node:assert/strict";
import test from "node:test";

import { buildPublicReportLink, publicReferencesByKey } from "./public-report.ts";

test("分享链接只带 token，不暴露 notebook / report id", () => {
  assert.equal(
    buildPublicReportLink("rshr-abc", "https://host.example"),
    "https://host.example/r/rshr-abc",
  );
  // 末尾斜杠不重复；token 做 URL 编码。
  assert.equal(
    buildPublicReportLink("a/b?c", "https://host.example/"),
    "https://host.example/r/a%2Fb%3Fc",
  );
  assert.equal(buildPublicReportLink("", "https://host.example"), "");
  assert.equal(buildPublicReportLink("   ", "https://host.example"), "");
});

test("引用按 key 建索引，跳过没有 key 的条目", () => {
  const byKey = publicReferencesByKey([
    { key: "k1", title: "甲", file_name: "", location: "", snippet: "" },
    { key: "", title: "无 key", file_name: "", location: "", snippet: "" },
    { key: "k2", title: "乙", file_name: "", location: "", snippet: "" },
  ]);
  assert.deepEqual(Object.keys(byKey).sort(), ["k1", "k2"]);
  assert.equal(byKey.k1.title, "甲");
  assert.deepEqual(publicReferencesByKey(undefined), {});
});
