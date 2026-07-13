import type { AskModeId } from "./ask-modes";
import type { ReasoningTraceStep } from "./ask-stream";

/** API/view models shared by the workspace orchestrator and extracted panels. */
export type NotebookSummary = {
  id: string;
  name: string;
  purpose: string;
  primary_domain: string;
  status: string;
  counts: Record<string, number>;
  created_label: string;
  target_users?: string;
  expected_questions?: string[];
  source_types?: string[];
  taxonomy?: string[];
  access_scope?: string;
  tier?: string;
  kg_ready?: boolean;
  kg_building?: boolean;
  base_kg_available?: boolean;
  base_notebook_name?: string;
  kg_pending_sources?: number;
  access?: "owner" | "reader";
  shared_from?: string;
};

export type MemoryOrigin = "ask_answer" | "external_agent";
export type MemoryStatus = "candidate" | "confirmed" | "rejected" | "deprecated";
export type MemoryPromotionState = "none" | "proposed" | "approved" | "rejected";

export type MemoryRecord = {
  id: string;
  notebook_id: string;
  created_by: string;
  agent_profile_id?: string | null;
  source_answer_id?: string | null;
  origin: MemoryOrigin;
  status: MemoryStatus;
  promotion_state: MemoryPromotionState;
  title: string;
  content_md: string;
  tags: string[];
  confirmed_by?: string | null;
  confirmed_at?: string | null;
  embedding_status: string;
  embedding_error: string;
  created_at: string;
  updated_at: string;
  provenance: Record<string, unknown>;
};

export type PaginatedMemories = {
  items: MemoryRecord[];
  total_count: number;
  offset: number;
  limit: number;
};

export type MemoryPreview = {
  title: string;
  content_md: string;
  tags: string[];
  provenance_summary: Record<string, unknown>;
};

export type AgentProfile = {
  id: string;
  owner_id: string;
  name: string;
  description: string;
  status: "active" | "revoked";
  created_at: string;
  updated_at: string;
};

export type AgentTokenSummary = {
  id: string;
  agent_profile_id: string;
  profile_name: string;
  scopes: string[];
  default_notebook_id: string;
  notebook_ids: string[];
  expires_at?: string | null;
  revoked_at?: string | null;
  last_used_at?: string | null;
  created_at: string;
};

export type AgentTokenIssued = Omit<AgentTokenSummary, "profile_name" | "revoked_at" | "last_used_at"> & {
  token: string;
};

export type SourceSummary = {
  id: string;
  notebook_id: string;
  title: string;
  type: string;
  status: string;
  parse_status: string;
  summary: string;
  element_count: number;
  file_name: string;
  file_size: number;
  source_url?: string;
  created_label: string;
  error_message?: string;
  extraction_warning?: string | null;
  kg_extracted?: boolean;
};

export type PaginatedSources = {
  items: SourceSummary[];
  total_count: number;
  offset: number;
  limit: number;
};

export const SOURCES_PAGE_SIZE = 50;

export type PaginatedKnowledge = {
  items: KnowledgeRecord[];
  total_count: number;
  offset: number;
  limit: number;
};

export type SourceElement = {
  id: string;
  source_id: string;
  element_type: string;
  location_label: string;
  text: string;
  metadata: Record<string, unknown>;
};

export type SearchHit = {
  scope: string;
  notebook_id: string;
  label: string;
  text: string;
  source_id: string;
  element_id: string;
};

export type Health = { status: string; llm_configured: boolean };

export type Evidence = {
  source_id: string;
  source_title: string;
  location_label: string;
  quoted_span: string;
  element_id: string;
};

export type AnswerAnchor = {
  key: string;
  object_id: string;
  object_type: string;
  label: string;
  name: string;
  definition?: string | null;
  snippet?: string | null;
  source_title: string;
  location_label: string;
  tier?: string;
};

export type Citation = {
  label: string;
  source_id: string;
  element_id: string;
  location_label: string;
  quoted_span: string;
  tier?: string;
};

export type AskResponse = {
  answer_id: string;
  conversation_id: string;
  conclusion: string;
  answer: string;
  grounded: boolean;
  anchors: AnswerAnchor[];
  related_knowledge: KnowledgeRecord[];
  citations: Citation[];
  llm_mode: string;
  evidence_level?: "grounded" | "overview" | "inferred";
  retrieval_query?: string;
  top_relevance?: number;
  reasoning_trace?: ReasoningTraceStep[];
  mode?: AskModeId;
  model_errors?: { stage: string; model: string; message: string }[];
  index_required?: boolean;
};

export type ChatTurn = { question: string; response: AskResponse };
export type ConversationSummary = {
  id: string;
  title: string;
  updated_at: string;
  turn_count: number;
  used_reasoning?: boolean;
};
export type ConversationDetail = {
  id: string;
  notebook_id: string;
  title: string;
  updated_at: string;
  turn_count: number;
  turns: { answer_id: string; question: string; response: AskResponse; created_at: string }[];
  active_job?: { job_id: string; question: string; mode: string; trace: ReasoningTraceStep[] };
};

export type ChatMode = "ask" | "rules" | "reports" | "memory";
export const CHAT_MODES: Array<[ChatMode, string]> = [
  ["ask", "Ask"],
  ["rules", "Knowledge"],
  ["memory", "Memory"],
  ["reports", "Deep Report"],
];

export type KnowledgeKind = string;
export type KnowledgeFieldValue = { key: string; value: string };
export type KnowledgeTypeCount = { object_type: string; label: string; count: number };

export type ObjectSchema = {
  object_type: string;
  plural: string;
  fields: string[];
  primary: string;
  description: string;
  label: string;
  list_fields: string[];
  source: string;
  status: string;
  rationale: string;
  notebook_id: string;
};

export type KnowledgeRecord = {
  id: string;
  object_type: string;
  headline?: string;
  fields: KnowledgeFieldValue[];
  status: string;
  owner?: string;
  last_reviewed?: string;
  evidence: Evidence[];
};

export const KNOWLEDGE_STATUS_OPTIONS = [
  "reviewed",
  "approved",
  "deprecated",
  "conflict",
  "project_specific",
];

export type KnowledgeItem = {
  id: string;
  status: string;
  owner?: string;
  last_reviewed?: string;
  evidence: Evidence[];
  title?: string;
  statement?: string;
  applies_to?: string[];
  recommendation?: string;
  risk_if_ignored?: string;
  severity?: string;
  name?: string;
  use_when?: string;
  benefit?: string;
  limitation?: string;
  description?: string;
  term?: string;
  definition?: string;
  headline?: string;
  object_type?: string;
  fields?: KnowledgeFieldValue[];
};

export const EMPTY_KNOWLEDGE: Record<string, KnowledgeItem[] | null> = {};

export type KnowledgeRef = { id: string; object_type: string; headline: string; status: string };
export type DuplicateGroup = { object_type: string; similarity: number; members: KnowledgeRef[] };
export type NotebookAnalytics = {
  answers_total: number;
  feedback_useful: number;
  feedback_not_useful: number;
  usefulness_rate: number;
  low_rated_questions: string[];
  candidate_counts: Record<string, number>;
  knowledge_counts: Record<string, number>;
  source_status_counts: Record<string, number>;
};

export type KnowledgeNode = { id: string; object_type: string; headline: string; status: string };
export type KnowledgeEdge = { from_id: string; to_id: string; relation: string; label: string };
export type KnowledgeGraph = { nodes: KnowledgeNode[]; edges: KnowledgeEdge[] };
export type UnifiedConceptNode = {
  id: string;
  object_type: string;
  payload: { name?: string; [key: string]: unknown };
};
export type UnifiedEdge = {
  source_object_id: string;
  target_object_id: string;
  edge_type: string;
  support_count?: number;
  source_count?: number;
};
export type UnifiedGraphResp = {
  nodes: UnifiedConceptNode[];
  edges: UnifiedEdge[];
  total_nodes?: number;
  total_edges?: number;
  truncated?: boolean;
  viz_building?: boolean;
};
export type EvidenceItem = {
  source_id: string;
  source_title: string;
  element_id: string;
  element_type: string;
  location_label: string;
  quoted_span: string;
  confidence: number;
  element_text?: string;
};
export type KgObject = {
  id: string;
  object_type: string;
  payload: { name?: string; section_path?: string; [key: string]: unknown };
  evidence: EvidenceItem[];
  edge_type?: string;
};
export type ConceptDetailResp = {
  canonical_id: string;
  canonical_name: string;
  members: KgObject[];
  attached: KgObject[];
  evidence: EvidenceItem[];
};
export type KgOccurrence = {
  quoted_span?: string;
  source_title?: string;
  source_id?: string;
  element_text?: string;
  location_label?: string;
  element_type?: string;
  confidence?: number;
};
export type KgProcedureStep = { name: string; element_text: string };
export type NodeContext = {
  id: string;
  object_type: string;
  name: string;
  section_path: string;
  occurrences: KgOccurrence[];
  definition: string | null;
  steps: KgProcedureStep[] | null;
};
export type PendingMerge = { id: string; canonical_a: string; canonical_b: string; score: number; status: string };
export type UnifiedKgStatus = {
  dirty: boolean;
  last_rebuild_at: string;
  objects: number;
  relations: number;
  clusters: number;
  viz_indexed: boolean;
  viz_nodes: number;
  viz_edges: number;
  viz_stale: boolean;
  viz_building?: boolean;
};
export type MergeReviewSummary = { reviewed: number; confirmed: number; rejected: number; unsure: number };
export type MergeReviewJob = { status: string; total: number; done: number; error: string };
export type FgNode = {
  id: string;
  name: string;
  type: string;
  val: number;
  degree: number;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
};
export type FgLink = { source: string | FgNode; target: string | FgNode; label: string; sourceCount?: number };
export type KgSearchHit = { object_id: string; name: string; object_type: string; score: number; match: string };
export type KgSearchResp = { query: string; hits: KgSearchHit[] };
export type KgNeighborsResp = { nodes: UnifiedConceptNode[]; edges: UnifiedEdge[] };
