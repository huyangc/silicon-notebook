import type {
  KgNeighborsResp,
  UnifiedConceptNode,
  UnifiedEdge,
  UnifiedGraphResp,
} from "./workspace-model.ts";

export type KgFocusPlan = {
  focusId: string | null;
  contextObjectId: string | null;
  expandedNodes: UnifiedConceptNode[];
  expandedEdges: UnifiedEdge[];
};

function edgeKey(edge: UnifiedEdge): string {
  return `${edge.source_object_id}→${edge.target_object_id}→${edge.edge_type}`;
}

/**
 * Prepare the targeted overlay used when an Ask citation opens the bounded KG view.
 *
 * The core graph contains only high-degree nodes. A cited node may therefore be
 * absent even though it exists in the notebook. The neighbors endpoint returns a
 * bounded one-hop overlay and, for clustered concepts, the canonical graph id that
 * replaces the raw knowledge-object id carried by the citation.
 */
export function prepareKgFocus(
  core: UnifiedGraphResp,
  targetNodeId?: string,
  neighborhood?: KgNeighborsResp | null,
): KgFocusPlan {
  if (!targetNodeId) {
    return { focusId: null, contextObjectId: null, expandedNodes: [], expandedEdges: [] };
  }

  const coreNodeIds = new Set(core.nodes.map((node) => node.id));
  const coreEdgeKeys = new Set(core.edges.map(edgeKey));
  const resolvedFocusId = neighborhood?.focus_id || targetNodeId;
  const neighborhoodNodeIds = new Set((neighborhood?.nodes ?? []).map((node) => node.id));
  const canFocus = !neighborhood?.locating_unavailable
    && (coreNodeIds.has(resolvedFocusId) || neighborhoodNodeIds.has(resolvedFocusId));
  return {
    focusId: canFocus ? resolvedFocusId : null,
    contextObjectId: canFocus
      ? (neighborhood?.focus_object_id || targetNodeId)
      : null,
    expandedNodes: (neighborhood?.nodes ?? []).filter((node) => !coreNodeIds.has(node.id)),
    expandedEdges: (neighborhood?.edges ?? []).filter((edge) => !coreEdgeKeys.has(edgeKey(edge))),
  };
}
