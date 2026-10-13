"use client";

import { useEffect, type ReactNode } from "react";
import { BookOpen } from "lucide-react";

/** Shared geometry keeps the successful-login cover in place as the app mounts below it. */
export function AuthBook({ children, opening = false }: { children: ReactNode; opening?: boolean }) {
  return <div className={`auth-book-scene${opening ? " is-opening" : ""}`}>
    <div className="auth-book-layout">
      <div className="auth-book-stage">
        <div className="auth-book-pages" aria-hidden="true" />
        <div className="auth-book-leaf auth-book-leaf-back" aria-hidden="true" />
        <div className="auth-book-leaf auth-book-leaf-front" aria-hidden="true" />
        <section className="auth-book-cover">
          <span className="brand-mark auth-book-monogram" aria-hidden="true">SN</span>
          <div className="auth-book-cover-heading">
            <BookOpen size={56} strokeWidth={1.1} aria-hidden="true" />
            <h1>Silicon Notebook</h1>
          </div>
          <div className="auth-book-controls">{children}</div>
        </section>
      </div>
    </div>
  </div>;
}

export function LoginEntrance({ onComplete }: { onComplete: () => void }) {
  useEffect(() => {
    const preference = window.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (preference?.matches) { onComplete(); return; }
    const changed = () => { if (preference?.matches) onComplete(); };
    preference?.addEventListener("change", changed);
    // The deadline also releases the app when animation events are unavailable.
    const deadline = window.setTimeout(onComplete, 1500);
    return () => {
      window.clearTimeout(deadline);
      preference?.removeEventListener("change", changed);
    };
  }, [onComplete]);

  return <div className="auth-book-entrance" onAnimationEnd={(event) => {
    if (event.animationName === "auth-book-reveal" && event.target === event.currentTarget) onComplete();
  }}>
    <AuthBook opening><p className="auth-book-success" role="status">登录成功，正在翻开笔记本…</p></AuthBook>
  </div>;
}
