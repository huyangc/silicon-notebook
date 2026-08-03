export type SourceScopePayload = {
  mode: "include" | "exclude";
  source_ids: string[];
};

export type SourceScopeSelection = {
  /** true: ids are exclusions; false: ids are inclusions. */
  allSelected: boolean;
  ids: Set<string>;
};

export const defaultSourceScopeSelection = (): SourceScopeSelection => ({
  allSelected: true,
  ids: new Set<string>(),
});

export function sourceIsSelected(
  selection: SourceScopeSelection,
  sourceId: string,
): boolean {
  return selection.allSelected
    ? !selection.ids.has(sourceId)
    : selection.ids.has(sourceId);
}

export function toggleSourceSelection(
  selection: SourceScopeSelection,
  sourceId: string,
): SourceScopeSelection {
  const ids = new Set(selection.ids);
  if (ids.has(sourceId)) ids.delete(sourceId);
  else ids.add(sourceId);
  return { ...selection, ids };
}

export function selectedSourceCount(
  selection: SourceScopeSelection,
  total: number,
): number {
  return selection.allSelected
    ? Math.max(0, total - selection.ids.size)
    : Math.min(Math.max(0, total), selection.ids.size);
}

export function sourceScopePayload(
  selection: SourceScopeSelection,
  total: number,
): SourceScopePayload {
  if (selectedSourceCount(selection, total) === 0) {
    return { mode: "include", source_ids: [] };
  }
  return {
    mode: selection.allSelected ? "exclude" : "include",
    source_ids: Array.from(selection.ids),
  };
}

export function removeSourceFromSelection(
  selection: SourceScopeSelection,
  sourceId: string,
): SourceScopeSelection {
  if (!selection.ids.has(sourceId)) return selection;
  const ids = new Set(selection.ids);
  ids.delete(sourceId);
  return { ...selection, ids };
}

/** Return the owning notebook id only when a source comes from a mounted base. */
export function crossLibrarySourceNotebookId(
  sourceNotebookId: string,
  activeNotebookId: string | null,
): string {
  if (!activeNotebookId || !sourceNotebookId) return "";
  return sourceNotebookId === activeNotebookId ? "" : sourceNotebookId;
}
