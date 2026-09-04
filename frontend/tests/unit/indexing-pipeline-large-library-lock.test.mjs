import assert from "node:assert/strict";
import test from "node:test";

import {
  describeIndexingPipelineState,
  indexingPipelineOptionLocked,
} from "../../app/indexing-pipeline-settings.ts";

const base = {
  pipeline_id: "deploy.custom",
  version: "v1",
  available: true,
  missing: false,
  pending: false,
  rebuild_status: "failed",
  options: [],
};

test("批 3·W3:大库 failed 态收敛 canRetry、保留 canRevert(恢复出口)", () => {
  const locked = describeIndexingPipelineState({
    ...base,
    large_library_locked: true,
  });
  assert.equal(locked.canRetry, false);
  assert.equal(locked.canRevert, true);
  assert.match(locked.detail, /暂不支持重试自定义管线/);

  const unlocked = describeIndexingPipelineState({
    ...base,
    large_library_locked: false,
  });
  assert.equal(unlocked.canRetry, true);
  assert.equal(unlocked.canRevert, true);

  // codex #674 R1:内建恢复重建失败态(pipeline_id 空)——重试即内建目标,
  // 服务端放行,锁定库上也必须保持可重试,且不误导去「切回内建」。
  const builtinFailed = describeIndexingPipelineState({
    ...base,
    pipeline_id: null,
    large_library_locked: true,
  });
  assert.equal(builtinFailed.canRetry, true);
  assert.doesNotMatch(builtinFailed.detail, /暂不支持重试自定义管线/);
});

test("批 3·W3:锁定只作用于非内建目标——内建 radio 保持可点", () => {
  const projection = { ...base, large_library_locked: true };
  assert.equal(indexingPipelineOptionLocked(projection, "deploy.custom"), true);
  assert.equal(indexingPipelineOptionLocked(projection, ""), false);
  assert.equal(indexingPipelineOptionLocked(projection, null), false);
  assert.equal(
    indexingPipelineOptionLocked({ ...base, large_library_locked: false }, "deploy.custom"),
    false,
  );
  assert.equal(indexingPipelineOptionLocked(null, "deploy.custom"), false);
});
