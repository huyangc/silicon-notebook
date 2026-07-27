import test from "node:test";
import assert from "node:assert/strict";

import {
  analysisArtifactAnomalies,
  analysisLedgerAnomalies,
  analysisSourceRowAnomalies,
  sourceAnomalies,
} from "./anomaly-severity.ts";

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

// —— 图谱分析报告的三个分档函数(kg-analysis-view spec §3.3)——————————————
//
// 为什么在这里直测而不是只在组件层断言:组件测试断的是**标签文字**
// (`getByText("来源已不存在")`),档位换了照样绿 —— 而档位就是这套分级的全部意义
// (红=数字不可信 / 黄=整理一下就好 / 灰=不是异常)。下面每条都断 `severity`。

test("产物比当前内容还新 → integrity(不是把「数字不可信」说成「整理一下就好」)", () => {
  // ⚠ 这条同时钉住**分支顺序**:fixture 里 stale 也是 true,所以把 ahead 分支挪到
  // stale 之后(或删掉它)都会退化成 retrieval/「落后于当前内容」—— 语义正好反转。
  // seq_behind 在 service 侧刻意不 clamp 到 0(kg_analysis.py 的 Freshness),
  // 负值 = 账本比库还新 = 库被手工改过,必须原样冒出来。
  const anomalies = analysisArtifactAnomalies({
    present: true,
    freshness: { seq_behind: -12, stale: true },
  });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "integrity");
  assert.equal(anomalies[0].label, "比当前内容还新");

  // stale 为 false 时同样是 integrity(不能因为「没标 stale」就当没事发生)。
  const alone = analysisArtifactAnomalies({
    present: true,
    freshness: { seq_behind: -1, stale: false },
  });
  assert.equal(alone.length, 1);
  assert.equal(alone[0].severity, "integrity");
  assert.equal(alone[0].label, "比当前内容还新");
});

test("产物落后当前若干次变更 → retrieval(数字没算错,只是描述更早的库状态)", () => {
  const anomalies = analysisArtifactAnomalies({
    present: true,
    freshness: { seq_behind: 28, stale: true },
  });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "retrieval");
  assert.equal(anomalies[0].label, "落后于当前内容");
});

test("产物在场且与当前一致 → 空数组", () => {
  assert.deepEqual(
    analysisArtifactAnomalies({ present: true, freshness: { seq_behind: 0, stale: false } }),
    [],
  );
  // 缺 freshness(seq_behind/stale 都取不到)也不能凭空造一条异常。
  assert.deepEqual(analysisArtifactAnomalies({ present: true }), []);
  assert.deepEqual(
    analysisArtifactAnomalies({ present: true, freshness: { seq_behind: null, stale: null } }),
    [],
  );
});

test("合法缺席(零板块的库不出来源画像)→ info,不是异常", () => {
  const anomalies = analysisArtifactAnomalies({ present: false, absence: "expected" });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "info");
  assert.equal(anomalies[0].label, "本次无需生成");
});

test("同一轮里少了一份产物 → integrity(账本非空却缺一份,同屏数字不可互相印证)", () => {
  const anomalies = analysisArtifactAnomalies({ present: false, absence: "unexpected" });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "integrity");
  assert.equal(anomalies[0].label, "本该有却缺失");
});

test("从没算过 → info(「没算过」不是「算出来是 0」,但也不是异常)", () => {
  for (const absence of ["never_computed", null, undefined]) {
    const anomalies = analysisArtifactAnomalies({ present: false, absence });
    assert.equal(anomalies.length, 1, `absence=${absence}`);
    assert.equal(anomalies[0].severity, "info", `absence=${absence}`);
    assert.equal(anomalies[0].label, "尚未生成", `absence=${absence}`);
  }
});

test("缺席优先于新鲜度:产物不在场时不看 seq_behind", () => {
  // 缺席的产物三个新鲜度字段本该是 null;万一后端送来一个负数,分档也必须先说
  // 「本该有却缺失」——把 present 判定挪到 freshness 之后会在这里报红。
  const anomalies = analysisArtifactAnomalies({
    present: false,
    absence: "unexpected",
    freshness: { seq_behind: -5, stale: true },
  });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].label, "本该有却缺失");
});

test("账本不是同一轮算的 → integrity(整份报告级别)", () => {
  assert.deepEqual(analysisLedgerAnomalies({ ledger_consistent: true }), []);
  const anomalies = analysisLedgerAnomalies({ ledger_consistent: false });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "integrity");
  assert.equal(anomalies[0].label, "数字口径不一致");
});

test("来源画像里指向已删来源的那一行 → info(下次整理会自己消失,不是数据坏了)", () => {
  assert.deepEqual(analysisSourceRowAnomalies({ source_missing: false }), []);
  const anomalies = analysisSourceRowAnomalies({ source_missing: true });
  assert.equal(anomalies.length, 1);
  // ⚠ 档位是这条断言的重点:改成 integrity 会让一屏几十行历史孤儿引用全变红字。
  assert.equal(anomalies[0].severity, "info");
  assert.equal(anomalies[0].label, "来源已不存在");
});
