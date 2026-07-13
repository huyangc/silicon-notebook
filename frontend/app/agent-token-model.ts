export const AGENT_SCOPE_OPTIONS = [
  { value: "knowledge:read", label: "读取 Knowledge" },
  { value: "memory:read", label: "读取已确认 Memory" },
  { value: "memory:read_candidates", label: "读取待确认候选" },
  { value: "memory:propose", label: "主动提交候选 Memory" },
  { value: "ask:execute", label: "执行 Notebook Ask" },
] as const;

export type AgentScope = (typeof AGENT_SCOPE_OPTIONS)[number]["value"];

export type AgentTokenDraft = {
  default_notebook_id: string;
  notebook_ids: string[];
  scopes: string[];
  expires_at: string;
};

export function agentTokenDraft(defaultNotebookId = ""): AgentTokenDraft {
  return {
    default_notebook_id: defaultNotebookId,
    notebook_ids: defaultNotebookId ? [defaultNotebookId] : [],
    scopes: ["knowledge:read", "memory:read"],
    expires_at: "",
  };
}

export function agentTokenRequest(profileId: string, draft: AgentTokenDraft) {
  const notebookIds = Array.from(new Set([
    draft.default_notebook_id,
    ...draft.notebook_ids,
  ].filter(Boolean)));
  return {
    agent_profile_id: profileId,
    scopes: Array.from(new Set(draft.scopes)),
    default_notebook_id: draft.default_notebook_id,
    notebook_ids: notebookIds,
    expires_at: draft.expires_at || null,
  };
}

export function canIssueAgentToken(profileId: string, draft: AgentTokenDraft): boolean {
  return Boolean(
    profileId
    && draft.default_notebook_id
    && draft.notebook_ids.includes(draft.default_notebook_id)
    && draft.scopes.length
    && draft.expires_at,
  );
}
