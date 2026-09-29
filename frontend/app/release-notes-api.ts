import { requestJson, requestVoid } from "./api-client.ts";

export type ReleaseNotesBuild = { version: string; ordinal: number };
export type ReleaseNote = { id: string; ordinal: number; body: string };
export type ReleaseNotesResponse = {
  available: boolean;
  build: ReleaseNotesBuild | null;
  /** 服务端已按新到旧排好序。 */
  notes: ReleaseNote[];
};

export async function fetchReleaseNotes(): Promise<ReleaseNotesResponse> {
  return requestJson<ReleaseNotesResponse>("/me/release-notes", { tag: "release-notes" });
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
