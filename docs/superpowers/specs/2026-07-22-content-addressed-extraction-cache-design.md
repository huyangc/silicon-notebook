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
| 缓存组件 | **自研 `SqliteCacheBackend`**（~160 行），经已有 `CacheBackend` Protocol 接入。 |
| 部署形态 | 单机多用户、单后端进程 → SQLite 后端足够。 |
| 默认状态 | **默认开**（优化的目的），保留 env 逃生口。 |

### 为什么自研而非用 diskcache

初始判断是引入 `diskcache==5.6.3`（"别造轮子"），原型对比实测后**结论反转**。

**决定性证据：`cull_limit` 陷阱。** diskcache 每次触发裁剪时剔除固定
`cull_limit` 条，而非"剔除到刚好达标"。条目越大越致命——20KB 条目 × 默认
`cull_limit=10` = 200KB，一次就能清空整个缓存。同一场景（size_limit=200KB，
20KB 条目，交错访问保持 h0-h2 为热）：

```
cull_limit=10 (默认)   热条目存活 0/3   终态 2条/74KB   ← 浪费 63% 容量
cull_limit=1           热条目存活 3/3   终态 8条/197KB  ← 正确
cull_limit=100         热条目存活 0/3   终态 2条/74KB
```

**KG 抽取响应正是几十 KB 的大条目**，会真实命中此坑。且它不报错——缓存"正常
工作"，只是留存率与命中率莫名偏低，极难诊断。

**性能对比**（Python 3.13.11，8 线程并发，150×20KB）：

```
自研 SqliteCache   写 4734/s   读 11514/s   留存 48/150   960KB/1000KB
diskcache          写 2263/s   读  5179/s   留存 40/150   852KB/1000KB
```

综合：我们只需要 get / put / TTL / 容量上限 / 按 tag 清空这 5 个能力，是 diskcache
功能的很小子集；自研约 160 行，性能约 2 倍，大条目下淘汰语义正确，且消除了
"依赖 12 个月无发布"的长期风险。

**风险与缓解**：自研原型第一版确实带一个真缺陷——`_evict_if_needed` 里
`ORDER BY used_at ASC LIMIT 64` 无条件删除整批，当候选批大于剩余条目数时一次清空
缓存、连带淘汰热条目。它**被测试当场捕获**，修复约 12 行（改为按 size 累加、删到
刚好达标）。这印证该类代码可测、风险可控，但也说明**下列测试是交付的必要条件而非
可选项**。

**diskcache 保留作为开发期的差分测试 oracle**（`cull_limit=1` 时两者语义一致），
列为 dev 依赖，不进生产依赖。

## 架构

```
kg_llm()        → CachedKGClient(inner, backend)     # 覆盖 初抽 / gleaning / refine
make_embedder() → CachedEmbedder(inner, backend)     # 覆盖全部 6 个 embed_texts 调用点
                          ↓
                  CacheBackend (Protocol，仅 get/put)
                          ↓
                  SqliteCacheBackend (~160 行)
```

装饰器模式，`extract_graph` / `extract_window` / refine / gleaning / 6 个 embed
调用点**全部零改动**。

`OpenAICompatibleClient._get_cache()` 内联的开关判断与路径解析一并**删除**，改为向
`make_cache_backend(settings)` 索取（ask 路径同样受益，全系统只有一套缓存机制、
一个构造点）。旧的 `llm_cache.db` 表结构不兼容（缺 tag/size/used_at 列），不迁移、
直接弃用——缓存冷启动重建即可。

### SqliteCacheBackend 要点

- 表 `cache(key PK, value, tag, size, created_at, used_at)` + `idx(used_at)` +
  `idx(tag)`；`meta.total_bytes` 增量维护，避免每次裁剪 `SUM()` 全表扫，另备
  `recount()` 修正进程崩溃导致的漂移。
- **粗粒度 LRU**：命中时仅当距上次刷新超过 `refresh_window`（默认 1h）才写
  `used_at`。cache hit 是热路径，逐次 `UPDATE` 会把"读"变成"写"，在高命中率场景
  （reparse 重跑）下写放大可观。牺牲少量淘汰精度换掉绝大部分写。
- **裁剪必须删到刚好达标**（按 size 累加选取受害者），不可无条件删除整批——这正是
  diskcache 的 `cull_limit` 陷阱，也是自研原型第一版的缺陷所在。
- 裁剪先清过期条目（免费空间），不足再按 `used_at` 升序淘汰；裁到
  `size_limit × 0.9` 留 headroom，避免每次 put 都触发。
- `PRAGMA synchronous = NORMAL`——缓存丢失可重建，不值得为它付 fsync。

### 配置

沿用现有 `LLM_CACHE_ENABLED` 作为**总开关**（避免引入第二个开关概念），默认值由
`false` 改为 **`true`**；`LLM_CACHE_PATH` 继续指向 SQLite 文件（换新文件名）。新增
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

## 缓存模块的内聚与可替换性

这是一条**硬性要求**：缓存必须高内聚，将来出现合适组件时能低成本替换。

### 现状问题

缓存代码目前散在 5 个文件（`core/llm_cache.py`、`core/llm.py`、`core/config.py`、
`services/model_provider.py`、`services/sqlite_repository.py`）。最严重的是
`OpenAICompatibleClient._get_cache()` 内联了**开关名、配置项名、相对路径的仓库根锚定
规则、具体实现类名**——这些都是缓存的内部事务，却由客户端持有。本设计新增两个消费者
（`CachedKGClient`、`CachedEmbedder`），若照此模式就会出现三份重复构造逻辑，换组件
需改三处。

### 模块结构

```
app/core/cache/
  __init__.py        # 唯一公开面: make_cache_backend() / CacheBackend / CacheAdmin
  backend.py         # Protocol 定义 + NoCacheBackend
  sqlite_backend.py  # 自研实现（全模块唯一写 SQL 处）
  policy.py          # key 计算 + 可缓存性判定
```

消费者只允许 `from app.core.cache import make_cache_backend, CacheBackend`。

### 三条规则

**1. 唯一构造点。** `make_cache_backend(settings)` 是缓存的唯一诞生处，负责读开关、
解析路径、选实现。消费者不自行 new、不解析路径、不读配置。换组件 = 改工厂一行。

**2. Protocol 分两层。** 这是可替换性的关键：

| 接口 | 方法 | 要求 |
|---|---|---|
| `CacheBackend` | `get(key) -> str \| None`、`put(key, value, tag="")` | **必需，仅 2 个** |
| `CacheAdmin` | `evict_tag(tag)`、`stats()`、`clear()` | 可选，`isinstance` 探测 |

`tag` 是可选参数，不支持的实现忽略即可——降级为"无法按 model 清空"，**不影响
正确性**。若把 `evict_tag`/`stats` 并入必需接口，将来换任何简单 KV 组件都得先补齐
管理方法，可替换性即告失效。管理端点在后端不支持 `CacheAdmin` 时如实降级提示。

**3. 策略与存储分离。** key 计算（`llm_key` / `embed_key`）与"什么不该缓存"
（空响应保护）属策略，留在 `policy.py`；backend 只负责存取字节。换存储不碰策略。

### 把要求变成可执行约束

- **导入守卫**：除 `app/core/cache/` 内部外，任何文件不得 import 具体实现类，只能
  import Protocol 与工厂。须做变异验证——在别处插入
  `from app.core.cache.sqlite_backend import SqliteCacheBackend`，守卫必须转红。
- **Protocol 契约测试套件**：参数化 fixture 覆盖全部 backend，测的是接口行为而非
  实现细节。这是可替换性的另一半——接口能插上不等于行为正确。新组件接入时跑通这套
  即可切换。

### 验收标准

替换为任意组件 = 新增 1 个文件实现 2 个方法 + 改工厂 1 行 + 契约测试通过，
**零调用方改动**。

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
- **显式失效（逃生口）**：`evict_tag(model)` 按 model 名清空——换模型服务时这才是
  正确操作，而非等 TTL 到期。写入时以 model 名作 tag。另加全清。

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
- **淘汰**（自研缓存的核心风险面，逐项必测）：
  - 超过 `size_limit` 时裁剪生效，且**裁剪后容量利用率仍应接近上限**——"缓存被清得
    只剩零星几条"是 `cull_limit` 类缺陷的特征，只断言"没超限"抓不到它。
  - **热条目不被误删**：注意测试本身要写对——LRU 看的是最后访问时间而非访问次数。
    有效用例须在灌入冷数据的**过程中交错访问**热条目，否则热条目的 `used_at` 仍旧
    早于所有冷条目，被淘汰是正确行为（此坑在原型阶段实际绊倒过一次）。
  - **候选批大于剩余条目数**时不得清空缓存（原型第一版的真实缺陷）。
  - **差分测试**：同一随机读写序列下，与 `diskcache(cull_limit=1)` 逐步比对命中/
    落空判定，分歧数必须为 0。
  - 覆盖写时 `total_bytes` 按差值更新；`recount()` 与增量计量一致。
- **测试隔离**：确认测试环境拿到的是 `NoCacheBackend`。
- **属性透传**：`control` 经装饰器后仍可达，取消语义不变。
- **Protocol 契约套件**：参数化 fixture 覆盖全部 backend（`sqlite` / `noop` / 未来
  新增者），只测接口行为，不碰实现细节——put 后 get 得回原值、缺失 key 返回 None、
  覆盖写生效、`tag` 参数被接受（即便被忽略）。新组件接入时跑通即可切换。
- **内聚导入守卫**：除 `app/core/cache/` 内部外，无文件 import 具体实现类。须变异
  验证：在别处插入 `from app.core.cache.sqlite_backend import SqliteCacheBackend`，
  守卫必须转红。

## 不做

- **parse 缓存**——仅占 1%，实测推翻了最初假设。
- **ask 路径专门优化**——其 prompt 含检索上下文与对话历史，命中率极低，且会导致
  「同一问题永远同一答案」的语义问题。ask 走通用 `LLMCache` 切换即可，不特殊处理。
- **跨 notebook 整源去重**——见上文。
- **多机共享缓存**——部署形态确定为单机多用户单进程。`CacheBackend` Protocol 已为
  将来换 Redis 留好位置。
