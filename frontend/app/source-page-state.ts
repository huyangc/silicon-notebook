export function sourcePageRequestIsCurrent(
  requestId: number,
  latestRequestId: number,
  requestedNotebookId: string,
  activeNotebookId: string | null,
  workspaceGuardPassed: boolean,
): boolean {
  return requestId === latestRequestId
    && requestedNotebookId === activeNotebookId
    && workspaceGuardPassed;
}


export function clampSourcePage(
  requestedPage: number,
  totalCount: number,
  pageSize: number,
): number {
  if (pageSize <= 0) return 0;
  const lastPage = Math.max(0, Math.ceil(Math.max(0, totalCount) / pageSize) - 1);
  return Math.min(Math.max(0, requestedPage), lastPage);
}
