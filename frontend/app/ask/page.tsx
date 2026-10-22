"use client";

import { useLayoutEffect, useRef, useState } from "react";
import GlobalAskWorkspace from "./global-ask-workspace";
import { API_BASE } from "../api-config";
import { AuthedImage } from "../authed-image";
import { ImagePreviewModal } from "../image-preview-modal";
import { currentPreviewImage, type AnswerImagePreviewRequest } from "../image-preview";
import { sourceImageAssetUrl } from "../source-image";

// 独立访问的 `/ask`：没有笔记本页那套 root modal 协调器，也没有需要让位的窗口，
// 引用配图的大图预览由本页直接承接——与浮窗里同一个 `ImagePreviewModal`、同一份
// 冻结画册、同一条取图归属（每张图带着自己的取图库）。Esc / 遮罩 / 关闭按钮都只关
// 预览，焦点回到点开它的那个元素（与 `/c/{token}` 同一条做法）。
export default function GlobalAskPage() {
  const [preview, setPreview] = useState<AnswerImagePreviewRequest | null>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const image = currentPreviewImage(preview);
  const open = (request: AnswerImagePreviewRequest) => {
    returnFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    setPreview(request);
  };
  useLayoutEffect(() => {
    if (preview) return;
    const target = returnFocus.current;
    returnFocus.current = null;
    if (target?.isConnected) target.focus({ preventScroll: true });
  }, [preview]);
  return <>
    <GlobalAskWorkspace onPreviewImage={open} imagePreviewOpen={Boolean(preview)} />
    {preview && image && image.notebookId && <ImagePreviewModal
      referenceLabel={image.referenceLabel}
      imageIndex={preview.index}
      imageCount={preview.items.length}
      onSelectImage={(index) => setPreview((prev) => (prev ? { ...prev, index } : prev))}
      onClose={() => setPreview(null)}
    >
      <AuthedImage url={sourceImageAssetUrl(API_BASE, image.notebookId, image.assetId)} alt={image.alt} />
    </ImagePreviewModal>}
  </>;
}
