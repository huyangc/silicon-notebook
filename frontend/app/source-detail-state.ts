export function sourceElementDomId(elementId: string): string {
  return `source-element-${elementId.replace(/[^a-zA-Z0-9_-]/g, "_")}`;
}
