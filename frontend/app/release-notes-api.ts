import { requestJson, requestVoid } from "./api-client.ts";
import type { ReleaseNoteLevel } from "./release-notes-model.ts";

export type ReleaseNotesBuild = { version: string; ordinal: number };
export type ReleaseNote = {
  id: string;
  ordinal: number;
  level: ReleaseNoteLevel;
  audience: "all" | "admin";
  title: string;
  /** markdown,可为空。 */
  body: string;
};
export type ReleaseNotesResponse = {
  available: boolean;
  build: ReleaseNotesBuild | null;
  /** 只含重点(新功能/变化),服务端已排好序、最多 5 条。 */
  notes: ReleaseNote[];
  /** 没放进 notes 的修复与改进条数,弹窗据此提示去更新记录页。 */
  more_count: number;
};
export type ReleaseNotesHistoryResponse = {
  available: boolean;
  build: ReleaseNotesBuild | null;
  /** 当前版本及以前对该用户可见的全部说明(含后台改进),新到旧。 */
  notes: ReleaseNote[];
};

export async function fetchReleaseNotes(): Promise<ReleaseNotesResponse> {
  return requestJson<ReleaseNotesResponse>("/me/release-notes", { tag: "release-notes" });
}

export async function fetchReleaseNotesHistory(): Promise<ReleaseNotesHistoryResponse> {
  return requestJson<ReleaseNotesHistoryResponse>("/me/release-notes/history", { tag: "release-notes" });
}

/** `throughOrdinal` 传 GET 返回的 build.ordinal，而不是「当前最新」，见后端契约。 */
export async function markReleaseNotesSeen(throughOrdinal: number): Promise<void> {
  await requestVoid("/me/release-notes/seen", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ through_ordinal: throughOrdinal }),
    tag: "release-notes",
  });
}
