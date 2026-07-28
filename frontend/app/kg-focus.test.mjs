import test from "node:test";
import assert from "node:assert/strict";

import { prepareKgFocus } from "./kg-focus.ts";

test("引用目标不在核心图时，把定向邻域叠加进视图并定位规范节点", () => {
  const plan = prepareKgFocus(
    {
      nodes: [{ id: "hub", object_type: "concept", payload: { name: "Hub" } }],
      edges: [],
      truncated: true,
    },
    "ko-raw-concept",
    {
      focus_id: "K-canonical-concept",
      nodes: [
        { id: "K-canonical-concept", object_type: "concept", payload: { name: "Target" } },
        { id: "hub", object_type: "concept", payload: { name: "Hub" } },
      ],
      edges: [
        {
          source_object_id: "K-canonical-concept",
          target_object_id: "hub",
          edge_type: "related",
        },
      ],
    },
  );

  assert.equal(plan.focusId, "K-canonical-concept");
  assert.deepEqual(plan.expandedNodes.map((node) => node.id), ["K-canonical-concept"]);
  assert.equal(plan.expandedEdges.length, 1);
});

test("目标已在核心图时不重复叠加节点或边", () => {
  const coreEdge = {
    source_object_id: "target",
    target_object_id: "hub",
    edge_type: "uses",
  };
  const plan = prepareKgFocus(
    {
      nodes: [
        { id: "target", object_type: "claim", payload: { name: "Target" } },
        { id: "hub", object_type: "concept", payload: { name: "Hub" } },
      ],
      edges: [coreEdge],
    },
    "target",
    {
      focus_id: "target",
      nodes: [{ id: "target", object_type: "claim", payload: { name: "Target" } }],
      edges: [coreEdge],
    },
  );

  assert.equal(plan.focusId, "target");
  assert.deepEqual(plan.expandedNodes, []);
  assert.deepEqual(plan.expandedEdges, []);
});
