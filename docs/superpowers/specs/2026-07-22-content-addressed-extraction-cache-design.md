# 内容寻址抽取缓存设计

日期：2026-07-22

## 问题

大规模使用时，内容完全相同的文件会被反复完整处理一遍。同一篇论文/规范被 N 个用户
上传，或同一批源被 `reparse`/`reextract` 重跑，每一次都重新付出全部 LLM 与 embedding
代价。

## 现状勘察

### 各阶段真实成本（本机事件日志，全部 `status=done` 的 pipeline 事件）

```
stage          n     median        p90        max      总计(s)
extract      307          2     154957     573264      19973.9   ← 占 pipeline 93.4%
embed        318          0       3346    1270023       2377.4   ← 见下方注
parse        319          1        109      48034        203.8   ← 占 pipeline  1.0%
pipeline     314          7     159233     675458      21396.2
```

在 275 个 `parse` 与 `extract` 均成功的配对样本上：`extract` 总耗时是 `parse` 的
**182 倍**，逐源比值中位数 2704x，74% 的源上 extract 更慢。即使取 parse 最慢的样本
（明显走了 MinerU，28.6s / 33.7s），extract 仍是 431s / 414s，仍有 12~15 倍差距。

> **embed 的 2377s 不可与其它阶段直接相加**：它在后台线程中与 extract 并发执行
> （`process_source` 里 `_embed_bg`），并非串行墙钟时间。缓存它省下的主要是
> **embedding API 配额与并发资源**，而非流水线时长。优先级据此排在 extract 之后。

原因是调用次数的量级差异：parse 每源一次，extract 是 **每窗口一次 × N 窗口 ×
gleaning 轮数**。文档越大差距越大。

> 设计过程中最初假设 parse（MinerU）最贵，被上述实测推翻。parse 缓存因此被移出范围。

### 已有资产

- `sources.file_hash` 已存 `sha256(文件字节)`，UI 上传路径已填充
  （`backend/app/services/source_ingestion.py`）。
- 唯一消费者是离线 `batch_ingest` 的 `already_ingested`，且是**同 notebook 内整源跳过**。
  UI 上传路径完全不查该哈希——两条路径行为不一致。
- `sources` 表**没有 `file_hash` 索引**，`source_id_by_hash` 是全表扫。
- `CacheBackend` Protocol（`get`/`put`）与 `NoCacheBackend` 已存在于
  `backend/app/core/llm_cache.py`——可插拔缓存的架构已就位。
- `LLMCache`（SQLite）已存在但**只挂在 `OpenAICompatibleClient` 上**，且**默认关闭**
  （`LLM_CACHE_ENABLED=false`），**无任何淘汰机制**。

### 关键发现：占 93% 的路径根本没接缓存

KG 抽取走的是 `KGClient`（`backend/app/services/kg/client.py`），而 `llm_cache` 的挂载点
`_get_cache()` 只在 `OpenAICompatibleClient`（`backend/app/core/llm.py`）里。打开
`LLM_CACHE_ENABLED` 对 KG 抽取**毫无作用**。

### 关键发现：抽取 prompt 是内容的纯函数

`_prompt(labeled_text, section_path, doc_type, base_filter)` 全文已核对，**不含**
source_id、文件名、时间戳或任何随机成分：

- `labeled = "\n".join(f"[{i}] {e.text}" ...)` —— 标号是**窗口内局部序号**
- `make_windows` 只依赖文本内容与章节结构
- `base_filter` 只是 bool（base / personal 两态）

因此：**同内容 + 同 doc_type + 同 tier → 逐字节相同的 prompt → 缓存必命中**。
`doc_type` 与 `base_filter` 参与 key 是语义正确的（不同文档类型、不同层级的抽取
重点本就应当不同），代价是相应场景不命中，接受。

## 决策

| 维度 | 决策 |
|---|---|
| 复用边界 | **全局跨用户共享**。内容上不泄密（复用方自己手里本就有这份文件）；唯一副作用是「秒完成」构成存在性侧信道，内网部署可接受。 |
| 复用层次 | 在**外部调用边界**缓存，产物仍各自落库。不共享行 → 删除、重解析、权限、归属完全不受影响。 |
| 范围 | KG 抽取缓存（93%）+ embedding 缓存（11%）。**parse 缓存不做**（1%）。 |
| 缓存组件 | `diskcache==5.6.3`，经 `CacheBackend` Protocol 适配。 |
| 部署形态 | 单机多用户、单后端进程 → SQLite 后端足够。 |
| 默认状态 | **默认开**（优化的目的），保留 env 逃生口。 |

### 为什么用 diskcache 而非自研淘汰

顾虑是它 12 个月无新发布、文档只测到 CPython 3.10，而本项目跑 3.13。实测
（Python 3.13.11，8 线程并发写，模拟 `kg_extract_workers`）：

```
diskcache 5.6.3 on Python 3.13.11
并发写 150×20KB 耗时 101ms  (1491 写/秒)
volume=852KB (上限 1000KB)  条目数=40/150 → LRU 裁剪生效
最近写的还在: True   最早写的已淘汰: True
```

真实写入速率约每秒零点几次（每次 LLM 调用本身耗时数秒到数十秒），性能余量 3~4 个
数量级。它是纯 Python + SQLite，无 C 扩展、无 ABI 风险。

自研 LRU + size limit + TTL 约 100 行，但并发裁剪、裁剪抖动、误删热条目这类缺陷
很难测。而 `CacheBackend` Protocol 已经把风险隔离好了：若 diskcache 将来失效，
替换为自研实现是**局部改动，不动任何调用点**。锁定版本 `diskcache==5.6.3`。

## 架构

```
kg_llm()        → CachedKGClient(inner, backend)     # 覆盖 初抽 / gleaning / refine
make_embedder() → CachedEmbedder(inner, backend)     # 覆盖全部 6 个 embed_texts 调用点
                          ↓
                  CacheBackend (已有 Protocol)
                          ↓
                  DiskCacheBackend (~20 行适配)
```

装饰器模式，`extract_graph` / `extract_window` / refine / gleaning / 6 个 embed
调用点**全部零改动**。

`OpenAICompatibleClient._get_cache()` 亦改为返回 `DiskCacheBackend`（ask 路径一并
受益，全系统只有一套缓存机制）。旧的 `llm_cache.db` 不迁移、直接弃用——缓存冷启动
重建即可。

### 配置

沿用现有 `LLM_CACHE_ENABLED` 作为**总开关**（避免引入第二个开关概念），默认值由
`false` 改为 **`true`**；`LLM_CACHE_PATH` 改指向 diskcache 目录。新增
`LLM_CACHE_SIZE_LIMIT`（字节）与 `LLM_CACHE_TTL_DAYS`（默认 90）。按项目既有约定，
所有新配置项的环境变量映射必须用 `validation_alias`——pydantic-settings v2 下
`Field(env=)` 静默失效。

### 缓存键

| | key | 依据 |
|---|---|---|
| KG | `sha256(model + messages全文 + schema_hint)`，复用现有 `cache_key()` | prompt 已核对为内容的纯函数 |
| embed | `sha256(model + 单条 text[:embed_truncate_chars])`，**per-text 而非 per-batch** | 见下两条 |

embed key 的两个必须点：

- **per-text 而非 per-batch**：否则批次边界一变（`embed_batch_size` 调整、或上游
  chunk 数量变化）就全部 miss。
- **必须对截断后的文本取哈希**：`DashscopeEmbedder.embed_texts` 内部先执行
  `t[:embed_truncate_chars]`（默认 2000）才发给 API。若用原文取哈希，两个前 2000
  字符相同的长文本会各占一条缓存却拿到完全相同的向量——白白损失命中率。对截断后
  文本取哈希同时自动捕获了 `embed_truncate_chars` 配置变更的影响。

embed 缓存**存储 API 原始维度的输出**。4096→1024 的运行时维度截断发生在消费侧
（原向量作为真相源保留不改写），因此缓存层与维度决策相互独立，不受其影响。

副产品：**prompt 一改，缓存自动全冷**。不需要维护版本号，正确性天然成立——这是
调用级缓存优于「抽取产物缓存」的关键之处。

### 注入点

- KG：`self.kg_llm()`（`backend/app/services/source_ingestion.py`），per-user KG 模型
  配置在此解析。装饰器**必须透传 `control` 属性**——`run_extraction` 靠
  `getattr(kg_client, "control", None)` 做取消与并发控制，吞掉它会破坏取消语义。
- embed：`make_embedder()` 工厂（`backend/app/services/embedding.py`）。注意与
  `bind_model_status_identity` 的包装次序，不得破坏 model_status 身份绑定与
  model_error 上报通道。

## 三个安全阀

### 1. 绝不缓存空响应（最高优先级）

`KGClient.chat_json` 在输出预算烧光时返回 `"{}"`（`kg/client.py`，即
`reasoning-empty-content-degeneration` 记录的那个退化）。**缓存 `"{}"` 等于把一次
偶发退化永久固化。** 拒绝写入：空串、`"{}"`、`safe_json()` 解析后为空 dict、以及
任何异常路径。

### 2. 测试隔离

已有实测教训：带真 `.env` 跑后端全量会被 `llm_cache.db` 污染出大规模假失败。
conftest 必须强制 `NoCacheBackend`（或临时目录），且该隔离本身要有测试。

### 3. 装饰器属性透传

见上文注入点。`control` 之外的属性一并透传（`__getattr__` 兜底）。

## 生命周期

代码侧的所有变更**都不需要 TTL**，key 自带版本语义：

| 变化 | key 捕获 | 结果 |
|---|---|---|
| 改 prompt | 是（prompt 全文在 key 里） | 自动全冷 |
| 改窗口参数 `n`/`m` | 是（labeled_text 变） | 自动失效 |
| 改 `should_extract_window` | 是（窗口集合变） | 自动失效 |
| 换模型（改 model 名） | 是 | 自动失效 |
| **服务端换掉同名模型的权重** | **否** | ← TTL 存在的理由之一 |

TTL 只为两件事存在：

1. **同名模型权重被替换** —— key 里的 `model` 只是字符串。
2. **删除后残留** —— 缓存 value 是 KG 抽取结果，而 prompt 明确要求
   「Preserve entity/concept names EXACTLY as they appear」，**原文片段留在缓存里**。
   用户删掉 notebook 后，其内容的衍生片段仍在全局缓存中。决策：**TTL 封顶即可**，
   不做定向清理——缓存 key 是 sha256 无法反查，且定向清理会连带删掉他人仍在使用的
   共享条目（那人的文件还在，逻辑上也说不通）。

三层机制：

- **容量驱动（主力）**：`size_limit` + LRU。99% 的回收由它完成，热条目自然留存。
- **TTL（兜底）**：默认 **90 天**，env 可配。必须**长**——短 TTL 会毁掉最大的收益
  场景：`reparse`/`reextract` 那种几万源重跑可能几个月后才发生，届时命中率接近 100%。
- **显式失效（逃生口）**：借 diskcache 的 `tag` 按 model 名清空——换模型服务时这才是
  正确操作，而非等 TTL 到期。另加全清。

## 可观测

无埋点则无法证明缓存在工作。

- 命中 / 未命中计数进事件日志；现有 `stage` 事件（parse/embed/extract）附
  `cache_hits` / `cache_misses`。
- 一个「缓存现状」查询：总大小、命中率、按 model 分布——否则无从判断何时该清。

## 顺带修复

- `sources` 加 `file_hash` 索引（现为全表扫）。
- UI 上传做**同 notebook 内**去重，对齐 `batch_ingest` 的既有行为，消除两条路径的
  不一致。跨 notebook 不做整源去重——用户通常确实想在自己库里拥有这份文件，且跨用户
  共享 source 行会引爆权限、删除级联与归属问题。

## 测试

- **key 稳定性**：同内容、不同 source_id / 文件名 → 同 key；不同 doc_type / tier → 不同 key。
- **空响应不入缓存**：`""` / `"{}"` / 异常路径，逐一验证；需**变异验证**（改回违规
  形态确认守卫真的转红）。
- **embed 批量部分命中**：N 条命中 K 条时，只对 miss 的 N−K 条调用后端，且返回
  **顺序与长度严格对齐**。错配是静默灾难（向量张冠李戴，检索层看不出来），必须
  用「命中项与未命中项交错」的用例覆盖。重组时 miss 子集仍须遵守 dashscope 的
  `_BATCH = 10` 硬上限（重新分批，而非把原批次原样透传）。
- **重复文本**：同一批内出现两条相同文本时，只应产生一次后端调用，且两个位置都拿到
  向量——这是 per-text key 的直接推论，容易在重组逻辑里写错。
- **淘汰**：超过 size_limit 时裁剪生效、热条目不被误删。
- **测试隔离**：确认测试环境拿到的是 `NoCacheBackend`。
- **属性透传**：`control` 经装饰器后仍可达，取消语义不变。

## 不做

- **parse 缓存**——仅占 1%，实测推翻了最初假设。
- **ask 路径专门优化**——其 prompt 含检索上下文与对话历史，命中率极低，且会导致
  「同一问题永远同一答案」的语义问题。ask 走通用 `LLMCache` 切换即可，不特殊处理。
- **跨 notebook 整源去重**——见上文。
- **多机共享缓存**——部署形态确定为单机多用户单进程。`CacheBackend` Protocol 已为
  将来换 Redis 留好位置。
