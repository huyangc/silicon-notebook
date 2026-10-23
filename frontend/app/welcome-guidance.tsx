/**
 * welcome-guidance.tsx
 *
 * 问答欢迎页「怎么开始」那一句。说到的动作就是可点的字：「上传来源」打开添加来源，
 * 「挂载一个参考库」打开笔记本设置并停在「参考库」一节。当前用户做不了的动作不说——
 * 只读成员两件都做不了时，只如实说内容由谁添加。
 */
"use client";


export function WelcomeGuidance({
  blocked,
  canAddSources,
  canMountBases,
  mirrored = false,
  onAddSources,
  onMountBases,
}: {
  /** true：还没有任何可检索的内容（输入框锁着）；false：有参考库可问，但本库还没有来源。 */
  blocked: boolean;
  canAddSources: boolean;
  canMountBases: boolean;
  /** 镜像库:内容随源环境同步,不是这里的谁添加的。 */
  mirrored?: boolean;
  onAddSources: () => void;
  onMountBases: () => void;
}) {
  const addSources = canAddSources && (
    <button type="button" className="link-button inline-action" onClick={onAddSources}>
      上传来源
    </button>
  );
  if (!blocked) {
    return canAddSources ? (
      <p>{addSources}（PDF、Markdown、DOCX 或 PPTX）后，这里会根据来源内容生成可追溯的问题建议。</p>
    ) : (
      <p>{mirrored ? "来源随源环境同步过来后" : "来源添加后"}，这里会根据来源内容生成可追溯的问题建议。</p>
    );
  }
  const mountBases = canMountBases && (
    <button type="button" className="link-button inline-action" onClick={onMountBases}>
      挂载一个参考库
    </button>
  );
  if (!addSources && !mountBases) {
    return (
      <p>
        {mirrored
          ? "这本笔记本还没有可检索的内容，内容随源环境同步过来之后就能在这里提问。"
          : "这本笔记本还没有可检索的内容，由库主或可编辑的成员添加之后就能在这里提问。"}
      </p>
    );
  }
  return (
    <p>
      {addSources}
      {addSources && "（PDF、Markdown、DOCX 或 PPTX）"}
      {addSources && mountBases && "，或"}
      {mountBases}
      ，就能基于内容对话了。
    </p>
  );
}
