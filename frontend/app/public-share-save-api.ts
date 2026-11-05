import { requestJson } from "./api-client.ts";

export type PublicShareKind = "conversations" | "reports";
export type PublicShareSaveResult = { source_id: string; notebook_id: string; reused: boolean };

export function savePublicShare(kind: PublicShareKind, token: string,
  payload: { notebook_id: string; title: string; expected_revision: string; turn_index?: number }) {
  return requestJson<PublicShareSaveResult>(`/public/${kind}/${encodeURIComponent(token)}/save`, {
    tag: "public-share-save", logPath: `/public/${kind}/{token}/save`,
    unauthorized: "preserve", method: "POST", body: JSON.stringify(payload),
  });
}
