import { performApiRequest } from "../../api-client.ts";
import { readHttpError, throwHumanizedHttpError } from "../../errors.ts";

export type AuthPolicy = {
  mode: "local" | "dual" | "binding_required" | "sso_only" | "retired";
  revision: number;
  provider_id: string | null;
  provider_namespace: string | null;
  plugin_id: string | null;
  config_generation: number;
  retired_at: string | null;
  updated_by: string | null;
};

export type AuthMigrationPreflight = {
  policy: AuthPolicy;
  active_users: number;
  unready_users: number;
  ready_admins: number;
  ready: boolean;
};

export type AuthAccount = {
  id: string;
  username: string;
  display_name: string;
  status: "active" | "disabled";
  local_login_name?: string | null;
  external_username?: string | null;
};

export type AuthAccountPage = { items: AuthAccount[]; total: number; offset: number; limit: number };
export type AuthGrant = { grant_token: string; purpose: "enroll" | "recover" | "replace"; expires_in: number };
export type AuthAuditItem = {
  id: string;
  actor_id: string | null;
  target_user_id: string | null;
  action: string;
  provider_namespace: string | null;
  subject: string | null;
  grant_reference: string | null;
  created_at: string;
};
export type AuthAuditPage = { items: AuthAuditItem[]; total: number; offset: number; limit: number };

async function adminJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await performApiRequest(path, { tag: "admin", ...init });
  if (response.status === 403) {
    await readHttpError(response, "admin");
    throw new Error("forbidden");
  }
  if (!response.ok) await throwHumanizedHttpError(response, "admin");
  return response.json() as Promise<T>;
}

export const fetchAuthPolicy = () => adminJson<AuthPolicy>("/admin/auth/policy");
export const fetchAuthMigration = () => adminJson<AuthMigrationPreflight>("/admin/auth/migration");
export const fetchAuthAccounts = (offset = 0, limit = 50) => adminJson<AuthAccountPage>(`/admin/auth/accounts?offset=${offset}&limit=${limit}`);
export const fetchAuthAudit = (offset = 0, limit = 100) => adminJson<AuthAuditPage>(`/admin/auth/audit?offset=${offset}&limit=${limit}`);
export const updateAuthPolicy = (mode: AuthPolicy["mode"], expectedRevision: number, allowRollback = false) => adminJson<AuthPolicy>("/admin/auth/policy", {
  method: "PATCH", body: JSON.stringify({ mode, expected_revision: expectedRevision, allow_rollback: allowRollback }),
});
export const prepareAuthProviderMaintenance = (expectedRevision: number, configurationGeneration: number) => adminJson<AuthPolicy>("/admin/auth/provider-configuration", {
  method: "PATCH", body: JSON.stringify({ expected_revision: expectedRevision, configuration_generation: configurationGeneration }),
});
export const updateAuthAccountStatus = (id: string, status: AuthAccount["status"]) => adminJson<AuthAccount>(`/admin/auth/accounts/${encodeURIComponent(id)}`, {
  method: "PATCH", body: JSON.stringify({ status }),
});
export const issueAuthGrant = (purpose: AuthGrant["purpose"], subject: string, targetUserId?: string) => adminJson<AuthGrant>("/admin/auth/grants", {
  method: "POST", body: JSON.stringify({ purpose, subject, ...(targetUserId ? { target_user_id: targetUserId } : {}) }),
});
