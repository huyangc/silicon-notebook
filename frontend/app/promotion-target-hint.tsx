/**
 * promotion-target-hint.tsx
 *
 * 「贡献到公共知识库」因为这本笔记本还没挂公共知识库而禁用时，紧挨按钮的那半句。说到的
 * 动作就是可点的字：能挂载的人（库主、非镜像）看到「需先挂载公共知识库」，点了打开笔记本
 * 设置并停在「参考库」一节；其他成员只被告知要库主先挂；镜像库谁都挂不了，就不说。
 *
 * 按钮自己的 `title`（PROMOTION_TARGET_NONE_REASON）照旧保留；这句是让原因不用悬停就看得见。
 */
"use client";


/** 谁能为这本笔记本挂载公共知识库：自己 / 库主 / 谁都不行（镜像）。 */
export type MountBasesAccess = "self" | "owner" | "none";


export function PromotionTargetHint({
  access,
  onMountBases,
}: {
  access: MountBasesAccess;
  onMountBases: () => void;
}) {
  if (access === "none") return null;
  return (
    <span className="tool-hint promotion-target-hint">
      {access === "self" ? (
        <>
          需先
          <button
            type="button"
            className="link-button inline-action"
            title="打开笔记本设置，挂载一个公共知识库"
            onClick={onMountBases}
          >
            挂载公共知识库
          </button>
        </>
      ) : "需库主先挂载一个公共知识库"}
    </span>
  );
}
