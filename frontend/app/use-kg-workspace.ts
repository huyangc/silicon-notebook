"use client";

import { useRef } from "react";
import type { KgWorkspaceOwner } from "./kg-workspace-model";
import { useKgGraph } from "./use-kg-graph";
import { useKgKnowledge } from "./use-kg-knowledge";
import { useKgOwner } from "./use-kg-owner";
import { useKgSchema } from "./use-kg-schema";
import type { NotebookSummary } from "./workspace-model";

// Thin composition layer over the KG workspace owner. The state itself lives
// in three independently testable domain owners — `use-kg-knowledge.ts`
// (rows / types / filter / paging / duplicates / context), `use-kg-schema.ts`
// (graph object-type registry view + mutations) and `use-kg-graph.ts`
// (unified graph, pending-merge review, durable build/relink/rebuild
// tracking) — over the one shared actor + notebook + generation gate in
// `use-kg-owner.ts`.
//
// The three domains never see each other. Everything that has to span them
// (clear on owner change, invalidate, adopt a freshly established owner) is
// routed here, from the authority's fan-out into each domain's own **named
// command**; no domain ever receives another domain's setter, and page.tsx
// still sees exactly one namespaced view (`.knowledge` / `.schema` /
// `.graph`) plus the flat command surface listed at the bottom of this file.
type KgWorkspacePolicy = {
  canGovernKnowledge: boolean;
  canManageNotebookSchemas: boolean;
  canManageGlobalSchemas: boolean;
  canWriteKg: boolean;
  externalBuildPolling: boolean;
};

type KgWorkspaceEffects = {
  notify: (message: string) => void;
  reportError: (error: unknown) => void;
  refreshCollection: (guard: () => boolean) => Promise<void>;
  refreshNotebook: (notebookId: string, guard: () => boolean) => Promise<NotebookSummary>;
  focusGraphNode: (nodeId: string) => void;
};

export type UseKgWorkspaceOptions = {
  actorId: string | null;
  notebookId: string | null;
  policy: KgWorkspacePolicy;
  effects: KgWorkspaceEffects;
};

type KgDomains = {
  knowledge: ReturnType<typeof useKgKnowledge>;
  schema: ReturnType<typeof useKgSchema>;
  graph: ReturnType<typeof useKgGraph>;
};

export type KgWorkspace = ReturnType<typeof useKgWorkspace>;

export function useKgWorkspace({
  actorId,
  notebookId,
  policy,
  effects,
}: UseKgWorkspaceOptions) {
  // The authority's fan-out has to reach hooks that do not exist yet at the
  // moment it is constructed, so it is routed through this ref, published at
  // the end of every render. Every fan-out callback fires from an effect or
  // an event handler — never during render — so the ref is always current by
  // the time one runs.
  const domainsRef = useRef<KgDomains | null>(null);

  const {
    authority,
    activateActor,
    beginNotebookTransition,
    finishNotebookTransition,
    leaveWorkspace,
  } = useKgOwner({
    actorId,
    notebookId,
    fanOut: {
      clearVisibleState: () => {
        const domains = domainsRef.current;
        if (!domains) return;
        domains.knowledge.clearVisibleState();
        domains.schema.clearVisibleState();
        domains.graph.clearVisibleState();
      },
      invalidate: () => {
        const domains = domainsRef.current;
        if (!domains) return;
        domains.knowledge.invalidate();
        domains.schema.invalidate();
        domains.graph.invalidate();
      },
      adoptOwner: (owner: KgWorkspaceOwner, notebookSnapshot: NotebookSummary | null) => {
        domainsRef.current?.graph.adoptOwner(owner, notebookSnapshot);
      },
    },
  });

  const knowledge = useKgKnowledge({ authority, policy, effects });
  const schema = useKgSchema({ authority, policy, effects });
  const graph = useKgGraph({ authority, policy, effects });
  domainsRef.current = { knowledge, schema, graph };

  return {
    knowledge: knowledge.view,
    schema: schema.view,
    graph: graph.view,
    activateActor,
    beginNotebookTransition,
    finishNotebookTransition,
    leaveWorkspace,
    enterKnowledge: knowledge.enterKnowledge,
    selectKnowledgeKind: knowledge.selectKnowledgeKind,
    selectKnowledgeStatus: knowledge.selectKnowledgeStatus,
    goToKnowledgePage: knowledge.goToKnowledgePage,
    refreshKnowledge: knowledge.refreshKnowledge,
    invalidateKnowledge: knowledge.invalidateKnowledge,
    updateKnowledge: knowledge.updateKnowledge,
    findDuplicates: knowledge.findDuplicates,
    mergeKnowledge: knowledge.mergeKnowledge,
    loadKnowledgeContext: knowledge.loadKnowledgeContext,
    openSchemas: schema.openSchemas,
    closeSchemas: schema.closeSchemas,
    selectSchemaView: schema.selectSchemaView,
    patchSchema: schema.patchSchema,
    createSchema: schema.createSchema,
    deleteSchema: schema.deleteSchema,
    induceSchemas: schema.induceSchemas,
    openGraph: graph.openGraph,
    closeGraph: graph.closeGraph,
    openAnalysis: graph.openAnalysis,
    closeAnalysis: graph.closeAnalysis,
    updateGraphSearch: graph.updateGraphSearch,
    changeRange: graph.changeRange,
    toggleType: graph.toggleType,
    clearTypes: graph.clearTypes,
    selectNode: graph.selectNode,
    reviewPendingMerges: graph.reviewPendingMerges,
    reviewAllMerges: graph.reviewAllMerges,
    decideMerge: graph.decideMerge,
    startRelink: graph.startRelink,
    startRebuild: graph.startRebuild,
    startKgBuild: graph.startKgBuild,
    observeNotebook: graph.observeNotebook,
    observeKgBuild: graph.observeKgBuild,
  };
}
