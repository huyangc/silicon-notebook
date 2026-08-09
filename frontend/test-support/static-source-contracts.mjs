export const STATIC_SOURCE_CONTRACTS = Object.freeze({
  architectureBoundaries: Object.freeze({
    category: "architecture",
    reason: "prevents component and model duplication across module boundaries",
    roots: ["page.tsx", "answer-panel.tsx", "workspace-model.ts", "kg-type-mark.tsx"],
  }),
  askModeVocabulary: Object.freeze({
    category: "protocol-vocabulary",
    reason: "enforces a repository-wide single source of Ask display names",
    roots: ["."],
  }),
  trustedErrors: Object.freeze({
    category: "security",
    reason: "prevents raw diagnostic text from reaching user-visible sinks",
    roots: ["."],
  }),
  rawEnumFallback: Object.freeze({
    category: "user-visible-vocabulary",
    reason: "prevents unlabelled backend enum values from rendering",
    roots: ["."],
  }),
});
