"use client";

import type { ReactNode } from "react";
import { X } from "lucide-react";

import { FloatingModalCard } from "./floating-modal-card";

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
  return (
    <section
      className="utility-modal answer-image-preview"
      role="dialog"
      aria-modal={interactive}
      aria-hidden={!interactive}
      inert={interactive ? undefined : true}
      aria-label={`${referenceLabel || "引用"}附图预览`}
      style={{ zIndex }}
      onClick={(event) => {
        if (event.currentTarget === event.target) onClose("backdrop");
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose("escape");
      }}
    >
      <FloatingModalCard
        storageKey="answerImagePreview.window"
        className="utility-modal-card answer-image-preview-card"
        resizable
      >
        {(floating) => (
          <>
            <header className="source-modal-header" {...floating.dragHandleProps}>
              <div>
                <h2>本段附图 {referenceLabel}</h2>
                <p>模型未直接读取图片</p>
              </div>
              <button type="button" autoFocus onClick={() => onClose("button")} aria-label="关闭图片预览">
                <X size={18} />
              </button>
            </header>
            <div className="answer-image-preview-body">
              {children}
            </div>
            <span
              className="kh-modal-resize-handle"
              aria-hidden="true"
              {...floating.resizeHandleProps}
            />
          </>
        )}
      </FloatingModalCard>
    </section>
  );
}
