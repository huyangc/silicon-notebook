# 产品与 API 参考

[返回 README](../README_zh.md) · [English](./product-and-api.md)

本文保留详细的产品行为与 HTTP/MCP 契约。仓库根 README 作为精简入口；实现和架构细节仍以 [architecture.md](../architecture.md) 与 [AGENTS.md](../AGENTS.md) 为准。

## 当前范围

当前仓库已进入以 KG-native 管线为核心的本机真实 beta 闭环：

- Python FastAPI 后端；SQLite 持久化路径 `.local/silicon_notebook.db`
- `frontend/` 下的 Next.js / React / TypeScript 前端
- 由部署者统一管理 OpenAI-compatible chat、embedding 与 rerank 服务；workload 绑定及每服务 `max_concurrency` 集中写入一个 TOML
- 未配置 LLM/embedder 时全管线可离线运行（deterministic fallback）
- 干净起点：全新数据库只初始化本机用户，不预置 demo 笔记本或合成来源
- 支持 PDF、Markdown、DOCX、PPTX、CSV、XLSX、旧版二进制 XLS 的 multipart 文件上传（经共享 KG job scheduler 异步执行）。添加来源弹窗会从中间压缩过长的待上传文件名，保留末尾/扩展名并通过悬停显示全名；拖放与文件选择共用同一套入列校验——拖放由前端显式接管而不交给浏览器按 `accept` 静默过滤，入列时被跳过的文件（类型不支持——旧版 Office 另给「另存为」引导、超过单文件大小上限、超出单次批量上限）在弹窗内逐条持久列出文件名与原因，不允许只靠短暂 toast；有效文档上限判定会计入整个暂存批次，批次大于剩余名额时在提交前直接禁用上传，并说明剩余名额、超出数量和处理办法。
- **KG-native 摄取**：结构化 Markdown 解析 → 贪心窗口化 KG 抽取（Concept / Claim / Formula / Procedure）并发 embedding → 抽取优先状态（`extracted` = KG 就绪，不等 embedding）
- PDF/DOCX/PPTX/XLSX 走 MinerU（公式/表格/版面、内嵌图片）；MinerU 不可用或没有可用产出时各自回落本机库解析。每条本地链都是**分级降级**而不是一步跌到最粗的抽取器：PDF 走 PyMuPDF4LLM 版面感知 Markdown、pypdf 仅最后兜底；DOCX 走 mammoth，其语义化 HTML 保住标题层级、列表标记与表格结构（`table_html` 存入 metadata），python-docx 的逐段落/逐行拍平降为最后兜底；PPTX 走 python-pptx，连原始幻灯片 XML 抽取整块丢失的幻灯片表格、图表标题、组合形状与演讲者备注一并取回，原始 XML 抽取降为最后兜底；XLSX/XLSM 走 openpyxl。兜底路径不持久化 DOCX 内嵌图与 PPTX 图片（URL 来源可能只是个短命临时文件），mammoth 的 base64 data URI 直接丢弃、不塞进元素正文。这类降级对**每一种有损**兜底都会披露（PDF、DOCX、PPTX），不只 PDF；工作簿刻意排除——openpyxl 兜底对单元格值全保真，打警告是噪音，而且 MinerU 不支持工作簿的部署会得到一个用户永远消不掉的假警告。工作簿的 MinerU 非空产出还要先与工作簿自身的非空**行数与格数**做一次本地零模型覆盖对账才被采信，**任一维度**覆盖不足即整份丢弃、改用 openpyxl。两个维度缺一不可：只数行看不出宽表下最常见的那种残缺——MinerU 保住了每个 `<tr>`，却把渲染页宽之外的列整片丢掉；非表格文本元素每条最多只顶一行一格，图片块两个分子都不计入。采信阈值为 0.8，行与格两个维度共用（`backend/app/services/parsers.py` 的 `MINERU_WORKBOOK_MIN_ROW_COVERAGE`；数值仅在本节登记）。云端上传路径的工作簿走同一道对账，且内嵌图片只在产出被采信之后才持久化，被拒的产出不会留下孤儿资产。触发警告时来源仍为 `extracted`，对外只暴露 `parse_quality_warning`。旧版二进制 `.xls`（前 OOXML 时代的 BIFF 格式）没有任何 MinerU 分支——MinerU 根本不支持这种容器——一律直接走 `xlrd`（该格式唯一的纯 Python 读取器）；其余旧版二进制 Office 格式（`.doc`、`.ppt`）仍不受支持，界面会引导用户另存为 `.docx`/`.pptx`。
- MinerU 抽取的内嵌图片在来源正文内联展示；图注与文字保持可搜索。**Markdown 来源的内嵌图片走同一套护栏**：`![alt](src)` 带 alt 文字时，alt 作为图注写入元素 `metadata.caption`，与 MinerU 解析出的 PDF 图注同等参与检索（进 chunk）；无 alt 的普通路径图片仍不产出元素（除非后面跟着下述「图片描述块」）。`src` 为 `data:image/{png,jpeg,gif,webp};base64,...`（与 MinerU 图片资产同一套 mime 白名单，识别不了的 mime 如 svg/bmp/avif 不会被解析为图片，其 `![alt](data:...)` 字面量无论独占一整段、与其他文字混排，还是出现在列表项/标题/表格单元格中，都会被剥离只留 alt 文本，base64 绝不进入元素文本）时，图片字节解码后落为来源图片资产、来源详情正常显示；单图字节上限与每源张数上限复用 MinerU 图片配置（`MINERU_MAX_IMAGE_BYTES` 默认 5MB、`MINERU_MAX_IMAGES_PER_SOURCE` 默认 200），`MINERU_RETURN_IMAGES=false` 时同样不落资产——该开关现在门控所有来源的图片持久化，不再只管 MinerU 解析出的文档。data URI 本身绝不写入元素 metadata；无 alt、未成功落资产、又没有图片描述块的 data URI 图片不产出元素。已知边界：只处理独占一整段的图片，列表项/表格单元格内的内嵌图片只保留 alt 文本、不落资产。**图片描述块**：图片行之后（中间可以隔空行，也可以紧贴着写——引用块能打断段落）紧跟一个引用块，块内第一行只有 `**图片描述**` 标记时，该引用块里**所有**引用行都是这张图的描述：描述文本折进图片元素的 `metadata.description`，并与图注一起构成元素文本进 chunk。因此**带描述的图片即使没有 alt 图注也可被检索**（导出工具产出的图往往没有 alt，描述块是它们唯一的入口），引用附图旁边的说明在没有图注时也回退显示这段描述（沿用同一个图注截断长度）。折叠掉的只是「引用块另成一条段落元素」这件事——同一段话不会占两个检索位；知识图谱抽取仍按普通段落读它的逐字原文。四条形状判据（判宽了就会把正常引用吞成描述）：标记行必须只有标记本身，粗体与行尾冒号可有可无，标记后面还跟正文时必须隔一个冒号（`> **图片描述**：正文`），所以「图片描述如下：……」这类正常引用不算；引用块里不能出现围栏代码块/缩进代码块/HTML 块（它们的正文不挂在 inline 节点上，折进去就是静默丢内容），而列表/标题/表格/嵌套引用照收——约定说的是「后续的**所有**引用行」，图片描述常带项目符号；标记之后必须还有**渲染后**非空的引用文本（只有一个光标记、或只有 `<br>`／空 alt 图片的引用块保持原样）；图片与引用块之间**按原文**不能隔着别的内容（链接引用定义 `[foo]: /url` 不产出任何 token，但它是原文里的内容）。一张图的多段描述折成**一个**元素，因此不再被600 字 chunk 切分——超长描述的向量只覆盖 embedding 截断长度之内那段（已登记的取舍，词法检索不受影响）。引用本机图片文件路径的 markdown 可先用 `scripts/embed_md_images.py` 就地转成 data URI 再上传（见 README「产品流程」一节）。
- **Markdown + 图片压缩包上传**：添加来源弹窗还接受一个压缩包，或直接拖入一个文件夹——只要它把 markdown 与其引用的图片保持相对路径放在一起，也就是 Notion/语雀/HackMD 导出天然就是的形态。一套零依赖的浏览器端纯函数管线（`frontend/app/md-bundle.ts` / `bundle-intake.ts`）解析压缩包的 central directory、用浏览器原生 `DecompressionStream('deflate-raw')` 解压、按包内/文件夹相对路径解析每个 `![alt](src)`（处理 `./`、`../`、`%20` 等 URL 编码）、按**魔数**而非扩展名嗅探匹配到的文件、把匹配上的 png/jpeg/gif/webp 图片内联成 base64 `data:` URI——与上面 markdown 摄取护栏认的形态完全一致——最终把这份自包含 markdown 原样交给既有上传接口。`.zip` 只是前端的交换格式：它绝不进入后端的支持后缀白名单，且只有独占一整段的图片才会被内联，与服务端判据逐字镜像，避免前端配对成功而服务端悄悄丢弃。配对回执在弹窗内**持久**列出（不是一闪而过的 toast），分五类：已内联、未找到（附带若干条近似候选路径）、不支持（语法/位置/格式不符，或超过单图/单来源上限）、云端 `http(s)` 链接（v1 不拉取，原始 Markdown 文字保留）、无图注的图片（提示上传后无法被检索——图注是图片进入检索的唯一入口，见下文「引用附图（本段附图）」；紧跟着「图片描述」引用块的图片**不**报这一条，它已经可以被检索到）。`GET /system/config` 另发 `source_image_max_bytes` / `source_image_max_per_source`（镜像部署的 `MINERU_MAX_IMAGE_BYTES` / `MINERU_MAX_IMAGES_PER_SOURCE`，旧后端缺字段时为 `null`——含义是「拿不到这个上限，不做本地预检，交给服务端护栏兜底」；显式下发的 `0` 是**合法值**且语义相反：一张都不持久化，浏览器按「图片存储已关闭」整体跳过内联）与 `source_images_enabled`（镜像 `MINERU_RETURN_IMAGES`；缺字段按 `true` 处理，因为这个开关此前从不存在，不能让旧部署凭空弹出一条假警告），供配对阶段预检单图上限并在图片存储关闭时整体跳过内联、给出持久提示。精确护栏数值见[下表](#markdown-压缩包上传护栏)。
- **精确短语（用户检索语法）**：用**英文半角双引号**括起来的内容整体参与检索，不做分词。`什么是 "static timing analysis" 的原理` 里那段短语会作为一个不可拆的词项进入词法候选（SQLite 走带引号的 FTS5 词项，PostgreSQL 走转义后的 `ILIKE` 子串），在关键词覆盖率里也只算**一项**——整段命中才得分，散落着 `static`/`timing`/`analysis` 的文档一分不给，因此含完整短语的原文会排在前面；同时这段短语无条件获得一次精确定位探测（下文的精确标识符通道），命中的小节整体取齐。引号是**强偏好**而不是硬过滤：语义检索照常进行，不会因为某篇文档缺这段短语就把它从结果里剔除。打分侧（关键词覆盖率与 BM25/RRF 排序）会归一空白，所以文档里跨换行、多空格写的同一段短语照样算命中；**候选生成侧做不到**——FTS5 trigram 短语与转义后的 `ILIKE` 都是字面连续匹配，写成 `static   timing\n analysis` 的文档若只靠这段短语就捞不上来（要抹平它得加一列归一化的索引文本，而无索引的正则扫描是本层禁止的全库扫）。这时查询里其余词项与语义召回照常工作。识别有三条边界：只认英文半角双引号（中文排版引号 `“…”` 在散文里是普通引用，认它会把大量既有提问悄悄变成带约束的提问）、引号内至少 3 个字（SQLite 的三字符索引更短的索引不到）、一段文本里**不同**的引号内容超过 4 段时整条语法不生效（那是 JSON 之类的机器文本，引号在那里是标点不是约束）；数的是不同内容而非出现次数——推理与报告的内部检索问题会把同一段短语在目标、规范化问题和每条必答主题里各留一份。提问框与深度报告输入框在你敲下引号的当下就回执识别结果——识别到哪几段、或为什么这次没识别——不会让一次没生效的约束静默通过。规划与反思提示语在问题真的带引号时才追加一句「原样保留引号内容」，因此模型改写子查询也不会把它拆散；笔记本搜索框只是整串子串匹配，本来就不分词，那里**被识别的**那几段的引号会被去掉（未被识别的引号原样保留，仍可用来搜字面 JSON/代码）。私有 Memory 的候选生成是把整串当一个短语探测，因此每段被识别的短语会作为额外的 OR 词项进同一条有界查询，让「只含该短语、不含整句」的记忆也能进候选池；评分侧仍拿原串，短语必须整段命中。
- 混合检索：CJK 感知 bi-gram 关键词 + float32 语义检索（每 notebook 独立缓存）。SQLite FTS5 保留整句精确匹配加分，同时以安全引用的 OR 词项召回拉丁字母/数字词、重叠中文三字片段，以及 `_`/`-`/`.` 连接的完整标识符（`set_db` 这类，受「须含字母、长度 ≥4、至多 16 个」约束）；PostgreSQL 在原生 trigram 候选生成前拆分同一组有界词项，并对 `ILIKE` 分支转义 LIKE 元字符，使 `set_db` 这类词项保持字面量，不会退化成通配把 `setXdb` 也拉进候选。带索引的 Chunk/KG 路径合并有界 ANN 与词法候选窗口，带索引的 Relation 检索按方向平衡补入与词法命中 KG 端点相邻的关系并保留端点顺序。纯词法候选按 keyword-only 参与融合，不会被写入伪造的零语义分。
- 内置关系在抽取与图消费者之间共用同一套有向端点契约。违反核心类型配对的历史行仍可审计，但不能影响 graph/PPR/canonical/relation 检索；管理员定义对象类型可继续使用已知边 id 扩展。可选跨元素补全按来源代次的持久 keyset 水位推进有界页面，只使用同源索引候选并经过双阶段验证、代次复核与灰度闸，默认关闭；它不会做文档级或整书全表扫描。
- KG-native 接地问答：逐句 `[k_i]` 引用（半角 `[k1]` 与本地化 `【k1】` 共用同一绑定，半角/中文逗号的复合形式也兼容，并统一渲染为紧凑编号引用；模型直接输出的数字复合引用如 `[1, 2, 3]` 在能映射到已知引用时也可点击）、多轮会话、1-hop KG 邻居扩展，推理模式实时显示可展开的一行 agent 轨迹
- **意图优先的逐步推理问答**：正式界面启动 `reasoning` job 前，先由 `POST /api/notebooks/{id}/ask/intent` 在完全不读取 notebook / 参考库语料的条件下理解问题；它只能使用当前会话最近的用户问题，不能使用语料派生的助手回答，也不会创建 conversation 或 job。意图清晰时自动继续；因模型规范化没有经过人工审阅，原始问题仍是第一条权威检索种子，规范化表述只能补充。会改变方向的歧义暂停确认后，审阅后的表述才成为权威。冻结的主题/方向、实体、比较轴、约束、排除项、前提、期望输出和答案统一支配 Memory、PPR、证据检索与合成；首轮先执行完整权威问题，再轮询确认方向让每个必答主题都拿到一个种子；超出该档位首轮宽度的方向由一段有界补种在同一份步骤预算内顺延执行，预算覆盖不到的会在轨迹里披露并回喂给反思，而不是被丢弃。第二个规划器不得替换。无效确认在创建持久状态前返回 422，取消预检会把取消事件传给意图模型。
- **推理模式的类型化查询期推导：** agent 可调用 `follow_chain`，把有证据的两跳 `A→B→C` 临时组合成 `A→C`；首版只允许 `derived_from / kind_of / prerequisite_of / precedes / part_of`。两条直接关系各自保留可引用的关系证据；被拒绝、无 quote、类型或 `validity_scope` 冲突的路径 fail-closed；推论明确标作「推断」，且绝不写回 KG。该能力不新增 migration、索引或历史回填；查询只对既有 source/target 索引做有界抽样，高度节点无法在预算内确认时直接放弃推论。
- 两层知识库：每个 notebook 带 `tier`（`base` | `personal`，默认 `personal`）。`chunk` 基线只从当前 active notebook 读取 chunk；可选 KG overlay / PPR 才可能加入 federated KG 上下文与 base-backed chunk，`graph` / `reasoning` 使用 federated KG 路径。exact-score 的 `base` 次序只适用于知识对象命中：`federated_retrieve()` 不改相关度分数，分数更高的 personal hit 仍排在前面；`federated_retrieve_relations()` 的关系命中仍只按 score 排序。回答合成阶段另有独立规则：当 base 与 personal 证据冲突时，以 base 立场为准并指出差异。引用携带其 tier（`AnswerAnchor.tier`），Ask 在每条引用上渲染 `base`/`personal` 标记。
- **用户系统**：自助注册（用户名规则：单个字母 + `00` + 6 位数字，如 `a00123456`，存储为小写）+ 密码登录，使用不透明 Bearer 会话 token。每个 notebook 由其创建者所有；用户库包含自己拥有的 notebook，以及主动加入的大型只读共享 notebook。首次启动时自动创建内置 `admin` 账号（登录用户名 `admin`，密码来自 `SILICON_NOTEBOOK_ADMIN_PASSWORD`，本地默认 `admin`；production/对外监听必须修改），并由它持有原有 notebook。管理员可在用户使用总览通过 `PATCH /api/admin/users/{user_id}/role` 授予或撤销 `admin` 角色；内置管理员和当前操作管理员自身不可被降级，已有会话会在下一次请求时读取到新权限。用户使用总览先对 `/api/admin/users` 返回的完整集合排序，再按页显示；默认每页 20 条，可切换为 50/100 条，并可点击各数据列表头切换升降序。总览及 `GET /api/admin/users/{user_id}/notebooks` 的问答用量字段统一为 `questions`，按归属目标用户的持久 `ask_jobs` 提交次数计数（失败/取消任务也计入），而非 `conversations` 会话容器数量；同一会话内连续提问会分别累计，共享成员的提问不会算到笔记本所有者名下。用户总数包含其在加入的只读共享笔记本中的提交；`GET /api/admin/users/{user_id}/notebooks` 刻意保持 owner-only，因此只分解自有笔记本里的提问，其合计不要求等于用户总数。报告数同此口径：总览按报告创建者计数（含在共享笔记本中自建的报告），明细仍只列自有笔记本，合计同样不要求等于总数。旧 `conversations` 字段为 API 兼容继续返回并标记 deprecated。每个笔记本对用户上传的文档数量设有上限（默认 20，可由 `USER_UPLOAD_DOCUMENT_LIMIT` 配置）；管理员在用户使用总览调整——设置全局默认（`PATCH /api/admin/settings/upload-limit-default`）并为单个用户设置覆盖值（`PATCH /api/admin/users/{user_id}/upload-limit`，传 `null` 清除覆盖、回落全局默认）；管理员拥有的笔记本不受此限。任何管理员都可将 notebook 发布为公共知识库。公共知识库对普通用户的列表隐藏，但可在每个笔记本的参考库选择器里发现，仅对显式挂载了它们的笔记本参与检索。升级到 schema 20 不会回填挂载：所有既有笔记本挂载数清零，联邦检索对它们全部停止，直到用户自己显式挂载一个参考库。非内置用户可在头像菜单自助修改密码，走 `PATCH /api/me/password`（body `{"old_password", "new_password"}`；当前密码错误或新密码空白返回 400）：成功后当前会话保持登录，该用户其他浏览器会话全部吊销。管理员可在用户使用总览通过 `POST /api/admin/users/{user_id}/reset-password`（body `{"new_password"}`）重置某用户密码；目标用户的浏览器会话全部吊销、须用新密码重新登录，且请求必须来自真实登录的管理员会话——`auth_optional` 的匿名回退被拒绝（403）。Agent 长期凭据不在两种吊销范围内。内置 `admin` 账号在两条路径都被拒绝（409）：它的密码每次启动都会按 `SILICON_NOTEBOOK_ADMIN_PASSWORD` 重新写入，改密请修改环境变量后重启；界面对它隐藏「修改密码」入口，用户总览该行密码列显示「受保护」。本地/测试场景可设置 `SILICON_NOTEBOOK_AUTH_OPTIONAL=true` 跳过登录。前端在首次加载时显示登录/注册界面，顶栏展示已登录用户名和退出按钮。
- **分享链接**：owner 可发布不透明 notebook 链接；小 notebook 复制到接收者账号，大 notebook 以只读成员方式加入。写权限仍归 owner；当前没有实时协同编辑。
- **群组共享**：用户按群组（`project`｜`department`｜`domain`）组织，组内两级角色；组管理员经授权边把知识库共享给整个群组。群组共享的库出现在成员笔记本列表的**「群组」分区**，成员可提问、写自己的深度报告并把它挂为参考库，且**读权 ⇒ 可挂载**。详见下文「群组知识共享」章。
- **报告公开链接**：单份**已完成**的深度报告可由有写权限的成员发布成免登录只读页（`POST /notebooks/{nb}/reports/{rid}/share` 发放 token，`DELETE` 撤销，`GET /public/reports/{token}` 匿名读取）。发放是幂等的——重复分享返回同一个 token，已发出去的链接不会突然失效；撤销后与从未存在的 token 无法区分，都是 404。未完成的报告不能分享（409）。匿名端点挂在**独立 router** 上：主 API router 带 router 级 `Depends(get_current_user)`，公开端点挂在那上面会 401 拦掉它服务的访客；它也因此不绑定请求用户，所以只能调用不依赖 current-user 的仓储方法（ContextVar 未设时 `current_user` 会回退 seeded admin）。投影是**白名单**而非脱敏：正文、问题、时间，以及每条引用的标题/原始文件名/位置/摘录。`source_id`、`element_id`、`object_id`、`notebook_id` 与整个 `understanding` 合同（含意图与冻结的来源范围）都不跨出去——公开页本就不能打开原始资料，给出这些 id 只会让人拿去探测已认证接口。资料基础等披露已在生成时固化进 `content_md`，无需另行下发。**渲染管线与站内共用**：正文经同一个 `remarkCitations` 把 `[k]` / 【k】标记链接化成可点编号，编号取自 key 里的序号（后端已全局重编号；公开投影会丢掉既无标题又无摘录的条目，按位置数会和正文对不上），点击跳到本页「引用出处」条目并高亮——公开页打不开原文，所以标记通往的是摘录而不是原始资料。表格/代码块沿用 `.answer-table-wrap` / `.answer-code`，宽内容在自己的内容块里横向滚动；页面必须自带 `katex/dist/katex.min.css`，否则 rehype-katex 产出的 MathML 不被裁掉，每条公式会连着逐字符的 MathML 文本渲染两遍。**截断一律披露、不静默丢尾**：研究问题原样返回，引用的标题/原始文件名/摘录仍有界但超限会置 `title_truncated`/`file_name_truncated`/`snippet_truncated`。精确上限见[下表](#报告公开分享护栏)。
- **问答会话公开分享**：一条**已完成**的多轮问答会话可由其创建者发布成免登录只读页 `/c/{token}`，端到端平移「报告公开链接」（`POST /notebooks/{nb}/conversations/{cid}/share` 发放 token，`GET /notebooks/{nb}/conversations/{cid}/share` 供创建者回读 token/水位，`DELETE /notebooks/{nb}/conversations/{cid}/share` 撤销，`GET /public/conversations/{token}` 匿名读取，`GET /public/conversations/{token}/assets/{alias}` 服务被引用的图片）——同一个独立匿名 router（不带 `Depends(get_current_user)`）、同一种行级创建者门（`_own_conversation_or_404`；「存在但不是你的」与「不存在」同为 404）、同一种白名单投影形状、同一套渲染管线（`remarkCitations`、`.answer-table-wrap`/`.answer-code`，自带 `katex/dist/katex.min.css`）。本条只登记会话形态之上多出来的部分。发放**幂等**，并**同时**推进水位 `shared_through_at`/`shared_through_id`——请求体带 `expected_through_id`（客户端据以算披露的那批 turns 里最新一条答案 id），服务端把水位钉死在**正好那条**，因此披露读取与本次 POST 之间新落的答案**不会**被发布（关闭「披露到 X、公开到更新的 Y」的 consent TOCTOU）；披露的边界答案已被删则拒绝（409，刷新重看），省略/空则回退当前最新；「分享」与「更新到最新」是同一次调用。公开页只渲染水位之前已写入的轮次（生成中的轮次有 `ask_jobs` 无 `answers` 行、构造性排除），边界是对水位答案 `(created_at, rowid/ordinal)` 的 **keyset**（精确含 tie-break，同刻并列时排在水位之后的那条不会被多纳入），仅当 `shared_through_id` 因 answer 被删而解析不到时回退纯 `created_at` 时刻区间。空 `created_by` 会话与零答案会话都拒绝分享（均 409；后者会回滚刚发放的 token）。投影额外剥掉 `reasoning_trace`、`intent`、`retrieval_scope`/`retrieval_query` 与一切可寻址 id（含 `memory_id`）——被引用的 Memory 摘录仍会公开（这是自我发布：创建者只可能引到自己的 Memory），但分享弹窗必须在链接发出前说明摘录条数。被引图片经**按 token 派生的 HMAC 别名**（`conversation_asset_alias`，绝不给出真实 `asset_id`）访问，撤销链接因而连图片一并失效，同一张图在两条不同会话链接下也无法互相关联；图片端点只服务该冻结快照内被引用到的资产，并回 `Cache-Control: no-store`。清单卡（`result_sets`）v1 不投影，但计数会披露而不是静默丢弃（`PublicTurn.omitted_result_sets`）。该弹窗有两个入口、发的是同一次调用：问答历史弹层里会话卡片上的分享按钮发布整条会话（边界 = 当前最后一条答案），每条回答动作行里（排在复制按钮之后）的分享按钮则把该答案的 id 作为 `expected_through_id` 发布到这条回答为止。边界模式另有三条界面侧规则：点中的那条 id 逐字发出，绝不在会话详情加载失败时退化成空请求体（空体会按当前最新发布，也就是比弹窗承诺的更多）；边界排在已发布水位**之前**时会被 advance-only 的存储层拒绝，因此该支不提供任何发布动作，改为说明链接已经覆盖这条回答以及其后 N 轮，并指出先撤销再重新分享是缩小范围的唯一途径；同一支的披露计数按完整轮次列表统计而非截断后的那批，因为它描述的是链接当前公开了什么。精确的轮数/引用数/摘录/问题/图注/别名上限见[下表](#问答会话公开分享护栏)。
- **绑定 notebook 的私有 Memory**：用户可手动把 Ask 回答生成可编辑预览，并在确认后沉淀为可复用 Memory。外层提供用户级总 Memory 页面，notebook 卡片显示当前用户的数量，工作区为 **问答**（Ask） | **知识库**（Knowledge） | **记忆**（Memory） | **深度报告**（Deep Report）。外部 Agent 可经 MCP 提交 `candidate`；它只在同一用户、同一 notebook 的获授权 Agent 间共享，用户确认前不会进入正式 Ask/搜索/报告检索。
- 可选图推理问答模式（`mode="graph"`，opt-in / 实验性）：基于 `knowledge_relations` 构建 rustworkx 内存图，做有界多跳 derivation/support 链遍历，答题时做对抗式链路校验并给出最弱环 `chain_trust` 分（默认 Ask 仍为 `chunk`）
- 深度报告（两阶段后台任务）：notebook 级「深度报告」动作把一个问题变成多节技术报告。**阶段1a 是完全不读语料的问题理解**：提取可编辑的最终研究问题、目标、必答主题、实体、比较轴、约束、排除项、期望输出、暂定假设、置信度与最多八个阻断性歧义，不调用 notebook 检索。报告停在 `intent_ready` 等**创建者**确认，除非创建请求带 `auto_generate=true` 且问题清晰（无阻断性歧义）——此时服务端复用人工确认端点的同一份确定性冻结自动确认意图（不二次调用理解模型）并直接推进到规划；带阻断性歧义的问题无论是否 `auto_generate` 都仍停在该状态，必填歧义必须先回答，人工或服务端才能确认。带来源/参考库范围的报告在自动确认前还会重跑与人工确认端点相同的范围重验；重验不过（意图理解期间来源被删、参考库被卸载）就留在确认门，事件 reason 记 `scope_invalid`；重验通过时采用刷新后的冻结范围（期间新增来源/挂库导致的 narrowed 变化会像人工确认一样落进持久化 understanding 与后续规划/生成的范围上下文）。确认权归**报告的创建者**而不是笔记本 owner：共享库里成员建的报告由该成员自己确认，谁都不能确认或推进别人的报告（行级 `created_by` 隔离，见「群组知识共享」章），owner 也不例外。「只能等待」因此只对**别人的**报告成立。自动推进失败或竞态一律 fail-open 留在原状态、人工确认门保持可用，只发无正文事件 `report_intent_auto_confirm_skipped`。确认操作（人工或自动）以数据库原子转换认领 `intent_ready → planning`，并确定性冻结用户已经看过的合同，不会再调用一次隐藏的理解模型；澄清答案只补充内部检索/写作问题，不进入报告可见标题。**阶段1b 仅在意图确认后开始**：确认后的问题和答案成为权威输入，再对每个必答主题做有界的零 LLM 覆盖探针，同时统计联邦 KG 与直接解析 `SourceElement` 命中；此后 STORM 式规划器才使用来源标题、KG 命中和 chunk 出处来改进术语、排序、专家视角和张力。语料不足只能形成缺口，不能替换或收窄用户明确要求的主题；代码会验证映射并补回模型漏掉的必答主题。大纲编辑器展示每节对应的用户问题、可编辑检索方向，以及原文元素/KG/公共库覆盖；绑定某个必答主题的最后一节不可删除，API 同步强制此约束。**阶段2（确认大纲后）**：除完整 `reasoning` 深挖外，每条已确认检索方向都会实际执行；各节并行。chunk、KG 对象、类型化关系、confirmed Memory 与直接 `SourceElement` 共用 `[k]` 绑定链路，原始 element 不再只是不可引用的提示附文：小库可直接评分 element；不可复制的大库先走有界 chunk ANN/FTS，再只按命中 chunk 的精确 `element_ids` 做有界主键 hydration，绝不全表加载 element 文本/向量。参考文献按具体证据锚点去重，不再按来源标题折叠，点击报告引用可展开其绑定的来源、位置和原文片段。Ask 与报告引用仅在来源已接地判定为论文（`is_paper=true`）且解析出非空 `paper_title` 时优先显示论文名；其余情况继续显示普通来源名/文件名。引用响应另以 `source_file_name` 携带持久化上传文件名；当它与显示标题不同时，Ask/报告引用卡显示「原始文件」，挂载公共参考库的证据同样适用。该值只可来自 `sources.file_name`，绝不能使用 MinerU 临时/输出 Markdown 名。模型返回的 `grounded` 仅是建议，后端会重新解析锚点，并要求被引证据达到相关度阈值。最终编辑器只生成执行摘要并标记未完整回答的必答主题/跨节冲突，不改写章节、不新增事实。原有（推断）/【通识】纪律、五档研究深度、`KG_JOB_CONCURRENCY` 并行、实时 `section_status`、取消和 Markdown/ZIP 导出保持不变。`ReportSummary` 与 `ReportDetail` 新增返回 `updated_at` 和 `generation_started_at`；后者在成功原子认领 `outline_ready → generating` 时写入报告内部状态。已完成报告的列表与详情用 `updated_at` 显示浏览器本地时区的精确生成时间，同时保留相对时间，并展示从 `generation_started_at` 到最终写入的总耗时。意图确认和大纲确认的等待时间不计入；旧报告缺少生成开始戳时不编造耗时。未完成报告只显示创建时间，不冒充已有最终耗时。
- **深度报告容量、输出与重试护栏**：整篇生成按 `REPORT_GENERATION_CONCURRENCY` 准入（每个后端进程默认 1 篇）；已准入报告至多同时运行 `REPORT_SECTION_CONCURRENCY` 节（默认 5），并同时受所绑模型服务容量和 `POSTGRES_POOL_MAX_SIZE - 2` 约束——后者只约束节级扇出：每节自身的子查询扇出仍可能短暂借用更多池连接，等待者受池获取超时约束，因此这道闸限制的是池压力上界，不是为在线请求预留固定连接数；排队报告不持有数据库连接。分节撰写使用 `REPORT_SECTION_MAX_TOKENS=65536`；详尽档全篇蓝图与最终只读终审分别通过 `REPORT_SYNTHESIS_MAX_TOKENS`、`REPORT_SUMMARY_MAX_TOKENS` 使用独立的 `102400` completion 上限。这些配置是 completion 上限，不是应用侧总上下文声明，也不会预占输出；实际绑定 provider/model 必须能在对应 workload 的 prompt 下接受该上限。蓝图提示只选择承重主张，单节最多 12 条、全篇最多 60 条。主张的 facet 标签接受 `id:value` 复合形态并确定性归一为合法前缀；标签写成某个 facet 的名称、声明取值，或其 id/名称/取值的大小写变体时，只要拼写无歧义即确定性修复为所属 facet id（声明 id 优先于其他 facet 的名称/取值；两个及以上 facet 共享的拼写绝不猜测归属）。修不回来的标签只清空该条主张自己的标签——facet 标签是组织性标注，绝不因它作废整份蓝图；证据绑定、章节归属与结构仍原子校验。修复/清空计数以仅含计数与不透明报告 id 的 `report_synthesis_facet_tags` 事件记录（绝不带 facet 拼写），综合 prompt 会逐字枚举 frame 的合法 facet id。综合失败的披露语义不变。若全部章节都为空或失败，报告终态为 `failed`；至少一节有效时继续 fail-open，并把失败节显式写入报告。保留已确认大纲的失败报告可原子重新进入 `generating`：重试保留冻结意图/大纲、重置 `generation_started_at`、清空旧生成产物，绝不重新理解问题或规划。认领后的排队时间计入生成耗时。
- **Retrieval run、充分性与 reasoning 动作护栏**：一次报告规划/生成 run 在全部节 worker 间共享 `REPORT_RETRIEVAL_FANOUT=8` 个叶子 KG/chunk/element/PPR 槽；规划的独立 KG/原文元素探针使用 `REPORT_PROBE_CHANNEL_CONCURRENCY=2`（校验范围 `1..2`）。等待报告 leaf 槽位时会有界检查取消，拿到槽位后、发起 I/O 前再检查一次；已进入底层数据库/后端调用的 leaf 安全收束，不脱离成后台任务。Ask 共用同一 request-local 成功 query embedding single-flight，不改原扇出。报告充分性至少要求 `REPORT_SUFFICIENCY_MIN_RELEVANT_ITEMS=3` 个相关证据单元与 `REPORT_SUFFICIENCY_MIN_FAMILIES=2` 个可区分来源族（完整范围用 `REPORT_SUFFICIENCY_COMPLETE_MIN_FAMILIES=3`），且 `REPORT_SUFFICIENCY_MAX_TOP_FAMILY_SHARE=0.8`。reasoning run 最多执行 `REASONING_MAX_PPR_RETRIEVES=3`、`REASONING_MAX_EXACT_LOOKUPS=3`、`REASONING_MAX_FOLLOW_CHAIN_ACTIONS=3`；跨库社区同伴总帽是 `REASONING_COMMUNITY_PEERS_CAP_FACTOR=2 × COMMUNITY_PEERS_TOPK`，大纲更新最多 `REASONING_MAX_OUTLINE_UPDATES=6`。默认值与原内联规则完全相同，部署只应基于冻结评测集调整。规划子阶段、retrieve/synthesis/draft/final-editor、章节外层尝试和 retrieval-run 缓存/扇出计数都只发送 fail-open、无内容事件，字段仅限阶段/run 类型、不透明 report/run id、索引、计数、状态和毫秒。
- **邻居展开上限与后台任务固定 worker 队列**：逐步推理的一次 `expand_graph` **每个方向**（出边/入边各自计算）最多展开 `REASONING_NEIGHBOR_EXPAND_LIMIT=1000` 个**唯一合格邻居**，因此即便碰上病态枢纽节点，邻居知识对象的取数也保持有界。上限的单位是邻居而不是关系行：同一个邻居常带多条重复/佐证关系，还有一部分行会被判为不可查边，所以数据库读取界取邻居预算的四倍（有意的过扫描：把这些行吸收掉，而不是让它们吃掉预算、把靠后的合法邻居整个略过）。对象状态的合格性在读取上限**之前**于 SQL 侧生效（只作用于 JOIN 的邻居那一侧），因此指向 deprecated 等对象的关系不会占掉有界读取窗口、把排在它们后面的可用邻居挡在外面。取行仍按稳定的关系 id 序，走既有 `(notebook_id, source/target_object_id, id)` 索引。截断绝不静默：展开轨迹步带 `neighbor_truncated` 与该上限，反思循环也会收到「哪些节点只展开了一部分」，让模型改换动作，而不是把它当成「这个节点只有这些邻居」。另外，后台任务使用**两个互相独立的固定 worker 队列**，分池判据是量级差而不是重要性：重活（知识图谱分析/重建、补上关联、统一图重建、冲突检测、合并预审）在每个后端进程内至多使用 `BACKGROUND_MAINTENANCE_CONCURRENCY=4` 个 worker；秒级轻活（论文元数据补抽、命令目录识别、knowhow 投影与孤儿资产清扫）另有 `BACKGROUND_LIGHT_JOB_CONCURRENCY=4` 个 worker。合用一个队列时，几个小时级重建就能把用户点一下就该出结果的格子投影饿死。提交只入队并立即返回，绝不为每个等待的维护任务创建一个线程；交互路径 `ask-*` 与已有整篇准入闸的 `report-*` 刻意都不进这两个队列。需要知道的后果：任务排队期间在库里的状态仍是 `running`/`queued`，界面暂不区分「排队中」与「执行中」；排队只在后端日志披露（等待越过阈值记一条 warning，开始执行后再记一条说明等了多久的 info，只带池名与任务类别，绝不带 id）。
- 边可信与治理：每条边的可信信号（evidence / 同源佐证 / 类型合法性）+ 高风险边优先的审核队列；被审核拒绝的边从图推理中排除
- 知识治理：通过 `/knowledge-types` + `/knowledge?type=...` 浏览任意对象类型，状态生命周期，重复检测与合并；`deprecated` 对象从检索和 1-hop 扩展中排除。个人→基准节点晋升（propose → under_review → approve/reject），批准时去重入库，配套策展晋升队列
- 统一 KG：跨文档概念聚类（`concept_clusters`），待合并审核
- Object 级 KG 可视化：Concept / Claim / Formula / Procedure 节点，类型形状、边标签、多选过滤、按类型分组侧栏
- Notebook 集合页（网格/紧凑/列表、编辑/删除）；点击「＋ 新建」直接创建 `Untitled notebook` 并进入，无弹窗
- 第一版不使用 Docker

PostgreSQL + pgvector 仍是后续生产/团队 beta 目标，当前本机开发不需要。

## 产品流程

外层页面为 notebook 集合页（KG-native 管线）：

1. 点击「＋ 新建」——系统立即创建 `Untitled notebook` 并进入，无弹窗。
2. 上传 PDF、Markdown、DOCX、PPTX、CSV、XLSX 或旧版二进制 XLS 来源（multipart）。登录后的系统配置返回一份脱敏解析能力注册表，驱动上传校验与导入界面的支持格式提示；服务端 endpoint、路径、凭证与原始异常绝不下发。解析路由流程本身（自托管 MinerU → MinerU 公共云 → 内置兜底的顺序、能力、执行边界、可用状态）刻意不向用户展示——用户只需要知道支持哪些格式。选择始终自动完成，已配置的自托管路径绝不会被静默替换成公共云。
3. 后端（异步后台作业）：结构化 Markdown 解析 → 分块 + 向量化——源处理完即可做 chunk-native 问答。
4. **KG 抽取按需触发**（见下方「KG 抽取触发」）：摄取期仅当该 notebook 已有 KG、或 `KG_AUTO_EXTRACT=true` 时才抽。`KG_JOB_CONCURRENCY` 只控制并行来源任务；每次抽取模型调用都由 `kg_extract` workload 所绑定服务的系统调度器准入，因此服务 TOML 中的 `max_concurrency` 始终是唯一模型容量上限。抽完的新源随后增量融入统一 KG。
5. 知识对象写入 `knowledge_objects` + `knowledge_relations`，并绑定元素级 evidence。
6. 混合检索（bi-gram 关键词 + float32 矩阵语义）驱动 KG-native 问答：答案含逐句 `[k_i]` 引用，支持多轮会话，并沿 KG 关系做 1-hop 邻居扩展。
7. 统一 KG 跨文档聚合概念；待合并的跨文档概念对可逐一确认或拒绝。

进入单个 notebook 后：

- 顶栏：左上角只保留可编辑 notebook 标题；notebook 描述在没有对话时显示到问答欢迎态里，顶部工具栏在桌面宽度下保持各动作标签完整。
- 左栏：用户导入来源文件，实时显示 parse-status（绿色仅给 `extracted`，其余处理中为橙色），并按后果分级显示来源异常徽标（完整性问题如解析失败标红、仅影响检索的问题如部分内容未分析标黄；待补全等中性待办状态只在来源详情显示），支持详情预览、删除，以及由问答和新建深度报告共用的检索范围复选框。来源详情每次只取并渲染 40 个 element 的有界页（API 单次上限 100），前后页按需加载；引用跳转由后端解析到包含目标 element 的页，因此打开大文档不会一次水合和挂载全部元素。所有面向用户的来源计数只计这组可见的导入来源，排除隐藏的 `memory` / `knowhow` 投影来源。网络来源检索暂不开放。
- 主栏：四个 tab——**问答**（Ask）、**知识库**（Knowledge）、**记忆**（Memory）、**深度报告**（Deep Report）。Ask 提供逐句 `[k_i]` 引用、三种检索模式、多轮会话、实时推理轨迹与反馈；鼠标悬停问题气泡或回答卡时在下方显示时间，点击后固定显示，点击其他位置后恢复悬停逻辑。问题采用已持久化的网页端提交瞬间，回答采用 `AskResponse.answered_at` 返回的权威答案写入瞬间（旧 payload 从 `answers.created_at` 投影）。时间按浏览器本地时间格式化：今天只显示时间；本周（周一为一周起点）内的其他日期显示星期与时间；超出本周显示日期与时间，日期和星期二选一；今年省略年份，其他年份显示年份。会话历史收进 Ask 顶栏的单行 `历史 N` 入口和可展开管理面板，旁边的 `+` 会直接开始新会话。历史按带亚秒精度的最近活动排序并显示活动时间；首轮问题一提交就会立即出现，即使在 `started` 到达前切到同库旧会话，模型仍在回答时也能重新打开。终态历史摘要按当前 notebook 独立刷新，不依赖原 run 继续占有回答区；同库列表调用会收敛到最新请求。加载 notebook/会话的最新详情期间，输入框与模式控制保持禁用。Knowledge 负责动态类型浏览与治理；Memory 只显示当前用户绑定在此 notebook 的私有记录；Deep Report 负责两阶段报告、大纲审阅、进度、导出、取消和删除。问答输入框中 `Enter` 发送，`Shift+Enter` 保留换行；模型处理中锁定输入与模式切换，发送按钮切换为中断控制。transport 断连只停止向当前客户端继续推送；导航、刷新或 transport 丢失后 detached Ask job 仍在后台运行并可保存最终回答。用户点击中断则调用 `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel`，由后端设置取消事件，使 worker / LLM 路径停止，且不保存被取消的最终回答；如果点击时首个 `started` 尚不可读，界面立即恢复草稿，但该 run 的 transport 只继续读取到取得 job id，完成后端取消后即 abort。主工作区保持两列且没有固定 Studio 右栏。
- 知识图谱以全屏浮层打开：object 级 KG 节点（Concept / Claim / Formula / Procedure），类型形状，边关系标签，多选类型过滤，按类型分组侧栏（选中节点聚焦画布）。从问答知识对象引用打开时会精确定位对应节点：目标不在有界高连接度核心图时，前端按引用真实来源 notebook（包括挂载 base）叠加其有界一跳邻域，纯 graph-BFS anchor 也保留所属库 id。浏览器仍以当前 active notebook 过权限，后端只在其有效 participant 集内校验或解析对象并内部代理 base 的邻域/详情/context 读取；挂载公共 base 不会授予该 base 的直接成员权限。引用携带的原始 Concept id 由邻域接口通过单 id 聚类查询解析成 canonical `focus_id`，同时保留 raw object id 供 context 读取，不能用合成图节点 id 查询 `knowledge_objects`。大库 viz 产物仍在构建时，接口会显式返回暂不可定位而不进入全量 cluster-map fallback，前端也不会留下无法消费的 pending focus。侧栏的「出处」以结构化证据卡片展示，长标题、位置、公式与中英混排正文都限制在面板内；来源元素类型为 `formula` 的摘录会走共用块级 KaTeX 渲染，不再直接显示命令文本。
- 知识图谱视图头部还有一个**图谱分析**按钮（批 1，共 4 批；见 `docs/superpowers/specs/2026-07-25-kg-analysis-view-design.md`），与「图谱 Schema」并列；报告读取仍只要求 notebook 读权限。面板先给出可操作的结论，而不是直接堆技术数字：报告是否可信、各对象类型的合并信号、主题结构信号，以及首个需要复核的来源。界面直接说明收敛率与关联度是诊断信号而非越高越好的总分，并给出红／黄／灰／当前状态图例。五行产物账本逐项说明每份数据回答的问题，最大概念合并组与关联形成方式则展开为完整诊断区块，不再只显示一条账本回执。可编辑成员还会看到「生成分析／更新分析」动作：它复用 `POST /notebooks/{id}/unified-kg/rebuild`、现有确认框、按笔记本单飞后台任务与 `job_id` 配对完成轮询，不重新抽取来源，任务完成后自动刷新报告；只读成员仍可查看相同报告，但没有写动作。`GET /notebooks/{id}/kg-analysis` 返回对象构成、**按对象类型分列**的合并收敛率（concept / claim / formula / procedure 分开算——四类混算会把 concept 真实收敛率稀释约 3 倍）、主题板块列表与跨板块边（供俯瞰图使用）；`GET /notebooks/{id}/kg-analysis/sources` 分页返回逐来源画像，默认按「与主体板块最不连通」在前排序，可切换为「最紧密」在前。与其它所有面向用户的来源计数同口径，来源画像也只覆盖可见的导入来源：隐藏的 `memory` / `knowhow` 投影来源的对象在**预计算**时就被排除，因此这些内部标题不会进报告，也不会把「最不连通」的排行头部占满。孤儿引用（`source_id` 指向已被删除的来源）则刻意**保留**并标记 `source_missing`——那是诊断信号，不是隐藏来源。两个端点只读 `rebuild_communities` 顺带产出的三张预计算产物表（`kg_community_edges`、`kg_source_profiles`、`kg_analysis_artifacts`），在线路径不做全表扫。这三张表与**板块划分本身**在**同一个**写事务里发布，所以报告永远不会把新一代板块与上一代账本拼在一起。每个数字都标注自己建于哪一代、落后当前多少——逐指标标注，不是整页顶一条「可能过期」的横幅。世代有**两条且相互独立**：`kg_mutation_seq`（对象与关系的写入）与 `cluster_mutation_seq`（合并结果）。合并的写路径刻意不动前者，所以从 `concept_clusters` 算出来的四份产物（簇大小直方图、最大簇榜单、跨板块边、来源画像）另带 `built_at_cluster_seq` / `cluster_seq_behind`，一次纯合并写入就会让它们变陈旧；`relation_provenance` 只读关系表、不盖这个戳，也刻意不被合并作废——重算它等于白跑一趟关系全表扫。依赖板块的那两份（跨板块边、来源画像）盖的是**它们描述的板块划分**建在哪一代合并结果上：同一轮重建过划分就是整数，只补账本的那一轮显式记 `null`——那时的划分是库里现成的，而它建在哪一代没有任何地方记。所以 `stale` 是**三值**的：某条线明确落后为 `true`，两条都对齐为 `false`，合并世代无从判断为 `null`；`null` 不等于 `false`，界面单独出一条提示而不是说「与当前一致」。产物账本对**两条**世代线都有独立于社区层的新鲜度闸，已经建过社区的库在下一次普通整理时就能补齐账本，不需要强制重建。主题板块列表本身走**同一套**三值判据，不是另写一份：它的 KG 世代那条线是 `community_seq`，合并世代那条线读的就是上面那个戳——那个戳记的本来就是「板块划分建在哪一代合并结果上」，而依赖板块的两行账本与重铸板块 id 同事务作废，所以「行在」就意味着它描述的正是当前这套划分。于是纯合并写入之后，板块那一格与建在同一套划分上的那两份产物**同时**报「对不上合并进度」，而不是因为 `kg_mutation_seq` 恰好没动就自称「与当前一致」。该动作只刷新派生分析状态；面板仍不执行删除／隔离治理。
- 「分析」菜单本身只包含晋升队列（admin）、发布/撤回公共知识库（admin）与边审查队列。看板、全屏知识图谱是其他顶栏动作；「图谱 Schema」已从顶栏移入知识图谱视图头部，不再是独立顶栏动作。管理员维护全局对象类型基线；笔记本 owner 可查看当前生效基线、copy-on-write 改写继承定义、仅在本库停用该类型，或新建本库专属类型；删除覆盖会恢复全局定义。只读成员可查看同一份生效定义但没有写控件。管理员还可切换到全局基线视图，其改动只影响尚未覆盖同名类型的笔记本。当前不再暴露已退役的内容生成或派生规则动作。现有 notebook 分析视图提供独立的 Memory 和 Knowhow 内容资产卡片：Memory 指标严格限定为当前登录用户和当前 notebook（admin 也不跨用户汇总），Knowhow 指标遵循 notebook 的既有读取权限。卡片只展示计数、健康度/最近活动摘要和跳转入口；浏览与编辑仍复用现有的 Memory、Knowhow 页面和编辑器。

知识对象类型的显示名只有一份真源：后端 `app/domain/extraction_profiles.py` 的 `OBJECT_TYPE_LABELS`（`app/services/extraction_profiles.py` 只是 re-export shim），由 `GET /notebooks/{id}/knowledge-types` 以 `KnowledgeTypeCount.label` 下发给前端。凡是拿得到这个 API label 的调用点——Knowledge 浏览器的类型 tab 与条目——一律直接使用它，因此用户自定义类型（例如 knowhow 表列名投影出来的类型）同样能显示正确的中文名。只拿得到 `object_type` 字符串的调用点——引用浮层与知识图谱画布/侧栏——回落到前端内置小表 `frontend/app/kg-type-model.ts` 的 `KG_TYPE_LABELS`；`kg-type-mark.tsx` 消费并 re-export 该模型供共用渲染。该表逐字等于后端常量；`scripts/check_object_type_labels_contract.py` 作为硬门挂在 `scripts/check.sh` 里，两份一旦漂移即构建失败。未知/自定义类型一律原样显示其 `object_type`，绝不 TitleCase 成臆造的英文。这两张表的键都由用户可控字符串索引，查表必须走 `Object.hasOwn(...)` 而非裸下标：`constructor`、`__proto__` 会命中原型链上继承的函数/对象，而不是「查不到」。

面向用户的文案另有一份词汇契约，真源是 `AGENTS.md`「界面词汇表」：表中每一行把一个内部词（基准库、chunk、KG、抽取、投影、晋升、schema、deprecated……）映射到界面唯一允许使用的说法。内部名保留在代码、类型、注释与架构文档里——只有渲染给用户看的字符串才改写；而**被持久化**而非被渲染的值（`Untitled notebook` 这个默认库名、协议上的 enum id）属于契约不属于文案，任何一轮措辞调整都不得顺手改动它们。`scripts/check_ui_vocabulary.py` 作为硬门挂在 `scripts/check.sh` 里执行该表，其**作用域跟着信任边界走、不跟着目录树走**：既扫描 `frontend/app` 每个源文件的渲染文本——字符串字面量加 JSX 文本节点，并先剥离注释、标识符、正则体与 `${…}` / `{…}` 插值——也扫描后端每处 `user_error(status, "…")` 的消息字面量，因为 `api/deps.py` 恰恰只给这批 4xx `detail` 打上 `X-User-Message: 1`，而 deny-by-default 的前端见到该标记就把它原样显示给用户。打标记等于声明「这是给人看的文案」，那就同样受这份词表约束；此前把守卫圈在 `frontend/app` 里，正是「仅管理员可设置基准库」「仅管理员可管理晋升队列」四条 403 一路上屏而守卫全绿的原因。裸 `HTTPException(detail=str(exc))` 刻意不在扫描面内——它永远不上屏，detail 是诊断 / MCP 契约，这条分界由 `backend/tests/test_user_error.py` 守。任一侧命中黑名单词即构建失败。另有一条独立守卫 `frontend/app/raw-enum-fallback.test.mjs`（由 `npm run test` 递归收集，因而同样是 `scripts/check.sh` 的硬门），拒绝「兜底即原值」（`MAP[x] ?? x`，以及通过正规 API 达成同一效果的 `label(map, x, x)`）：这种查表一旦后端新增枚举值，就会把英文 id 直接渲染给用户；应改用 `frontend/app/vocabulary.ts` 的 `label(MAP, value, fallback)`，它强制传中性兜底词，使该 bug 写不出来。该检查跑在真正的 TypeScript AST 上而非正则：渲染位置的 `M[x] ?? x` 与内部归一化的 `ALIASES[v] ?? v` **语法形状完全一致**，只有上下文能区分泄漏与正常代码——正则版误报了后者，又整个漏掉了 `M?.[x] ?? x`、`getLabels()[x] ?? x` 与 `label(m, x, x)`。它自己的文件头如实写明仍然看不到的部分（先算进变量再渲染、`alert(...)` 这类非 JSX 出口），诚实标注优于假装全覆盖。若确实要原样透出**用户自己写的**字符串（自定义 `object_type`、用户自建的 schema 字段名），则显式写成 `Object.hasOwn(...) ? ... : raw`，顺带规避上面那个原型链隐患。该守卫是词黑名单而非语义检查：有两行只覆盖其无歧义的复合形态——图谱视图里裸用「节点」「边」是正当的，且「边」与「旁边」「边框」同形。`backend/tests/test_ui_vocabulary_guard.py` 存放它的正例与反例，并额外在「词汇表新增一行却既没有对应规则、也没有登记豁免理由」时失败，使黑名单无法悄悄退化成只覆盖词表的一个子集。

重新解析保留 source 行与原始文件：替换 source element / chunk 及其 embedding，并在重建前删除 extraction run 与 source-derived knowledge。删除复用同一 source-derived cleanup，随后删除 source 行（外键级联 source-owned records）与本地文件。

可见导入来源计数与物理记账刻意分离：隐藏的 Memory/Knowhow 投影来源不会出现在来源栏或面向用户的计数中，但 `size.sources`、复制阈值、存储统计和后台调度仍按物理行计数。`has_unindexed_content` 也会在可见导入来源增量为零但派生内容发生变化时保留 scale-index 更新决策。

检索索引调度提供立即执行与低峰排队两种操作。没有构建正在运行时，`when=now` 会在认领立即构建的同一临界区内覆盖同 notebook 先前的 idle 项；认领后新加入的 idle 请求仍会保留，worker 启动失败也会恢复被覆盖项。调度 tick 会逐个认领队列项，忙碌 notebook 的后续任务继续排队，单项启动失败不会丢失或阻断其余项。`AskResponse.index_required` 只记录回答生成时的降级状态；问答界面还会读取实时 `ScaleIndexStatus.exists`，有界前台轮询结束后由 `index_done` 事件刷新当前 notebook，因此索引发布后无需改写历史回答即可同步移除旧提示。

当笔记本的 `ScaleIndexStatus.state` 为 `"queued"` 时，状态响应会披露排队的具体原因，而不只是一个裸的排队标记：`queue_position`（从 1 开始）与 `queue_length` 描述在低峰队列中的位次与队列长度，`queued_at` 给出该项**首次**入队时刻（UTC ISO 字符串；重复排队只更新构建模式、保留原时间戳——与按插入序的位次同锚点，两者不会自相矛盾），`offpeak_in_window` 说明服务器当前是否已处于空闲时段内，不在窗口内时 `offpeak_next_start_at` 给出下一个空闲时段开始时刻（UTC ISO 字符串），`last_build_ms`（0 表示未知）携带上一次构建的实际耗时，供界面设定预期。以上六个字段均为可选，旧后端缺字段时前端优雅降级为此前的通用排队文案。已有已发布索引的排队笔记本同时仍带出 `last_built_at`，看板卡片排队时因此能继续显示「上次构建 X 前」。`queue_position` 是首次入队序（服务端按首次入队时刻排序推导，工作线程启动失败后的队列恢复不会改变位次），不代表等待顺序——低峰窗口一开，`_process_idle_queue` 会对队列里每一项并发启动构建，不承诺谁先谁后；前端 `queuedScheduleHint` 也因此只在队列长度 ≥ 2 时报告「共 N 项」而不报告位次。数值档位与内部术语不会原样上屏。铃铛（待确认中心）现在也会透传 `type:"index"` 条目底层的 `"queued"` 状态（此前会被改写成 `"building"`），并展示独立文案「索引已排队，将在空闲时段构建」，不再带百分比进度。

notebook 工作区隐藏集合页全局上边栏，采用偏工程风格的视觉治理。Ask、报告、Memory、Knowhow 展示的 Markdown 中，即使独占一行的单行 `$$...$$` 紧邻正文，也仍按块级公式渲染；宽块级公式只在自身内容块内横向滚动。来源详情、知识对象与知识图谱出处卡的公式视图在直调 KaTeX 前会剥除包裹整值的 Markdown 数学定界符，仍无法解析时显示原始文本，畸形公式输入不会再变成空白可视化。渲染模型产出文本的三个面（Ask 回答与 Memory 卡片、深度报告、报告公开分享页）里，**单个 `~` 是字面量而不是删除线**：GFM 删除线必须写成规范的 `~~文本~~`。这不是风格取舍——放任单波浪线成对，中文技术答案里 `7~5nm`、`80~90%`、`2~3 周` 这类区间与 `~3GHz` 这类约等于同段出现两个就会被配成一对，中间整段正文被划掉。Knowhow 格子预览刻意仍按单波浪线成对（它与格子 Markdown 规整的判定口径互相论证，见 `AGENTS.md`）。

### 按来源选择检索范围

来源侧栏初始全选所有可见导入来源，每行提供复选框，并提供“全选”/“清空”。当前选择同时传入不读语料的问答意图预检、问答执行和新建深度报告，约束当前 notebook 的内容块、来源元素、知识证据与关系、图路径/PPR 输出以及报告检索。范围被收窄时，隐藏的 Memory/Knowhow 投影证据也不参与，因为这些内部来源没有面向用户的复选框。这份隐藏证据里两种来源的范围不同：Knowhow 投影是笔记本级共享的，会进入每位成员的上限；Memory 投影按创建者私有，只进创建者自己的上限，且过滤就发生在那一次读取里，所以共享笔记本绝不会把一位成员的私有 Memory 暴露给另一位。已挂载的参考库是独立参与者，始终保持在范围内。

`AskIntentPreviewRequest`、`AskRequest` 和 `ReportCreate` 接受可选的顶层 `source_scope`：`{ "mode": "include" | "exclude", "source_ids": string[] }`。`include` 只放行列出的当前 notebook 可见来源；`exclude` 放行除列出来源以外的所有当前 notebook 可见来源。省略该字段保持历史整库范围，`exclude` 加空列表是前端表达“当前所有可见导入来源”的紧凑形式。API 入口会校验每个显式范围并冻结为明确的 include 列表；服务端还会按当前可见来源总数计算 `narrowed`，覆盖客户端提交的同名值：冻结“当前全选”的快照不能被误判成真的排除了来源。因此全选运行（包括只有一篇文章的 notebook）保留对话历史和正常图扩展/推理通道，只有集合确实变小时才启用受限模式的跳过。服务端会为全选运行私下快照当时已有的隐藏 Memory/Knowhow 参与者 id，真正收窄才排除它们；这些 id 不进入公开 scope 响应或持久化合同。两种模式都在来源可分区候选与结果校验中保留冻结快照，所以并发新增来源不会扩大已在运行的请求；若当前可见来源全集与全选快照不再一致，无法安全隔离的整图通道会在 I/O 前关闭。后端对外库、隐藏或已失效的来源 id 返回 422；若本地有效范围为空且未挂载参考库，问答/意图预检/报告创建返回 409。浏览器同步禁用问答输入和新建报告控件，但仍可查看既有报告。报告会把解析后的公开范围持久化在问题理解合同中，在意图确认与生成前重新校验，并为规划和写作重新水合私有参与者快照。

这个检索范围有**两个互相独立的维度**，都由问答意图预检/执行和新建深度报告共用、都默认全选：当前 notebook 每个可见导入来源一个复选框（`source_scope`），每个已挂载参考库**整库**一个复选框（`base_scope`，不展开到库内来源）。`AskIntentPreviewRequest`、`AskRequest` 和 `ReportCreate` 在 `source_scope` 之外接受可选的顶层 `base_scope`：`{ "mode": "include" | "exclude", "notebook_ids": string[] }`。`include` 只放行列出的挂载库；`exclude` 放行除列出库以外的全部挂载库；省略该字段保持「全部挂载库无条件参与」的历史行为。`notebook_ids` 只能指向当前已挂载到该 notebook 的参考库（否则 422）；与 `source_scope` 完全一样，API 入口把每一次提交——包括 `exclude` 加空列表——冻结成显式 include 快照，并在服务端重算 `narrowed`、忽略客户端提交的同名值。冻结对报告最要紧：解析后的范围持久化在问题理解合同里，并在意图确认与生成前原样重新应用，所以报告创建之后新挂载的参考库不会静默参与它。

两个维度**正交**。`source_scope` 的收窄关的是**当前库自己**的通道（PPR、私有 Memory、社区报告、弱支撑关系、精确章节查找、报告整库画像）；取消一个参考库的勾选绝不能顺带关掉其中任何一个，否则用户「少借一个库」就要为此付出当前库检索质量下降的代价。跨库通道各自认库维度：集合枚举与集合地图、联邦候选检索、社区扩展、图漫游、`follow_chain`、证据装配，以及 **KG 可用性闸**。最后这一个是**判据**而非结果过滤——本库无图、唯一带图的库被取消勾选时必须判「无图」，否则 `kg_required` 不翻真、graph 路径会跑在一份这次不许读的图上；它经与候选检索同一个 `resolve_participants` 收口解析参与库，判据是**冻结的选择**（提交过即认），不看是否收窄。会话历史是唯一同时认两个维度的闸：上一轮答案可能引用了用户刚取消勾选的那个库的内容。

库级收窄**只在一个边界**生效——参与库列表，过滤一次、被计划/遍历/分母/收尾指纹共同读取——因为枚举要求**行与计数出自同一谓词**：只过滤行而分母仍把全部挂载库算进去，会让一次真的走完的清单被永久判成 `concurrent_change`。`enumeration_active()` 不受影响：工具照常提供，只是作用域收窄。**泄漏面包括查询词本身**：社区扩展拿参考库的兄弟**实体名**当查询词，这些词会进可见轨迹、进已用查询记录并回喂反思，所以收窄必须发生在取实体名的入口，光过滤结果无效。`resolve_participants` / `mount_sql.py` 不动——它与**权限**判定共用（跨库来源详情代理、引用解析、图片资产读取），一个按请求的检索复选框没有资格收窄授权集合。

两条二阶代价已登记并接受：图漫游与 PPR 的过滤在遍历/截断**之后**，因为联邦图按 scope 无关的键在进程级缓存——被排除库的节点仍占扩散名额、仍可当中转跳，但它的内容一个字都不会进渲染出的 prompt、也不可被引用。整图规模守卫同样保持 scope 盲，所以取消勾选一个超大参考库并不会把那道守卫重新关掉。

**409「范围为空」的判据现在是两维的合取**——判据是「这次勾了哪些库」而非「挂了哪些库」——Ask 三个入口与报告的创建/确认/生成三处都生效。请求没有**收窄**的那一维按 notebook 真实证据宇宙作答；两维都没提交的请求交给既有 `ask_available` 闸。**本地证据宇宙 ≠ 可见导入来源数**：Knowhow 格子、该用户的已确认 Memory 与本地图谱都没有可见来源行，所以 `NotebookSummary` 在 `ask_available` 之外并列暴露 `local_evidence_available`（由 catalog 复用它本来就要求值的三条本地判据算出、零新增查询），空判把它与来源数取**或**，故只增不减。真收窄仍以选择为准：把来源清空就是空的，哪怕本地另有证据。

`AskResponse.retrieval_scope` 是只读回执：`{ local: { selected, total }, bases: [{ notebook_id, name, included }] }`。库名是**授权时刻的持久化快照**，绝不按当前挂载重新映射，所以重开旧答案时仍能看到那个此后已被取消挂载的库；检索侧从不回读它。两维都没收窄时该字段缺席，序列化因此与历史答案逐位一致（浏览器每次请求都会发两份范围）。它的披露面刻意比跨库来源代理更窄：只有库名与计数，没有文件路径、没有错误原文、没有来源身份。

浏览器把这两个维度并列成来源面板上的两组复选框：先是双段计数的「检索范围 · 本库 N/M · 参考库 K/L」工具条（「全选」/「清空」一并管两维），然后是「参考库」分组（每个已挂载库一行，只显示库名，不展开到库内来源），最后是「本库来源」分组——来源搜索框归在这一组的标题之下，因为它只查当前 notebook，摆在参考库之上会让人以为能一并搜到参考库里的内容。同一句双段计数还显示在问答输入框上方。两维同时为空时禁用问答输入与新建报告，判据与后端 409 逐条对齐（本地那一维读 `local_evidence_available`，不拿可见来源数代替）。`retrieval_scope` 在场时，答案卡在正文之上渲染一行默认折叠的「检索范围：…」，展开可见每个参考库本轮参没参与；缺席即不渲染，浏览器不按回执数字自行重算「算不算收窄」——那份判据只有后端一处。

浏览器的**严格推理门控同样按「这次勾了哪些库」判定，而不是「挂了哪些库」**。`NotebookSummary` 并列下发 `base_kg_notebook_ids`——它是 `base_kg_available` 的**分解**，列出挂载的参考库里哪几个已建知识图谱。零新增查询（`mounted_bases_row` 的每一行本来就带 `has_kg` 列，聚合布尔只是把它 `any(...)` 掉了），回填路径与 `base_kg_available` **逐字相同**，两者必须自洽（非空 ⟺ 为真）——它们是同一次读取的两种投影。前端据此把「深入分析 / 知识图谱」的可用性判成「本库有图 **或** 本次勾选的参考库里有带图的」，「将借用参考库「…」推理」也只点名**既被勾选、又已建图**的那几个库：读聚合布尔会在「本库无图 + 唯一带图的参考库被取消勾选」时放行一个这轮根本取不到图的模式（后端的知识图谱可用性闸早已按库维度收窄），并当着用户的面点名一个本轮不参与的库。字段缺席（版本 skew）时退回「聚合布尔 ∩ 勾选集」，只会比原判据更保守，不会更宽。这道门开始拦一个原先放行的模式之后，「取不到图谱」的提示按**成因**分成两支、出路不同：挂了已整理图谱的参考库、只是这次没勾 → 提示去来源面板重新勾选，且刻意**不给**「整理知识图谱」按钮；一个带图谱的参考库都没挂 → 保持原有的「整理知识图谱」提示与按钮。合成一支就会在用户只需点回一个复选框时，劝他跑一次整库图谱整理（真金白银的模型调用）。

顶层复选框范围是当前 notebook 的硬上限，也是检索范围的**唯一**来源。模型既不提议也不收窄或扩大它：语料盲意图规划器不输出任何来源身份，run 内也没有任何动作能改变哪些来源在范围内。全选 include 快照保留正常图通道，私下快照当时已有的隐藏 Memory/Knowhow 参与者，并冻结可分区候选和结果校验。验证后新增或删除可见来源/隐藏参与者时，在 I/O 前跳过高风险图通道；可分区检索仍按冻结上限继续。挂载参考库仍是独立参与者。带索引的 chunk/元素检索会在 scale 工件中持久化逐行来源代码；HNSW 在进入 Top-K 前应用允许来源谓词，hydrate 后还会在评分/合成前复核。旧发布索引缺少这个可选 sidecar 时仍可加载，但会先使用有界来源内 FTS，直到重建或 delta fold 写入映射。KG、PPR 与精确查找等确定性种子结束后，若证据仍完全为空，逐步推理会在让反思模型判断充分性之前确定性补一次有界原文元素检索。无法在遍历前安全应用当前来源谓词的持久化通道（当前 notebook 的全图/PPR/关系扩展、精确章节查找与报告整库画像）会在收窄或参与者全集漂移时被跳过；事后过滤不能作为授权，因为未选候选仍可能占满 Top-K 或提供不可见的图前提。按来源限定的 chunk、元素和 KG 直接检索仍正常执行，base 库 KG 种子也可直接映射回 base 原文，不经过组合全图遍历。

内部 `SourceSubgraphSnapshot` 是替换上述当前 notebook 跳过通道的读取侧准备，但现在只通过统一、受质量门控的 Ask 与深度报告激活入口消费。它在一次可重复读中只解析冻结的可见来源 id，每条 SQL 都在 `LIMIT` 前应用来源谓词；SQLite 的对象、chunk 与 cluster 读取固定从所选来源索引起步，避免先遍历被排除来源再过滤。一条关系只有在关系自身来源以及两端对象都属于所选来源集合时才会进入快照。对象到 chunk 的 membership 只来自当前代次的来源事实、规范化证据元素绑定与所选 chunk，绝不读取整库实体/chunk map。缓存身份只使用 O(1) 的 notebook KG/cluster 单调变更序列及有界的来源/run/回填状态：正式 live 投影写入会推进 KG 序列，历史修复会推进其来源账本，因此缓存命中不再重数事实、chunk 或证据绑定；构建快照时再用已经受限的事实窗口校验当前 live 代次完整性。反向索引未回填、来源删除、代次漂移或未知事实投影版本会在依赖图能力使用前 fail closed；某一行数护栏越界时只关闭依赖该分支的能力。live 或回填投影不完整时仍可用已经证实的来源事实行提供安全名称与 evidence，但事实完整性和 PPR/membership 能力保持关闭；缓存中的 payload/evidence 树递归不可变。全范围、off 与 shadow 仍保持历史响应、prompt、轨迹、候选顺序、证据预算和引用契约；只有通过 attestation 的 active run 可在 B 后追加来源内 G。

内部正整数护栏为：`SOURCE_SUBGRAPH_MAX_SOURCES=32`、`SOURCE_SUBGRAPH_MAX_OBJECTS=20000`、`SOURCE_SUBGRAPH_MAX_RELATIONS=40000`、`SOURCE_SUBGRAPH_MAX_CHUNKS=20000`、`SOURCE_SUBGRAPH_MAX_FACTS=20000`、`SOURCE_SUBGRAPH_MAX_FACT_ELEMENTS=60000`、`SOURCE_SUBGRAPH_MAX_MEMBERSHIPS=60000`、`SOURCE_SUBGRAPH_MAX_CLUSTER_MEMBERSHIPS=20000`、`SOURCE_SUBGRAPH_CACHE_MAX_ENTRIES=64`。每个数据库分支最多探测对应上限加一行以识别越界；非正值在 Settings 校验阶段直接拒绝。

所选来源原语层只通过统一 Ask/深度报告激活服务消费。邻居/扩展、关系搜索和两跳 chain 的每个结果都会再次校验 capability、关系来源、两个端点、evidence 与 review 状态；被排除来源既不能占结果名额，也不能充当中间节点。精确查找只在 snapshot 的所选 chunk 上复用既有标识符分组/子树语义；游标枚举的总数与分页只来自完整的所选 snapshot，不读取 notebook collection map。原语硬上限为：fan-out 16、扩展深度 3、扩展节点 128、chain 结果 16、关系结果 32、枚举页 100；精确查找继续使用既有 `EXACT_LOOKUP_*` 护栏。

内部受保护增强服务会在调用图 provider 前冻结历史最终 baseline。纯图 chunk 只使用调用方给出的独立 token 预算，并且只追加到隔离的增强提案；预算不足时丢弃图候选，不会重新截断 baseline 证据。baseline 中已经存在的 chunk 保持正文、来源与引用句柄、分数、相关度和位置，只有隔离副本合并生产者 provenance；off 与 shadow 的用户可见结果始终是原 baseline；active 只有通过质量门后才能发布受保护的追加提案。图失败或超时时返回同一 baseline 与 manifest；`baseline_evicted_count` 非零时丢弃整段图提案。baseline 与增强 reasoning 动作使用不可相互借用的 step 账本。shadow 关闭或图预算为零时不会调用 provider。Ask 与深度报告通过统一激活服务消费这条图通道；off 与 shadow 的调用、prompt、响应和引用仍保持历史 baseline。

小中型所选来源 snapshot 还有一条内部稀疏 PPR producer，默认为不可见 Shadow 观测启用，并由 `SOURCE_SUBGRAPH_PPR_ENABLED` 独立控制。它直接用获授权的 snapshot 节点、关系、对象到 chunk membership 和 cluster router 构建双向、列随机 CSR；来源归属、两端、evidence、review 状态与允许成员检查均发生在插边和度数归一化前。reset 中不属于这张 scoped graph 的对象/chunk id 会被忽略。min-max、Top-K 与 hydrate 命中只覆盖 snapshot 的授权 chunk，因此被排除的 B/C 来源不能影响 A 的度数、reset 归一化、分数、顺序或内存上限。transition 缓存身份由冻结 scope 及 KG/cluster/source 代次组成；LRU 最多 8 项，冷构建在 service 内 single-flight。在线护栏为：总图顶点 40,000（KG 节点 + chunk + cluster router）、逻辑无向边 100,000（CSR 重复折叠前最多 200,000 条双向 transition entry）、返回 chunk 100。构建器固定为 O(顶点 + 边)；越过构建护栏分别返回 `ppr_node_limit_exceeded` / `ppr_edge_limit_exceeded`，build/run 异常返回 `ppr_build_failed` / `ppr_run_failed`。禁止回退整库 CSR。这条 producer 只为统一 Ask/深度报告激活服务排序 G，绝不修改 B。

在大 notebook 中，按来源分区 scale 伴生产物在不打开 notebook 整库 CSR 的前提下提供同一所选来源授权语义。离线 full build 或 fold 每次只从 `knowledge_object_sources`、当前代次来源事实/元素绑定、来源自有 chunk/关系及来源受限 cluster membership 读取一个可见来源；每条分支复用对应 snapshot 的 LIMIT+1 护栏，SQLite chunk 强制从 `idx_chunks_source` 起步。它为每个来源发布一个哈希直寻 partition，并发布一份绑定主 scale manifest version、大小恒定的根 manifest。每个 partition 还绑定完整、无正文的 source/run/backfill signature，并记录所有 payload 文件的 SHA-256，因此即使计数不变，provenance 修复或可解析的文件篡改也会使旧 partition 失效；请求期身份探测仍为 O(selected)，不读取图行。partition 保存本地列随机 CSR、逐行对象类型与 chunk 身份，以及该来源拥有的跨 partition 关系端点。多来源冷读在 payload I/O 前先校验所有所选恒定大小 manifest，并把本地 transition entries 加上每条跨来源关系预留的两个 entry，按累计 60,000 nodes / 240,000 transition entries 护栏做保守预检。随后单来源请求直接复用已校验 CSR；多来源请求通过稀疏数组并出授权 identity，并只填充一次受限 cross-edge 分配。仅在关系所属来源被选、构建时 evidence 同源、两端均在所选对象并集且中央 edge registry 接受端点类型时接纳跨 partition 关系。PPR 最多执行 `SOURCE_PARTITIONED_PPR_MAX_ITERATIONS=30` 次整 CSR 迭代，先做局部 Top-K，再确定性排序最多 100 个返回 chunk candidate；禁止把全部 chunk 结果转成 Python tuple 并全排序。旧版、缺失、损坏、越界工件或任何 parent/source identity 失配都会在图使用前返回明确 unavailable reason；不存在整图或事后过滤兜底。运行时最多保留 2 个组合 partition handle，冷加载/组合 single-flight。`SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED` 控制发布，`SOURCE_PARTITIONED_PPR_ENABLED` 控制运行时 reader；两者默认开启且可独立回滚。reader 只供统一 Ask/深度报告激活服务使用，不可用时 fail closed 回 B。

版本化的所选来源 rollout gate 使用五类强制场景的 baseline/shadow 配对 observation：单篇、少量来源、至少含 10,000 chunks 的超大单篇、mounted-base evidence，以及 gold evidence 必须分别绑定不同来源别名的跨来源同名对象。两条 lane 必须使用相同 provider/model/prompt version、temperature、Top-P、seed、corpus signature、本地/基座 source id、alias-to-id 绑定与场景规模。EnergAIzer/PDAgent 案例必须执行 `expand_graph`，至少保留 20 个 baseline KG candidate，原 baseline candidate list/manifest 与 citation key 完全不变，且 baseline eviction 为零。任何越界 evidence/citation、畸形指标、scope 漂移、缺少图动作或 baseline 改变都是硬失败。Evidence Recall@20、citation coverage、citation validity、grounded-sentence coverage 与非空答案率不得下降；no-answer、erroneous-refusal 与 outline-drop rate 不得上升——逐案例与汇总都要满足；baseline 非零的结构分母不能在 shadow 变成零。shadow 延迟与数据库读取行数最多为 baseline 的 1.5 倍、峰值内存最多 2 倍，每案例最多新增 4,000 prompt tokens 和一次模型调用。不含正文的 attestation 只保存 run/corpus/model/policy identity、钉死的 canonical-golden 摘要、场景计数、无正文的逐案例与汇总指标、失败项及完整性摘要；绝不包含问题、答案、evidence/citation/source id 或 excerpt。production 不信任裸 `approved`，会重新验证每个逐案例和汇总质量/成本护栏。摘要不是签名，因此 active rollout 必须从受信部署工件读取，并同时匹配期望 corpus 与 model；任一 pin 缺失都 fail closed。`off` 完全 inert；`shadow` 无需 attestation 即可采集配对数据；`allowlist`、稳定 hash rollout 与 `on` 在默认 policy attestation 未验证批准时一律 fail closed。这道 gate 是所有用户可见 Ask/深度报告激活的强制控制点。

所选来源激活契约由 Ask 与深度报告共用，但它始终是内部检索实现细节。对真正收窄的本地来源范围，Ask 的 `chunk`、`reasoning`、实验 `graph` 与深度报告都会在历史 baseline 冻结后调用同一个所选来源激活服务。省略 scope 或全选 snapshot（包括单来源 notebook 中选中唯一来源）不进入该服务，保持历史响应形状。`SELECTED_SOURCE_GRAPH_ROLLOUT_MODE=shadow` 为默认值：它构建并测量 `G` 但仍返回 `B`；`allowlist`、稳定 hash `rollout` 与 `on` 必须同时通过受信、无正文 attestation 及精确 corpus/model pin。active 输出严格为 `B` 后追加 `G`；`G` 的默认独立预算为 4,000 tokens，不能驱逐、重排或重打分 `B`。snapshot/partition 失败、scope 漂移、任何终态越界候选或非零 baseline eviction 都 fail closed 回 `B`。mounted base 证据继续走独立历史 lane，绝不参加当前 notebook 的所选来源图遍历。rollout 状态不得进入公开 Ask/报告字段、推理轨迹、进度 stream 或 UI；只有无正文内部 `selected_source_graph` 事件携带它，浏览器还会过滤历史持久化的 `source_subgraph` 步骤。

无正文 `selected_source_graph` 事件仅属于运维内部 telemetry，所有用户可读 debug 日志的列表、统计、搜索、翻页和详情响应都会过滤它。

### 界面模式（自动/高级）

每个用户持久化一份 `user_profiles.ui_mode` 偏好（`"auto"` 默认 / `"advanced"`），经 `GET /me` 的 `UserProfile.ui_mode` 下发，并可通过自助端点 `PATCH /me/ui-mode`（body `{"ui_mode": "auto" | "advanced"}`；非法取值返回 422）修改。早于该字段的旧后端不会返回它，前端把缺失值按 `auto` 处理。高级模式就是本文档通篇描述的完整界面，与既有行为逐位一致。自动模式是从头像菜单开关的刻意精简界面：只保留「通用问答」「深入分析」两个 Ask tab，隐藏引擎子切换（深入分析固定为 `reasoning`）、检索档位控件（固定「标准」）、深度报告研究深度控件（固定「标准」，`depth=2`），以及上文的来源/参考库范围复选框——自动模式下被隐藏的控件永远在请求侧发送未收窄的默认值（当前 notebook 全部可见来源、全部已挂载参考库），绝不沿用高级模式会话残留的收窄勾选。自动模式创建的深度报告固定携带 `auto_generate=true`（参见上文「深度报告」条目及其可能触发的 `report_intent_auto_confirm_skipped` 事件）。问答输入框仅在该笔记本**尚无可用证据**且存在解析未到终态的来源时锁定——已有证据的笔记本上传新文档不会锁死输入框；能可靠统计处理中来源数（来源搜索框为空且当前已加载全部可见来源）时显示「N 篇文档处理中，完成后即可提问」，否则显示不带数字的「文档处理中，完成后即可提问」。切换模式即时生效，只改变前端渲染哪些控件与请求携带的默认值，从不改变后端检索或生成逻辑；高级模式的行为不受自动模式是否存在影响。

## 群组知识共享

三个真实场景——**项目**组共享一个知识库、**部门**共享若干个、范围更大的**领域**知识
库——由同一套模型承载。三者的差异**只落在配置上，不落在机制上**。

### 模型：群组、授权边、挂载

- **群组**是一组用户加上组内角色。`kind ∈ {project, department, domain}` 只是**分类
  标签**：它决定谁能建这个组、界面怎么措辞，不影响任何权限机制。项目组人人可建，
  部门组与领域组仅系统管理员可建——正是这道闸让标签可信。因此 `kind` 建成之后**不可
  改**：把它传给更新端点会被明确拒绝而不是静默忽略，否则普通用户能把自己建的项目组
  改标成「部门」，那道闸就白设了。
- 每个群组恰有一个生效中的 **owner**（`groups.owner_id`）。创建者是初始 owner，
  同时也是组管理员。owner 只能转让给现有成员；转让事务会把目标提升为组管理员，
  原 owner 仍保留组管理员身份。owner 在转让前不可降级、不可移出、也不可自行退出；
  `created_by` 只保留为不可变的创建审计。
- **授权边**是 `(notebook, principal, role)`，`principal ∈ {user, group,
  group_admins, everyone}`、`role ∈ {viewer, admin}`。表里的每一行都是**生效中**的
  授权——判定谓词零 status 过滤，因此不存在「忘了滤 pending」这类漏洞形态。笔记本
  owner（`created_by`）仍是隐含的最高授权。
- **挂载**机制不变，只改有效性谓词：**读权 ⇒ 可挂载**，但受下面的借入挂载门约束。

「某人在不在组里」与「这个组能不能读这本库」是两件独立的事，两者都**实时判定**：把人
移出组、删掉授权边、或者删掉整个组，都在下一次请求上立刻生效。

### 角色

组内两级，加上笔记本 owner：

| 能力 | 成员 | 组管理员 | owner |
| --- | :-: | :-: | :-: |
| 打开库、看来源/图谱、提问（会话按提问者隔离）、存自己的 Memory | ✓ | ✓ | ✓ |
| 在库内创建**自己的**深度报告（计入自己的用量；他人不可见） | ✓ | ✓ | ✓ |
| 把本库挂为自己 notebook 的参考库 | ✓ | ✓ | ✓ |
| 添加/删除/重新解析来源，触发图谱与检索索引构建 | | ✓ | ✓ |
| 管理授权边、改名、图谱 Schema 覆盖 | | ✓ | ✓ |
| 挂载配置、`share_token` 链接分享（撤链接连带踢只读成员） | | | ✓ |
| 删库、转让 owner | | | ✓ |

P2 兑现了这两格。内容管理能力（来源增删/重解析、构建触发、knowhow/知识治理/命令目录
写）以及 `notebook:manage` 现在解析到 **admin 档**——owner ∪
`role='admin'` 的有效授权边（谓词唯一定义点 `access_sql.NOTEBOOK_ADMIN_SQL`，它复用读权的
受限三臂外加 `role='admin'` 并排除 `everyone`）。组管理员因此能经浏览器管理内容与共享。

**`notebook:manage` 到底覆盖什么**——它是 `PATCH /notebooks/{id}` 加三个授权边端点，而那个
PATCH 编辑的是笔记本的整份**描述性画像**，不只是改名:`NotebookUpdate` 收 `name`、`purpose`、
`primary_domain`、`target_users`、`expected_questions`、`source_types`、`taxonomy`、
`access_scope` 八个字段。本页早先写作「改名」，那是「PATCH 那个端点」的简写，不该被读成字段
清单。使这件事安全的是下面两条性质，它们是**承重的**而不是碰巧成立:

- **这八个字段没有一个参与授权判定。** 访问权只由 `access_sql.py` 的三条谓词（外加
  `mount_sql.py` 的可挂载性）决定，它们引用的是 `notebooks.created_by`、`notebooks.tier`、
  `notebook_members` 与 `notebook_grants`，八个字段一个都不在其中。尤其 `access_scope`
  是**「这个库是给谁用的」这句描述文字**，不是访问控制列:它只被 notebook store 写入、
  被目录投影读回，再无别的消费者。所以改它们不可能提权。这条由反向护栏冻结
  （`backend/tests/test_notebook_update_authorization_free.py`）——往 `NotebookUpdate` 里
  加字段时**必须有人回来重新回答**这个问题，而不是静默继承旧答案。
- **生命周期状态是 repository 私有的。** `NotebookUpdate` 设了
  `model_config = ConfigDict(extra="forbid")`，所以 `status`（尤其内部的 `copying` 哨兵）、
  `tier`、`created_by`、`is_shared` 经这个端点根本写不进来——未知键是 422，不是静默忽略。

这些字段就是普通的用户可见内容:`primary_domain` 在库内搜索框里可被匹配到，`purpose` 会经
MCP 的 `list_notebooks` / `select_notebook` 提供给外部 Agent（截断到 500 字符）。所以它们是
**内容邻接**的——描述这个库讲什么——正落在内容管理权本来的范围里。「拆端点或逐字段校验、
让非 owner 只能改名」这条路评估过并否决:为纯描述性元数据增加一道真实的接缝不划算，而同一个
组管理员本来就能把这个库里每一份来源增删重解析。

分享界面据此新增「组管理员可管理这本笔记本」勾选：勾上就在 `(group, viewer)` 之外追加一条
`(group_admins, admin)` 边；撤销共享时同组两行一起删，共享清单把两行折叠成一条并标注管理权。
**但两类 owner 专属能力刻意不翻**：`notebook:delete`（删库，爆炸半径整本库且 owner 不可
撤销）与 `notebook:configure`（挂载配置 + `share_token` 链接分享）恒 owner——见下文
「挂载配置与链接分享恒 owner」。**Agent/MCP 面也一个字不动**：`sources:write` /
`sources:delete` / `maintenance:execute` 仍是 owner-only 红线——长期 token 是独立凭据，其
owner 可能在签发后很久才被授管理权，MCP 写工具删文档的爆炸半径正是这道 owner 门当初要防的。
浏览器 HTTP 面已放宽 admin、Agent token 面没有，是刻意分歧不是疏漏，组管理员的写权只在浏览器
界面生效。

系统管理员另有**群组维度的运维旁路**——可读任意组详情、可管理任意组的成员与授权边，
不要求他是该组成员。这与既有的「系统管理员可转移 `notebooks.created_by`」同性质，理由
有两条具体的：「至少保留一名组管理员」在并发窗口下仍可能留下一个零管理员的组，而所有
管理端点都要求组管理员身份，那个组在 API 里就**永远不可恢复**；以及
`GET /groups?scope=all` 是给系统管理员的全局管理面，没有旁路它就是一张每行都点不开的
表。旁路**不覆盖自助退出**（「退出」的前提是本人真的在组里），不伪造成员身份
（`my_role` 仍如实为空），也不放宽笔记本维度的任何读写守卫。

### 群组工作台

头像菜单里的「群组」打开集合层的独立页面，而不是弹窗。左侧选择群组，右侧按「知识库／
成员／共享申请／设置」四个页签组织工作。每个成员只能查看并打开其当前组角色实际授予读权的
Notebook；仅有 `group_admins` 边的库不向普通成员披露。owner 与组管理员可把自己有管理权的 Notebook 加入本组、撤销本组可见性，并可选授予组管理员
内容管理权。「成员」页还允许他们生成或重新打开一条可重复使用的邀请链接、复制、换新（原子
作废旧链接）或撤销。链接里的 bearer token 会跨登录/注册门保留；认证完成后浏览器先从 URL
历史中移除 token，服务端再原子地把调用者加入为普通成员。重复打开幂等，既有管理员不会被
降级；未知、已撤销、已换新或群组已删除的 token 都返回相同的 404。链接不会自动过期，因此
管理员必须把它当作 bearer 凭据，并在受邀范围变化时主动撤销或换新。owner 转让与删除群组放在设置页的独立确认区。页面复用集合页的壳层、字体、控件、
间距、颜色与响应式断点，不引入另一套视觉系统；当前群组和页签可通过 URL hash 定位与返回。

### 端点面

`group_routes.py` 里 27 个端点（含 P2 审批流 7 个），另加笔记本 router 上一个只读端点。

| 端点 | 谁可以调 | 说明 |
| --- | --- | --- |
| `POST /groups` | 任何用户（`project`）；管理员（`department`/`domain`） | 创建者在同一事务里成为 owner 与组管理员 |
| `GET /groups` | 任何用户 | 我加入的群组；`?scope=all` 仅系统管理员 |
| `GET /groups/{id}` | 组成员（管理员经旁路） | 组详情 + 成员清单 |
| `PATCH /groups/{id}` | 组管理员 | 只改 `name` / `description` |
| `DELETE /groups/{id}` | 群组 owner（系统管理员恢复旁路） | 同一写事务清掉指向本组的授权边 |
| `POST /groups/{id}/transfer` | 群组 owner（系统管理员恢复旁路） | 目标必须是现有成员；目标升管理员，原 owner 保留管理员 |
| `PUT /groups/{id}/members/{user_id}` | 组管理员 | 加人与改角色同一个端点 |
| `DELETE /groups/{id}/members/{user_id}` | 组管理员 | owner 必须先转让；移除最后一名管理员也返回 409 |
| `DELETE /groups/{id}/membership` | 成员本人 | 自助退出；owner 必须先转让，最后一名管理员返回 409 |
| `GET` / `POST` / `DELETE /groups/{id}/invite-link` | 组管理员（系统管理员恢复旁路） | 无副作用查看、创建/复用或撤销本组当前的 bearer 邀请链接 |
| `POST /groups/{id}/invite-link/rotate` | 组管理员（系统管理员恢复旁路） | 原子换新 token，旧链接立即停止解析 |
| `POST /group-invites/{token}/join` | 已登录 | 原子加入为 `member`；已有角色幂等保留；无效/已撤销/组已删除统一 404 |
| `GET /users/resolve?username=` | 任何登录用户 | 按用户名**精确**查，只返回 id/用户名/显示名 |
| `GET /notebooks/{id}/grants` | `notebook:manage` | 该库全部授权边，四类主体如实返回 |
| `POST /notebooks/{id}/grants` | `notebook:manage` **且**目标组组管理员 | 只收 `group` / `group_admins` 两个主体 |
| `DELETE /notebooks/{id}/grants/{grant_id}` | `notebook:manage` | 笔记本维度撤销 |
| `GET /groups/{id}/shared-notebooks` | 群组成员（系统管理员旁路） | 本组成员可见的共享知识库清单 |
| `DELETE /groups/{id}/shared-notebooks/{nb}` | 组管理员 | 组维度撤销，删掉指向本组的**全部**边 |
| `POST /notebooks/{id}/share-requests` | `notebook:manage` **且**目标组**普通成员** | **P2** 提交共享申请；目标组的**组管理员**会被 403 拒绝——他直接走 `POST /notebooks/{id}/grants` 发边，永远不经这张表。幂等（撞在飞申请返回既有 pending，不是 409） |
| `GET /notebooks/{id}/share-requests` | `notebook:manage` | **P2** 请求者本人对本库发起过的申请（弹窗回显待审批/已驳回） |
| `GET /me/share-requests` | 已登录 | **P2** **我**发起的、仍**待审批**的全部申请，跨笔记本。它是撤回端点的另一半：同一条授权轴（`requested_by`）、不挂任何笔记本能力，所以**已失去管理权**的申请人仍找得到、撤得掉自己的提议。刻意**不**挂在 `/notebooks/{id}/…` 下——那个维度已经有一份 manage 门的清单，一个维度只能有一套口径。只回 pending：已决定的申请撤不回来，列出来只会白扩披露面 |
| `DELETE /notebooks/{id}/share-requests/{rid}` | 已登录，**且这条申请是你提的** | **P2** 撤回**待审批**申请（整行删，不是第三个状态）；已决定 409、不存在 404。⚠ **刻意没有 notebook 能力依赖**：授权轴是**申请归属**而不是当前的库权限。批准会拒绝已失去管理权的申请人，撤回若也要求管理权，这类申请就**既批不了也撤不掉**，永远卡在审核队列里 |
| `GET /groups/{id}/share-requests` | 组管理员 | **P2** 审核队列：共享给本组的待审批申请清单 |
| `POST /groups/{id}/share-requests/{rid}/approve` | 组管理员 | **P2** 同一写事务写 `(group, viewer)` 边并标 approved；已共享幂等；不存在/已决定 404 |
| `POST /groups/{id}/share-requests/{rid}/reject` | 组管理员 | **P2** 标 rejected、不写边；申请者可对同库同组重新发起 |
| `GET /notebooks/{id}/share` | `notebook:configure` | 只读；见下。⚠ **恒 owner，不是 `notebook:manage`**——链接分享是库主对本库对外处置的配置，不随内容管理权转移 |

有几条边界必须写明：

- **群组的可见性口径是 404，不是 403。** 非组成员访问一个群组，得到的答复与「这个组
  不存在」完全一样——群组名本身就是可探测信息（哪个部门在用这个系统、有没有某个项目
  组）。唯一刻意的例外是 `POST /notebooks/{id}/grants`：那里「组不存在」（404）与
  「组存在但你不是它的管理员」（403）**可区分**，因为能走到这一步已经证明请求者对这
  本库有管理权，而且他手上必须已经有那个 128 位随机组 id（猜不出来）——于是拿到两条
  各有出路的错误文案，而不构成枚举通道。
- **创建组授权边是双重条件**（设计决策 9）：请求者既要对这本笔记本有管理权，又要是
  目标群组的组管理员。群组那一半的权威判定在 **store 的写事务里**，不做前置查询：这条
  边一落库就**立刻**给整组人读权，而「先查一次、再在另一个事务里插入」中间那个窗口
  足够让组被删、或让发起者被移出/降级，边却照发不误。
- **撤销是不对称的。** 库的管理者可以从笔记本维度删任意一条边；组管理员可以从群组维度
  删掉指向本组的全部边。两个入口各自只需要自己那一半权限——组管理员管理共享给本组的
  全部内容，库主随时可以收回自己的库。
- **`user` 与 `everyone` 主体不经这些端点。** `user` 主体继续走既有的只读共享
  （share_token）流程，`everyone` 继续走 `POST /notebooks/{id}/tier`。同一件事有两个
  写入口，迟早会有一个漏掉另一个的某条校验。
- **非法 `scope` 明确 422，绝不静默落回 `mine`**——一个拼错的 `?scope=al` 在 200 下返回
  一份收窄过的清单，调用方读到的就是「这是全部」。
- **`GET /users/resolve` 任何登录用户可调**，这是内部部署下**已登记接受**的取舍：它让
  用户名可被逐个探测。两个替代方案都更糟——只允许组管理员调，就没法加第一个成员；做成
  模糊搜索，则把逐个探测换成批量枚举。返回面刻意只有 id / 用户名 / 显示名，不含邮箱、
  角色、用量。
- **孤儿边要标注，不能藏。** 指向已不存在的群组的边带 `principal_kind="missing"` 回来。
  删组事务会清掉这类边，但 `principal_id` 没有外键，合库仍可能复活它们
  （`scripts/merge_dbs.py` 负责清扫）；有了这个标注，库主才看得懂残留的那条是什么、
  知道可以删。

### 成员贡献审批流（P2）

普通成员想把**自己的**库共享给一个组，但他只是那个组的普通成员、直接发不了授权边——于是走
「申请 → 组管理员审批」。**方向轴要看清**：请求者是这本库的 manage 权（owner/admin）持有者、
对目标组**只是普通成员**；组管理员分享进**自己管理**的组永远走既有 grants 端点、不经这张表。

- 申请是**双重条件**：对库有 manage（依赖层 `notebook:manage` 挡）＋是目标组成员（端点体内查
  `user_group_role` 非空，普通成员即可）。非成员与「组不存在」同为 **404**（群组可见性口径，不
  泄露组的存在性）。
- 状态机 `pending → approved/rejected` **单向**。**撤回不是第三个状态**：申请者对**待审批**申请走
  `DELETE` 整行删；已批准/已驳回是既成的决定，撤回它没有意义——store 按精确状态判定，已决定的撤回
  请求映射成 **409**，根本没有这条（或不属于这本库）则 404。`decided_by`/`decided_at` 因此纯粹是
  「组管理员做出的决定」，撤回不写这两列。
- **批准在同一写事务**里插 `(group, viewer)` 授权边并把申请标 `approved`；已共享（同库同组已有边）
  时**幂等**——不因「已经共享过」让批准失败（那会留下一条永远批不掉的申请）。并发双审由 store 的
  行锁＋精确状态匹配挡住。驳回只标 `rejected`、不写任何边，申请者可看到「已驳回」并对同库同组**重新
  发起**（`rejected` 不占 `WHERE status='pending'` 的那条部分唯一索引）。
- **申请人必须能在不依赖笔记本任何权限的前提下够到自己的申请。** 撤回刻意只认申请归属，那么
  「列出申请、给出申请 id」的那条路也必须如此——否则这个口子在**它唯一存在意义的场景**里就是
  够不着的（申请人失去管理权 → 批准会拒绝这条申请 → 笔记本维度的清单对他 404）。
  `GET /me/share-requests` 就是那个全局入口；界面把它放在**群组面板**而不是笔记本工作区，因为
  他可能连读权都一并失去、根本打不开工作区。它的**披露面是逐字段选定的**，判据是「他原本知不
  知道」——以及同样要紧的**「他现在还知不知道」**:他学到的是**提交那一刻**的标签。
  `notebook_id` **恒带**(他提交时就持有;`notebooks.created_by` 只在建库与深拷贝时写入
  ——没有转让功能，`NotebookUpdate` 里也没有这一列、有护栏钉着——所以不存在「拿旧 id 探测新
  主人」这条通道)。两个**展示标签按当前权限给,且各自独立判**:`notebook_name` 只在读权谓词
  对他仍成立时给,`group_name` 只在他仍是该组成员时给;否则返回空串,界面渲染一句中性占位。
  少了这一条,这份清单就是一条**活的**通道:对方改名,新名字会持续送到一个可能已经无权观察
  该对象的人手里——与「跨库资产 `no-store`」「取消挂载即 404」「公开报告页每次实时复核创建者
  读权」是同一条线。两半**刻意分开判**:一起判的话只失去一半的人两个名字都会消失,多条待审批
  就都长成「知识库 → 群组」,分不清该撤哪条。撤回本身不受影响——它的轴是申请归属,不是标签
  可见性。`group_id` 是他自己选的、且提交时是其成员的组。`status` 恒 `pending`，因此
  `decided_by`/`decided_at` 恒为 null——**审批者身份不经这条路泄露**，因为已决定的申请压根不在
  结果里。库的任何当前状态（来源数、是否仍共享、现任成员）一律不带。
- **幂等提交**：同库同组已有一条待审批申请时，创建端点**返回既有那条**而不是报 409——申请者刷新页面
  重复点提交是常见操作，不该弹错误（`uq_share_requests_one_pending` 保证同一 (库, 组) 至多一条在飞）。
- 申请不授予任何权限：`pending` 不进任何判定谓词，grants 表恒为纯生效授权。删组/删库经 FK CASCADE
  带走申请。铃铛：组管理员的待审批申请数进待确认中心（复用 `pending_actions`，读谓词同口径）。
- `status` 一律精确匹配 `pending`/`approved`/`rejected` 三值，绝不用 `!=` 当「已决定」判据；`decided_at`
  只写 SQL `NULL`/ISO 时间戳、绝不写空串（它是本表唯一进入 shadow 正向复制的可空时间列，空串会让 PG 的
  `timestamptz` 类型报错并 poison 整条通道）。

### 挂载配置与链接分享恒 owner（P2）

P2 把内容管理权翻给了组管理员，但**挂载配置与 `share_token` 链接分享**刻意留在 owner——它们是 owner
对本库检索范围与对外处置的配置，不随内容管理权转移，在能力表里单列一格 `notebook:configure`（恒
owner，不并进 `notebook:manage`）。两条硬理由：

- **挂载配置**：`mount_sql` 的「同 owner 候选」是按被挂库 owner 解析的。组管理员若能改挂载，就能经
  `GET /notebooks/{id}/mountable` 枚举出库主**从未共享**的全部私有库名、`PUT .../bases` 把它们挂进这本
  共享库，再经 active-notebook 代理端点读到全文——一条越权读通道。
- **链接分享**：`share_token` 能替库主铸一条对外免登录链接，让组外任意人整本 copy。尤其
  `DELETE /notebooks/{id}/share`（撤链接分享）会**连带踢掉全部只读成员**（`clear_share` 清
  `notebook_members`），爆炸半径超出内容管理，刻意留在 `notebook:configure`。

所以「组管理员能管共享」= 能管**授权边**（grants），**不**等于能动挂载或链接分享：`notebook:manage`
覆盖改名（`PATCH /notebooks/{id}`）＋授权边管理（`GET`/`POST`/`DELETE /notebooks/{id}/grants`），
`notebook:configure` 覆盖挂载（`bases` / `mountable`）与链接分享（`share` / `mounted-by-count`）。

### 读权 ⇒ 可挂载，以及借入挂载的「未共享门」

挂载有效性历来是「公共库 ∨ 同 owner」，刻意**排除**只读分享进来的库，理由是「撤销分享
后挂载边还在，会成为越权通道」。这条顾虑已由实时谓词吸收（撤销 → 谓词不满足 → 边失效），
因此**读权 ⇒ 可挂载**成为一致规则——这正是「项目成员挂载项目知识库」的需求本体。
`GET /notebooks/{id}/mountable` 随之放宽。

但历史顾虑还有实时判定治不了的另一半：**转手再分享**。Carol 把 Y 分享给 Alice，Alice
把 Y 挂进自己的 X，Alice 再把 X 分享给 Bob——Bob 就经 X 的代理读取与联邦检索读到了 Y 的
全文，而 Carol 从未授权他。全程没有任何授权被撤销，是挂载方**新增**一次共享就凭空多出
一批读者。

所以挂载谓词的受限读权那一支额外要求：借入挂载**仅在挂载方笔记本自身没有被共享出去**
（没有任何 `notebook_members` 行、也没有任何 `notebook_grants` 行）时有效。挂载方一旦被
共享，借入边即刻失效（边保留置灰，与既有失效边惯例一致）；取消共享后自动恢复。
`tier='base'` 与 `everyone` 授权**不受此限**——受众本就是全员，转手不增加任何暴露面，这
也是 `access_sql` 要把授权边拆成受限片段与 `everyone` 片段的**唯一**理由（读权谓词本身
并不区分这两类）。同 owner 支同样不受限：挂载方 owner 共享 X 就是在处置自己的内容。
谓词只认「有没有被共享」这个事实，**不比对两边的受众**：受众比对要跨库展开成员/组/授权边
三张表，而这是每次参与集解析都要跑的热路径；宁可保守，恢复手段（取消共享）在用户手上。
产品含义与「挂载不传递」同向：**借来的东西不转借。**

界面上有两处配套：分享弹窗在**发出共享之前**就提示「共享后，本笔记本借入的参考库将暂停
参与检索」；被这道门关上的边有自己的文案（「本笔记本已共享，借来的参考库暂停参与检索；
取消本笔记本的共享即可恢复」），而不是那句对借入边根本不成立的固定文案「该库已不是公共
知识库，且不属于你」。这道门自己的判别式由挂载谓词派生成 `MOUNT_GATE_CLOSED_EXPR`，消费点
不许手拼；它同时保持被挂库的真实名字可见——此形态下挂载方 owner 对被挂库**仍有合法读权**，
没有泄露可言。

### 群组库在笔记本列表里的呈现

经生效组授权边可读、且**既非 owner 也非** `notebook_members` 行的库，构成笔记本列表的
**「群组」分区**。

- `NotebookSummary.access` 仍是 `"reader"`，**不新增枚举值**（已定裁决 7）。群组成员拿到
  的就是 reader 那一档，行为逐字相同：隐藏写按钮、Ask 走同一读守卫、默认全选检索范围、
  隐藏参与者快照。而「访问权**从哪来**」是正交的一维，所以另开一个字段。
- `NotebookSummary.granted_via` 是 `{group_id, group_name, kind}` 的列表，驱动卡片上的
  「来自群组《X》」标注。自有库与经分享链接加入的库为空，因此旧行为逐字不变。**列表与
  详情两条路径都回填**，去重口径两边一致：**成员行优先**——既经分享链接加入、又在被授权
  群组里的人，`granted_via` 为空、「退出共享」仍然可用（它删的正是那条成员行）。
- 反过来，`granted_via` 非空的卡片**不得**显示「退出共享」：那个按钮只删
  `notebook_members` 行，对授权边一点作用都没有，点了而库还在列表里是一个**必然发生的
  假失败**。改为展示「由组管理员管理」的静态说明。
- `GET /notebooks/{id}/mountable` 换了响应模型 `MountableNotebook`，多一个
  `origin ∈ {base, mine, shared}`，由 `MOUNT_VALID_EXPR` 已有的列信息（`tier` 与
  `created_by`）投影而来，**零新增查询**。挂载选择器据它分三档「公共知识库 / 我的笔记本 /
  共享给我的」——「读权 ⇒ 可挂载」放开之后别人 owner 的库也进候选，只按 `tier` 分会把它们
  标成「我的笔记本」，一句事实错误的标签。优先级 base → mine → shared：自己 owner 的公共
  知识库仍判 `base`，本字段出现之前的分组结果逐字不变，只有新准入的那批行落进第三组。
  该字段刻意挂在**新模型**上而不是 `NotebookRef`（后者同时是 `MountedBase` 的基类与另一条
  并不计算这个标志的查询的响应模型）。
- owner 侧的「已分享」口径改为**并集**：只读共享（`notebooks.is_shared`）**∨** 存在指向
  群组的授权边。卡片徽标与 `shared-by-me` 总览用同一个判据，`SharedByMeItem.group_count`
  带上共享给了几个**不同的**群组。`share_token` 为空而 `group_count` 非 0 的行，就是一条
  只因群组共享而出现的记录（没有链接可发）。没有链接的行不算规模、不查成员——`mode` /
  `size` / `members` 说的都是链接。
- 待确认中心的铃铛现在能解析出**非自有库**的库名（此前共享库里建的报告库名为空），并且它的
  报告那一半移到了「有没有自有库」这道闸**之外**——那一半的谓词只有 `created_by`、一个
  notebook id 都不消费，于是「没有任何自有库的成员」铃铛恒为 0，而他的报告正卡在
  `intent_ready` 等他确认，是条走不通的路。

### `GET /notebooks/{id}/share` 零副作用

打开分享弹窗此前会 `POST .../share`，于是「只想共享给群组」的用户会顺带被发一条分享链接
——一次纯查看的动作产生了持久副作用。新增的 GET 只读回当前链接状态、**绝不铸新 token**，
链接改由用户显式点「开启链接分享」时才 POST（那条 POST 幂等，已有 token 原样返回）。没有
token 时 `share_token` 为空串，并且**不计算** copy-stats：那是一次真实的规模统计（大库上
不便宜），而没有链接的库根本用不到它。因此 `copyable` / `size` 只在 `share_token` 非空时
有意义，消费方必须先判它。

### 共享库里的深度报告

建报告的能力跟着**读权**走，报告则**按创建者隔离**。

- 创建报告只要求对该笔记本有读权。凡是碰**已存在**报告的端点——详情、确认意图、改大纲、
  生成、取消、删除、分享、读分享状态、撤销分享，共九个——都要再过
  `report_routes.py::_own_report_or_404` 这道行级校验（`reports.created_by == 当前用户`，
  另有 AST 守卫钉住「每个这类端点都调了它」）；列表与导出按同一谓词在 SQL 里收窄。别人的
  报告与「不存在」同为 404。
- **notebook owner 也不例外**：他只看得见自己建的报告。这是刻意不引入「owner 看全部」这条
  新披露；要给别人看，走既有的公开分享链接。
- 由于该能力跟的是**读权**而不是「读权怎么来的」，经 share-token 加入的普通只读成员**同样
  获得**建报告的能力。这是**有意**的（能力表达的是「谁能读这本库」），不是群组特性的溢出，
  也是相对旧版本的一次**对外行为变化**。
- 成员发布的公开链接是一个**受托面，有效期 = 他的读权**。`GET /public/reports/{token}` 在
  token 命中后**每次请求实时复核**报告**创建者**当前是否仍对该 notebook 有读权
  （`user_can_read_notebook(notebook_id, created_by)`，两个 id 显式传参——匿名 router 不绑定
  请求用户，绝不能碰 `current_user` 的 seeded-admin 回退）。不通过时返回**与无效 token 同款
  的 404**：可区分的响应会把某人的组成员身份透露给匿名调用者。恢复授权即复活链接，与 token
  既有的幂等语义一致。这与挂载实时判定是同一套哲学，理由也相同：失权路径有好几条（授权边
  删除、退组、组被删、成员行移除、库改 tier），级联要在每一处记账，漏一处就留一个永久后门。
  具体形态是——成员失权后，成员被读守卫挡在库外、owner 被行级判定挡在报告外，**谁都够不到
  撤销**，而公开页会永远 200 服务 owner 的语料。owner 自己的报告与全部历史报告零变化
  （创建者 = owner，读权恒成立）。
- 管理员用量归集同此口径：`GET /admin/users` 的报告数按 `reports.created_by` 计，不按笔记本
  owner，与既有的 `questions`（按提交者计数、含共享库提交）以及展开清单、活动流的报告条目
  （本就按创建者收窄）一致。

### 已登记的上限与取舍

- **组名 120 字符，组说明 1000 字符。** 两者都是用户编辑的数据，所以超限**明确拒绝**，
  绝不静默截断。
- **清单端点无分页**——`GET /groups/{id}` 的成员清单、`?scope=all` 的全量群组、
  `GET /notebooks/{id}/grants`，以及 P2 审批流的两个清单（`GET /groups/{id}/share-requests`
  的组内待审批队列、`GET /notebooks/{id}/share-requests` 的本人申请回显）。这是**已定取舍而非
  遗漏**：规模按单组最多几百人设计（决策 11），一次全量返回在这个量级内成立；申请清单只含**待审批**
  行、且每个 (库, 组) 至多一条在飞（见下），量级更小。写在这里是免得将来有人把「没分页」读成 bug。
- **防重复 pending 一条**——`uq_share_requests_one_pending` 保证同一 (库, 组) 至多一条在飞申请。
  创建端点撞它时**幂等返回既有 pending 行**，不是 409：申请者刷新页面重复点提交是常见操作。这是**产品
  行为契约而非配额**，写在此处是因为它决定了申请清单的量级上限。
- **「群组」分区把一个已知的 N+1 放大了**，作为已登记的债务保留。`granted_notebook_rows`
  只是一条查询，但每行都要过 `NotebookSummaryQuery.from_row`，而它逐库发若干次计数/挂载
  查询——约 7 条语句/库，500 本组授权库量级即约 3500 条。「自有库」与「加入的库」两段是
  同一个既有形态，所以这不是本特性引入的新问题，但群组分区把可能的行数放大了一个量级。
  优化方向（批量预取计数）另行排期。
- **不做判定缓存。** 单次判定是若干带索引的 `EXISTS` 探测（grants 按 `notebook_id` 或
  `(principal_type, principal_id)`，`group_members` 按主键或 `user_id`），与现状同量级；
  热路径不新增全表扫描。

## Knowhow 表

notebook 内的 **Knowhow 表** 动作（与知识图谱并列，单开一个面板）管理 **knowhow 表**：把领域经验沉淀成一行行经验记录，列名自由命名。首个实例是半导体时序违例排查（行=违例类型；列=现象识别、根因分析、修复方法、依赖工具），但列名完全是用户自定义文本，不锁定词表。建表可以从**导入**开始（xlsx/csv/Markdown，预览时给出列→内容类型的映射建议）：新表导入会让用户选择“属性按列”（默认，首行为表头）或“属性按行”（首列为属性名），后端在预览与确认导入前自动把属性行表转置成内部统一的属性列表；追加导入与投影管线仍只处理属性列表。结构校验失败会在向导里显示安全、可操作的修改建议：尤其是记录分组使用横向合并单元格的属性行表，合并展开后也能识别并提示改选“属性按行”；重复/空表头、不支持的文件、编码错误和失效的列设置都会说明应修改什么，不再只显示“请重试”。也可以用**建表向导**从零搭建（先定列名表头，再填行）。填值两条路可自由混用：应用内经**格子编辑器**（markdown 编辑默认单栏专注、可切「并列对比」或「铺满预览」且按会话记忆、图片粘贴或拖拽即可上传、自动本地草稿（每条离开路径都会先把未保存内容同步落成可恢复的本地草稿、落不进就不离开，经 Esc／点背景／× 关闭或切换格子会先确认）、保存并下一格连续录入），或线下走 **Excel 模板往返**：按当前表头下载 `.xlsx` 模板（表头行冻结），批量填写后上传追加（提交前会预览未匹配列，以及行标题与已有行重名的提示）。

至多一列可被指定为整张表的**行标题列**（表级设置，不是逐列打标）。设置后，表中每个非空格子都会成为知识图谱节点——节点的类型就是所在列名——并用 `about` 边连回该行的行标题格；同一列里不同行出现的相同值会归并成一个节点（十行都引用同一个工具，就是一个工具节点带十条入边）。不设置行标题列，整张表就只参与检索——格子照常切成 chunk 供问答使用，但不建任何图谱节点，适合每行是一条记录而非一个具名概念的流水型表格。

投影出的格子知识对象默认进入 reasoning/graph 的图谱节点检索（`KNOWHOW_KG_NODE_RETRIEVAL_ENABLED=true`），因此命中格子既可作为图遍历种子，引用也保留直达该行详情的跳转。把开关设为 `false` 只回滚这条直接对象路径；逐格 chunk 仍参与问答检索。默认开启后的类型扩展只认由表 `hidden_source_id` 持有的对象，不会把无关的自定义 Schema 类型扫进来；按来源收窄的类型集合和标准化 chunk-vector→对象旁挂会跨子查询做 single-flight 缓存。旁挂版本同时包含图变更状态和这批格子 chunk-vector 的计数/时间，因此不会 bump KG 的纯向量修复也能触发刷新；KG 变更还会显式驱逐缓存。

投影状态是整表完成契约，不是逐行进度捷径：整表的 chunk、embedding、知识对象/关系、变更序号与图缓存通知全部完成前，行保持 `pending`/`syncing`；只有这些收尾工作成功后才发布 `synced`。因此调用方观察到所有行均已结束时，可以立即读取完整图谱，不受后台线程调度先后的影响。状态发布还必须匹配本轮读取的表变更序号：旧任务绝不能覆盖并发新编辑留下的 `pending`，新版本由已排队的下一轮任务处理。

每列还带一个**内容类型**——仅作确定性解析提示，从不调用 LLM：**方法步骤**列解析成有序步骤列表，**工具/事物**列按列表项/换行拆分并去重成多个节点，**普通**列整格作为一个节点。格子编辑器与行详情抽屉都提供显式的**优化表达**按钮（绝不自动触发）：调用系统为 `knowhow_optimize` 绑定的服务，在保持原意的前提下规整结构与措辞，原文与建议对照展示，只有逐格确认后才会回填。

行/整表的**一键规整**先冻结完整整表快照，再以有界前端并发生成候选，禁止无界 `Promise.all`：上限取 3 与当前 `knowhow_reformat` 服务实时容量的较小值，状态读取失败时安全回退为 2。相同 `(column_id, trimmed 原始 Markdown)` 的输入共享同一个在途请求；只有请求成功且结果仍未过期才可复用。取消或关闭后不再启动新任务，并忽略迟到响应。进度按物理格子计数，局部失败可重试；整体确认后仍按完整物理/共享保存单元串行写入。每个保存单元继续校验 `expected_before`、anchor 指定、完整分组精确成员及 HTTP 409 stale 守卫；过期候选保留并可重跑，父表直到弹窗关闭才 reload。任何 stale 一经观察就立即登记待刷新，即使随后中止其余慢请求也不会丢失。队列里的有改动/已保存项可在同一弹窗进入单项原始 Markdown diff（行级增删，并对中文、拉丁文本、标点、空白做有界行内 token 高亮），也可切到渲染预览；超长内容降级为有界首尾摘要。已保存项可安全结束批量弹窗，再打开既有物理格详情；若同一批次还观察到 stale，父组件必须先等待带 epoch 守卫的详情 reload 成功，再从新行集重算目标。刷新失败、请求 epoch 失效或行/列已消失时不得打开旧目标，并通过现有可恢复的表操作错误横幅提示用户。共享/合并值选择 `row.position` 最小、再按 row id 的稳定代表格。该流程仍是一次整体确认，不扩成逐项接受/拒绝。

主表继续采用 `table-layout: fixed`、横向滚动和 sticky 首列，但通过 `colgroup` 输出内容感知宽度。纯函数只摘要表头和最多 64 条可见行（前 48 + 后 16）；每个样本格必须在换行归一化、Markdown 正则/分行和 grapheme 处理之前先截断固定 code-unit 前缀，再最多考察 8 条可见行、120 个 grapheme，折减 Markdown 控制符，并让 CJK/全角/emoji 权重大于 ASCII，最后按列类型套 min/max，状态/操作列保持固定宽度；窄屏使用更紧的范围。计算以表 identity、列和可见行为 `useMemo` 依赖，任何 render 都不得做无界 R×C 或整格扫描。本次不包含手动拖拽和宽度持久化。

Knowhow 的所有权与权限仍使用稳定 user id（`created_by`、owner 与权限检查）。面向人的审计快照（`knowhow_changes.actor`、里程碑创建者、格子代码更新者标签）统一取 session 用户 `username.trim()`，其次 `display_name.trim()`，最后才回退 user id；Agent 继续使用 `profile_name`。所有普通 Knowhow 写入口共用同一 actor-label helper，复制、导入、转移等路径则显式拆开 identity id 与审计 label。历史 id 形状的审计值不做破坏性改写；读 API 在固定上限内批量识别并解析为当前 username，未知/已删除用户或 Agent 自由文本原样返回，严禁 N+1，async 路由中的同步 identity 投影必须在线程池执行。对 `origin=agent` 的历史 change，只解析语义 `payload.before` 快照中被替换的旧人类 updater；Agent actor 与 `payload.after/current` 的 updater 始终按 profile 自由文本原样保留，即使碰巧形似 user id。wire 字段名保持兼容。尤其 `knowhow_cell_code.updated_by` 已参与整表 fingerprint，不能仅为显示批量改写存量：新写入保存 label，单格 GET/PUT 响应直接携带可读 `updated_by`，读取投影不改变历史链。

记录型表的行详情抽屉，以及行标题分组矩阵里的每个物理分支，都提供显式的**智能补全空列**。它只为该行真正未存值或存为精确空串的格子生成建议；已经保存的纯空白文本仍算现有内容，不会被静默改写。一轮请求汇合两路证据：同表最多 8 条至少填写了一个目标列的参考行（优先同一行标题分组，再按当前已填写列的相似程度与覆盖度挑选），以及一次对当前 notebook 与当前有效显式挂载参考库的有界 `ReasoningRetriever` 检索。后者复用 Ask `reasoning` 的规划、联邦检索、反思、关系扩展与有证据查询期推导，但补全专用策略会在候选进入模型反思前排除私有 Memory 与当前表自身投影，并关闭来源归属不透明的 PPR/社区扩展以及精确标识符通道（补全的查询是 JSON 信封而非问题，否则会探测信封自身的字段名）；它不会调用 Ask 答案合成、创建对话/job 或保存 Ask 答案。结构化响应会为每个请求列给出建议或明确放弃、置信度、简短依据、合法同表行 id 与服务端签发的库内 evidence key，并附最终推理轨迹和有界证据卡。模型伪造的 evidence key 会被过滤；过滤后没有任何合法表内或库内引用的建议会强制变成 abstain。personal 与 base 证据冲突时以 base 为准并披露差异。可拖动的审阅弹窗分开展示同表参考与禁用链接/图片的库内 Markdown 证据，用户逐项确认，系统绝不自动写入。接受时仍走普通格子保存，并携带 `expected_before=""` 与 `origin="llm_complete"`：生成期间若格子已被填写，就不会覆盖，正常历史与同步照常触发。`reasoning_agent` 与 `knowhow_complete` 都必须配置且以 system 级指令把证据视为不可信数据。推理响应畸形、任一 provider 不可用、检索/合成失败，或合成响应不可解析/顶层结构不可用时显式失败；单条建议畸形则过滤、降级或转成 abstain。任何路径都不能静默退成同表补全或用离线内容冒充建议。

Ask 引用命中 knowhow 格子时会直接跳转到该行的详情抽屉，而非通用来源视图。notebook 深拷贝会把 knowhow 表完整带过去——表、列、行、格子、代码附件在副本里全部重新映射 id——且不重跑 embedding，未变化的格子文本在副本里复用原向量。

外部 Agent 接入面（HTTP + MCP、判别集、代码附件）见 [Memory 与 Agent MCP](#memory-与-agent-mcp)；HTTP 路径清单见 [API](#api)。

## Memory 与 Agent MCP

从界面签发 token、配置 Codex/Claude CLI 并运行官方 client 示例的逐步操作见
[Agent MCP 与 Memory 接入 SOP](./agent-mcp-memory-sop_zh.md)。本节继续作为产品/API 权威契约；
SOP 负责首次接入与验收步骤。

Memory 必须由用户手动选择、归创建者私有，并且始终绑定到且只绑定到一个 notebook。
在 Ask 回答上点击“保存到 Memory”后，后端先生成标题、正文和标签预览，用户可编辑，
只有最终确认才写入 `confirmed` Memory。预览模型未配置或失败时，系统确定性地用问题作
标题，并用移除显示引用后的回答作正文。当该 Memory 所属 notebook 非 base 库且已开启
知识图谱抽取（与上传来源同一判定门）时，确认动作与“保存到 Memory”弹窗会显示默认勾选的
“同时抽取到知识图谱”复选框；勾选后用与上传逐字相同的抽取管线把该 confirmed Memory 抽进
该 notebook 自己的 KG，记为对用户不可见、不进任何来源列表与计数的隐藏合成源，可在每次
确认时取消；base 库除外，只经下文的晋升人审进入 KG。总 Memory 页面只聚合当前登录用户的数据；
notebook 卡片数量和 notebook Memory 标签是同一份数据的 notebook 局部视图。总数与待确认数
始终按 owner 全量统计，不随状态、搜索或 notebook 筛选变化；notebook 筛选项来自有界的 owner
聚合查询，不做逐 notebook 查询。

生命周期为 `candidate | confirmed | rejected | deprecated`。Agent 只能创建 `candidate`；
token 具备 `memory:read_candidates` 时，同一用户、当前所选 notebook 下获授权的所有 Agent
profile 都可检索它。Candidate 永远不会进入正式 notebook Ask、notebook 搜索、Deep Report
或 `search_notebook_context`；只有用户确认后才进入正式平面。Rejected/deprecated 在两个
平面都排除。检索先判断相关性，权威只在同等相关或冲突证据间生效：
`candidate < personal 原始证据 < confirmed Memory < base KG/base 原始证据`。

Candidate provenance 会保存创建它的 Agent profile id/name 与每一条提交的 evidence ref，但绝不
保存 bearer token。服务端逐条按 candidate 的 owner 与 notebook 校验，并保存 `validated` 或
`invalid` 状态及有界原因；历史未验证或无效引用仍可由 owner 查看，但绝不会标成 trusted 或成为
可晋升 evidence。Candidate 详情、审核与 provenance API/UI 都只对 owner 开放。把 Ask 回答保存为
Memory 时，后端会在写 Memory、revision、provenance 的同一个 `BEGIN IMMEDIATE` 事务内再次校验
owner/member 实时权限，因此并发撤销分享不会留下半写入 Memory。

由 Ask 保存的 Memory 会持久化回答的 citation provenance，并在每张 Memory 卡片展示来源显示名、
与之不同时的原始上传文件名、位置和原文摘录。在 notebook 内的 Memory 标签中，仍存活的引用可经
当前活跃 notebook 的参与集权限打开精确 source element。复制或移动后的 Memory 继续在嵌套
provenance 中保留原引用，但明确标成仅存档；目标 notebook 不会把这些旧 id 当成新的跳转权限。

Memory 输入在 API 与 service 两层统一归一化并 fail-closed：title/content 去除首尾空白后必须非空。
当前上限为 title 80 字符、content 40,000 字符、tag 最多 20 个且每个 80 字符、审核/candidate
reason 1,000 字符、task context 序列化 UTF-8 8,192 bytes、evidence 最多 50 条且序列化 UTF-8
32,768 bytes、client request id 200 字符。HTTP 违规返回 422；MCP/内部调用也经过同一 service 校验。
嵌套 NaN、正负 Infinity 会在持久化前被拒绝，合法 JSON null 则保持原样往返。
MCP 提案严格使用这些 Core 上限，不再叠加更窄的重复限制。
tag 原始列表会先按 20 条限额校验，再 trim/去重；空白 tag 直接拒绝。

总 Memory 页的“Agent 接入”可创建稳定 Agent profile，以及明文只显示一次的 token。
Token 有过期时间、默认 notebook、notebook allowlist，并只授予所需的
`knowledge:read`、`memory:read`、`memory:read_candidates`、`memory:propose`、
`ask:execute`、`knowhow:code`、`sources:write`、`sources:delete`、`maintenance:execute`
子集；可即时撤销。后端 requirements 已包含官方 `mcp>=1.26.0` client/server
SDK。启动后，Streamable HTTP 服务位于 `/mcp/`（写 `/mcp` 会经 307 到达）。本机可用
签发回执还会给出匿名 `GET /api/agent-mcp/onboarding`：这是一份机器可读的 Markdown 交接说明，
把 `MCP_PUBLIC_URL` 逐字印成要配置的地址（绝不改写——代理可能只公布这一条精确路由），同时写明
打到后端的 `POST /mcp` 是 307 指向 `/mcp/`，好让客户端无法在重定向中保留方法、请求体与
Authorization 的 Agent 知道解法；工具清单从部署实例的冻结目录派生，`mcp_server.PUBLIC_TOOLS`
来自同一默认冻结组合目录。用户把链接与 token 分开交给 Agent；该端点绝不接收、嵌入或回显 bearer token，并且在 repository warm-up
尚未完成时也可读取。
带任意 query string 或 Authorization header 的请求会被拒绝。启动也会拒绝非绝对
`http(s)`、path 不精确等于 `/mcp`，或含 userinfo、query/fragment、空白/控制符/反引号的
`MCP_PUBLIC_URL`。
loopback HTTP；默认允许远程明文 HTTP 并放宽 Host/Origin（DNS-rebinding）校验，供可信内网使用，
启动会打印明文告警（Agent token 明文过网）。公网部署请设 `MCP_REQUIRE_HTTPS=1` 强制 HTTPS
（并恢复 Host/Origin 校验），并把 `MCP_PUBLIC_URL` 设为公开的 HTTPS `/mcp` URL。

**长任务发心跳，传输走 SSE。** MCP 客户端不会无限等一次工具调用：Claude Code 用的是
*idle* 超时——一次调用在若干秒内既没给出响应、也没发过任何 progress 通知就被中断——别的
客户端则是每次调用一个固定上限。`reasoning` 档的 `ask_notebook` 动辄跑几分钟（规划、联邦
检索、反思循环、答案合成），`build_kg` 更久，所以没有心跳时客户端会放弃一次服务端仍在正常
执行的调用，Agent 看到的是一个传输错误，而答案本来马上就到。因此 22 个 core 工具的阻塞主体一律
跑在同一道心跳下，**每 5 秒**一拍，内容只有工具名与已耗墙钟秒数——绝不带问题原文、笔记本或
来源名称，与观测事件同一条口径。不需要它的场合是免费的：客户端没有在请求 `_meta` 里带
`progressToken` 时该通知是 no-op，而第一拍要等满一个间隔，所以毫秒级返回的工具（绝大多数）
一拍都不会发。它也绝不会让调用失败——通知写不出去（客户端挂断）就停止心跳，工作照常跑完。
心跳不是我们这边的超时：这个面上没有任何东西会放弃仍在运行的工作。

要把这些心跳送到客户端，Streamable HTTP 传输必须以 `text/event-stream` 应答，而不是缓冲成
一个 JSON body：JSON 模式下 SDK 会把每请求的流读干、只找响应，**沿途经过的每一条通知都被
丢弃**——`report_progress` 照样“成功”，客户端什么也收不到。唯一一处用户可见的代价，写在这里
而不是留给别人踩：`POST /mcp/` 必须带 `Accept: application/json, text/event-stream`，否则
传输直接回 **406 Not Acceptable**。Streamable HTTP 规范本来就要求客户端两个都发，官方 SDK、
测试与 SOP 里手写的 `curl` 也都是这么发的。响应带 `X-Accel-Buffering: no` 与 15 秒一次的 SSE
保活注释，好让前置 nginx 不去缓冲这条流、把心跳静默架空；不认这个 header 的代理需要为
`/mcp` 这条 location 关闭响应缓冲，并把读超时设到高于该部署预期的最长一次回答。注意两层是
不同的东西：保活注释是让 HTTP 与代理计时器不掉线的裸字节，只有 progress 通知才能重置客户端
MCP 层的 idle 计时。

客户端自己的上限仍是外层边界，且只能由客户端配置，服务端抬不动它。Claude Code 是在
`~/.claude.json`（或项目的 `.mcp.json`）里给该服务条目加 `"timeout": <毫秒>` 后重启，全局
等价物是环境变量 `CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT` 与 `MCP_TOOL_TIMEOUT`；Codex 是服务条目
上的 `tool_timeout_sec`。默认值随客户端与版本而异，所以 `reasoning` 回答或构建耗时较长的部署
应显式调高，而不是指望某个特定默认值。
过期时间必须带明确时区偏移；浏览器把本地 `datetime-local` 转成 UTC，后端按 UTC 瞬间归一化保存。
无时区 datetime 会被拒绝，不会按服务端本地时区猜测。

Codex 推荐把签发的 token 放入环境变量，再注册服务：

```bash
export SILICON_NOTEBOOK_AGENT_TOKEN='<一次性显示的 token>'
codex mcp add silicon-notebook --url 'http://127.0.0.1:8000/mcp/' \
  --bearer-token-env-var SILICON_NOTEBOOK_AGENT_TOKEN
```

Codex 持久化的是环境变量名，不是 token 值。Agent shell 子进程中的临时 `export` 无法修改
当前客户端的父进程环境。Agent 可以保存 URL/配置；若没有获准使用的持久 secret 机制，必须请
用户在启动 Codex 的环境中设置变量并重启。`codex mcp list` 只证明配置项存在；只有新 session
发现 MCP，并成功执行 `list_notebooks` 与 `select_notebook`，才算认证接入成功。

当前本机 Claude Code CLI 接受 HTTP transport 和显式 Authorization header，并会在连接时按启动它
的进程环境解析 header 里的 `${VAR}`：

```bash
claude mcp add --transport http silicon-notebook 'http://127.0.0.1:8000/mcp/' \
  --header 'Authorization: Bearer ${SILICON_NOTEBOOK_AGENT_TOKEN}'
```

header 必须单引号，否则 shell 会先展开它；这样落到配置里的是变量名而不是凭据。未定义的变量会被
原样发出、只以坏 token 失败且配置阶段不报错，所以只有真的连一次才能证明它解析成功。不加
`-s user` 时该配置只在当前目录生效。若客户端不支持插值，落盘的就是原始 header：应使用最小
scope、短有效期，保护本机配置，并在使用后撤销/轮换。

每个新 MCP session 必须先调用 `select_notebook`，再调用数据工具。默认 core 的二十二个工具如下；
`mcp_server.PUBLIC_TOOLS` 就是下面这 22 条本身，不是更大的组合目录——它与 `mcp_server.CORE_TOOLS`
是同一份清单：

| 分组 | 工具 | Scope |
| --- | --- | --- |
| Memory / 上下文 | `list_notebooks`、`select_notebook`、`search_agent_memory`、`search_notebook_context`、`get_memory`、`ask_notebook`、`propose_memory` | `knowledge:read` / `memory:read` / `memory:read_candidates` / `memory:propose` / `ask:execute` |
| Knowhow 读取 | `list_knowhow_tables`、`get_knowhow_discrimination`、`get_knowhow_row` | `knowledge:read` |
| Knowhow 代码写入 | `put_knowhow_cell_code` | `knowhow:code` |
| 引用点查 | `get_cited_element` | `knowledge:read` |
| 来源管理 | `add_source_text`、`add_source_url`、`reparse_source` | `sources:write`（owner-only） |
| 来源删除 | `delete_source` | `sources:delete`（owner-only，且仅限 Agent 添加的来源） |
| 来源状态读取 | `get_source_status` | `knowledge:read` |
| 构建 | `build_kg`、`build_retrieval_index` | `maintenance:execute`（owner-only） |
| 构建状态读取 | `get_build_status` | `knowledge:read` |
| 库理解（Agent） | `get_notebook_profile`、`add_observation` | `agent_profile:read` / `agent_observation:write` |

实际部署以 server-local frozen catalog 作为 discovery 与 onboarding 的权威清单：它精确等于上表
22 个工具，由七个 core registrar 实时派生，`mcp_server.PUBLIC_TOOLS` 就是这份清单本身而不是第二份
手抄。每次调用都重新检查 live token/scope/allowlist/成员权，所有写 scope 都强制经过 owner-only
notebook 闸。结果在构造时就被复制进有界形状——深度不超过 5 层，逐字段/map/list 施加上限——超大容器不会
先被完整构造出来才裁剪。这份有界拷贝随后被逐步、可见地压缩（先缩最长字符串，再丢 map 条目，
再丢 list 条目，最后才动标识符），直到总量落进 12,000 UTF-8 bytes 预算内；每一刀都记进
`truncation` 字段回传（`truncated`/`omitted_items`/`omitted_map_entries`/`omitted_characters`/
`omitted_fields`）。异常只返回稳定错误码；FastMCP schema 错误发生在工具体之前，归 transport/request
audit。只有拷贝真的缩无可缩时才整次拒绝，不会返回被静默截断的结果。

`list_notebooks` 与 `select_notebook` **不需要任何 scope**：判据只有 token 存活、目标笔记本在
白名单内、且对它有读权限。因此无论 token 权限收得多窄，session 都能正常起步。

服务端会在数据调用时重新检查 scope、allowlist、token 状态和 notebook 权限；返回文本是
不可信 evidence，不是可执行的 Agent 指令。

`ask_notebook` 接受可选的 `conversation_id`（至多 200 字符，与 `AskIntentPreviewRequest.conversation_id`
同一上限），并回传本次答案实际记入的 `conversation_id`。传入 id 即接续该会话跨轮对话——包括
另一个 Agent profile 或网页端开启的会话——前提是它属于同一 owner 且同一个已选笔记本。属于
其他笔记本或其他 owner 的 id **不报错**：服务端会静默新建一个会话，调用方通过比对回传 id 与
自己发出的 id 即可察觉。每个 anchor 另带 `source_id`、`element_id`，knowhow 投影节点还带
`knowhow: {table_id, row_id}`。新增的 `citations` 回退列表携带非 anchor 证据的 `label`、
`source_id`、`element_id`、`location_label`、`quoted_span`、`source_file_name` 与 `tier`；
`notebook_id` 与 `memory_id` 只在非空时下发，knowhow 投影来源的 citation 也带与 anchor 相同的
`knowhow: {table_id, row_id}`。`memory_id` 非空的行需要 `memory:read`——没有该
scope 时整行在结果截断**之前**被过滤，且不计入截断计数，否则那个被隐藏的私有 Memory 条数会
被算术还原出来。anchors 与 citations 各自最多 20 行。响应预算分两步：先把**每一条** anchor 的
`provenance` 各自压到 500 字符，然后才把 anchors 整体压到 3,500 字符；citations 另外预压到
1,800 字符，使大体量引用不会挤掉正文。

`get_cited_element` 把一条引用还原回原文：按 `ask_notebook` 或 `search_notebook_context` 返回的
`source_id` 与 `element_id` 原样传入，取回该元素自身的文本、它在文档中的位置和文档显示标题。
它披露的范围不超过当前所选笔记本的答案本来就可以引用的内容——本库来源加上它当前挂载的参考库。

**来源管理。** 这一组里凡是接受 `source_id` 的工具，都只在**当前所选笔记本内**解析它——不含
挂载的参与库，也不含隐藏的 `memory`/`knowhow` 投影行。这比 `get_cited_element` 更窄：后者刻意
覆盖已挂载的参考库，因为答案的引用本来就会指向那里。

`add_source_text` 用 Agent 提供的文本建立一份 Markdown 文档来源：`title` 至多
200 字符，`content_md` 必须非空且不超过本部署的 `SOURCE_UPLOAD_MAX_MB` 单文件上限（按存储的
UTF-8 字节计）。提交的标题逐字存进来源行的标题；磁盘文件名是另一个**派生**值——经净化、压到
200 UTF-8 字节（好让 `{source_id}_` 前缀与 `.md` 后缀一起仍装得进 255 字节的路径组件）、再缀上
后缀——标题过长时被压缩的只有这个文件名，存下来的标题不受影响。重复提交
逐字节相同的内容会复用既有来源并回传 `reused: true`，不产生重复行。`add_source_url` 按 URL
添加 PDF，服务端会先探测，取不到或不是 PDF 一律拒绝。两者都受笔记本文档数量上限约束，唯一的
例外是「重复提交解析到既有来源」——它不新增文档，在已满时仍然放行，否则上面那条幂等承诺恰好
会在最需要重试的时候失效。解析
在后台进行，用 `get_source_status` 轮询：它返回 `parse_status`、`status`、`element_count`、
`kg_extracted`（图谱里是否有这份来源的知识对象）、`kg_analyzed_empty`（分析**跑完了**、而这份
文档确实没有可整理的知识——正文极少或整份是没有图注的扫描件）、`agent_created`，以及派生的
`parse_failed` 布尔与
`parse_quality_warning`，而不是原始 `error_message`（后者是逐字保存的 `str(exc)`，经常带着
服务端绝对路径）。`parse_quality_warning` 是 MinerU 降级信号：即使来源已到 `extracted`，版面、
公式与表格仍可能有误，准备引用它的 Agent 需要知道这一点。`reparse_source` 重跑一份来源的解析与抽取；该
来源的解析锁被占用时直接拒绝（约 0.5 秒的有界探测而非等待——那把锁跨越两次模型调用，真的
正在解析的来源一秒后仍在解析）。

`delete_source` 不可逆，权限刻意收窄：需要 `sources:delete`（`sources:write` 不蕴含它），
**并且**该来源必须是 Agent 添加的。判据是 `agent_created` 布尔——v48 `sources.agent_profile_id`
非空的投影——所以用户上传的文档无论 token 带什么 scope 都删不掉。判据是「某个 Agent 添加过」
而不是「本 profile 添加过」：Agent 身份会轮换、会撤销，按 profile 判会让退役身份留下永远删不掉
的来源。出处只在 INSERT 分支写入，因此重传用户的字节只会复用他那一行、仍算用户添加，笔记本
深拷贝还会显式清空该列——副本一律视为用户添加。来源列表与详情响应同样暴露这个 `agent_created`
布尔，网页端来源列表把它渲染成中性的「Agent 添加」徽标。

**构建。** `build_kg` 触发增量知识图谱抽取（已抽取的来源跳过，此前部分失败的来源重试），
`build_retrieval_index` 触发检索索引重建，`when="now"`（默认）立即开始、`when="idle"` 排进
下一个空闲窗口。两者都是 owner-only、立即返回，由 `get_build_status` 轮询——它同时给出图谱
状态（就绪/构建中、待处理来源数、当前任务阶段与进度）与检索索引状态（是否存在/构建中/排队中、
队列位次、下一个空闲窗口）。`build_kg` 因该笔记本已有构建在跑而拒绝，是**排队信号而不是错误**：
笔记本级单飞守卫正在生效，调用方应轮询 `get_build_status` 直到它清空，而不是立刻重试。
`build_retrieval_index` 在笔记本规模不足以需要索引时拒绝。`get_build_status` 是纯读取，笔记本
的任何成员都可调用。

整个来源与构建面的写入一律 owner-only。token 的白名单可能包含 owner 只是以只读成员身份加入的
笔记本；在那里添加、重新解析、删除来源或发起后台构建，等于把这份共享的读侧升级成写侧，因此
一律拒绝。读取仍沿用其 HTTP 对应端点的成员可读口径。

四个 knowhow 工具与 `/api/agent/knowhow/...` 下的 HTTP 端点（见 [API](#api)）共用同一套
service 函数，HTTP 与 MCP 不会在响应形状上走样。`list_knowhow_tables`、
`get_knowhow_discrimination`、`get_knowhow_row` 需要 `knowledge:read`；
`get_knowhow_discrimination` 对设有行标题列的表按行返回标题，以及每个方法步骤列的
`{column_id, column_name, text, code_status}`（表未设行标题列则返回 400），供 Agent
据此跑自己的判别逻辑挑选适用的修复方法。`get_knowhow_row` 返回一行的完整格子文本
（方法步骤/工具事物列另带 `steps`/`items`）及该行全部**代码附件**的代码本体。代码附件
是外部 Agent 针对某格方法已经写好的代码——notebook 从不生成也不执行，也从不进
embedding/chunk/索引/KG 投影——其新鲜度（`implemented`/`stale`/`none`）在读取时用格子
当前内容的 hash 与附件保存时的 hash 比对推导；判别集只带这个三态，不带代码本体，以控制
体积。读代码依然只需要 `knowledge:read`；只有写入（`put_knowhow_cell_code`，以及对应的
HTTP `PUT`/`DELETE .../code`）才需要 `knowhow:code`——一个既要读现有代码又要写新版本的
token，两个 scope 都要授予。

与上面来源与构建面的 owner-only 写入不同，`knowhow:code` 刻意**不加 owner 轴**：Agent
在这里的写能力完全由 scope 决定（设计文档 §⑥-4），owner 只是以只读成员身份加入的共享
笔记本，也能保存格子代码附件。代码附件是惰性数据——从不执行、不进索引/embedding/KG
投影，而删除或重新解析文档会波及每个成员的检索，两个面因此采用不同的权限模型。这处
分歧是拍板取舍不是疏漏，`backend/tests/test_memory_mcp.py` 对两侧行为各钉一条测试。

**库理解（Agentic Memory P3）。** `get_notebook_profile`（scope `agent_profile:read`）读取
的正是网页端「AI 对这个库的理解」面板同一份数据：共享 `base` 层加调用者自己的 `mine`
覆盖层（绝不是别人的），每块只投影 `{label, value, updated_at}`——不带 `evidence` 来源
id、不带 `revision`、不带变更历史，因此只持有这一个 scope 的 token 无法借此探测本无权
读取的来源 id。响应里每块都标 `content_is_untrusted_evidence: true` 与
`citable: false`：它是规划用的提示脚手架，绝不能被引用。`AGENT_PROFILE_ENABLED` 关闭、
或该库尚未生成过理解时，工具返回 `enabled: false` 与空块，而不是报错。
`add_observation`（scope `agent_observation:write`）向调用者自己在该库的观察队列追加一行
不超过 `AGENT_OBSERVATION_TEXT_MAX_CHARS`（500）字符的记录，按 `client_request_id` 幂等去
重（与 `propose_memory` 同一套机制）。幂等窗口**以环形保留为界**（登记的合同而非缺陷）：
当 `AGENT_OBSERVATION_RING_MAX` 条更新的观察把某行淘汰后，重试它的旧 `client_request_id`
会写出一条新行——为以秒计的重试合同单开一张永久 key 表不值一次迁移。它立即返回——写入本身只是一次有界 INSERT 加同一
事务内的有界环形淘汰 DELETE，零模型调用，因此没有异步状态可轮询。它是**第二个**绕开
`_writable_notebook` owner-only 门的 Agent 写（第一个是 `put_knowhow_cell_code`）：爆炸半径
结构上只到 token 持有者自己的覆盖层而非整库检索，因此只读成员自己的 Agent 也能用它；这条
观察记录的用途与边界详见下面「Notebook understanding blocks」一节。特性关闭时
`add_observation` 直接报错，而不是静默收下一批永远不会被巡固任务读取的数据。

只有 `confirmed` Memory 可发起 KG 晋升。创建者提交后，admin queue 展示脱敏后的结构化提取
候选与服务端验证过的 evidence，而不是原始 Memory revision/provenance 浏览器。提案会固定精确的
来源 revision、脱敏候选快照和审核所见 evidence；编辑或弃用审核中的 Memory 会在同一事务中废止
旧提案并重置晋升状态，编辑后可重新提交。当前 provenance 会清除 proposal 指针，固定提案只保留在
快照与队列历史中。批准时会重新校验 Memory 当前仍为 confirmed 且创建者仍有访问权，
并在写事务内校验固定 revision 与 notebook，再复用 KG dedupe/merge 创建或合并一个或多个 Base KG 对象；批准/拒绝会记录当前登录的
admin reviewer，API 与晋升审计记录完整的 `base_object_ids`。这一过程不会改变或暴露原私有 Memory。
删除 notebook 会级联删除所有成员绑定到它的私有 Memory，因此删除弹窗会提示这一生命周期
后果，但不会泄露成员身份或数量。

仓库内固定 Memory 评测计算 Recall@5、MRR、nDCG，以及三项零容忍计数：candidate 进入正式
平面、跨用户、跨 notebook 泄漏。A/B harness 比较 no-Memory、KB-only 与
KB+confirmed-Memory 三种检索条件。

## KG 抽取触发

源解析 + 向量化完成后即可做 chunk-native 检索，因此 **KG 抽取按 notebook「按需开启」，并非每次上传都抽**：

| 上传时 notebook 状态 | 是否抽 KG | 怎么触发 |
|---|---|---|
| 尚无 KG（新库） | **不**自动抽 | 按需构建：`POST /api/notebooks/{id}/kg/build`（界面：notebook 的**「构建知识图谱」**动作；在无 KG 的库上选「深入分析」组——即 `strict` 的 `reasoning` / `graph`——时也会提示构建） |
| 已有 KG | 每个新源**自动后台抽取** | 无需手动触发——续抽以保持 KG 完整；新源随后增量融入跨文档统一 KG |

摄取期判定 = `KG_AUTO_EXTRACT 或 该 notebook 已有 KG`：

- `KG_AUTO_EXTRACT`（默认 `false`）——为 `true` 时**所有** notebook 每次上传都抽 KG。
- 否则仅当该 notebook 已有 KG 对象时，上传才抽。

即：**首次 opt-in**（构建 KG，或设 `KG_AUTO_EXTRACT=true`），之后新文档自动抽取 + 融合。整库重抽用 `POST /api/notebooks/{id}/kg/rebuild`；离线批量构建见[离线批量摄取](./operations_zh.md#离线批量摄取目录--kg)。

### KG 构建故障隔离

手动整理/全部重新分析会创建持久化、任务级的 `kg_build_jobs` 记录；同一 notebook
同时只允许一项 KG 任务运行。Notebook 与索引状态 API 会返回
`probing → extracting → stopping → finished`、来源进度和经过审查的用户提示。
前端刷新后仍能恢复该状态；失败后显示「继续分析未完成内容」。

被中断的任务落到同一个失败终态：离线批量运行按 Ctrl-C 或收到终止信号时，会合作式
停掉在飞窗口、在任务落终态之前把它们排空（守卫随该行一起释放），并记 `worker_interrupted`，因此被终止的运行不会让知识库一直显示「分析
中」。只有无法捕获的终止（SIGKILL、被 OOM 杀、掉电）才会把该行留在进行中，那种情况
由服务端启动兜底落终态。

每次 KG 模型请求使用 `KG_LLM_TIMEOUT_SECONDS`（默认 `60` 秒），瞬态错误最多重试
`KG_LLM_MAX_RETRIES` 次（默认 `2`，允许 `0..3`）。若服务持续不可达，或认证失败/
请求被永久拒绝，本次任务共享的中断控制会阻止继续发起请求，取消尚未开始的
source/window 工作，在首个窗口确认熔断时、窗口级与来源级 drain 开始前就持久化
`stopping`，再等待已经开始的调用安全退出。中断范围只限当前 notebook 的本次 KG
任务，不影响其他 notebook 或之后重新发起的任务。可用性探测会显式绕过 LLM 响应
缓存且不回写缓存，旧的成功探测不能在当前模型已经不可用时放行破坏性重建。

**离线跳过模式（opt-in，仅 CLI）。** 停掉整个任务对交互式构建是对的默认值，但对
上千个来源的无人值守长跑是错的取舍：一次偶发波动就报废一整轮几小时的批量。
`scripts/batch_ingest.py kg --skip-model-failures` 把「模型不可用」的作用域从任务
收窄到**单个来源**：该来源用运行控制器的子控制器，因而只有它自己的在飞窗口被取消
并排空；它退回未分析状态（重跑同一条命令即自动重试被跳过的那些），任务继续往下跑。
兜底仍在：连续 `--max-consecutive-model-failures` 个来源都因模型问题失败、中间没有
任何一个成功时，升级为原来的任务级熔断，照常公布
`stopping`/`kg_build_circuit_opened`。阈值高于 `--workers` 这条关系是**强制**的——
一次瞬时抖动会同时打中所有在飞来源：省略该参数时按 `max(32, 2 × workers)` 派生，
显式给出不高于并发度的取值则直接报错而不是静默抬高。跳过模式**刻意不覆盖起始可用性
探测**：探测失败意味着服务此刻就是死的，而用户尚无任何投入，快速失败既更省又更诚实
——放行只会对着已知挂掉的服务逐源白烧重试预算，凑满阈值后照样停。该计数是任务级的、跨目标分页存活：抽取目标按 500 行原始来源翻页，
稀疏 notebook 每页可能只有一个目标，按页重置的计数永远达不到阈值。开关默认关闭，
API 路径无法设置；开启时打印告警，且成功、升级熔断与中断三条路都会报出被跳过的
来源数与有界 id。

已完成来源的结果会保留；同一来源的 object/relation 分块共用一个 SQLite 事务，被
中断或写入失败的来源不会留下半成品；旧版本遗留但最新 extraction run 已失败的图也
仍判定为未完成。之后普通「继续分析」只处理未完成来源。只有显式「全部重新分析」
会清空已有 KG，而且会在删除前先探测模型服务。若进程重启时仍有 running job，启动
恢复会把 job 与 running extraction run 标为 failed，并把所有遗留的 `extracting`
来源恢复为 `parsed`，包括尚未来得及创建 extraction run 就中断的来源。extraction
run 进入完成或失败终态后还会精确失效该 notebook 的待处理来源缓存，避免轮询长期把
已完成来源误报为未完成。

前端用 notebook、workspace epoch 与请求 epoch 共同约束建库响应归属，并在持久化 job
仍为 `running` 时持续轮询，不再用固定时限伪造本地完成。安全结构化事件覆盖
`kg_build_started`、`kg_build_progress`、`kg_build_circuit_opened`、
`kg_build_stopping`、`kg_build_succeeded` 与 `kg_build_failed`，不记录 provider
诊断、prompt、来源正文、token 或凭据。

### 知识冲突检测的规模边界

`POST /api/notebooks/{id}/kg/conflicts/resolve` 是一次后台通道：先零模型地找出可能
互相矛盾的候选对，再对每一对做一次 LLM 裁决。它因此有两类成本，各自设界。

**准入与单飞。** 该 notebook 的活跃知识对象数超过 `KG_CONFLICT_MAX_OBJECTS`（默认
`200000`），或关系数超过 `KG_CONFLICT_MAX_RELATIONS`（默认 `1000000`）时直接拒绝，
并给出可读文案——把这种规模的库放进后台，只会得到一个跑不完的任务和一张一直在涨的
账单。对象默认值的内存依据：向量以一整块 `(N, dim)` float32 装载，20 万 × 1024 维约
0.8 GB；独立的关系 rail 用来保护边数不受对象数约束的稠密图或重复佐证图。关系准入查询
本身最多扫描到 rail+1；worker 也只读取这个哨兵窗口，封住预检后并发新增关系的竞态。一旦
出现哨兵，整轮跳过，绝不把关系前缀冒充完整冲突检测。同一 notebook 同时只允许一次检测
在跑，重复点击返回 409。
这个单飞槽是**独立**的：它与「补上关联」「重新合并」互不排斥，那两个共用一个槽是因为
它们重写同一批派生产物，而冲突检测写的是冲突评审队列。**建图末尾自动跑的那一次走
同样全部闸**（同一个槽、同一组对象/关系判据）：已有检测在跑就跳过，超限也跳过，两种
情况都只记一条原因事件，绝不因此让建图失败。

**候选与裁决。** 进入 LLM 裁决的候选总数上限为 `KG_CONFLICT_MAX_CANDIDATES`
（默认 `800`），并**按信号类分配配额**：边策略（同头/同尾/同对不同边）与节点策略
（判别 + 语义）各得一半，某一类用不满时余额让给另一类，类内保留检测器产出序。这不是
细节：检测器会先输出全部边候选，一个高连接度节点就能产生上千条同头候选，纯前缀截断会
让判别类候选（nmos/pmos 这种）整类消失。被丢弃的条数按类如实回报（任务结果里的
`truncated` / `truncated_edge` / `truncated_node`，以及一条只含计数的事件），不会伪装
成「就这么多」。

**语义候选的规模分派。** 语义策略比较同类型对象的向量。同类型组内对象数不超过
`KG_CONFLICT_SEMANTIC_BRUTEFORCE_MAX`（默认 `512`）时保持逐对精确比较，结果与
改造前一致；超过时改用近似最近邻，每个对象取
`KG_CONFLICT_SEMANTIC_ANN_K`（默认 `10`）个近邻。后者是**近似召回**，属于登记接受的
行为差异：阈值以下走的仍是原来那条精确路径，阈值以上的库在改造前根本跑不完这一轮
比较。同属登记的还有一处数值表示变化：精确分支现在按 float64 点积累加而不是逐个装箱
浮点求和，差异在 1e-12 量级，只有恰好压在阈值上的对才可能换边。近邻索引不可用时只跳过
该组的语义策略、记一条只含组大小的计数事件，其余策略照常。

## 命令目录（工具手册）

工具的**命令手册**是常规摄取处理得最差的一类文档：分块会把同一条命令的说明、参数、
示例切成互不相干的片段，KG 抽取又会把参数表变成一堆游离的断言——于是一个关于命令
契约的问题，回来的是一段**关于**这条命令的散文。命令目录改为把这类来源按结构化条目
摄取（命令名、语法、参数、默认值、示例），并在人工确认后落进一张普通的 Knowhow 表。

全流程 opt-in、按来源触发，上传时什么也不跑。

**前提：来源必须已经解析完。** 成本预告与发起都要求 `parse_status` 落在
「已解析」这一档（`parsed` / `extracting` / `extracted`——后两档是解析之后的知识图谱
抽取阶段，元素早已齐了），否则返回带用户可读文案的 `409`：还在解析（或只导入了
元数据）就请等解析完成，解析失败则请重新解析或重新上传。对着还没落下元素的来源做的
成本预告会报出「约 0 段」，读起来像「这份文档没什么可抽的」；对它发起识别则会
把一份手册的一小段记成完整的一次识别。

**整篇通读，不做规则分节。** v1 先用规则挑出「命令节」再送模型，实测挑得不准——挑漏的
整段读不到，挑错的白花钱。现在不再做任何形状判断：整份文档按文档序打包成有界的**段**
（实现里叫窗口，每段至多 `WINDOW_CHARS` 字符）。元素整个放进当前段，放不下就开下一段；
单个元素比一整段预算还长时，按字符切成连续几段落进相邻的段——切点会向前回退到最近的空白
（换行优先，回退窗口 `SPLIT_BOUNDARY_LOOKBACK_CHARS`），因为命令名和 flag 里都没有空白，
落在空白上的切点就不可能把 `global_placement` 劈成 `global_pl` + `acement`（劈开之后两段
都不含这个名字，它在两段里都不是候选，这条命令就此消失）；回退窗口里一个空白都没有（压缩
串、超长单行）就按原位切，尽力而为。**没有任何内容被丢弃**
——v1「超预算的元素整个丢掉」的截断语义不复存在（实测里一张 120 参数的表整张消失，
只留下一个光秃秃的标题，而否决率还报 0.0，因为已经没有东西可以失败了）。每段的出处
标签取段内**第一个**标题；段内没有标题时沿用上一段的**最后一个**标题——一段从 `set_a`
开始、到 `set_e` 结束，它的下一段该标成 `set_e`，回头标成 `set_a` 会把审阅者指向几条
命令之前。

**每段的候选名单，以及它为什么能跨段接力。** 每段的合法命令名来自三类扫描：段内标题里
的标识符、用法行首的 token（代码块优先于散文）、行内代码里的标识符；标识符形状规则不变
（`_`/`.` 连接的放行，只用连字符的必须带数字）。扫描读**整段的每一行**，没有行数上限——
段本身已按字符有界，而一个元素可以是三百行的展平参数表，漏掉后面那些行等于让那里记着的
命令永远进不了名单、也永远不会被拆段救回来（服务不到的名字既不能被认领，也不会留下任何
拒绝记录或比率变化）。名单是**约束**而不是菜单：模型只能从名单
里逐字挑，名单外的名字整条否决——上限提到 `MAX_CANDIDATES`，因为一段是文档的一块切片
而不是一条命令的小节，好几条命令挤在同一段里是常态，名单在最后一条之前截断就等于把一条
真命令否决掉。

**超上限就拆段，不截名单。** 段与段不重叠、也不会被重看第二遍，所以名单截断不是「第 33 条
命令待会儿再问」，而是**这一轮乃至任何一轮都不会问**——模型只可能答错它被服务过的名字，
从没进过提示词的名字不会产生任何否决、比率变化或报告行，和 v1 的丢弃分支是同一类静默损失。
所以分段是两遍：先按字符预算打包，再把候选数超过 `MAX_CANDIDATES` 的段**按元素边界近似对半
拆开**（整段只有一个元素时按字符切，切点与超长元素同一套安全边界），递归到每段候选不超限
为止。拆出来的都是正常的段，一个字符都不会跨边界丢，代价只是多一次模型调用。递归到段长
`WINDOW_SPLIT_FLOOR_CHARS` 以下仍超限的，才保留截断：这种密度的段已经不是「命令被挤掉的
文档」而是一份**命令名索引**（「另见」块、全表命令清单），再拆每一片还是索引、却每片都要
付一次调用。这最后一种截断必须**披露**——被截掉的条数逐段记在 `candidates_overflowed` 上，
经窗口账目汇总进 run 账目，非零时随 `catalog_job_finished` 事件报出（正常文档恒为 0，所以
这个字段出现本身就是信号）。

一条命令的文档也经常比它开头那一段更长：120 个参数的表能占好几段，而第一段
之后的每一段里根本没有命令名，所以名单会**接力**——上一段有自己的候选就把自己的候选交给
下一段，上一段自己没有候选就把它接到的那一份继续往下传，遇到新候选即重置（新命令开篇就是
旧命令收尾）。接力来的名字允许在续段里被认领：名单成员资格照查，只免除「逐字出现在本段
原文」这一条（那个名字本来就是从它出身的那一段里逐字扫出来的，接力带过来的是见证不是猜测），
而 `syntax`、参数名与 `default` 仍必须在**本段**接地。**接力产出不清除疑似标记**：跨段合并
时「疑似只是被提及」这个标记按 AND 合并（任何一段把这条命令记全了就清掉它），但**靠接力
认领**的条目没有这一票——它的「不疑似」是接力的豁免而不是判定结果（续段里既没有标题也没有
用法行，本来就没在判这件事），拿它去 AND 会把上一段挣来的警告洗掉，而且恰好是在接力存在
的意义所在的那种形态上、也就是每一次都洗掉。判据是**本段有没有直接证据**（标题或用法行里
逐字出现），不是「名字在不在接力名单里」：接力名单里的名字若在本段有直接证据，它照样可以
清除标记。**已登记的取舍**：一段的候选名单是这段
**提到**的每一个命令形状的名字，不只是它记录的那一条，所以接力可能把一张孤立的参数表键到
一条只是被顺带提到的命令上——名字是真的、参数是真的、都在文档里，没有任何接地规则拦得住，
人工审阅是这一层唯一的兜底。

**零成本跳过闸。** 发不发模型调用是确定性判据：**本段有自己的候选**，或者**本段有 flag
形状的参数且接到了上文的名字**，两者有一个成立才发调用。按形状看是三种成本后果：命令后面
的整页散文免费掠过（有接力也不发——接力一旦开始就再也不空，凭它开闸等于按页付整本书的钱，
而那一页值得抽的只是一句说明）；文档开篇那种没有接力的孤立参数表也免费（一个可认领的名字
都没有，接地保证产出必为空）；真正接在命令后面的续表照常付费，这正是接力存在的理由。被
跳过的段不发调用、不做存活探针、也不发 `catalog_section_done` 事件（一份散文为主的 PDF 有
成千上万个这样的段），但照常计入进度——分母算它、分子不算它的进度条永远走不完；接力也照常
穿过它。那次进度写入的**返回值**因此是跳过段唯一的存活信号：来源或笔记本被删除会级联删掉
任务行，忽略它就会让后台一路写完剩下每一个段、再给一个已不存在的任务落终态（整篇是散文的
文档会报成一次普通成功，而结果谁也打不开）。所以进度写没写中就结束这次识别，按既有的「任务
已被删除」结局处理；用户显式点的取消优先级更高，两者同时成立时按取消收尾。

**抽取。** 每个来源一个后台任务，由覆盖 `queued` 与 `running` 的条件唯一索引守卫：
行先写、线程后起，落在那个窗口里的重复请求会被挡住，而不是排出第二个写同一份候选的
worker。每段起步一次模型调用；一段里的 flag 形状参数超过 `SLICE_PARAM_LIMIT` 个时按批
分片，每批一次调用（大参数表一次问会撑爆输出预算，所以分片是必需的而不是优化）。**每个
分片看到的原文是整段全文**：v1 那种「开头摘录 + 本批参数所在行」的窄视图，在「一个分片 =
一条命令的小节」时是对的，在「一个分片 = 一份上万字符的文档切片」时会变成系统性遮蔽——
三十个候选里只有第一个出现在视图里。代价是多分片的段每片重复携带整段（每次调用至多
`WINDOW_CHARS` 字符），刻意接受。没有第二意见，也没有精炼轮次。

**回答是一份条目清单。** 顶层是 `{"entries": [...]}`，这一段记录了几条命令就返回几条，
不再是一次一条。`entries: []` 是**合法**回答（这一段确实没有记录命令），不算「没给出可用
回答」——散文密集的手册不能因此被熔断误杀。只有缺 `entries`、或者它根本不是列表，才按
不可用回答处理并走减半重问的补救。清单里的非对象项会变成一条**可见的被拦行**，而不是
静默的缺口。

**接地校验，以及条目为什么会被拦。** 每条抽取结果落库前都要拿原文核对：命令名
必须在服务端给出的候选清单里、且逐字出现在**本段**原文（接力来的名字只免除后半条）；每个
参数名必须以原始形态出现（写成 `-density` 的就得带前导短横，原文里写着 `-density` 而回答
只给 `density` 一律拦）；`syntax` 必须是原文某条用法行的连续拷贝；原文里找不到的 `default`
会被清空。命令名不过关否决整条，其余只丢那一个字段。**被拦的条目同样入表**，带上原因和
一段有界的原文窗口——一次产出很少的抽取，用户唯一能自己判断「是模型错了还是这份
文档根本不是手册」的依据就是它们。

**参数、`syntax` 与 `default` 按命令的证据段接地，不是整段。** 一段是文档的一块切片、
常常同时记着好几条命令，所以「这个 flag 在这一段里有没有出现」是个错问题——它同时放过和
误杀两件事：一段里有 `foo_cmd density` 与 `bar_cmd -density` 时，把 `-density` 挂到
`foo_cmd` 名下能通过（那个 flag 确实在段里，只是在另一条命令的表里），而 `foo_cmd` 自己
合法的位置参数 `density` 反倒会被「掉短横」那条规则误拒（段里确实有 `-density`）。两者是
同一个毛病的两面。所以每段会按命令切成若干**证据段**：一行属于某个候选名的**结构性出现**
（该行在标题元素里，或该候选名是该行用法行的行首 token）就为它开一段，直到下一个别的候选
的结构性锚点为止；同名多次出现取并集；**行内代码提及不开段**——「另见 \`bar_cmd\`」正是
误归属的来源。首个锚点之前的**前奏段**归接力：续段的孤立参数表恰好就是它，零锚点的整段
即全是前奏，接力认领照常接地。代价登记在案：一条只被行内提及、既无结构性锚点也不在接力
名单里的命令，没有可接地的段，参数与 `syntax` 会全被拒——它保留名字（候选清单为它背书）
而失去正文，方向与「疑似只是被提及」的标记一致。**问了什么仍是段级的**：分批指派与覆盖率
账目照旧按整段的 flag 清单算，归属只影响「答案要在哪儿接地」，把它也切开会把「模型没答这
个参数」变成「模型把它挂到别处了」，那是另一件事。

**没有短横参数的命令照样抽参数。** 一整段里一个 `-flag` 都没有，不等于这条命令没有
参数——`set_dont_use lib_cells` 这种**位置参数**正是单行文档最常见的写法。这类命令没有
可服务的参数清单，所以问法不同（照着用法行把位置参数原样抄回来，真没有才回空），但
把关的规则一模一样：名字必须逐字出现在本段原文，编出来的照样被拦并入表。

**每一批参数只按它自己那批判。** 一段里的参数多时会分几批提取，每批只问其中一部分，
而它的回答要在两个方向上都对得上那一批：不在这一批里的参数即便逐字对得上原文也要丢掉
（这一段的参数全写在同一段原文里，光靠接地校验分不出它属于哪一批），这一批里
问了却没回来的参数则记在这条命令上。两者都在审阅面板里挨着这条命令显示。后者同时是
保留率诚实的前提：分母是「这一轮问了多少」而不是「模型愿意答多少」，所以二十个参数只
答一个是 5% 而不是 100%。一批参数覆盖率不到一半时会拆成两半重问一次——与回答过长走的
是同一个补救，因为是同一个毛病；而回答条数与所问一样多、只是答错了的那种不重问，问得
更少也救不了它。

**跨段合并：一条命令一行。** 参数表跨过段边界，或后文（SEE ALSO、示例章）再次出现同名
命令时，同一条命令会在好几段里各产出一条结果。目录里仍然只有一行：后面的段把结果并回
前面那一段已经写下的行——参数按名去重、**先写的赢**，`syntax` 与说明只补空，出处元素在
上限内取并集，原文摘录保留**第一段**的（那是这条命令被介绍的地方，也是有用的那一处）。
唯一的例外是那一行已经被人动过：审阅者已经确认或跳过的行**不会被改写**，这一段的参数改为
**追加一行**——一条审阅者看得见、可以自己跳过的重复行，远好过一次静默丢失（这一段找到的
参数在别处并不存在）。

**熔断。** 处理满十段**实际发过调用**的段之后（被跳过的段不进这个样本），命令名整条否决率
超过 20%、参数保留率低于 50%、或「完全没给出可用回答的分片占比超过 20%」，任一成立就直接
判失败并给出用户可读的理由，而不是带着一份看起来合理的近空目录收工。三根轴缺一不可，因为
它们互相看不见：一条结果完全可以选对命令名、同时把参数全部编造出来；而一个什么都不返回的
模型要按「它没在回答」如实报出来，而不是报成「参数丢了」这个症状——那一轮会付完整本手册的
钱，最后报成功、目录是空的。合法的 `entries: []` **不算**没给出可用回答，只有回答本身不可
解析才算。瞬态的模型服务错误（限流、上游报错）不算抽取结果：它直接判任务失败，绝不会被记成
「这一段本来就没有命令」。

**一次调用都没产出东西，不算成功。** 这一轮**发过**至少一次模型调用、模型连一条条目都没
尝试过、候选行与被拦行又都是零时，任务落 `failed` 并给出用户可读的理由（「没有识别出任何
命令；这份来源可能不是命令手册，或命令的写法与识别规则不符」）。其余每一种「看起来空」的
结果仍然是成功：有被拦行的那一轮已经把「为什么什么都没留下」摆给了用户；每一段都被跳过的
那一轮压根没调用过模型（一份本来就不是手册的来源，正确地一分钱没花）。这是对一轮**已经跑完**
的任务的判决，所以刻意不做成第四根熔断轴——熔断是按比率中途叫停，它要等最后一段跑完才算得出来。

**模型自撰的字段有上界，也有标注。** 说明、示例和每个参数自己的说明是接地校验刻意不查的
三个字段（散文无法逐字比对），所以都在候选落库前截断：每个字段各有上界，参数说明还额外
有整行的总量上界，被总量截掉的条数与其他拦截原因一起如实报出。示例在审阅面板里显示，并
附一句「示例为模型生成，未经原文校验」。

**成本预告。** `.../command-catalog/preview` **零模型调用**给出两个数，各自的有界方式不同。
**多少段**光靠算术答不了：`⌈全文字符 ÷ WINDOW_CHARS⌉` 会低报两次——元素整个放进一段，
放不下时留在那段里的预算空隙没人花（三个 7,000 字符的元素是 3 段，算术说 2 段），而候选
过密的段还会再被拆开。两者都只会让段数**更多**，所以算术是**下界**不是计数。因此：有界前缀
恰好覆盖了全文时（`sampled=false`），`estimated_windows` 取前缀真跑分段得到的段数，是
**精确值**，而且不额外花钱（前缀本来就要读来估调用数）；前缀到顶时取几个**下界**里的最紧者，
并作为**显式下界**报出（界面文案写「至少约 N 段」）。为了数准而把要估算的那次全文扫描先做
一遍，正是成本预告不该有的失败。

下界成立有两个不显然的条件。其一，**字符数必须按分窗自己的归一化来数**：每个元素在打包前
都会去掉首尾空白，所以按原始 `LENGTH` 求和描述的是一份分窗永远看不到的文档——2001 个「1 个
字符 + 20 个尾随空格」的元素原始 42021 字符（算术说 4 段），实际只有 1 段，一个高于真值的
「下界」比没有下界更糟。两条 SQL（全文聚合与前缀行）因此都按 `TRIM` 后的长度数；元素之间的
连接符刻意不计——那只会让总数更小，方向是安全的。**两侧剥的必须是同一批字符**：SQL 的
`TRIM`/`BTRIM` 只剥空格、制表、换行、回车四个 ASCII 字符，所以分窗侧也钉死在这四个，而不用
会剥掉全部 Unicode 空白的 `str.strip()`——否则一份用 U+3000（中文排版里到处都是的全角空格）
或 NBSP 填充的文档，真实字符数比 SQL 报的少，算术下界又会跑到真值之上。代价是这类空白按
内容计、占段预算，方向保守（只会让估算更小）。切点回退用的是 Unicode 宽的空白判定，那是在
选**在哪儿切**而不是在数**有多少**，不受这条约束。其二，**前缀分出的段数不能直接加上余量**：
分窗只有在下一个元素装不下时才封窗，所以前缀的**最后一段还开着**，没读到的元素会继续往里
填而不是另起一段（4 个短元素、上限 3 条，实际 1 段，直接相加会报 2 段）。所以只把已封的段
算作确定，最后那段的字符退回池子里与余量一起算；余量则用每行**自己的完整长度**从全文总量里
减（不能用传回来的裁剪后文本减，否则每个被裁元素的尾巴会留在余量里被重复计一次）。

**多少次调用**同样没法靠算术：它取决于跳过闸（散文段免费）和每段的
参数条数（上百个 flag 的段要分好几片），两者都得读正文，所以只在有界前缀里精确测量（连接力
一起推进，否则前缀里的闸会和真跑时答得不一样）。**前缀之外的段一律不报价**：此前按每段 1 次
计，那个数同时往两个方向错——跳过闸让纯叙述的段免费（一本以叙述为主的书被收了它根本不会发生
的调用费），而参数密集的段一段要好几片（手册被严重低估）；何况它描述的是这次预告没读过的
文本，却与旁边「实际可能更多」的措辞自相矛盾。所以 `estimated_calls` 只覆盖已读的那段前缀，
`windows_in_prefix` 说明那是多少段，界面据此说「按开头 X 段估算需约 M 次，其余段落视内容
而定」，不替没读过的段落编次数。**这个 X 只数已封的段**：分段只在下一个元素装不下时才封窗，所以被测前缀的**最后一段还开着**——没读到的元素会继续往里填，一个元素就可能把纯叙述段变成命令段、或把它的 flag 清单顶过分片上限而多出一次调用。给一个还会变的段报价，等于让「按开头X 段」变成对没读完的文本的断言，与「前缀之外不报价」是同一条理由。所以计价与 X 都只算已封段，末段的字符退回池子与余量同算（与段数下界共用同一个「哪段还开着」的判定，不写两份）。前缀只packs 出一段时 X 就是 0，界面退化为只给段数下界。**被测前缀在第一个被裁的元素处就停。** 有界读取按元素裁剪
之后仍会继续返回后面元素的头，把这批行一起分段等于把「被裁掉的尾巴之后」的内容**提前拼到
前面**——量出来的是文档里根本不存在的拼接，却挂着「开头 X 段」的名义（一个 11,990 字符的散文
元素后面跟一条命令：真实是两段、而且第一段是免费的纯叙述，拼接后却成了「第一段就要一次
调用」）。判据是该元素 strip 后全长大于传回来的文本长度，也就是**内容**被截断——只截掉尾随
空白的不算，那种元素一个字都没丢。从那一行起（含它自己）全部不进分段测量，只进算术余量。
首个元素就超长时一段都没量到，`windows_in_prefix` 与 `estimated_calls` 都是 0，界面此时只给
段数下界并说「调用次数视内容而定」，绝不渲染成「按开头 0 段估算需 0 次调用」——那会把「没测」
说成「测出来是零」。`sampled=true`
表示前缀到顶了、这些数字是下界；两条边界都会置位它，其中**逐条截断**才是真正会扭曲估算的
那条——把一张参数表截短就少了参数名、少了分片。条数那条边界按**元素总数是否大于读回的行数**
判，不是「读回行数是否已达上限」：整篇恰好等于上限的文档是被完整读过的，把它判成采样等于
在唯一一份估算完全准确的文档上，把精确值降级成下界、还让界面写「至少约」。这个比较能成立
的前提是两个数出自**同一代次**——两条读取是两条独立语句，中间落进一次重解析就会把一个代次
的字符总量配上另一个代次的前缀，结果不像错的、只是描述了一份不存在的文档。所以预告在两条
读取前后各取一次来源代次（与确认路径同一份 `MAX(source_elements.created_at)` 实现），不一致
就**整对重读**一次（只重读代次没有意义：那只是确认了漂移，然后照样报混代次的数）；仍不一致
就返回带用户可读文案的 `409`（「正在重新解析，请稍后」——重解析正在跑，多读几次也赢不了这场
竞速）。`skipped_windows_in_prefix` 是前缀内被跳过
的段数，它是「为什么调用数远小于段数」唯一的解释项，少了它，一份大多是散文的手册看到
「约 40 段 / 约 3 次调用」会读成漏算。v1 的 `signal`（形状检测）、`is_manual` 与
`estimated_sections` 随规则分节一起退役，刻意**不**留兼容别名：v2 没有「命令节」这个东西
可数，留一个恒为 0 的字段比删掉更容易被读成「这份文档没有命令」。

**进度的两个字段名没改，语义改了。** `sections_total`/`sections_done` 现在计的是**段**数
（含被跳过的段），数据库列名保留——改名要加迁移、还会断掉既有观测面，两样都换不来任何东西。
`truncated_sections` 已从传输层删除：v2 没有截断这回事（超长元素被切成连续几段落进相邻的段），
一个恒为 0 的字段只会让界面一直渲染一句永远不会发生的告警。

**确认与合并。** 候选在人工确认前都是未生效的。**识别还没跑完时不能确认也不能跳过**——
两个端点对非终态任务（`queued`/`running`）返回带用户可读文案的 `409`，审阅面板本来也只在
终态才开审阅入口。这不是保守，是堵一条会造出**永远确认不了的候选**的路：识别中途确认某条
命令后，后面的段把这条命令的续表参数合并写回时发现该行已确认，就按既定的降级路径追加一条
同名新候选（那是刻意的，参数看得见比丢掉好）；可确认这条替补行时目标表里已经有同名的行，
按「同名一律不改行、只回报 conflict」的合并语义会被跳过——迟到发现的参数从此可见而不可落库。
闸放在 API 边界，服务层的锁与降级路径原样保留为纵深防御（用户先取消、恰好与最后一段的写回
交错这类合法竞态仍会走到它）。确认时若不存在则创建名为
「命令目录：<来源标题>」的 Knowhow 表（固定六列：命令 / 语法 / 参数 / 说明 / 示例 /
出处，其中「命令」是行标题列），已存在则**只新增表里没有的命令**。`<来源标题>` 与来源在
产品其他各处（引用卡、证据卡、清单卡）显示的名字是同一个：来源已接地判定为论文且解析出
非空 `paper_title` 时用论文标题，否则用普通来源名/上传文件名——同一份手册不会在自己的
目录表上叫另一个名字。这个标题只在**首次**为某个任务创建目标表那一刻解析一次；此后该
任务的确认都按它记住的 `applied_table_id` 继续写回同一张表，哪怕后续论文元数据补抽让
标题变了，也不会改名或劈出第二张表。重新发起识别产生的新任务（自己的
`applied_table_id` 起初为空）同样会写回这张表——继承该来源最近一次任务确认写入的目标；
只有这个继承目标已被删除时，才会改按派生标题新建或查找。候选的命令在表里
已有对应行时，会原样回报进 `conflicts`，那一行**不动**——v1 刻意绝不覆盖用户可能已经
手工订正过的内容，完整的 diff/merge 属于后续任务。列按**名字**寻址，所以之后编辑目标表的
列不会把内容悄悄挪进错误的一列；表若已丢掉「命令」列，则拒绝写入而不是硬写。一次
`all_pending` 最多确认一页，剩余数量由 `pending_remaining` 如实回报。落库全部走 Knowhow
既有服务层，所以表的变更历史会像记录任何一次普通编辑那样记录它们。

「出处」列写的是来源名，后面接原文里的标题路径。某一段没有可继承的标题路径时，出处**只写
来源名**：候选行上那个内部序号标签（审阅面板会把它显示成「第 N 段」）指的是字符预算切出来的
一条边界，它落进知识表就会被人长期保存、几个月后再读到、还会出现在图谱里，而在那个语境里
它什么也没说。真正的标题路径（`Global Placement > Commands`）照常保留——那一条确实说清了
这条命令记在文档的哪个位置。

每一条退出路径都会把任务行落成终态，`Ctrl-C`/`SIGTERM` 也不例外：留在
`queued`/`running` 的行会一直占住这个来源的守卫直到后端重启，进程自己兜不住的那部分
由启动兜底收尾。

**上一轮还有候选没审阅完时，重新发起识别会被拦住，跳过是唯一的显式放弃出口。**
`.../job` 只会返回一个来源最近一次任务，若这时又发起新一轮，上一轮的候选就再也够不着
了——永远留在 `candidate` 态、却没有任何页面能重新打开它。前端与 `.../command-catalog`
自己的 `409` 都会拦住这种情况（覆盖上一轮可能到达的任意终态：成功、失败或取消），
拦截提示写的就是「确认或跳过」。`apply` 只在候选与目标表冲突时才会把它挪出 `candidate`
态而不落库；一条审阅者单纯不想要、又不冲突的候选此前没有任何路可走。
`.../command-catalog/dismiss` 就是这条路——审阅面板里「跳过所选」/「跳过全部待审阅」
两个动作，选择契约、并发锁（每个笔记本一把的目录锁）与 `catalog:write` 权限（P2 起 owner∪组管理员）都镜像 `apply`，
只是完全不碰 Knowhow 表。

**来源被重新解析之后，这一轮的结果就作废了。** 每个任务在创建时记下当时的**来源代次**
（该来源全部元素共有的那一个落库时刻，重新解析会整批换掉它们），`apply` / `dismiss`
在动手之前先比一次：对不上就返回带用户可读文案的 `409`，并在同一次调用里把这个任务
剩下的候选整批标成 `dismissed`（原因 `source_reparsed`）。候选里的命令名、原文摘录与
出处指向的全是抽取时读到的那一代元素，原样确认进知识表写下去的就是文档里已经不存在的
内容。整批作废不是附赠的：上面那条「待审阅候选拦重跑」的守卫会读同一批候选，不清掉它们
就会两头卡死——每次确认因为过期被拒，每次重跑又因为还有未审候选被拒。同一条判据也让
重新发起识别在来源已重新解析时直接放行（顺手清掉旧候选），因为重新解析恰恰就是用户想
重跑的原因。代次刻意**不**取 `sources.updated_at`：那是有意做粗的变更信号，每次状态迁移
（重新抽取知识图谱、写摘要）都会推进而元素纹丝不动，按它判会在一次普通的重新抽取之后
谎报「来源已重新解析」，并逼用户重跑一整轮付费识别。

端点（都作用在路径里那个笔记本自己的来源上；读取需要笔记本读权限，写入需要 owner）：

- `GET  /api/notebooks/{id}/sources/{sid}/command-catalog/preview` —— 成本预告：`estimated_windows`（前缀覆盖全文时是真跑分段的精确值，`sampled=true` 时是显式下界）、`estimated_calls`（**只覆盖已读前缀**，其余段落不报价）、`windows_in_prefix`（这次计价覆盖了多少段）、`skipped_windows_in_prefix`，有界读取触顶时带 `sampled`（按元素总数是否超过读回行数判，不是「行数是否达上限」）；来源尚未解析完或解析失败时返回带用户可读文案的 `409`，两条读取跨了一次重解析（重读一次仍漂移）时是另一条 `409`
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog` —— 发起抽取；该来源已有活跃任务（既有任务从 `.../job` 取）、抽取模型未配置、来源尚未解析完或解析失败、或上一轮还有候选没审阅完（需先确认或跳过）时返回带用户可读文案的 `409`。上一轮的候选若已因来源被重新解析而过期，则不拦，改为整批清掉再放行
- `GET  /api/notebooks/{id}/sources/{sid}/command-catalog/job` —— 该来源最近一次任务：`status` 与 `progress`（`sections_total`、`sections_done`——两个列名保留、计的是段数，`entries`、`rejected`、`uncovered`、`pending_candidates`），失败时带 `failure_reason`。内部诊断列 `diagnostic` 刻意不进响应
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog/cancel` —— `cancelling`（worker 会在下一个分片边界停，飞行中的一次模型调用被取消时也会停，不必等到那次调用返回）、`cancelled`（本进程没有在跑它，直接落终态）或 `not_running`
- `GET  /api/notebooks/{id}/sources/{sid}/command-catalog/candidates` —— keyset 分页（`job_id?`、`state=candidate|rejected|applied|dismissed`、`cursor`、`limit`）并带各档 `counts`。`next_cursor` 是上一页最后一条的 `position` 而不是 offset：确认候选会改 `state`，offset 分页会漏行/重行。`dismissed` 候选带 `dismiss_reason`：`conflict_existing_row`（apply 发现已有同名行）、`user_dismissed`（人工显式跳过）或 `source_reparsed`（来源已重新解析，这一轮结果整批过期）
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog/apply` —— 任务未落终态（`queued`/`running`）时返回带用户可读文案的 `409`（理由见上文「确认与合并」）。body 为 `{candidate_ids}` **二选一** `{all_pending: true}`——两者同时非空/为真会返回带用户可读文案的 `422`（此前会静默偏向 `all_pending`，比调用方明确写出的 `candidate_ids` 更宽的一次写，调用方看不出自己传的选择被悄悄吞掉）；返回 `table_id`、`created`、`applied`、`rows_added`、`conflicts` 与 `pending_remaining`（一次调用最多确认一页）。来源在这一轮之后被重新解析时返回带用户可读文案的 `409`，并整批作废该任务剩余候选；重新解析**正在进行中**（还没换掉元素）时是另一条 `409`，措辞不同且**不作废任何候选**——解析可能在换元素之前就失败，那批候选仍然有效
- `POST /api/notebooks/{id}/sources/{sid}/command-catalog/dismiss` —— 任务未落终态时同样 `409`（文案按「跳过」这个动词写）。body 为 `{candidate_ids}` **二选一** `{all_pending: true}`，选择契约（含两者同传时的 `422`）与单页上限同 `apply`；把选中的 `candidate` 态候选标记为 `dismissed`（原因 `user_dismissed`），不碰任何 Knowhow 表；返回 `dismissed`（真正被标记的 id）与 `pending_remaining`。来源被重新解析、或重新解析正在进行中时，两条 `409` 都同 `apply`

数值上限（唯一登记处；代码里各有同名常量）：

| 项 | 常量 | 值 |
|----|------|----|
| 每段字符预算 | `WINDOW_CHARS` | 12,000 |
| 每段候选名单上限 | `MAX_CANDIDATES` | 32（v1 为 16；接力名单共用同一上限） |
| 拆段递归下限 | `WINDOW_SPLIT_FLOOR_CHARS` | 750（`WINDOW_CHARS / 16`；到此仍超限即截断并披露） |
| 切点回退安全边界 | `SPLIT_BOUNDARY_LOOKBACK_CHARS` | 200（向前回退找空白/换行；找不到就按原位切） |
| 每个分片的参数条数 | `SLICE_PARAM_LIMIT` | 20 |
| 单个分片最多模型调用 | `MAX_CALLS_PER_SLICE` | 11（含两条补救二分，`1 + 2·(1 + 2·2)`） |
| 每行拦截记录上限 | `MAX_WINDOW_REJECTIONS` | 24（溢出计数，绝不静默丢） |
| 每行出处元素上限 | `MAX_ANCHOR_ELEMENTS` | 12（跨段取并集时的上界） |
| 熔断样本下限 | `MIN_WINDOWS_BEFORE_ALERT` | 10 个**实际发过调用**的段 |
| 熔断三轴阈值 | `COMMAND_REJECT_ALERT_RATIO` / `ARGS_KEEP_ALERT_RATIO` / `SLICE_FAILURE_ALERT_RATIO` | >20% / <50% / >20% |
| 成本预告有界前缀 | `PREVIEW_ELEMENT_LIMIT` / `PREVIEW_ELEMENT_CHARS` | 2,000 条元素 / 每条 1,200 字符 |
| 模型自撰字段 | `MODEL_DESCRIPTION_CHARS` / `MODEL_EXAMPLE_CHARS` / `MAX_MODEL_EXAMPLES` | 1,000 / 500 / 8 |
| 参数说明双层上界 | `MODEL_ARG_DESC_CHARS` / `MODEL_ARG_DESC_TOTAL_CHARS` | 每条 400 / 每行 8,000 |

已退役：v1 的分片视图上限 4,000 字符与开头摘录 600 字符（`MAX_SLICE_WINDOW_CHARS` /
`OVERVIEW_HEAD_CHARS`）——分片视图现在恒为整段全文，见上文「抽取」。

## 检索模式（问答）

`POST /ask` 按 `mode` 分派——注册表 `backend/app/services/ask_modes.py` 是唯一真源（默认 `chunk`）。联合范围按路径区分：`chunk` 基线 active-only；可选 KG overlay / PPR 可加入 federated KG 与 base-backed chunk；`graph` / `reasoning` 走 federated KG。`federated_retrieve()` 的知识对象命中不改 score，只在完全平局时以 `base` 为第二排序键；`federated_retrieve_relations()` 的关系命中仍只按 score 排序。这些排序信号不进入接地阈值。

流式 Ask 只在持久答案与浏览器 final 事件之后运行完成后 observer。该 point 的协作式墙钟预算来自 `ASK_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS`（默认 `30`，有效范围 `>0..300` 秒）：已经进入同步调用的 callback 可安全完成，但 deadline 到达后 host 不再启动后续 contribution。这只是部署/内部扩展护栏，不改变检索、答案正文、引用或用户已收到的 final 事件。

Deep Report 完成后处理使用独立部署护栏：`REPORT_POST_COMPLETION_EXTENSION_TIMEOUT_SECONDS` 默认 `30`、有效范围 `>0..300` 秒，deadline 是协作式的。只有报告行从 `generating` 原子提交到 `done` 且全部生成执行上下文释放后才运行该 point；它不能改变章节正文、引用、参考文献、检索输出或终态。

| 模式 | 分组 | 需 KG | 一句话 |
|------|------|-------|--------|
| **`chunk`**（默认） | general | 否 | chunk-native 通用问答：大召回 → 选择 → 长上下文综合 → 引用绑回源 chunk。 |
| **`graph`** | strict | 是 | 对跨文档知识图谱做单趟个性化 PageRank（PPR）传播。 |
| **`reasoning`** | strict | 是 | agentic 迭代 plan → retrieve → reflect → answer（流式输出实时轨迹）。 |

### 可选生成问题召回补充

`GENERATED_QUESTION_INDEX_MODE` 是部署级 rollout，取值为 `off`（默认）、`shadow` 或 `on`，不是用户检索范围开关。运维人员先执行 `scripts/batch_ingest.py question-index --notebook-id ...`；每个通过校验的生成问题独立落一行向量，但只寻址一个不可变原 chunk。生成文本绝不作为 evidence、绝不进入引用，也不返回浏览器。重解析/删除通过外键级联清理；notebook 深拷贝和 SQLite→PostgreSQL 迁移会随原 chunk 重映射或保真复制。

在线路径仅在 chunk 基线命中少于 `GENERATED_QUESTION_TRIGGER_HITS` 时运行（默认 `5`，最小 `1`）。它最多读取 `GENERATED_QUESTION_MAX_SCAN_ROWS + 1` 行（扫描上限默认 `10,000`，最小 `1`）；超过上限就跳过补充，绝不无界扫描。排序最多覆盖 `GENERATED_QUESTION_RECALL × GENERATED_QUESTION_QUESTIONS_PER_CHUNK` 个问题行，随后最多保留 `GENERATED_QUESTION_RECALL` 个原 chunk（默认分别为 `40` 与 `3`；recall 最小 `1`，每 chunk 问题数范围 `1..8`）。冻结 source ceiling 与 retrieval-run actor 的私有 Memory 谓词都会在 SQLite/PostgreSQL 的 `LIMIT` 前执行，因此被排除来源或其他成员的 Memory 问题既不能占用 scan cap，也不能影响排序；孤儿 Memory projection fail closed，可见来源与 notebook-wide Knowhow 仍可进入。`shadow` 经共享 candidate contributor 执行对比并发出只含计数的内部 telemetry，但返回完全相同的 baseline tuple；只有 `on` 才能在 MMR/fusion 前、基线后追加命中的原 chunk，不能驱逐或重排基线命中。`off` 不读表，也不新增 query embedding 调用。

离线构建要求 chat workload `chunk_question_generation` 与 embedding workload `chunk_embedding`。每个 chunk 的完成时间会把模型成功返回空列表也记成已处理，保证幂等；单 chunk 失败不盖完成标记，后续可重试。`--force` 明确表示重生成已完成 chunk。首版使用有界矩阵扫描而非独立 ANN 工件，因此超过扫描上限的大库保持纯 baseline；rollout 必须先用 `shadow` 的无正文命中/新增/跳过计数做 A/B，再考虑切 `on`。

### id 与显示名

上表的 id（`chunk` / `reasoning` / `graph`，以及分组 id `general` / `strict`）是**协议**：`POST /ask` 收的是它，历史会话与书签存的是它，后端注册表 `backend/app/services/ask_modes.py` 声明的也是它。它们是稳定量，不因为「名字不好听」而改。

界面上**显示**的名字是另一层，纯 UI，归前端注册表 `frontend/app/ask-modes.ts` 所有：

| 协议 id | 问答面板显示名 |
|---|---|
| `chunk` | 通用问答 |
| 分组 `strict`（选择器给出的入口，组内默认引擎是 `reasoning`） | 深入分析 |
| `reasoning` | 逐步推理 |
| `graph` | 关联追溯 |

该注册表的 `groupLabel()` / `modeLabel()` 是唯一读取口：前端任何其它文件都不得硬编码显示名，散文里提到就用模板插值。两边由 `ask-modes.test.mjs` 强制——它递归扫描 `frontend/app`，当前显示名出现在注册表之外即失败，退休名（严格推理 / 深挖推理 / 图谱多跳）复活也失败。因此改显示名只是改注册表一行，不动任何 id、请求/响应载荷或已存会话；id 集合另由 `scripts/check_ask_modes_contract.py` 跨前后端锁同步。

**`chunk` —— chunk-native，含可选 chunk×graph mix。**
- *基线：* chunk 大召回（`CHUNK_RECALL`）→ MMR / 多子查询配额多样性选择（`CHUNK_MMR_K`）→ 长上下文综合，不碰 KG。
- *mix*（仅当 `CHUNK_KG_OVERLAY_ENABLED=true` **且** 配齐 qwen3-rerank **且** 有 KG 时生效）：三路并池——(a) 向量 chunk、(b) query 种子周围的 KG 局部结构（实体 + 其 1-hop 关系，只检索一次）、(c) 这些 KG 对象背后的源 chunk——round-robin 合并 → qwen3 cross-encoder rerank → 按 token 预算装填（`MAX_ENTITY_TOKENS` / `MAX_RELATION_TOKENS` / `MAX_TOTAL_TOKENS`）。每个候选会累积 producer support，不能从融合分反推来源。最终选择可通过 `CHUNK_GRAPH_RESERVE`（默认 0/关闭）为已经越过原相关度门槛的 graph-only chunk 预留席位，并通过 `EXACT_SECTION_RESERVE`（默认 4）为下面的精确标识符通道预留席位；两者都绝不扩大 item/token 预算，也不改变 oversized-first-chunk 例外，更不会挤掉对方已保住的块。答案在同一套 `[k]` 映射里同时引用 chunk 与 KG 项，接地跨 chunk ∪ KG。未配 rerank 或无 KG 时**字节等价回退**到基线。（忠实 LightRAG 的 `mix` 模式。）

**精确标识符通道（`EXACT_LOOKUP_ENABLED`，默认开）。** 有一类问题靠调排序治不好：它其实是「这是哪一节」的定位问题。手册里 `set_db` 的小节被分块切成主描述 / 参数表 / 示例，相关性打分只会留下其中最强的一块，答案就缺参数细节。因此问题里点到可精确查找的名称时，检索先多跑一条零模型通道——精确子串匹配定位小节，再把该节（含其子节）整体取齐——并按各分支既有口径并入候选。这把闸刻意比词法层的标识符抽取更窄：带 `_` 或 `.` 的名称（`set_db`、`config.yaml`）一律放行，而只用连字符连接的词必须带数字，于是型号/版本名（`GPT-4`、`v1-2`）留下，普通英文词组（`state-of-the-art`、`real-time`、`end-to-end`）被拦住。拦它们的理由很实在：这类词几乎出现在每个分析型问题里——深度报告每一节的问题差不多都含一个——而每个词都要付一次真探测，一旦命中章节标题还会把整章推进证据预算。它们仍留在词法召回里，那边多一个词项几乎不花钱。命中随后先折叠到它所属的那条命令，再分配小节名额，因此一条命令的参数表/示例子节不会把第二条被点名命令的名额吃光。这些块按 keyword-only 计分——算的是「对**名称**的覆盖率」而不是「对整句问题的相关度」，于是两个调用方对同一块证据给出同一个数——带普通 `lexical` 来源标注，并在 mix 最终选择中占 `EXACT_SECTION_RESERVE` 个席位，避免 rerank 又把参数表截掉。所有查询都有硬界（`EXACT_LOOKUP_MAX_IDENTIFIERS` / `EXACT_LOOKUP_FTS_K` / `EXACT_LOOKUP_MAX_SECTIONS` / `EXACT_LOOKUP_MAX_CHUNKS_PER_SECTION`）；问题里没有这类名称时不会多发任何查询，那些提问完全不受影响。只作用于当前笔记本，挂载的参考库刻意不在范围内。**标题面包屑是 Markdown 解析路径才有的东西**，所以「整节取齐」只对那样解析的来源成立；MinerU 解析的 PDF/DOCX 没有面包屑，通道在那里退回到「精确返回命中的那些块」，仍然能救回普通排序会丢掉的参数表。

**`graph` —— 跨文档 KG 上的 PPR。** 经 `federated_retrieve` 取种子（KG 实体 + 其源 chunk；`RELATION_RETRIEVAL_ENABLED=true` 时再融合关系索引命中）作为 HippoRAG 式**个性化 PageRank**（`GRAPH_PPR_ENABLED`，默认开）的个性化向量，通过共享知识图谱把相关度跨文档传播；排名靠前的 chunk 喂出接地答案，`[k]` 锚点指向 KG 对象/关系。`GRAPH_PPR_ENABLED=false` 时回退为沿推理边的有界 BFS。

**`reasoning` —— 意图优先的 agentic 深挖检索。** 正式界面先调用 `/ask/intent`；这一步最多使用当前会话最近五个用户问题，不使用语料派生的助手回答，也不读取语料或创建持久 conversation/job。清晰请求自动确认，阻断性歧义以内联审阅等待补充。`/ask` 与 `/ask/stream` 在原始 `question` 之外接收已审阅的 `intent`；后端确定性冻结合同，形成唯一内部研究问题，供 Memory 检索、PPR、证据检索与答案合成共用。确认后的检索方向直接成为首轮子查询，不再执行旧的第二次问题规划；反思阶段可以按证据增加查询，但不能替换合同。超出该档位首轮宽度的方向不会被丢弃：一段确定性补种在 PPR / 精确查找 seed pass 之后、反思循环之前按合同顺序执行它们，最多占用共享推理步骤预算的一半，每条执行过的方向都产生一条普通的 `retrieve` 轨迹步。一份 run 内的注册表把每个已审阅方向映射到唯一简称(默认截断点撞出同一简称时依次加宽,仍撞则追加序号后缀),并可反查回方向原文——模型自始至终只看得到简称、也只会用简称重提;`add_subquery` 提交经这份注册表解析后,命中尚未执行的方向会按方向自己的完整契约原文执行(账目记在方向身份上,不是裸简称),命中已经执行过的方向(无论是补种执行的还是此前某轮 `add_subquery` 补上的)则判定为重复、不重跑检索。每一轮反思提示都会看到当前仍未覆盖的方向,好让模型优先把自己的预算花在这些方向上;等反思循环结束,才会按这份终态把仍未覆盖的方向记成一条 `skip` 步——不是按预算刚耗尽那一刻的旧账,若反思期间已经全部补齐则不落这一步。响应持久化确认后的 `intent`、暴露内部 `retrieval_query`，并在正式检索前以 `intent` 作为首个引擎轨迹步骤；会话里保存/显示的仍是原始问题。轨迹覆盖整轮而不只是检索段：问题理解跑在持久 job 之前，界面自行合成理解阶段的前几步（理解中 → 已理解或待澄清 → 已确认）拼在后端步骤之前，不再为它另设轨迹之外的提示条；该阶段的客户端墙钟以可选且有上限的 `intent.understanding_ms` 回传（绝不参与检索），成为持久 `intent` 步的 `duration_ms`。后端在 Memory 检索之前推送 `intent` 步；命中私有记忆时记 `memory` 步，它记录的是**召回**而非归因（归因由答案里的 `[k]` 引用承担），零命中则记一条带耗时的 `skip` 步，让候选查询与 embedding 调用的耗时留在总耗时里。答案生成之后记 `synthesis` 步——那次生成通常是整轮最长的一段，既要可见也要计入轨迹总耗时；它的引用数取绑定锚点，不取检索到的证据卡数。未携带 `intent` 的直接兼容调用保留清晰问题的旧路径，但遇到确定性无法解析的指代或纯泛化请求会 fail closed。随后委托 `ReasoningRetriever` 检索（与 `graph` 同样走 PPR 传播）、反思是否充分，按需扩图/加子查询直到能回答，并经 NDJSON stream（`/ask/stream`）输出 `reasoning_trace`。遇到显式推导问题时可调用 `follow_chain`：通过两轮有界邻接抽样复用既有 source/target 索引，再确定性检查类型、状态、审核、evidence 与 `validity_scope`；两条存储关系作为可引用前提，`A→C` 只作为带「推断」标记的查询期结论。高度节点抽样被截断且无法证明不存在直接边时，宁可不推。上面的精确标识符通道以两种方式接进这个循环，两者都零模型调用，且覆盖 `reasoning` 问答与每一节深度报告检索（报告引擎逐字复用 `ReasoningRetriever`）；`graph` 模式不接，knowhow 智能补全按策略位主动关闭它（补全的查询是 JSON 信封而非问题，否则会在每次请求上探测信封自身的字段名）。权威问题本身点名了标识符时，初检索之后无条件跑一次确定性 seed pass（记 `exact_lookup` 轨迹步，界面显示「精查」），打分用的是它实际探测到的名称本身而非整句问题——把精确命中拿去和长问题里一堆无关词打分会把它的相关度拖低到丢字符预算、甚至拖过接地判定阈值；反思模型也可以在某个被点名命令的完整定义仍未覆盖时，主动选择 `exact_lookup` 动作并给出 `exact_term`，打分方式相同。该动作与 seed pass 共用同一把名称形状闸——低选择度的任意短串会被拒绝，而不是变成全库子串扫描——每个名称一次 run 内只探测一次（seed pass 共用同一份账目），agent 主动调用每 run 至多 3 次，被跳过、重复或零收益的尝试都会带着模型能据此调整的理由回喂给下一轮反思（按理由/名称去重，同一个非法输入不会让账目无限增长）。问题里没有标识符时不发任何调用，也不多出轨迹步。严格 / KG 接地。

#### 来源范围只由用户勾选决定

检索的来源范围**只**来自[按来源选择检索范围](#按来源选择检索范围)那组可见来源复选框；模型不参与判断问题点名了哪个来源。语料盲意图规划器不输出任何来源身份，也不存在来源确认步骤：一次逐步推理从开始到结束都继承请求携带的复选框上限，run 内任何动作都改不动它。

早先的版本允许意图规划器输出 `source_refs`，由 `/ask/intent` 在有界、纯身份的来源目录里按稳定 id、显示标题或原始文件名做规范化**精确等值**匹配，配一道 `source_scope_confirmation` 审阅闸与签名预检能力，并提供 `search_evidence(query, source_refs?)` 动作在已确认 run 内继续收窄。这整套合同已移除。精确等值兑现不了简称——问「pdagent」而来源标题是「PDAGENT-BENCH: Characterizing, Grounding, and Architecting LLM/VLM Agents for VLSI Physical Design」时解析结果为零匹配——而该设计又是 fail closed 的，于是一句普通问题会以确定性 422 失败、重试无效。既然来源勾选本就在用户手里，模型再猜一遍只增加失败模式，不增加能力。

#### 模型 JSON 恢复与流保活

`reasoning_agent` 决策与 `ask_answer` 合成始终先走严格 JSON 解析。严格解析失败后，共享修复层只允许接收对象首尾完整、且仅有可恢复语法错误（如缺引号/逗号）的响应。修复结果不得超出 schema example 的顶层字段，布尔字段必须是真正的 JSON boolean，枚举样例值必须留在词表内，拒绝非有限数；每个非空字符串值还必须逐字存在于原始响应中。截断对象、数组/标量、未知字段、类型混淆和字符串重构仍视为畸形响应。检索器异常会降级成可持久化的终态 Ask 回答，不再因为不存在的 result 中止编排。

`/ask/stream` 的交付队列空闲时每 **5 秒**发送一条不含业务内容的空白 NDJSON 行，并关闭常见代理缓冲；既有客户端会忽略空行。这样慢反思或慢合成期间 transport 仍有字节流动，但不会伪造推理步骤；断连仍只停止该客户端接收，detached job 继续运行。心跳只处理 idle timeout——ingress/CDN 若配置总请求时长硬上限，仍需由部署者调整。

### 逐步推理档位与完整集合请求

档位在提问框里通过与深度报告「研究深度」**同一个**档位控件选择——共用一个组件，两处不会走样：一个带当前档名的 chip，点开是滑块弹层，显示该档档名与一句中性说明。界面只呈现档名与那句说明；精确上限在下面这张表（由 `frontend/app/ask-retrieval-effort.ts` 与 `backend/app/core/ask_retrieval_policy.py` 双向锁定），不铺在控件上。`answer_element_items` 与下表末三列的 `enum_*` 是这个镜像关系的例外——它们都是后端专有字段，前端没有对应消费者，只影响服务端最终合成 prompt 的组装与[集合枚举工具](#集合枚举工具)的预算。

逐步推理接受下表五个稳定的 `retrieval_effort` 协议 id，默认 `standard`。证据充分时模型可以提前停止，但不能突破任一上限。“最终 floor / aspect / cap”的计算是 `min(cap, max(floor, aspect × 实际执行查询数))`。KG / 原文上下文是真正的证据字符硬上限：原文分区包含结构化预览、chunk 与直接来源元素；KG 分区包含 KG 对象/关系、已确认 Memory 与查询期推导链；最终证据块不超过两者之和。`answer_element_items` 是最终合成 prompt 里允许纳入的直接来源元素(公式/表格/图片等)条数上限，按检索相关度降序择优而非插入序，且仍占用上面同一份原文上下文预算。`enum_page_size` / `enum_pages_per_run` / `enum_rows_per_run` 约束下文的集合枚举工具（`enumerate_elements` / `enumerate_kg_objects` 两个动作及其 `collection="sources"` 参数值）：页大小各档相同，每 run 的额外翻页次数与累计行数随档位增长，与其他上限同一套增长节奏。

| 档位 id | 界面名 | 每查询相关性结果 | 最终 floor / aspect / cap | 最大推理步骤 / 首轮子查询 | KG / 原文上下文字符 | 合成纳入的直接来源元素 | 枚举页大小 | 每 run 额外翻页 | 每 run 累计行数 |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `overview` | 概览 | 4 | 8 / 2 / 12 | 4 / 2 | 4,000 / 12,000 | 4 | 50 | 2 | 100 |
| `standard` | 标准 | 8 | 20 / 3 / 36 | 8 / 5 | 6,000 / 30,000 | 6 | 50 | 4 | 200 |
| `deep` | 深入 | 8 | 24 / 4 / 48 | 16 / 6 | 8,000 / 50,000 | 8 | 50 | 6 | 300 |
| `thorough` | 详尽 | 12 | 32 / 5 / 64 | 32 / 8 | 12,000 / 80,000 | 12 | 50 | 8 | 400 |
| `exhaustive` | 穷尽 | 16 | 40 / 6 / 96 | 50 / 10 | 16,000 / 120,000 | 16 | 50 | 12 | 600 |

“首轮子查询”上限只约束**首轮并发**，不代表装不下的已确认方向就不再执行。超出该宽度的已审阅方向会顺延进一段有界补种：它排在确定性 seed pass 之后、反思循环之前，与推理步骤共用同一份预算（补种最多用掉其中一半，反思循环始终留有可用份额）。步骤预算覆盖不到的方向会持续回喂给反思阶段让模型优先补齐；等反思循环结束仍未覆盖的方向,才会在轨迹里作为一条跳过步显式披露（反映的是运行结束时的最终状态，不是预算刚耗尽那一刻），绝不静默丢弃，反思期间用简称重提某个方向也会被识别为对应同一方向而非另一次检索。

候选生成不随档位放大，而由部署参数独立控制。`CHUNK_RECALL` 默认 **200**，分别约束带索引的 Chunk/KG ANN 与词法候选窗（默认去重前最多 400）；`RELATION_RECALL` 默认 **200**，分别约束 Relation ANN 与词法端点扩出的关系 ID 总窗（默认去重前最多 400），词法总窗内部仍为 source/target 两个方向预留份额。修改任一部署值都会改变实际候选窗，因此界面不会把默认值展示成请求级硬上限。

意图预检把结果范围分为 `ranked`、`complete`、`aggregate`、`hybrid`，并带显式完整性标记；用户确认时会根据最终编辑后的权威措辞与澄清答案确定性重算 scope。像“列出这个 Knowhow 表的所有方法”这样的请求不会变成更大的相关性 Top-N。确定性执行器只接收它能从物理行证明正确的语义：直接整表行/方法清单、直接物理行/记录计数，以及在此基础上的分析。条件子集、去重/种类计数（如“多少种”）、分组聚合目前回退到相关性检索，并明确说明尚不支持精确完整性。受支持的 100 行表可以返回 `100/100`。五档共用完全相同的完整枚举安全契约：

| 完整枚举阈值 | 精确上限 |
|---|---:|
| 每个游标页的行数 | 25 |
| 每次请求页数 | 50 |
| 每次请求扫描/返回的物理行数 | 1,250 |
| 每次请求的表数 | 8 |
| 每表选择的列数 | 8 |
| 交给模型的单元格摘录 | 1,000 字符 |
| 结构化结果载荷 | 256,000 字符 |
| 答案正文内联行数 | 100 |
| 每个结果卡初始显示行数 | 20 |

正文 100 行同时也是 hybrid 模型一次能看到的最大行预览，但不会删除权威结构化结果中的行。因此响应分开表达每表覆盖、请求/批次覆盖（`selected_tables/known_tables`、返回/已知行）和 hybrid 分析覆盖；例如枚举 `200/200`、分析 `100/200` 必须写成“枚举完整、分析部分”。界面初始 20 行只影响展示，其余已回传行可展开并跳回原行。轻量 catalog 最多返回 8 个表描述，不读取格内容、代码附件或健康详情；它会在应用窗口前优先放入问题中显式点名的表，因此排序第 9 张及之后的目标表仍可访问。全库聚合计数与序列和仍覆盖整个 notebook。只有游标耗尽，且枚举前后的 `mutation_seq`、基于历史的 `enumeration_seq`、行数、列元数据和所选/全局范围均稳定，覆盖率才可标完整。触及任一安全线或并发改表时，批次返回 `complete=false` + `explicit_partial`；因 8 表上限遗漏其他表时，已经单独耗尽的所选表仍可保持自己的 `complete=true`。低档位不会缩小这些完整枚举上限。当前执行器只覆盖上述 Knowhow 物理行语义；其他 Knowhow 语义仍必须披露尚不支持完整枚举。来源元素、知识对象与库内文档集合另有一条独立的有界枚举路径——见下文[集合枚举工具](#集合枚举工具)——由模型显式调用，而不是被这里的意图 scope 执行器自动触发；Memory 与其他集合仍必须披露尚不支持完整枚举。

退役 id `fast`、`global` 透明映射到 `chunk`（旧会话/书签不会 422）；其余未知 mode 返回 HTTP 422。

### 集合枚举工具

逐步推理还能通过模型调用的 reflect 动作 `enumerate_elements`（来源元素）与 `enumerate_kg_objects`（知识对象），以及在这两个动作上给出的 `enumerate.collection` 参数值 `"sources"`（改为列出库里的文档），对这几类有界集合做**列举**而非只做排序。模型面的动作空间在这个特性内维持 10 个：第三个集合是一个参数值，不是第三个动作（[下文](#大纲便签与按节合成)的大纲便签之后另外新增了货真价实的第十一个动作 `update_outline`，与这里无关）。三个集合都由零 LLM 执行器提供：模型只决定是否枚举、枚举多少；覆盖率永远由执行器算出，绝不采信模型自报。这是与上文 Knowhow 物理行执行器**并行的独立机制**——共用同一张按档位缩放的预算表，但不共用同一个意图 scope 门，且由模型显式调用而非被 `result_scope` 自动触发。

**可枚举集合。** 来源元素：`formula`（公式）/ `table`（表格）/ `image`（图片）/ `code_block`（代码块）（paragraph、heading 等高体量低语义类型刻意排除）。知识对象：`concept`（概念）/ `claim`（论断）/ `formula`（公式）/ `procedure`（过程），限定为可用状态。两张白名单各只有一处真源（`app.services.collection_catalog`），不允许再抄一份。来源清单没有子类型——库的文档目录本身就是一整个集合。

**来源清单（库里有哪几篇）。** 给出 `enumerate.collection:"sources"`（此时 enumerate 的其他参数忽略——目录没有子类型；该字段的其他取值则落回按动作本身的类型分派），按来源页签的顺序（创建时间、再 id）列出作用域内的每一份文档——所以截断时的前缀就是最早加入的那几篇，那是界面唯一支持的「前 N 篇」读法：显示名（接地判定为论文时优先显示论文标题，与引用同口径）、文档类型（就是上传时那张类型表里的界面词，未识别则不显示）、以及该来源**已存**的摘要摘录。它列的就是来源页签列的那一批——同一条「用户可见来源」谓词，所以答案里不会出现一张用户在来源页签上根本看不到的文档卡（Memory 合成源与 knowhow 表的隐藏投影源两侧都不在内）。它的用途是让「当前 notebook 的文章分析」「库里有哪几篇」这类问题先拿到目录，再按标题对值得深入的那几篇继续检索——后一步走既有的补充子查询机制，没有新增能力。这份清单不读取原文、不做模型调用，成本是每个参与库一次有界读取加上按页的批量取值。目录进入答案合成时，prompt 会同时带上一条粒度指导：篇数少到能逐篇处理就逐篇作答，篇数多则按主题归纳成几个维度并点名各维度下的文档。这个判断交给模型——它手上有精确计数，也只有它知道问题对每一篇的要求有多深——任何位置都不设数值阈值。

**清单里永远不含私有 Memory。** 一条已确认的 Memory 属于某一个人，其他通道也都是这么对待它的：Ask 只经按 owner 隔离的记忆检索通道读到它，Knowhow 智能补全按合同把它排除在候选之外。而集合清单是按笔记本的参与库作用域取数的，自身没有 owner 过滤——把 Memory 留在作用域里，就等于让共享笔记本的每个成员都能按公式/表格/图片/代码块、以及从它抽出的知识对象，把别人的确认记忆逐条读出来。因此地图计数与两个执行器都排除隐藏的 Memory 合成源及其对象，且是**无条件**的：单人笔记本与共享笔记本口径完全一致，一份清单永远只有一个含义。（Knowhow 投影源仍然在内——那是全笔记本可见的内容，本来就能从表本身看到。）这个口径刻意窄于看板上的知识计数：后者回答的是「这个库里有多少知识」，把 Memory 派生对象算进去是对的。

**集合地图（先计数后取行）。** 在这些工具真正值得调用之前，每个 run 只构建一次一行文本，形如 `[Collections in scope] elements: formula 12 (3 sources), table 5, image 0, code_block 0 | KG objects: concept 1234, claim 567, formula 89, procedure 0 | knowhow tables: 2 | sources: 7`（每个白名单 kind/type 恒在场，缺省即 0，让模型能分清「数出来是 0」与「压根没上报」；硬顶 600 字符），注入 plan/reflect 上下文，作用域为 active notebook 加其当前有效的挂载参考库。行尾的 `sources` 是用户可见的文档数（与来源页签、文档数量上限同一口径），且与来源清单的分母出自同一处，不会出现「地图说 7、清单列 8」。计数是索引辅助的，不携带来源标题、文件名或摘录文本——它是 prompt 脚手架，不是证据。地图统计的每源集合与枚举执行器实际遍历的源集合按构造逐字一致，因此不会出现「地图说 12、清单只给 8」的假部分。

同一行还会进入**写答案的那次模型调用**：作为原文证据分区最前面的一小块服务端确定性输出（固定表头 + 已封顶的地图行，整块不超过 800 字符；因为它是服务端算出来的、不是检索到的条目，所以不带 `[k]` 引用）。这是在补一个真实的缺口：reflect 提示语明确要求模型「集合远大于本轮清单额度时，别翻页，直接用计数作答」，而答案合成是另一次调用，本来根本看不到那个数——极端情况下（集合很大、又没检索到别的证据）合成压根不会触发。只要枚举工具开着且地图建成，这一块就会注入，与本轮是否真的枚举过无关；它排在所有证据块最前，避免预算吃紧时第一个被牺牲的恰好是它。

**覆盖率合同。** 每个已枚举集合都携带一个 `TypedCollectionCoverage`：`returned_total`（跨续跑链累计已返回的条数）、`total`（地图给出的已知规模，取不到时为 `null`——渲染为总数未知，绝不写成 `/0`）、`complete`、`truncated_reason`（`budget` / `payload` / `concurrent_change`）与 `overflow_semantics`（沿用共享的 `explicit_partial`）。`complete=true` 要求游标耗尽、首页前与末页后的作用域身份一致，且——跨续跑链——累计条数与已知规模相符；其余情况一律 `complete=false`。这里的「作用域身份」包含参与的库集合本身，并在收尾时重新解析一次：翻页途中挂载、卸载或使某个参考库失效，会改变「整个作用域」指的是什么，此时结果按并发变化报告，绝不说成「全部」。覆盖率之外，结果还携带 `synthesis_rows` / `synthesis_complete`：已枚举的清单与真正进入答案合成 prompt 的有界预览分开统计，因此「枚举 200/200、预览 100」会披露为「枚举完整、分析部分」——与 Knowhow batch 自身的枚举/合成两轨披露同构。这份预览额度在本轮的各个集合之间**分配**而非先到先得：先给每份清单保底等分，某份装不满的余量再按顺序让给后面的。于是一轮里列了两个集合就会预览两个，而不是被第一份吃光额度、让多集合问题只按一张卡作答。

**出处与答案归因合同。** 每个送达的来源元素/KG 对象条目至多携带一条仍存活的有界原文引用；KG 对象从自身已封顶的 evidence element id 列表中取首个仍有效的元素。挂载参考库的证据由服务端在 active notebook 已授权的 participant 集内解析，浏览器只拿来源标题、位置、定位 id 与摘录，绝不调用参考库的成员专用 source 端点。真正进入合成预览的每个条目都会获得隔离的 `k5001+` id 和反向映射；模型使用该条目时必须引用对应 `[k]`，最终回答也只有这些实际绑定的清单锚点才算完成归因。只有带存活原文出处的确定性精确枚举行才能据此判 grounded，同时不会伪造语义相关度分数；孤立行仍可列出并引用其清单身份，但不能把回答判为有据。反过来，枚举答案若一个锚点都没绑定，响应不会把其他相关性检索卡片当作这份清单的兜底来源。结果卡直接显示每一项的原文引用：本库与挂载参考库条目都可经 active-notebook 代理跳到精确来源元素，参考库来源仍保持只读。若历史 KG 条目引用的元素已经全部消失，界面明确显示「暂无可用原文出处」，绝不伪造引用。
**界面展示。** 问答结果卡始终显示覆盖率徽章、清单名称与可选的单一来源范围，但条目内容默认收起。用户打开后才挂载按来源分组的元素或知识对象，并先显示既有的 20 条预览；额外的已加载条目仍通过第二个控件展开。部分结果与合成预览披露位于折叠内容内，卡片关闭时不会挂载或请求图片条目。

**预算与续跑。** 一次动作在其 run 级预算内（`enum_rows_per_run` 总行数、`enum_pages_per_run` 额外翻页次数，以及共享的 `structured_payload_chars` 结构化载荷上限）自动翻页，直到游标耗尽或触及某项上限；只有每个被访问分片（元素按来源、知识对象按参与的库）的第二页及之后才计入页上限，因为一个分片的第一页是唯一能看到它的方式。三个上限都是**每次请求**的，不是每个动作各来一份：每次动作只拿到本 run 的剩余额度，因此一轮深度检索里的多次枚举，累计返回的结构化载荷也不会超过文档写明的请求级上限。载荷上限在两个位置、按两种量法各收一次口，因为它们回答的是两个问题：执行器在遍历时按紧凑的内部行计费（这道闸拦的是「读得比该请求允许产出的还多」），响应映射再按**序列化后的行**计费——那才是真正下发与持久化的形状，而且明显更宽。被第二道闸拦停的那一份，自己报 `truncated_reason=payload`，`returned_total` 等于实际送达的条数；合成预览也按送达的那份渲染，所以 prompt 的覆盖率块头与结果卡永远报同一个数。该游标是 run 内部句柄，不出现在响应里——客户端只看到覆盖率徽章；截断时看到的是「同一轮推理内可以继续列出」，而不是一个可操作的续跑入口。`complete=false` 恒含一个可续跑的游标，唯一例外是 `truncated_reason=concurrent_change`——此时作用域在两次调用之间发生了变化，链条绝不能静默从头重来。对尚未列完的集合重复请求，若本 run 预算尚有行数/翻页/载荷余量则从该游标续跑；预算耗尽时改为按「已达本轮上限」跳过（仍披露为部分结果），而不是按「已枚举过」跳过——只有链条已经 `complete=true` 的集合才会被跳过为「已枚举过」，因为再问一次只会重复已经报过的条目。

**限定单一来源。** 内部来源 id 从不上屏，也从不给模型看——候选摘要与引用里出现的一直是来源标题——所以「只列《某某》里的公式」是按**标题**表达的。服务端在地图已经规划要走的那批来源里做确定性解析：去掉首尾空白、忽略大小写后的精确匹配，绝不做模糊或按相关度排序的匹配，并且有界，避免一个很大的挂载参考库把一次反思变成全库标题扫描。名字没有匹配、或匹配到多个来源时，这一个动作被跳过并在推理轨迹里说明原因：既不会悄悄扩成对整个作用域的枚举（那是在回答另一个问题），也不会在同名文档里替用户挑一个。若作用域内含该类条目的来源多到超过解析器允许查看的范围，它直接拒绝作答，而不是拿看过的那一部分下结论——「唯一」是整个作用域的性质，同名的第二个来源可能就落在没看到的那一段里。

**治理历史很长的笔记本上的知识对象清单。** 知识对象的翻页是对 `(笔记本, 类型, 创建时间, id)` 的纯游标读取；已停用等不可用状态的对象在读回之后过滤，用的就是地图计数所用的同一份可用状态定义。之所以放到读取之后过滤，是为了让「一页」在某类型对象大多已被停用的笔记本上仍然只付一页的代价——把状态条件写进那条查询，它没有索引可走，会被迫访问无界多的行。过滤带来的额外读取本身按动作有界；触顶时给出的是普通的诚实部分结果（`truncated_reason=budget`）并附可续跑游标，绝不会把一份被悄悄截短的清单说成完整。

**尚未构建知识图谱的笔记本。** 这些工具不需要图谱。一个只解析了来源、还没做知识图谱分析的笔记本——自动抽取默认关闭，这本就是常态——照样可以被问「有哪些公式/表格/图片/代码块」；只要作用域里至少还有一个可枚举**元素或知识对象**集合，逐步推理就照常运行。响应里仍会如实报告该笔记本没有知识图谱（「构建知识图谱」的提示继续显示），只是不再因此拒绝作答。既没有图谱、也没有任何元素/知识对象集合的笔记本，仍然直接得到「请先构建知识图谱，或挂载一个已分析的参考库」——因为对它而言这类工具都只会返回空。来源数**刻意不算**在这道闸里：任何非空库的来源数都 ≥1，把它算进来等于把这道闸拆掉，那句明确的提示会对所有这类库消失；放行之后，来源清单与另外两个一样可用。

**范围指示语不是检索词。** 「当前 notebook」「这个库」「本库」「整个库」「知识图谱 / KG」这类短语指的是用户打开的那个笔记本及其挂载作用域——那是每次检索本来就在的范围，不是能在库里找到的内容；没有任何文档会写着装着它的那个库的名字。因此意图理解、两处规划与反思都被明确要求：把这类短语解析成范围之后**剥掉**，不带进任何子查询、关键词或要精确查找的名称，同时问题本身要原样留下（「当前 notebook 里的文章讲了什么」问的是那些文档，剥掉范围词不能把它变成另一个问题）。问库本身的规模或构成，答案来自上面那行计数与这几个枚举动作，而不是去搜「知识图谱」这几个字。这一层刻意只写在提示语里，不做确定性的词表剥离——那会变成词法路由，还会误伤真的在讨论知识图谱的文档。

**计数跟着解析当场生效。** 写入一个来源的元素时，该来源的变更信号在**同一个数据库事务**里一并推进，因此新元素一提交，支撑地图的按源计数就立即失效。刚解析完的来源马上就能被数到、进入遍历计划、被列出来——不存在「元素已入库、却仍被数成 0」的窗口，也不必等解析收尾的那次状态写入。（显式点名的 `source_id` 仍然直接查询该源而不从地图推断：用户亲自点名的一个来源，值得一次索引查询。）

**往返开销。** 一次枚举动作的页查询次数由它自己的预算限住，而且是**强制**的而非默认成立：没有该类条目的来源根本不会被访问，而一个来源之所以在计划里，正是因为地图在它里面数出过条目——访问它就会产出行，行数又受条目上限约束。于是页上限与行上限一起框住了总往返次数。每个被访问分片的第一页刻意不计入页上限：把它也计入，会让真实语料最常见的形态（一百个来源各一条公式）在任何档位都无法达到完整覆盖。

**跨库条目。** 条目若属于一个挂载参考库（而非 active notebook）的集合，会标注「来自参考库《名》」。来源详情跳转与图片统一走 active-notebook 代理端点：浏览器只按用户当前打开的笔记本过权限，服务端再在它实时有效的 participant 集内解析资源。参考库来源仍保持只读，浏览器绝不调用该库的成员专用端点。服务端还可随条目带出上面所述的有界原文引用，使清单卡和答案引用详情在跳转前即可展示标题、位置与摘录。**来源清单**里的跨库条目同一口径：既标注来源库名，也经同一条代理打开——它点名的那份文档正是该代理要解析的资源。仍保留的一项诚实边界是「按纯文本兜底解析（无结构化解析）」来源的计数披露：这类来源的元素本身就不存在，元素类枚举因此完全看不到它们；覆盖率只对**已经入库的元素**声明完整性，不对作用域内的每个来源声明完整性。

**「尚不支持完整枚举」免责声明何时不再前置。** 一个没有执行器能精确服务的完整性请求，回答会前置一句免责声明。只有四条同时成立时它才被抑制：意图 scope 不是 `aggregate`、意图合同里没有任何约束/排除项/前提、至少有一张清单结果卡返回了条目，且该卡的覆盖率为「完整」。其余情况一律保留。方向刻意偏向多警告：一张卡的覆盖率只证明「某个物理集合被完整走了一遍」，证明不了那个物理集合就是问题真正要的那个（经过条件筛选/分组/去重的）子集——而后者没有确定性判据，做出来只会是猜。

将 `REASONING_ENUM_TOOLS_ENABLED` 设为 `false` 可完全禁用这一整套：不构建地图、两个动作与来源清单参数一并不提供、无图早退恢复，逐步推理回到接入前的行为，零额外查询开销。

### AI 对这个库的理解

Agent 维护一份低成本、经 LLM 巡固的、关于笔记本的理解摘要——「AI 对这个库的理解」——以五个固定 label 块的形式，作为 prompt 脚手架注入逐步推理的规划与反思上下文（深度报告每节的深挖检索同样注入，形态逐字一致）。它绝不是证据：`ReasoningResult` 不为它新增任何字段，因此它永远不会进入答案合成、不能被 `[k]` 引用，也不会出现在深度报告正文里。

**五个块与两个归属层。** 三个块构成整本笔记本共享的**底座层**，每位成员都能读到：`corpus_shape`（这个库大致收了哪一类资料）、`key_entities`（反复出现到值得提前知道的名字/主题）、`corpus_gaps`（已经看出来的空缺——没有表格/公式/图片/代码块，或解析质量警告；解析失败与尚未解析完成（后者含排队/解析中的在途文档）分列为两个计数——提问时可以避开）。两个块构成每位成员**私有的覆盖层**：`retrieval_notes`（该成员自己积累的问法经验，只对自己的提问生效、只有自己可见）与 `usage_gaps`（该成员问过但这个库没给出答案的方向，以零命中提问计数而非来源 id 的形式记录）。底座层由按笔记本的巡固 job 在来源变更累计到阈值后刷新；每个覆盖层由按 `(笔记本, 用户)` 的巡固 job 在该成员完成足够多次提问或一次深度报告后刷新（报告完成直接达阈，是刻意登记的、比把报告并入提问计数更简单的触发规则）。报告现在**既是触发器也是输入**（Agentic Memory P2）：它排出的那次整理会同时读该成员近期的提问轨迹**与**该成员自己最近完成（`status='done'`）的深度报告——后者投影自每份报告持久化的 `sections_json[i].attempted` 账目，每个已确认检索方向只留**该方向自己的措辞**与**执行是否出错**两样，此外一概不留：不带正文、引用、步骤类型、耗时或步骤序列（这些从未被持久化，因而也无法找回）。只做过报告、还没提问过的成员因此不再落 `no_usage_sample`——仅这一份报告样本就够。

**报告样本只喂 `retrieval_notes`，对 `usage_gaps` 一分不贡献。** 这是决定而非遗漏。`attempted` 账目里唯一的计数 `new` 看着像命中数，其实不是：它数的是 run **共享候选池**新增的知识对象，所以只要某个方向返回的是前面方向已经收进来的东西它就是 0（同一节的方向按设计就该重叠，何况节问题会先播种候选池）、没有知识图谱的库里每个方向恒为 0、chunk 与 element 命中它根本不计。把 `new == 0` 读成「这个方向空手而归」，等于拿一个量别的东西的计数器，把「这个库没有 X 资料」写进该成员的私有笔记；因此零命中证据（`usage_gaps` 的计数与空手查询样本）保持 **ask-only**，与 P2 之前逐字一致。报告样本贡献的是**措辞**——这个人怎么写检索方向——那正是 `retrieval_notes` 要的东西。同理，渲染出的报告段**绝不断言方向条数**：下面那条方向行数上限与「这份报告一个方向都没跑」在行没了之后无法区分，所以方向被全部截掉的报告只披露「未取样」，不报一个它撑不起的数字。

报告样本的归属谓词是 `reports.created_by`，它是报告的**原始创建者**而未必是这次触发整理的那个人——共享笔记本里任何可写成员都能重新触发别人建的报告生成，所以那次触发方自己的覆盖层刷新可能合理地空手而归（正常的 `no_usage_sample` 终态，不是错误），而不会去整理一份不属于触发者自己的报告。这条不对称是已登记、刻意选择安全方向的取舍：进入某个成员样本的，永远只有他自己的行。

**隔离是结构性的，不是过滤器。** 底座链路的巡固输入在结构上就够不到任何使用/查询/回答数据——它的 SQL 字面上不可能出现 `ask_jobs`/`ask_trace_steps`/`answers`/`memory_items`/`conversations`/`reports` 任一表名，由 allowlist 语义守卫钉住（巡固模块内每个函数必须被分类、各类只能调用本类白名单里的端口方法、读轨迹的 SQL 必须自带归属谓词），而不是靠运行期检查。覆盖层链路的巡固输入只读那一位成员自己的检索轨迹**以及（Agentic Memory P2 / T4 起）那一位成员自己已完成的深度报告**——绝不读别人的——归属谓词（`created_by`/`user_id`）写在取这两者的 SQL 里，而不是事后在应用代码里过滤——与本文档别处 `memory_items.created_by` 的边界同一形状。成员一旦失去该笔记本的读权，覆盖层随即和其他按成员的读一样失效（参与集实时判定）；该成员被移出笔记本时覆盖层会被物理清空——**先删 job/状态行、后删块行**：在飞巡固 worker 的撤销护栏以那一行为判据，先清块的顺序留过一个「worker 把刚清掉的块重建出来、settle 还是绿的」的窗口。**移出之后才到达的完成通知（P1 登记的 R4/R5 残余，已关闭）：** `note_ask_completed`/`note_report_completed`/`start_overlay`（手动重建入口）三处都在 bump 计数或认领链路**之前**——而不是之后——复核同一份读侧参与集谓词；先 bump 会重建出这道检查本来要防止被重建的那一行。检查本身整体 fail-open（异常按放行处理，与本特性其余部分同一条一致），所以残余从「必然复活」收窄为「只有这次有界读本身失败才复活」；上面那条代际护栏（`claim_token`，Agentic Memory P2 / T2）关掉了配套的另一条竞态——行已经被别的路径重建时，一个过期 worker 的 `settle()` 不会再悄悄覆盖更新一轮认领写好的块，因为 `settle()` 现在会区分「行真的没了」（成员确实被移出——该抹）与「行属于更晚的一次认领」（绝不能抹）。底座那两条会看到私有 Memory 的聚合（知识对象类型计数、反复出现的概念名）各自在**自己的语句里**排掉 Memory 合成来源，而不是先读一次 Memory 的来源 id 清单再相减／再当排除清单传进去：两次读之间没有共享快照，中间一次并发的 Memory 增删就会让相减漏减、让排除清单漏项，而后者漏掉的是**概念名称本身**，它会被写进全体成员都读得到的共享块。

**块可以被撤回，但人写的那段不行。** 「省略即保留上一版」是刻意的（模型没话说时不该清掉已有内容），但只有这一条规则时，一段依据已经被删除／重新解析掉的旧论断会永远留在块里、每一轮都被带进提问。所以巡固回复另有一个显式的撤回标记：模型可以指名撤回某一块，服务端把该块的内容清空、行与变更历史照常保留、来源记为「AI 整理」而不是「用户」。这条拒绝**到用户手写的块为止，且不止拦撤回、连普通更新一起拦**：模型对用户手写块的改写会把它的来源身份翻成「AI 整理」，下一轮就可以撤掉用户的原话——所以只要用户写的那块还有内容，服务端拒绝 job 对它的**任何**写入（更新与撤回同判）、记账、同一次回复里其他块不受牵连。用户把块交还给 AI 的方式是清空它：清空保留「用户」身份但值为空，而**空的**用户块刻意不算权威（否则清空会把这个 label 永远冻住，而清空的产品语义正是「让 AI 重新填」）。撤回一个本来就空（或根本没写过）的块不算一次写入。

**撤回的触发信号是渲染出来的证据存活对账，不是模型的猜测。** 每次刷新都会在每个带证据的**底座** job 写块旁渲染一段方括号对账：仍在本轮统计取样里的证据 id 逐个具名（「supported by: s1, s2」）；还在库里、只是没进统计取样前 40（或本身是没有可列内容的健在散文）的另外单独计数（「+N more still in the library」），绝不计入「已消失」；已经不在库里的计数（「M no longer in the library」）；一个块的证据全部消失时，渲染一个明确的 `[all supporting documents are gone]` 标记而不是一个计数。存活判据用的是这次读到的**完整**用户可见文档全集，而不是取样过的统计清单——两者是刻意不同的两个集合，只按取样判会把一份健在的文档误报成消失。prompt 明确告诉模型这段对账只用来核对、不是可复用的证据：对账里出现的 id 绝不能抄进一条**新**主张的 evidence，新主张的 evidence 只能取自统计清单里的文档 id。用户手写的块永远不渲染这一行（它们本来就没有证据，也永远不可能被撤回）。

**依据在界面上可以点开。** AI 整理出来的块在正文下方显示它的依据：底座三块列出支撑它的资料，每份一个可点的标签，点开就是那份资料的详情（与问答引用卡走同一条打开路径）；`usage_gaps` 显示的是服务端自己数出来的「N 次没找到结果的检索」，不可点——它的依据是检索行为而不是某份资料。人手写的块不挂依据。

**数值上限。**

| 项 | 取值 |
| --- | --- |
| 块数 | 5（底座层 `corpus_shape`／`key_entities`／`corpus_gaps`；每成员覆盖层 `retrieval_notes`／`usage_gaps`） |
| 每块字符上限 | 400 |
| 整块渲染字符上限（全部块拼接） | 1,200 |
| 每块变更历史环形缓冲 | 20 条 |
| 底座链路触发阈值 | 累计 5 次来源变更 |
| 覆盖层链路触发阈值 | 该成员累计 10 次已完成提问，或 1 次已完成深度报告（直接达阈） |
| 每次巡固读取的覆盖层轨迹取样 | 最近 40 次提问 |
| 每次巡固读取的覆盖层轨迹步行数 | ≤600（与 40 次提问取样是两个独立维度——40 次提问各有多少步本身没有上界；触顶时优先丢弃最旧提问的步） |
| 每次巡固读取的覆盖层报告取样 | 最近 10 份已完成（`status='done'`）报告（Agentic Memory P2，T4） |
| 每次巡固读取的覆盖层报告方向行数 | ≤200（与 10 份报告取样是两个独立维度，与上面轨迹步上限同理；触顶时优先丢弃最旧报告尾部的方向）。被截到零条方向的报告渲染成「未取样」——到那一步，「触顶」与「这份报告本来就没跑方向」已经无法区分，而按默认配置完整样本本就常态触顶 |
| 报告方向措辞截断长度 | 120 字符（与提问文本、步骤摘要共用同一条逐项截断） |
| 巡固输出预算 | 2,048 tokens |
| 语料统计取样文档数 | 40 |
| 底座链路每条主张保留的证据 id 数 | 8（同时是存活对账里逐个具名渲染的 id 上限） |
| 喂给底座 prompt 的高频概念名 | 24 个，单名 ≤48 字符，合计 ≤600 字符 |
| 覆盖层 prompt 的使用情况段 | 提问清单与报告方向清单**共享同一份** ≤3,000 字符预算，另加 ≤12 条零命中查询样本（每条 ≤120 字符）与固定表头。一份预算但**按分配**而非先到先得：有报告要渲染时提问清单至多用掉其中一半，没用完的余量滚给报告段，两段谁也饿不死谁。没有报告的成员不受这条分配影响，渲染结果与接入这一段之前逐字一致 |

**注入面。** `AGENT_PROFILE_ENABLED` 开启（且已装配画像存储）的 run，会把底座层与当前用户自己的覆盖层同时注入规划 prompt 与反思循环上下文，位置都在集合地图那一段**之前**。一个 Agent 还没为其形成过任何理解的笔记本不注入任何内容、也不记 trace 步——这与 Memory 零命中记 `skip` 步的口径**刻意不同**：Memory 未命中背后是一次 embedding 往返和一次向量扫描，值得记账；而理解块的读取是亚毫秒级的主键点查，它的缺席在一个全新笔记本的每一轮里都是纯噪声。请求确实收窄了检索来源范围时仍然注入——这与集合地图不同（地图在收窄时被清空，因为它承诺的是一批本次枚举不到的集合），理解块不开任何通道、不是证据、不能被 `[k]` 引用，收窄的 run 一样能从「Agent 已经摸出的问法与已知空缺」里受益。在深度报告一侧，理解块只触达每节自己的深挖检索（`_deep_dive`），而深挖恒以 `intent_queries` 起步、不调用规划模型——所以报告路径上理解块实际只落在反思循环里，从不触达报告自己的规划调用。

**端点与角色矩阵。**

| 端点 | 谁能用 |
| --- | --- |
| `GET /notebooks/{id}/understanding` | 任意有读权的成员；返回 `enabled`、`base`（块的取值/证据/revision/`updated_at`/`updated_origin`——变更历史环形缓冲本身在 v1 不对外暴露，只给当前取值）、`mine`（当前用户自己的覆盖层，同形状）、`job`（`{base, mine}`，各自的状态/待处理计数/`updated_at`/失败文案，链路从未被认领过时为 `None`）与 `can_edit_base` |
| `PUT /notebooks/{id}/understanding/{label}`（`scope: "shared"|"mine"`） | `shared` 需要等同 owner 的 `agent_profile:write` 能力；`mine` 只需读权 + 该覆盖层的行级归属 |
| `DELETE /notebooks/{id}/understanding/{label}?scope=` | 与写端点相同的权限口径；清空取值但保留该行与其历史 |
| `POST /notebooks/{id}/understanding/rebuild`（`{scope}`） | 与写端点相同的权限口径；手动认领并重跑该链路的巡固，忙碌或总闸关闭时 409 |

`AGENT_PROFILE_ENABLED`（默认 true）是唯一总闸，同时管住注入、巡固触发与两个 API 面的可见性——关闭后处处逐字回到接入前：不注入、不记 trace 步、不排巡固，API 不是 404 而是返回 `enabled=false` 且两个列表为空（让前端能区分「关了」与「还没形成理解」），重建端点 409。

**Agent 观察记录喂覆盖层，且不可信（Agentic Memory P3）。** 持有 `agent_observation:write` scope
的外部 Agent 可随时调用 MCP 工具 `add_observation`，向自己在这个 `(笔记本, 用户)` 下的观察
队列追加一行——「我在处理这个库时发现了 X」——与任何一次巡固运行完全解耦。这是原始、
**不可信**的输入：与上面这一位成员自己的提问/报告不同，写下观察的是另一方而非其覆盖层
将被塑造的那个人，所以只要一次巡固运行有观察记录要读，就会先给模型发一条 `system`
消息，说明每一行都是外部 Agent 使用接口留下的数据、绝不是指令，且一条观察只有在**与**
该成员自己的提问或报告**相符**时才能支撑某个论断——它绝不能单独构成一个块的全部依据。
一个从未用过这个工具的成员看到的 prompt 与本特性接入前逐字一致（没有 `system` 消息、
没有多出来的段落）——不可信框架的成本不由从未用过该工具的人承担。**仅有观察记录、没有
提问也没有报告时，不会触发巡固**：100% 不可信的输入不足以支撑一次模型调用，覆盖层链路
既有的空样本闸维持不变。当一次运行确实读到了观察记录时，它们会渲染在提问/报告样本之后
**独立的一段**，用**自己的**字符预算（见下表），而不是与提问/报告共享预算——否则一个
爱写短句的 Agent 就能靠数量把一位成员真实的活动挤出同一个池子。观察记录永远不会移动
`usage_gaps` 所依据的零命中查询计数；那个信号继续只从该成员自己的检索轨迹派生，与本特性
接入前一致。

管理这些记录完全走「我的」半侧——清空或查看观察队列只需要笔记本读权加行级归属，不需要
`agent_profile:write`（共享底座那个能力），因为这些行是调用者自己的。
`GET /notebooks/{id}/agent-observations` 按新到旧列出调用者自己的观察（`limit` 默认 20，
上限 200——即下面的环形上限）；`DELETE /notebooks/{id}/agent-observations` 清空，可选按
某个 `agent_profile_id` 收窄。两端点在特性关闭时都 409。网页面板把这个入口显示为「我的
检索心得」下的「Agent 记录」，并明确说明观察记录只会用来更新成员自己的理解、绝不是证据
也不能被引用。

| 观察相关设置 | 取值 |
| --- | --- |
| 每 `(笔记本, 用户)` 的观察环形上限（Agentic Memory P3） | 200 条——`append_observation` 在同一写事务内淘汰超出这个上限的最旧记录 |
| 单条观察字符上限 | 500（`add_observation` 的 `text`） |
| 每次巡固读取的观察取样 | 最近 20 条，与上面提问/报告取样各自独立查询 |
| 渲染出的观察段字符上限 | 600——**自己的**预算，不占用上面 3,000 字符使用情况段的份额 |

**已登记取舍。** 笔记本深拷贝不带这两张表——副本从零重新形成自己的理解，这是刻意设计：理解块描述的是 Agent 对**这一本**笔记本使用方式的体会，不是来源材料本身该被继承的事实。同步 `POST /ask`（不建立持久 `ask_jobs` 行）不推进覆盖层计数器，与「用量统计按持久 `ask_jobs` 提交次数计数」的既有口径一致。单人笔记本的底座与覆盖层链路刻意**不**合并执行——各自照常排队与运行，登记为 P1 的简化而非正确性要求。任一链路的巡固失败仍会消费认领时刻快照下的 `pending_signal` 计数，因此失败的一轮需要重新攒满阈值才会重试，把成本封在「每个阈值批次至多一次模型调用」，而不是对随后每一次变更都重试。把成员移出共享笔记本会经成员移除路径清空其覆盖层（`kick_all_members` 刻意**不**清理——已登记的例外，因为读侧参与集闸本就让被踢出成员的覆盖层在每个消费方那里都不可达）。

### 检索策略经验

在上面的笔记本理解块之外，还有第二份独立的记忆（Agentic Memory P2）：一张**部署级全局**的检索打法库，由已完成的 Ask run 离线蒸馏而来，作为它自己有界的 prompt 块注入同一套规划/反思 prompt。理解块描述的是**这一本**笔记本，一条经验条目描述的却是**一类问题的形态**——「在具备这些特征的问题下，这个检索动作值得／不值得用」——它不携带任何笔记本、来源或用户身份：该表没有 `notebook_id`，没有 owner 列，每个人读到的是同一批行。

**形态。** 一条经验是一对封闭的 IF→THEN：一个八键**情境**指纹（Ask 模式；`result_scope`；`retrieval_effort`；这次 run 是否要求完整性；实体数与必答主题数的**分桶**——`none`／`few`（≤2）／`many`；确认后的意图是否带约束；是否带排除项——每个值都取自封闭枚举，没有一个来自问题本身的措辞）映射到一个封闭**动作**词表里的一个词（reflect 循环自己的检索动作，折成它们的 trace-step 拼写：`retrieve`/`ppr`/`exact_lookup`/`expand`/`expand_community`/`follow_chain`/`enumerate`/`outline`——`enumerate` 是刻意的通配，代表 `enumerate_elements` 或 `enumerate_kg_objects` 两者之一，因为一次已完成 run 持久化的轨迹分不清它当时用的是哪一个），一个**极性**（`good`/`bad`），以及模型撰写的一句**理由**。`support` 计数记录有多少次蒸馏 run 支持这条结论；`adopted` 计数记录有多少次被展示过这条经验的 run 之后真的选了那个动作，它是表满之后淘汰排序的首要键。

**outcome 口径——P4 已回收其中四个动作。** v1 只能在**步粒度**观测失败（零命中的动作），只能在**run 粒度**观测成功（这次 run 自己的引用数/`evidence_level`），因为没有任何持久化字段记录「答案实际引用的是哪一步的结果」。Agentic Memory P4（T1/T2）在写侧把这个缺口补上，且不改变每一步本身**做**了什么：四个自身结果就是可寻址对象/chunk id 列表的动作分支——`retrieve`（首次候选拉取，以及每一轮 `add_subquery`）、`ppr`、`exact_lookup`、`expand`——现在无条件把 `result_ids` 写进自己的 trace-step detail，覆盖每个调用点（确定性 seed pass 与模型主动选中的那一轮都写），零命中时的空列表本身就是信号，用来把「跑过、什么都没找到」与「老轨迹行、字段缺席」区分开；列表截到 `TRACE_RESULT_IDS_MAX`（见下方数值表）。`expand_community`/`follow_chain`/`enumerate`/`outline` **刻意不改**——它们的结果不是同一种 id 列表形状；`search_elements` 落在词表之外的 `fallback` 步，所以原文段落级的锚点结构上仍不可归因（已登记的边界，不是遗漏）。最终 `synthesis`/`answer` 步单独写 `anchor_evidence_ids`：答案真正绑定的 `[k]` 锚点（按 `object_id`），截到 `TRACE_ANCHOR_EVIDENCE_IDS_MAX`（这是**ranked** 答案锚点清单的协议上限——五档里最大的那档 `ranked_final_cap`，按现有各档预算这种形状的 run 实测不会真的触顶；**集合枚举**类 run 不受 `ranked_final_cap` 约束——每一行进入合成预览都会拿到自己独立的 `k5001+` 锚点 id，一份足够大的枚举清单真能超过 96、把这个上限顶到。触顶时稀疏标记 `anchor_evidence_ids_truncated` 会置位，投影 pass 1 据此把整条 run 判成不可归因——这是刻意选的安全方向：一次超大枚举 run 悄悄丢掉蒸馏信号只是样本薄一点，而接受一份被截断的锚点集会教会经验库假的「没命中」，因为真正命中的那个 id 可能恰好在被切掉的尾巴里）。逐动作的成功现在是 `RunObservation.ActionObservation.anchored_hits`——这个动作自己的 `result_ids`（本 run 内所有调用累计）里有多少个 id 最终被答案真正引用——外加一个独立的 `attributable` 布尔，而不是折成一个可空计数（隐私守卫的类型注解扫描器只认 `int`/`bool`/封闭 `Literal`，不认 `Optional[int]`）；`attributable=False` 表示「无法判断」（老轨迹行，或本 run 该动作没有写出任何 `result_ids`），绝不是「这个动作没帮上忙」。id 与 id 的求交只在蒸馏投影自己的循环里就地算一次，用的是函数局部变量，从不进入 `RunObservation` 的任何字段——原始 id 从未到达撰写理由的那个模型（见下面的隐私段落）。轨迹展示侧两个键都是没有任何分支去读的裸句柄：前端 `getTraceStepDetail` 在任何 step_type 下都没有读 `result_ids` 或 `anchor_evidence_ids` 的分支（它的通用兜底只读 `count`/`found`/`anchors` 这类，从不读 id 列表键）。这次改动之前写下的轨迹行本就没有这两个字段，蒸馏投影对它们照旧回落到 v1 的、只有 run 粒度的观测——`zero_hits`/`citations`/`answered` 仍是那种情形的兜底，不是同一个信号的冗余拷贝；不需要也没有回填，因为蒸馏读的本就是最近一批有界窗口内的已完成 run。这两个键对单条持久化轨迹行自身存储体积的贡献不大，且刻意不开一个可关的开关：一条典型行（每个被接入的步几条 `result_ids`,外加一份有界的 `anchor_evidence_ids`)给该行的 JSON 大约多带 2–4 KB,最坏情形(每个符合条件的步都顶到各自的上限)约 10 KB 封顶。不开开关的理由是硬的:开关会打破读侧的按键存在判据——缺键意味着「这一步从没写过结果」(P4 之前的老形态),带键意味着「这一步跑过,这就是它找到的」(哪怕是空列表也一样),而一个「有时写、有时按需隐藏」的开关会让这两种读法互相分不清。

**隐私是结构性的，不是一句请求。** 这是本仓库唯一一张**没有任何归属谓词**的存储——没有 `notebook_id`，没有 `created_by`，没有 `owner_id`——因为它存的是每个用户在每个笔记本里贡献、又被每个用户读到的通用打法。隔离保证因此不能像别处那样活在一条 SQL 谓词里，只能提前一层，活在「蒸馏模型究竟能看见什么」的**形状**上。一次已完成的 run 被投影成一个冻结的 `RunObservation`（以及它可达的每一个类型），其中每个字段都是 `int`、`bool` 或封闭 `Literal`——它可达的形状里没有任何自由文本字段，所以撰写理由的模型从未见过问题原文、答案、文档标题或任何 id。这是一条测试可以通过读类型注解来核验的性质，而不是靠一句 prompt 指令让模型自己把标识符「参数化」（`set_db` → `{identifier}`）——设计稿最初就是这么设想的，被否决正是因为那种形状下的泄漏没有任何报错、也没有任何测试会红。一道专门的隐私守卫会**同时**扫描投影模块与蒸馏 prompt 模块（不是只扫其中一个——把一处读 `question` 的代码从一个文件挪到另一个文件，只扫一个模块的守卫会被绕过），断言封闭字段性质、逐个命名并说明危险原因的禁用标识符扫描，以及「动作词表不含任何范围类词」的反向守卫。这份保证的代价是表达力：一条经验说不出「先列目录再按标题深挖」，只能说「在这类问题形态下 `enumerate` 值得用、`exact_lookup` 不值得」。设计稿最初提议的两个情境特征在 v1 刻意**不**采集，均已登记而非遗漏：一是任何从问题原文派生的特征（是否含引号短语、是否含可精确查找的标识符——采集任一个都要求投影层触碰 `question`）；二是笔记本形状（文档数分桶、语料语种、是否有知识图谱）——后者的理由是，在 `support` 常常等于 1 的情况下，一个人人可读的全局行上带着一份有辨识度的语料指纹，会指认出某个人在某个库的某一次 run。设计稿原本的「按笔记本加权」经验选择（§12-Q3）因此在 v1 弱化为**只按意图形状**匹配（已登记的偏离，见设计文档分期表）。

**永不改变的一条：经验只影响这次 run 怎么查，绝不影响它可以读什么。** 动作词表结构上不可能出现任何改变范围的动作——它不含任何来源范围或参考库相关的动作，渲染出的条目里也没有任何来源/笔记本/参考库名可渲染。「用户勾选是检索范围唯一来源」这条红线不受触动。

**蒸馏**是一个独立的、部署级、低频的离线 job（有自己的 workload id `retrieval_experience_distill`，可以单独指向不同模型，或独立于笔记本理解巡固单独关闭），由自己的累计已完成 Ask 数阈值触发，读取最近一批已完成的提问（同覆盖层的轨迹样本一样双重设界：一个提问数上限，外加一个独立的步行数上限）。它不需要任何游标表：重复处理一批有重叠的 run 是幂等的，因为一条经验的 `support` 只对**尚未出现在它自己那份有界、按最新排序的 `provenance` 列表里**的 run id 才递增——批次大小与 `provenance` 上限被钉在一起来保证这条不变式（一批读取的 run 数绝不能超过一条经验能记住的上限，否则被挤出尾部的 id 会在下一批重叠时被重复计数）。模型看到的是这批里出现频率最高的若干情境（每次调用有界，好让 prompt 有界而不必限制批次的 run 数），外加针对每个情境、库里已有的相似情境条目（Mem0 式的局部更新），并对每个情境返回 `ADD`/`UPDATE`/`NOOP`；畸形回复整份拒绝而不是修补，库保持原状。表满之后的淘汰按 `(adopted, support, updated_at)` 升序——先淘汰没被采用过的，再淘汰支撑薄弱的，最后按时间。

**被动注入做到任务级**（不管模型有没有主动要，每一次 run 的每一次规划/反思 prompt 都自动推送），由一把独立于蒸馏开关的**注入闸**控制——一个部署可以只蒸馏、只观测而从不注入，这正是设计文档为「效果可能不明显」登记的风险缓解手段。开启时，一次 run 会把整张（有硬顶、常驻内存、纯函数）表按当前情境打分——深度报告逐节深挖场景在意图契约尚不存在时也能算出情境，缺失的键落回各自的 `unknown`/`none`/`false` 默认值——注入相似度最高的至多三条（相似度地板 0.5；低于它就判定这条经验说的是另一类问题，比不注入还糟，因为模型分不清两者）作为一个小块，紧跟在笔记本理解块之后、集合地图之前渲染，规划 prompt 与每一轮反思都会带上它。和理解块一样，它**只**是 prompt 脚手架——`ReasoningResult` 不为它新增字段，绝不是证据，也绝不可被 `[k]` 引用。深度报告一侧只接入每节自己的深挖检索（那条路径恒传 `intent_queries`，不调用规划模型），在没有完整意图契约时用该节自己持久化的 `result_scope`/`completeness_required`。`adopted` 只在极性为 `good`、且该条经验在块自身字符上限内**真正送达**（不是仅被选中——装不下的行永远不会被模型看到，也就谈不上被采用）、且模型自己那一轮 reflect 决策点名了那个动作（发生在块非空的那一轮）时才计数。

**两笔账如实登记，而不是假设它们不存在。** 第一笔，最坏送达：整块字符上限（600 字符，含固定表头与一句框定语）给行本身留下的预算大约只有 380 字符左右；当一条送达经验的理由逼近它自己 160 字符的上限时，被选中的三条里实际能装进去的大约只有一条——「至多三条」是**选中**宽度，不是**送达**保证，调用方因此报告的是块真正送达的条数而不是选中数，原因正在这里。第二笔，reflect 累计成本：这个块在**每一轮**反思都会重新拼进 reflect 的上下文，不是每次 run 只拼一次，而且它的体积不随检索档位缩小——在 `exhaustive` 档 50 步的上限下，一次 run 生命周期内累计重复的块文本大约是 3 万字符量级，这正是注入闸默认**关**、独立于蒸馏闸默认**开**的原因。

**模型主动拉取的回想：`consult_memory`（Agentic Memory P4，T5）。** 与上面的被动块并列的第二条路径，这一条才是真正的**步级**：一个零参数的 reflect **动作**——`consult_memory`——模型可以选择拿一轮反思去调它，而不是被动接收自动推送。它出现在 reflect schema 的 `next_action` 枚举里，要求**三个**条件同时成立：部署自己的总开关（`REASONING_CONSULT_MEMORY_ENABLED`，默认开）、这次 run 的 `retrieval_effort` 档位是 `deep`/`thorough`/`exhaustive`（`overview`/`standard` 永远不提供——与大纲便签「低档不值得付这份成本」同一条道理，但门槛更低，因为一次 `consult_memory` 调用只花一轮反思，不会像大纲便签那样触发按节合成），**以及**被动注入闸（`RETRIEVAL_EXPERIENCE_INJECT_ENABLED`）也要开着。第三个条件是对设计稿原始草图「总开关+档位」两条独立闸的刻意收窄，已在设计文档登记：注入闸关着时，整个部署都读不到经验库任何东西，若只按「总开关+档位」放行，会让模型看到一个能调、却注定永远拉不到任何东西的动作——那笔反思轮预算纯属浪费。深度报告的逐节深挖自动继承同一道闸——depth 按大纲便签（PR-5）同一张表映射到检索档位，深挖自身的 `limits`/`retrieval_experiences` 接线本就没变，depth 落在 `deep` 及以上档时这个动作自然可用，不需要为它新增任何接线。

`consult_memory` 读的是被动块读的同一张表，同一条相似度地板、同一套封闭词表，但**选择**刻意不同——两处不同都是为了让每次调用都值一轮反思，而不是被动块那三行的第二份拷贝：它排除被动块（或本 run 更早一次 `consult_memory` 调用）已经送达过的行——模型每一轮都看得到那些行，再送一遍不是新信息、只是白花一轮；它优先返回**本 run 内**已经连续空转的动作相关的条目——那正是模型在纠结「要不要继续押注一个已经停摆的通道」时最想知道的一刻。一次调用还会顺带、每 run 最多一次，带上调用者自己在 P2/P3 笔记本理解块里那句还没送达的「检索心得」覆盖行（`agent_notebook_profile` 每个成员自己的 `retrieval_notes` 行）——当共享理解块自身的整块字符硬顶恰好把它截没、模型从未看到时——这是与行选择那半同一类「试一次就不再重复」的差集逻辑。本 run 迄今为止 `consult_memory` 累计选中的全部内容，在**每一次**调用时都重新整块渲染（不是每次调用再叠加一个新的有独立上限的块），所以同一个 run 里调两次，加起来花的 prompt 预算也不会超过被动块自己那一份；渲染出的整块，在 run 的整个生命周期内，硬顶在 `CONSULT_MEMORY_BLOCK_MAX_CHARS`（见下方数值表）。它是零 LLM、零 embedding 的——纯粹是对已经驻留在进程内存里的行做一次内存内打分选择，成本形态与被动块自己的打分一致。

**渲染优先级与「只标真送达」账目（修复轮 spec④/Q-P1-3）。** 600 字符硬顶按整行/整句丢弃装不下的内容，与被动块同款；但库行与覆盖层心得同时在场时谁先占到剩余预算不是随意的——覆盖层心得**先**渲染，排在所有经验库行之前。它是稀缺的、有界的一行个性化信号（这位成员自己的检索心得，没有任何别的通道会展示它），而经验库行是可能有很多条的共享打法之一，这次被挤掉、下次调用还能再选中；预算紧张时应该让更稀缺的信号占到位子。渲染器会原样回报哪些行、以及覆盖层心得有没有真的被渲染进文本，调用方的账目跟随这份回报而不是渲染前的选中集：`consult_delivered_ids` 只收真正渲染进去的行 id（被硬顶挤掉的行仍然可以被未来某次调用选中——按"已送达"标记它会让它永远没有机会再出现），覆盖层心得只有在真的至少渲染进去过一次之后才被记为"已送达"（此后才不再被当作"新"提供）。轨迹步的 `entries` 计数是本次调用真正新送达的行数（渲染之后的数字），不是渲染前的选中数——与 `rendered_row_count` 已经对被动块沿用的那条"报告实际展示了什么，不是选中了什么"同一条纪律；`chars` 仍是整个累计块此刻的长度。一次调用如果真的什么新东西都没选中（能匹配的行早已全部送达，也没有新的覆盖行），照旧落一条 `reason=consult_memory_nothing_new` 的 `skip` 步；一次调用**确实**选中了新东西、却被 600 字符硬顶全部挤在块外，则落另一条独立的 `reason=consult_memory_block_full` 的 `skip` 步——两种情形预算（`consult_used`，进而 `REASONING_MAX_CONSULT_MEMORY`）都照扣（毕竟真的尝试过），但两个不同的 reason 让读轨迹的人一眼分清"没什么可给"和"找到了但装不下"。每 run 调用次数上限 `REASONING_MAX_CONSULT_MEMORY`（见下方数值表）；超过上限的调用同样落一条写明原因的 `skip` 步。

**服务端确定性推送的步级提示（Agentic Memory P4，T6）。** 一条独立的、零 LLM 的机制，**只**受注入闸（`RETRIEVAL_EXPERIENCE_INJECT_ENABLED`）门控——不要求任何档位，因为它不占用额外反思轮：服务端自己在四个已埋点的动作分支（`ppr`/`exact_lookup`/`expand`/`follow_chain`——这四个分支的派发代码本来就会算出「本轮新增了几条」这个确定性计数，作为自己既有账目的一部分）之一记录到该动作在本 run 内**第二次**连续零新增调用时，往 reflect 循环回喂给模型的账目摘要里追加一句话。这句提示会点名该动作、说明零命中已经连续几次，并**逐字引用**（不改写）经验库里与该动作、该 run 当前情境匹配度最高的一条 `bad` 极性条目的理由（相似度地板与本特性其余各处一致；库里找不到匹配的 `bad` 条目就不提示——一句没有内容的提示比不提示更糟）。整个 run 至多提示两次，是四个被跟踪动作**合计**的总闸（不是每个动作各两次）——这个上限存在的理由是防止四个通道同时停摆时，一轮反思的账目摘要被四条提示同时拉长。一个动作一旦被提示过，本 run 内不会再被第二次提示，不管它之后又累计了多少次零命中。这句提示套的是一个固定的中文模板，这里逐字照抄代码里的写法——`（提示:「{动作}」这类动作在当前场景已连续 {N} 次未拿到新证据;以往打法经验:「{rationale}」。可考虑改用其他动作。）`——被引用的理由原样嵌入（已经受写入侧 `RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS`——被动块依赖的同一个 160 字符上限——约束过，这里是复用既有上限，不是新开一个），动作名走的是模型自己 schema 里用的那套拼写（`_ACTION_IDS`），不是内部 trace-step 拼写。全程 fail-open：读经验库、给情境打分、拼句子都是对本轮反思已经在内存里的数据做的纯函数计算，链路上任何一步读失败都只是这一轮不提示，不会打断反思循环。

**只经由轨迹可见。** v1 不为它新增 `AskResponse` 字段，也不做管理面板——一张全局的、跨用户的、带 `support`/`adopted` 计数的封闭词表表，即便已经封闭化，也仍然是关于别人用量的信息，所以唯一面向用户的界面是轨迹里的一步轻量展示（界面标签「打法」），报告送达条数与字符数，与集合地图同一条「集合地图式内部脚手架」登记口径。P4 的 `consult_memory` 有自己独立的轨迹步（界面标签「回想」，刻意与「打法」和 Memory 召回步的「记忆」都不同名），报告的是**这次调用自己**渲染之后新送达的条数（修复轮 spec④，不是累计总数）；P4 的步级提示不新增任何轨迹步类型——它搭在既有的 reflect 账目摘要文本里，与「已确认方向未执行」的披露走的是同一条路。面向管理员查看库内容的界面留给后续阶段。

| 项 | 数值 |
| --- | --- |
| 部署保留的条目上限 | 300（质量上限，不是存储上限——淘汰按 `(adopted, support, updated_at)` 升序） |
| 每条经验保留的出处 run id 数 | 60（按最新排序；与下面的批次大小构成不变式） |
| 蒸馏批次——已完成提问数 | 40 |
| 蒸馏批次——轨迹步行数 | ≤600（与 40 条提问样本分开的独立上限，理由与笔记本理解覆盖层的步上限相同） |
| 蒸馏触发阈值 | 累计 40 次已完成提问（刻意与批次的提问数相等） |
| 理由字符数 | 160（超长是拒绝这一条，不是静默截断） |
| 蒸馏输出预算 | 1,024 tokens |
| 每次蒸馏调用展示给模型的情境数 | 4（批次内出现频率最高的若干个） |
| 每个情境展示的已有相似条目数 | 3，相似度 ≥ 0.5 |
| 计数分桶边界（`entity_count`/`topic_count`） | `none`=0，`few`≤2，`many`>2 |
| 情境键词表 | 8 个键（mode、result_scope、retrieval_effort、completeness_required、entity_count、topic_count、has_constraints、has_exclusions） |
| 动作词表 | 8 个词（`enumerate` 是两个枚举动作的通配） |
| 极性词表 | 2 个（`good`/`bad`） |
| 注入——送达条目数 | ≤3，相似度地板 0.5 |
| 注入——整块字符上限 | 600（表头+框定语+行；装不下的行整行丢弃） |
| 步→锚点归因——每个 trace 步的 `result_ids` 上限（`TRACE_RESULT_IDS_MAX`） | 20 |
| 步→锚点归因——每 run 的 `anchor_evidence_ids` 上限（`TRACE_ANCHOR_EVIDENCE_IDS_MAX`） | 96（**ranked** 答案的协议上限——五档里最大的那档 `ranked_final_cap`，那种形状的 run 不会真的触顶；集合枚举 run 的 `k5001+` 锚点可以超过它，触顶即截断、整条 run 判不可归因——刻意选的安全方向） |
| 步→锚点归因——单条轨迹行自身的存储体积 | 典型 2–4 KB（`result_ids` + `anchor_evidence_ids` 合计）；最坏约 10 KB（每个符合条件的步都顶到各自上限）——不开单独开关（会打破读侧的按键存在判据） |
| `consult_memory`——每 run 调用次数上限（`REASONING_MAX_CONSULT_MEMORY`） | 2 |
| `consult_memory`——每次调用返回条目数（`CONSULT_MEMORY_TOP_K`） | 3（与被动块自己的 top-K 同一量级，仍是「几条打法提示」） |
| `consult_memory`——整个 run 累计的整块字符上限（`CONSULT_MEMORY_BLOCK_MAX_CHARS`） | 600（形态与数值同被动块自己的上限；本 run 累计选中的内容在每次调用时重新整块渲染，不是逐次调用叠加） |
| `consult_memory`——提供的档位 | 仅 `deep`/`thorough`/`exhaustive`（`overview`/`standard` 永不提供） |
| 步级零命中提示——触发所需的连续零命中次数 | 同一动作连续 2 次零新增调用 |
| 步级零命中提示——每 run 提示次数 | 2（四个被跟踪动作合计的总闸，不是每个动作各 2 次） |
| 步级零命中提示——引用理由字符上限 | 复用既有的 `RETRIEVAL_EXPERIENCE_RATIONALE_MAX_CHARS` 写入侧上限（160），不新开一个 |

### 我的回答偏好（用户检索/回答风格 Profile）

Agentic Memory P3 的第二条、相互独立的 B 线：一份很小的按用户偏好文档
`user_profiles.search_profile_json`（`NULL` = 用户从未设置过偏好、也从未有归纳任务写过
值——与 `ui_mode` 同一套契约），在账户菜单的「我的回答偏好」里编辑，并作为一行有界文本
注入 Ask 的规划与答案合成 prompt。它绝不触碰检索——形状里没有任何来源、笔记本或范围
字段——只影响回答怎么组织、怎么措辞。

**形状。** 四个封闭字段，各自携带 `{value, origin, updated_at}`：`answer_language`
（`auto`/`zh`/`en`）、`answer_shape`（`auto`/`bullets`/`table_first`/`prose`）、
`answer_detail`（`auto`/`concise`/`detailed`）、`domain_terms`（自由文本列表，≤10 条、每条
≤32 字符）。`origin` 为 `"user"` 表示用户显式设置过，为 `"job"` 表示由 T7 归纳任务写入。
字段缺席——而不是存一个显式 `"auto"`——才是「可再被归纳」的标记，也是渲染器判定「这个
字段不用说」的依据；用户把某字段改回自动即删除该条目，而不是存一个显式的 `auto`——与
理解块「清空即交还」是同一套契约。归纳任务绝不能覆盖当前存储 origin 为 `"user"` 的字段——
与 `agent_profile_job` 的 `user_authoritative` 对提示块已经在强制的同一条规则，这里作用在
偏好字段而非提示块上。

**v1 只归纳一个字段，纯确定性，零模型调用。** 一个后台任务（有自己独立的按用户触发阈值，
与理解块的两条巡固链路各自独立）读取该用户最近若干次已完成提问的语言，样本数足够大且
某一种语言占明确多数时写入 `answer_language`（`origin="job"`）；样本不足或没有明确多数时
不写——写一个猜出来的 `auto` 会挡住之后更好的样本再填这个字段。**另有两个候选信号 v1
刻意不归纳**（已登记，非遗漏）：用户常用的检索档位（目前没有能安全消费它的下游，纯风险
无收益）；用户常用的领域术语（把用户过去的措辞固化进未来每一次 prompt，与「你倾向用
中文提问」是不同性质的主张；v1 把 `domain_terms` 完全交给用户自己填）。**归纳出的值绝不
单独进入 prompt**——镜像 P2 检索策略经验库「先接好管线、注入待验证」的姿态：一个推断错的
`answer_language` 会与答案 prompt 「按提问语言回答」的默认规则直接矛盾，所以设置界面照常
显示推断值（带「自动判断」徽标），用户必须显式确认（也就是执行一次 `origin="user"` 写入）
才能让它进入模型调用。`domain_terms` 与任何显式选择的 `answer_shape`/`answer_detail`/
`answer_language`（`origin="user"`）立即生效注入——那些是用户真正提出过的要求。

**注入面：只接 Ask，v1 刻意不接深度报告。** `search_profile_wiring_active`（受
`USER_SEARCH_PROFILE_ENABLED` 门控）开启且该用户至少有一个用户显式设置的字段时，渲染出
的这行会同时出现在规划 prompt 与每一次答案合成调用里，并带一句明确的边界说明：只影响
措辞与组织形态，绝不影响证据可用性或 `[k]` 绑定。它不注入反思循环——措辞偏好与下一步该
选哪个检索动作无关，这一点与会影响反思的理解块、检索策略经验条目不同。报告生成
（`report_engine.py`）v1 完全不读它；后续阶段可能扩展进去。

**数值上限。**

| 项 | 取值 |
| --- | --- |
| 字段数 | 4（`answer_language`、`answer_shape`、`answer_detail`、`domain_terms`） |
| `domain_terms` 上限 | ≤10 条，每条 ≤32 字符 |
| 渲染出的风格块字符上限 | 600（`SEARCH_PROFILE_BLOCK_MAX_CHARS`）——预算按「最大合法档案必然完整渲染」定标（codex #535 R5：预算小于输入上限之和会让「已保存」的用户选择在渲染点被静默丢弃）；逐条装入保留为纵深防御 |
| 归纳取样规模 | 最近 30 次已完成提问（`SEARCH_PROFILE_LANGUAGE_SAMPLE_LIMIT`） |
| 归纳最小样本地板 | 10（`SEARCH_PROFILE_LANGUAGE_MIN_SAMPLES`）——低于此不写 |
| 归纳多数阈值 | 占全样本 0.7（`SEARCH_PROFILE_LANGUAGE_MAJORITY_RATIO`；「其他」语言占比仍留在分母里） |
| 归纳任务触发阈值 | 该用户累计 20 次已完成提问（`USER_SEARCH_PROFILE_TRIGGER`，默认） |
| 部署开关 | `USER_SEARCH_PROFILE_ENABLED`（默认 true）——关闭后处处逐字回到接入前；`GET /me` 仍照常返回该行上已存在的取值，不会伪造成 `search_profile: null`，但 `PATCH /me/search-profile` 409 |

### 大纲便签与按节合成

被门控在「穷尽」检索档位的逐步推理，可以在反思循环中维护一份有界的、由模型撰写的大纲便签；当终态大纲解析后仍有两节或以上带着存活证据时，答案就按这份大纲逐节合成，而不是一次性通读全部证据来写。总开关是 `REASONING_OUTLINE_ENABLED`（默认 true）；关闭时，或档位不是「穷尽」时，这整套机制完全不存在——没有这个动作、没有对应 schema 分支、没有 trace 步，与这个特性从未接入逐字一致。

**`update_outline` 动作。** reflect 循环新增第十一个动作 id `update_outline`，只在「穷尽」档提供。每次调用都携带整份章节结构（结构是全量替换，不是增量补丁）：至多 12 节、两层（节可带 parent）、每节标题至多 60 字符、每节至多绑定 8 个证据 key。证据另守 citation persistence：同一稳定节 id 的合法 `evidence` 与旧绑定取并集，遗漏不再等于删除；要删除旧绑定必须在该节 `remove_evidence` 中点名，且同一个 key 同时出现在两处时显式删除优先。8 键满额时旧绑定优先，装不下的新 key 会在下一轮大纲账目中点名，而不是静默挤掉旧证据，模型可先显式腾位再重试。pending 本身不会冻结普通更新：常规额度尚存时，下一份全量载荷仍可增删、重排、改标题，里面所有合法绑定都会照常合并。`sufficient` 终态只有当轮真的提交了 `update_outline`、合并后仍有未接纳 key、且正常 reflect 步骤仍有余额时，才最多得到一次纯换键纠错；stale 熔断只看是否存在未接纳 key（当轮没碰大纲也会给出同样仅一次的纠错轮），直接 `answer` 收尾不追加纠错；第 6 次常规更新耗尽自身额度后也可暴露一次纯换键资格。这些纠错提交必须保持所有章节 id/标题/parent 不变、不能执行检索；第 6 次后的首次纠错提交立即消费资格，即使因结构违规被拒也不能重试。整体 `max_steps` 是绝对上限：最后一步产生 overflow 时只在收尾披露，不会跑成 51/50；stale 熔断事实会先落轨迹，再在仍有步骤余额时进入终态纠错。仍未接纳的 key 会在收尾 trace 中明确披露，答案只使用已经接受的绑定。整节从结构中省略仍会删除该节。每个 run 常规调用至多 6 次；超出后的非纠错提交会被跳过，并告知模型大纲已定稿。若一次提交里没有任何可用的节，同样跳过并**保留**上一份大纲——一次畸形响应把已经建好的大纲抹掉是最坏的结果，这套机制绝不让一轮坏响应清空已有结构。

**证据绑定由服务端校验，不采信模型自报。** 一个绑定 key 合法，当且仅当它仍在本 run 的存活候选池中，并且曾在至少一轮候选摘要窗口里实际展示过，或已经由当前大纲合法持有。run 内的「曾展示」集合单调累积，因此早先看见并绑定的 key 后来滑进摘要省略中段也不会失效；从未渲染过的中段 id 则不能仅靠模型猜中而通过。枚举清单的条目 id 与来源 id 刻意都不在合法集合内：前者模型根本看不到（清单只回计数，不回 id），后者是因为一份文档不是一条可引用的证据。非法 key 被静默丢弃；一节如果最终没有任何合法绑定，会被记为空节而不是被丢弃——空节正是模型下一步该定向检索的方向。

**未接纳 key 持久化。** 溢出的 key 是服务端持有的 pending 状态，不是只存在一轮的诊断。它会跨纠错提交持续点名，直到成功进入该节、模型把这个 pending key 本身写进 `remove_evidence` 明确放弃，或全量替换结构直接删除整节。模型即使腾出了旧位置却漏抄待换入的新 key，收尾 trace 仍会明确披露 unresolved overflow，不会静默丢掉新证据。完整 pending 由结构上限夹在每节 56 个 key（6 次常规提交加 1 次纠错、每次最多 8 个），但每轮 reflect prompt 只展示前 8 个和剩余计数；处理或放弃这一批后再露出下一批，与 `remove_evidence` 单次最多 8 个的输入上限对齐，prompt 不随批次数增长。

**大纲是整份回喂，不是增量对账。** 因为每轮反思都是一条全新的 prompt、没有对话历史，而章节结构又是全量替换语义，所以每轮都会把当前整份大纲连同各节缺失绑定的清单一起回喂，模型看到的永远是自己上次真正交出的东西，不必凭记忆重建；即便模型抄漏某个证据 key，服务端也会按稳定节 id 做并集保底。纯粹的措辞整理不算进展：大纲修订对推理循环的空转熔断保持中性（真正的检索动作仍会照常重置熔断计数），所以来回提交两份长得不一样、内容却没变化的大纲，蒙混不过熔断计数。

**采用引导。** 「动作在场」和「动作被用上」不是同一件事：在真实语料上，模型多次把一本笔记本的文档目录**完整列了出来**，然后把剩下的轮次花在无定向的检索上，而没有把这份目录变成章节。因此当大纲仍为空、而服务端手上已经握着开大纲的结构性理由时，反思上下文会与其它几份账目并排追加一行确定性的引导。它只在下列条件**全部成立**时出现：大纲闸开着；当前大纲**为空**（模型一旦建了大纲，便签本身就接管这个位置）；本 run 的引导额度（**2 轮**）尚未用尽；且存在两条结构性理由之一——本 run 已把**来源清单完整列完**且至少有 **2** 篇文档，或已确认意图给出了至少 **2** 个必答检索方向。两条都成立时用清单那条措辞，因为它带着一个真实条数。没列完的清单一律不算：按半份目录建出来的大纲天然缺节，而模型此刻并不知道自己缺了哪几节。这一行只陈述事实、点出条数，并显式给出「判断不需要就忽略」的出口——它是一条账目，不是一条命令，模型仍然可以选择一次作答。它不触发任何额外查询或模型调用、不新增动作 id，低于「穷尽」档或总开关关闭时逐字节缺席。真的发出引导的那一轮会给既有 reflect 轨迹步加上 `outline_nudged: true`；没发出的轮次，该步的 detail 键集不变。

**按节合成。** run 收尾时，若把终态大纲的绑定对到当时存活的候选池上，仍剩两节或以上带至少一个合法绑定，且该 run 没有产出集合清单（类型化枚举或结构化整表批次——清单 run 保持单次合成：清单预览与覆盖披露只进单次合成上下文，节切片装不下它们；把这种 run 节化，等于拿 ranked 样本写散文而让手上已有的完整清单闲置），答案就按节生成——每次调用只看见该节绑定证据装配出的上下文切片（复用既有的证据装配机制）。各节的号段互不相交，每节的引用标记先按该节自己的 id 映射解析、再合并——写出另一节号段的引用只可能是幻觉（模型压根没见过那个号），因此被丢弃，而不是被悄悄记到错误的那一节头上。答案正文就是各节自己的 `##`/`###` 标题按大纲顺序拼接（沿用聊天答案自身的标题字阶，与深度报告的标题字阶保持独立）。集合地图、枚举工具预览、私有 Memory 与查询期推导链都不会进入任何一节的切片——它们都不在合法绑定目标之列，一节根本没法「要」它们——在单次合成回退路径上保持原样不动。按节合成被绕过时（可装配节不足两节，或该 run 产出过清单），大纲披露不随之消失：只要大纲规划真的跑过，收尾 synthesis detail 仍带出大纲键集（`outline_sections` 为 0、`outline_fallback` 为 false），包括被略过节的标题——单侧答案不能在悄悄丢掉「问到了没找到」的那一面时显得完整。每一节都只用自己的证据切片与锚点通过 `classify_evidence` 判定。收尾 synthesis trace detail 里的 `section_grounded` 是有界的逐节记录列表（`id`、`title`、该节 `evidence_level` 与布尔 `grounded`），不是整篇布尔值，旁边另列无据节标题。响应继续复用既有三档 `evidence_level`：全部已合成节 grounded 时照旧采用全局分类结果；否则只把全局结果封顶在 `overview`，绝不向上抬。因此零节达到精确 `grounded` 档时，如果每节其实都有相关高分引用、只是模型自报保守，整篇仍可诚实保留 `overview`，不会被强制误写成 `inferred`／「未命中笔记本依据」。任一节的合成调用失败（在自己的重试之后仍失败）会丢弃整个半成品，转而回退到普通单次合成，而不是交付一份看不出缺口的残篇；若回退成功，那次失败会从用户可见的错误横幅里摘掉（事件日志仍完整记录），因为这一轮最终是恢复过来的。

**重试成功不再显示失败横幅。** 答案合成在第一次调用失败或返回空内容时会重试一次。这次有界重试现在遵循与上面回退同一条规则，而且适用于每一条问答路径、不只是按节合成内部：第二次拿到答案时，本次调用期间记下的失败会从响应的模型错误清单里摘掉，恢复过来的一轮不会在一份完整答案上方挂着「本次回答可能不完整」。两次都失败时所有报警一条不摘——包括最后那条空内容的终态报警，因为「检索到却答不出」必须可见。摘除只针对本次调用自己记的答案合成那几条，且按**工作身份**而非位置过滤：同一轮里其它工作（证据精炼、向量、重排）记下的报警一条不动——无论它记在这次调用之前还是调用过程之中，`events.jsonl` 两种情况下都完整记录。

**只经轨迹与答案自身的标题结构可见。** v1 刻意不为大纲新增任何 `AskResponse` 字段：每次大纲更新落一条独立的 `outline` 类型 trace 步（界面标签「大纲」），每写完一节落一条轻量的 `synthesis` 类型进度步，最终答案展示各节自己的 Markdown 标题。深度报告自己的逐节深挖在穷尽档同样共用这套机制——见下文「深度报告接入大纲共演化」。报告自己的「确认后冻结」研究问题合同不受影响：那里的大纲共演化只作用于节内部的检索与撰写结构，绝不触碰已确认的节/主题绑定。

**KG 弱支撑边回喂。** 大纲机制生效时，服务端还会从知识图谱本身给模型喂一条定向检索提示：每次**被接受**的 `update_outline` 调用之后，服务端看一遍模型刚绑定为证据的那些 KG 对象，找出从它们**出发**（仅出边——反向端无索引，是登记在案的残余）、且只有一两篇来源支撑的 canonical 关系——对综述类问题而言，这正是最值得继续追查的方向。总开关是 `REASONING_OUTLINE_KG_GAP_ENABLED`（默认 true），叠在大纲总闸之上；两者任一关闭都是零额外查询、prompt 逐字不变。提示通过模型已有的动作（`add_subquery` / `follow_chain` / `expand_graph`）生效——零新动作、零新模型调用、零 schema 变更。它绝不进入答案正文或引用，只是给检索用的便签指引。数值上限——支撑来源数阈值、探测行数上限、seed 上限、每轮行数、单行/整段字符界——是契约值，统一列在下表，这里不重复。每轮 `outline` 类型 trace 步在被接受的 apply 新入队候选时会带上整数字段 `kg_gap_candidates`（终态纠错轮算出的候选只入账、不渲染——那一轮自己的便签正文写着不得执行检索）。若 canonical 关系层缺席（从未构建过）或探测本身抛出异常，该特性对当轮静默且无害地缺席——KG 提示是锦上添花，绝不能成为整个 run 失败的理由。

| 配置项 | 数值 |
| --- | --- |
| 弱支撑阈值（支撑来源数） | ≤ 2 |
| 每次 apply 的探测行数上限 | 24 |
| 每次 apply 的 seed 上限 | 96 |
| 每轮 reflect 提示行数 | ≤ 6 |
| 单行字符数 | ≤ 80 |
| 整段字符数 | ≤ 520 |

### 深度报告接入大纲共演化(研究深度 ↔ 检索档位,PR-5)

深度报告的「研究深度」滑块与逐步推理「检索档位」选择器共用同一批档名。每节深挖（`_deep_dive`）现在把自己的 `depth` 值映射到与 `ask_retrieval_limits` 相同的档位预算，而不再是不论滑块位置永远按 `standard` 预算跑（PR-5 接入前的行为）。映射按阈值判定而非固定字典，因为接口把 `depth` 夹在 `[1, 16]` 的任意整数，不只是滑块的五个停靠点：

| depth ≥ | 检索档位 |
| --- | --- |
| 1 | overview（概览） |
| 2 | standard（标准） |
| 4 | deep（深入） |
| 8 | thorough（深度） |
| 16 | exhaustive（穷尽） |

处于中间值的 depth（如 3、5、7、15）落到更低的那一档，不向上取整。每节自己的反思步数上限（`max_steps`）仍等于报告自己的 depth 值（1/2/4/8/16），绝不采用检索档位表自身的步数上限（4/8/16/32/50）：报告的成本按节数放大，把某一档的步数上限乘上每一节不是用户在滑块上同意的那个量级；两者中更紧的那个数字始终生效。

**行为变化，显式登记。** 因为此前不论 depth 取值，检索预算都固定在 `standard`，所以低档位（1、2）现在的检索预算比接入前更小，高档位（8、16）现在的检索预算比接入前更大。这是把同名档位的语义对齐（同一个档名在 Ask 与深度报告两处买到同一份相关性/上下文预算）的修复，不是回归。

**档位同样缩放大纲阶段的 0-LLM 探针宽度。** 覆盖探针（逐必答主题）与充分性探针（逐草拟节）各自最多执行按档位定的查询数——`overview` 2 条、`standard`/`deep` 3 条、`thorough`/`exhaustive` 4 条（接入前不论深度恒为 4；报告行上缺 depth 时保持历史宽度）。默认档刻意保 3：确认问题恒居每个主题查询列表之首，宽度 2 只剩 1 条主题专属探针，而充分性判定的阈值对样本量敏感。登记的后果：低档喂进同一套充足/薄弱/缺失阈值的探针命中更少，其大纲充分性结论会比历史宽度 4 时读起来更保守。探针产出只喂给 STORM 规划器与充分性 Judge 的覆盖计数，绝不进报告正文证据，所以低档换到的是规划接地的粒度而不是证据——这正是低档在大库上提速的语义。同一次规划 run 内重复的探针查询（确认问题按设计排在每个主题查询列表之首，主题/节之间也常共享检索方向）只检索一次并记忆化；memo 不跨 run 存活、失败不缓存。大纲阶段的 LLM 调用同样随档位下调：意图理解与 STORM 各档照跑，充分性 Judge 的 **LLM 精修半**只在 `deep`/`thorough`/`exhaustive` 运行——`overview`/`standard` 保留 Judge 的确定性半（大纲编辑器显示的每节 coverage 计数与充足/薄弱/缺失结论），这与 Judge 既有 fail-open 路径的产出逐字一致（LLM 半本就只降不升、模型失败时整段跳过），且人工大纲确认门紧随其后。「多视角规划大纲中」与「大纲就绪」之间现在还会上报「检查各节证据充分性」，STORM 之后的探针段从此可见，不再呈现为卡死。每节深挖还会把**节复合问题作为第一条种子**（镜像 Ask：完整权威问题恒为首条），随后接该节经用户确认的检索方向（`intent_queries`）注入推理 run，从而跳过每节一次的规划 LLM 调用——已审阅的方向集是权威，不再让第二个模型重新解读；reflect 循环及其证据驱动的补查原样保留，run 步数预算装不下的方向仍由 run 后合并兜底执行，而 run 内检索**本身失败**的方向（账目上的稀疏 `failed` 标，与合法的零命中探针不同）同样由该合并重新执行，不会静默丢证据。

**档位管的是整节，不只是 `run()`。** 检索之后还有两段此前不看滑块、按定值跑，等于 `run()` 刚兑现的档位又被送了回去。两段现在同样随档位缩放：(a) 按方向补检索的**合并**——每条已确认检索方向仍然逐条真执行，但每个方向的**取数**按该档的 `ranked_per_query_take` 与元素额度（不再是固定的 20 + 8），合并结果再按该档的 `ranked_final_cap` 与 `answer_element_items` 重新截断（相关度降序，元素 tie-break `element_id`），4 个方向不再能把概览档的报告顶到它自己的上限之外。被大纲绑定、但**本来就在选集里**的对象优先占用上限席位而不是豁免上限（豁免会让总数越过上限），只有 `outline_evidence` 那批补集在上限之外——与它们在 Ask 侧位于 `top_hits` 选集之外是同一条规则。(b) 节撰写上下文——KG 块用 `kg_context_chars`，原文块与直接原文段**共享** `chunk_context_chars`（原文段吃 chunk 用剩的额度，且最多 `answer_element_items` 条进 prompt，按相关度择优而非插入序），不再是 `ANSWER_CONTEXT_BUDGET_CHARS` / `REPORT_SECTION_CHUNK_BUDGET` 那对定值外加一份 1/3 的元素额度。共享的来源分区同样给大纲留位置：绑定的 chunk 排在最前（该渲染器逐 chunk 独立，重排是安全的），绑定的原文段按自己的实际长度预留额度（上限为分区的一半）——否则 chunk 先吃满这份共享预算，绑定的原文段拿到的就是 0。大纲绑定的**元素**优先占用条数上限的席位并排在最前（上限本身仍是闭的——一份大纲能绑的键远多于该档允许的条数），与 KG 侧对绑定对象的规则一致——绑定键横跨三个候选 id 空间，被条数上限截掉的绑定元素与被截掉的对象一样会在「发现的结构」里丢掉自己的 `[k]`。查询期推导链与 confirmed Memory 按 KG 块用剩的 `kg_context_chars` **整块**准入（两者都自带硬上限，截半块会把一个 `[k]` 标记切断）；没被准入的块也不进证据映射。大纲绑定对象的优先额度在**一次** `knowledge_context` 调用内完成（`priority_object_ids`/`priority_budget_chars`），绝不由调用方拆成两次：该块末尾的 `relations:` 行是对一次调用自己的证据集内部求的，拆开会把所有跨两半的边静默丢掉。

**大纲便签与 KG 弱支撑边回喂在 depth=16（穷尽）时自动激活。** 因为 `outline_wiring_active` 只判 `limits.effort == "exhaustive"` 与 `REASONING_OUTLINE_ENABLED`（见上文）两个条件，报告经 depth 映射到达穷尽档时，每节深挖里会原样激活同一套大纲便签、`update_outline` 反思动作，以及（`REASONING_OUTLINE_KG_GAP_ENABLED` 开启时）弱支撑关系提示——零新增开关、报告侧零专属接线。集合枚举工具在这条路径上仍不可达：报告构造 `ReasoningRetriever` 时不传 `collection_catalog`/`collection_enumeration`，枚举闸不论档位都保持关闭。

**「发现的结构」块（仅节内生效，绝不回写已确认大纲）。** 当某节深挖整理出非空大纲便签时，`_deep_dive` 把终态子大纲连同各子节绑定的证据 key 折成一段有界的「发现的结构」块（≤12 行、行 ≤80 字符、整块 ≤1200 字符；超界按顺序截断并显式记账 `(+N 子节略)`，不静默丢行），作为 `discovered_structure` 传给 `report_section_prompt`。prompt 教撰写模型：这只是一条**建议，不是合同**——可以用 `###` 子标题按此结构组织本节正文，缺证据的子话题必须如实略过，且不得越出本节自己的范围。它绝不增删改用户确认过的节，也绝不触碰 `reports.outline_json`——报告自己确认的大纲（必答主题、节绑定）不受影响。低于 depth 16，或某节深挖没整理出大纲时，该块缺席，不会向这些报告注入「发现的结构」指令。

**节级进度文案。** 某节深挖期间，它的实时 `section_status` phase 文案会从笼统的「深挖」细化为「深挖中（已整理大纲 N 节）」——一旦大纲便签持有至少一节，在观察到 `outline` 类型 trace 步时立即生效并**强制**落库（不等到下一个检索步；大纲步紧跟在刚推过节流窗的 reflect 步之后，且常常是本节最后一个推理动作，走节流的话这次写会被随后强制写的「撰写」盖掉、用户永远看不到）——不新增表列、不新增 SSE 事件，其余写入仍复用既有 2 秒节流持久化。

### 深度报告可信度与综合

这份报告合同防止把相关性排序得到的技术扫描伪装成完整、独立或全篇综合的结论。冻结的报告 understanding 使用共享意图结果，包含 `result_scope` 和 `completeness_required`。真正的报告侧集合枚举尚未接入前，范围为 `complete`、`aggregate` 或 `hybrid` 的请求必须说明它按相关性检索生成、未做完整枚举。假设仍是可见的范围默认值，但绝不算证据，也不进入检索串。

规划、报告「资料基础」披露和界面共用同一份持久化、有界画像：它由数据库聚合和一页有界代表来源生成，不再把每份来源逐行载入应用内存。画像披露可见/展示来源数量、来源类型/年份分布（含年份未知）、保守重复膨胀下界/身份不确定性，以及按已有类型和年份元数据分层选取的代表资料。完整的 source→family 映射不再复制进意图合同、大纲、轮询响应或模型 prompt；只有覆盖探针、主张或引用实际触达的来源 id 才会一次性有界解析。解析出的行只可合并非空且相同的文件哈希、已接地且相同的论文标题，无法解析或身份不确定的资料保持分开。它不承诺 DOI/arXiv/标题/文件族的完整 canonical 化，也不伪造全库精确资料族数。充分性判断依据相关的可区分资料组、相关性和已确认方向的分布；抽取对象与元素命中数只是诊断量，不是独立权威。画像缺席有两种互不相干的原因，读者必须能区分：本次运行**收窄了来源范围**时有意跳过全库聚合，而聚合本身也可能出错。两者都持久化 `unavailable_reason`（`scope_restricted` / `failed`）而不是一个空画像，报告正文与界面各自给出对应说法，只有真失败才发不含资料内容的 `report_corpus_profile_failed` 运维事件；两种情况报告都继续 fail-open。不可用标记是**非空**对象，所以每个消费方都必须显式判定而不能靠真值判断——把它当画像格式化会把每个计数渲染成 0，并把一份从未测量过的语料摘要送进规划。历史报告存的是空画像，事后无法归类，因此保持原来的失败说法。画像只统计**当前笔记本**，而检索是跨已挂载参考库的，所以披露另行说明正文实际引用了多少份参考库资料：该数由已装配好的引用推导（零新增查询）、按**来源**去重而不是按锚点计数。没有这一句，「基于 N 份资料生成」会被读成证据的全部，即使多数引用来自挂载库。

参考文献仍保留可点击的精确锚点。分组只是展示层：身份未解析的每份资料仍按自己的 source id 独立可见，不会被压成共享的「未知」条目。因此正文披露锚点数和**可见来源组数**；可信度回执另行显示更保守、仅计身份已确认资料的**可区分资料数**，并披露 Top-1 锚点占比上界和重复膨胀率。身份未解析的锚点不增加独立资料数；计算 Top-1 上界时，先将所有这类锚点按最不利情况归入已解析资料中占比最高的一族，再除以全部锚点。「引证覆盖率」明确标为**高风险断言引证覆盖率**：确定性扫描器只检查可观察形式（带单位数字、`O(...)`、显式排序/最高级、绝对比较）在同句或同表格行是否有合法 `[k]`。标题、代码、纯公式块、章节/图编号和已标记 `（推断）`/`【通识】` 的文字不计入；英文按各自句号边界审计。它不能证明引用是否语义蕴含断言。审计与披露始终执行，但证据等级降级另受 `REPORT_HIGH_RISK_DOWNGRADE_ENABLED` 控制，默认关闭，待真机分布校准后再决定默认值；开启时，仅当未引证比例严格大于 `REPORT_HIGH_RISK_UNSUPPORTED_RATIO`，grounded 章节才会被封顶为 `overview`。

对于比较、综述、分类形请求，规划可给出由正交 facet 和条件 axis 组成的有界可选框架。用户在大纲旁查看、编辑它，确认后节级副本即为权威，意图合同内嵌副本只作兼容镜像。**所有**报告深度的章节执行都是「并行检索 → 至多一次全局综合调用 → 并行撰写 → 只审计终审」。唯一的闸是章节数：一节报告不存在跨章节一致性，因此跳过综合并保留逐节 `检索→撰写` 流水线；深度只决定检索预算。这是明确的取舍——低档位就此失去逐节流式（已就绪章节现在要等最慢检索），换取每份多节报告一次全篇综合。综合蓝图分配中心答案、共享定义、带证据键的主张（含条件/反证）和章节 owner/不重复交接。撰写者接收的是主张而非按文献顺序堆放的证据，必须先给结论、区分共识/分歧与条件，并避免逐篇复述或跨论文的不可比排序。主张账本把实际输出的主张文本及锚点绑定到同句/同表格行。接地是**逐行**的，不是整节的：这是对该节已输出正文的事后审计层，不是撰写输入，所以一行的主张文本不是正文原文、引用键非法或不出现在同一句话里、或未通过其他逐行检查时，只丢弃**该行**、账本其余部分保留——没有任何下游逻辑把主张行互相拼接，丢一行不会污染留下的行。主张 id 不要求蓝图预先声明：一行覆盖了撰写者在任何承诺之外补写的正文，是合法的新主张，不是违规。超过 24 条上限的提交会先截断为前 24 行再逐行校验，第 25 行及之后的溢出行不会连坐前 24 行。账本状态四选一：`missing`（模型未返回列表）、`invalid`（列表为空，或每一行都被丢弃）、`partial`（部分行保留、部分行被丢弃或截断掉）、`available`（截断之后提交的每一行都保留）。可选的 `frame_assignments` 分类标签是组织性标注而非证据接地，改为逐条降级：未知 facet 键、不在该 facet 声明取值内的取值，或非对象载荷都只丢掉那一条标签，该行其余部分照常可用。声明取值之外的取值一律丢弃，绝不向最接近的声明取值靠拢。趋势主张的语气资格按所引可区分资料数确定性封顶：一份只能作为研究性方向，两份可标为发展中，三份及以上才有资格使用高置信语气；任一被引锚点缺少可确认的来源身份时，整个趋势只能标作研究性，而不能把未知锚点算成独立资料。正文强于该口径时会进入局限披露。终审读取框架、已校验蓝图、为每节预留份额且只按完整 JSON 主张记录裁剪的合法有界上下文、高风险审计和 exclusive facet 冲突，审计一致性、趋势语气和局限，但绝不改正文或添加事实。界面会在这些信号有信息量时显示综合状态（`available`、`skipped_no_evidence`、`failed_model`、`failed_validation`）和可用章节账本数（`available` 加 `partial`），并点明这些可用账本里有多少条是 `partial`（有行被丢弃或截断）：单节报告会隐藏预期的纯否定回执（`not_requested` 且账本为 `0/N`），综合还闸在 depth ≥ 8 时生成的历史报告同样隐藏（载荷里没有任何字段能给报告定代）；多节 no-op、任何跳过/失败和任一可用账本仍会显示。模型/校验失败会进入错误日志并回退独立撰写，无证据跳过不伪报为模型错误。

frame、blueprint 或 claims 账本缺失/畸形时会丢弃新增结构，回退到此前报告路径，绝不令报告失败。没有单独的 frame 校验调用；全局综合是唯一新增模型调用，在所有深度下每份多节报告各跑一次。

| 上限 | 数值 |
| --- | ---: |
| Frame facets / 每 facet values / axes | 8 / 12 / 8 |
| Blueprint 共享定义 / 主张 | 24 / 60 |
| 每节 writer ledger 主张 | 24 |
| 综合证据字符数 | 36,000 |
| 终审输入字符数 | 24,000 |
| 资料基础代表来源 / 类型或年份分布桶 | 20 / 32 |
| 单次按需来源身份解析 | ≤ 1,024 个实际触达 source id |
| `REPORT_HIGH_RISK_UNSUPPORTED_RATIO` | 0.25（严格大于才超限） |
| `REPORT_HIGH_RISK_DOWNGRADE_ENABLED` | false（审计披露仍启用） |
| 每报告新增模型调用 | ≤ 1，任意 depth，≥ 2 节 |
| 触发全篇综合的最小章节数 | 2 |

## 引用附图（本段附图）

**图注（或图片描述）是唯一入口。** 图片元素只有在带非空图注（`metadata.caption`）或非空图片描述（`metadata.description`，即 markdown 的 `> **图片描述**` 引用块；两者都见上文摄取规则）时才会进入分 chunk / 检索——两样都没有的图片压根不可检索，这是刻意的设计，本特性不为它补一条「就近带出」的救济路径。当某条带图注的图片被证据检索命中并进入回答绑定时，该条引用会在旁边带出这张带图注的图片，用一个与引证正文视觉分隔、标注为「本段附图」的独立区块呈现——模型没有看过这张图；这只是响应装配层的增强，检索/证据判定管线本身不变，附图也不是一种新的证据形态。

**响应形状。** `CitationImage`（`{element_id, asset_id, caption}`，图注做截断）是一个有界列表字段，同时挂在 `AnswerAnchor.images` 与 `Citation.images` 上（空列表按 `exclude_if` 惯例整体从 JSON 缺席，因此一条没有附图的回答不会多一个字节）。两处都要有：逐步推理模式的权威显示路径是 `[k]` 锚点，不是 `Citation` 回退列表，只加在 `Citation` 上会让主路径永远看不到图。旧持久化答案缺这个字段时按纯文本渲染——零 migration。

**是装配增强，不是新的检索通道。** 一次共享的批量主键读取（`evidence_context.py`）拿一批候选 element id，过滤出 `element_type='image'` 且 `metadata.asset_id` 非空的行并返回映射；不产生任何新增模型/embedding 调用。它接在四处已经持有完整证据对象的装配点：`ask_chunk` 的 chunk 引用（按该 chunk 完整的 `element_ids` 反查，而不只是可能为空的 `anchor.element_id`——「一段正文 + 一张配图」恰恰是多元素 chunk 的典型形态，只看 `element_id` 会漏掉图）、`ask_reasoning` 的锚点装配（chunk/element/KG 锚点各自提供自己的候选 id）、以及 `ask_graph` 的两处 chunk 引用。天然被排除、无需特判：knowhow 格子投影行 `element_type` 固定为 `knowhow_cell`，永远不命中 `image` 过滤；Memory 派生源虽然会解析出图片元素，但摄取路径刻意不为它接 `persist_image` 闭包，这些行永远没有 `metadata.asset_id`。

**预算是协议边界，不是部署设置。** 它约束的是响应体积和界面视觉噪音，与语料规模、检索档位无关，所以不像 `Settings` 承载的部署旋钮那样可调，而是具名常量：每锚点/引用上限、每次回答的总上限（按**已分配的位**计——同一张图被 5 个锚点引用就占 5 个位，是响应体积的上界而不是「用户能看到几张不同的图」）、更大的每**报告**上限（报告的参考文献天然比一次回答的锚点数更多），以及一个图注截断长度。四处装配的候选先去重、再按 element id 升序排序后再截断，确保同一个问题问两次得到同一批图。精确数值见下表。

**深度报告的装配刻意是独立的一次调用。** `report_engine.py` 在**每份报告一次**——全部章节撰写完成、`references` 已经是最终全局重编号的 `k1, k2, …` 形状之后——才附图（绝不按章节各调一次，那会让每节都能吃到一整份按报告计的预算），且这次调用 fail-open 包裹：图片查询异常只丢图，绝不影响已经生成好的报告正文（与既有 `_resolve_source_families` 的 fail-open 惯例一致）。每条参考文献的候选 id 取自身 `element_id` 与其底层 chunk 完整 `element_ids` 的并集，与 Ask 侧规则逐字一致；报告的引用 ctx 里压根没有 `memory_id` 这个概念，Memory 来源引用天然被排除，无需显式过滤。

**刻意排除的两处。** 报告公开分享页的引用白名单（见上文「群组知识共享/报告公开链接」）从不投影 `asset_id`/`element_id`——与该页面挡掉所有其他内部句柄的边界完全一样——所以一份被公开分享的报告永远不带图，即便底层 `ReportDetail.references` 带了图。管理员活动日志（`/dev/logs` → 活动）的引用详情复用同一套引用详情组件与其既有的防御式回退（拿不到可解析的 `notebookId` 就不渲染图片资产 URL），因此自动降级为纯文字引用，无需为它另写代码。

**前端。** 该区块复用既有的鉴权、按视口懒加载的 `AuthedImage`：弹层/详情面板没打开就不会发出任何图片请求。Ask 侧的锚点弹层与引用详情面板支持点击打开来源或放大；深度报告的引用详情面板刻意**不**接这个跳转（v1 里报告的引用详情面板本就没有「打开来源」的交互——这是与 Ask 侧刻意的不对称，不是遗漏）。

| 上限 | 数值 |
| --- | ---: |
| `CITATION_IMAGES_PER_ANCHOR`（每锚点/引用附图数） | 3 |
| `CITATION_IMAGES_PER_ANSWER`（每次 Ask 回答已分配位数） | 12 |
| `CITATION_IMAGES_PER_REPORT`（每份深度报告已分配位数） | 24 |
| `CITATION_IMAGE_CAPTION_CHARS`（图注截断长度） | 200 |

### Markdown 压缩包上传护栏

| 上限 | 数值 |
| --- | ---: |
| `MD_BUNDLE_MAX_ENTRIES`（压缩包保留条目数，去掉目录条目/`__MACOSX` 资源叉/精确重复项之后） | 2,000 |
| `MD_BUNDLE_MAX_DECLARED_ENTRIES`（EOCD 声明条目数的扫描前上限，`MD_BUNDLE_MAX_ENTRIES × 4`） | 8,000 |
| `MD_BUNDLE_TOTAL_BYTES_FACTOR`（解压后总字节上限系数，`× source_upload_max_bytes`） | 4 |
| 压缩输入上界（读进内存**之前**按 `File.size` 判定）：`min(source_upload_max_bytes × 4, 绝对顶) + 容器余量`。解压预算内 + 有界容器开销的包绝不在此被拒；压缩态超过绝对顶的归档一律拒绝——已登记的浏览器安全取舍（请把 md 拆出直接上传） | 见公式 |
| `BUNDLE_ZIP_INPUT_FALLBACK_CAP_BYTES`（双重身份：`source_upload_max_bytes` 未到达时的回退值——刻意不沿用「不预检」；**同时**是绝对浏览器安全顶——顶配部署按 `× 4` 公式会放行 4 GiB 整包分配；取值 = `SOURCE_UPLOAD_MAX_MB` 的协议最大值） | 1,024 MiB |
| `BUNDLE_ZIP_INPUT_OVERHEAD_SLACK_BYTES`（zip local/central 头、EOCD 与不可压数据 deflate 微膨胀的有界余量，保证贴着解压预算的合法包不因容器字节被误拒） | 4 MiB |
| `MD_BUNDLE_MAX_SUGGESTIONS`（每张未匹配图片给出的近似候选路径数） | 3 |
| `BUNDLE_DIR_MAX_DEPTH`（拖入文件夹的遍历深度） | 16 |
| `BUNDLE_DIR_MAX_FILES`（拖入文件夹的文件数上限，与 `MD_BUNDLE_MAX_ENTRIES` 同值） | 2,000 |
| 文件夹总量上界（读任何文件内容**之前**按累加的 `File.size` 判定）：`min(source_upload_max_bytes × 4, 绝对顶)`——与上面压缩输入上界共用同一条解压后总量线与同一个绝对顶，但**不加**容器余量（文件夹条目的 `File.size` 本就是真实内容字节数，没有 zip 头/deflate 微膨胀这层容器开销要抵消） | 见公式 |
| `INLINE_TOO_LARGE_IMAGE_LINES`（超限时逐条列出的图片明细行数，按体积降序） | 3 |
| `BUNDLE_STAGE_FALLBACK_MAX_FILES_PER_BATCH`（`source_upload_max_files_per_batch` 尚未到达时，**内联之前**那道名额闸的回退值；取值 = 后端固定的 `SOURCE_UPLOAD_MAX_FILES_PER_BATCH`） | 20 |

### 报告公开分享护栏

| 上限 | 数值 |
| --- | ---: |
| `REPORT_QUESTION_MAX_CHARS`（**创建**报告时研究问题的字符上限；超限 422 拒绝、绝不裁短了存，前端 compose 框同值护栏——按 Unicode 码点数与 Pydantic 对齐，超限拦住提交并说清，不替用户裁剪） | 4,000 |
| `MAX_REFERENCES`（单份报告投影的引用上限；超出部分披露为 `truncated_references`） | 500 |
| `MAX_REFERENCE_TITLE_CHARS`（每条引用标题/原始文件名字符上限） | 400 |
| `MAX_SNIPPET_CHARS`（每条引用摘录字符上限） | 1,200 |

报告正文（`content_md`）**原样返回**。研究问题在 `REPORT_QUESTION_MAX_CHARS` 以内也**原样返回**——而创建端点已经拒收更长的输入，所以**今天能建出来的每一份报告，问题都是完整的**。问题此前被截到 2,000 字符，静默丢掉的正是产生这份报告的那段文字（公开页发的是 `reports.question`，即**创建时**那一份——确认门只把编辑后的 `resolved_question` 写进 `understanding`，从不回写这一列）。

超过上限只可能出现在**创建期护栏上线之前**建的报告上（它们的分享链接已经发出去了）：这时投影按上限截断并置 `question_truncated`，公开页显示「（研究问题过长，已截断）」。这样投影**自身有界**（否则匿名响应会被客户端输入撑到无界），同时不静默丢尾、也不改写用户已经存下来的数据（刻意不做数据迁移）。注意有界性的论据**不是**「正文本来更大」——`content_md` 是模型生成、受生成预算约束，而问题是原始客户端输入。

前端那半护栏**按 Unicode 码点判**（与后端 Pydantic 同一把尺；`<textarea maxLength>` 数的是 UTF-16 code unit，含 emoji 时会在半数处就停手，两边号称「同一护栏」却对不上），且**超限只拦提交、不动输入**：粘进来的全文原样留在输入框里，提示写明「超出 N 字上限（当前 M 字）」并禁用「生成深度报告」，绝不替用户把尾巴删掉——那正是同一条红线要防的静默截断。引用的标题、原始文件名与摘录是证据元数据、仍受上限约束，但**超限会置 `title_truncated`/`file_name_truncated`/`snippet_truncated` 披露**（公开页显示「已截断」提示），不静默丢尾。`key`（24）、`location`（200）与时间戳（64）刻意不披露截断：它们是服务端派生的标签（`kN`、`PDF p.3`、ISO 时刻），没有用户自撰的尾巴可丢。

后三个上限定义在 `backend/app/services/report_public_view.py`，创建上限定义在 `backend/app/models/reports.py`（前端镜像在 `frontend/app/report-api.ts::REPORT_INPUT_LIMITS`）；与下表的会话侧同名常量彼此独立（两条分享链路各有自己的契约），但截断披露这条口径必须一致。

### 问答会话公开分享护栏

| 上限 | 数值 |
| --- | ---: |
| `ASSET_ALIAS_HEX_CHARS`（单个资产别名长度：HMAC-SHA256 截断后的十六进制字符数） | 32 |
| `MAX_REFERENCED_ASSETS`（图片端点单请求扫描的不同资产上限；必须 ≥ `MAX_TURNS × CITATION_IMAGES_PER_ANSWER`——由 `test_endpoint_scan_cap_covers_every_alias_the_projection_can_emit` 守卫，否则晚轮次图片的别名会永远解析不到） | 6,000 |
| `MAX_TURNS`（单页公开页渲染的最多轮数；超出部分披露为 `truncated_turns`） | 500 |
| `MAX_REFERENCES`（每轮引用上限） | 500 |
| `MAX_REFERENCE_TITLE_CHARS`（每条引用标题/原始文件名字符上限） | 400 |
| `MAX_SNIPPET_CHARS`（每条引用摘录字符上限） | 1,200 |
| `MAX_CAPTION_CHARS`（每张图 caption 字符上限） | 500 |
| `ASK_QUESTION_MAX_CHARS`（**提交**提问时的字符上限，定义在 `backend/app/models/ask.py`；超限 422 拒绝、绝不裁短了存） | 4,000 |
| `CONVERSATION_TITLE_MAX_CHARS`（**重命名**会话时标题的字符上限，定义在 `backend/app/models/ask.py`；超限 422 拒绝、绝不裁短了存） | 200 |

每轮问题**与会话标题**均**原样返回、不截断**：与 `answer_md` 同理，它们是用户自撰的 artifact，静默截断会丢掉产生该答案的原始问题、或截断给会话命名的标题。引用的标题、摘录与原始文件名是证据元数据、仍受上限约束，但**超限会置 `title_truncated`/`snippet_truncated`/`file_name_truncated` 披露**（公开页显示「已截断」提示），不静默丢尾（codex #522 R3/R4）。

「原样返回」之所以仍是**有界**承诺，全靠写入侧拒收超长文本，问题与标题**各有一条**闸：

- **问题**：`AskRequest.question` 带 `max_length=ASK_QUESTION_MAX_CHARS`，因此 `POST /notebooks/{id}/ask`、`POST /notebooks/{id}/ask/stream` 与 `POST /notebooks/{id}/ask/intent` 超限一律 422，前端提问框同值拦住提交。**MCP 工具 `ask_notebook` 同样执行这条闸**，并给自己的可读文案而不是抛一个裸校验错误：长期 token 的 Agent 客户端此前可提交任意长度的问题，现在会收到 `question too long: … the maximum is 4,000 …`。这是一次**刻意**的行为变化——MCP 客户端与浏览器一样是写入侧，而超过这个长度的材料应当作为来源上传、而不是塞进提问。
- **标题**：`ConversationRenameRequest.title` 带 `max_length=CONVERSATION_TITLE_MAX_CHARS`，`PATCH /conversations/{id}` 超限 422。重命名是标题唯一能超过服务端自动取的前 60 字的途径，所以这一个端点就是它的全部写入侧；MCP 面没有重命名工具，无需对等。取 200 而不是 4,000：那把尺是给**问题正文**定的，会话标题是一行标签。

两条闸的前端都按 **Unicode 码点**数与 Pydantic 对齐、超限当场说清并拦住提交，**绝不替用户裁剪**（`ASK_INPUT_LIMITS`）。少了写入侧这半，匿名响应就不受客户端输入约束——正是 codex #525 R1 P2 对报告投影提的那条。

仍有一处明知未闭合、如实登记而非粉饰的缺口：两条闸上线**之前**写入的行（更早的轮次可带更长的问题，更早改过的标题可更长）。要在投影里限住它，需要给该轮加一个披露字段并改公开页——报告侧 `PublicReport.question_truncated` 付的正是这个代价——因此列为独立工作，不用一次静默截断糊过去。

前七个上限均定义在 `backend/app/services/conversation_public_view.py`。

## API

当前 beta 的关键 API：

- `GET /api/notebooks`、`POST /api/notebooks`、`PATCH /api/notebooks/{id}`、`DELETE /api/notebooks/{id}`
- `GET /api/notebooks/{id}/analytics`
- `GET /api/notebooks/{id}/analytics/content-overview` —— 面向当前查看者的内容资产：`memory`（`total`、`confirmed`、`candidate`，最多三条最近 `id`/`title`/`status`/`updated_at`）与 `knowhow`（`table_count`、`row_count`、`projection_pending`、`projection_failed`、`stale_code_count`，最多三条最近表摘要）
- `GET /api/notebooks/{id}/checkup` —— 流水线体检（只读，看板高频入口）：聚合来源与索引的损坏/待办信号——空源、缺检索片段、缺检索向量、待分析来源、检索索引过期/损坏——每项含数量、命中样本与建议修复动作，健康时全为 0。看板「来源状态」「索引与构建」两块与头像旁铃铛消费它；健康的库保持中性、不打扰。两项「缺检索向量」的数量走进程内短时缓存，**计数至多陈旧 30 秒**——补齐完成后可能要再等一轮轮询才归零（修复按钮也因此多按住一会儿，是刻意的）。
- `POST /api/notebooks/{id}/sources/reparse` —— 体检修复：批量重新解析指定来源（空源/缺片段），后台复用既有解析管线，按 notebook 作用域过滤入参
- `POST /api/notebooks/{id}/backfill-vectors` —— 体检修复：后台补齐该库缺失的检索向量（只补缺失、幂等，仅嵌入、不动解析）
- `POST /api/notebooks/{id}/paper-meta/backfill` —— owner 触发的论文元数据补抽（后台、幂等可续跑），返回 `{queued}`；LLM 未配置 409。来源面板的「补全论文信息」按钮**只在确有活可干时显示**：`NotebookSummary` 的 `paper_meta_missing`（仅单库 `GET /api/notebooks/{id}` 精确回填，按补抽排队同口径的 EXISTS 探针计算；列表投影与旧后端为 `null`＝未计算）为 `false` 且当前可见来源页没有 `paper_meta_status="missing"` 的行时隐藏；`null`/缺失按旧行为继续显示（隐藏只能由显式的 `false` 触发），补抽运行期间保持可见以承载「补全中…」态
- `GET /api/system/config` —— 登录后可读的非敏感浏览器配置；当前返回 `source_upload_max_bytes`（来源选择器使用的部署上限字节值）、`source_upload_max_files_per_batch`（固定的单次请求文件数护栏），以及供 Markdown 压缩包上传配对预检使用（见上文「引用附图（本段附图）」）的 `source_image_max_bytes` / `source_image_max_per_source`（镜像 `MINERU_MAX_IMAGE_BYTES` / `MINERU_MAX_IMAGES_PER_SOURCE`；旧后端缺字段时为 `null`，含义是「拿不到这个上限，交给服务端护栏兜底」；`0` 是合法值，语义是「一张都不持久化」，等效于图片存储关闭）与 `source_images_enabled`（镜像 `MINERU_RETURN_IMAGES`；缺字段按 `true` 处理，因为该开关此前从不存在，不能让旧部署凭空弹出假警告）
- `GET /api/system/extensions` —— 登录后可读的 build-time workspace UI contribution 元数据投影。响应只含 API version、稳定的 plugin/display/version/contribution 标识、实时 availability，以及 `disabled | unavailable | null` 三态固定原因；绝不下发 capability 名、依赖/信任拓扑、endpoint、路径、凭据或异常文本。production 已把既有 Agent Profile 入口注册为 `workspace.side_panel` 的 `builtin.ask_agent_profile.workspace_panel`。浏览器只在 workspace 成功提交后按 actor generation 读取一次；集合页和未登录不调用，同用户切库复用，旧后端/缺行/不可用均 fail closed。插件入口点击前不读取 Agent Profile 数据。
- `GET /api/admin/extensions` —— 仅系统管理员可读的已加载部署插件拓扑只读投影（每个插件恰好 6 个白名单字段）；见[部署插件](#部署插件)。
- `/api/extensions/{plugin_id}/…` —— 部署插件自有 HTTP 路由的唯一挂载面，经 router 级会话认证；见[部署插件](#部署插件)。
- `SILICON_NOTEBOOK_UI_PLUGINS` 是纯前端构建期输入（`:` 分隔的本地插件包目录列表，默认未设置/空），只被 `frontend/scripts/sync-ui-plugins.mjs` 消费，绝不会到达运行中的后端进程。该脚本写出 `frontend/.local/ui-extension-contract.json`——一份部署期对账输入（不是前端或后端任何一侧的运行时依赖），形状为 `{api_version, contributions: [{plugin_id, version, contribution_id, slot, capability}, ...]}`，与 `backend/tests/fixtures/ui_extension_contract.json` 同形同排序键 `(plugin_id, version, contribution_id, slot, capability)`：内容就是那份 fixture 的内建行，拼上各已配置包 `ui-plugin.json` 里的行。浏览器侧，插件被注入的 `actions.api` 端口把每次请求限定在 `/api/extensions/{plugin_id}/` 之下，并剥离插件自带的 `authorization`/`cookie` 请求头，固定 `tag`/`auth`/`unauthorized`，插件无法覆盖它们。插件自己的后端路由除真正的会话失效外绝不能返回 401：端口恒设 `unauthorized: "clear-and-reload"`，裸上游 401（例如插件后端依赖的某个第三方凭据过期）会把已登录用户的 token 清掉并整页刷新——插件后端必须把上游 401 翻成 `502`/`424` 再返回。无正文的响应必须经 `requestVoid` 读取，不能用 `requestJson`——后者会对响应体调 `.json()`，遇到 `204`/空体当场抛解析错误。`GET /api/admin/extensions`（仅管理员）支撑新增的只读管理员页面 `/admin/extensions`，列出运行中后端实际加载的扩展拓扑——内建与部署注册的 contribution，检索类与 UI 类都在内——供运维查看；它与 `SILICON_NOTEBOOK_UI_PLUGINS` 无关，后者只影响前端构建。
- `POST /api/notebooks/{id}/sources` —— multipart 文件上传（异步解析/抽取）。每个文件在 multipart 流写入临时 spool 时即受限，超过 `SOURCE_UPLOAD_MAX_MB`（默认 50 MiB）返回 413；每次请求超过 20 个文件也返回 413。浏览器读取上面的两个护栏，取得前禁用文件输入，选择时即时拒绝超限文件，并在发送前复查暂存文件。每个被接受的文件以 `{source_id}_{净化后的客户端文件名}` 落盘；该组件整体压进文件系统 255 字节的单组件上限（按 UTF-8 字节截断主干、保留扩展名——浏览器允许客户端提交最长 255 字节的文件名，加上 37 字节的 id 前缀会在 ext4/XFS/NTFS 上超限导致上传失败）；被压缩的只有派生的磁盘名，存储的文件名与标题保持客户端原值
- `GET /api/sources/{id}`、`DELETE /api/sources/{id}`、`POST /api/sources/{id}/parse`、`GET /api/sources/{id}/elements`、`GET /api/sources/{id}/elements-page?offset=&limit=&anchor_element_id=` —— owner∪成员口径，按来源自己所属的笔记本判权限。分页读取返回 `{items,total_count,offset,limit}`，`limit` 最大 100；anchor 有效时会把 `offset` 调整为包含目标元素的页。
- `GET /api/notebooks/{id}/sources/{source_id}/elements-page?offset=&limit=&anchor_element_id=` —— 采用当前活跃 notebook 参与集授权的有界来源详情读取。浏览器使用该端点；代理全量 element 端点继续保留向后兼容。
- `GET /api/notebooks/{id}/sources/{source_id}`、`GET /api/notebooks/{id}/sources/{source_id}/elements` —— 同样两个读取，但权限按路径里的**当前活跃**笔记本判，目标在它的有效参与集（自身 + 已生效挂载的参考库）内解析。挂载参考库不等于获得该库的直接成员权限，因此浏览器始终只用活跃笔记本过权限、由后端内部代理读取；参与集每次请求实时判定，被挂库降级/易主/深拷贝中或挂载被取消时当场 404。本库来源走的是同一条路径（参与集首项恒为活跃笔记本自身），响应如实返回来源真正所属的笔记本，供前端据此按只读渲染。写入刻意不代理——重新解析与删除仍是 `/api/sources/{id}` 上受 `sources:write` 能力守卫（P2 起 owner∪组管理员）的直接操作。详情响应比 `/api/sources/{id}` 更窄：去掉 `file_path` 与原始 `error_message`（两者都可能带服务端绝对路径），改回一个如实的 `parse_failed` 布尔；跨库的隐藏合成源（memory/knowhow 投影行，集合地图刻意把它们算进作用域）直接拒绝
- `GET /api/notebooks/{id}/assets/{asset_id}` —— 图片资产（knowhow 单元格图片、来源插图）适用同一条参与集规则：路径里的笔记本是查看者的活跃笔记本，资产自己声明所属笔记本，不在活跃笔记本有效参与集内的资产一律 404。经挂载库代理来的资产用 `Cache-Control: no-store`，取消挂载即刻生效；活跃笔记本自己的资产保持原有长缓存
- 命令目录：`GET .../sources/{sid}/command-catalog/preview`（零模型调用的成本预告）、`POST .../sources/{sid}/command-catalog`（发起；该来源已有活跃任务、或上一轮还有候选没审阅完时 409）、`GET .../command-catalog/job`、`POST .../command-catalog/cancel`、`GET .../command-catalog/candidates?job_id=&state=&cursor=&limit=`（keyset 分页 + 各档计数）、`POST .../command-catalog/apply` body `{candidate_ids}` 或 `{all_pending}`（创建或追加「命令目录：<来源>」表，绝不覆盖已有行）、`POST .../command-catalog/dismiss` body `{candidate_ids}` 或 `{all_pending}`（把候选标记为已跳过，不写任何表——不冲突的候选唯一的放弃出口——见[命令目录](#命令目录工具手册)）
- `GET /api/notebooks/{id}/understanding` —— 「AI 对这个库的理解」；任意有读权的成员；返回 `enabled`、`base`、`mine`、`job`、`can_edit_base`——见[AI 对这个库的理解](#ai-对这个库的理解)
- `PUT /api/notebooks/{id}/understanding/{label}` body `{scope: "shared"|"mine", value, expected_revision}` —— `shared` 需要 `agent_profile:write` 能力，`mine` 只需读权 + 行级归属；超过 400 字符上限返回 422，`expected_revision` 过期返回 409
- `DELETE /api/notebooks/{id}/understanding/{label}?scope=&expected_revision=` —— 与写端点相同的权限口径与同一套乐观并发：`expected_revision` 为界面上看到过的版本号（必填；过期 409）；清空取值但保留该行与其历史
- `POST /api/notebooks/{id}/understanding/rebuild` body `{scope}` —— 与写端点相同的权限口径；忙碌或 `AGENT_PROFILE_ENABLED` 关闭时返回 409
- `GET /api/notebooks/{id}/knowledge-types`、`GET /api/notebooks/{id}/knowledge?type=concept|claim|formula|procedure|...`、`PATCH /api/notebooks/{id}/knowledge/{knowledge_id}`
- `GET /api/notebooks/{id}/graph`
- Knowhow 表：`GET|POST /api/notebooks/{id}/knowhow`、`GET|PATCH|DELETE .../knowhow/{table_id}`、`POST .../knowhow/{table_id}/reproject`——另有导入（`POST .../knowhow/import/preview`、`POST .../knowhow/import`）、列/行/格编辑（`POST .../knowhow/{table_id}/columns`、`PATCH|DELETE .../columns/{column_id}`、`POST .../knowhow/{table_id}/rows`、`DELETE .../rows/{row_id}`、`PATCH .../rows/{row_id}/cells/{column_id}`）、Excel 模板往返（`GET .../knowhow/{table_id}/template`、`POST .../knowhow/{table_id}/append` 配 `mode=preview|commit`）、显式的建议式表达优化（`POST .../rows/{row_id}/cells/{column_id}/optimize`），以及带全库推理取证的单行空列补全建议（`POST .../knowhow/{table_id}/rows/{row_id}/complete`，可选 `target_column_ids`，返回 `retrieval_mode` + `retrieval_scope` + `retrieval_status` + `reasoning_trace` + `evidence` + `suggestions`）
- `GET /api/notebooks/{id}/search?q=`
- `POST /api/notebooks/{id}/ask/intent` —— `reasoning` 的无语料意图预检；接收 `{question, conversation_id?}`，最多读取当前会话最近五个用户问题，不创建 conversation/job，返回可编辑的问题合同和阻断性歧义；客户端断开时向模型调用传递取消事件
- `POST /api/notebooks/{id}/ask` — 接地问答（逐句 `[k_i]` 引用；`mode`：默认 `chunk` | `graph` | `reasoning`；`reasoning` 可传 `retrieval_effort`，默认 `standard`；官方网页端以带时区的 `asked_at` 传入只用于显示的提交时间；响应以 `answered_at` 返回权威持久化完成时间；集合型回答可带结构化 `result_sets` 与精确覆盖率；联合范围遵循上文各 mode 的边界）
- `POST /api/notebooks/{id}/ask/stream` — Ask 进度的 NDJSON stream（同样接受可选的带时区 `asked_at` 请求字段；先发带持久化 `job_id` 和 `conversation_id` 的 `started` 事件，再发进度/最终事件）；前端用该会话 id 在答案生成前立即入历史并支持重新打开。transport 断开连接只会停止当前客户端继续接收，后台 job 仍继续并可保存回答
- `GET /api/notebooks/{id}/ask/jobs/{job_id}` — 供重连/恢复流程读取 detached Ask job 的 `status`、`trace` 与 `answer_id`；job 必须属于路径中的 notebook 和当前用户；状态为 `done` 后，前端重新加载 conversation 取得最终 `AskResponse`
- `POST /api/notebooks/{id}/ask/jobs/{job_id}/cancel` — 用户显式中断端点；job 必须属于路径中的 notebook 和当前用户；设置取消事件并在保存被取消的最终回答前停止 worker
- `GET /api/notebooks/{id}/conversations`、`GET|PATCH|DELETE /api/conversations/{id}`
- `POST /api/answers/{answer_id}/feedback`
- Memory：`GET /api/memories`、`GET /api/notebooks/{id}/memories`、`GET|PATCH /api/memories/{memory_id}`、`POST /api/memories/{memory_id}/confirm|reject|deprecate|promote`、`POST /api/answers/{answer_id}/memory-preview`、`POST /api/notebooks/{id}/memories/from-answer`
- Agent 接入：匿名机器可读说明 `GET /api/agent-mcp/onboarding`；认证管理面 `GET|POST /api/agent-profiles`、`PATCH /api/agent-profiles/{profile_id}`、`POST /api/agent-profiles/{profile_id}/tokens`、`GET /api/agent-tokens`、`DELETE /api/agent-tokens/{token_id}`；Streamable HTTP MCP 挂载在 `/mcp`
- Knowhow agent 接入面：`GET /api/agent/knowhow/tables?notebook_id=`、`GET /api/agent/knowhow/tables/{table_id}/discrimination`、`GET /api/agent/knowhow/rows/{row_id}`、`GET|PUT|DELETE /api/agent/knowhow/rows/{row_id}/cells/{column_id}/code`——session 或 Agent Bearer token 均可访问；读需要 `knowledge:read`，代码写入需要 `knowhow:code`（见 [Memory 与 Agent MCP](#memory-与-agent-mcp)）
- 统一 KG：`POST .../unified-kg/rebuild`、`GET .../unified-kg`、`GET .../unified-kg/pending-merges`、`POST .../unified-kg/merges/{id}/confirm|reject`
- 重新合并（界面：知识图谱视图与「索引与构建」面板里的**「重新合并」**）：`POST /api/notebooks/{id}/unified-kg/rebuild` 启动**后台**任务并返回 `{status: "rebuilding", notebook_id, job_id}`——它不再返回 `{clusters: N}`，因为这件事的工作量取决于笔记本规模而不是这一次点击：内容版本闸让「输入没变」的库仍然毫秒级返回，但真正要重聚的那一趟会在整张图上流式扫种子代表（基准库规模是分钟到小时级，早已越过 PostgreSQL 的语句超时，而且整段时间钉着一个请求 worker）。它没有 LLM 前置条件——但并非严格零模型：`kg_merge_review` / `kg_concept_description` 已配置时会作为 fail-open 增强被调用。单飞**与补上关联共用同一个任务槽**：一本库一格，因为重新合并会整表重写 `concept_clusters` 与板块划分，而补上关联往聚类要读的那张图上追加边——并发不是多花一份钱，是一方在另一方还在读的输入上发布结果。再点一次、或另一件正在跑时点，都返回 409 并**点名真正占着槽的那个动作**。与补上关联一样，任务槽是**按进程**的（生产固定 `--workers 1`）；离线 CLI（`scripts/recluster_kg.py`、`batch_ingest`）是独立进程、直接调用这一趟，不在此列。完成信号是 `GET /api/notebooks/{id}/unified-kg/rebuild/status`（notebook 读权限），返回 `{job_id, notebook_id, status, running, clusters}`，`status` ∈ `running` / `succeeded` / `failed` / `idle`；`idle` 同时覆盖「从没跑过」「进程重启过」和「这一格现在被补上关联占着」，所以浏览器的有界轮询一定会收工，两个轮询也都不会挂在对方的任务上。任一终态都按当前范围重拉图谱、待确认合并与概念合并状态，忙碌指示按「在重新合并的是哪个笔记本」作用域。它与 `GET .../unified-kg/status` 刻意分开——后者的 `building` 说的是可视化产物。强制全量重聚（改了聚类**设置**，那是内容版本闸看不见的）仍然只在 CLI。待审队列以 canonical component 对为用户语义：重建时每个展示对只保留一条确定性的最高分代表；稳定的 rejected/deferred seed 决策经 confirmed union 投影成 component 级 cannot-link，因此同组另一个 seed 不能把刚分开的展示对重新生成。一次人工决定会按确定顺序锁住同展示对的完整行集，并把旧部署遗留的全部重复行原子收束为最新状态；只要任一兄弟行已经 confirmed，拒绝就按物化 union 翻转处理。重建发布替代 pending 代次前，会在同一个刷新事务里删掉旧代并重新应用实时决定，所以聚类之后才落库的决定也不会被旧快照重新发布。点全为待审状态展示对的**「分开」**既不改变当前簇也不改变检索产物，所以它会直接离队，不置 dirty、更不会启动重建；若把含任一 confirmed 行的展示对翻成 rejected，则仍须 invalidate + dirty，因为旧 union 可能已经物化。只有确认待审合并才立即触发重新聚类。确认合并若撞上共享任务槽（409），会在客户端记一个待补发标记，等占槽任务的终态轮询观测到时自动补发（补发本身撞到非 409 的瞬时失败同样保留标记续轮询，不会丢弃），且受同一次轮询的尝试上限兜底；这是一份**作用域限定在标签页保持打开期间的尽力承诺**。重载或重开页面即丢失客户端标记，不会有任何东西自动补发这条确认——这是刻意的：标记无法从泛化的 dirty 标志重建，因为普通的补上关联同样会把图谱标成 dirty，据此推断待补发会在用户没有请求的情况下自动发起一次可能数小时的全量重聚。兜底是既有的「待重建」dirty 标签加一次手动点击「重新合并」。能补上这个缺口的服务端持久重试队列是后续迭代的候选项，本次未实现。
- 补上关联（界面：知识图谱视图里的**「补上关联」**）：`POST /api/notebooks/{id}/kg/relink` 启动**后台**任务并返回 `{status: "relinking", notebook_id, job_id}`——它不返回统计，因为这件事的工作量取决于笔记本规模而不是这一次点击。它是零模型的确定性动作，因此没有 LLM 前置条件。单飞按笔记本，且**同时覆盖两个入口**——这个端点，以及 KG 构建成功后的确定性补连尾（两者跑的是对同一本库的同一趟活）。运行期间再点返回 409 并带用户可读文案；构建尾抢不到任务槽就跳过（fail-open，只发一条无正文事件 `{kind: "kg_relink_skipped", notebook_id, holder}`），而不是再把每个来源读一遍。`holder` 是另一次补上关联时，跳过不丢任何东西（同一趟活）；`holder` 是正在跑的**重新合并**时，跳过是刻意接受的丢弃而不是「有人替你做了」——重新合并不会追加这条尾要写的边，所以重新合并运行期间完成的分析，其补连尾会被跳过，待手动点一次「补上关联」补连。任务槽是**按进程**的：生产固定 `--workers 1`，那里它就是部署级的守卫；多 worker 下端点与构建尾只在同一个 worker 内互斥，`GET .../kg/relink/status` 也只回报接这次请求的那个 worker 知道的事。离线 CLI 是独立进程，不在此列。完成信号是 `GET /api/notebooks/{id}/kg/relink/status`（notebook 读权限），返回 `{job_id, notebook_id, status, running, isolated_before, edges_added, isolated_after}`，`status` ∈ `running` / `succeeded` / `failed` / `idle`。进度只记在提供服务的进程内，所以「从没跑过」和「进程重启过」都如实回报 `idle`，浏览器的有界轮询因此一定会收工；任一终态都按当前范围重拉图谱，而它的忙碌指示按「在补的是哪个笔记本」作用域，切库既不会把另一个库的按钮变灰，也不会去刷错库的图谱。任务本身逐来源推进——由 `sources` 的 keyset 驱动，另加每次运行一次的查询发现没有来源行可指认的分区（对象存着空 `source_id`，或指向已被删除的来源）——每个来源只读该来源的对象加上与这些对象相邻的全部关系（**包含跨来源关系**——只靠跨来源边连接的节点不算孤立）。边是逐来源提交的，所以只要写过边就发布 KG 变更信号，中途失败的那一趟同样发布。产出的边就是此前整库实现产出的那一批，只有一处已登记的例外：按来源那条读取钉死了插入序，而旧查询继承的是 planner 给的 `updated_at` 序，因此在每节点补边上限绑定时，某个孤立节点可能被接到同来源里另一个同样合法的伙伴上。边数与孤立数不受影响。
- `GET .../concepts/{canonical_id}/detail`、`GET .../objects/{object_id}/context`
- 图谱分析报告读取（仅需 notebook 读权限）：`GET /api/notebooks/{id}/kg-analysis`——可选 `boards`/`top_members`/`edges` 上限，返回对象构成、按对象类型分列的收敛率、主题板块列表与跨板块边，每项都标注建于哪次 `kg_mutation_seq`；`GET /api/notebooks/{id}/kg-analysis/sources`——可选 `limit`/`offset`/`order=sparse|connected`，分页返回逐来源画像。两者都只读 `unified-kg/rebuild` 写入的 `kg_community_edges` / `kg_source_profiles` / `kg_analysis_artifacts` 预计算产物表。面板内仅可编辑成员可见的生成／更新按钮调用已有 `POST /api/notebooks/{id}/unified-kg/rebuild`，不是第三个图谱分析端点。
- 全局图谱类型基线（写入仅管理员）：`GET /api/object-schemas`、`POST /api/object-schemas`、`PATCH /api/object-schemas/{type}`、`DELETE /api/object-schemas/{type}`。
- 笔记本生效图谱类型：`GET /api/notebooks/{id}/object-schemas` 要求 notebook 读权限；`POST /api/notebooks/{id}/object-schemas`、`PATCH /api/notebooks/{id}/object-schemas/{type}`、`DELETE /api/notebooks/{id}/object-schemas/{type}` 要求 notebook owner 权限。修改继承的全局类型会创建本库覆盖；删除覆盖会恢复继承；只有该 notebook 已不存在此类型知识对象时才能删除本库专属类型。`POST /api/notebooks/{id}/schema-proposals` 把仅供审核的候选类型保存在当前 notebook，而不是写入全局基线；owner 明确批准前，候选行绝不会遮蔽继承类型。全局与笔记本类型写入跨两张注册表串行执行，模型返回的候选会在落库前重新校验；数据库合并预检遇到同名但语义列不同的全局定义会直接拒绝，绝不静默保留目标库行。`object_type` 与每个字段键必须以 ASCII 小写字母开头，只能包含 ASCII 小写字母、数字和下划线，最长 80 个字符。每份定义最多包含 64 个不重复字段和 64 个不重复列表字段；`primary` 必须属于 `fields`，每个列表字段也必须属于 `fields`。每个人类可读的类型文本（`plural`、`label`、`description` 与候选 `rationale`）最多 2,000 个字符。浏览器在创建或编辑提交前执行同一组护栏，API 仍是权威校验方。
- `GET /api/notebooks/{id}/duplicates`、`POST /api/notebooks/{id}/knowledge/{knowledge_id}/merge`
- 两层：`POST /api/notebooks/{id}/tier` body `{tier: "base" | "personal"}` → 返回更新后的 `NotebookSummary`（tier 非法 400，notebook 不存在 404）。设置 notebook 的联合层（base = 可发布为公共知识库，personal = 默认用户笔记）；`base` notebook 只有在被其它笔记本显式挂载后才参与该笔记本的检索（`GET`/`PUT /api/notebooks/{id}/bases`，候选列表见 `GET /api/notebooks/{id}/mountable`）。
- 参考库挂载：`GET /api/notebooks/{id}/bases` → `MountedBase[]`（本 notebook 的挂载边，含置灰的失效边）；`PUT /api/notebooks/{id}/bases` body `{base_notebook_ids}` → 全量替换，返回更新后的 `MountedBase[]`（含不可挂载的 id 时 400；仅 owner 可写）；`GET /api/notebooks/{id}/mountable` → `MountableNotebook[]`（可挂候选：所有公共知识库、本 notebook 自己同 owner 的库，以及群组知识共享之后本 notebook owner 读得到的每一本库——受借入挂载的未共享门约束；每个候选带 `origin ∈ {base, mine, shared}`，供选择器如实分组）。
- 群组与授权边：`POST`/`GET /api/groups`、`GET`/`PATCH`/`DELETE /api/groups/{id}`、`POST /api/groups/{id}/transfer`、`PUT`/`DELETE /api/groups/{id}/members/{user_id}`、`DELETE /api/groups/{id}/membership`、`GET`/`POST`/`DELETE /api/groups/{id}/invite-link`、`POST /api/groups/{id}/invite-link/rotate`、`POST /api/group-invites/{token}/join`、`GET /api/users/resolve?username=`、`GET`/`POST /api/notebooks/{id}/grants`、`DELETE /api/notebooks/{id}/grants/{grant_id}`、`GET`/`DELETE /api/groups/{id}/shared-notebooks[/{notebook_id}]`，另加只读的 `GET /api/notebooks/{id}/share`。角色、唯一 owner、邀请链接生命周期、可见性口径（404 而非 403）、创建授权边的双重条件、不对称撤销与已登记的上限全部见上文「群组工作台」章。
- 边可信与策展：`GET /api/notebooks/{id}/edge-review-queue`、`POST /api/notebooks/{id}/relations/{rel_id}/review`
- 治理 / 晋升：`POST /api/notebooks/{id}/knowledge/{knowledge_id}/promote`、`GET /api/promotion-queue`、`POST /api/promotion-queue/{candidate_id}/approve|reject`
- 深度报告（两阶段）：`POST /api/notebooks/{id}/reports` body `{question, depth?, auto_generate?}` → `{report_id}`；先做不接触语料的问题理解，并停在 `status=intent_ready` 等人工确认，除非请求带 `auto_generate=true` 且问题清晰（无阻断性歧义）——此时意图同样通过同一份确定性冻结自动确认（不二次调用理解模型）后再进入规划。`GET .../reports/{rid}` 会返回持久化的 `understanding` 与状态/进度。`POST .../reports/{rid}/intent` body `{resolved_question, answers:[{id,answer}]}` 校验所有必填歧义，并原子认领进入语料规划的唯一转换，返回 `{status:"planning"}`；重复或过期确认返回 409 且不会启动第二个任务。规划完成后停在 `outline_ready`；若原始请求含 `auto_generate=true`，则意图阶段同样自动确认（仅当无阻断性歧义）之后自动进入生成。富 `outline` 含每节 `intent_ids`、`intent_questions`、可编辑 `sub_queries`、客观 `coverage`、视角/张力/充分性，详情继续包含 `content_md` 与实时 `section_status`。`PATCH .../reports/{rid}/outline` body `{sections}` 仅在 `outline_ready` 编辑草案；服务端保留 intent catalog，最多接受 `REPORT_MAX_SECTIONS` 节，每节最多接受 `REPORT_MAX_SUBQUERIES_PER_SECTION` 条非空检索方向。浏览器从 `/api/system/config` 同步这两项护栏并阻止超限提交；直接 API 客户端的章节或检索方向超限时收到 422，不再被静默截断。没有有效节或某个必答主题失去最后一个章节绑定时也返回 422。`POST .../reports/{rid}/generate` body `{depth?}` 可从 `outline_ready` 或仍保有大纲的 `failed` 报告原子启动**阶段2 生成**，其他状态返回 409；重试在认领事务内保留确认意图/大纲并清空旧生成产物。生成章节含后端重算的 `evidence_level`/`grounded`；引用可携带精确 `source_id`/`element_id`。另 `GET /reports`（列表）、`POST .../cancel`、`DELETE`、`POST .../reports/export` `{report_ids}` → `reports.zip`。批量导出先由 repository SQL 按 notebook/creator 收窄已完成且正文非空的报告并释放连接，再恰好一次调用启动冻结的 single `report.exporter` Provider；默认内建 Markdown provider 保持原 ZIP 文件名与字节，archive 仍归 core 且畸形部分输出整体拒绝。浏览器单篇 Markdown 下载仍在本地完成，不增加请求。节级并发采用上文报告专用的数据库保护护栏，不再复用 `KG_JOB_CONCURRENCY`。

  **「待分析来源」不含「分析过、这篇确实没有可整理的知识」。** 判据不是「这份来源没有知识对象」——正文极少、或整份是没有图注的图片的文档，分析跑完了本来就是零个对象，把它算成待分析会让计数**永远降不下来**：来源徽标一直显示「待分析」，看板一直提示「继续分析」，而每一轮分析都会把这些来源按完整模型成本重跑一遍、再得到零。判定为「已分析·无知识」需要最近一次 `kg` 抽取记录同时满足：状态为 `completed`、消息带成功路径写下的零对象标记、没有失败窗口、且不是 partial 重试的半成品。`no-llm`（抽取模型未配置）**刻意不算**——那种来源确实还没被分析过，配好模型之后必须被重新捡起。判据的权威表述只有一份：`backend/app/models/sources.py` 的 `kg_analyzed_without_objects`；两条计数查询按方言镜像它（那个位置必须是一条 `COUNT`），并由 `backend/tests/test_kg_empty_extraction_marker.py` 逐用例与它对账。增量分析跳过这些来源，整库重建仍会选中它们——换模型或带 OCR 重新解析之后，走的就是那条路回来。

报告规划护栏默认为：每节四条检索方向、八个直接元素候选，语料地图
侦察分别取 12 个 KG 条目、八个 PPR chunk 和八条已确认 Memory。它们可通过
`REPORT_MAX_SUBQUERIES_PER_SECTION`、`REPORT_PROBE_ELEMENT_LIMIT`、
`REPORT_SCOUT_KG_LIMIT`、`REPORT_SCOUT_CHUNK_LIMIT` 与
`REPORT_SCOUT_MEMORY_LIMIT` 分别配置；修改侦察宽度只改变规划上下文，
不改变最终章节的证据预算。

当前持久化/API 契约是 `reports` 表与 `/reports` API；已退役的内容工作室存储与路由不属于当前 runtime。

### 部署插件

`GET /api/admin/extensions` 仅系统管理员可读，返回启动时冻结的 registry 拓扑的白名单投影：每个插件恰好 6 个字段——`id`、`version`、`trust`、`display_name`、`contributions[{id,point,kind}]`、`ui_contributions[{id,slot,capability}]`——绝不返回模块路径、文件路径、settings 值、内部 capability reason 或异常文本；部署把某个插件条目标为 `enabled=false` 时它从不注册，因此也不会出现在这里。`GET /api/system/extensions`（任意已登录用户，只给实时可用性）不变。

`/api/extensions/{plugin_id}/…` 是部署插件自有 HTTP 路由的唯一挂载面：router 级会话依赖意味着没有匿名面，路由工厂拿到的是 8 个字段的 `PluginRouteContext`——`plugin_id`、`settings`、`require_notebook_capability`、`require_notebook_read`、`current_actor`、`user_error`、`url_sources`、`emit_event`——绝不给 repository、全局 `Settings`、model client、FastMCP host 或原始 bearer token。**这些接缝背后的每个 core 端口都自己给请求的当前用户做授权判定**——例如 `url_sources.import_urls` 会对调用用户核对 `sources:write` 能力,不通过就 404——所以挂载点自身的 `{notebook_id}` 路径形状守卫只是纵深防御,不是授权边界本身。URL 导入端口一份实现两种调用形态：同步 (`def`) handler 本来就在 FastAPI 线程池里、直接调 `import_urls`；`async def` handler 跑在事件循环线程上，必须 `await import_urls_async(...)`——它把同一份阻塞工作（数据库写入，外加每个 URL 一次串行远端探测）连同请求上下文一起挪进线程池，因此两条路的授权判定完全一致。从 async handler 调 `import_urls` 会在动任何东西之前抛 `RuntimeError`：这是开发者错误，以带 traceback 的 `500` 呈现（文案点名该 await 哪个方法），绝不作为面向用户的文案。插件代码**抛出**（`fastapi.HTTPException` 或 `starlette.exceptions.HTTPException` 皆可——前者是后者的子类，两者被接住的方式完全一样）**或返回**的 401 都会被翻译成 424（并记一条事件），不会被误当成会话失效。「插件代码」既指 handler，**也指插件自己的 `Depends(...)` 依赖（嵌套多少层都算）**：FastAPI 在调 endpoint 之前就把依赖解完了，所以把上游检查写进依赖（这是最常见的写法）否则会让一个真 401 漏到浏览器、把用户登出。依赖只覆盖「抛出」那一半——依赖的返回值是作为参数注入的，永远不会变成响应。core 自己的依赖按对象身份**加**定义所在模块双判排除在外，所以 core router 级会话门产生的真 401 仍然原样是 401；生成器（`yield`）依赖与 security scheme 不动。

数值上限：插件可观测事件白名单恰好 4 个字段（`event`/`outcome`/`count`/`elapsed_ms`）；`count`/`elapsed_ms` 必须是 `0..1e9` 区间的整数；稳定码（事件名、outcome，或发现/挂载拒绝 reason）最长 64 字符；每个插件最多声明 1 个 HTTP 路由贡献。新插件接入 SOP——写后端 bundle 与构建期 UI 包、本地联调、打包、安装、启动校验、升级/回滚，以及完整拒绝码表：[部署插件 SOP](./deployment-extensions-sop_zh.md)。

## 管理员用户活动日志（`/dev/logs`）

`/dev/logs` 共享同一条顶部范围条（被查看用户、日期范围），分两个视图 tab：**活动**（默认）与**模型调用**（原有的按天 LLM 调用流水查看器，逐位保留——`kind`/`status`/`model` 过滤、全文搜索、按天下拉、自动刷新均未改动，其 API 仍是 `/api/debug/logs/...`，仍受 `DEBUG_LOGS_ENABLED` 门控）。

「活动」视图三栏：

- **范围** —— 被查看用户自己的笔记本列表（名字 + 来源/提问/报告计数），可展开看该库的来源清单（显示名 + 解析状态徽章）。点笔记本把中栏活动流过滤到该库；点来源在右栏详情打开它（**不**过滤中栏，见下文「未做」）。
- **活动流** —— 提问 / 来源 / 报告三类条目按时间倒序混合成一条流，按 `(created_at DESC, id DESC)` 复合游标翻页（「加载更多」），默认每页 50 条（`limit`，上限 200）。原生日期选择器 + 「全部时间」驱动一个半开区间 `[since, until)`。区间边界带**浏览器 UTC 偏移**（`2026-08-04T00:00:00+08:00`），所以「一天」指的是浏览者本地日历日——与界面上一切时间的渲染时区同一个；两端各自算偏移，夏令时切换日不会差一小时。畸形边界返回 `400` 加可上屏的中文文案（`X-User-Message`），既不静默放宽窗口，也不 500。三类条目在两个数据库后端都经同一个 `parse_activity_instant`（`app/core/activity_time.py`）归一时间：SQLite 侧经 `julianday()` 按绝对时刻比较（历史行是裸 naive 文本、新行带 offset，混合格式统一按裸串=UTC 读），PostgreSQL 侧直接按 `timestamptz` 原生比较，并带与 SQLite 同形的 `COALESCE` 兜底（`ask_jobs.created_at` 在 PG schema 里可空，而 PG 的 `DESC` 默认 `NULLS FIRST`，少了兜底这行会被顶到第 1 页并让 Python 归并直接 `TypeError`）。两侧把不可解析 / NULL 的值折到**同一个哨兵时刻**：它既排到最末、又能经游标往返，`next_cursor` 因此不会发出下一页解析不了的值。三类各自独立 keyset 分页、在 Python 内按时间归并，不做三表 `UNION ALL`——大库上可诚实回报「本页覆盖到 HH:MM 为止」的有界边界，而不是一次全局排序爆炸。
- **详情** —— 选中提问显示完整问题、答案、引用与推理轨迹（复用既有 Ask 面板渲染件，只读）；选中来源显示显示名、解析状态与派生诊断；选中模型调用（仅「模型调用」视图内）复用既有 prompt/response 详情面板，未改动。

三个新增端点（均为只读，权限口径镜像 `debug_logs._resolve_owner`：被查看的 `user_id` 等于当前用户自己的 id，或当前用户是 admin，否则 403）。三者另受独立部署开关 `USER_ACTIVITY_VIEW_ENABLED` 门控（默认 **true**——「活动」是 `/dev/logs` 默认 tab，若沿用默认关闭的 `DEBUG_LOGS_ENABLED`，普通部署一打开页面就 404；两个开关相互独立，`DEBUG_LOGS_ENABLED` 只继续管「模型调用」视图背后的 `/api/debug/logs/...`）。前端没有别的途径拿到这个部署时取值，因此 `GET /system/config` 的 `SystemConfiguration` 响应把它作为 `user_activity_view_enabled` 一并下发；`/dev/logs` 据此在关闭时隐藏「活动」tab、把 `view` 默认落到 `llm`（`?view=activity` 深链同样被归一，不会打开一个三个端点全 404 的视图）。字段缺失（旧后端 + 新前端）按 `true` 处理，与后端自身默认值一致，不藏掉一个其实可用的 tab。左栏复用的 `GET /admin/users/{user_id}/notebooks` 权限口径同样是 self-or-admin 而非仅 admin，普通用户查看自己的活动才能读到自己的笔记本清单：

- `GET /admin/users/{user_id}/activity?notebook_id=&since=&until=&before_ts=&before_id=&limit=` —— 上文的提问/来源/报告混合流。
- `GET /admin/users/{user_id}/notebooks/{notebook_id}/sources?offset=&limit=` —— 该笔记本的来源清单（`limit` 默认 50、上限 200；左栏面板只取第一页，不做进一步翻页）；`notebook_id` 不属于 `user_id` 时 404。
- `GET /admin/users/{user_id}/asks/{job_id}` —— 单条提问的完整详情（问题/答案/轨迹）；`job_id` 不属于 `user_id` 时 404。

三类活动一律按 **owner-only** 归属：只分解被查看用户自己创建的笔记本（`created_by`，排除深拷贝进行中的笔记本），与用户使用总览「展开清单刻意保持 owner-only（即使合计包含共享库提交）」同一口径。隐藏合成来源（`memory`/`knowhow`）既不进来源清单、不进活动流，**也不进范围栏的每库来源计数**——那个计数现在与清单本身用同一条可见性谓词（单一真源），表头与展开清单不可能再对不上。（`GET /admin/users/{user_id}/notebooks` 同时供着这两块屏，所以用户使用总览的每库来源数对「存过 Memory 或建过 Knowhow 表」的用户会降到同一个可见来源数——此前是偏高的。）同一份笔记本清单的报告计数同样收窄到 `created_by = user_id`——与 `questions` 已有的谓词、活动流自己展开的报告条目同一口径：共享笔记本里另一位可写成员建的报告不再计入笔记本 owner 的表头（此前是偏高的，且计数会与展开的活动流悄悄对不上——那条条目从来不会出现在活动流里）。来源活动条目**不**携带原始 `error_message`（可能带服务端绝对路径），只给派生的 `parse_failed` 布尔，加 `extraction_warning`/`parse_quality_warning`/`paper_meta_status`，与 `ScopedSourceDetail` 既有的脱敏规则同形。它的显示名来自 `SourceSummary` 新增的 `display_title` 字段（论文标题优先，与引用卡同一份实现 `source_display.py::source_display_title`）——这是**新增**字段，原有 `title` 字段原样保留，既有的来源页签零回归。`SourceSummary` 另新增原始 `created_at` 时间戳：右栏详情的两个入口（范围栏清单与活动流）都按**浏览器**时区渲染它；既有的 `created_label` 是服务端预先格式化好的日历日，继续用它会让同一份来源按点开的入口不同显示成两个日期。`created_label` 保留给既有的来源页签。报告条目的耗时逐字复用深度报告的既有规则：`generation_started_at → updated_at`；缺开始戳的旧报告不显示耗时，未完成报告只显示创建时间。

以下明确**未做**（分期到后续迭代，不是遗漏）：把模型调用挂到触发它的提问下面（需要新的写侧日志上下文透传，历史日志无法回填）；把检索/解析阶段耗时挂进提问/来源详情；左栏点来源过滤中栏活动流（当前点来源只打开右栏详情）；笔记本来源清单翻到第一页之后的内容。

## 当前限制

- SQLite 检索使用关键词/FTS 兼容的 CJK 处理与有界 float32 矩阵/scale index；PostgreSQL 在相同 repository ports 后使用 `pg_trgm`/`ILIKE` 与 byte-oriented float32 向量。pgvector 仍是未来放量选项，不是运行前置。
- 大文档摄取已加固：贪心窗口化 KG 抽取（成本线性），并发 embedding 逐批落库。极大规模下可再接入 `sqlite-vec`。
- Ask 不再在请求路径里同步补齐 embedding 或全量扫描 source elements；使用已有的关键词/向量索引，在维护任务运行时仍保持响应；并输出每阶段计时（`ask_stage` 事件）。
- 统一 KG rebuild 改为显式且可观测（`GET /notebooks/{id}/unified-kg/status`）；摄取来源只标记图谱为 dirty 而非同步重建，打开图谱浮层不再自动重建（按需刷新）。
- 跨文档概念合并使用确定性别名归一化 + 有界 top-k 向量候选（可扩展到上千概念）；可选 LLM 预审（`POST /notebooks/{id}/unified-kg/merges/review`）对小批量近义词候选做高置信确认/拒绝。
- KG 抽取需要在系统模型 TOML 中绑定 `kg_extract` workload；离线 smoke 在需要验证检索/治理时会显式写入 KG 对象。
- 两层与深度推理尚属早期：图推理 Ask 模式（`mode="graph"`）为 opt-in / 实验性（Ask 面板开关仍驱动默认的 `chunk`/`reasoning` 路径）。把 notebook 标为 `base`/`personal`（经 `POST /notebooks/{id}/tier`）、边可信审核队列、晋升（个人→基准）现都已有专属前端控件（在分析工具栏）；把一个 notebook 发布为公共知识库只是让它可被挂载——tier 感知联合检索与 base 优先冲突规则只对显式把它挂为参考库的笔记本生效。
- Notebook 分享有三种形态：链接复制、只读成员、共享给群组——都不是实时协同编辑。群组成员可以提问、写自己的深度报告；内容管理（来源、图谱、授权边）仍归 owner，组管理员的写权限是 P2。
- SQLite 与 PostgreSQL 都可由唯一 repository factory 原子选择，发行默认仍是 SQLite。只改 `DATABASE_URL` 不会同步既有行；存量切换/回滚必须停写、验证备份，必要时执行外部数据迁移，并在启动后做一致性检查。PostgreSQL 向量存 `bytea`，不要求 pgvector。
- `off` 模式 PDF 回退用 PyMuPDF4LLM 的分页 Markdown，保留标题、多栏阅读顺序和重建表格；只有该解析器缺失或报错时才最后回退 pypdf。公式、图片和复杂扫描件的权威高保真路径仍是 MinerU。URL/上传文件的云解析在重试后仍失败时也走同一本地回退，并以 `extracted` + `parse_quality_warning=true` 返回；来源详情会说明风险并提供重新解析/删除入口，后续 MinerU 重解析成功会清掉警告。见[用 MinerU 解析 PDF](./operations_zh.md#用-mineru-解析-pdf)。
- 用户记忆保持手动 opt-in，当前没有自动记忆行为。
