# 笔记本单文件导出 / 跨部署导入：实施计划

状态：已定稿（§9 裁决 2026-10-22），待开工。

## 0. 用户裁决（2026-10-22）

- 入口：网页（设置里导出下载、「＋ 新建」旁导入上传）+ 管理员 CLI，两者都要。
- 导入语义：**独立可编辑的新笔记本**——像「复制笔记本」一样换新 id，导入者为所有者，与源端此后无关；
  同一文件可导入多次。不走 `notebooks.sync_origin` 镜像围栏。
- 内容：只带笔记本本体（材料/原文件/附件、元素、切块、向量、KG、Knowhow）；问答、报告、反馈等交互数据不带。
- 向量：文件记录各 embedding workload 的模型名与维度；导入端一致就照搬，不一致就丢弃并在后台重新嵌入。KG 照搬。

## 1. 与现有能力的关系

| 能力 | 语义 | 本特性如何用 |
|---|---|---|
| `notebook_sharing.copy_notebook` | 同库深拷贝、换新 id、失败只补偿目标侧 | **共用重映射写入相位**（核心复用） |
| `app.migration.sync` | 目录型包、保留源 id、只读镜像、username 映射 | 不用其语义；把可移植行编码（`{"$bytes": b64}`、SQLite 形态）、`is_safe_*` 路径函数、确定性 JSONL 抽到中立 domain 模块共用 |

## 2. 总体设计

```
同库复制：  SnapshotRowSource(内存快照)   ┐
                                            ├─► NotebookCopyWriter（按相位重映射写入，
跨部署导入：ArchiveRowSource(zip 流式读)  ┘     CopyProfile 决定差异）
导出：      store.iter_copy_rows(流式，同一查询集合) ─► ArchiveWriter(zip64)
```

`CopyProfile` 集中两条路径的差异：

| 差异项 | 复制 | 导入 |
|---|---|---|
| 哨兵状态 | copying | importing（不被 `sweep_stale_copies` 按超时误收割；不受复制级联删除量不变量约束） |
| 引用策略 | 宽松（`map.get(x, x)` 透传） | 严格：结构列查不到映射→拒绝文件；JSON 内嵌 id→置空计数（透传会劫持目标端同 id 行） |
| 用户列 | 部分列改新主人 | 全部用户列（含 `knowhow_cell_code.updated_by`、`notebook_assets.created_by`）改导入者 |
| 挂载边 notebook_bases | 按新主人重判 | 全部丢弃，报告计数 |
| 向量 | 照搬 | 按 workload 比 (model, dim)，逐行核字节长 |
| 文件 | copytree | 从 zip 取已校验的清单条目，`O_EXCL|O_NOFOLLOW` |
| 校验 | 与源本比行数 | 按写入累计行数核对 + 悬空引用检查 |
| 收尾 | knowhow 重投影 | + `maybe_auto_index` + 重新嵌入作业 |

三处必须修正的现状问题：
1. `snapshot_copy_rows` 全量载入且受复制上限（5000 chunks+对象 / 200k 行）约束 → 导出另做流式 `iter_copy_rows`，查询集合相同、分页读、不查复制上限；写入相位按批消费。
2. 宽松透传在跨部署时有劫持风险 → 导入严格；导出端先规范化已知软指针（global_object_id、source_candidate_id、after_object_id 等）。
3. `validate_copy` 依赖同库源本 → 导入改用累计行数 + 悬空检查。

复制闭包现覆盖 25 张表（notebooks、notebook_bases、sources、source_paper_meta、source_authors、source_elements、chunks、chunk_questions、knowledge_objects、knowledge_source_facts(+elements/+backfills)、knowledge_relations、concept_clusters、notebook_object_schemas、四张 embeddings、knowhow 五表、notebook_assets；`extraction_runs` 写入时合成）。记忆、授权/成员、使用痕迹、KG 派生层（communities、canonical_relations 等）、knowhow 历史不在闭包内，导入与复制保持一致。

## 3. 文件格式 v1（`.snbk`，zip，强制 ZIP64）

```
manifest.json                  # 最后写入
rows/<table>.jsonl             # sort_keys；SQLite 形态；bytes 列 {"$bytes": b64}；时间戳统一 UTC ISO 文本
files.jsonl                    # 文件清单（路径、大小、sha256）
files/notebooks/<相对路径>      # storage/notebooks/<nb>/
files/assets/<asset_id>.<ext>  # storage/assets/<nb>/
```

manifest：`format="silicon-notebook.notebook-archive"`、`format_version=1`、`exported_at`、`schema`（仅诊断）、
`notebook`（展示字段，源 id 仅参考）、`kg`（starts_dirty / clusters_dropped）、`embeddings`（按 workload
`{protocol, model, dim, encoding:"float32-le"}` + `vectors_included`）、`tables{columns, rows, bytes, sha256}`、
`files{count, bytes}`、`global_object_types`、`excluded`（不暴露记忆条数，守 M2）。

版本兼容：`format_version` 独立于 DB schema；导入端拒绝更新的版本。真正的兼容门是「文件每表列集合 ⊆ 目标端列集合」，
多出列→拒绝并提示升级本部署。守卫测试钉住「复制闭包表集合 == 归档表集合」；闭包加表必须同时升格式版本（写进 development.md）。

## 4. 导入细则

- 向量：chunk_embeddings 按来源类型分开判（文档→chunk_embedding，knowhow→knowhow_embedding）；`chunk_questions.vector`
  NOT NULL，不一致时在导入作业内（发布前）重新嵌入问题文本，失败则丢行并写报告。
- 全局自定义对象类型：目标端没有的写成本笔记本 `notebook_object_schemas`（source='custom'），已有的以目标端为准。
- 非终态来源（解析/抽取中）改为失败、需重新解析。
- 资产行 source_id 映射不上：丢资产行与文件，计数。
- 发布后：knowhow 重投影、`maybe_auto_index`、按需提交重新嵌入作业；knowhow 创世记录 note「随笔记本《X》导入而来」。

## 5. 安全

条目名白名单（manifest.json / files.jsonl / `rows/<已知表>.jsonl` / 安全相对路径）；拒绝重复名、绝对路径、反斜杠、盘符、
`..`、符号链接/设备、加密、非 stored/deflate；条目数、解压总量、单条压缩比上限；JSONL 行长上限、捕获 RecursionError；
每表实际行数 == 声明行数；先预检（结构 + 全部 sha256 + manifest 一致性）再开写；目标路径自拼 + `resolve()` 证明在根内；
文件里的任何内容都不能指定授权、成员或挂载。参照 `parsers.py` 的 Markdown 压缩包护栏。

## 6. 大笔记本与失败补偿

- 导出：PG 在 REPEATABLE READ 事务里命名游标流式读；SQLite 单读事务逐行迭代；记忆 evidence 剔除改逐行。代价：长事务持 xmin / WAL 增长。
- 导入：分页解码按批写，定期刷 `updated_at` 心跳；id 映射约 200B/行（100 万行≈200MB）。
- 补偿：小库级联删；超过复制上限的转删除作业（importing CAS→deleting + 同事务建 `notebook_delete_jobs` 行）；rmtree 本次建的两棵目录。启动时 `sweep_stale_imports` 收割心跳过期的哨兵。
- 网页导出写 `storage/transfers/exports/<job>.snbk.part`，完成改名，TTL 过期清理。

## 7. 网页 API

| 端点 | 鉴权 | 说明 |
|---|---|---|
| `POST /api/notebooks/{id}/export` | 新能力 `notebook:export`（owner 档，镜像围栏放行） | 202 + 作业；规模超 web 上限 413，提示用 CLI |
| `POST /api/notebook-imports` | 见 §9 | 原始 `application/zip` 流式落盘，202 + 作业 |
| `GET /api/notebook-transfers[/{job}]` | 本人 | 列表 / 状态、阶段进度、新笔记本 id、报告 |
| `POST /api/notebook-transfers/{job}/download-ticket` + `GET /api/notebook-transfers/download?ticket=` | 本人 / 票据 | 一次性票据流式下载（§9.4），`filename*=UTF-8''` |
| `DELETE /api/notebook-transfers/{job}` | 本人 | 取消/删除 |

作业类型 notebook-export / notebook-import / notebook-reembed 进重活池。新 Settings：`NOTEBOOK_TRANSFER_WEB_MAX_MB`、
`NOTEBOOK_TRANSFER_TTL_SECONDS`、`NOTEBOOK_IMPORT_STALE_SECONDS`；`/system/config` 暴露 web 上限；Next 反代请求体上限取
「来源上传包络」与「导入上限 + 1MiB」较大者；待办中心增加导出/导入条目。

前端：`notebook-transfer-api.ts`、`use-notebook-transfer.ts`、`notebook-transfer-panel.tsx`（新建）；设置弹窗「导出」区块、
「＋ 新建」旁「导入」根弹窗；`workspaceCapabilities` 加 `canExportNotebook`；按钮按压反馈与结果就近显示；文案遵守
`ui-vocabulary.md`（不出现「向量」「embedding」「chunk」）。

## 8. CLI（`scripts/cli.py notebook ...`）

```
notebook export --notebook <id>... (--out <file>|--out-dir <dir>) [--no-vectors] [--json]
notebook inspect <file> [--json]
notebook import <file>... --owner <username> [--name N] [--dry-run]
        [--confirm-service-stopped] [--rebuild-scale auto|skip] [--reembed auto|skip] [--json]
```

export 只读；import 走维护仓库、PG 上要求 `--confirm-service-stopped`；重嵌入与重投影同步执行；scale 重建 PG 走
`scale_build_cli.run_build`、SQLite 跳过。登记 `test_schema_guest_guard.py`。

## 9. 裁决（用户 2026-10-22）

1. 记忆：**不带**，与复制 / M2 一致；导出弹窗提示可另用「导出我的记忆（Markdown）」。
2. 导入权限：**所有登录用户**；非管理员受每本 `user_upload_document_limit` 篇文档上限，超出拒绝并提示请管理员用 CLI 导入。
3. 作业持久化：**进程内注册表 + 磁盘 sidecar，不加表、零迁移**；单 API 进程前提写进部署文档；重启时运行中的作业判失败。
4. 网页大文件下载：**一次性下载票据**，浏览器原生流式下载：
   - `POST /api/notebook-transfers/{job}/download-ticket`（本人、作业 ready）→ `{url, expires_at}`；
     票据 32 字节随机、内存只存 sha256、绑定 (job_id, user_id)、TTL 60 秒、**单次使用**（兑换即作废，CAS 消费）。
   - `GET /api/notebook-transfers/download?ticket=…` 不要求鉴权头，按票据流式返回文件；`Cache-Control: no-store`、
     `Referrer-Policy: no-referrer`；作业过期/删除/用户被停用即失效；失败一律 404 不区分原因。
   - 访问日志与 llm/诊断日志对该路径的 query 脱敏（票据不入日志）；前端用隐藏 `<a download>` 导航触发。
   - 因此 web 导出不再受浏览器内存约束，web 上限只由磁盘与 `NOTEBOOK_TRANSFER_WEB_MAX_MB` 决定；上传方向仍受 Next 反代内存缓存约束。
   - 测试：票据重放、过期、跨用户、作业删除后兑换、日志不含票据。

按推荐默认（如有异议再改）：导出仅 owner、镜像也允许导出（入口加卡片菜单）；重新嵌入在发布后后台补（期间问答只有关键词召回）；
chunk_questions 在导入作业内重嵌入；CLI 导入 PG 上必须停服；KG 派生层与复制一致不带；导入默认保留原名、弹窗可改。

## 10. PR 拆分（同一轮全部交付）

1. **重构 copy_notebook（零行为变化）**：抽 `notebook_copy_writer.py`（CopyRowSource / NotebookCopyWriter / 相位；`CopyProfile` 推迟到 PR4 有第二个消费者时再引入，避免零消费者扩展点；磁盘文件操作经 `materialize_assets` 回调留在服务里）；Snapshot 来源每表一个批次保持冻结写序；
   复制表征测试（SQLite+PG）、记忆排除、图片资产、knowhow 复制全部不改即绿；下调 `architecture_boundary_baseline.json`。
2. **可移植编解码 + 流式快照 + 导出 + CLI export/inspect**：`domain/portable_rows.py`（`sync/package.py` 再导出）、`domain/notebook_archive.py`、
   两后端 `iter_copy_rows`、`notebook_transfer/export.py`、`scripts/notebook_archive.py`；守卫登记；导出用例（含记忆排除、流式分页、超复制上限仍可导）。
3. **重新嵌入作业**：`services/notebook_reembed.py`，下沉 `source_routes._backfill_vectors_job`（端点契约不变），加 KG 对象/关系两条腿，跳过 knowhow 隐藏来源。
4. **导入核心 + CLI import**（随 CopyProfile 一起：`NotebookCopyRun.source_notebook` 收窄为源名字符串，磁盘状态字段 assets_dest_dir/copied_files/assets_copied 移回服务侧）：`archive_reader.py`（§5 护栏）、`import_.py`、store 新方法（哨兵/心跳/校验/发布/补偿/收割/列集合）；
   四向往返（SQLite↔PG）、重复导入互不相干、向量一致/不一致、全套篡改用例且失败后零残留。
   **PR2 交接**（导出端已落地的事实与导入端必须补的点）：
   - manifest 的列名来自文件：拼 SQL 前逐个用 `is_safe_identifier` 校验，并与目标端该表的列集合比对（⊆，多出列即拒绝），绝不把文件里的名字直接拼进语句。
   - 空表的 `columns` 是 `[]`（列集合取自行的键并集）：不能当作「该表必有的列」，也不能据此推断 NOT NULL。
   - 解包文件时防大小写/Unicode 归一化撞名：`files/**` 的相对路径按目标文件系统的比较规则（NFC/NFD、大小写不敏感卷）去重，撞名即拒绝。
   - 时间戳经现有 `app.migration.shadow.transform` 路径写入：有时区的值是 UTC 文本；SQLite 的无时区值原样携带、按约定当 UTC 读（与 sync 一致）；`""` 哨兵按 `_EMPTY_TIME_SENTINELS` 落库。补一条用例：无时区 SQLite 时间戳与 `""` 哨兵导入 PG。
   - 笔记本行由 `_reset_copied_notebook_row` 复位，覆盖 `sync_origin`、`created_by`、`indexing_pipeline_job_id` 等；文件里的 `share_token` 已是 NULL、`is_shared` 已是 0，`notebook_bases` 恒为空（条数在 `manifest.excluded.mount_edges`）。
   - `sources.file_path` 是相对 `files/notebooks/` 的路径；资产文件是 `files/assets/<旧 asset id>.<ext>`。
   - `concept_clusters.canonical_id` 的取值空间：所有写入方都写成 `<类型前缀><种子>`（`kg_merge`：`id_prefix + seed`，前缀 `K-`/`KL-`/`KF-`/`KP-`），**永远不是裸对象 id**，所以复制里的 `object_map.get(canonical_id, canonical_id)` 恒等。唯一嵌对象 id 的形态是名字退化时的 `<前缀>~ko-<对象 id>`（`seed_or_unique`），`memory_sql.cluster_seed_object_id` 据此判 Memory 与陈旧簇。导出端已丢弃「嵌入的对象不在导出范围内」的簇行（计数 `concept_clusters.canonical_id`）；导入端须把嵌入的旧对象 id 按对象映射改写成新 id（`<前缀>~<新 id>`），否则导入库的下一次复制会因种子对象不存在把整簇当陈旧丢掉。
5. **网页 API 与作业**：路由、能力、作业注册表、Settings、system config、待办中心、Next 反代上限；权限/413/流式/过期用例，OpenAPI 冻结契约更新。
6. **前端**：API/hook/面板、能力位；vitest + 按钮反馈/长任务/root modal 守卫 + ui-vocabulary；Browser pane 实机走通「导出→下载→导入→打开」。
7. **文档收尾**：product-and-api、architecture、operations、deployment-and-configuration、development（中英成对）、incremental-sync-design 边界、
   scripts/README、user-manual_zh、ui-vocabulary、release-notes（level=feature）。
   development.md / `_zh.md` 写明规则：复制闭包增删表 ⇒ 同时更新归档格式（`ARCHIVE_TABLES`）并提升 `FORMAT_VERSION`（`test_notebook_archive_format` 两条守卫钉住）。

## 11. 风险

id 映射内存；长事务（xmin / WAL）；单进程作业前提；Next 反代按内存缓存请求体（web 上限不宜调大）；knowhow 向量与 chunk 补齐 workload 混用的既有缺陷需显式排除；
严格引用可能拒绝历史脏数据（导出端规范化需覆盖全部软指针，用真实形态数据验证）；复制表征测试冻结调用序；跨后端时间戳统一 UTC 文本。
