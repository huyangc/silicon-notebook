"use client";

import { Users } from "lucide-react";

import { collaboratorName, collaboratorRoleLabel } from "./notebook-collaborators-api.ts";
import type { SharedByMeItem } from "./notebook-share.ts";

type Collaborators = SharedByMeItem["collaborators"];

/**
 * 「我分享的」卡片里点名共享的那一部分(分享编辑 P1)。
 *
 * 只因点名共享而出现的行(没有链接、没有群组)靠它才不是一张空卡片:头部一个人数小标,正文列出
 * 每个人与他的权限。名单是事实陈述,增删在那本笔记本的「分享」里,这里不放动作。旧后端不发
 * `collaborators` → 缺失按空,两部分都不渲染。
 */
export function ShareOverviewCollaboratorsChip({ collaborators }: { collaborators?: Collaborators }) {
  const count = (collaborators ?? []).length;
  if (count === 0) return null;
  return (
    <span className="share-channel-chip is-group">
      <Users size={12} /> 点名共享 {count} 人
    </span>
  );
}

export function ShareOverviewCollaborators({ collaborators }: { collaborators?: Collaborators }) {
  const people = collaborators ?? [];
  if (people.length === 0) return null;
  return (
    <div className="share-overview-channel is-group">
      <ul className="share-overview-collaborators" aria-label="点名共享">
        {people.map((person) => (
          <li key={person.user_id || person.username}>
            {collaboratorName(person)} · {collaboratorRoleLabel(person.role)}
          </li>
        ))}
      </ul>
    </div>
  );
}
