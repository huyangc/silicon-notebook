/**
 * kg-readiness-hint.tsx
 *
 * 来源栏里那一句「知识图谱现在怎样、能做什么」。说明里提到的动作就是可点的字：
 * 「整理知识图谱」「分析并合并」「挂载参考库」都是行内文字按钮，按下即做，不让人读完一句话
 * 再去别处找入口。动作做不了时（没有挂载权限）那半句就不说，而不是说一个找不到的入口。
 *
 * 只是呈现：整理/合并调 `onBuild`，挂载参考库调 `onMountBases`（问答里是打开笔记本设置、
 * 停在「参考库」一节）。整理在途时按钮换成「整理中…」并禁用，进度卡由壳层另行显示。
 */
"use client";


export type KgReadinessHintProps = {
  kgReady: boolean;
  /** 已就绪图谱之后新增、尚未分析的来源数。 */
  pendingSources: number;
  /** 本笔记本没图谱时，已挂载的参考库里有没有整理好的图谱可借。 */
  baseKgAvailable: boolean;
  /** 挂没挂参考库（不论有没有图谱）：决定是说「也没挂参考库」还是「挂的参考库也都没整理」。 */
  hasMountedBase: boolean;
  /** 会用到图谱的那个回答方式的名字（如「深入分析」）。 */
  strictLabel: string;
  /** 整理 / 合并正在进行（或图谱正在删除）：动作禁用。 */
  busy: boolean;
  building: boolean;
  /** 当前用户能不能挂载参考库（库主、且不是镜像库）。 */
  canMountBases: boolean;
  onBuild: () => void;
  onMountBases: () => void;
};


export function KgReadinessHint({
  kgReady,
  pendingSources,
  baseKgAvailable,
  hasMountedBase,
  strictLabel,
  busy,
  building,
  canMountBases,
  onBuild,
  onMountBases,
}: KgReadinessHintProps) {
  function action(label: string, busyLabel: string, title: string) {
    return (
      <button
        type="button"
        className="link-button inline-action"
        disabled={busy}
        title={title}
        onClick={onBuild}
      >
        {building ? busyLabel : label}
      </button>
    );
  }

  if (kgReady && pendingSources > 0) {
    return (
      <p className="tool-hint kg-readiness-hint">
        知识图谱已就绪 · {pendingSources} 篇新来源待分析，
        {action("分析并合并", "分析中…", "分析新增来源并合并进知识图谱")}
      </p>
    );
  }
  if (kgReady) {
    return (
      <p className="tool-hint kg-readiness-hint">
        {`✓ 知识图谱已就绪 · 「${strictLabel}」将同时使用图谱`}
      </p>
    );
  }
  if (baseKgAvailable) {
    return (
      <p className="tool-hint kg-readiness-hint">
        本笔记本尚未整理知识图谱，{strictLabel}将借用已挂载的参考库；也可
        {action("为本笔记本单独整理", "整理中…", "为本笔记本单独整理知识图谱")}
      </p>
    );
  }
  return (
    <p className="tool-hint kg-readiness-hint">
      {hasMountedBase
        ? "本笔记本尚未整理知识图谱，挂载的参考库也都还没有整理。"
        : "本笔记本尚未整理知识图谱，也没挂参考库。"}
      {action("整理知识图谱", "整理中…", `整理知识图谱可增强${strictLabel}效果`)}
      可增强{strictLabel}效果
      {canMountBases && (
        <>
          ，也可
          <button
            type="button"
            className="link-button inline-action"
            title="打开笔记本设置，挂载一个已整理知识图谱的参考库"
            onClick={onMountBases}
          >
            挂载已整理的参考库
          </button>
        </>
      )}
      。
    </p>
  );
}


export type KgUnavailableHintProps = {
  /** strict:内置深入分析(无图照常答,只是只用原文);extension:声明了需要图谱的扩展引擎。 */
  variant: "strict" | "extension";
  /** 取不到图谱只是因为带图谱的参考库这次没勾(出路是勾回来,不是整理一次整库图谱)。 */
  blockedByScope: boolean;
  strictLabel: string;
  /** 能不能为本笔记本整理图谱(可写、非镜像)。 */
  canBuild: boolean;
  canMountBases: boolean;
  /** 整理在途 / 提问在途:动作禁用。 */
  busy: boolean;
  building: boolean;
  onSelectKgBases: () => void;
  onBuild: () => void;
  onMountBases: () => void;
};


/**
 * 问答输入框下排、回答方式旁的那句「这一问取不到知识图谱」。和来源栏那句同一条规矩:
 * 说到的出路就是可点的字。两种成因出路不同——带图谱的参考库没勾 → 「勾选带图谱的参考库」
 * (刻意不劝整理,那是一次整库的模型调用);一个带图的都没有 → 「整理知识图谱」/「挂载已整理
 * 的参考库」。当前用户做不了的出路不说。
 */
export function KgUnavailableHint({
  variant,
  blockedByScope,
  strictLabel,
  canBuild,
  canMountBases,
  busy,
  building,
  onSelectKgBases,
  onBuild,
  onMountBases,
}: KgUnavailableHintProps) {
  const className = variant === "strict" ? "mode-hint" : "chat-hint";
  const lead = variant === "strict"
    ? (blockedByScope
      ? `已整理知识图谱的参考库这次都没勾选，${strictLabel}将只用原文检索`
      : `该笔记本尚无知识图谱，${strictLabel}将只用原文检索`)
    : "当前扩展引擎需要知识图谱";
  if (blockedByScope) {
    return (
      <span className={className}>
        {lead}；
        <button type="button" className="link-button inline-action" disabled={busy} onClick={onSelectKgBases}>
          勾选带图谱的参考库
        </button>
        {variant === "strict" ? "可增强" : "后即可使用"}
      </span>
    );
  }
  if (!canBuild && !canMountBases) {
    return <span className={className}>{variant === "strict" ? lead : `${lead}，本笔记本还没有整理`}</span>;
  }
  return (
    <span className={className}>
      {lead}；
      {canBuild && (
        <button type="button" className="link-button inline-action" disabled={busy} onClick={onBuild}>
          {building ? "整理中…" : "整理知识图谱"}
        </button>
      )}
      {canBuild && canMountBases && "或"}
      {canMountBases && (
        <button type="button" className="link-button inline-action" onClick={onMountBases}>
          挂载已整理的参考库
        </button>
      )}
      {variant === "strict" ? "可增强问答效果" : "后即可使用"}
    </span>
  );
}
