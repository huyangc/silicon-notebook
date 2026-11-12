import assert from "node:assert/strict";
import test from "node:test";
import {
  answerBodyWithoutRankingNotice,
  rankingNoticeLines,
} from "../../app/answer-completeness.ts";

test("ranking notice is stripped only as the exact server prefix", () => {
  const notice = "没能按提及次数排行：统计时出错。\n按提及它的来源篇数统计，范围：100 篇来源。";
  assert.equal(answerBodyWithoutRankingNotice(`> ${notice}\n\n正文`, notice), "正文");
  assert.equal(answerBodyWithoutRankingNotice(notice, notice), "");
  for (const body of [notice + "\n\n正文", `> ${notice}\n正文`, `前言\n> ${notice}\n\n正文`]) {
    assert.equal(answerBodyWithoutRankingNotice(body, notice), body);
  }
  assert.equal(answerBodyWithoutRankingNotice("正文", undefined), "正文");
  assert.equal(answerBodyWithoutRankingNotice("正文", ""), "正文");
  assert.deepEqual(rankingNoticeLines(notice), notice.split("\n"));
});

test("Phase B public-turn fixture loses its notice from the Markdown body", () => {
  // Copied from backend/tests/test_concept_ranking_answer.py::
  // test_public_turn_keeps_ranking_notice_and_omits_ranking_card (AskResponse.model_dump()).
  const fixture = {
    ranking_notice: "按提及它的来源篇数统计，范围：100 篇来源。",
    answer: "> 按提及它的来源篇数统计，范围：100 篇来源。\n\n正文",
  };
  const body = answerBodyWithoutRankingNotice(fixture.answer, fixture.ranking_notice);
  assert.equal(body, "正文");
  assert.equal(body.includes(fixture.ranking_notice), false);
});
