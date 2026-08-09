import assert from "node:assert/strict";
import test from "node:test";

import {
  buildPublicReportLink,
  publicCitationRefs,
  publicReferenceNumber,
} from "../../app/public-report.ts";

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

test("显示编号取自 key 的序号，不受公开投影过滤挤位影响", () => {
  // 公开投影会丢掉既无标题又无摘录的条目，位置序号因此和正文标记对不上。
  const references = [
    { key: "k1", title: "甲", file_name: "", location: "", snippet: "" },
    { key: "k7", title: "乙", file_name: "", location: "", snippet: "" },
  ];
  assert.equal(publicReferenceNumber(references[0], 0), 1);
  assert.equal(publicReferenceNumber(references[1], 1), 7);
  // key 不是 kN 形状才退回位置序号。
  assert.equal(publicReferenceNumber({ key: "src-9" }, 4), 5);
  assert.equal(publicReferenceNumber(undefined, 0), 1);
});

test("正文标记映射与清单共用同一套编号，没有 key 的条目不入映射", () => {
  const refs = publicCitationRefs([
    { key: "k1", title: "甲", file_name: "", location: "", snippet: "" },
    { key: "", title: "无 key", file_name: "", location: "", snippet: "" },
    { key: "k7", title: "乙", file_name: "", location: "", snippet: "" },
  ]);
  assert.deepEqual(Object.keys(refs).sort(), ["k1", "k7"]);
  // 显示形态与站内答案/报告逐字一致。
  assert.equal(refs.k1.displayLabel, "[1]");
  assert.equal(refs.k7.displayLabel, "[7]");
  assert.deepEqual(publicCitationRefs(undefined), {});
});
