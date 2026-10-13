"use client";

import { useEffect, type ReactNode } from "react";
import { ArrowUpRight, BookOpen } from "lucide-react";

/** Shared geometry keeps the successful-login cover in place as the app mounts below it. */
export function AuthBook({ children, opening = false }: { children: ReactNode; opening?: boolean }) {
  return <div className={`auth-book-scene${opening ? " is-opening" : ""}`}>
    <div className="auth-book-layout">
      <div className="auth-book-intro" aria-hidden={opening || undefined}>
        <div className="auth-book-wordmark"><span className="brand-mark" aria-hidden="true">SN</span> silicon-notebook</div>
        <p className="auth-book-eyebrow">把知识炼成经验</p>
        <h1>每一次探索<br />都值得<span>被记录</span></h1>
        <p className="auth-book-description">收集资料，连接知识。<br />从一个问题开始，写下属于你的理解。</p>
        <div className="auth-book-caption"><span /> 你的下一页，从这里开始 <ArrowUpRight size={16} aria-hidden="true" /></div>
      </div>
      <div className="auth-book-stage">
        <div className="auth-book-pages" aria-hidden="true" />
        <div className="auth-book-leaf auth-book-leaf-back" aria-hidden="true" />
        <div className="auth-book-leaf auth-book-leaf-front" aria-hidden="true" />
        <section className="auth-book-cover">
          <div className="auth-book-cover-heading">
            <span className="auth-book-edition">一本属于你的知识笔记</span>
            <BookOpen size={36} strokeWidth={1.1} aria-hidden="true" />
            <h2>知识有迹<br />探索无界</h2>
            <span className="auth-book-cover-rule" />
          </div>
          <div className="auth-book-controls">{children}</div>
          <div className="auth-book-cover-footer" aria-hidden="true"><span>silicon-notebook</span><span>从此页，向更远</span></div>
        </section>
      </div>
    </div>
    <div className="auth-book-footer" aria-hidden="true">把零散的发现，写成相连的知识。</div>
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
