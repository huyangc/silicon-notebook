// 守卫:图谱对象的**统称**不得降格成其中某一种类型。
//
// 背景:图谱是对象级的——内置 concept / claim / formula / procedure 四型,外加
// knowhow 表以列名生成的自定义 object_type。「概念」只是四型中**一型**的界面名
// (`概念 Concept`)。散文重写时曾把候选计数、引用锚点、入图提示统统写成「概念」,
// 于是 Claim / Formula / Procedure / 自定义类型在界面上集体消失。
//
// 本文件不去数「有几种类型」(那只会把 kg-type-mark 的表抄一遍,恒真),而是钉住
// 一条真不变量:**统称串里不许出现任何一个具体类型名**。谁再把统称改回「概念」
// (或改成「论断」「公式」「过程」),这里就红。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { getTraceStepDetail } from "./reasoning-trace.ts";
import { ANCHOR_SET_HINT } from "./knowhow-manage-logic.ts";
import { UNINDEXED_SCOPE_HINT } from "./scale-index.ts";

const read = (name) => readFile(new URL(`./${name}`, import.meta.url), "utf8");

// kg-type-mark.tsx 带 JSX,node 的类型剥离 import 不了 → 按源码解析它的内置表。
// 这张表本身已由 scripts/check_object_type_labels_contract.py 钉死 ≡ 后端
// OBJECT_TYPE_LABELS,所以从这里读到的类型名就是后端真源的类型名。
const kgTypeMark = await read("kg-type-mark.tsx");
const answerPanel = await read("answer-panel.tsx");
const pageTsx = await read("page.tsx");

function builtinTypeLabels() {
  const block = kgTypeMark.match(/const KG_TYPE_LABELS: Record<string, string> = \{([\s\S]*?)\};/);
  assert.ok(block, "KG_TYPE_LABELS 没找到——内置类型表被改名或换了写法");
  const labels = [...block[1].matchAll(/^\s*([A-Za-z_$][\w$]*)\s*:\s*"([^"\\]*)"\s*,?\s*$/gm)]
    .map(([, key, label]) => ({ key, label }));
  assert.ok(labels.length >= 2, "内置类型表解析出的条目太少——正则与写法失配了");
  return labels;
}

/** 「概念 Concept」→「概念」:类型名的中文部分,也就是散文里最容易误用的那个词。 */
const typeNouns = () => builtinTypeLabels().map(({ label }) => label.split(/\s+/)[0]);

test("内置类型确实多于一种,且各有各的名字", () => {
  // 前提校验:如果哪天真的塌成一型,下面「统称不许等于类型名」就没有意义了,
  // 该重新设计词汇而不是让这组断言空转。
  const labels = builtinTypeLabels();
  const keys = labels.map((l) => l.key);
  const nouns = typeNouns();
  assert.ok(keys.includes("concept"), `内置类型里没有 concept:${keys.join(",")}`);
  assert.ok(keys.length >= 4, `内置类型只剩 ${keys.length} 种,词汇设计需重审`);
  assert.equal(new Set(nouns).size, nouns.length, `类型名有重复:${nouns.join(",")}`);
  // 「概念」必须只是其中一个类型名——它就是本文件要防的那个降格目标。
  assert.ok(nouns.includes("概念"), "concept 的界面名不再含「概念」,本守卫的前提变了");
});

test("推理轨迹的对象计数用统称,不点名任一类型", () => {
  const detail = getTraceStepDetail({ step_type: "answer", detail: { kg: 9, elements: 3 } });
  assert.match(detail, /9 个知识对象/);
  assert.match(detail, /3 段原文/);
  for (const noun of typeNouns()) {
    assert.equal(detail.includes(noun), false, `计数文案点名了具体类型「${noun}」:${detail}`);
  }
});

test("knowhow 行标题列提示用统称,不点名任一类型", () => {
  // 每行进图谱后的 object_type 取自用户自定义的列名,连内置四型都不是,
  // 更不该写成「概念」——何况 knowhow 界面里「概念」另指 anchor 分组。
  assert.match(ANCHOR_SET_HINT, /知识对象/);
  for (const noun of typeNouns()) {
    assert.equal(ANCHOR_SET_HINT.includes(noun), false, `提示语点名了具体类型「${noun}」`);
  }
  assert.equal(ANCHOR_SET_HINT.includes("节点"), false, "行标题列提示回退到了图论内部词「节点」");
});

test("引用锚点的两条提示用统称,不点名任一类型", () => {
  // 引用锚的是 object_id,其类型可以是四型之一或自定义类型。
  const titles = [...answerPanel.matchAll(/"([^"\n]*(?:知识对象|概念|论断|公式|过程)[^"\n]*)"/g)]
    .map(([, s]) => s)
    .filter((s) => s.includes("引用"));
  assert.ok(titles.length >= 2, `没抓到引用相关的提示文案(抓到 ${titles.length} 条)`);
  for (const title of titles) {
    assert.match(title, /知识对象/);
    for (const noun of typeNouns()) {
      assert.equal(title.includes(noun), false, `引用提示点名了具体类型「${noun}」:${title}`);
    }
  }
});

test("未索引徽章的说明用统称,不点名任一类型", () => {
  // 未索引的来源什么都不参与:段落、图谱对象、关联全在内。列举里那一项若写成
  // 「概念」,用户会以为 claim / formula / procedure / knowhow 自定义类型不受影响。
  assert.match(UNINDEXED_SCOPE_HINT, /知识对象/);
  for (const noun of typeNouns()) {
    assert.equal(
      UNINDEXED_SCOPE_HINT.includes(noun),
      false,
      `未索引提示点名了具体类型「${noun}」:${UNINDEXED_SCOPE_HINT}`,
    );
  }
});

test("未索引说明只有一份字面量 —— 复制两份正是它漂移的原因", () => {
  // 上面那条断言只看常量的内容。它一度**假绿**:page.tsx 里另有两份手写副本,
  // 常量改对了、副本没改,守卫却全程满意。所以这里额外钉住「单一真源」——
  // page.tsx 只准引用常量,不准把这句话再抄回 JSX 里。
  const inlineCopies = pageTsx.match(/未索引部分不参与/g) ?? [];
  assert.equal(
    inlineCopies.length,
    0,
    `page.tsx 里又出现了 ${inlineCopies.length} 份手写副本;请改引用 scale-index.ts 的 UNINDEXED_SCOPE_HINT`,
  );
  const refs = pageTsx.match(/UNINDEXED_SCOPE_HINT/g) ?? [];
  // import 一次 + 两处徽章 = 3。少于 3 说明某处渲染点被摘掉或换回了字面量。
  assert.ok(refs.length >= 3, `page.tsx 只引用了 ${refs.length} 次 UNINDEXED_SCOPE_HINT,渲染点丢了`);
});

test("自定义类型原样透出,不被折叠进内置表", () => {
  // knowhow 列名生成的 object_type 没有内置译名,kgTypeLabel 诚实回落到原串
  // (刻意为之:用户自己起的名字,编不出更好的)。若哪天改成回落到某个内置类型名,
  // 自定义类型就会在界面上伪装成 Concept。
  assert.match(
    kgTypeMark,
    /return Object\.hasOwn\(KG_TYPE_LABELS, type\) \? KG_TYPE_LABELS\[type\] : type;/,
  );
});
