import test from "node:test";
import assert from "node:assert/strict";

import { kgCanvasState } from "../../app/kg-workspace-model.ts";

const GRAPH = (extra = {}) => ({ nodes: [], edges: [], ...extra });

test("还没拿到响应时是加载态", () => {
  assert.equal(kgCanvasState(null, false, 0), "loading");
  // 即使别的信号都在，没有响应就还不能下结论。
  assert.equal(kgCanvasState(null, true, 12), "loading");
});

test("后端说在建时是构建态", () => {
  assert.equal(kgCanvasState(GRAPH({ viz_building: true }), true, 0), "building");
});

test("两个标志同时为真时构建态压过不可用态", () => {
  // 批 3·W4 T-W4-3b：后端把两者造成互斥，所以这是防御性排序而不是常态。评审实测
  // 把 building/unavailable 两行对调后既有用例全绿——这条就是杀那个移动变异的钉子：
  // 「正在建」有信息量且会自己了结，「不可用」是个死态，同时为真时必须先说前者。
  assert.equal(
    kgCanvasState(GRAPH({ viz_building: true, viz_unavailable: true }), true, 0),
    "building",
  );
});

test("大库无预览且没人在建时是第四态，而不是「没有匹配的节点」", () => {
  // 批 3·W4 T-W4-3：在线懒构建被规模闸删除后，后端如实回 viz_building:false +
  // viz_unavailable:true。此前这种响应会落进 empty 分支，界面说「清空搜索后可查看
  // 完整图谱」——清空搜索并不会让预览出现，那句话在这里永远兑现不了。
  const state = kgCanvasState(GRAPH({ viz_building: false, viz_unavailable: true }), false, 0);
  assert.equal(state, "unavailable");
});

test("深链叠加出真实邻域时出图，不被第四态卡片盖住", () => {
  // 批 3·W4 T-W4-3b 顺修 2：原本这里断言的是 "unavailable"——只要后端说没有折叠图
  // 产物，画布就渲染那张卡片，哪怕 base 挂载 + 引用深链已经叠加出了真实的一跳邻域。
  // 那些节点不依赖折叠图产物（kg_neighbors 的邻域读是另一条路径），把它们盖掉是在
  // 说谎。裁决改为：有可见节点就出图，`unavailable` 只在零可见节点时生效。
  const state = kgCanvasState(GRAPH({ viz_unavailable: true }), false, 7);
  assert.equal(state, "graph");
});

test("零可见节点时第四态仍然生效", () => {
  // 上一条的另一半：判据是「零可见节点 且 viz_unavailable」，不是把第四态删掉。
  assert.equal(kgCanvasState(GRAPH({ viz_unavailable: true }), false, 0), "unavailable");
});

test("普通空图仍然是 empty，没被大库状态借走", () => {
  assert.equal(kgCanvasState(GRAPH(), false, 0), "empty");
  assert.equal(kgCanvasState(GRAPH({ viz_unavailable: false }), false, 0), "empty");
});

test("有可见节点时正常出图", () => {
  assert.equal(kgCanvasState(GRAPH({ total_nodes: 3 }), false, 3), "graph");
});
