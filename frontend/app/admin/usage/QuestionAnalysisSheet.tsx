"use client";

import { useEffect, useMemo, useState } from "react";

import { ActivityView } from "../../dev/logs/activity/ActivityView.tsx";
import type { ActivityTypeOption } from "../../dev/logs/activity/ActivityStream.tsx";
import type { AdminUserUsage } from "./api.ts";

// 模块级常量:不是因为引用必须稳定——ActivityView 用 useState 的惰性初始值只在
// **挂载**那一刻读一次 activityTypeOptions 算初始 activityType(见其
// `useState(() => initialActivityType(activityTypeOptions))`),此后 ownerId/users
// 变化触发的重渲染即便传入一个新数组引用,也不会被那次挂载后的重渲染重新读取,
// 「点击「深度报告」后状态保持」因此不依赖这里的引用是否稳定。放在模块级只是
// 省掉每次渲染重新分配这个字面量数组。
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
