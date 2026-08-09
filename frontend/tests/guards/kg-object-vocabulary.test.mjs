import test from "node:test";
import assert from "node:assert/strict";

import {
  KG_TYPE_LABELS,
  kgTypeLabel,
} from "../../app/kg-type-model.ts";
import { getTraceStepDetail } from "../../app/reasoning-trace.ts";
import { ANCHOR_SET_HINT } from "../../app/knowhow-manage-logic.ts";
import { UNINDEXED_SCOPE_HINT } from "../../app/scale-index.ts";
import {
  importsFrom,
  parseModule,
  stringLiterals,
} from "../../test-support/semantic-source.mjs";


const typeNouns = () => Object.values(KG_TYPE_LABELS)
  .map((label) => label.split(/\s+/)[0]);


test("内置类型确实多于一种,且各有各的名字", () => {
  const keys = Object.keys(KG_TYPE_LABELS);
  const nouns = typeNouns();
  assert.ok(keys.includes("concept"));
  assert.ok(keys.length >= 4);
  assert.equal(new Set(nouns).size, nouns.length);
  assert.ok(nouns.includes("概念"));
});


test("推理轨迹的对象计数用统称,不点名任一类型", () => {
  const detail = getTraceStepDetail({
    step_type: "answer",
    detail: { kg: 9, elements: 3 },
  });
  assert.match(detail, /9 个知识对象/);
  assert.match(detail, /3 段原文/);
  for (const noun of typeNouns()) {
    assert.equal(detail.includes(noun), false);
  }
});


test("knowhow 与未索引提示使用对象统称", () => {
  for (const hint of [ANCHOR_SET_HINT, UNINDEXED_SCOPE_HINT]) {
    assert.match(hint, /知识对象/);
    for (const noun of typeNouns()) {
      assert.equal(hint.includes(noun), false);
    }
  }
  assert.equal(ANCHOR_SET_HINT.includes("节点"), false);
});


test("引用提示使用对象统称", async () => {
  const answerPanel = await parseModule("answer-panel.tsx");
  const titles = stringLiterals(answerPanel)
    .filter((value) => value.includes("引用") && value.includes("绑定"));

  assert.equal(titles.length, 2);
  for (const title of titles) {
    assert.match(title, /知识对象/);
    for (const noun of typeNouns()) {
      assert.equal(title.includes(noun), false);
    }
  }
});


test("未索引说明只从 scale-index 常量进入 workspace", async () => {
  const page = await parseModule("page.tsx");
  assert.equal(
    importsFrom(page, "./scale-index")
      .some((item) => item.imported === "UNINDEXED_SCOPE_HINT"),
    true,
  );
  assert.equal(
    stringLiterals(page)
      .filter((value) => value.includes("未索引部分不参与"))
      .length,
    0,
  );
});


test("自定义类型原样透出且不命中原型链", () => {
  assert.equal(kgTypeLabel("custom_type"), "custom_type");
  assert.equal(kgTypeLabel("constructor"), "constructor");
  assert.equal(kgTypeLabel("concept"), "概念 Concept");
});
