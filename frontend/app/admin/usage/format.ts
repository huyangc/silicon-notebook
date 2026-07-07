export function canSeeAdminUsage(role: string | undefined): boolean {
  return role === "admin";
}

export function formatLastActive(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16);
}

export function logsDrillHref(userId: string): string {
  return `/dev/logs?owner=${encodeURIComponent(userId)}`;
}
