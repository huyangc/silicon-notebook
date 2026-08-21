# 模块化插件架构交付与 PR 流水计划

日期：2026-08-21
依据：[`modular-plugin-architecture-design-2026-08-21.md`](./modular-plugin-architecture-design-2026-08-21.md)

## 1. 决策

采用小步 PR 流水，不把设计文档中的一个 Phase 直接等同于一个 PR。

- 当前 Phase 0 作为第一个 PR；
- Phase 1–5 预计再拆约 20 个 PR，数量是规划上界而不是交付指标；
- Phase 1 的 3 个 PR 先冻结，后续只在前一 Phase 验收后细化下一 Phase；
- 不再等待 Fable5 逐 PR review；每个 PR 使用两路独立 subagent review；
- 两路 review 均无 P0/P1、CI 全绿、没有未闭合的阻断项后即可 squash merge；
- Phase 6 外部插件不进入本轮流水，只有出现真实第三方/独立发布需求时再立项。

这种拆法优于少量大 PR：行为等价、架构边界、迁移切换和旧路径删除可以分别验证；失败时回滚单个语义增量，不需要回滚整个阶段。

## 2. 流水模型

每个 PR 经过以下固定状态：

```text
fresh master
  → 实现 + 本地定向测试
  → commit/push Draft PR（CI 启动）
  → subagent A：架构/设计合同 review
  → subagent B：行为等价/测试/安全 review
  → 修复 findings + 重新 review/CI
  → P0/P1 = 0，P2 已修复或有明确接受记录
  → CI green
  → squash merge + 删除远端分支
  → 下一核心 PR 从新 master 开始
```

流水并发限制：

- 核心迁移链最多两个 PR 在制：前一个处于 CI/merge gate 时，可以准备后一个，但后一个在前一个合入前不开放正式 review；
- 不维护超过一层的 stacked PR；若后一个基于前一个 head 开发，前一个合入后立即 rebase 到 master 并重新跑门禁；
- 前端状态轨道可与核心轨道并行，但同一时间只允许一个修改 `frontend/app/page.tsx` 的 PR，避免状态所有权拆分互相冲突；
- migration、schema、同一 composition root 或同一主流程切换点不并行修改。

## 3. 每个 PR 的统一完成定义

PR 描述必须包含：

1. 对应设计章节和本 PR 要守住的不变量；
2. 明确的 scope / non-goals；
3. 无插件、关闭、失败三种路径中的适用等价证据；
4. 旧路径是否删除；若暂留，写明唯一删除 PR，不允许长期双路；
5. 测试命令、结果和 G1 耗时；
6. 依赖图/SCC、ports、repository reverse import、facade seat 结果；
7. rollback 方法；
8. 两路 subagent review 的 verdict 与已处理 finding；
9. 用户可见改动的前后端 parity；
10. 文档同步清单。

默认阻断规则：

- P0/P1 必须修复；
- P2 默认修复。若确实留待后续，必须有具体后续 PR、不会改变当前正确性的论证和用户接受记录；
- P3 可登记，不阻断；
- 任意 review 修复若改变设计合同、主流程顺序、scope、权限、预算、持久化或终态语义，必须重新触发两路 review；
- CI flaky 不按重跑即通过处理：先证明与变更无关或修掉根因。

## 4. PR 分解

### PR-00：Phase 0 架构底座（已合入）

状态：已通过两路 subagent review 与 CI，PR #541 squash merge。

范围：

- 清除 3 个 SCC 和 `ports.py` services 反向依赖；
- domain contracts、Extension SDK 基础合同、empty frozen registry；
- facade 只减不增、repositories→services 只降不升、空 registry 隔离等守卫；
- 文档与变异测试。

合入门：完整后端 G1、architecture contracts、前端测试/build、两路 subagent review、CI。

### Phase 1：Retrieval Contributor（3 PR，近期冻结）

#### PR-01：RetrievalContributorHost + Contract Kit

状态：已通过双路 subagent review 与 CI，并由 PR #542 squash 合入。

范围：

- 定义 point-specific context/result/budget/cancellation/provenance 合同；
- 建立 capability decision catalog；registry freeze 校验每个 required capability 的判定入口存在，但不冻结其实时 availability；
- 建立 baseline-preserving host、request-time availability、脱敏 failure/event；
- host 在零 contribution 时短路返回原 baseline，不走降级 adapter；
- Ask/Report 只接同一个共享 host 入口，不注册真实 contributor；
- 共享 host 通过 `selected_evidence` / `chunk_candidates` 两个类型化 invocation
  保留真实能力原有的不同物理时点，不把 generated-question 移到 MMR/fusion 后，
  也不把 selected-source graph 移到其 attestation/baseline guard 前；
- scope/provenance 复检必须 batch 化，禁止按 candidate 产生 N+1；核心 request
  cancellation 传播，插件自己的失败、超时或 local cancellation 才 fail-open；
- `app.bootstrap` 是把扩展 runtime 与 repository adapter 接起来的唯一外层组合根，
  workflow 只依赖 domain host port；availability probe 不得获得 reader/model/connection，
  执行 context 在确认至少一个 contribution available 后才构造；
- manifest 声明与实时 capability decision 共同裁剪每个 contribution 可见的窄端口；
  invocation 路由和 core-owned admission policy 随拓扑冻结，implementation 运行期改字段
  不得改写路由；强合同 contribution 可登记 atomic admission；
- proposal 在 core-owned deployment limit 后才进入一次权威 batch hydrate；插件提交的 value
  不得直接进入结果，畸形 enum/identity/provenance/token 必须完整 fail-open；
- contract kit 覆盖 no-op、异常、超时、取消、越 scope、非法 provenance、驱逐/重排 baseline、连接持有与 content-free event。

不做：selected-source graph 或 generated-question 迁移，不新增用户能力。

#### PR-02：Selected-source graph adapter

状态：已通过多轮双路 subagent review 与三条 CI，由 PR #543 squash 合入。

范围：

- 把 selected-source graph activation 注册为首个内建 retrieval contributor；
- Ask/Report 删除各自直接调用，统一通过 PR-01 host；
- 原样保留 attestation、rollout、baseline manifest、独立预算、scope drift、eviction 整段丢弃、fail-closed 边界和事件；
- 增加旧路径/新路径的特征化等价测试和 adapter 专属 contract tests。
- 首个真实 context factory 同批补 SQLite/PostgreSQL（含 pool-size-1）的连接持有
  conformance，证明 host 在拿任何 contributor fan-out 前已退出数据库 lease；PR-01
  只有 core-private probe 合同与 fail-closed mutation，不把尚未存在的 adapter 伪装成
  生产连接检测。

硬门：通用 host 合同只能是下界，不得把 selected-source graph 的更强合同降级。

#### PR-03：Generated-question supplement adapter

状态：已通过双路 subagent review 与三条 CI，由 PR #544 squash 合入。

范围：

- 将 generated-question query supplement 注册为 contributor；
- 逐字保留 off/shadow/on；shadow 返回精确 baseline；on 只追加原始 chunk，不驱逐、不重排；
- 保留 source ceiling、bounded read、offline-only index build、original-chunk provenance；
- 删除候选检索中的专用插件循环/双路入口。
- 在原 `_retrieve_chunks` 返回 `(scored, ids, matrix)`、进入 MMR/fusion 之前落
  `chunk_candidates` 的生产锚点与位置守卫；PR-01 只冻结 invocation 合同，避免在
  迁移前增加一个无法正确重建 ids/matrix 的假 host seam。

Phase 1 出口：Ask/Report 共享一个 host；两项真实 contributor 都通过 contract kit；无插件等价测试继续成立。

### Phase 2：Parser ProviderChain（2 PR）

#### PR-04：ProviderChain runner + acceptance contract

状态：实现中；已冻结 dormant 三环 DAG、两阶段副作用合同与生产路由物理断线，待双路 review 与 CI。

- 增加稳定 ID/DAG 链序、accept/reject-with-reason、warning、availability、取消和两段式副作用合同；
- contract kit 覆盖拒收降级、失败降级、禁止路由、验收前零持久化副作用；
- 组合现有 parser links，但暂不切换生产路由。

#### PR-05：Parser routing 一次性切换

- 内置解析、MinerU self-hosted、MinerU cloud 全部切到 ProviderChain；
- 保留 automatic routing、自托管不落公共云、workbook 对账、探针/带图重映射、fallback warning 真源集合；
- upload admission、system configuration、UI supported-format hints 仍由同一 capability registry 派生；
- 同一 PR 删除旧 dispatcher，不留双真源。

### Phase 3：Ask/Report application pipeline（3 PR）

#### PR-06：Ask reasoning/retrieval stage DTO

- 先拆 `reasoning_retrieval.run` / `ask_reasoning` 的不可变 stage input/output；
- 显式标注 scope、retrieval run、leaf slot、连接持有区间、取消点；
- 纯重构，回答、引用、trace、持久化和终态顺序不变。

#### PR-07：Ask auditors/observers

- 建立 answer auditor 和 completed observer host；
- 把现有 Ask completion 后处理迁成内建 observers；
- 回答终态先持久化/交付，observer 失败不能反转当前请求；
- facade 仅保留兼容调用 adapter，不增加插件 seat。

#### PR-08：Report stages + auditors/observers

- 拆 planning/generation/final audit 的明确 stage DTO；
- 保持 intent confirmation、mandatory topics、scope revalidation、all-section retrieval、并行 drafting、claim ledger、final editor 和 retry 语义；
- 引入 report auditor/completed observer，不允许重写正文或新增事实。

### Phase 4：Ingestion、Knowledge、MCP、export（5 PR）

#### PR-09：Ingestion element enricher

- 在解析完成、核心验证之前增加类型化 element contribution；
- 保持 source 生命周期、parser capability、图片/元素 provenance、失败和重试语义。

#### PR-10：Knowledge candidate projector

- 插件只产生候选，核心继续拥有 schema validation、审核、写事务和生命周期；
- 不允许插件直接写核心表或持有核心事务。

#### PR-11：`create_memory_mcp` capability bundle 拆分

- 先完成 A4 高内聚重构；
- 工具权限、公开 allowlist、agent token、Memory review gate 等行为不变；
- 本 PR 不开放通用 tool provider。

#### PR-12：统一 tool host / `agent.tool_provider`

- 仅在 §6.6 的四条前置红线全部可执行后开放；
- 核心派生公开工具集合，插件不能绕过授权、审阅或数据范围。

#### PR-13：Report exporter Provider

- 作为 single Provider 的首个真实消费者；
- 只读取完成报告的 public/export view；
- 原有导出格式和 UI 保持，新增用户格式时必须同 PR 做前后端 parity。

状态型插件 migration/conformance 模板不单独预建；等第一个真实带表插件出现时，作为该插件的前置 PR 按 §10.1/§10.2 落地，避免无消费者抽象。

### 并行轨道 F：`page.tsx` 状态所有权（预计 5 PR）

这些 PR 不依赖 Extension SDK，可在核心 Phase 1–4 期间流水推进，但彼此串行：

- **PR-F1**：source library hook（建议首个样板）；
- **PR-F2**：Ask session hook；
- **PR-F3**：report workspace hook；
- **PR-F4**：KG workspace hook；
- **PR-F5**：collection state + modal manager；若 diff 无法在一次 review 中完整验证，自动拆成 F5/F6，不为保持 PR 数量强行合并。

每个 hook 都必须显式处理 notebook/user identity、cleanup/cancellation、权限重验、删除 tombstone、轮询终止；不引入新全局状态库，不直接读取其他 hook 的内部 setter。

### Phase 5：Frontend workspace registry（2 PR）

#### PR-14：Build-time UI registry + parity guard

- 建立 `frontend/features/extension-sdk`；
- `/system/extensions` 只投影脱敏实时 availability；
- build-time contribution、server capability、permission、UI mode 四门同时成立才展示；
- 先只定义 `side_panel` / `source.detail_section`，零真实 UI 插件时页面逐字等价。

#### PR-15：首个全栈 UI 插件样板

- 从已有真实内建插件中选择一个，而不是为了样板制造新功能；
- 同 PR 提供 backend capability/API 与 frontend contribution；
- 插件组件只拿窄 API/navigation/context，不能拿 workspace 全部 setters；
- 完成后再评估是否需要新的 slot，不预建 `main_tab` / `toolbar_action`。

## 5. Subagent review 组织

每个 PR 默认两路 reviewer，均只读，不直接修代码：

### Reviewer A：架构与设计合同

- 逐项对照本 PR 引用的设计章节；
- 检查依赖方向、SCC、facade、registry/manifest、顺序和 availability；
- 检查是否新增万能 context/hook、service locator、第二真源或长期兼容双路；
- 对迁移 PR 检查旧路径是否按计划删除。

### Reviewer B：行为、安全与测试

- 检查 baseline 等价、scope/权限/provenance、预算、取消、连接持有、终态顺序；
- 检查 disabled/unavailable/failure/no-plugin；
- 检查测试落点、变异有效性、G1/G2 分类和前后端 parity；
- 对用户可见 PR 检查错误文案、UI mode 和 API/UI 可达性。

前端主导 PR 可把 Reviewer B 替换为 frontend/full-stack reviewer。schema PR 则增加第三路 SQLite/PostgreSQL migration/conformance reviewer。

Review 输出统一为：finding severity、绝对路径/行号、违反的不变量、可复现证据、建议修复、最终 PASS/BLOCK verdict。

## 6. CI 与合入策略

- PR 先以 Draft 触发 CI；subagent review 与 CI 并行；
- 每次 push 后等待所有 required checks 对应最新 head SHA，旧 SHA 的绿灯不计；
- required checks 全绿、review verdict PASS 后转 Ready 并 squash merge；
- 合入由 Codex 执行，但只合入当前计划中的准确 PR，不批量合并队列中的其他 PR；
- merge 后验证远端 master 包含 squash commit，再创建下一核心分支；
- CI 红时先定位并修复，不用重复 rerun 掩盖确定性失败；外部服务检查无法读取日志时，记录检查 URL 和需要人工处理的具体项；
- 合入后若出现回归，优先 revert 单个 squash commit，再在新 PR 修复，不在 master 上直接补丁。

## 7. 近期执行顺序

1. PR-00：完成两路 subagent review，开 Draft PR，CI green 后合入；
2. PR-01：RetrievalContributorHost + Contract Kit；
3. PR-02：selected-source graph adapter；
4. PR-03：generated-question supplement adapter；
5. Phase 1 出口 review 后，再冻结 PR-04/05 的具体 diff 范围；
6. 前端 PR-F1 可在 PR-01 合入后启动独立流水，但不得与另一个 `page.tsx` PR 并行。
