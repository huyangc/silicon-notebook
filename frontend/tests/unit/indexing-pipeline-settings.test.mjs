import test from "node:test";
import assert from "node:assert/strict";

import {
  describeIndexingPipelineState,
  indexingPipelineConfirmMessage,
  indexingPipelineIdsEqual,
  notebookIndexingPipelineReadOnlySummary,
  normalizeIndexingPipelineId,
  selectedIndexingPipelineOption,
} from "../../app/indexing-pipeline-settings.ts";

function projection(overrides = {}) {
  return {
    pipeline_id: null,
    version: "builtin-v1",
    available: true,
    missing: false,
    pending: false,
    options: [
      {
        pipeline_id: null,
        label: "内建管线",
        description: "builtin",
        version: "builtin-v1",
        available: true,
        selected: true,
      },
      {
        pipeline_id: "plugin.arxiv",
        label: "arXiv 管线",
        description: "plugin",
        version: "2026.08",
        available: true,
        selected: false,
      },
    ],
    ...overrides,
  };
}

test("normalizeIndexingPipelineId folds nullish to builtin empty id", () => {
  assert.equal(normalizeIndexingPipelineId(null), "");
  assert.equal(normalizeIndexingPipelineId(undefined), "");
  assert.equal(normalizeIndexingPipelineId("plugin.arxiv"), "plugin.arxiv");
  assert.equal(indexingPipelineIdsEqual(null, ""), true);
});

test("selectedIndexingPipelineOption resolves builtin and plugin ids", () => {
  assert.equal(selectedIndexingPipelineOption(projection())?.label, "内建管线");
  assert.equal(
    selectedIndexingPipelineOption(projection(), "plugin.arxiv")?.label,
    "arXiv 管线",
  );
});

test("describeIndexingPipelineState distinguishes missing pending and unavailable", () => {
  assert.match(
    describeIndexingPipelineState(projection({ missing: true, available: false })).detail,
    /切回内建/,
  );
  assert.match(
    describeIndexingPipelineState(projection({ missing: true, available: false })).detail,
    /重建全库索引/,
  );
  assert.match(
    describeIndexingPipelineState(projection({ pending: true })).detail,
    /旧索引继续可读/,
  );
  const failed = describeIndexingPipelineState(projection({
    pending: true,
    rebuild_status: "failed",
  }));
  assert.match(failed.detail, /重试当前管线/);
  assert.equal(failed.canRetry, true);
  assert.match(
    describeIndexingPipelineState(projection({
      pipeline_id: "plugin.arxiv",
      available: false,
    })).detail,
    /当前不可用/,
  );
  assert.equal(describeIndexingPipelineState(projection({ pending: false })), null);
});

test("indexingPipelineConfirmMessage states the full rebuild consequence", () => {
  assert.match(indexingPipelineConfirmMessage({ label: "arXiv 管线" }), /重建全库索引/);
  assert.match(indexingPipelineConfirmMessage({ label: "arXiv 管线" }), /旧索引继续可读/);
});

test("notebookIndexingPipelineReadOnlySummary exposes the projected pipeline for readers", () => {
  const summary = notebookIndexingPipelineReadOnlySummary({
    indexing_pipeline_id: "plugin.arxiv",
    indexing_pipeline_version: "2026.08",
    indexing_pipeline_available: true,
    indexing_pipeline_missing: false,
    indexing_pipeline_pending: true,
  });
  // summary 路径拿不到 descriptor label,上通用界面词——绝不把 `plugin.arxiv`
  // 这种内部 id 当文案上屏(评审 P2:词汇守卫扫不到运行时数据)。
  assert.ok(!summary.label.includes("plugin.arxiv"), summary.label);
  assert.match(summary.label, /部署插件提供的索引管线/);
  assert.match(summary.label, /v2026\.08/);
  assert.match(summary.detail, /正在重建全库索引/);
});
