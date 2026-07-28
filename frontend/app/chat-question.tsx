import { useEffect, useRef, useState } from "react";

import { formatQuestionTime } from "./chat-question-time";

type ChatQuestionProps = {
  question: string;
  askedAt?: string;
  now?: Date;
};

export function ChatQuestion({ question, askedAt, now }: ChatQuestionProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState(false);
  const [focused, setFocused] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [clock, setClock] = useState(() => now ?? new Date());
  const timeLabel = askedAt ? formatQuestionTime(askedAt, now ?? clock) : "";
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
      className="chat-user-message"
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
    >
      <div
        className="chat-user"
        role={timeLabel ? "button" : undefined}
        tabIndex={timeLabel ? 0 : undefined}
        aria-pressed={timeLabel ? pinned : undefined}
        aria-label={timeLabel ? `${question}，提问时间 ${timeLabel}` : undefined}
        onClick={() => { if (timeLabel) setPinned(true); }}
        onKeyDown={(event) => {
          if (!timeLabel) return;
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            setPinned(true);
          } else if (event.key === "Escape") {
            setPinned(false);
          }
        }}
      >
        {question}
      </div>
      {visible && (
        <time className="chat-question-time" dateTime={askedAt}>
          {timeLabel}
        </time>
      )}
    </div>
  );
}
