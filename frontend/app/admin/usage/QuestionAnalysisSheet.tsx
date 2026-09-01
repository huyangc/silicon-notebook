"use client";

import { useEffect, useMemo, useState } from "react";

import { ActivityView } from "../../dev/logs/activity/ActivityView.tsx";
import type { AdminUserUsage } from "./api.ts";

export function QuestionAnalysisSheet({
  users,
  currentUserId,
}: {
  users: AdminUserUsage[];
  currentUserId: string;
}) {
  const initialOwner = useMemo(() => {
    if (typeof window === "undefined") return currentUserId;
    const requested = new URLSearchParams(window.location.search).get("owner") || "";
    return users.some((user) => user.id === requested) ? requested : currentUserId;
  }, [currentUserId, users]);
  const [ownerId, setOwnerId] = useState(initialOwner);

  useEffect(() => {
    if (ownerId && users.some((user) => user.id === ownerId)) return;
    setOwnerId(initialOwner || users[0]?.id || "");
  }, [initialOwner, ownerId, users]);

  function selectOwner(value: string) {
    setOwnerId(value);
    const params = new URLSearchParams(window.location.search);
    params.set("sheet", "questions");
    if (value) params.set("owner", value);
    else params.delete("owner");
    window.history.replaceState(null, "", `?${params.toString()}`);
  }

  return (
    <section className="usage-analysis-sheet" aria-label="提问分析">
      <div className="usage-analysis-toolbar">
        <label>
          用户
          <select value={ownerId} onChange={(event) => selectOwner(event.target.value)}>
            {users.map((user) => (
              <option value={user.id} key={user.id}>{user.username}</option>
            ))}
          </select>
        </label>
        <span>只读查看提问、回答与推理轨迹；不会修改用户的笔记本。</span>
      </div>
      <ActivityView
        fixedActivityType="ask"
        scopeKey={JSON.stringify(["admin-usage-questions", ownerId])}
        userId={ownerId}
      />
    </section>
  );
}
