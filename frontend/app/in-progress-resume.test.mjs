import test from "node:test";
import assert from "node:assert/strict";

import {
  shouldResumeReviewAll, shouldResumeScaleIndex, shouldResumeKgBuild, kgBuildFinished,
} from "./in-progress-resume.ts";

test("reviewAll: running → 接回；其余/空 → 否", () => {
  assert.equal(shouldResumeReviewAll({ status: "running", total: 5, done: 1, error: "" }), true);
  assert.equal(shouldResumeReviewAll({ status: "done", total: 5, done: 5, error: "" }), false);
  assert.equal(shouldResumeReviewAll({ status: "idle", total: 0, done: 0, error: "" }), false);
  assert.equal(shouldResumeReviewAll(null), false);
  assert.equal(shouldResumeReviewAll(undefined), false);
});

test("scaleIndex: building=true → 接回；false/缺省/空 → 否", () => {
  assert.equal(shouldResumeScaleIndex({ building: true }), true);
  assert.equal(shouldResumeScaleIndex({ building: false }), false);
  assert.equal(shouldResumeScaleIndex({}), false);
  assert.equal(shouldResumeScaleIndex(null), false);
});

test("scaleIndex: state===\"queued\" 即使 building=false 也应接回（已排队不刷新 bug）", () => {
  assert.equal(shouldResumeScaleIndex({ building: false, state: "queued" }), true);
  assert.equal(shouldResumeScaleIndex({ state: "queued" }), true);
  assert.equal(shouldResumeScaleIndex({ building: false, state: "indexed" }), false);
  assert.equal(shouldResumeScaleIndex({ building: false, state: "stale" }), false);
});

test("kgBuild: durable running job takes precedence over legacy flag", () => {
  assert.equal(shouldResumeKgBuild({
    kg_building: false,
    kg_build: { status: "running" },
  }), true);
  assert.equal(shouldResumeKgBuild({
    kg_building: true,
    kg_build: { status: "failed" },
  }), false);
  assert.equal(shouldResumeKgBuild({ kg_building: true }), true);
  assert.equal(shouldResumeKgBuild({ kg_building: false }), false);
  assert.equal(shouldResumeKgBuild({}), false);
  assert.equal(shouldResumeKgBuild(null), false);
});

test("kgBuildFinished: 看 kg_building 而非 kg_ready（重抽已建库时 kg_ready 恒真）", () => {
  assert.equal(kgBuildFinished({
    kg_ready: true,
    kg_build: { status: "running" },
  }), false);
  assert.equal(kgBuildFinished({
    kg_building: true,
    kg_build: { status: "failed" },
  }), true);
  assert.equal(kgBuildFinished({ kg_building: true }), false);
  assert.equal(kgBuildFinished({ kg_building: false }), true);
  assert.equal(kgBuildFinished({ kg_ready: true, kg_building: true }), false);  // 关键：不误停
  assert.equal(kgBuildFinished({}), true);
  assert.equal(kgBuildFinished(null), true);
  assert.equal(kgBuildFinished(undefined), true);
});
