export function usernameForOwner(
  users: { id: string; username: string }[],
  owner: string,
  selfName: string,
): string {
  if (!owner) return selfName;
  const hit = users.find((u) => u.id === owner);
  return hit ? hit.username : owner;
}
