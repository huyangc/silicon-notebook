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
  workspaceUiExtensionParity: Object.freeze({
    category: "cross-language-protocol",
    reason: "keeps build-time UI contributions identical to the frozen backend declaration contract",
    roots: ["features/extension-sdk/registry.ts", "../backend/tests/fixtures/ui_extension_contract.json"],
  }),
  workspaceUiPluginPackaging: Object.freeze({
    category: "supply-chain",
    reason:
      "keeps out-of-tree UI plugin packages inside the import allowlist and the generated"
      + " artifacts out of version control",
    roots: [
      "features/ext-*",
      "features/extension-sdk/registry.local.ts",
      "package.json",
      "../.gitignore",
    ],
  }),
});
