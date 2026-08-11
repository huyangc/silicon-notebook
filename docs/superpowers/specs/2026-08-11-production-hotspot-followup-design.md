# 生产热点整改第三轮：残余在线复杂度收敛

日期：2026-08-11
基线：`origin/master` `39965b15`（已包含 PR #493 与 PR #502）

## 目标与边界

本轮只处理前两轮整改后仍在线可达、且复杂度随 notebook 图规模增长的四个残余形状。
优先级判定同时满足：生产规模可放大、调用链真实可达、现有上限只封输出而没有封住工作量。

纯性能改造必须保持结果等价。治理准入不是检索截断：超过安全规模时请求明确拒绝，不能只读
前缀后把不完整检测伪装成完整结果。Ask / Deep Report 的候选、PPR 分数、排序和引用不得因此
改变。

本轮不借机处理已经登记但需要独立数据模型或产品语义的项目，例如 review queue 全局精确排序、
promotion exact seed、relink Rule-2 多模式匹配，以及近似化的图检索预算。这些项目不能混进
一次“性能等价”变更。

## Lane 1：后台任务从“线程内等信号量”改为固定执行容量

### 现状

`background_jobs.submit()` 为每个维护任务立即创建一个 daemon thread，线程启动后才等待
`BoundedSemaphore`。信号量限制了正在执行的任务数，却没有限制等待线程数；突发提交量为 `Q`
时，进程仍会创建 `O(Q)` 个线程及其栈和 Context。

### 方案

- 重活与轻活继续保持两个独立池，容量继续由现有 Settings 决定。
- 每个池使用固定数量 worker；提交只入队，不再为等待者创建线程。
- 返回兼容 handle，保留现有调用方和测试使用的 `join()` / `is_alive()` 语义。
- 在调用线程执行 `copy_context()`，worker 执行时进入该快照，保持 per-user 模型、日志归属和
  pending 通知语义。
- 排队仍位于 `diagnostics.job_scope` 外，异常隔离、脱敏排队日志和两个池互不饿死的合同不变。
- `ask-*` / `report-*` 继续沿用现有路径；本轮不把交互任务并入维护池。

复杂度由 `O(Q)` 等待线程降为 `O(pool_capacity)` worker 线程 + `O(Q)` 轻量队列项。

## Lane 2：viz 辅助索引持久化与有界子图提取

### 现状

紧凑 `VizEdgeSet` 已消除 JSON 常驻，但首次视图仍惰性执行节点度排序 `O(V log V)` 与边源排序
`O(E log E)`。`_unified_graph_bounded` 为返回少量核心节点仍构造全节点 membership mask 并扫描
全部边；逐邻居读取 edge type 还可能重复扫描同一邻接段。

### 方案

- 构建 `viz.npz` 时同时持久化稳定度序、按 `(src, dst, original_edge_index)` 排列的边
  permutation 与 source indptr；新产物加载后直接使用，旧产物保留一次性惰性回退。
- 加载时严格校验新辅助数组的形状、置换、三键顺序与分段；缺少辅助键的旧 artifact 仍有效，
  旧版仅按 source 排列或损坏的辅助数组不参与事实数据，安全退回一次性惰性派生。核心 degree /
  label / adjacency 数组自身错位或类型非法则把 artifact 判为 unusable，不让辅助安装从请求路径抛错。
- bounded core 先从持久度序取 `limit` 个位置，再对 kept source × kept target 在对应 source 段内
  间接二分 pair range，只收集实际命中的 original edge ids 并按原序恢复输出；不构造长度 `V` 的
  mask，也不因一个 kept source 是高出度 hub 就扫描其完整邻接段。
- edge type 同样按不同有向 pair 批量二分，pair range 内取最大的 original edge index，保持“重复
  有向边最后一条 edge type 生效”的历史语义。

节点集合、节点顺序、边集合、边顺序、edge type、`total_*` 与 `truncated` 由 old-vs-new
differential tests 逐项钉住。令 `K` 为保留节点数、`d_s` 为 source 段长度、`M` 为实际命中边数，
在线工作量变为 `O(K log K + Σ_{s∈K} K log d_s + M log M)`；它与未命中的 hub 出边数仅呈对数关系。
旧 artifact 只在首次访问承担一次兼容索引构建成本。

## Lane 3：多 participant CSR 一次组合，严格复现逐轮语义

### 现状

`_scale_combined_graph` 对第 2..N 个 participant 逐次 `splice_csr`。每次都复制和归一化当前累计
矩阵，累计成本随 participant 数和累计 nnz 呈超线性增长。

### 等价性陷阱

现有 `splice_csr` 每一轮把累计矩阵重置为结构权重 `1`，再与当前 participant 的结构相加后列归一。
因此多个 participant 共享同一坐标时，最终权重不是简单的“所有出现次数之和”：在最后一个
participant 之前的重复会先折叠为一次，只有最后一轮与既有结构重叠时才形成权重 `2`。

### 方案

- 一次建立最终 `combined_ids` 与各 participant 的 index remap，保持逐库追加的 id 顺序。
- 将最后一个 participant 之前的所有坐标做布尔并集（data 统一为 `1`）。
- 再叠加最后一个 participant 的结构坐标，最后只执行一次列归一化。
- 单 participant identity 快路径、active delta 拼接和 cross-layer bridge 顺序不变。

随机重叠图、重复边、空图、不同 participant 顺序均与历史逐轮 oracle 对比：ids 精确相等，CSR
结构精确相等，数值按既有浮点容差相等；再由 PPR hit id/score 序列回归确认检索结果不变。

## Lane 4：冲突治理关系规模准入

### 现状

对象数已有 `KG_CONFLICT_MAX_OBJECTS` 准入，但对象通过后仍会一次性加载 notebook 的全部关系
薄投影。关系数与对象数不成固定比例，高密度或重复佐证图仍可能在后台任务内造成无界内存峰值。

### 方案

- 在进入后台任务和读取关系正文前执行精确 relation count 准入。
- 新增经校验的部署设置 `KG_CONFLICT_MAX_RELATIONS`；精确数值只登记在
  `docs/product-and-api.md` / `_zh.md`。
- 超限返回明确 409，并记录只含稳定 reason 的事件；不读取前 N 条继续跑，也不返回“部分
  检测成功”。自动触发路径采用同一准入。
- 准入通过后，检测算法、候选顺序、LLM 裁决、写入和事件语义不变。

该保护不参与 Ask / Deep Report 检索，也不改变已有 KG；代价是超限 notebook 必须先离线治理或
提高经容量评估后的部署 rail，不能通过在线端点执行完整冲突检测。

## 验证与交付

1. 每条 lane 先跑目标测试与 old-vs-new differential tests。
2. 运行 `scripts/check.sh`；若改动触及前端合同，再单独确认 production build。
3. 未参与对应实现的高能力 subagent 做检索等价性、并发/资源安全和代码质量交叉 review。
4. 修复所有 actionable findings 后提交单一分支，创建 PR。
5. PR 进入 ready 状态后等待全部 required CI；失败则读取日志、修复并重跑，全部绿色后合入。

实施分工：固定执行容量由平衡型模型实现；viz 与 CSR 等价性由高能力模型实现；主 agent 负责
跨 lane 集成、文档、全量验证与交付。最终 review 使用未参与相应实现的 subagent，避免自审。
