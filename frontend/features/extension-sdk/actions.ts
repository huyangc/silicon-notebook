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
): WorkspaceExtensionActions {
  return {
    openUnderstanding(): void {
      if (!owner || !owns(owner)) return;
      openUnderstanding();
    },
  };
}
