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

test("大库无预览且没人在建时是第四态，而不是「没有匹配的节点」", () => {
  // 批 3·W4 T-W4-3：在线懒构建被规模闸删除后，后端如实回 viz_building:false +
  // viz_unavailable:true。此前这种响应会落进 empty 分支，界面说「清空搜索后可查看
  // 完整图谱」——清空搜索并不会让预览出现，那句话在这里永远兑现不了。
  const state = kgCanvasState(GRAPH({ viz_building: false, viz_unavailable: true }), false, 0);
  assert.equal(state, "unavailable");
});

test("第四态不因为节点数恰好非零而被跳过", () => {
  // 防回归：判据必须读 viz_unavailable，而不是「节点数为 0」的近似。
  const state = kgCanvasState(GRAPH({ viz_unavailable: true }), false, 7);
  assert.equal(state, "unavailable");
});

test("普通空图仍然是 empty，没被大库状态借走", () => {
  assert.equal(kgCanvasState(GRAPH(), false, 0), "empty");
  assert.equal(kgCanvasState(GRAPH({ viz_unavailable: false }), false, 0), "empty");
});

test("有可见节点时正常出图", () => {
  assert.equal(kgCanvasState(GRAPH({ total_nodes: 3 }), false, 3), "graph");
});
