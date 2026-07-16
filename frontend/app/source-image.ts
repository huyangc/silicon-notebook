export function sourceImageAssetUrl(apiBase: string, notebookId: string, assetId: string): string {
  if (!assetId || !notebookId) return "";
  return `${apiBase}/notebooks/${notebookId}/assets/${assetId}`;
}
