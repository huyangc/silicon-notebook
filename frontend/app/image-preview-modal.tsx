"use client";

import { useEffect, useRef, useState, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, Minus, Plus, RotateCcw, X } from "lucide-react";
import {
  TransformComponent,
  TransformWrapper,
  type ReactZoomPanPinchRef,
} from "react-zoom-pan-pinch";

const IMAGE_PREVIEW_MIN_SCALE = 1;
const IMAGE_PREVIEW_MAX_SCALE = 8;
const IMAGE_PREVIEW_WHEEL_STEP = 0.12;
const IMAGE_PREVIEW_BACKDROP_CLICK_SLOP_PX = 6;

export function ImagePreviewModal({
  children,
  referenceLabel,
  onClose,
  interactive = true,
  zIndex,
  imageIndex = 0,
  imageCount = 1,
  onSelectImage,
}: {
  children: ReactNode;
  referenceLabel: string;
  onClose: (reason: "button" | "backdrop" | "escape") => void;
  interactive?: boolean;
  zIndex?: number;
  /** 当前显示的是这一组图片里的第几张（从 0 起）。 */
  imageIndex?: number;
  /** 这一组一共几张。为 1（或没有 onSelectImage）时不出现任何切换控件。 */
  imageCount?: number;
  /** 切换到第 index 张。调用方只负责换 children，夹取与端点判断都在本组件内。 */
  onSelectImage?: (index: number) => void;
}) {
  const [scale, setScale] = useState(IMAGE_PREVIEW_MIN_SCALE);
  const backdropPointerStartRef = useRef<{ x: number; y: number } | null>(null);
  const transformRef = useRef<ReactZoomPanPinchRef | null>(null);
  const previewLabel = referenceLabel ? `引用 ${referenceLabel} 图片预览` : "引用图片预览";
  const canNavigate = imageCount > 1 && Boolean(onSelectImage);
  const atFirst = imageIndex <= 0;
  const atLast = imageIndex >= imageCount - 1;

  // 换图 = 换 children，缩放/位移必须跟着回到初始值,否则上一张放大到 400% 后翻页
  // 会落在新图的某个角上。刻意用命令式复位而**不是**给 TransformWrapper 加
  // key：重挂载会让关闭按钮上的 autoFocus 每翻一页就把焦点抢回去,鼠标用户连点
  // 「下一张」时焦点会悄悄挪到「关闭」上,下一次空格/回车就把预览关了。
  // `0` 是动画时长——换图是瞬时的,补间只会让新图带着上一张的缩放闪一下。
  useEffect(() => {
    transformRef.current?.resetTransform(0);
    setScale(IMAGE_PREVIEW_MIN_SCALE);
  }, [imageIndex]);

  // 夹取只写这一处：两颗按钮在端点上是 disabled、左右方向键到端点后原地不动,
  // 调用方因此永远只会收到合法下标。
  const selectImage = (next: number) => {
    if (!onSelectImage) return;
    const clamped = Math.min(Math.max(next, 0), imageCount - 1);
    if (clamped !== imageIndex) onSelectImage(clamped);
  };

  // 左右方向键挂在 window 上而不是像 Escape 那样挂在本节点上：本预览没有焦点陷阱,
  // 焦点可能根本不在它里面（Tab 出去、或打开时那次 autoFocus 没落地——真机上量到过
  // activeElement 就是 body），那时 React 的 onKeyDown 一个字都收不到。被更高层弹窗
  // 盖住(interactive=false)时不挂,单张图时也不挂；输入类元素上的方向键属于光标移动,
  // 不抢。
  useEffect(() => {
    if (!interactive || !canNavigate) return;
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      if (event.defaultPrevented) return;
      const target = event.target;
      if (target instanceof Element
        && target.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']")) return;
      event.preventDefault();
      selectImage(imageIndex + (event.key === "ArrowLeft" ? -1 : 1));
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactive, canNavigate, imageIndex, imageCount, onSelectImage]);

  return (
    <section
      className="utility-modal answer-image-preview"
      role="dialog"
      aria-modal={interactive}
      aria-hidden={!interactive}
      inert={interactive ? undefined : true}
      aria-label={previewLabel}
      style={{ zIndex }}
      onClick={(event) => {
        if (event.currentTarget === event.target) onClose("backdrop");
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose("escape");
      }}
    >
      <TransformWrapper
        ref={transformRef}
        initialScale={IMAGE_PREVIEW_MIN_SCALE}
        minScale={IMAGE_PREVIEW_MIN_SCALE}
        maxScale={IMAGE_PREVIEW_MAX_SCALE}
        centerZoomedOut
        limitToBounds
        wheel={{ step: IMAGE_PREVIEW_WHEEL_STEP }}
        panning={{ velocityDisabled: true }}
        doubleClick={{ mode: "toggle" }}
        onTransform={(_ref, state) => setScale(state.scale)}
      >
        {({ zoomIn, zoomOut, resetTransform }) => (
          <div
            className="answer-image-preview-stage"
            onPointerDown={(event) => {
              backdropPointerStartRef.current = { x: event.clientX, y: event.clientY };
            }}
            onPointerUp={(event) => {
              const start = backdropPointerStartRef.current;
              backdropPointerStartRef.current = null;
              if (!start) return;
              const dx = event.clientX - start.x;
              const dy = event.clientY - start.y;
              if (Math.hypot(dx, dy) > IMAGE_PREVIEW_BACKDROP_CLICK_SLOP_PX) return;
              const target = event.target;
              if (!(target instanceof Element)) return;
              if (target.closest(
                ".answer-image-preview-controls, .answer-image-preview-step, .answer-image-preview-content",
              )) return;
              onClose("backdrop");
            }}
          >
            <div className="answer-image-preview-toolbar">
              {canNavigate && (
                <span className="answer-image-preview-count" aria-live="polite">
                  {imageIndex + 1} / {imageCount}
                </span>
              )}
              <div className="answer-image-preview-controls" aria-label="图片缩放控制">
                <button type="button" onClick={() => zoomOut()} aria-label="缩小图片">
                  <Minus size={18} />
                </button>
                <button
                  type="button"
                  className="answer-image-preview-scale"
                  onClick={() => resetTransform()}
                  aria-label="重置图片缩放"
                  title="重置为 100%"
                >
                  <RotateCcw size={15} />
                  <span>{Math.round(scale * 100)}%</span>
                </button>
                <button type="button" onClick={() => zoomIn()} aria-label="放大图片">
                  <Plus size={18} />
                </button>
                <span className="answer-image-preview-control-divider" aria-hidden="true" />
                <button type="button" autoFocus onClick={() => onClose("button")} aria-label="关闭图片预览">
                  <X size={19} />
                </button>
              </div>
            </div>
            {canNavigate && (
              <>
                <button
                  type="button"
                  className="answer-image-preview-step prev"
                  onClick={() => selectImage(imageIndex - 1)}
                  disabled={atFirst}
                  aria-label="上一张图片"
                  title="上一张（←）"
                >
                  <ChevronLeft size={26} />
                </button>
                <button
                  type="button"
                  className="answer-image-preview-step next"
                  onClick={() => selectImage(imageIndex + 1)}
                  disabled={atLast}
                  aria-label="下一张图片"
                  title="下一张（→）"
                >
                  <ChevronRight size={26} />
                </button>
              </>
            )}
            <TransformComponent
              wrapperClass="answer-image-preview-zoom"
              contentClass="answer-image-preview-zoom-content"
            >
              <div className="answer-image-preview-content">{children}</div>
            </TransformComponent>
          </div>
        )}
      </TransformWrapper>
    </section>
  );
}
