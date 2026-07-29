import type { ReactNode } from "react";
import { useEffect, useRef, useState } from "react";

import { formatQuestionTime } from "./chat-question-time";

type ChatAnswerProps = {
  answeredAt?: string;
  children: ReactNode;
  now?: Date;
};

const INTERACTIVE_SELECTOR = "a,button,input,select,textarea,[role='button']";

/**
 * Answer-card counterpart to ChatQuestion's timestamp interaction.
 *
 * The answer body contains citation links and action buttons, so it cannot be
 * turned into one giant button.  Clicking ordinary answer content pins the
 * timestamp; clicking a nested control preserves that control's own action.
 */
export function ChatAnswer({ answeredAt, children, now }: ChatAnswerProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [clock, setClock] = useState(() => now ?? new Date());
  const timeLabel = answeredAt ? formatQuestionTime(answeredAt, now ?? clock) : "";
  const visible = Boolean(timeLabel) && (hovered || focused || pinned);

  useEffect(() => {
    if (now) setClock(now);
  }, [now]);

  useEffect(() => {
    if (now || !visible) return;
    const nextMidnight = new Date();
    nextMidnight.setHours(24, 0, 0, 10);
    const timer = window.setTimeout(
      () => setClock(new Date()),
      Math.max(0, nextMidnight.getTime() - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [clock, now, visible]);

  useEffect(() => {
    if (!pinned) return;
    const dismiss = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setPinned(false);
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [pinned]);

  return (
    <div
      ref={rootRef}
      className="chat-assistant-message"
      onPointerEnter={() => {
        if (!now) setClock(new Date());
        setHovered(true);
      }}
      onPointerLeave={() => setHovered(false)}
      onFocus={() => {
        if (!now) setClock(new Date());
        setFocused(true);
      }}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setFocused(false);
          setPinned(false);
        }
      }}
      onClick={(event) => {
        if (!timeLabel) return;
        const target = event.target as Element;
        if (target.closest(INTERACTIVE_SELECTOR)) return;
        setPinned(true);
      }}
    >
      <div className="chat-assistant">{children}</div>
      {timeLabel && (
        <button
          type="button"
          className={`chat-answer-time-toggle${visible ? " visible" : ""}`}
          aria-label={`回答时间 ${timeLabel}`}
          aria-pressed={pinned}
          onClick={() => setPinned((value) => !value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              setPinned(false);
            }
          }}
        >
          <time dateTime={answeredAt}>{timeLabel}</time>
        </button>
      )}
    </div>
  );
}
