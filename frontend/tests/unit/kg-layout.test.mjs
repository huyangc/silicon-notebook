import { test } from "node:test";
import assert from "node:assert/strict";
import {
  KG_BAND_STRENGTH,
  kgBandTarget,
  kgBandVelocity,
  kgTypeBandTargets,
} from "../../app/kg-layout.ts";

const W = 1000;
const H = 800;
const CENTER = [W / 2, H / 2];

// object_type 是用户可自定义的字符串,这些是 Object.prototype 上真实存在的键。
// 裸索引 targets[type] 会拿到继承成员(函数/对象),?? 不接管 → 坐标 undefined → NaN。
const PROTO_KEYS = ["constructor", "__proto__", "toString", "valueOf", "hasOwnProperty"];

test("kgTypeBandTargets: 四个内置类型都有有限锚点", () => {
  const targets = kgTypeBandTargets(W, H);
  assert.deepEqual(Object.keys(targets).sort(), ["claim", "concept", "formula", "procedure"]);
  for (const [type, xy] of Object.entries(targets)) {
    assert.equal(xy.length, 2, `${type} 锚点应是 [x, y]`);
    assert.ok(xy.every(Number.isFinite), `${type} 锚点必须全有限`);
  }
});

test("kgBandTarget: 内置类型取到自己的分带锚点", () => {
  const targets = kgTypeBandTargets(W, H);
  for (const type of ["concept", "claim", "formula", "procedure"]) {
    assert.deepEqual(kgBandTarget(targets, type, W, H, true), targets[type]);
  }
});

test("kgBandTarget: 原型链键回落中心坐标(回归 — 原来是 NaN)", () => {
  const targets = kgTypeBandTargets(W, H);
  for (const type of PROTO_KEYS) {
    const target = kgBandTarget(targets, type, W, H, true);
    assert.deepEqual(target, CENTER, `${type} 必须回落中心`);
    assert.ok(target.every(Number.isFinite), `${type} 坐标必须全有限`);
  }
});

test("kgBandTarget: 未知/自定义类型回落中心坐标", () => {
  const targets = kgTypeBandTargets(W, H);
  for (const type of ["evidence_tier", "", "工艺角", "0", "length"]) {
    const target = kgBandTarget(targets, type, W, H, true);
    assert.deepEqual(target, CENTER);
    assert.ok(target.every(Number.isFinite));
  }
});

test("kgBandTarget: 只选单一类型时(useTypeBands=false)全部收敛到中心", () => {
  const targets = kgTypeBandTargets(W, H);
  for (const type of ["concept", ...PROTO_KEYS, "evidence_tier"]) {
    assert.deepEqual(kgBandTarget(targets, type, W, H, false), CENTER);
  }
});

test("旧写法 targets[type] ?? center 确实产出 NaN(证明这个洞真实存在)", () => {
  const targets = kgTypeBandTargets(W, H);
  // ?? 只在 null/undefined 时接管;继承来的 constructor 是函数,truthy,直接漏过去。
  const naive = targets["constructor"] ?? CENTER;
  assert.equal(typeof naive, "function");
  assert.equal(naive[0], undefined);
  assert.ok(Number.isNaN((naive[0] - 0) * KG_BAND_STRENGTH * 1));
});

test("kgBandVelocity: 原型链/未知类型不产生 NaN,vx/vy 全有限", () => {
  const targets = kgTypeBandTargets(W, H);
  const nodes = [
    // 坐标/速度缺省(力学首帧)与已有值两种形态都覆盖
    { type: "concept", x: 10, y: 20, vx: 1, vy: -2 },
    ...PROTO_KEYS.map((type) => ({ type })),
    ...PROTO_KEYS.map((type) => ({ type, x: -5, y: 900, vx: 0.5, vy: 0.5 })),
    { type: "evidence_tier", x: 3, y: 4 },
  ];
  for (const node of nodes) {
    for (const useTypeBands of [true, false]) {
      const target = kgBandTarget(targets, node.type, W, H, useTypeBands);
      const next = kgBandVelocity(node, target, 0.7);
      assert.ok(
        Number.isFinite(next.vx) && Number.isFinite(next.vy),
        `${node.type} (useTypeBands=${useTypeBands}) 产生了非有限速度: ${JSON.stringify(next)}`,
      );
    }
  }
});

test("kgBandVelocity: 多步迭代后坐标仍有限(NaN 会自我传染,单步不够)", () => {
  const targets = kgTypeBandTargets(W, H);
  const nodes = [...PROTO_KEYS, "concept", "evidence_tier"].map((type) => ({
    type,
    x: 0,
    y: 0,
    vx: 0,
    vy: 0,
  }));
  for (let step = 0; step < 30; step += 1) {
    const alpha = 1 - step / 30;
    for (const node of nodes) {
      const target = kgBandTarget(targets, node.type, W, H, true);
      const next = kgBandVelocity(node, target, alpha);
      node.vx = next.vx;
      node.vy = next.vy;
      node.x += node.vx;
      node.y += node.vy;
    }
  }
  for (const node of nodes) {
    assert.ok(
      [node.x, node.y, node.vx, node.vy].every(Number.isFinite),
      `${node.type} 迭代后坐标被污染: ${JSON.stringify(node)}`,
    );
  }
});
