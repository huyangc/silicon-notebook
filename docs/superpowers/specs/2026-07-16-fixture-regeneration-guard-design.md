# 契约夹具重生成门禁修复（Repository contract fixtures）

日期：2026-07-16
状态：已批准，待实现

## 问题

`scripts/generate_repository_contract_fixtures.py` 的 `_assert_baseline_sources()` 要求
`backend/app/**/*.py` 每个文件的路径与字节都精确等于 `SOURCE_COMMIT = "3334626"`，
否则 `raise SystemExit("refuse fixture regeneration after backend/app path changes")`。

- 经验证：在当前 HEAD 调用该守卫必然抛 `SystemExit`。
- `git rev-list --count 3334626..HEAD -- backend/app` = 58，即基线固定以来已有 58 个合法提交动过 `backend/app`。
- 守卫在四处被调用：`main()`、`generate_v9_fixture()`、`generate_ask_goldens()`、`generate_api_contract()`。
  因此 `main()` 及其记录在案的重生成流程永久不可用。

**已造成的实际损害**：clean HEAD 上 `test_repository_surface_manifest.py` 有 2 个测试失败
（`test_static_repository_consumer_scan_matches_manifest_exactly`、
`test_compatibility_exports_and_import_consumers_are_complete`），因为 `facade_surface.json`
已随新增 `test_knowhow_*`/`test_paper_meta_*`/`test_memory_*` 消费者漂移，却因守卫无法重生成。

## 根因

脚本诞生于 PR #241（God 对象 → 组合式重构），此后从未改过（`git log --follow` 仅一个提交）。
设计文档明确：夹具生成器必须「针对**未改动的** runtime」运行，`3334626` 是重构前基线。
守卫的作用是：保证这批夹具**一次性**、逐字节地刻画重构前行为，用来证明重构行为不变。
它本是「跑一次就退役」的工具，没人 bump 是因为 bump 从来不在设计里。真正被漏掉的，是在
重构落地之后，把守卫与「这些夹具已演化为活契约」这一事实做对账。

## 关键区分：冻结 vs 活契约（三类，非两类）

守卫把三种不同的东西混为一谈——**调查后修正**了初判：

- **活契约（characterization contracts，应由 main() 对现网重生成）**：`ask_responses.json`、
  `api_contract.json`、`repository_v9` 的投影（`expected_snapshot.json` / `manifest.json`）、
  phase 契约。每个消费者测试都断言 **现网 runtime == 冻结 JSON**
  （如 `app.openapi() == frozen`、`collect_ask_goldens() == frozen`、把冻结 `baseline.db`
  经当前代码 migration 后 `normalized_repository_snapshot() == frozen`）。行为/schema 演化时
  它们**必须**被重新祝福，历史上 `2357066`/`efea3b8`/`48fab84` 正是这么做的——绕过守卫、
  直接调用无守卫的 collector（守卫只压在 file-writer 上，不压在 `collect_*` /
  `normalized_repository_snapshot` 上）。

- **冻结自基线（守卫承重，正常运行**不**重生成）**：
  - `repository_v9/baseline.db` —— 由重构前代码产出的 **schema v9** 数据库。当前代码（v18）
    无法复现它；`generate_v9_fixture()` 今天跑只会产出 v18 库、毁掉全部意义。main() 只**重放**它
    来刷新 v9 投影。
  - `facade_surface.json` —— **刻意冻结**。`backend/tests/test_repository_surface_manifest.py`
    （3687 行）用 **298 组 `*_ALLOWED_*` allowlist + 221 条 `EXPECTED_PATCH_DELTAS`** 显式登记
    自基线以来的每一处漂移，构成可审阅的审计轨迹。**重生成它 = 推翻整套审计体系**——这不是
    这次的活儿。它的漂移按 allowlist 模式维护，不靠重生成。

> 初判把 `facade_surface.json` 当成活契约是错的；surface_manifest 的 allowlist 体系证明它是
> 冻结自基线的。这一发现改变了范围：main() 只重生成活契约，facade/db 保持冻结。

## 决策（方向 A）

拆开守卫混淆的两个概念，把守卫收敛到只保护 `baseline.db`：

1. **保留 `SOURCE_COMMIT = "3334626"`** 作为 provenance/血缘常量（改注释即可）。
   它被写进每个夹具的 `source_commit` 字段，并在 3 处测试里硬断言 `== "3334626"`
   （`api_contract:278`、`v9_fixture:26`、`ask_golden:50`），还是 v9 库真实出身。**不 bump。**
   —— 为什么不选 bump（方向 B）：会谎报 v9 库出身、破坏这 3 处断言，且下个 `backend/app`
   提交就再次失效（治标不治本，是个跑步机）。

2. **守卫从「全拦」收敛到「只拦 re-baseline」**：从 `main()`、`generate_ask_goldens`、
   `generate_api_contract` 移除 `_assert_baseline_sources` 调用（这些只重生成活契约）。
   守卫仍保留在 `generate_v9_fixture()`（冻结 baseline.db 的重建路径，退役/一次性），
   并在 `main()` 的 `--rebaseline` 分支显式再调用一次（护住 facade 写入）。

3. 新增 `refresh_v9_snapshot()`（无守卫）—— 复制**冻结**的 `baseline.db` + storage 到临时目录，
   经当前代码打开、`normalized_repository_snapshot()` 重放，写出 `expected_snapshot.json`
   并刷新 `manifest.json`（`database` 哈希取自未改动的冻结库、`expected_snapshot` 哈希更新、
   `storage_files` 不变、`schema_version` 仍 9）。**绝不重写 baseline.db。** 与既有重放测试同构。
   保留 `generate_v9_fixture` 的**函数名**（`REQUIRED_GENERATOR_CALLABLES` 断言需要）。

4. **`main()` 恢复可跑**：默认对当前 runtime 重生成**活契约**（ask / api / phase）+ 重放刷新
   v9 快照；**不**触碰 `facade_surface.json` 与 `baseline.db`（保持冻结）。新增 `--rebaseline`
   flag：受守卫（只在检出到 `3334626` 才通过），重生成两个冻结产物（facade + baseline.db）。

5. **文档**：改写脚本 docstring、脚本内嵌与 committed 的 `repository_v9/README.md`，说清三类产物
   与工作流；`facade_surface.json` 的维护走 surface_manifest allowlist、不走重生成。

6. **修 master 上 2 个红测试**（`test_static_repository_consumer_scan_matches_manifest_exactly` /
   `test_compatibility_exports_and_import_consumers_are_complete`）：按 surface_manifest 自身的
   allowlist 模式，为最近 knowhow/paper-meta/memory PR 引入、尚未登记的 facade 消费点补 allowlist
   条目（新增一组带注释的 `*_ALLOWED_*`）。**不**重生成 facade_surface.json。

## 保住的不变量

- 三处 `== "3334626"` 断言：重生成的活契约仍带 `source_commit="3334626"`（SOURCE_COMMIT 未变）→ 全绿。
- `baseline.db` 字节不变；`manifest.database` 哈希不变；`storage_files` 不变；schema_version 仍 9。
- `facade_surface.json` 字节不变（冻结）；surface_manifest 的 298 allowlist + 221 delta 体系不动，只**追加**。
- `REQUIRED_GENERATOR_CALLABLES`（含 `generate_v9_fixture`）、`normalized_repository_snapshot` 注解不变。
- `test_v9_fixture_replays_through_the_current_repository` 语义不变（仍是重放冻结库）。

## 落地范围与验证

- 端到端证明：实际跑修好的 `main()`（默认档），确认活契约（ask / api / phase / v9 快照）重生成后
  与 committed **逐字节一致**（当前它们与现网同步 → 零 churn，证明脚本可用且不误伤）。
- 补 allowlist 后，全套契约测试全绿：
  `test_repository_surface_manifest / _phase_contracts / _v9_fixture / _ask_repository_golden /
   _api_contract / _facade_contract / _callers_static / _snapshot_verifier`。

## 风险

- 编辑生成器脚本会移动其行号，但脚本本身被 surface_manifest 的消费者扫描**排除**（`path == GENERATOR`
  continue），且只做 `REQUIRED_GENERATOR_CALLABLES` 子集断言（保留 6 个函数名即可）→ 生成器改动对
  surface_manifest 安全。
- 补 allowlist 面向的是脆弱的 3687 行文件（见记忆 surface-manifest-line-shift-gotcha）；只**追加**
  新 allowlist 组、不移动既有行，改完整文件跑通验证。
- 若跑 `main()` 意外牵出 api/ask/snapshot 的额外漂移（当前它们碰巧同步），按同一「无守卫 collector
  重放」口径重新祝福，并在提交说明里说清哪些契约被刷新、为什么。

## 实现补记（build 中发现，2026-07-17）

1. **readiness 门 503**：`generate_api_contract` 的 `_serialization_contract` 经 TestClient 打真实
   路由，但启动就绪门（PR#250，晚于生成器冻结）默认 not-ready → 503。测试靠 conftest `mark_ready()`
   规避；脚本作为纯进程无 conftest。修：在 `generate_api_contract` 里 `readiness.mark_ready()` 前置
   （与 conftest 同口径）。这正是断裂守卫导致的 bit-rot——生成器从没机会跑到、也就没跟上就绪门。

2. **callers_static 冻结行号 pin（关键约束）**：`test_repository_callers_static.py` 在 48 处 pin 了
   生成器**内部行号**（`INDEPENDENT_SQL_SITES` / `SQLITE_CONNECT_SITES` / `FACADE_CLASS_IMPORT_SITES`
   / `EXPECTED_REMEDIATION_SITES`，行号 40–1992），且**无**行不敏感杠杆。故生成器改动采「**最小行位
   移**」：所有 >1992 行的新增（`refresh_v9_snapshot` 定义、`main` 重构）放到文件底部；≤1992 行的编辑
   全部 net-zero（docstring 保持 6 行、内嵌 README 保持 10 行、guard→注释 1 换 1）。结果：48 个 pin
   全不动，生成器改动对 callers_static 零影响。

3. **merge_dbs.py 预存漂移（PR#276，用户批准折进本 PR 单独 commit）**：`scripts/merge_dbs.py` 用了
   ~15 处裸 SQL/connect + `test_merge_dbs.py:30` import facade，均未登记进冻结 allowlist，导致 5 个
   契约测试在 master 上**先于本改动**就红（surface_manifest 2 + callers_static 2 + architecture 1）。
   按各自的 allowlist 模式追加登记：surface_manifest 加 `MERGE_DBS_ALLOWED_IMPORTS`；callers_static 的
   `INDEPENDENT_SQL_SITES`（+21）与 `SQLITE_CONNECT_SITES`（+2）加 merge_dbs 块。追加这些条目使
   callers_static 自身的 facade import 行位移（690→728），按既有约定同步更新 surface_manifest 里那处
   line-sensitive 的 import pin + 注释。

**最终验证**：默认档 `main()` 跑通、活契约零 churn、corrupt-and-restore 证明确实重写；`--rebaseline`
在 HEAD 正确 refuse、冻结产物不动；全套 158 个契约测试全绿；3511 用例零收集错误。
