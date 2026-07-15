# 论文元数据抽取与按作者搜索 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 论文摄取时抽取元数据（作者/机构/标题/venue/年份/DOI/关键词）经接地校验后入库，来源搜索按作者/论文标题命中，历史源三通道补抽。

**Architecture:** 两张侧表（`source_paper_meta` 1:1 + `source_authors` 1:N）；一次小 LLM 调用对文档头部 ~4000 字符抽取，零 LLM 接地校验（归一化子串包含）挡记忆幻觉；挂载 `process_source`（force）与 `run_extraction`（catch-up），批量补抽走 CLI phase 与应用内端点。不进 KG、不改 `sources.title`。

**Tech Stack:** FastAPI + SQLite（Python 3.13）、pydantic v2、Next.js（page.tsx 单体）、pytest。

**Spec:** `docs/superpowers/specs/2026-07-15-paper-metadata-extraction-design.md`

## Global Constraints

- 仓库根：`/Users/hzf/workspace/silicon_notebook/.claude/worktrees/angry-albattani-379851`（worktree，分支 `claude/paper-metadata-extraction-38ea3b`）。测试一律 `cd backend && python -m pytest tests/<file> -q`；若 `python` 缺依赖改用 conda base 解释器（`which python3` / miniconda base）。
- **行号漂移纪律**：`test_repository_surface_manifest.py` 按 file:line 精确比对 consumers。新 facade 方法追加在 `SQLiteRepository` 类**尾部**（`backend/app/services/sqlite_repository.py` 约 3274 行、模块级 `def _now()` 之前）；`source_store.py`/`routes.py`/`source_ingestion.py` 的新方法/端点一律**追加文件（或类）末尾**；对既有测试文件的新增测试**追加 EOF**。manifest 消费点引用（`batch_ingest.py:277/287/358/360/789/808/823` 等）都在本计划的插入点之前，不受影响。
- 每个 LLM 相关新增调用点：仅本特性设计的一次 `chat_json`（temperature=0.0，`cap_kwargs(client, "openai_compat_max_tokens")`），不加任何其他 LLM/embed 调用。
- 接地不变量：LLM 返回字段不在头部文本中（归一化匹配）则不落库；`raw_json` 存 `{"llm":…, "dropped":…}` 审计信封。
- 前端 API 路径**不带 `/api` 前缀**（`API_BASE` 已含）；中文文案弯引号沿用现状，禁止批量替换引号；UI 对齐精致（复用现有 class，不新造粗糙样式）。
- 不启停用户服务；不自动回填存量（仅显式触发）。
- 每个 Task 结束：该 Task 的测试 + 声明的回归测试全绿后 `git commit`。
- pydantic-settings v2：新配置字段名即环境变量名（大小写不敏感），不用 `Field(env=)`。

---

### Task 1: 迁移 `_migration_17` + SCHEMA_VERSION 17 + goldens

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py`（`:14` 常量；`_migration_16` 之后追加 `_migration_17`，即 `:1302` 附近）
- Modify: `backend/app/services/sqlite_repository.py:211`（镜像常量 16→17）
- Modify: `backend/tests/test_sqlite_migrator_component.py:21`（断言 16→17；若测试名含 `v16` 同步改 `v17`）
- Modify: `backend/tests/test_memory_kg_schema.py:56`（16→17；测试名 `test_schema_version_is_16`→`_17`）
- Modify: `backend/tests/test_legacy_db_compat.py:59`（16→17；测试名 `test_v16_schema_version_is_current`→`v17`）
- Modify: `backend/tests/test_repository_v9_fixture.py:97` + `backend/tests/fixtures/repository_contract/repository_v9/`（先 `git show 2357066 --stat` 和 `git show 2357066 -- backend/tests/fixtures/repository_contract/repository_v9/` 看 v16 bump 时这些文件怎么改的，照做）
- Regen: `backend/tests/fixtures/schema_contract.txt`
- Test: `backend/tests/test_paper_meta_schema.py`（新文件）

**Interfaces:**
- Produces: 表 `source_paper_meta(source_id PK, notebook_id, is_paper, paper_title, venue, pub_year, doi, keywords, raw_json, model, created_at, updated_at)`；表 `source_authors(id PK, source_id, notebook_id, position, name, affiliation, created_at)`；索引 `idx_source_paper_meta_nb` / `idx_source_authors_source` / `idx_source_authors_nb`。后续 Task 依赖这两表存在。

- [ ] **Step 1: 写失败测试** — 新建 `backend/tests/test_paper_meta_schema.py`。先读 `backend/tests/test_knowhow_schema.py` 全文，**复用其构建全新库与「已部署库升级」的 setup 帮助函数/写法**（该文件是上一次加表 PR 的同款测试），断言换成：

```python
"""source_paper_meta / source_authors 迁移测试(paper-metadata Task 1)。

镜像 test_knowhow_schema.py 的两层覆盖:全新库经 _migration_1..17 建齐;
已部署库(user_version=16)经版本闸补建 —— 防「新表塞进已封版迁移导致
已部署库漏建」(schema-migration-convention)。
"""

def _columns(db, table):
    return [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]

# 断言 1(全新库): 两表存在,列序如下
EXPECTED_PAPER_META_COLS = [
    "source_id", "notebook_id", "is_paper", "paper_title", "venue", "pub_year",
    "doi", "keywords", "raw_json", "model", "created_at", "updated_at",
]
EXPECTED_AUTHOR_COLS = [
    "id", "source_id", "notebook_id", "position", "name", "affiliation", "created_at",
]
# 断言 2(升级库): 先把库停在 user_version=16(镜像 test_knowhow_schema.py:199 的
# `PRAGMA user_version = 15` 写法),跑 migrate(),断言 17 in applied 且两表已建。
# 断言 3: 索引 idx_source_paper_meta_nb / idx_source_authors_source /
#         idx_source_authors_nb 存在(查 sqlite_master WHERE type='index')。
# 断言 4: 级联 —— 插入 notebook/source/meta/author 行后 DELETE sources 行,
#         两表对应行消失(PRAGMA foreign_keys 按 test_knowhow_schema.py 同款处理)。
```

- [ ] **Step 2: 跑测试确认失败** — `cd backend && python -m pytest tests/test_paper_meta_schema.py -q`，预期 FAIL（`no such table: source_paper_meta`）。
- [ ] **Step 3: 写迁移** — `migrations.py:14` 改 `SCHEMA_VERSION = 17`；在 `_migration_16` 方法结束后（`_recover_interrupted_jobs` 之前）追加：

```python
    def _migration_17(self) -> None:
        """论文元数据两表(paper-metadata Task 1):source_paper_meta(1:1;行存在=
        已尝试,is_paper=0 为「已判定非论文」标记行,防对同一源反复调 LLM)+
        source_authors(1:N;position=署名序,affiliation 多机构以 '; ' 连接)。
        接地校验后的数据才落库(app/services/paper_meta.py)。

        已部署库(user_version>=1 时 _migration_1 短路)靠本迁移补建——与
        _migration_16 同款两层写法(仅新建表,不改 _migration_1 baseline;
        全新表无历史行,无列序顾虑)。
        """
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_paper_meta (
                  source_id TEXT PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  is_paper INTEGER NOT NULL DEFAULT 0,
                  paper_title TEXT,
                  venue TEXT,
                  pub_year INTEGER,
                  doi TEXT,
                  keywords TEXT NOT NULL DEFAULT '[]',
                  raw_json TEXT NOT NULL DEFAULT '{}',
                  model TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_source_paper_meta_nb
                  ON source_paper_meta(notebook_id);

                CREATE TABLE IF NOT EXISTS source_authors (
                  id TEXT PRIMARY KEY,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  position INTEGER NOT NULL,
                  name TEXT NOT NULL,
                  affiliation TEXT NOT NULL DEFAULT '',
                  created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_source_authors_source
                  ON source_authors(source_id);
                CREATE INDEX IF NOT EXISTS idx_source_authors_nb
                  ON source_authors(notebook_id);
                """
            )
```

同时 `sqlite_repository.py:211` 镜像常量改 17。
- [ ] **Step 4: 跑新测试至绿** — `python -m pytest tests/test_paper_meta_schema.py -q`，预期 PASS。
- [ ] **Step 5: 修 bump 波及 + regen goldens** —
  1. 上面列出的四个硬编码 16 测试逐个改 17（值 + 测试名）。
  2. `UPDATE_SCHEMA_GOLDEN=1 python -m pytest tests/test_legacy_db_compat.py -k contract -q` 重生成 `schema_contract.txt`，然后 `git diff backend/tests/fixtures/schema_contract.txt` 确认**只新增**两表列与三索引行、零删除。
  3. repository_v9 fixture 按 `git show 2357066` 的先例更新（通常 `manifest.json` 里 schema 版本 + `expected_snapshot.json` 的 `user_version` 与新增表快照；若有校验脚本 `scripts/verify_repository_snapshot.py`，用它验证）。
- [ ] **Step 6: 回归** — `python -m pytest tests/test_legacy_db_compat.py tests/test_sqlite_migrator_component.py tests/test_memory_kg_schema.py tests/test_knowhow_schema.py tests/test_repository_v9_fixture.py tests/test_repository_snapshot_verifier.py tests/test_repository_facade_contract.py -q`，预期全 PASS。
- [ ] **Step 7: Commit** — `git add -A && git commit -m "feat(schema): source_paper_meta + source_authors tables (_migration_17)"`

---

### Task 2: 接地校验模块 `paper_meta.py`（纯函数 + prompt）

**Files:**
- Create: `backend/app/services/paper_meta.py`
- Test: `backend/tests/test_paper_meta_grounding.py`（新文件）

**Interfaces:**
- Produces（Task 4/5 依赖，签名照抄）:
  - `PAPER_META_SCHEMA_HINT: str`
  - `paper_meta_prompt(head_text: str) -> str`
  - `verify_paper_meta(data: dict, head_text: str, model: str) -> dict` — 返回可直接交给 `SourceStore.upsert_paper_meta` 的 dict，键：`is_paper, paper_title, venue, pub_year, doi, keywords, authors, model, raw_json, dropped`（`authors` 为 `[{"position": int, "name": str, "affiliation": str}]`；`dropped` 仅供调用方记事件，store 不消费）。

- [ ] **Step 1: 写失败测试** — 新建 `backend/tests/test_paper_meta_grounding.py`：

```python
"""接地校验(anti-hallucination)单测:不在头部文本中的字段不落库。"""
import json

from app.services.paper_meta import paper_meta_prompt, verify_paper_meta

HEAD = (
    "Attention Is All You Need\n"
    "Ashish Vaswani, Noam Shazeer, Niki Parmar\n"
    "Google Brain; Google Research\n"
    "Published at NIPS 2017. doi:10.5555/3295222\n"
    "Keywords: transformer, attention\n"
    "Abstract: The dominant sequence transduction models ..."
)


def _base(**over):
    data = {
        "is_paper": True,
        "title": "Attention Is All You Need",
        "authors": [
            {"name": "Ashish Vaswani", "affiliations": ["Google Brain"]},
            {"name": "Noam Shazeer", "affiliations": ["Google Research"]},
        ],
        "venue": "NIPS", "year": 2017, "doi": "10.5555/3295222",
        "keywords": ["transformer", "attention"],
    }
    data.update(over)
    return data


def test_grounded_fields_survive():
    meta = verify_paper_meta(_base(), HEAD, model="m1")
    assert meta["is_paper"] is True
    assert meta["paper_title"] == "Attention Is All You Need"
    assert [a["name"] for a in meta["authors"]] == ["Ashish Vaswani", "Noam Shazeer"]
    assert meta["authors"][0]["position"] == 0
    assert meta["authors"][0]["affiliation"] == "Google Brain"
    assert meta["venue"] == "NIPS" and meta["pub_year"] == 2017
    assert meta["doi"] == "10.5555/3295222"
    assert meta["keywords"] == ["transformer", "attention"]
    assert meta["model"] == "m1"
    assert json.loads(meta["raw_json"])["dropped"] == {}


def test_hallucinated_author_dropped_and_audited():
    meta = verify_paper_meta(
        _base(authors=_base()["authors"] + [{"name": "Geoffrey Hinton", "affiliations": []}]),
        HEAD, model="m")
    assert "Geoffrey Hinton" not in [a["name"] for a in meta["authors"]]
    assert json.loads(meta["raw_json"])["dropped"]["authors"] == ["Geoffrey Hinton"]
    assert meta["dropped"]["authors"] == ["Geoffrey Hinton"]


def test_unverifiable_affiliation_cleared_author_kept():
    meta = verify_paper_meta(
        _base(authors=[{"name": "Ashish Vaswani", "affiliations": ["MIT CSAIL"]}]),
        HEAD, model="m")
    assert meta["authors"][0]["name"] == "Ashish Vaswani"
    assert meta["authors"][0]["affiliation"] == ""
    assert "MIT CSAIL" in meta["dropped"]["affiliations"]


def test_name_normalization_tolerates_case_space_diacritics_and_order():
    head = "José García-López and Wei Zhang, 2023, ACM"
    meta = verify_paper_meta(
        {"is_paper": True, "title": "", "venue": "ACM", "year": 2023,
         "authors": [{"name": "Jose Garcia Lopez", "affiliations": []},
                     {"name": "Zhang, Wei", "affiliations": []}],
         "doi": "", "keywords": []},
        head, model="m")
    assert [a["name"] for a in meta["authors"]] == ["Jose Garcia Lopez", "Zhang, Wei"]


def test_venue_year_not_in_text_nulled():
    meta = verify_paper_meta(_base(venue="ICML", year=2021), HEAD, model="m")
    assert meta["venue"] is None and meta["pub_year"] is None
    assert meta["dropped"]["venue"] == "ICML" and meta["dropped"]["year"] == 2021


def test_doi_must_match_format_and_text():
    assert verify_paper_meta(_base(doi="not-a-doi"), HEAD, "m")["doi"] is None
    assert verify_paper_meta(_base(doi="10.1234/absent"), HEAD, "m")["doi"] is None


def test_year_range_guard():
    meta = verify_paper_meta(_base(year=222), HEAD, model="m")
    assert meta["pub_year"] is None


def test_not_paper_blanks_everything():
    meta = verify_paper_meta(_base(is_paper=False), HEAD, model="m")
    assert meta["is_paper"] is False
    assert meta["authors"] == [] and meta["paper_title"] is None
    assert meta["keywords"] == [] and meta["doi"] is None


def test_prompt_forbids_memory_fill():
    p = paper_meta_prompt("some text")
    assert "do NOT" in p and "memory" in p and "some text" in p
```

- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_paper_meta_grounding.py -q`，预期 FAIL（`ModuleNotFoundError: app.services.paper_meta`）。
- [ ] **Step 3: 实现模块** — 新建 `backend/app/services/paper_meta.py`：

```python
"""论文元数据抽取:prompt/schema hint + 零 LLM 接地校验(anti-hallucination)。

设计不变量(specs/2026-07-15-paper-metadata-extraction-design.md §5.3):LLM 返回
的每个字段写库前必须「接地」——归一化后能在文档头部文本中找到——防模型对
「认识的」论文用参数记忆补全(张冠李戴作者/机构)。不在文本中的字段不落库,
丢弃明细进 raw_json 审计信封。纯函数,无 DB/网络依赖。
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Dict, List, Optional

PAPER_META_SCHEMA_HINT = (
    '{"is_paper":true,"title":"","authors":[{"name":"","affiliations":[""]}],'
    '"venue":"","year":2024,"doi":"","keywords":[""]}'
)

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")


def paper_meta_prompt(head_text: str) -> str:
    return (
        "Extract bibliographic metadata from the OPENING TEXT of a document.\n"
        "Rules:\n"
        "- Use ONLY the text below. Even if you recognize the paper, do NOT "
        "fill in anything from memory — omit whatever the text does not show.\n"
        "- If this is not an academic paper (web page, manual, slides, notes, "
        '...), return {"is_paper": false} and leave every other field empty.\n'
        "- authors: in byline order, names EXACTLY as written in the text "
        "(original language/spelling). affiliations: that author's "
        "institutions per the superscript/layout mapping; use [] when unsure "
        "— never guess.\n"
        "- venue: journal/conference name only if it appears in the text; "
        "year: publication year only if it appears in the text; doi: only if "
        "a DOI string appears; keywords: only from an explicit keyword list.\n"
        "- Return JSON only.\n\n"
        f"Opening text:\n{head_text}"
    )


def _norm(text: str) -> str:
    """接地匹配归一化:NFKD 去变音符、casefold、只留字母数字(空白/标点不敏感)。"""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.casefold() if ch.isalnum())


def grounded(value: str, head_norm: str) -> bool:
    needle = _norm(value)
    return bool(needle) and needle in head_norm


def author_grounded(name: str, head_norm: str) -> bool:
    """姓名接地:直接匹配,或 2+ token 时容忍「姓,名」/「名 姓」次序翻转。"""
    if grounded(name, head_norm):
        return True
    tokens = [t for t in re.split(r"[\s,]+", name or "") if t]
    if len(tokens) >= 2:
        return grounded("".join(reversed(tokens)), head_norm)
    return False


def verify_paper_meta(data: Dict[str, Any], head_text: str, model: str) -> Dict[str, Any]:
    """接地校验:返回可直接交给 SourceStore.upsert_paper_meta 的 meta dict。
    不在头部文本中的字段不落库;丢弃明细记入 raw_json 审计信封
    {"llm": 原始返回, "dropped": {...}} 并以 "dropped" 键回传给调用方记事件。"""
    head_text = head_text or ""
    head_norm = _norm(head_text)
    dropped: Dict[str, Any] = {}
    is_paper = bool(data.get("is_paper"))

    title = str(data.get("title") or "").strip() or None
    if title and not grounded(title, head_norm):
        dropped["title"] = title
        title = None

    venue = str(data.get("venue") or "").strip() or None
    if venue and not grounded(venue, head_norm):
        dropped["venue"] = venue
        venue = None

    year: Optional[int] = None
    raw_year = data.get("year")
    if raw_year is not None and str(raw_year).strip():
        try:
            candidate = int(str(raw_year).strip())
        except ValueError:
            candidate = 0
        if 1900 <= candidate <= 2100 and str(candidate) in head_text:
            year = candidate
        else:
            dropped["year"] = raw_year

    doi = str(data.get("doi") or "").strip() or None
    if doi and not (_DOI_RE.match(doi) and doi.lower() in head_text.lower()):
        dropped["doi"] = doi
        doi = None

    keywords: List[str] = []
    dropped_keywords: List[str] = []
    for raw_kw in data.get("keywords") or []:
        keyword = str(raw_kw).strip()
        if not keyword:
            continue
        (keywords if grounded(keyword, head_norm) else dropped_keywords).append(keyword)
    if dropped_keywords:
        dropped["keywords"] = dropped_keywords

    authors: List[Dict[str, Any]] = []
    dropped_authors: List[str] = []
    cleared_affiliations: List[str] = []
    position = 0
    for raw_author in data.get("authors") or []:
        name = str((raw_author or {}).get("name") or "").strip()
        if not name:
            continue
        if not author_grounded(name, head_norm):
            dropped_authors.append(name)
            continue
        affiliations = [
            str(a).strip()
            for a in (raw_author or {}).get("affiliations") or []
            if str(a).strip()
        ]
        kept = [a for a in affiliations if grounded(a, head_norm)]
        cleared_affiliations.extend(a for a in affiliations if a not in kept)
        authors.append(
            {"position": position, "name": name, "affiliation": "; ".join(kept)}
        )
        position += 1
    if dropped_authors:
        dropped["authors"] = dropped_authors
    if cleared_affiliations:
        dropped["affiliations"] = cleared_affiliations

    return {
        "is_paper": is_paper,
        "paper_title": title if is_paper else None,
        "venue": venue if is_paper else None,
        "pub_year": year if is_paper else None,
        "doi": doi if is_paper else None,
        "keywords": keywords if is_paper else [],
        "authors": authors if is_paper else [],
        "model": model,
        "raw_json": json.dumps({"llm": data, "dropped": dropped}, ensure_ascii=False),
        "dropped": dropped,
    }
```

- [ ] **Step 4: 跑测试至绿** — `python -m pytest tests/test_paper_meta_grounding.py -q`，预期 PASS（9 个测试）。
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(services): paper metadata prompt + grounding verification (zero-LLM)"`

---

### Task 3: SourceStore 持久化 + pydantic 模型 + 水合 + 按作者搜索 + api_contract regen

**Files:**
- Modify: `backend/app/models/schemas.py`（`SourceElement` 类前插 `PaperAuthor`/`PaperMeta`；`SourceSummary` 类尾加 3 字段（`:275` 附近）；`SourceDetail`（`:310`）加 `paper_meta`）
- Modify: `backend/app/repositories/sqlite/source_store.py`（EOF 追加 5 个方法；`list_sources_page:83-86` q 扩展；`source_from_row`/`sources_from_rows`/`get_source` 水合）
- Regen: `backend/tests/fixtures/repository_contract/api_contract.json`
- Test: `backend/tests/test_paper_meta_store.py`（新文件）

**Interfaces:**
- Consumes: Task 1 的两张表。
- Produces（Task 4/5 依赖）:
  - `SourceStore.upsert_paper_meta(source_id: str, notebook_id: str, meta: dict) -> None`
  - `SourceStore.get_paper_meta(source_id: str) -> Optional[dict]`（含 `authors` 按 position 升序）
  - `SourceStore.paper_meta_for_sources(db, source_ids) -> Dict[str, dict]`
  - `SourceStore.sources_missing_paper_meta(notebook_id: str, include_existing: bool = False) -> List[str]`
  - pydantic `PaperAuthor{name, affiliation}`、`PaperMeta{is_paper, title, venue, year, doi, keywords, authors}`；`SourceSummary.authors: List[str]`/`pub_year`/`venue`；`SourceDetail.paper_meta: Optional[PaperMeta]`。

- [ ] **Step 1: 写失败测试** — 新建 `backend/tests/test_paper_meta_store.py`。先读 `backend/tests/test_knowhow_store.py` 开头，**复用其 store/repo 构建 fixture 写法**；测试体：

```python
"""SourceStore 论文元数据持久化/水合/搜索测试(paper-metadata Task 3)。"""

META = {
    "is_paper": True, "paper_title": "FinFET Scaling Study",
    "venue": "IEDM", "pub_year": 2024, "doi": "10.1109/x.2024",
    "keywords": ["finfet", "scaling"], "model": "m1",
    "raw_json": '{"llm":{},"dropped":{}}',
    "authors": [
        {"position": 0, "name": "Alice Wu", "affiliation": "NTU"},
        {"position": 1, "name": "Bob Li", "affiliation": "TSMC; NTU"},
    ],
}

# test_upsert_then_get_roundtrip:
#   upsert 后 get_paper_meta 返回全字段 + authors 按 position 升序;
#   再次 upsert(改 paper_title、去掉一个作者)后 get 反映覆盖(作者整组替换,无残留)。
# test_get_missing_returns_none: 未写过的 source get_paper_meta() is None。
# test_marker_row_not_paper: upsert is_paper=False 空字段 → get 返回 is_paper False、
#   authors==[](行存在,幂等标记语义)。
# test_batched_hydration: 3 个源两个有 meta,paper_meta_for_sources 返回两键;
#   monkeypatch SourceStore.IN_CHUNK=1 再跑一次结果一致(IN 分批覆盖)。
# test_sources_missing_paper_meta: 建 4 源 —— A(paper,无 meta,parsed)命中;
#   B(有 meta)不命中但 include_existing=True 时命中; C(doc_type='textbook')不命中;
#   D(source_type='memory')不命中。
# test_list_page_q_matches_author_and_paper_title:
#   list_sources_page(nb, q="alice wu") 命中 A;q="finfet scaling study" 命中 A;
#   q="不存在的名字" total_count==0;返回的 SourceSummary.authors ==
#   ["Alice Wu","Bob Li"]、pub_year==2024、venue=="IEDM"。
# test_get_source_detail_carries_paper_meta:
#   get_source(A).paper_meta.title=="FinFET Scaling Study" 且
#   .authors[0].affiliation=="NTU";无 meta 的源 paper_meta is None。
# test_source_delete_cascades: 删 sources 行后 source_authors/source_paper_meta 空。
```

每条注释各展开为一个真实测试函数（fixture 建 notebook + `insert_source`，写法照 `test_knowhow_store.py`/`test_sources_page_batched.py` 现有惯例）。
- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_paper_meta_store.py -q`，预期 FAIL（`AttributeError: upsert_paper_meta`）。
- [ ] **Step 3: schemas.py** — 在 `SourceElement`（`schemas.py:247`）类定义**之前**插入：

```python
class PaperAuthor(BaseModel):
    name: str
    affiliation: str = ""  # 多机构以 "; " 连接;接地校验不过则为空


class PaperMeta(BaseModel):
    """论文元数据(接地校验后)。非论文源/未抽取时整个对象缺省。"""
    is_paper: bool = False
    title: Optional[str] = None
    venue: Optional[str] = None
    year: Optional[int] = None
    doi: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)
    authors: List[PaperAuthor] = Field(default_factory=list)
```

`SourceSummary` 类末尾（`kg_extracted` 字段之后）追加：

```python
    # 论文元数据投影:作者姓名按署名序;非论文/未抽取为空(paper-metadata)。
    authors: List[str] = Field(default_factory=list)
    pub_year: Optional[int] = None
    venue: Optional[str] = None
```

`SourceDetail` 追加字段：

```python
class SourceDetail(SourceSummary):
    file_path: str = ""
    error_message: str = ""
    paper_meta: Optional[PaperMeta] = None
```

- [ ] **Step 4: SourceStore 方法（EOF 追加）+ 水合 + 搜索** — `source_store.py` 文件末尾追加（import 区补 `PaperAuthor, PaperMeta` 到既有 `from app.models.schemas import` 列表）：

```python
    # ------------------------------------------------------- paper metadata
    def upsert_paper_meta(self, source_id: str, notebook_id: str, meta: dict) -> None:
        """写入/覆盖论文元数据(单写事务):source_paper_meta upsert + source_authors
        整组 delete+insert。meta 形状 = paper_meta.verify_paper_meta 的返回(已接地
        校验);行存在即「已尝试」,is_paper=0 是「已判定非论文」标记(幂等防重调 LLM)。
        作者行 id 取 source_id 限定的确定性复合键(重抽稳定,无碰撞面)。"""
        now = self.now()
        with self.database.write() as db:
            db.execute(
                """
                INSERT INTO source_paper_meta
                  (source_id, notebook_id, is_paper, paper_title, venue, pub_year,
                   doi, keywords, raw_json, model, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                  is_paper=excluded.is_paper, paper_title=excluded.paper_title,
                  venue=excluded.venue, pub_year=excluded.pub_year, doi=excluded.doi,
                  keywords=excluded.keywords, raw_json=excluded.raw_json,
                  model=excluded.model, updated_at=excluded.updated_at
                """,
                (
                    source_id, notebook_id, 1 if meta.get("is_paper") else 0,
                    meta.get("paper_title"), meta.get("venue"), meta.get("pub_year"),
                    meta.get("doi"),
                    json.dumps(meta.get("keywords") or [], ensure_ascii=False),
                    str(meta.get("raw_json") or "{}"), str(meta.get("model") or ""),
                    now, now,
                ),
            )
            db.execute("DELETE FROM source_authors WHERE source_id = ?", (source_id,))
            for author in meta.get("authors") or []:
                position = int(author.get("position", 0))
                db.execute(
                    "INSERT INTO source_authors "
                    "(id, source_id, notebook_id, position, name, affiliation, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"{source_id}:auth:{position:03d}", source_id, notebook_id,
                        position, str(author.get("name") or "").strip(),
                        str(author.get("affiliation") or "").strip(), now,
                    ),
                )

    @staticmethod
    def _paper_meta_dict(row: sqlite3.Row, authors: List[sqlite3.Row]) -> dict:
        return {
            "source_id": row["source_id"],
            "is_paper": bool(row["is_paper"]),
            "paper_title": row["paper_title"],
            "venue": row["venue"],
            "pub_year": row["pub_year"],
            "doi": row["doi"],
            "keywords": json.loads(row["keywords"] or "[]"),
            "model": row["model"],
            "authors": [
                {"position": a["position"], "name": a["name"],
                 "affiliation": a["affiliation"]}
                for a in authors
            ],
        }

    @staticmethod
    def paper_meta_model(meta: Optional[dict]) -> Optional[PaperMeta]:
        """store dict → API 模型(SourceDetail.paper_meta)。标记行(is_paper=0)也
        返回对象(is_paper False),前端按 is_paper 门控显示。"""
        if meta is None:
            return None
        return PaperMeta(
            is_paper=meta["is_paper"], title=meta["paper_title"],
            venue=meta["venue"], year=meta["pub_year"], doi=meta["doi"],
            keywords=list(meta["keywords"]),
            authors=[
                PaperAuthor(name=a["name"], affiliation=a["affiliation"])
                for a in meta["authors"]
            ],
        )

    def get_paper_meta(self, source_id: str) -> Optional[dict]:
        with self.database.connect() as db:
            return self.paper_meta_for_sources(db, [source_id]).get(source_id)

    def paper_meta_for_sources(self, db: sqlite3.Connection,
                               source_ids: Sequence[str]) -> Dict[str, dict]:
        """批量水合(IN 分批守 999 变量上限,同 sources_from_rows 惯例)。
        无 meta 行的源不在返回里。"""
        meta_rows: Dict[str, sqlite3.Row] = {}
        author_rows: Dict[str, List[sqlite3.Row]] = {}
        ids = list(source_ids)
        for i in range(0, len(ids), self.IN_CHUNK):
            batch = ids[i:i + self.IN_CHUNK]
            ph = ",".join("?" for _ in batch)
            for row in db.execute(
                f"SELECT * FROM source_paper_meta WHERE source_id IN ({ph})", batch,
            ).fetchall():
                meta_rows[row["source_id"]] = row
            for a in db.execute(
                f"SELECT source_id, position, name, affiliation FROM source_authors "
                f"WHERE source_id IN ({ph}) ORDER BY source_id, position ASC", batch,
            ).fetchall():
                author_rows.setdefault(a["source_id"], []).append(a)
        return {
            sid: self._paper_meta_dict(row, author_rows.get(sid, []))
            for sid, row in meta_rows.items()
        }

    def sources_missing_paper_meta(self, notebook_id: str,
                                   include_existing: bool = False) -> List[str]:
        """补抽目标源:doc_type 为 academic_paper(含 ''=默认,与 run_extraction 的
        `or "academic_paper"` 语义一致)、已有解析产物(parsed 及之后)、非 memory/
        knowhow 合成源;默认排除已有 meta 行(幂等续跑),include_existing=True
        (--force)全量。"""
        missing = (
            "" if include_existing else
            " AND NOT EXISTS (SELECT 1 FROM source_paper_meta m WHERE m.source_id = s.id)"
        )
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT s.id FROM sources s "
                "WHERE s.notebook_id = ? "
                "  AND s.source_type NOT IN ('memory', 'knowhow') "
                "  AND s.doc_type IN ('', 'academic_paper') "
                "  AND s.parse_status IN ('parsed', 'extracting', 'extracted') "
                f"{missing} ORDER BY s.created_at ASC",
                (notebook_id,),
            ).fetchall()
        return [r["id"] for r in rows]
```

水合接线（三处小改）：
1. `sources_from_rows`（`:502`）：`source_ids = [r["id"] for r in rows]` 行后加 `paper_meta = self.paper_meta_for_sources(db, source_ids)`；构造 `SourceSummary(...)` 的 `kg_extracted=` 行后追加：

```python
                authors=[a["name"] for a in paper_meta[sid]["authors"]]
                    if sid in paper_meta else [],
                pub_year=paper_meta[sid]["pub_year"] if sid in paper_meta else None,
                venue=paper_meta[sid]["venue"] if sid in paper_meta else None,
```

2. `source_from_row`（`:471`）：同样在 `kg_extracted=` 后追加（用 `pm = self.paper_meta_for_sources(db, [row["id"]]).get(row["id"])` 先取）。
3. `get_source`（`:97`）：`SourceDetail(**summary.model_dump(), file_path=..., error_message=...)` 增 `paper_meta=self.paper_meta_model(self.paper_meta_for_sources(db, [source_id]).get(source_id))`。

搜索扩展 — `list_sources_page`（`:83-86`）的 `if needle:` 块整体替换为：

```python
        if needle:
            where += (
                " AND (LOWER(title) LIKE ? OR LOWER(file_name) LIKE ?"
                " OR EXISTS(SELECT 1 FROM source_authors a"
                "    WHERE a.source_id = sources.id AND LOWER(a.name) LIKE ?)"
                " OR EXISTS(SELECT 1 FROM source_paper_meta m"
                "    WHERE m.source_id = sources.id AND LOWER(m.paper_title) LIKE ?))"
            )
            like = f"%{needle}%"
            params += [like, like, like, like]
```

（docstring 的「按 title/file_name 过滤」同步改为「按 title/file_name/作者名/论文标题」。）
- [ ] **Step 5: 跑测试至绿** — `python -m pytest tests/test_paper_meta_store.py tests/test_sources_page_batched.py -q`，预期 PASS。
- [ ] **Step 6: api_contract golden regen** — SourceSummary/SourceDetail 变形会红掉三个契约测试。先 `git show b7985e8` 看上次「直接重算」怎么做，再读 `backend/tests/test_repository_api_contract.py:270-320` 确认 `_contract()` / `_runtime_serialization()` 的取数方式，写一段一次性 python（放 scratchpad，不入库）把 `api_contract.json` 的 `"openapi"` 与 `"serialization"` 两键用与测试**完全相同**的计算重写（`source_commit` 保持不动）。然后 `python -m pytest tests/test_repository_api_contract.py -q` 预期 PASS；`git diff --stat backend/tests/fixtures/repository_contract/api_contract.json` 确认只有新增字段相关变化。
- [ ] **Step 7: 回归** — `python -m pytest tests/test_paper_meta_schema.py tests/test_legacy_db_compat.py tests/test_repository_api_contract.py -q`，预期 PASS。
- [ ] **Step 8: Commit** — `git commit -am "feat(store): paper meta persistence, hydration and author-aware source search"`

---

### Task 4: 服务集成（ensure + 双挂载 + backfill）+ facade 委托 + manifest

**Files:**
- Modify: `backend/app/services/source_ingestion.py`（import 区 + `run_extraction` 开头 + `process_source` 挂载 + 类尾追加两方法）
- Modify: `backend/app/services/sqlite_repository.py`（类尾 ~3274 行前追加 3 个委托）
- Modify: `backend/app/repositories/ownership_manifest.py`（`SURFACE_MEMBERS` 按字母序插 3 条）
- Modify: `backend/app/core/config.py`（settings 加 2 字段，放 `kg_llm_model` 字段之后）
- Test: `backend/tests/test_paper_meta_service.py`（新文件）

**Interfaces:**
- Consumes: Task 2 `verify_paper_meta`/`paper_meta_prompt`/`PAPER_META_SCHEMA_HINT`；Task 3 store 方法。
- Produces（Task 5/6 依赖）:
  - `SourceIngestionService.ensure_paper_metadata(source, elements=None, force=False) -> str`（状态串 `stored|not_paper|skipped|disabled|no_llm|no_text|failed`）
  - `SourceIngestionService.backfill_paper_metadata(notebook_id, force=False, progress=None) -> dict`（计数 `{"total": int, "<status>": int, ...}`；progress 签名 `(done, total, source_id, status)`）
  - facade 同名委托：`SQLiteRepository.backfill_paper_metadata` / `.get_paper_meta` / `.sources_missing_paper_meta`
  - settings：`paper_meta_enabled: bool = True`（env `PAPER_META_ENABLED`）、`paper_meta_head_chars: int = 4000`（env `PAPER_META_HEAD_CHARS`）

- [ ] **Step 1: 写失败测试** — 新建 `backend/tests/test_paper_meta_service.py`。repo fixture 照 `backend/tests/test_batch_ingest.py` 顶部的构建方式（含临时目录/Settings 覆写）；FakeLLM：

```python
import json


class _FakeKgLLM:
    def __init__(self, payload):
        self.configured = True
        self.model = "fake-kg"
        self.payload = payload
        self.calls = 0

    def chat_json(self, messages, schema_hint, **kwargs):
        self.calls += 1
        return json.dumps(self.payload)


PAYLOAD = {
    "is_paper": True, "title": "Gate Sizing Under Variability",
    "authors": [{"name": "Chen Hao", "affiliations": ["Fudan University"]}],
    "venue": "DAC", "year": 2025, "doi": "", "keywords": [],
}
HEAD_TEXT = ("Gate Sizing Under Variability\nChen Hao\nFudan University\nDAC 2025\n"
             "Abstract: ...")
```

用 repo 建 notebook + 源（源文本=HEAD_TEXT，可用 `repo.eval_insert_source_for_test` 或按 test_batch_ingest 的上传惯例），monkeypatch `repo` 的 kg_llm client 为 `_FakeKgLLM(PAYLOAD)`，覆盖：

```text
test_ensure_stores_verified_meta          ensure→"stored";get_paper_meta 作者/venue/年份齐;fake.calls==1
test_ensure_idempotent_skip               二次 ensure→"skipped";fake.calls 仍 1
test_ensure_force_reextracts              force=True→"stored";fake.calls==2
test_not_paper_marker_prevents_retry      payload is_paper=False→"not_paper";行存在 is_paper=0;再 ensure→"skipped"
test_no_llm_returns_no_llm_without_row    configured=False→"no_llm";无行;不抛
test_memory_source_gated                  source_type='memory' 源→"skipped";fake.calls==0
test_textbook_doc_type_gated              doc_type='textbook'→"skipped"
test_disabled_setting_gates               settings.paper_meta_enabled=False→"disabled"
test_llm_exception_is_swallowed           chat_json raise→"failed";无行;不抛(下次可重试)
test_run_extraction_catch_up              清掉 meta 行后跑 repo 的 run_extraction 路径(KG FakeLLM 可同 payload;
                                          若拆装成本高,直接调 service.run_extraction 前置断言 meta 行补上)
test_backfill_counts_and_progress         建 2 缺 meta 源+1 已有 → backfill 返回 {"total":2,"stored":2};
                                          progress 收到 2 次回调;再跑一次 total==0
```

- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_paper_meta_service.py -q`，预期 FAIL（`AttributeError: ensure_paper_metadata`）。
- [ ] **Step 3: config.py** — `kg_llm_model` 字段（`config.py:143`）之后插入：

```python
    # --- 论文元数据抽取(paper-metadata):对 academic_paper 源的文档头部做一次小
    # LLM 调用,接地校验后入库(source_paper_meta/source_authors)。
    paper_meta_enabled: bool = Field(True, validation_alias="PAPER_META_ENABLED")
    paper_meta_head_chars: int = Field(4000, validation_alias="PAPER_META_HEAD_CHARS")
```

- [ ] **Step 4: source_ingestion.py 服务实现** —
  1. import 区补：`import concurrent.futures`（与既有 import 并列）、`from app.core.llm import cap_kwargs`、`from app.services.kg.client import safe_json`、`from app.services.paper_meta import PAPER_META_SCHEMA_HINT, paper_meta_prompt, verify_paper_meta`。
  2. 类尾（`run_extraction` 之后）追加：

```python
    # ---------------------------------------------------- paper metadata
    def ensure_paper_metadata(
        self,
        source: "SourceSummary | SourceDetail",
        elements: Optional[List[SourceElement]] = None,
        force: bool = False,
    ) -> str:
        """单源论文元数据抽取(best-effort,幂等)。返回状态串 stored/not_paper/
        skipped/disabled/no_llm/no_text/failed,仅供调用方统计,不进状态机。
        挂载点:process_source(force=True,re-parse 即刷新)与 run_extraction 开头
        (force=False,历史源 catch-up);批量走 backfill_paper_metadata。
        成本:每源一次 chat_json(头部 ~paper_meta_head_chars 字符,输出~300 token);
        gate=paper_meta_enabled ∧ doc_type∈{'',academic_paper} ∧ 非合成源 ∧ LLM
        已配 ∧ 有文本 ∧ (force ∨ 无行)。失败不写行(下次可重试)、不碰
        extraction_runs、不阻断流水线(摄取侧惯例,不用 note_model_error)。"""
        if not getattr(self.settings, "paper_meta_enabled", True):
            return "disabled"
        if source.type in ("memory", "knowhow"):
            return "skipped"
        doc_type = (
            self.normalize_doc_type(getattr(source, "doc_type", "") or "")
            or "academic_paper"
        )
        if doc_type != "academic_paper":
            return "skipped"
        if not force and self.sources.get_paper_meta(source.id) is not None:
            return "skipped"
        client = self.kg_llm()
        if not getattr(client, "configured", False):
            return "no_llm"
        if elements is None:
            elements = self.source_elements(source.id)
        head_chars = int(getattr(self.settings, "paper_meta_head_chars", 4000))
        head_text = self.source_files.read_source_text(
            getattr(source, "file_path", "") or "", elements
        )[:head_chars]
        if not head_text.strip():
            return "no_text"
        try:
            raw = client.chat_json(
                [{"role": "user", "content": paper_meta_prompt(head_text)}],
                PAPER_META_SCHEMA_HINT,
                temperature=0.0,
                **cap_kwargs(client, "openai_compat_max_tokens"),
            )
            meta = verify_paper_meta(
                safe_json(raw), head_text,
                model=str(getattr(client, "model", "") or ""),
            )
            self.sources.upsert_paper_meta(source.id, source.notebook_id, meta)
            self.event_log.emit({
                "kind": "paper_meta",
                "source_id": source.id,
                "notebook_id": source.notebook_id,
                "is_paper": bool(meta["is_paper"]),
                "authors": len(meta["authors"]),
                "dropped": meta["dropped"],
            })
            return "stored" if meta["is_paper"] else "not_paper"
        except Exception:
            self.event_log.logger.exception(
                "paper metadata extraction failed for %s", source.id
            )
            return "failed"

    def backfill_paper_metadata(
        self,
        notebook_id: str,
        force: bool = False,
        progress: Optional[Callable[[int, int, str, str], None]] = None,
    ) -> dict:
        """批量补抽缺论文元数据的源(CLI phase=metadata 与应用内端点共用)。
        幂等键=meta 行存在;失败源不落行,重跑自动重试(断点续跑)。有界并发
        (≤8,受 kg_extract_workers 约束),任务级 copy_context 传播 per-user
        模型配置。返回 {"total": N, "<status>": n, ...} 计数。"""
        self.notebooks.get_row(notebook_id)  # KeyError if missing
        targets = self.sources.sources_missing_paper_meta(
            notebook_id, include_existing=force
        )
        counts: dict = {"total": len(targets)}
        if not targets:
            return counts
        workers = max(1, min(8, int(getattr(self.settings, "kg_extract_workers", 4))))
        lock = threading.Lock()
        done = 0

        def _one(source_id: str) -> None:
            nonlocal done
            try:
                row = self.sources.get_source(source_id)
                status = self.ensure_paper_metadata(row, force=force)
            except Exception:
                status = "failed"
                self.event_log.logger.exception(
                    "paper metadata backfill failed for %s", source_id
                )
            with lock:
                done += 1
                counts[status] = counts.get(status, 0) + 1
                current = done
            if progress is not None:
                progress(current, len(targets), source_id, status)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="paper-meta"
        ) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, _one, sid)
                for sid in targets
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()
        self.event_log.emit(
            {"kind": "paper_meta", "notebook_id": notebook_id, "backfill": counts}
        )
        return counts
```

  3. **挂载①** `process_source`：`self.set_source_status(source_id, "parsed", summary=summary)`（`:515`）之后、chunk build try 块之前插入：

```python
            # 论文元数据(best-effort):初次上传即抽,re-parse 时 force 刷新;
            # 失败不阻断流水线。落库在终态转换前,前端轮询随状态变化带到。
            self.ensure_paper_metadata(source, elements=elements, force=True)
```

  4. **挂载②** `run_extraction`：`elements = self.source_elements(source_id)`（`:918`）之后插入：

```python
        # 历史源 catch-up:补论文元数据(幂等,有行即跳;失败不影响 KG 抽取)。
        self.ensure_paper_metadata(source, elements=elements, force=False)
```

- [ ] **Step 5: facade 委托（类尾 ~3274 行、模块级 `def _now()` 之前追加）**：

```python
    # ---------------------------------------------------- paper metadata
    def get_paper_meta(self, source_id: str) -> Optional[dict]:
        return self._runtime.source_store.get_paper_meta(source_id)

    def sources_missing_paper_meta(
        self, notebook_id: str, include_existing: bool = False
    ) -> List[str]:
        return self._runtime.source_store.sources_missing_paper_meta(
            notebook_id, include_existing
        )

    def backfill_paper_metadata(
        self, notebook_id: str, force: bool = False, progress=None
    ) -> dict:
        return self._runtime.source_ingestion.backfill_paper_metadata(
            notebook_id, force=force, progress=progress
        )
```

- [ ] **Step 6: 跑服务测试至绿** — `python -m pytest tests/test_paper_meta_service.py -q`，预期 PASS。
- [ ] **Step 7: manifest 对齐** — 跑 `python -m pytest tests/test_repository_surface_manifest.py tests/test_repository_facade_contract.py -q`。按失败输出在 `ownership_manifest.py` 的 `SURFACE_MEMBERS` 按字母序插入 3 条 `SurfaceMember`（owner：`get_paper_meta`/`sources_missing_paper_meta` → `'SourceStore'`；`backfill_paper_metadata` → `'SourceIngestionService'`；kind `'method'`；consumers 用测试报告的实际 file:line，本任务阶段是测试文件，Task 5/6 加了 routes/batch 消费点后**会再红一次，属预期，届时补行号**）。`RUNTIME_COMPONENT_OWNERS` 无需改（两个 owner 已注册）。重跑至绿。
- [ ] **Step 8: 回归** — `python -m pytest tests/test_paper_meta_store.py tests/test_batch_ingest.py -q`（后者防 process_source 挂载破坏既有摄取测试；若有 FakeLLM 未配的用例,ensure 走 no_llm 静默,不应有行为变化），预期 PASS。
- [ ] **Step 9: Commit** — `git commit -am "feat(ingest): paper metadata extraction with grounding, pipeline mounts + backfill service"`

---

### Task 5: backfill API 端点 + 契约 regen

**Files:**
- Modify: `backend/app/api/routes.py`（**EOF 追加**端点）
- Regen: `backend/tests/fixtures/repository_contract/api_contract.json`（同 Task 3 Step 6 方法）
- Modify: `backend/app/repositories/ownership_manifest.py`（3 条 member 的 consumers 补 routes.py 行号）
- Test: `backend/tests/test_paper_meta_api.py`（新文件）

**Interfaces:**
- Consumes: facade `backfill_paper_metadata` / `sources_missing_paper_meta`。
- Produces: `POST /api/notebooks/{notebook_id}/paper-meta/backfill` → `{"queued": int}`；409=LLM 未配置；404=非 owner/不存在（不泄露存在性）。

- [ ] **Step 1: 写失败测试** — 新建 `backend/tests/test_paper_meta_api.py`。先找现有对 `POST /notebooks/{id}/kg/build` 或近似 owner 门控端点的测试（`grep -rn "kg/build\|require_notebook_access" backend/tests | head`），复用其 TestClient + 登录 fixture。用例：

```text
test_backfill_endpoint_queues_missing     owner 调 → 200 {"queued": 2};(monkeypatch
                                          background_jobs.submit 为同步直调或断言其被调用)
test_backfill_endpoint_zero_noop          全部已有 meta → {"queued": 0} 且不 submit
test_backfill_requires_llm                LLM 未配置 → 409
test_backfill_owner_gate                  非 owner 用户调 → 404
```

- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_paper_meta_api.py -q`，预期 FAIL（404 路由不存在）。
- [ ] **Step 3: 实现端点（routes.py EOF 追加）** — 模板照 `build_kg`（`routes.py:993-1006`）：

```python
@router.post(
    "/notebooks/{notebook_id}/paper-meta/backfill",
    dependencies=[Depends(require_notebook_access)],
)
def backfill_paper_metadata(notebook_id: str) -> dict:
    """补抽该 notebook 缺论文元数据的源(后台线程,幂等可续跑)。返回排队数;
    LLM 未配置 409。owner 门控由 require_notebook_access 承担(非 owner 404)。"""
    repo = repository()
    llm_ready = getattr(repo.kg_llm_client, "configured", False) or getattr(
        repo.llm_client, "configured", False
    )
    if not llm_ready:
        raise HTTPException(status_code=409, detail="LLM not configured")
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    queued = len(repo.sources_missing_paper_meta(notebook_id))
    if queued:
        background_jobs.submit(
            repo.backfill_paper_metadata, notebook_id,
            name=f"papermeta-{notebook_id}",
        )
    return {"queued": queued}
```

（`repository`/`background_jobs`/`require_notebook_access` 均为 routes.py 既有 import；若缺则在其 import 现址补名字，不新增 import 行序扰动——先 grep 确认。）
- [ ] **Step 4: 跑测试至绿** — `python -m pytest tests/test_paper_meta_api.py -q`，预期 PASS。
- [ ] **Step 5: 契约 + manifest 再对齐** — `python -m pytest tests/test_repository_api_contract.py tests/test_repository_surface_manifest.py tests/test_repository_callers_static.py -q`；openapi 因新端点变化 → 重跑 Task 3 Step 6 的 regen 脚本；manifest 三条 member 的 consumers 按输出补 `backend/app/api/routes.py:<line>`。重跑至绿。
- [ ] **Step 6: Commit** — `git commit -am "feat(api): paper metadata backfill endpoint (owner-gated, background)"`

---

### Task 6: CLI phase `metadata` + README 双语

**Files:**
- Modify: `backend/app/services/batch_ingest.py`（choices/`--force`/分发/`run_metadata`）
- Modify: `README.md`、`README_zh.md`（batch_ingest 用法段落各加 metadata phase）
- Modify: `backend/app/repositories/ownership_manifest.py`（consumers 补 batch_ingest.py 行号）
- Test: `backend/tests/test_batch_ingest.py`（**EOF 追加**用例，防行号漂移）

**Interfaces:**
- Consumes: facade `backfill_paper_metadata`。
- Produces: `python scripts/batch_ingest.py metadata --notebook-id <id> [--force]`，退出码 0 成功 / 2 配置错误。

- [ ] **Step 1: 写失败测试** — `test_batch_ingest.py` EOF 追加（fixture 复用该文件现有 repo/args 构造惯例）：

```text
test_metadata_phase_requires_llm       kg_llm 与 llm 均未配置 → main 返回 2,stderr 含「LLM 未配置」
test_metadata_phase_requires_notebook  未给 --notebook-id → 返回 2(绝不新建 notebook)
test_metadata_phase_backfills          FakeLLM + 2 缺 meta 源 → 返回 0;两源 get_paper_meta 非 None;
                                       再跑一次输出 total 0(幂等续跑)
test_metadata_phase_force              --force → 已有行源也重抽(fake.calls 增加)
```

- [ ] **Step 2: 跑测试确认失败** — `python -m pytest tests/test_batch_ingest.py -k metadata_phase -q`，预期 FAIL（argparse invalid choice）。
- [ ] **Step 3: 实现** —
  1. choices（`batch_ingest.py:885`）加 `"metadata"`；argparse 参数区（`--allow-no-embed` 附近）加：

```python
    p.add_argument("--force", action="store_true",
                   help="metadata phase: 已有元数据行的源也重抽(prompt/校验升级后刷新)")
```

  2. `run_metadata`（定义放 `run_embed` 之后、argparse 之前）：

```python
def run_metadata(repo, args) -> int:
    """phase=metadata:补抽 notebook 内缺论文元数据的源(幂等可续跑;--force 重抽)。
    只作用于已解析的 academic_paper 源;原始 PDF 缺失也可跑(读 DB 内 elements)。"""
    if not (
        getattr(repo.kg_llm_client, "configured", False)
        or getattr(repo.llm_client, "configured", False)
    ):
        print("error: LLM 未配置 → 无法抽取论文元数据。配置 OPENAI_COMPAT_* 或 "
              "KG_LLM_* 后重试(CLI 不静默降级)。", file=sys.stderr)
        return 2
    if not args.notebook_id:
        print("error: --phase metadata 需要 --notebook-id(本 phase 不新建 notebook)。",
              file=sys.stderr)
        return 2

    def _progress(done: int, total: int, source_id: str, status: str) -> None:
        print(f"[meta {done}/{total}] {source_id} {status}", flush=True)

    counts = repo.backfill_paper_metadata(
        args.notebook_id, force=args.force, progress=_progress
    )
    print(f"[meta done] {json.dumps(counts, ensure_ascii=False)}", flush=True)
    return 0
```

  3. main 分发：放在 `backfill-source-index` 块（`:979`）之后、embed 就绪校验（`:987`）**之前**（metadata 不产向量，不受 embed 校验约束；也绝不能走到 `ensure_notebook` 的新建逻辑）：

```python
    if args.phase == "metadata":
        return run_metadata(repo, args)
```

- [ ] **Step 4: 跑测试至绿** — `python -m pytest tests/test_batch_ingest.py -k metadata_phase -q`，预期 PASS；全文件回归 `python -m pytest tests/test_batch_ingest.py -q` PASS。
- [ ] **Step 5: README 双语** — `README.md` 与 `README_zh.md` 找到 batch_ingest/离线摄取小节（`grep -n "batch_ingest" README*.md`），各追加 metadata phase 一段（通用口径，不含机器路径）：用途（历史论文补抽作者/机构等元数据，幂等可续跑）、命令示例 `python scripts/batch_ingest.py metadata --notebook-id <id> [--force]`、前置条件（LLM 已配置；源已解析）。
- [ ] **Step 6: manifest consumers 补行号** — `python -m pytest tests/test_repository_surface_manifest.py tests/test_repository_callers_static.py -q`，按输出把 `backfill_paper_metadata` 的 consumers 补上 `backend/app/services/batch_ingest.py:<line>`，重跑至绿。
- [ ] **Step 7: Commit** — `git commit -am "feat(cli): batch_ingest metadata phase for historical paper backfill + README"`

---

### Task 7: 前端（类型 + 详情块 + 搜索占位 + 补抽按钮）

**Files:**
- Modify: `frontend/app/workspace-model.ts`（`SourceSummary` type + 新类型，`:106-122`）
- Modify: `frontend/app/page.tsx`（详情 modal `:4412` 后、搜索框 `:3575`、add-source 区 `:3488` 附近）
- Modify: 全局样式文件（`grep -rn "source-detail-meta" frontend/app/*.css` 定位）

**Interfaces:**
- Consumes: Task 3/5 的 API 形状（`SourceSummary.authors/pub_year/venue`、`SourceDetail.paper_meta`、backfill 端点）。
- Produces: 用户可见的论文信息块、作者搜索、补抽入口。

- [ ] **Step 1: 类型** — `workspace-model.ts` 在 `SourceSummary` 前加：

```typescript
export type PaperAuthor = {
  name: string;
  affiliation: string;
};

export type PaperMeta = {
  is_paper: boolean;
  title?: string | null;
  venue?: string | null;
  year?: number | null;
  doi?: string | null;
  keywords: string[];
  authors: PaperAuthor[];
};
```

`SourceSummary` type 末尾加：

```typescript
  authors?: string[];
  pub_year?: number | null;
  venue?: string | null;
  paper_meta?: PaperMeta | null;
```

- [ ] **Step 2: 详情 modal 论文信息块** — page.tsx `source-detail-meta` div（`:4412-4416`）之后、`extraction_warning` 段之前插入（弯引号沿用现状；类名与既有 modal 风格统一）：

```tsx
              {sourceDetail.paper_meta?.is_paper && (
                <div className="source-detail-paper">
                  {sourceDetail.paper_meta.title && (
                    <p className="paper-title">{sourceDetail.paper_meta.title}</p>
                  )}
                  {sourceDetail.paper_meta.authors.length > 0 && (
                    <p className="paper-authors">
                      {sourceDetail.paper_meta.authors.map((a) => (
                        <span key={a.name} className="paper-author"
                              title={a.affiliation || undefined}>
                          {a.name}
                        </span>
                      ))}
                    </p>
                  )}
                  {(sourceDetail.paper_meta.venue || sourceDetail.paper_meta.year) && (
                    <p className="paper-venue">
                      {[sourceDetail.paper_meta.venue, sourceDetail.paper_meta.year]
                        .filter(Boolean).join(" · ")}
                    </p>
                  )}
                  {sourceDetail.paper_meta.doi && (
                    <a className="paper-doi" target="_blank" rel="noreferrer"
                       href={`https://doi.org/${sourceDetail.paper_meta.doi}`}>
                      DOI: {sourceDetail.paper_meta.doi}
                    </a>
                  )}
                  {sourceDetail.paper_meta.keywords.length > 0 && (
                    <p className="paper-keywords">
                      {sourceDetail.paper_meta.keywords.map((k) => (
                        <span key={k} className="tag">{k}</span>
                      ))}
                    </p>
                  )}
                </div>
              )}
```

配套 CSS（追加到 `source-detail-meta` 样式所在文件同区域；对齐/间距/字号与现有 modal 一致，作者名间用小间距，机构走 title hover）：

```css
.source-detail-paper { display: flex; flex-direction: column; gap: 6px; margin: 10px 0 4px; }
.source-detail-paper .paper-title { font-weight: 600; line-height: 1.4; }
.source-detail-paper .paper-authors { display: flex; flex-wrap: wrap; gap: 4px 12px; }
.source-detail-paper .paper-author { cursor: default; text-decoration: underline dotted transparent; }
.source-detail-paper .paper-author[title]:not([title=""]) { text-decoration-color: currentColor; opacity: .92; }
.source-detail-paper .paper-venue, .source-detail-paper .paper-doi { font-size: 12px; opacity: .75; }
.source-detail-paper .paper-doi { width: fit-content; }
.source-detail-paper .paper-keywords { display: flex; flex-wrap: wrap; gap: 4px; }
```

（变量/颜色照该文件现有写法调整——不硬编码新色值。）
- [ ] **Step 3: 搜索占位** — `:3575` `placeholder="搜索来源（标题/文件名）"` → `placeholder="搜索来源（标题/作者/文件名）"`。
- [ ] **Step 4: 补抽入口** — add-source-button（`:3488`）同区（`!isReader` 门控内）加一枚次级小按钮（class 复用该文件现有次级按钮样式，先 grep 附近按钮的 className 选现成的；文案友好不暴露技术细节）：

```tsx
              <button
                className="<现有次级按钮class>"
                title="为已上传的论文补齐作者、机构等信息"
                onClick={async () => {
                  if (!currentNotebookId) return;
                  try {
                    const res = await api<{ queued: number }>(
                      `/notebooks/${currentNotebookId}/paper-meta/backfill`,
                      { method: "POST" }
                    );
                    setToast(res.queued > 0
                      ? `已提交 ${res.queued} 篇论文的信息补全`
                      : "论文信息已是最新，无需补全");
                  } catch (err) {
                    reportError(err);
                  }
                }}
              >
                补全论文信息
              </button>
```

（API 路径不带 `/api` 前缀；409 会经 `api()` 的 detail 抽取进 `reportError`，文案后端已是 "LLM not configured"——前端 catch 后 toast/报错沿用现有 reportError 行为即可。）
- [ ] **Step 5: 验证** — worktree 无 node_modules：`cd frontend && ls node_modules 2>/dev/null || npm ci --no-audit --no-fund`（若网络不可用，跳过并在 Task 8 标注）；然后 `npx tsc --noEmit` 预期零错误。肉眼核对 JSX 括号配平 + `git diff frontend/app/page.tsx | grep -c '^-.*[""]'` 为 0（不许动既有弯引号）。
- [ ] **Step 6: Commit** — `git commit -am "feat(frontend): paper metadata display, author search hint + backfill entry"`

---

### Task 8: 全量回归 + 端到端冒烟 + PR

**Files:** 无新文件（修尾巴）。

- [ ] **Step 1: 后端全量** — `cd backend && python -m pytest tests/ -q -x --timeout=600`（若无 timeout 插件去掉该参）。预期全绿；任何 manifest/契约红按对应 Task 的对齐步骤修。
- [ ] **Step 2: 端到端冒烟（无真模型）** — 写临时脚本（放 scratchpad，不入库）：临时目录建 repo → 建 notebook → 插入一个含论文头部文本的源 → 挂 FakeLLM → `process_source` 或 `ensure_paper_metadata` → 断言 `list_sources_page(q="<作者名>")` 命中且 `SourceSummary.authors` 非空 → `get_source().paper_meta.title` 正确。打印结果留证。
- [ ] **Step 3: spec/README 一致性** — 快查 spec 第 3/5/9 节与实现无漂移（字段名、状态串、端点路径）；有漂移改 spec 并注明。
- [ ] **Step 4: PR** — 分支 rebase 到最新 master 保持线性（`git fetch origin && git rebase origin/master`，冲突按各 Task 语义解），`git push -u origin claude/paper-metadata-extraction-38ea3b`，`gh pr create --base master` —— PR 描述含：特性摘要、成本账（每新 paper 源 +1 次小 LLM 调用）、接地校验不变量、三通道补抽用法（含 CLI 示例）、SCHEMA_VERSION 16→17 需重启后端提示、真机验证清单（配真模型后跑一次 `metadata` phase 并抽查 dropped 事件）。PR body 尾部加 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`。

---

## Self-Review 记录

- Spec 覆盖：§3→Task 1；§4→Task 3/4；§5.1-5.2→Task 2/4；§5.3 接地→Task 2；§6→Task 3/5；§7→Task 3；§8→Task 7；§9→Task 4/5/6；§10→Task 4；§11 测试→各 Task Step 1；§12 守卫→约束区+各步骤。无缺口。
- 类型一致性：`verify_paper_meta` 返回键 = `upsert_paper_meta` 消费键 = `_paper_meta_dict` 产出键；`paper_meta_model` 映射 `paper_title→title`/`pub_year→year` 与 pydantic/TS 两侧一致；`ensure_paper_metadata` 状态串与 backfill 计数键一致；progress 四参签名 CLI/服务一致。
- 占位符：Task 1/3/4/5/6 的「复用现有 fixture 写法」均指向具体文件与行为断言（测试体逐条列出），regen 步骤指向具体先例 commit 与命令——实现子代理需先读所指文件再落码。
