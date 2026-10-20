#!/usr/bin/env python3
"""Guard: user-facing Chinese UI copy must not leak internal jargon.

Scope follows the **trust boundary, not the directory**. These sources of copy reach
a user's screen, and all of them are scanned:

  1. frontend/app and frontend/features *.ts/*.tsx *rendered text* — string
     literals plus JSX text nodes.
  2. backend `user_error(status, "…")` messages — `api/deps.py` marks exactly these
     4xx `detail` strings with `X-User-Message: 1`, and `frontend/app/errors.ts` is
     deny-by-default: a marked detail is shown to the user **verbatim**. Marking a
     string is therefore a promise that it is user copy, so it inherits the copy
     rules. Scoping this guard to `frontend/app` alone is what let 「基准库」and 「晋升队列」
     ship inside marked 403s while the guard stayed green.
  3. backend **reasoning-trace summaries** — the server writes them, the front end
     renders them **verbatim** (`answer-panel.tsx` prints `step.summary` with no
     mapping of its own), so they are user copy that never passes through
     `frontend/app`. `TraceStep(..., summary=...)` calls in
     `TRACE_SUMMARY_MODULES` are scanned as user-facing copy.

  5. backend **display fields** — registered call shapes whose fields the front end
     renders verbatim: `IndexingPipelineOption(label=…, description=…)` and the KG
     build job failure text (`….kg_build_jobs.finish/set_stage(…, error_message=…)`,
     shown through `KgBuildJobStatus.user_message`). See `DISPLAY_FIELD_CALLS`.

  6. backend **model workload labels** — the values of `_WORKLOAD_LABELS` in
     `backend/app/services/model_registry.py` travel as `workload_label` to the
     model-failure banner (`frontend/app/model-services.ts`) and the admin usage
     pages, which print them verbatim. See `WORKLOAD_LABEL_MODULE`. The same
     channel covers the model-service display names this repository ships as
     defaults — `ROLE_LABELS` in `scripts/migrate_legacy_model_env.py` and the
     `display_name` lines of `model-services.example.toml` — which become the
     `display_name` the model-service status card prints verbatim. Names an
     operator types into their own TOML are deployment data and out of reach.

  7. backend **promotion source titles** — a public library's synthetic
     「收录自：<原标题>」 sources are named by `PROMOTION_TITLE_PREFIX` /
     `MEMORY_PROMOTION_TITLE_PREFIX` in `backend/app/domain/promotion_source.py`; the
     title is shown verbatim as the source's name (source list, detail, search,
     activity). See `PROMOTION_TITLE_MODULE`. The `LEGACY_*` prefixes in the same
     module are the storage spelling of titles written before the 2026-10-19
     wording ruling (the sealed v88 / 0068 migrations still write them); readers map
     them to the current spelling, so they never reach a screen and are not scanned.

  8. backend **report follow-up reasons** — when a question attached a deep report
     and the turn does not read it, the receipt (`LinkedReportReceipt.reason`), the
     pre-send check (`ReportLinkCheck.reason`) and the reasoning trace show one
     sentence verbatim: `REASON_PREFIX` + a clause from the named constants of
     `backend/app/domain/report_followup.py`. Their call sites reach the text
     through a cross-module import and a subscript, which channels 2/3/5 do not
     follow by design, so the constants themselves are scanned. See
     `REPORT_REASON_MODULE`.

  For the backend channels (2, 3, 5) the copy does not have to be a literal at the
  call site: `_IndirectCopy` follows variables, conditionals, concatenation,
  `%`/`.format`, `X.get(k, default)`, module constants and module-helper returns.
  They also strip the deployment's registered environment-variable names (derived
  from `backend/app/core/config.py`) before matching (`BACKEND_ENV_VAR`) — a config
  key named in an error message is not jargon; other ALL_CAPS codes are still checked.

  Bare `HTTPException(detail=str(exc))` is deliberately **not** scanned: it is never
  displayed (the front end falls back to a generic message by status code) and its
  detail is a diagnostics/MCP contract. `tests/test_user_error.py` guards that split.

  4. `release-notes/*.md` (README excluded) — the release manifest hands each note to
     the front end's update dialog verbatim; each line is scanned with the same rules.

Any hit fails the build. Run by scripts/check.sh.

Scope & deliberate limitation (see MEMORY severity lesson — a word blacklist is not
a semantic checker, so this does NOT claim full coverage):

  * A blacklisted term is flagged only when it sits inside a unit that also contains
    a CJK character — i.e. real Chinese UI copy. Pure-ASCII code/ids never trip it.
  * Comments (`// …`, `/* … */`) are blanked before scanning, so jargon left in
    code comments is intentionally ignored (the brief keeps internal names in code).
  * `${…}` template and `{…}` JSX interpolations are stripped from each unit before
    matching, so identifiers like `currentNotebook`, `{s.n_chunks}` are NOT flagged
    — only literal rendered text is. ASCII acronyms additionally require no adjacent
    ASCII letter, so `PKG`, `Scanner`, `currentNotebook` cannot false-positive.

The blacklist below covers `docs/ui-vocabulary.md`「界面词汇表」row by row. Two rows are covered
only in their unambiguous compound forms, on purpose (documented in that section):

  * 节点 / 边 — the graph view legitimately draws nodes and edges ("图谱技术上下文"
    in the table), and 边 is a substring of 旁边/边框/边距. Only 孤立节点, 补连边,
    关系边, 边审 are matchable; bare use stays a human-review item.
  * 知识库 — the legitimate Knowledge tab name; lint cannot tell it from misuse.

Also intentionally NOT blacklisted: 知识图谱, 索引 (user-dictionary words; only their
jargon modifiers CSR/ANN/暴力检索 and the abbreviation KG are).

The companion check — raw enum fallback (`MAP[x] ?? x`, `label(m, x, x)`) — lives in
`frontend/tests/guards/raw-enum-fallback.test.mjs`, not here. It needs the *context* of the
expression to tell a rendered lookup from internal normalisation, which means a real
TypeScript AST; doing that from Python would mean spawning node and dragging
`frontend/node_modules` in as a prerequisite for this otherwise dependency-free
script. `npm run test` collects `*.test.mjs` recursively, so `scripts/check.sh` still
runs it as a hard gate. Split of duties: this file guards *rendered text*, that one
guards *code shape*.

`--extra-root <dir>` (repeatable) is a self-check hook for out-of-tree deployment
plugins (`EXTENSIONS_CONFIG`; see `docs/deployment-extensions-sop*.md`): plugin source lives
outside this repository, so it cannot ship in `frontend/app`/`frontend/features` or
`backend/app` and therefore is not reachable by the two default scan roots above no
matter how the guard is invoked. Each `--extra-root` directory's `**/*.py` is scanned
for `user_error(...)` messages with the same rules as the default backend scan
(same blacklist, same `SANCTIONED_UI` allowance, same f-string handling) — a plugin
package that wants CI-grade assurance runs this guard against its own tree the same
way this repository runs it against `backend/app`. It is deliberately scoped to
*only* extend the scan face: passing no `--extra-root` leaves every check (including
`MIN_USER_ERROR_SITES`, which only ever counts the default `backend/app` root, never
an extra one — an empty or tiny plugin tree must not be able to push the floor check
below its real threshold) and every line of output byte-for-byte unchanged from
before this option existed.
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_PRODUCTION_DIRS = (
    ROOT / "frontend" / "app",
    ROOT / "frontend" / "features",
)
BACKEND_APP = ROOT / "backend" / "app"
# 第四条通道:release-notes/*.md 由打包清单原样交给前端弹窗展示,是用户可见文案。
RELEASE_NOTES_DIR = ROOT / "release-notes"

# 后端「这段文案会原样上屏」的唯一标记函数(app/api/deps.py)。
USER_ERROR = "user_error"

# 非空性下限:守卫按函数名做 AST 匹配,一旦 helper 改名 / 换成装饰器 / 调用写法变了,
# 匹配数会掉到 0 而退出码仍是 0 —— 正是本次要根治的那种假绿。这不是精确台账
# (新增站点不该让构建红),只是「扫描面塌了」的哨兵。2026-10 约 260 处。
MIN_USER_ERROR_SITES = 15

# —— 第三条通道:服务端写、前端逐字渲染的推理轨迹摘要 ——————————————————
# 扫描面是**具名模块**而不是整个 backend/app:轨迹摘要的产出方是有限的几个服务
# 模块(推理检索器,以及会自己写轨迹步的提问、全局问答、插件问答、表格分析、结构化
# 检索),把 `TraceStep(summary=…)` 这个形状泛化到全仓会把一堆同名 kwarg 的非 UI
# 调用拖进来,而收益是零。新增一个会写轨迹步的模块时,把它加进这张表——这是有意的
# 显式登记,不是靠 glob 猜。
#
# 路径按 `ROOT` 直接写死,**刻意不经过 `BACKEND_APP`**:那个常量是可被重定向的
# (`--extra-root` 的仓库外插件自检、以及把它指向 tmp 目录的几条自检用例),而这条
# 通道的扫描面是本仓库里具名模块,与插件自检面没有任何关系。跟着一个会被
# 重定向的根走,只会让"把后端根指到别处"的那几种调用顺带把这条通道也一起关掉。
TRACE_SUMMARY_MODULES = (
    ROOT / "backend" / "app" / "services" / "reasoning_retrieval.py",
    ROOT / "backend" / "app" / "services" / "ask_service.py",
    ROOT / "backend" / "app" / "services" / "global_ask.py",
    ROOT / "backend" / "app" / "services" / "plugin_ask_engine.py",
    ROOT / "backend" / "app" / "services" / "spreadsheet_analysis.py",
    ROOT / "backend" / "app" / "services" / "structured_retrieval.py",
    # 「就此追问」的 report 轨迹步(读了报告哪几节 / 为什么没读)。
    ROOT / "backend" / "app" / "services" / "report_followup.py",
)
TRACE_STEP = "TraceStep"
# Non-vacuity floor: keep the existing guard while removing retired modules.
MIN_TRACE_SUMMARY_SITES = 50

# —— 第五条通道:服务端写、前端逐字渲染的**展示字段** ——————————————————————
# 与轨迹摘要同构:这些字段由后端写进接口,前端原样上屏,不经 frontend/app。按**显式
# 登记的调用形状**匹配,不按 kwarg 名泛扫——`error_message=` 在删除作业、来源表等处
# 是不上屏的诊断原文,泛扫只会误报。新增一条会原样上屏的后端字段时,把它的形状登记
# 进这张表。
#
#   * `IndexingPipelineOption(label=…, description=…)`:索引管线选项,前端索引管线
#     设置原样渲染 label 与 description(page.tsx `{option.description}`)。
#   * `<…>.kg_build_jobs.finish/set_stage(…, error_message=…)`:知识图谱分析作业的
#     失败文案,经 `KgBuildJobStatus.user_message` 由前端终态提示原样弹出。
#   * `self.finish(…, error_message=…)`:同一张表在作业存储内部的写入
#     (`fail_submission`),只在 KG_BUILD_JOB_STORE_MODULES 里认。
#   * `LinkedReportReceipt(reason=…)` / `ReportLinkCheck(reason=…)`:「就此追问」不读
#     报告的原因,回执在标签旁、就地预检在发送前原样上屏。正式写法从第八条通道的
#     出处常量取(跨模块导入 + 下标,这里静态解析不了、不计站点);登记形状挡的是
#     有人在调用点直接写字面量或本模块常量。
#   * 接收者是名字时再看一跳别名:`jobs = r.kg_build_jobs; jobs.finish(…)` 同样认。
#   * SQL 字面量 `UPDATE kg_build_jobs SET … error_message='…'`:启动兜底把残留的
#     running 作业落成 failed(sqlite/migrations.py、postgres/maintenance.py),这句
#     同样经 user_message 上屏(`KG_BUILD_JOB_SQL_MESSAGE`)。
#
# 已知边界:`finish` / `set_stage` 的 error_message 是**仅限关键字**参数(两端存储
# 签名里在 `*` 之后),位置参数传不进来,所以不按位置定位;`**kwargs` 展开、多于一跳
# 的别名与跨模块传入的接收者静态解析不了,刻意跳过(与其它通道「解析不了就不查」一致)。
#
# 扫描面是本仓库 backend/app 全部(按 ROOT 写死,理由同 TRACE_SUMMARY_MODULES);
# DISPLAY_FIELD_ANCHOR_MODULES 是这几种形状现有出处的存在性哨兵,搬家/改名时一起改。
DISPLAY_FIELD_ROOT = ROOT / "backend" / "app"
DISPLAY_FIELD_CALLS = {
    # 调用名 → 上屏的 kwarg
    "IndexingPipelineOption": ("label", "description"),
    "LinkedReportReceipt": ("reason",),
    "ReportLinkCheck": ("reason",),
}
DISPLAY_FIELD_METHODS = {
    # 接收者属性链的末段 → (方法名 → 上屏的 kwarg)
    "kg_build_jobs": {"finish": ("error_message",), "set_stage": ("error_message",)},
}
KG_BUILD_JOB_STORE_MODULES = (
    ROOT / "backend" / "app" / "repositories" / "sqlite" / "kg_build_job_store.py",
    ROOT / "backend" / "app" / "repositories" / "postgres" / "kg_build_job_store.py",
)
_DISPLAY_FIELD_TOKENS = (*DISPLAY_FIELD_CALLS, *DISPLAY_FIELD_METHODS)
_STORE_MODULES_RELATIVE = frozenset(
    p.relative_to(DISPLAY_FIELD_ROOT) for p in KG_BUILD_JOB_STORE_MODULES
)
DISPLAY_FIELD_ANCHOR_MODULES = (
    ROOT / "backend" / "app" / "services" / "indexing_pipeline.py",
    ROOT / "backend" / "app" / "services" / "notebook_catalog.py",
    ROOT / "backend" / "app" / "services" / "knowledge_lifecycle.py",
    *KG_BUILD_JOB_STORE_MODULES,
    ROOT / "backend" / "app" / "repositories" / "sqlite" / "migrations.py",
    ROOT / "backend" / "app" / "repositories" / "postgres" / "maintenance.py",
    ROOT / "backend" / "app" / "services" / "report_followup.py",
)
# SQL 字面量里写进作业表的展示文案。只认 `error_message='…'` 这种直接写死的值;
# 占位符(`error_message=?` / `%s`)的值来自调用方,由上面的调用形状覆盖。
KG_BUILD_JOB_SQL_MESSAGE = re.compile(
    r"UPDATE\s+kg_build_jobs\s+SET\b[^;]*?\berror_message\s*=\s*'((?:[^']|'')*)'",
    re.IGNORECASE,
)
# 非空性下限(可解析站点)。2026-10 实测见 test_ui_vocabulary_guard 的真实扫描用例。
MIN_DISPLAY_FIELD_SITES = 10

# —— 第六条通道:模型工作负载显示名 ——————————————————————————————————
# `_WORKLOAD_LABELS` 的值作为 `workload_label` 原样上屏(模型失败横幅、管理员用量
# 页、解析问题档案)。只认模块顶层 `_WORKLOAD_LABELS = {…}` / `MappingProxyType({…})`
# 里的字符串字面量值;写法变了扫不到时由下面的非空性下限把它变成硬失败。
WORKLOAD_LABEL_MODULE = ROOT / "backend" / "app" / "services" / "model_registry.py"
WORKLOAD_LABEL_NAME = "_WORKLOAD_LABELS"
# 2026-10 实测 36 条;下限只防「扫描面塌了」,不是精确台账。
MIN_WORKLOAD_LABELS = 30
# 同一通道的第二部分:仓库**出厂**的模型服务显示名。它们会被写进部署的
# model-services.toml(迁移脚本生成、或照模板拷贝),再经 display_name 原样上屏
# (模型服务状态卡片)。运维自己在 TOML 里写的名字是部署数据,守卫够不着,也不该管。
SERVICE_LABEL_DICTS = (
    (ROOT / "scripts" / "migrate_legacy_model_env.py", "ROLE_LABELS"),
)
SERVICE_LABEL_TEMPLATE = ROOT / "model-services.example.toml"
# 2026-10:ROLE_LABELS 6 条 + 模板 4 条。
MIN_SERVICE_LABELS = 8

# —— 第七条通道:公共库「收录自」合成来源的标题前缀 ——————————————————————
# 前缀常量的值就是来源标题的开头,标题原样上屏。只扫 PROMOTION_TITLE_NAMES 点名的两个
# 模块顶层常量;`LEGACY_*` 是存量标题的存储拼写(读时映射成新前缀),不在此列。
PROMOTION_TITLE_MODULE = ROOT / "backend" / "app" / "domain" / "promotion_source.py"
PROMOTION_TITLE_NAMES = ("PROMOTION_TITLE_PREFIX", "MEMORY_PROMOTION_TITLE_PREFIX")

# —— 第八条通道:「就此追问」不读报告的原因文案 ——————————————————————————
# 回执 `LinkedReportReceipt.reason`、标签旁就地预检 `ReportLinkCheck.reason` 与推理
# 轨迹 report 步原样上屏的都是同一句话:前缀 `REASON_PREFIX` + 一个子句。子句只写在
# 下面这个模块的具名常量里(单一出处),各调用点经跨模块导入与
# `REASON_CLAUSES[code].format(…)` 取用——第二、三、五条通道的解析器按设计不跟跨模块
# 导入与下标,在调用点扫不到它们;所以这里直接扫出处。字符串常量与字典(含
# `MappingProxyType({…})` 包一层)的每个值都是一条;解析不了的报 UNRESOLVED_LABEL。
# 名字按集合核对(同第七条):改名或删掉其中一个就红,不许静默塌面。
REPORT_REASON_MODULE = ROOT / "backend" / "app" / "domain" / "report_followup.py"
REPORT_REASON_NAMES = (
    "REASON_PREFIX",
    "REASON_CLAUSES",
    "REASON_CLAUSE_MEMORY_CLOSED_NARROWED",
    "REASON_CLAUSE_MEMORY_CLOSED_CHANNEL",
)

# ASCII acronyms/words: matched only when not glued to another ASCII letter, so
# identifiers (currentNotebook, MemoryPanel, schemaBusy, PKG) cannot trip them.
ASCII_TERMS = {
    "KG": re.compile(r"(?<![A-Za-z])KG(?![A-Za-z])"),
    "CSR": re.compile(r"(?<![A-Za-z])CSR(?![A-Za-z])"),
    "ANN": re.compile(r"(?<![A-Za-z])ANN(?![A-Za-z])"),
    # 档位说明曾长期无人渲染，攒下「不把枚举伪装成大 TopN」这种开发者措辞，直到档位
    # 控件收敛才第一次上屏。检索取数方式对用户没有意义，界面只讲效果。
    "TopN": re.compile(r"(?<![A-Za-z])top-?n(?![A-Za-z])", re.IGNORECASE),
    "chunk": re.compile(r"(?<![A-Za-z])chunks?(?![A-Za-z])", re.IGNORECASE),
    "notebook": re.compile(r"(?<![A-Za-z])notebooks?(?![A-Za-z])", re.IGNORECASE),
    "Memory": re.compile(r"(?<![A-Za-z])memory(?![A-Za-z])", re.IGNORECASE),
    "schema": re.compile(r"(?<![A-Za-z])schemas?(?![A-Za-z])", re.IGNORECASE),
    "deprecated": re.compile(r"(?<![A-Za-z])deprecated(?![A-Za-z])", re.IGNORECASE),
    "spreadsheet analysis": re.compile(
        r"(?<![A-Za-z])spreadsheet analysis(?![A-Za-z])", re.IGNORECASE
    ),
    "workbook analysis": re.compile(
        r"(?<![A-Za-z])workbook analysis(?![A-Za-z])", re.IGNORECASE
    ),
    "analysis issue": re.compile(
        r"(?<![A-Za-z])analysis issues?(?![A-Za-z])", re.IGNORECASE
    ),
    "parse failure": re.compile(
        r"(?<![A-Za-z])parse failures?(?![A-Za-z])", re.IGNORECASE
    ),
    # —— 图谱质量分析视图（批 1）新增。这些挡的是**中文文案里夹带英文黑话**;裸标识符
    # (`const canonicalId`) 不在扫描面内——本守卫只在含中文的单元里匹配,且先剥注释/
    # 标识符/插值。
    #
    # `canonical` 是**补一个既有缺口**,不只是本特性的需要:界面词汇表一直
    # 把它与 projection/tier/chunk/KG/schema 并列为黑话,ASCII_TERMS 却从来没有它,
    # 直到本特性往词表加行、触发「词表 ⊇ 守卫覆盖」自检才暴露。
    "canonical": re.compile(r"(?<![A-Za-z])canonical(?![A-Za-z])", re.IGNORECASE),
    "cluster": re.compile(r"(?<![A-Za-z])clusters?(?![A-Za-z])", re.IGNORECASE),
    "community": re.compile(r"(?<![A-Za-z])communit(?:y|ies)(?![A-Za-z])", re.IGNORECASE),
    "outlier": re.compile(r"(?<![A-Za-z])outliers?(?![A-Za-z])", re.IGNORECASE),
    "centrality": re.compile(r"(?<![A-Za-z])centrality(?![A-Za-z])", re.IGNORECASE),
    # 检索结束事实(设计稿 2026-09-07 §7.2):一个必答方面的支撑全被最终装配的
    # 预算/过滤挡在合成之外,界面说「未送达」。内部名不上屏。
    "undelivered": re.compile(r"(?<![A-Za-z])undelivered(?![A-Za-z])", re.IGNORECASE),
    # —— 图谱分析「问题清单」(设计 2026-10-19 §十)。规则、状态与结构口径的内部名
    # 夹在中文文案里上屏就是黑话;界面词见 docs/ui-vocabulary.md 对应各行。
    "finding": re.compile(r"(?<![A-Za-z])findings?(?![A-Za-z])", re.IGNORECASE),
    "dismiss": re.compile(r"(?<![A-Za-z])dismiss(?:ed|al)?(?![A-Za-z])", re.IGNORECASE),
    "exemption": re.compile(r"(?<![A-Za-z])exemptions?(?![A-Za-z])", re.IGNORECASE),
    "off-topic": re.compile(r"(?<![A-Za-z])off-?topic(?![A-Za-z])", re.IGNORECASE),
    "duplicate import": re.compile(r"(?<![A-Za-z])duplicate imports?(?![A-Za-z])", re.IGNORECASE),
    "parse damage": re.compile(r"(?<![A-Za-z])parse damage(?![A-Za-z])", re.IGNORECASE),
    "under-merge": re.compile(r"(?<![A-Za-z])under-?merg(?:e|ed|ing)(?![A-Za-z])", re.IGNORECASE),
    "giant component": re.compile(r"(?<![A-Za-z])giant components?(?![A-Za-z])", re.IGNORECASE),
    "island component": re.compile(r"(?<![A-Za-z])island components?(?![A-Za-z])", re.IGNORECASE),
    "handled": re.compile(r"(?<![A-Za-z])handled(?![A-Za-z])", re.IGNORECASE),
    "dev-side defect": re.compile(r"(?<![A-Za-z])dev-side defects?(?![A-Za-z])", re.IGNORECASE),
    # 重新解析已提交、这一次还没跑完时入口上的占位:界面说「已提交重新解析，等待完成」。
    "reparse placeholder": re.compile(r"(?<![A-Za-z])reparse placeholders?(?![A-Za-z])", re.IGNORECASE),
    # 两个来源的概念集合重合度在界面上叫「概念重合度」,算法名不上屏。
    "Jaccard": re.compile(r"(?<![A-Za-z])jaccard(?![A-Za-z])", re.IGNORECASE),
    # 待确认合并的「同名不同写法」(kind=spelling_variant,设计 2026-10-19 §4.4):
    # squash key 是合并种子的内部口径,英文词名不上屏。
    "squash": re.compile(r"(?<![A-Za-z])squash(?![A-Za-z])", re.IGNORECASE),
    "spelling variant": re.compile(
        r"(?<![A-Za-z])spelling[ _]variants?(?![A-Za-z])", re.IGNORECASE
    ),
}

# CJK jargon. Regex (not plain substring) so the ambiguous rows can carry their
# context condition in the pattern itself rather than in a reviewer's head.
CJK_TERMS = {
    # —— 基准库 / 个人层 一行
    "基准库": re.compile(r"基准库"),
    "基准语料": re.compile(r"基准语料"),
    "底层库": re.compile(r"底层库"),
    "权威参考层": re.compile(r"权威参考层"),
    "个人层": re.compile(r"个人层"),
    # —— 建图 / 入图 一行。「入图」是子串匹配,中文没有词边界,所以两侧都要挡掉
    # 正常词组:后面跟「谱」是「加入图谱」这类动宾,前面是「插」则是「插入图片」
    # (编辑器里最普通不过的一句话,曾误报 3 处)。只有 未入图 / 已入图 / 入图状态
    # 这种状态说法才是黑话。
    "建图": re.compile(r"建图"),
    # 「构建 / 建立 / 重建 + 知识图谱」作动作 → 界面词是「整理知识图谱」。裸「构建」
    # 「建立」是正常动词,不进黑名单;只有跟「知识图谱」连写才是黑话。
    "构建知识图谱": re.compile(r"(?:构建|建立|重建)知识图谱"),
    "入图": re.compile(r"(?<!插)入图(?!谱)"),
    # —— 抽取一行。「抽取字段」是本表给 schema 定的**界面词**,不能自己杀自己。
    "抽取": re.compile(r"抽取(?!字段)"),
    "补抽": re.compile(r"补抽"),
    "重抽": re.compile(r"重抽"),
    # —— 索引一行(CSR/ANN 在 ASCII_TERMS)
    "向量检索索引": re.compile(r"向量检索索引"),
    "暴力检索": re.compile(r"暴力检索"),
    # —— 节点 / 边 两行:只收无歧义复合形态,理由见上方 docstring 与 docs/ui-vocabulary.md
    "孤立节点": re.compile(r"孤立节点"),
    "补连边": re.compile(r"补连边"),
    "关系边": re.compile(r"关系边"),
    "边审": re.compile(r"边审"),
    # —— 「Agent 记录」两种行:界面上一律叫「调用记录」,而它按哪一档能力被准许
    # (ask:execute 这类协议串)不上屏,只说这次调用做了什么。两个词都是无歧义的
    # 复合形态,不会误伤正常措辞。
    "调用记账": re.compile(r"调用记账"),
    "能力档": re.compile(r"能力档"),
    # 检索结束事实(设计稿 §7.2)的服务端记账名。界面只说「方面」——「必答」是
    # 契约侧的措辞,上屏会让用户以为自己漏答了什么。无歧义复合形态,不误伤。
    "必答方面": re.compile(r"必答方面"),
    # —— 其余各行
    "投影": re.compile(r"投影"),
    "预审": re.compile(r"预审"),
    "去重": re.compile(r"去重"),
    "晋升": re.compile(r"晋升"),
    # —— Agentic Memory P1「AI 对这个库的理解」。「巡固」是该特性的后台整理动作，是
    # 内部实现词，界面词是「重新整理」。裸「画像」**不**收进这里——图谱质量分析视图
    # 早已把「来源画像」用作合法界面词（source profile，见 kg-analysis-view.tsx），
    # 裸子串匹配会连它一起误杀；与裸「节点」/「边」同一处理：登记进 NOT_LINTABLE，
    # 靠人工把关。但**无歧义的复合形态**照「孤立节点」/「关系边」的既有政策收进黑名
    # 单——「底座画像」/「覆盖层画像」不可能是「来源画像」的子串，却是本特性开发者
    # 最顺手写出的措辞（后端注释与文档通篇是「底座层/覆盖层」）。
    "巡固": re.compile(r"巡固"),
    "底座/覆盖层画像": re.compile(r"(?:底座|覆盖层)画像"),
    # —— 图谱质量分析视图（批 1）。界面词是「主题板块」。
    # 「社区」是这四个新词条里**唯一**真正会被误写进中文文案的:图算法里叫社区,
    # 顺手就写成「社区规模」。本产品语境下没有「用户社区」这种正当用法,故直接收。
    # 「簇」只收**复合形态**「canonical 簇 / 概念簇 / 合并簇」:裸「簇」在中文里罕见,
    # 但真要出现(如某天写「簇状图」)不该被这条规则误杀,与「边」那行同一个道理。
    "社区": re.compile(r"社区"),
    "概念簇": re.compile(r"(?:canonical|概念|合并)\s*簇"),
    # —— Agentic Memory P3「Agent 记录」。「观察队列」是该特性 agent_observations
    # 表的内部实现措辞（后端注释/文档通篇如此），界面词是「Agent 记录」——与「巡固」/
    # 「底座/覆盖层画像」同一处理:无歧义复合形态直接收进黑名单。
    "观察队列": re.compile(r"观察队列"),
    # —— Agentic Memory P3「我的回答偏好」。「回答风格偏好」是 config.py 注释里
    # 「检索/回答风格偏好」文档的内部措辞，界面词是「我的回答偏好」。
    "回答风格偏好": re.compile(r"回答风格偏好"),
    # —— 图谱分析「问题清单」(设计 2026-10-19 §五 S3 / §十)。图论里的「连通分量」与
    # 设计稿里的「主连通块」在界面上分别说「主体内容」「孤岛」。两个都是无歧义的复合
    # 形态,不会误伤正常措辞(裸「分量」「连通」不收)。
    "连通分量": re.compile(r"连通分量"),
    "主连通块": re.compile(r"主连通块"),
}

CJK = re.compile(r"[一-鿿]")
INTERP = re.compile(r"\$\{[^{}]*\}")  # template ${...}

# 放行的界面短语:图谱对象类型/字段管理功能的界面名,经产品决策(2026-07-23)定为
# 「图谱 Schema」——EDA/芯片场景下用户反而熟悉 "Schema" 一词。这是本守卫对 schema
# 唯一放行的**复合短语**,像剥 ${…} 插值一样在匹配前从单元里剥掉;裸 schema / 裸
# Schema(不带「图谱」前缀)仍然照抓(见 ASCII_TERMS["schema"] 与其正例)。真源为
# docs/ui-vocabulary.md「界面词汇表」schema 行的放行注记。改动此处必须同步那条注记与
# backend/tests/test_ui_vocabulary_guard.py 的正反例。
#
# 前置断言 (?<![一-鿿]):放行短语的「图谱」前不能紧挨 CJK,否则会吃掉「知识图谱」的
# 尾字「图谱」——「构建知识图谱 Schema」里的「构建知识图谱」违规会被一起剥掉而漏报
# (code-quality 评审 P2)。所有合法用法(<Database/> 图谱 Schema、<h2>图谱 Schema</h2>)
# 的「图谱」前都是空格 / `>` / 串首,非 CJK,不受影响。
# 后置断言 (?![A-Za-z0-9_]):只放行完整单词 Schema,不放行它做**标识符前缀**的更长
# 形态——否则「图谱 Schemas」「图谱 Schematics」「图谱 Schema2」「图谱 Schema_v2」会被
# 当成放行短语剥掉而漏掉其中的 schema 黑话(codex 第 2/3 轮 P2)。这里刻意比
# ASCII_TERMS["schema"] 的 (?![A-Za-z]) **更严**:数字/下划线也算词字符,`Schema2`、
# `Schema_v2` 都不是那个被放行的单词,必须仍能被 ASCII 规则抓到。
# 该剥离统一放在 terms_in() 里,故前端渲染文本与后端 user_error() 两条扫描路径**对称**
# 生效(codex 第 2 轮 P2:界面词是全局契约,后端可展示文案里的「图谱 Schema」同样放行)。
SANCTIONED_UI = re.compile(r"(?<![一-鿿])图谱\s*Schema(?![A-Za-z0-9_])")
STRING = re.compile(r'"(?:[^"\\]|\\.)*"' r"|'(?:[^'\\]|\\.)*'" r"|`(?:[^`\\]|\\.)*`", re.S)
JSXTEXT = re.compile(r"[^<>{}]+")     # bare JSX text: a run between tags/expressions


# A `/` right after one of these (or at the start) opens a regex literal, not a
# division — regex bodies may contain quotes that would otherwise desync the string
# scanner (and then swallow a later `// 中文` comment). Covers the (,= cases in use.
_REGEX_OK_BEFORE = set("([{,;:=!&|?+-*%^~<>")


def blank_comments(text: str) -> str:
    """Replace // and /* */ comments *and regex-literal bodies* with spaces (newlines
    kept, length preserved — offsets stay valid for line numbers), respecting string,
    template, and regex literals so comment markers inside them survive. Quoted strings
    reset to code at a newline (they can't span lines), which bounds any mis-parse to a
    single line.

    Regex bodies are blanked rather than copied because the naive STRING regex that
    later extracts string literals does not know about regex literals: a body such as
    `/\\son\\w+\\s*=\\s*("[^"]*"|'[^']*'|[^\\s>]+)/gi` (page.tsx sanitizeTableHtml)
    contains unbalanced quotes, and copying it verbatim made STRING match from inside
    the regex to some later quote — dragging whole spans of code into the "rendered
    text" units and reporting them as UI copy. Regex bodies are code and are never
    rendered, so blanking loses nothing."""
    out: list[str] = []
    i, n, state, last = 0, len(text), "code", ""
    while i < n:
        c = text[i]
        two = text[i:i + 2]
        if state == "code":
            if two == "//":
                out.append("  "); i += 2; state = "line"; continue
            if two == "/*":
                out.append("  "); i += 2; state = "block"; continue
            if c == "/" and (last == "" or last in _REGEX_OK_BEFORE):
                out.append(c); i += 1; in_class = False
                while i < n:                       # blank the regex body
                    ch = text[i]
                    if ch == "\n":
                        break                      # unterminated on this line → recover
                    if ch == "\\" and i + 1 < n:
                        out.append("  "); i += 2; continue
                    if ch == "[":
                        in_class = True
                    elif ch == "]":
                        in_class = False
                    elif ch == "/" and not in_class:
                        out.append(c); i += 1; break
                    out.append(" ")
                    i += 1
                last = "/"; continue
            out.append(c)
            if c not in " \t":
                last = c
            state = {"'": "sq", '"': "dq", "`": "tpl"}.get(c, "code")
            i += 1; continue
        if state in ("sq", "dq", "tpl"):
            if c == "\n" and state != "tpl":
                out.append("\n"); state = "code"; last = ""; i += 1; continue
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(text[i + 1]); i += 2; continue
                i += 1; continue
            if (state, c) in (("sq", "'"), ("dq", '"'), ("tpl", "`")):
                state = "code"; last = c
            i += 1; continue
        if state == "line":
            out.append("\n" if c == "\n" else " "); state = "code" if c == "\n" else "line"
            i += 1; continue
        if state == "block":
            if two == "*/":
                out.append("  "); i += 2; state = "code"; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
    return "".join(out)


def blank_strings(text: str) -> str:
    """Replace string literals with equal-length blanks (newlines kept). Their
    content is scanned separately; blanking them leaves bare JSX text as the only
    remaining CJK in the file, so JSX-text extraction can't drag in code."""
    return STRING.sub(lambda m: "".join("\n" if c == "\n" else " " for c in m.group(0)), text)


def terms_in(unit: str) -> list[str]:
    # 放行的界面短语(「图谱 Schema」)在匹配前剥掉,同 ${…} 插值。放在这里(而非各调用点)
    # 让前端渲染文本 scan() 与后端 user_error() scan_user_error() 两条路径对称生效。
    unit = SANCTIONED_UI.sub(" ", unit)
    found = [name for name, pat in CJK_TERMS.items() if pat.search(unit)]
    found += [name for name, pat in ASCII_TERMS.items() if pat.search(unit)]
    return found


def scan(path: Path) -> list[tuple[int, str, str]]:
    """Blacklisted jargon in this file's rendered text: (line, term, unit)."""
    blanked = blank_comments(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str, str]] = []

    def record(off: int, text: str, unit: str) -> None:
        if not CJK.search(unit):
            return
        # A JSX text run starts right after the previous tag's `>`, so it usually
        # opens with a newline + indentation; counting newlines up to its *start*
        # would name the line above the copy. Skip the run's leading whitespace so
        # the reported line is the one the developer has to edit.
        off += len(unit) - len(unit.lstrip())
        # 放行短语的剥离已下沉进 terms_in()(前后端两条扫描路径共用);报错时仍用原始
        # unit,保证裸 schema/Schema 依旧被抓到。
        for term in terms_in(unit):
            hits.append((text.count("\n", 0, off) + 1, term, unit.strip()))

    # Pass 1 — string literals (title=/label:/placeholder/toast/…). Drop ${…}.
    for m in STRING.finditer(blanked):
        record(m.start(), blanked, INTERP.sub(" ", m.group(0)[1:-1]))
    # Pass 2 — bare JSX text between tags. With strings blanked, any run of
    # non-tag/non-brace chars that still contains CJK is genuine rendered text.
    nostr = blank_strings(blanked)
    for m in JSXTEXT.finditer(nostr):
        record(m.start(), nostr, m.group(0))
    return hits


def _message_literal(node: ast.expr | None) -> str | None:
    """The literal text of a `user_error()` message argument, or None if it isn't one.

    f-string interpolations are dropped and the literal spans joined — the same rule
    the frontend pass applies to `${…}`, so `f"{n} 个来源待抽取"` still trips 抽取.
    This is the *leaf* rule; `_IndirectCopy` builds the indirect forms (variable,
    conditional, concatenation, helper return…) on top of it.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        ]
        return " ".join(parts) if parts else None
    return None


# 后端可展示文案通道(user_error、轨迹摘要、展示字段)专用:匹配前剥掉**已登记**的
# 部署配置键名。「请先关闭 SILICON_NOTEBOOK_AUTH_OPTIONAL」里的 NOTEBOOK 是在告诉
# 运维改哪个配置,不是界面黑话。白名单从 `backend/app/core/config.py` 的
# `validation_alias="…"` 与 `os.environ.get("…")` 推出,不按形状放行——形状放行会
# 把「失败:KG_NOT_READY」这类内部码当配置键一起剥掉(KG 照样是黑话)。裸 notebook /
# 裸 KG 不受影响。刻意**不**放进 terms_in():前端渲染文本与更新说明里没有这种写法,
# 放宽它们的扫描面只会少报。
BACKEND_ENV_VAR = re.compile(r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+(?![A-Za-z0-9_])")
SETTINGS_MODULE = ROOT / "backend" / "app" / "core" / "config.py"
_ENV_READERS = frozenset({"get", "getenv"})


def _registered_env_vars() -> frozenset[str]:
    """Environment-variable names the deployment actually reads (see BACKEND_ENV_VAR)."""
    global _ENV_VAR_CACHE
    if _ENV_VAR_CACHE is None:
        names: set[str] = set()
        if SETTINGS_MODULE.is_file():
            tree = ast.parse(SETTINGS_MODULE.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if kw.arg == "validation_alias":
                        names.update(
                            c.value for c in ast.walk(kw.value)
                            if isinstance(c, ast.Constant) and isinstance(c.value, str)
                        )
                if (isinstance(node.func, ast.Attribute) and node.func.attr in _ENV_READERS
                        and node.args and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    names.add(node.args[0].value)
        _ENV_VAR_CACHE = frozenset(n for n in names if BACKEND_ENV_VAR.fullmatch(n))
    return _ENV_VAR_CACHE


_ENV_VAR_CACHE: frozenset[str] | None = None


def _backend_terms_in(unit: str) -> list[str]:
    registered = _registered_env_vars()
    return terms_in(BACKEND_ENV_VAR.sub(
        lambda m: " " if m.group(0) in registered else m.group(0), unit
    ))


_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPREHENSION_NODES = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
# 名字查找时依次经过的作用域:函数、lambda、推导式(Python 3 的推导式有自己的作用域)。
# 类体不在里面——方法看不见类体里的名字,与 Python 一致。
_LOOKUP_SCOPES = (*_FUNCTION_NODES, ast.Lambda, *_COMPREHENSION_NODES)
# 收集一个函数的绑定时不进入的子树:它们各自是独立作用域。推导式**要**进入——其中的
# `:=` 绑定到外层函数;推导式自己的循环变量在 `_bindings` 里本来就不会被当成外层绑定。
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
# 名字跳转与 helper 调用的跳数上限。只有这两种「换一处定义」的步骤扣深度;`+`、
# 条件表达式、`%`、`.format` 是同一棵表达式树里的结构节点,AST 本身有限,不扣——
# 否则 `"前缀" + a + "," + b + …` 这种长拼接链最左边的文案会被静默丢掉。上限只用来
# 防环(`x = x + "…"`);配合 `(节点, 剩余深度)` 级 memo,自引用的代价是线性的。
MAX_INDIRECTION_DEPTH = 5
# `+` 两侧单元做笛卡儿积的上限。超过时**不截断**:退化为左右两侧各自的单元分别检查
# ——每一侧都是会原样上屏的子串,单侧黑话一条不漏,只放弃跨边界拼出的组合。积的大小
# 先算后生成,超限时根本不生成组合。
MAX_UNITS_PER_EXPR = 64


def _dedupe(units: list[str]) -> list[str]:
    return list(dict.fromkeys(units))


def _bindings(scope: ast.AST) -> tuple[set[str], dict[str, list[ast.expr]]]:
    """Names bound directly in ``scope`` (module, function, lambda or comprehension),
    and the candidate values of the ones with a statically known value.

    Values come from ``name = value``, ``name: T = value``, ``(name := value)`` and
    ``name += value`` (the appended text is a candidate of its own, so jargon added
    that way is still checked). Parameters, loop targets, ``with … as``,
    ``except … as``, imports and tuple unpacking mark a name as local without giving
    it a value — a local without a statically known value shadows the module
    constant of the same name, exactly like Python scoping.

    Known limits (they err towards a false positive, never a silent skip): bindings
    are flow-insensitive within one scope (every value a name ever gets in that
    scope is a candidate, so a later ``except … as`` / loop rebinding of the *same*
    scope's name does not remove an earlier constant); ``global`` / ``nonlocal``
    declarations, ``match`` captures and ``del`` are not modelled.
    """
    local: set[str] = set()
    values: dict[str, list[ast.expr]] = {}

    def mark_targets(target: ast.expr) -> None:
        # 只有真正的绑定目标才算局部:名字,以及元组/列表/星号解包里的名字。
        # `MESSAGES["ok"] = …`、`obj.attr = …` 是**读取** MESSAGES / obj 再改它,
        # 不绑定接收者——把它记成局部会遮蔽同名模块常量,让本该检查的文案漏掉。
        if isinstance(target, ast.Name):
            local.add(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                mark_targets(element)
        elif isinstance(target, ast.Starred):
            mark_targets(target.value)

    if isinstance(scope, _COMPREHENSION_NODES):
        for generator in scope.generators:
            mark_targets(generator.target)
        return local, values
    if isinstance(scope, (*_FUNCTION_NODES, ast.Lambda)):
        args = scope.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                    *(a for a in (args.vararg, args.kwarg) if a is not None)]:
            local.add(arg.arg)
        if isinstance(scope, ast.Lambda):
            return local, values

    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPE_NODES):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                local.add(node.name)
            continue
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local.add(target.id)
                    values.setdefault(target.id, []).append(node.value)
                else:
                    mark_targets(target)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            local.add(node.target.id)
            if node.value is not None:
                values.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.NamedExpr) and isinstance(node.target, ast.Name):
            local.add(node.target.id)
            values.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, ast.AugAssign):
            mark_targets(node.target)
            if isinstance(node.target, ast.Name) and isinstance(node.op, ast.Add):
                values.setdefault(node.target.id, []).append(node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            mark_targets(node.target)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            mark_targets(node.optional_vars)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                local.add((alias.asname or alias.name).split(".")[0])
        stack.extend(ast.iter_child_nodes(node))
    return local, values


class _IndirectCopy:
    """Resolve a user-visible copy expression into the text *units* it can display.

    The backend channels (``user_error`` messages, trace summaries, display fields)
    used to accept only a literal at the call site, so copy written one step away —
    assigned to a variable first, picked by a conditional, concatenated, or returned
    by a module helper — was skipped silently (「横向对比:纳入 … 个同社区实体」shipped
    that way). Supported forms:

      * str constant / f-string — ``_message_literal`` (interpolations dropped);
      * ``a if c else b`` — each branch is its **own** unit, so text from the two
        branches is never glued into a phantom hit;
      * ``a + b`` — the two sides joined into one unit (an unresolvable side counts as
        an interpolation: a blank, so ``"社" + x + "区"`` never reads as 社区); past
        ``MAX_UNITS_PER_EXPR`` combinations each side is checked on its own instead;
      * ``"…" % args`` — the left side; ``"…".format(...)`` — the receiver;
      * ``X.get(k, default)`` — every value of the dict ``X`` resolves to, plus the
        default;
      * a name — the bindings of the innermost enclosing scope that binds it
        (function, lambda, comprehension), then the module level, following Python
        scoping (a parameter / loop variable / ``except … as`` shadows the module
        constant and stays unresolvable); see ``_bindings`` for the modelled forms;
      * a call to a function defined at this module's top level — every ``return``.

    Only name jumps and helper calls spend ``MAX_INDIRECTION_DEPTH``; results are
    memoised per ``(node, remaining depth)``. Anything else (``str(exc)``,
    ``exc.user_message``, imported constants, ``X[k]``…) resolves to no unit and stays
    deliberately unchecked, as before.
    """

    def __init__(self, tree: ast.Module) -> None:
        # 父节点表要遍历整棵树;只有遇到名字才需要作用域链,所以按需建。
        self._parents: dict[ast.AST, ast.AST] | None = None
        self._module = tree
        self._scopes: dict[ast.AST, tuple[set[str], dict[str, list[ast.expr]]]] = {}
        self._memo: dict[tuple[ast.AST, int], tuple[str, ...]] = {}
        self._functions = {
            node.name: node for node in tree.body if isinstance(node, _FUNCTION_NODES)
        }

    def _scope(self, node: ast.AST) -> tuple[set[str], dict[str, list[ast.expr]]]:
        if node not in self._scopes:
            self._scopes[node] = _bindings(node)
        return self._scopes[node]

    def _enclosing_scopes(self, node: ast.AST) -> list[ast.AST]:
        if self._parents is None:
            self._parents = {
                child: parent for parent in ast.walk(self._module)
                for child in ast.iter_child_nodes(parent)
            }
        chain: list[ast.AST] = []
        cur = self._parents.get(node)
        while cur is not None:
            if isinstance(cur, _LOOKUP_SCOPES):
                chain.append(cur)
            cur = self._parents.get(cur)
        return chain

    def _name_values(self, node: ast.Name) -> list[ast.expr]:
        for scope in self._enclosing_scopes(node):
            local, values = self._scope(scope)
            if node.id in local:
                return values.get(node.id, [])
        return self._scope(self._module)[1].get(node.id, [])

    def _locally_bound(self, node: ast.Name) -> bool:
        """True when ``node`` names a binding of an enclosing (non-module) scope."""
        return any(node.id in self._scope(scope)[0] for scope in self._enclosing_scopes(node))

    def _dicts(self, node: ast.expr, depth: int) -> list[ast.Dict]:
        if depth < 0:
            return []
        if isinstance(node, ast.Dict):
            return [node]
        if isinstance(node, ast.Name):
            return [d for v in self._name_values(node) for d in self._dicts(v, depth - 1)]
        return []

    def units(self, node: ast.expr | None, depth: int = MAX_INDIRECTION_DEPTH) -> list[str]:
        if node is None or depth < 0:
            return []
        key = (node, depth)
        if key not in self._memo:
            self._memo[key] = ()  # 计算中:同一 (节点, 深度) 的再入当作解析不了
            self._memo[key] = tuple(self._compute(node, depth))
        return list(self._memo[key])

    def _compute(self, node: ast.expr, depth: int) -> list[str]:
        literal = _message_literal(node)
        if literal is not None:
            return [literal]
        if isinstance(node, ast.IfExp):
            return _dedupe(self.units(node.body, depth) + self.units(node.orelse, depth))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = self.units(node.left, depth), self.units(node.right, depth)
            if not left and not right:
                return []
            lhs, rhs = left or [" "], right or [" "]
            if len(lhs) * len(rhs) > MAX_UNITS_PER_EXPR:
                return _dedupe(left + right)
            return _dedupe([l + r for l in lhs for r in rhs])
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return self.units(node.left, depth)
        if isinstance(node, ast.Name):
            return _dedupe([
                u for v in self._name_values(node) for u in self.units(v, depth - 1)
            ])
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "format":
                return self.units(func.value, depth)
            if isinstance(func, ast.Attribute) and func.attr == "get":
                found = [
                    u for d in self._dicts(func.value, depth)
                    for v in d.values for u in self.units(v, depth - 1)
                ]
                if len(node.args) > 1:
                    found += self.units(node.args[1], depth)
                return _dedupe(found)
            if (isinstance(func, ast.Name) and func.id in self._functions
                    and not self._locally_bound(func)):
                # 与变量同一套作用域规则:被参数 / 局部遮蔽的名字不是模块 helper,
                # 它的值静态未知,按不可解析处理。
                # 递归 helper 不单独设防:每次调用扣一级深度,加上 memo,天然有界。
                return _dedupe([
                    u for ret in self._returns(self._functions[func.id])
                    for u in self.units(ret, depth - 1)
                ])
        return []

    @staticmethod
    def _returns(function: ast.AST) -> list[ast.expr]:
        found: list[ast.expr] = []
        stack = list(ast.iter_child_nodes(function))
        while stack:
            node = stack.pop()
            if isinstance(node, _SCOPE_NODES):
                continue
            if isinstance(node, ast.Return) and node.value is not None:
                found.append(node.value)
            stack.extend(ast.iter_child_nodes(node))
        return found


Hits = list[tuple[int, str, str]]


def _scan_backend_tree(
    tree: ast.Module, *, display: bool, store_module: bool = False,
) -> tuple[int, Hits, int, Hits]:
    """One AST walk for both whole-tree backend channels: ``user_error()`` messages
    and (when ``display``) the registered display fields. Returns
    (user_error sites, user_error hits, resolvable display-field sites, display hits).
    """
    # 绝大多数后端文件两种站点都没有:解析器(要建整棵树的父节点表)按需才建。
    resolver: _IndirectCopy | None = None
    ue_sites, ue_hits, field_sites, field_hits = 0, set(), 0, set()
    def get_resolver() -> _IndirectCopy:
        nonlocal resolver
        resolver = resolver or _IndirectCopy(tree)
        return resolver

    for node in ast.walk(tree):
        if (display and isinstance(node, ast.Constant) and isinstance(node.value, str)
                and "kg_build_jobs" in node.value):
            for match in KG_BUILD_JOB_SQL_MESSAGE.finditer(node.value):
                field_sites += 1
                text = match.group(1).replace("''", "'")
                for term in _backend_terms_in(text):
                    field_hits.add((node.lineno, term, text))
            continue
        if not isinstance(node, ast.Call):
            continue
        # `user_error(...)` 与 `deps.user_error(...)` 都要认。
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name == USER_ERROR:
            ue_sites += 1
            resolver = resolver or _IndirectCopy(tree)
            kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            message = kwargs.get("message", node.args[1] if len(node.args) > 1 else None)
            for text in resolver.units(message):
                for term in _backend_terms_in(text):
                    ue_hits.add((node.lineno, term, text))
        fields = _display_field_kwargs(node, store_module, get_resolver) if display else ()
        if not fields:
            continue
        resolver = resolver or _IndirectCopy(tree)
        for keyword in node.keywords:
            if keyword.arg not in fields:
                continue
            units = resolver.units(keyword.value)
            if units:
                field_sites += 1
            for text in units:
                for term in _backend_terms_in(text):
                    field_hits.add((node.lineno, term, text))
    return ue_sites, sorted(ue_hits), field_sites, sorted(field_hits)


def scan_user_error(path: Path) -> tuple[int, Hits]:
    """Blacklisted jargon in this file's `user_error()` messages.

    Messages written indirectly (variable, conditional, concatenation, module
    constant, module helper return — see ``_IndirectCopy``) are resolved; what
    still cannot be resolved statically is skipped on purpose and covered at
    runtime by `tests/test_user_error.py`.

    Returns (number of user_error call sites seen, hits) — the count feeds the
    non-vacuity floor, so a rename that silently empties the scan is caught.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sites, hits, _, _ = _scan_backend_tree(tree, display=False)
    return sites, hits


def scan_trace_summaries(
    path: Path, tree: ast.Module | None = None,
) -> tuple[int, list[tuple[int, str, str]]]:
    """Blacklisted jargon in this module's server-written trace summaries.

    Scan ``TraceStep(..., summary=...)`` values rendered by the front end, literal
    or written indirectly (see ``_IndirectCopy``). Summaries that still cannot be
    resolved statically are skipped, exactly like an unresolvable ``user_error``
    message. Returns (resolvable sites seen, hits) — the count feeds the
    non-vacuity floor. ``tree`` lets ``main`` reuse the parse of the backend pass.
    """
    if tree is None:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    resolver = _IndirectCopy(tree)
    sites, hits = 0, set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if name != TRACE_STEP:
            continue
        for keyword in node.keywords:
            if keyword.arg != "summary":
                continue
            units = resolver.units(keyword.value)
            if units:
                sites += 1
            for text in units:
                for term in _backend_terms_in(text):
                    hits.add((node.lineno, term, text))
    return sites, sorted(hits)


def _receiver_tail(node: ast.expr) -> str | None:
    """Last segment of a call receiver: `self.kg_build_jobs` → 'kg_build_jobs'."""
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


_DISPLAY_METHOD_NAMES = frozenset(
    name for methods in DISPLAY_FIELD_METHODS.values() for name in methods
)


def _display_field_kwargs(
    node: ast.Call, store_module: bool, resolver: Callable[[], _IndirectCopy],
) -> tuple[str, ...]:
    func = node.func
    if isinstance(func, ast.Name):
        return DISPLAY_FIELD_CALLS.get(func.id, ())
    if not isinstance(func, ast.Attribute) or func.attr not in _DISPLAY_METHOD_NAMES:
        return ()
    tail = _receiver_tail(func.value)
    if store_module and tail == "self":
        tail = "kg_build_jobs"
    if tail not in DISPLAY_FIELD_METHODS and isinstance(func.value, ast.Name):
        # 一跳别名:`jobs = self.kg_build_jobs; jobs.finish(…)`。
        aliased = {_receiver_tail(v) for v in resolver()._name_values(func.value)}
        tail = next((t for t in aliased if t in DISPLAY_FIELD_METHODS), tail)
    return DISPLAY_FIELD_METHODS.get(tail or "", {}).get(func.attr, ())


def scan_display_fields(path: Path, *, store_module: bool = False) -> tuple[int, Hits]:
    """Blacklisted jargon in the registered display fields (see DISPLAY_FIELD_CALLS).

    ``store_module`` marks a KG build job store module, where ``self.finish(…)``
    writes the same displayed column. Returns (resolvable field sites, hits).
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    _, _, sites, hits = _scan_backend_tree(tree, display=True, store_module=store_module)
    return sites, hits


# 显示名解析不了时报的「词」:第六条通道不许静默跳过——一个看不出内容的显示名
# 恰恰是黑话能溜过去的那种写法。
UNRESOLVED_LABEL = "无法静态解析的显示名"


def _strict_label_units(
    node: ast.expr, resolver: _IndirectCopy, depth: int = MAX_INDIRECTION_DEPTH
) -> list[str] | None:
    """Every text a display-label expression can produce, or ``None``.

    Stricter than ``_IndirectCopy.units`` on purpose: there an unresolvable side
    of ``+`` becomes a blank so a partly-known message is still checked; a
    display label is a closed in-code value, so ANY unresolvable part (an
    imported name, a call, an f-string interpolation) makes the whole label
    unresolvable and the caller reports it instead of skipping it. Names are
    looked up with ``_IndirectCopy``'s scoping (module constants, shadowing).
    """
    if depth < 0:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _strict_label_units(node.left, resolver, depth)
        right = _strict_label_units(node.right, resolver, depth)
        if left is None or right is None:
            return None
        return _dedupe([a + b for a in left for b in right])
    if isinstance(node, ast.IfExp):
        body = _strict_label_units(node.body, resolver, depth)
        orelse = _strict_label_units(node.orelse, resolver, depth)
        return None if body is None or orelse is None else _dedupe(body + orelse)
    if isinstance(node, ast.Name):
        values = resolver._name_values(node)
        if not values:
            return None
        found: list[str] = []
        for value in values:
            units = _strict_label_units(value, resolver, depth - 1)
            if units is None:
                return None
            found += units
        return _dedupe(found)
    return None


def _label_hits(line: int, units: list[str] | None, source: str) -> Hits:
    if units is None:
        return [(line, UNRESOLVED_LABEL, source)]
    return [
        (line, term, text)
        for text in units if CJK.search(text)
        for term in terms_in(text)
    ]


def scan_workload_labels(path: Path, name: str | None = None) -> tuple[int, Hits]:
    """Blacklisted jargon in the model workload display labels (sixth channel).

    Returns (labels found, hits). Only the module-level ``name`` (default
    ``WORKLOAD_LABEL_NAME``) assignment is read; its value may be a dict literal
    or a single-argument call wrapping one (``MappingProxyType({...})``). Every
    value counts as a site; a value is resolved through concatenation and module
    constants (``_strict_label_units``), and one that cannot be resolved is
    reported as ``UNRESOLVED_LABEL`` rather than skipped.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    resolver = _IndirectCopy(tree)
    sites = 0
    hits: Hits = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        if (name or WORKLOAD_LABEL_NAME) not in names or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.Call) and len(value.args) == 1 and not value.keywords:
            value = value.args[0]
        if not isinstance(value, ast.Dict):
            continue
        for key, label in zip(value.keys, value.values):
            sites += 1
            # ``**other`` 展开(key 为 None)同样看不出内容,按解析不了报。
            units = None if key is None else _strict_label_units(label, resolver)
            hits += _label_hits(label.lineno, units, ast.unparse(label))
    return sites, hits


def scan_promotion_title_prefixes(path: Path) -> tuple[int, Hits]:
    """Blacklisted jargon in the promotion source title prefixes (seventh channel).

    Returns (constants found, hits); see ``_scan_promotion_title_names`` for the
    names themselves."""
    found, hits = _scan_promotion_title_names(path)
    return len(found), hits


def _scan_promotion_title_names(path: Path) -> tuple[set[str], Hits]:
    """The ``PROMOTION_TITLE_NAMES`` constants found in ``path`` and their hits
    (see ``_scan_named_texts``)."""
    found, hits, _ = _scan_named_texts(path, PROMOTION_TITLE_NAMES)
    return found, hits


def _scan_named_texts(path: Path, wanted_names: tuple[str, ...]) -> tuple[set[str], Hits, int]:
    """The named module-level text constants of ``path``: (names found, hits, texts).

    Every name must be a module-level assignment resolvable to string text --
    a string (through concatenation and module constants), or a dict literal
    (bare or wrapped in one single-argument call such as ``MappingProxyType``)
    whose every value is one; each string or dict value is one text. A name that
    is missing is simply not in the returned set (callers compare the SET with
    their names, so a renamed constant cannot hide behind a count that another
    name happens to fill); a text that cannot be resolved, or a ``**`` spread,
    is reported as ``UNRESOLVED_LABEL``.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    resolver = _IndirectCopy(tree)
    found: set[str] = set()
    hits: Hits = []
    texts = 0
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        wanted = set(names) & set(wanted_names)
        if node.value is None or not wanted:
            continue
        found |= wanted
        value = node.value
        if isinstance(value, ast.Call) and len(value.args) == 1 and not value.keywords \
                and isinstance(value.args[0], ast.Dict):
            value = value.args[0]
        if isinstance(value, ast.Dict):
            for key, text in zip(value.keys, value.values):
                texts += 1
                units = None if key is None else _strict_label_units(text, resolver)
                hits += _label_hits(text.lineno, units, ast.unparse(text))
            continue
        texts += 1
        hits += _label_hits(
            value.lineno,
            _strict_label_units(value, resolver),
            ast.unparse(value),
        )
    return found, hits, texts


def scan_report_reason_texts(path: Path) -> tuple[int, Hits]:
    """Blacklisted jargon in the report follow-up reason texts (eighth channel).

    Returns (texts found, hits) over ``REPORT_REASON_NAMES``; see
    ``_scan_named_texts``."""
    _, hits, texts = _scan_named_texts(path, REPORT_REASON_NAMES)
    return texts, hits


def scan_service_label_template(path: Path) -> tuple[int, Hits]:
    """Blacklisted jargon in the shipped template's ``services.*.display_name``.

    Parsed with ``tomllib`` (trailing comments, quoting and table layout are the
    TOML grammar's business, not a regex's), so the site count equals the number
    of ``[services.*]`` tables; a service whose ``display_name`` is missing or not
    a string is reported as ``UNRESOLVED_LABEL``. Lines point at the table header.
    """
    import tomllib

    text = path.read_text(encoding="utf-8")
    services = tomllib.loads(text).get("services", {})
    lines = text.splitlines()
    sites = 0
    hits: Hits = []
    for service_id, definition in services.items() if isinstance(services, dict) else ():
        sites += 1
        header = f"[services.{service_id}]"
        line = next(
            (i for i, row in enumerate(lines, 1) if row.strip() == header), 1
        )
        label = definition.get("display_name") if isinstance(definition, dict) else None
        units = [label] if isinstance(label, str) else None
        hits += _label_hits(line, units, f"{service_id}.display_name")
    return sites, hits


def scan_release_note(path: Path) -> list[tuple[int, str, str]]:
    """Blacklisted jargon in one release note (markdown shown to users verbatim).

    Each line is a unit; the same CJK-gated ``terms_in`` rules as the frontend scan apply.
    """
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not CJK.search(line):
            continue
        for term in terms_in(line):
            hits.append((lineno, term, line.strip()))
    return hits


def _release_note_files() -> list[Path]:
    if not RELEASE_NOTES_DIR.is_dir():
        return []
    return sorted(p for p in RELEASE_NOTES_DIR.glob("*.md") if p.name != "README.md")


def _rel(path: Path) -> str:
    """Repo-relative path for reporting; falls back to the absolute one when the
    scan root has been redirected (the self-tests point it at a tmpdir)."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extra-root",
        action="append",
        default=[],
        metavar="DIR",
        help=(
            "repeatable; scan this directory's **/*.py user_error(...) messages in "
            "addition to backend/app (out-of-tree deployment plugin self-check — see "
            "the module docstring). Only widens the scan face: omitting it leaves "
            "every check and every line of output unchanged."
        ),
    )
    # `argv is None` here means "called with no argument at all" (e.g. the direct
    # `guard.main()` calls in test_ui_vocabulary_guard.py), not "read this process's
    # sys.argv" — unlike the CLI entry point below, which explicitly forwards
    # `sys.argv[1:]`. Falling back to argparse's own None-means-sys.argv default
    # would make every in-process `main()` call parse pytest's own arguments.
    return parser.parse_args(argv if argv is not None else [])


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    extra_roots = [Path(p) for p in args.extra_root]

    files = sorted(
        p
        for directory in FRONTEND_PRODUCTION_DIRS
        for p in directory.rglob("*")
        if p.suffix in (".ts", ".tsx")
        and ".test." not in p.name
        and not p.name.endswith(".d.mts")
    )
    if not files:
        print("check_ui_vocabulary: no frontend production sources found", file=sys.stderr)
        return 1
    violations: list[str] = []
    for path in files:
        rel = _rel(path)
        for line, term, snippet in scan(path):
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            violations.append(f"  {rel}:{line}: 「{term}」in rendered text: {snippet!r}")

    # 后端侧:只有被 user_error() 标记为「可展示」的文案才受词表约束。
    py_files = sorted(BACKEND_APP.rglob("*.py"))
    if not py_files:
        print("check_ui_vocabulary: no backend/app sources found", file=sys.stderr)
        return 1
    # 第五条通道(展示字段)的扫描面按 ROOT 写死;默认情况下它与 BACKEND_APP 是同一棵
    # 树,就在同一次 AST 遍历里一起扫,不重复解析。BACKEND_APP 被重定向时(自检用例)
    # 再单独扫一遍真实仓库。
    shared_display = BACKEND_APP.resolve() == DISPLAY_FIELD_ROOT.resolve()
    marked_sites = display_sites = 0
    # 轨迹模块也在 backend/app 里:后端遍历时顺手留下它们的语法树,第三条通道不再重复解析。
    trace_trees: dict[Path, ast.Module] = {}
    display_violations: list[str] = []

    def record_display(path: Path, hits: Hits) -> None:
        rel = _rel(path)
        for line, term, snippet in hits:
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            display_violations.append(f"  {rel}:{line}: 「{term}」in display field: {snippet!r}")

    for path in py_files:
        rel = _rel(path)
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        if path in TRACE_SUMMARY_MODULES:
            trace_trees[path] = tree
        store_module = shared_display and (
            path.relative_to(BACKEND_APP) in _STORE_MODULES_RELATIVE
        )
        sites, hits, field_sites, field_hits = _scan_backend_tree(
            tree,
            # 文本预筛:不含任何登记名字的文件不可能有展示字段站点,省掉逐个调用的判定。
            display=shared_display and (
                store_module or any(token in text for token in _DISPLAY_FIELD_TOKENS)
            ),
            store_module=store_module,
        )
        marked_sites += sites
        display_sites += field_sites
        record_display(path, field_hits)
        for line, term, snippet in hits:
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            violations.append(f"  {rel}:{line}: 「{term}」in user_error message: {snippet!r}")

    # 仓库外插件自检面(裁决 3):只扩大扫描面,MIN_USER_ERROR_SITES 只对默认根计数,
    # 不传 --extra-root 时这整段不产生任何输出或副作用。
    extra_root_sites = 0
    for root in extra_roots:
        for path in sorted(root.rglob("*.py")):
            rel = _rel(path)
            sites, hits = scan_user_error(path)
            extra_root_sites += sites
            for line, term, snippet in hits:
                snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
                violations.append(
                    f"  {rel}:{line}: 「{term}」in user_error message (extra root): {snippet!r}"
                )
    if extra_roots:
        print(
            f"check_ui_vocabulary: extra-root scan covered {extra_root_sites} "
            f"{USER_ERROR}() call site(s) across {len(extra_roots)} root(s)"
        )

    if marked_sites < MIN_USER_ERROR_SITES:
        print(
            f"check_ui_vocabulary: only {marked_sites} {USER_ERROR}() call sites found in "
            f"backend/app (expected ≥ {MIN_USER_ERROR_SITES}). 这条检查失去了扫描面——"
            f"helper 是不是改名 / 换了调用写法?别调低下限来「修」它。",
            file=sys.stderr,
        )
        return 1

    # 第三条通道:服务端写、前端逐字渲染的推理轨迹摘要(见模块 docstring)。
    # 刻意排在 MIN_USER_ERROR_SITES **之后**:两条后端通道各有自己的「扫描面塌了」
    # 哨兵,而一次把 BACKEND_APP 指错的误配置会同时触发两条。先报 user_error 那条
    # 是因为它先于这条存在,原有那条自检用例的输出契约不该被这次扩面改写。
    trace_sites = 0
    for path in TRACE_SUMMARY_MODULES:
        if not path.exists():
            print(
                f"check_ui_vocabulary: trace-summary module missing: {_rel(path)} "
                f"(update TRACE_SUMMARY_MODULES)",
                file=sys.stderr,
            )
            return 1
        sites, hits = scan_trace_summaries(path, trace_trees.get(path))
        trace_sites += sites
        rel = _rel(path)
        for line, term, snippet in hits:
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            violations.append(
                f"  {rel}:{line}: 「{term}」in trace summary: {snippet!r}"
            )

    # 第五条通道:登记的后端展示字段(见 DISPLAY_FIELD_CALLS)。按 ROOT 写死,不跟
    # BACKEND_APP 走;同样排在前两条哨兵之后,理由同上。
    for path in DISPLAY_FIELD_ANCHOR_MODULES:
        if not path.exists():
            print(
                f"check_ui_vocabulary: display-field module missing: {_rel(path)} "
                f"(update DISPLAY_FIELD_ANCHOR_MODULES)",
                file=sys.stderr,
            )
            return 1
    if not shared_display:
        for path in sorted(DISPLAY_FIELD_ROOT.rglob("*.py")):
            sites, hits = scan_display_fields(
                path, store_module=path in KG_BUILD_JOB_STORE_MODULES
            )
            display_sites += sites
            record_display(path, hits)
    violations.extend(display_violations)

    # 第六条通道:模型工作负载显示名。按 ROOT 写死,理由同第五条。
    if not WORKLOAD_LABEL_MODULE.exists():
        print(
            f"check_ui_vocabulary: workload-label module missing: "
            f"{_rel(WORKLOAD_LABEL_MODULE)} (update WORKLOAD_LABEL_MODULE)",
            file=sys.stderr,
        )
        return 1
    label_sites, label_hits = scan_workload_labels(WORKLOAD_LABEL_MODULE)
    for line, term, snippet in label_hits:
        violations.append(
            f"  {_rel(WORKLOAD_LABEL_MODULE)}:{line}: 「{term}」in workload label: {snippet!r}"
        )
    service_sites = 0
    service_sources = [
        (path, lambda p, n=dict_name: scan_workload_labels(p, n))
        for path, dict_name in SERVICE_LABEL_DICTS
    ] + [(SERVICE_LABEL_TEMPLATE, scan_service_label_template)]
    for path, scanner in service_sources:
        if not path.exists():
            print(
                f"check_ui_vocabulary: service-label source missing: {_rel(path)} "
                f"(update SERVICE_LABEL_DICTS / SERVICE_LABEL_TEMPLATE)",
                file=sys.stderr,
            )
            return 1
        sites, hits = scanner(path)
        service_sites += sites
        for line, term, snippet in hits:
            violations.append(
                f"  {_rel(path)}:{line}: 「{term}」in model service label: {snippet!r}"
            )

    # 第七条通道:「收录自」合成来源的标题前缀。按 ROOT 写死,理由同第五条。
    if not PROMOTION_TITLE_MODULE.exists():
        print(
            f"check_ui_vocabulary: promotion-title module missing: "
            f"{_rel(PROMOTION_TITLE_MODULE)} (update PROMOTION_TITLE_MODULE)",
            file=sys.stderr,
        )
        return 1
    promotion_found, promotion_hits = _scan_promotion_title_names(PROMOTION_TITLE_MODULE)
    promotion_sites = len(promotion_found)
    for line, term, snippet in promotion_hits:
        violations.append(
            f"  {_rel(PROMOTION_TITLE_MODULE)}:{line}: 「{term}」in promotion source "
            f"title prefix: {snippet!r}"
        )

    # 第八条通道:「就此追问」不读报告的原因文案。按 ROOT 写死,理由同第五条。
    if not REPORT_REASON_MODULE.exists():
        print(
            f"check_ui_vocabulary: report-reason module missing: "
            f"{_rel(REPORT_REASON_MODULE)} (update REPORT_REASON_MODULE)",
            file=sys.stderr,
        )
        return 1
    reason_found, reason_hits, reason_texts = _scan_named_texts(
        REPORT_REASON_MODULE, REPORT_REASON_NAMES
    )
    for line, term, snippet in reason_hits:
        snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
        violations.append(
            f"  {_rel(REPORT_REASON_MODULE)}:{line}: 「{term}」in report reason text: "
            f"{snippet!r}"
        )

    note_files = _release_note_files()
    for path in note_files:
        rel = _rel(path)
        for line, term, snippet in scan_release_note(path):
            snippet = snippet if len(snippet) <= 80 else snippet[:77] + "…"
            violations.append(f"  {rel}:{line}: 「{term}」in release note: {snippet!r}")

    if trace_sites < MIN_TRACE_SUMMARY_SITES:
        print(
            f"check_ui_vocabulary: only {trace_sites} resolvable trace summaries found "
            f"across {len(TRACE_SUMMARY_MODULES)} module(s) (expected ≥ "
            f"{MIN_TRACE_SUMMARY_SITES}). 这条通道失去了扫描面——{TRACE_STEP} 是不是"
            f"改名 / 摘要改成了静态解析不了的写法(跨模块常量、str(exc)…)?"
            f"别调低下限来「修」它。",
            file=sys.stderr,
        )
        return 1

    if display_sites < MIN_DISPLAY_FIELD_SITES:
        print(
            f"check_ui_vocabulary: only {display_sites} resolvable display fields found "
            f"in backend/app (expected ≥ {MIN_DISPLAY_FIELD_SITES}). 这条通道失去了扫描面"
            f"——登记的调用形状是不是改名 / 换了写法?同步 DISPLAY_FIELD_CALLS / "
            f"DISPLAY_FIELD_METHODS,别调低下限来「修」它。",
            file=sys.stderr,
        )
        return 1

    if label_sites < MIN_WORKLOAD_LABELS:
        print(
            f"check_ui_vocabulary: only {label_sites} workload labels found in "
            f"{_rel(WORKLOAD_LABEL_MODULE)} (expected ≥ {MIN_WORKLOAD_LABELS}). "
            f"{WORKLOAD_LABEL_NAME} 是不是改名 / 换了写法?同步 WORKLOAD_LABEL_NAME,"
            f"别调低下限来「修」它。",
            file=sys.stderr,
        )
        return 1

    if promotion_found != set(PROMOTION_TITLE_NAMES):
        missing = sorted(set(PROMOTION_TITLE_NAMES) - promotion_found)
        print(
            f"check_ui_vocabulary: only {promotion_sites} promotion title prefixes found "
            f"in {_rel(PROMOTION_TITLE_MODULE)} (expected {len(PROMOTION_TITLE_NAMES)}; "
            f"missing {', '.join(missing)}). "
            f"{' / '.join(PROMOTION_TITLE_NAMES)} 是不是改名 / 换了写法?同步 "
            f"PROMOTION_TITLE_NAMES,别删这条通道来「修」它。",
            file=sys.stderr,
        )
        return 1

    if reason_found != set(REPORT_REASON_NAMES):
        missing = sorted(set(REPORT_REASON_NAMES) - reason_found)
        print(
            f"check_ui_vocabulary: only {len(reason_found)} report reason constants found "
            f"in {_rel(REPORT_REASON_MODULE)} (expected {len(REPORT_REASON_NAMES)}; "
            f"missing {', '.join(missing)}). 原因文案常量是不是改名 / 换了写法?同步 "
            f"REPORT_REASON_NAMES,别删这条通道来「修」它。",
            file=sys.stderr,
        )
        return 1

    if service_sites < MIN_SERVICE_LABELS:
        print(
            f"check_ui_vocabulary: only {service_sites} shipped model-service labels "
            f"found (expected ≥ {MIN_SERVICE_LABELS}). ROLE_LABELS / 模板的 display_name "
            f"是不是改名 / 换了写法?同步 SERVICE_LABEL_DICTS / SERVICE_LABEL_TEMPLATE,"
            f"别调低下限来「修」它。",
            file=sys.stderr,
        )
        return 1

    if violations:
        print("UI vocabulary contract MISMATCH — internal jargon in user-visible copy:", file=sys.stderr)
        print("\n".join(violations), file=sys.stderr)
        print(
            "\nRewrite the copy per docs/ui-vocabulary.md「界面词汇表」(e.g. 基准库→公共知识库, "
            "chunk→段, KG/CSR/ANN→索引, 抽取→分析). Internal names stay in code, not UI.",
            file=sys.stderr,
        )
        return 1
    ok_message = (
        f"UI vocabulary contract OK: scanned {len(files)} frontend production files + "
        f"{marked_sites} backend {USER_ERROR}() messages + "
        f"{trace_sites} trace summaries + "
        f"{display_sites} display fields + "
        f"{label_sites} workload labels + "
        f"{service_sites} model service labels + "
        f"{promotion_sites} promotion title prefixes + "
        f"{reason_texts} report reason texts "
        f"({len(CJK_TERMS) + len(ASCII_TERMS)} blacklisted terms)"
    )
    if extra_roots:
        ok_message += f" + {extra_root_sites} extra-root {USER_ERROR}() messages"
    print(ok_message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
