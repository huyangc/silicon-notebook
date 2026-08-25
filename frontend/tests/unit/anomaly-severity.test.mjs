import test from "node:test";
import assert from "node:assert/strict";

import {
  analysisArtifactAnomalies,
  analysisLedgerAnomalies,
  analysisSourceRowAnomalies,
  sourceAnomalies,
} from "../../app/anomaly-severity.ts";

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

test("MinerU 降级到本地解析器 → retrieval，并提示可重新解析或删除", () => {
  const anomalies = sourceAnomalies({ parse_quality_warning: true });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "retrieval");
  assert.equal(anomalies[0].label, "降级解析");
  assert.match(anomalies[0].tooltip, /版面、公式、表格或扫描内容可能不完整/);
  assert.match(anomalies[0].tooltip, /可重新解析，不满意可删除来源/);
  assert.ok(
    !anomalies[0].tooltip.includes("PDF"),
    "警告覆盖 docx/pptx/xlsx 降级，文案不得写死 PDF",
  );
});

test("索引管线分块回退内建 → retrieval(降级整理),与降级解析并存不互斥", () => {
  const anomalies = sourceAnomalies({ indexing_chunk_fallback: true });
  assert.equal(anomalies.length, 1);
  assert.equal(anomalies[0].severity, "retrieval");
  assert.equal(anomalies[0].label, "降级整理");
  assert.match(anomalies[0].tooltip, /已改用内建方式/);
  assert.match(anomalies[0].tooltip, /可重新解析重试/);
  const both = sourceAnomalies({
    parse_quality_warning: true,
    indexing_chunk_fallback: true,
  });
  assert.deepEqual(both.map((a) => a.label), ["降级解析", "降级整理"]);
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
      parse_quality_warning: false,
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

  // ⚠ 两条世代线各自都要能**单独**触发 integrity。产物同时盖 kg_mutation_seq 与
  // cluster_mutation_seq 两个戳;只判其中一条,另一条的损坏就落进 stale 分支 ——
  // 而 stale 的文案是「重新合并一次即可对齐」,等于把「数字不可信」说成「整理一下就好」。
  // 这个洞真实发生过:第 5 轮加簇世代时只加了响应字段、没接进分档,而上面那两条断言
  // 全绿,因为它们只覆盖 seq_behind(codex 第 6 轮评审)。将来再加第三条世代线同理。
  const clusterOnly = analysisArtifactAnomalies({
    present: true,
    freshness: { seq_behind: 0, cluster_seq_behind: -3, stale: true },
  });
  assert.equal(clusterOnly.length, 1);
  assert.equal(clusterOnly[0].severity, "integrity");
  assert.equal(clusterOnly[0].label, "比当前内容还新");

  // 反向:簇世代正常时不得被这条新分支误升档,仍按 stale 走 retrieval。
  const clusterFine = analysisArtifactAnomalies({
    present: true,
    freshness: { seq_behind: 28, cluster_seq_behind: 3, stale: true },
  });
  assert.equal(clusterFine.length, 1);
  assert.equal(clusterFine[0].severity, "retrieval");
  assert.equal(clusterFine[0].label, "落后于当前内容");
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
  assert.deepEqual(analysisArtifactAnomalies({ present: true, freshness: {} }), []);
  // ⚠ 主题板块那一块传的是 present:true + 板块表自己的新鲜度,而「从没建过板块」时
  // 那份 freshness 三个字段全是 null —— 那一档由块内空态文案解释,不许挂异常小字。
  // 它与下一条(建过、但说不出建在哪次合并上)的差别只在 built_at_seq。
  assert.deepEqual(
    analysisArtifactAnomalies({
      present: true,
      freshness: { built_at_seq: null, seq_behind: null, stale: null },
    }),
    [],
  );
});

test("产物在场却报不出合并世代 → retrieval,不是「无异常」", () => {
  // codex 第 7 轮 P2:依赖主题板块的两份数据由「只补账本」那条路径产出时,板块划分
  // 沿用库里现成的(建在哪一次合并上没有地方记),而边与来源映射按当前合并结果现算
  // —— 后端因此给 stale=null。这一档在这里必须**出小字**:`stale` 是三值的,而
  // `if (stale)` 对 null 为假,所以少了这条分支,一份拼了两个时候的数据会一个提示都
  // 不带地显示成正常 —— 后端刚修掉的那句谎话在前端原地复发。
  const unknown = analysisArtifactAnomalies({
    present: true,
    freshness: { built_at_seq: 128, seq_behind: 0, cluster_seq_behind: null, stale: null },
  });
  assert.equal(unknown.length, 1);
  assert.equal(unknown[0].severity, "retrieval");
  assert.equal(unknown[0].label, "对不上合并进度");

  // 缺席那一档仍按 absence 走,不会被这条新分支抢走(缺席在函数开头就返回了)。
  const absent = analysisArtifactAnomalies({
    present: false, absence: "expected",
    freshness: { seq_behind: null, stale: null },
  });
  assert.equal(absent.length, 1);
  assert.equal(absent[0].label, "本次无需生成");

  // 明确落后优先:stale=true 时报「落后于当前内容」,不被这条新分支降格。
  const stale = analysisArtifactAnomalies({
    present: true,
    freshness: { seq_behind: 28, stale: true },
  });
  assert.equal(stale[0].label, "落后于当前内容");
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

test("来源画像里指向已删来源的那一行 → info(不是数据坏了,但也不会自己消失)", () => {
  assert.deepEqual(analysisSourceRowAnomalies({ source_missing: false }), []);
  const anomalies = analysisSourceRowAnomalies({ source_missing: true });
  assert.equal(anomalies.length, 1);
  // ⚠ 档位是这条断言的重点:改成 integrity 会让一屏几十行历史孤儿引用全变红字。
  assert.equal(anomalies[0].severity, "info");
  assert.equal(anomalies[0].label, "来源已不存在");
  // ⚠ codex 第 9 轮 P2:文案**不得**承诺「重新整理后这一行会消失」——做不到。后端那次
  // 扫描是**刻意**保留孤儿 source_id 引用的(不写成 `JOIN sources`),重建板块也不会
  // 删掉底层知识对象,所以只要那些对象还在,这一行每次重算都照样出现。
  assert.ok(
    anomalies[0].tooltip.includes("知识对象还在"),
    `提示必须说清对象还在库里:${anomalies[0].tooltip}`,
  );
  assert.ok(
    !anomalies[0].tooltip.includes("消失"),
    `提示不得承诺这一行会自己消失:${anomalies[0].tooltip}`,
  );
});
