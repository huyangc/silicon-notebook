import { DEFAULT_NOTEBOOK_NAME } from "./workspace-model.ts";


export function defaultNotebookPayload() {
  return { name: DEFAULT_NOTEBOOK_NAME, purpose: "" };
}


export function namedNotebookPayload(name: string, purpose: string) {
  return {
    name: name.trim() || DEFAULT_NOTEBOOK_NAME,
    purpose: purpose.trim(),
  };
}


export function normalizedNotebookName(name: string): string {
  return name.trim() || DEFAULT_NOTEBOOK_NAME;
}
