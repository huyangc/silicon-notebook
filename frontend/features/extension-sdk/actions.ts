import type {
  WorkspaceExtensionActions,
  WorkspaceExtensionDialogCloseReason,
} from "./contracts.ts";


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
  openDialog: (contributionId: string) => void,
  closeDialog: (contributionId: string, reason: WorkspaceExtensionDialogCloseReason) => void,
): WorkspaceExtensionActions {
  return {
    openUnderstanding(): void {
      if (!owner || !owns(owner)) return;
      openUnderstanding();
    },
    // 弹窗的两条命令走同一道 owner 闸。`openDialog` 过闸后仍可能被 root-dialog 裁决
    // 拒绝——那是第二道闸，在壳层里（协调器的 open 返回 null 就不记持有者）。
    openDialog(contributionId: string): void {
      if (!owner || !owns(owner)) return;
      openDialog(contributionId);
    },
    // 关闭同样过闸，而且**同样按 contribution id 绑定**（codex #578 R1 P2）：owner 闸
    // 只挡得住「换代之后的旧回调」，挡不住「同一代里那一格已经易主」——一条 contribution
    // 留着旧的 `closeDialog()`（setTimeout、迟到的请求回调、没卸载干净的组件），此刻
    // holder 已是另一条时，那次调用会关掉别人的弹窗。持有者判据在壳层（它才知道 holder
    // 是谁），这里负责的是把身份原样带过去，让壳层判得成。
    closeDialog(contributionId: string, reason: WorkspaceExtensionDialogCloseReason = "button"): void {
      if (!owner || !owns(owner)) return;
      closeDialog(contributionId, reason);
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
