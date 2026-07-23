import test from "node:test";
import assert from "node:assert/strict";

import { sourceAnomalies } from "./anomaly-severity.ts";

// 异常清单 → 档位映射表(anomaly-tiers spec 权威):覆盖每一档、优先级排序、
// 以及「都不命中」的空数组情形。

test("parse_status=failed → integrity,label/tooltip 固定文案", () => {
  const anomalies = sourceAnomalies({ parse_status: "failed" });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "integrity");
  assert.equal(anomalies[0].label, "解析失败");
  assert.equal(anomalies[0].tooltip, "这个来源没能解析成功，可删除后重新上传。");
});

test("parse_status 缺失时回退 status 字段(与 status-dot 现有渲染规则一致)", () => {
  const anomalies = sourceAnomalies({ parse_status: null, status: "failed" });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "integrity");
});

test("extraction_warning 有值 → retrieval,tooltip 用后端原文(不改写)", () => {
  const raw = "第 3-5 页存在扫描图片，OCR 未能完整识别";
  const anomalies = sourceAnomalies({ extraction_warning: raw });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "retrieval");
  assert.equal(anomalies[0].label, "部分内容未分析");
  assert.equal(anomalies[0].tooltip, raw, "tooltip 必须是后端原文，不能被前端改写");
});

test("paper_meta_status=missing → info(待补全，非 amber，是刻意的重新分类)", () => {
  const anomalies = sourceAnomalies({ paper_meta_status: "missing" });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "info");
  assert.equal(anomalies[0].label, "待补全");
  assert.equal(anomalies[0].tooltip, "论文作者/机构等信息尚未补全");
});

test("paper_meta_status=not_paper → info", () => {
  const anomalies = sourceAnomalies({ paper_meta_status: "not_paper" });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "info");
  assert.equal(anomalies[0].label, "非论文");
  assert.equal(anomalies[0].tooltip, "该来源非学术论文，无需补全");
});

test("都不命中 → 空数组", () => {
  assert.deepEqual(sourceAnomalies({}), []);
  assert.deepEqual(
    sourceAnomalies({
      parse_status: "parsed",
      status: "parsed",
      extraction_warning: null,
      paper_meta_status: "has_meta",
    }),
    [],
  );
});

test("paper_meta_status=has_meta 不产生异常(非 missing/not_paper 的其它取值同理)", () => {
  assert.deepEqual(sourceAnomalies({ paper_meta_status: "has_meta" }), []);
});

test("integrity 排序优先:parse_status=failed 与 extraction_warning 同时命中时 integrity 排最前", () => {
  const anomalies = sourceAnomalies({
    parse_status: "failed",
    extraction_warning: "部分抽取失败",
  });
  assert.equal(anomalies.length, 2);
  assert.equal(anomalies[0].severity, "integrity");
  assert.equal(anomalies[1].severity, "retrieval");
});

test("三档同时命中:integrity 排最前，info 排最后，同档内保持稳定顺序", () => {
  const anomalies = sourceAnomalies({
    parse_status: "failed",
    extraction_warning: "部分抽取失败",
    paper_meta_status: "missing",
  });
  assert.deepEqual(
    anomalies.map((a) => a.severity),
    ["integrity", "retrieval", "info"],
  );
});

test("extraction_warning 为空串时不算命中(与「精确空串」区分开:这里是完全没有值)", () => {
  assert.deepEqual(sourceAnomalies({ extraction_warning: "" }), []);
});
