import test from "node:test";
import assert from "node:assert/strict";

import {
  mergeSessionListFallback,
  recordStartedConversation,
} from "./ask-session-state.ts";


test("a first question publishes a reopenable history item before its answer", () => {
  const sessions = [{
    id: "conv-old",
    title: "old",
    updated_at: "2026-07-20T00:00:00",
    turn_count: 2,
    used_reasoning: false,
  }];

  const result = recordStartedConversation(sessions, {
    conversationId: "conv-new",
    question: `  ${"new question ".repeat(8)}  `,
    startedAt: "2026-07-24T10:00:00.000Z",
  });

  assert.deepEqual(result.map((session) => session.id), ["conv-new", "conv-old"]);
  assert.equal(result[0].title, "new question new question new question new question new ques");
  assert.equal(result[0].updated_at, "2026-07-24T10:00:00.000Z");
  assert.equal(result[0].turn_count, 0);
  assert.equal(sessions.length, 1, "the prior state is not mutated");
});


test("a follow-up refreshes last activity and promotes the existing session", () => {
  const sessions = [
    {
      id: "conv-newer",
      title: "newer",
      updated_at: "2026-07-23T00:00:00",
      turn_count: 1,
      used_reasoning: false,
    },
    {
      id: "conv-follow-up",
      title: "keep this title",
      updated_at: "2026-07-01T00:00:00",
      turn_count: 4,
      used_reasoning: true,
    },
  ];

  const result = recordStartedConversation(sessions, {
    conversationId: "conv-follow-up",
    question: "do not replace the established title",
    startedAt: "2026-07-24T10:05:00.000Z",
  });

  assert.deepEqual(result.map((session) => session.id), ["conv-follow-up", "conv-newer"]);
  assert.equal(result[0].title, "keep this title");
  assert.equal(result[0].turn_count, 4);
  assert.equal(result[0].used_reasoning, true);
  assert.equal(result[0].updated_at, "2026-07-24T10:05:00.000Z");
});


test("a failed authority refresh fallback preserves optimistic durable sessions", () => {
  const optimisticNew = {
    id: "conv-new",
    title: "new question",
    updated_at: "2026-07-24T10:05:00.000Z",
    turn_count: 0,
    used_reasoning: false,
  };
  const optimisticFollowUp = {
    id: "conv-follow-up",
    title: "follow up",
    updated_at: "2026-07-24T10:04:00.000Z",
    turn_count: 2,
    used_reasoning: true,
  };
  const fallback = [
    { ...optimisticFollowUp, updated_at: "2026-07-01T00:00:00.000Z" },
    {
      id: "conv-old",
      title: "old",
      updated_at: "2026-06-01T00:00:00.000Z",
      turn_count: 1,
      used_reasoning: false,
    },
  ];

  const result = mergeSessionListFallback(
    [optimisticNew, optimisticFollowUp],
    fallback,
    new Set(["conv-new", "conv-follow-up"]),
  );

  assert.deepEqual(result.map((session) => session.id), [
    "conv-new",
    "conv-follow-up",
    "conv-old",
  ]);
  assert.equal(result[1].updated_at, "2026-07-24T10:04:00.000Z");
});
