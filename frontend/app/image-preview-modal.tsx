"use client";

import { useRef, useState, type ReactNode } from "react";
import { Minus, Plus, RotateCcw, X } from "lucide-react";
import { TransformComponent, TransformWrapper } from "react-zoom-pan-pinch";

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
}: {
  children: ReactNode;
  referenceLabel: string;
  onClose: (reason: "button" | "backdrop" | "escape") => void;
  interactive?: boolean;
  zIndex?: number;
}) {
  const [scale, setScale] = useState(IMAGE_PREVIEW_MIN_SCALE);
  const backdropPointerStartRef = useRef<{ x: number; y: number } | null>(null);
  const previewLabel = referenceLabel ? `引用 ${referenceLabel} 图片预览` : "引用图片预览";

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
              if (target.closest(".answer-image-preview-controls, .answer-image-preview-content")) return;
              onClose("backdrop");
            }}
          >
            <div className="answer-image-preview-toolbar">
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
