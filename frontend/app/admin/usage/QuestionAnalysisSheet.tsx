"use client";

import { useEffect, useMemo, useState } from "react";

import { ActivityView } from "../../dev/logs/activity/ActivityView.tsx";
import type { ActivityTypeOption } from "../../dev/logs/activity/ActivityStream.tsx";
import type { AdminUserUsage } from "./api.ts";

// 模块级常量:引用必须稳定——ActivityView 只在**挂载**时读它算初始 activityType,
// 若在这里写成内联字面量,ownerId/users 变化触发的每次重渲染都会传入一个新数组,
// 与「点击「深度报告」后状态保持」并不冲突(ActivityView 不再按引用订阅这个 prop
// 变化),但避免不必要的重建仍是更干净的写法。
const QUESTION_ANALYSIS_ACTIVITY_TYPES: ActivityTypeOption[] = [
  { value: "ask", label: "问答" },
  { value: "report", label: "深度报告" },
];

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
        <span>只读查看问答与深度报告：提问、回答、报告正文与推理轨迹；不会修改用户的笔记本。</span>
      </div>
      <ActivityView
        activityTypeOptions={QUESTION_ANALYSIS_ACTIVITY_TYPES}
        scopeKey={JSON.stringify(["admin-usage-questions", ownerId])}
        userId={ownerId}
      />
    </section>
  );
}
