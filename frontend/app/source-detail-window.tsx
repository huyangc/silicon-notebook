"use client";

import type { ReactNode } from "react";
import { X } from "lucide-react";

import { useFloatingWindow } from "./use-floating-window";


type SourceDetailWindowProps = {
  children: ReactNode;
  onClose: () => void;
  interactive?: boolean;
  zIndex?: number;
};


export function SourceDetailWindow({
  children,
  onClose,
  interactive = true,
  zIndex,
}: SourceDetailWindowProps) {
  const floating = useFloatingWindow({
    storageKey: "source.detail.window",
    resizable: false,
  });

  return (
    <section
      className="utility-modal"
      role="dialog"
      aria-modal={interactive}
      aria-hidden={!interactive}
      inert={interactive ? undefined : true}
      aria-labelledby="source-detail-window-title"
      style={{ zIndex }}
    >
      <div
        ref={floating.cardRef}
        className="utility-modal-card source-detail-card"
        style={floating.style}
      >
        <div className="source-detail-shell-header" {...floating.dragHandleProps}>
          <h2 id="source-detail-window-title">来源</h2>
          <button
            type="button"
            className="icon-button subtle-icon"
            onClick={onClose}
            title="关闭"
            aria-label="关闭来源详情"
          >
            <X size={22} />
          </button>
        </div>
        <div className="source-detail-body">{children}</div>
      </div>
    </section>
  );
}
