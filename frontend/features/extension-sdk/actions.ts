import type { WorkspaceExtensionActions } from "./contracts.ts";


/**
 * Freeze the owner visible when a plugin action is published, then revalidate
 * it at invocation time. A detached A/G1 button cannot act on a later A/G3
 * workspace even if a browser or component retains its old callback.
 */
export function createOwnedWorkspaceExtensionActions<Owner>(
  owner: Owner | null,
  owns: (candidate: Owner) => boolean,
  openUnderstanding: () => void,
  refreshSources: () => Promise<void>,
): WorkspaceExtensionActions {
  return {
    openUnderstanding(): void {
      if (!owner || !owns(owner)) return;
      openUnderstanding();
    },
    // 双闸的第一道：这里只复核 extension owner（同一份 exact-owner freeze-then-
    // revalidate）。第二道闸在 `app/use-source-library.ts:277-279`
    // (`loadSourcesPage` 自己的 notebook 闸）——两道闸各自独立，都必须过。
    refreshSources(): Promise<void> {
      if (!owner || !owns(owner)) return Promise.resolve();
      return refreshSources();
    },
  };
}
