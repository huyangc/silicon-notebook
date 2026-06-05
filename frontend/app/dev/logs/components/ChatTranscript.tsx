"use client";
import { useState } from "react";
import type { FullRecord, Message } from "../types";
import { prettyJson } from "../format";
import { CopyButton } from "./CopyButton";

function MessageBlock({ msg }: { msg: Message }) {
  const role = msg.role || "?";
  const cls = role === "system" ? "system" : role === "assistant" ? "assistant" : "user";
  return (
    <div className={`msg ${cls}`}>
      <div className="msg-role">
        {role}
        <CopyButton text={msg.content || ""} />
      </div>
      <pre className="msg-body">{msg.content}</pre>
    </div>
  );
}

export function ChatTranscript({ rec }: { rec: FullRecord }) {
  const [raw, setRaw] = useState(false);
  const messages = rec.request?.messages ?? [];
  const schemaHint = rec.request?.schema_hint ?? "";
  const responseText = rec.response?.content ?? "";
  const pretty = prettyJson(responseText);
  return (
    <div className="transcript">
      <div className="transcript-section-title">发送给 LLM 的对话（{messages.length} 条）</div>
      {messages.map((m, i) => (
        <MessageBlock key={i} msg={m} />
      ))}
      {schemaHint ? (
        <div className="msg schema">
          <div className="msg-role">
            schema_hint
            <CopyButton text={schemaHint} />
          </div>
          <pre className="msg-body">{schemaHint}</pre>
        </div>
      ) : null}
      {responseText ? (
        <>
          <div className="transcript-section-title">模型回复</div>
          <div className="detail-response">
            <div className="msg-role">
              response.content
              <button className="copy-btn" onClick={() => setRaw((v) => !v)}>
                {raw ? "美化" : "raw"}
              </button>
              <CopyButton text={responseText} />
            </div>
            <pre className="msg-body">{raw || !pretty.ok ? responseText : pretty.pretty}</pre>
          </div>
        </>
      ) : null}
    </div>
  );
}
