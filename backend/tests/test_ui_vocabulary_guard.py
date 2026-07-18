"""单元测试:`scripts/check_ui_vocabulary.py`(界面词汇守卫,不属于 app 包)。

按文件路径直接 import(同 test_mineru_probe.py)。不触网、不 import app。

为什么必须有这个文件:上一轮守卫是靠「手工跑 22 个正反例」验证的,手工 probe 防不住
回归——事实上它当时就漏了自己声明的词表的一大半(抽取 / 入图 / 预审 / 晋升 / Memory /
schema / deprecated 全没进黑名单),而守卫退出码仍是 0。所以这里钉三件事:

  1. **词表覆盖**:AGENTS.md「界面词汇表」第一列的每个词,要么被某条黑名单规则命中,
     要么在 NOT_LINTABLE 里有白纸黑字的豁免理由。新增一行却不加规则 → 红。
  2. **正例**:每个黑名单词在渲染文本里都真的会被抓到(没有写了不生效的死规则)。
  3. **反例**:注释 / 标识符 / 插值 / 纯 ASCII / 豁免上下文都不误报,以及
     raw-enum-fallback 只抓「兜底即原值」、放行正当兜底。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _ROOT / "scripts" / "check_ui_vocabulary.py"
_spec = importlib.util.spec_from_file_location("check_ui_vocabulary", _SCRIPT_PATH)
guard = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["check_ui_vocabulary"] = guard
_spec.loader.exec_module(guard)

ALL_TERMS = {**guard.CJK_TERMS, **guard.ASCII_TERMS}


def scan_src(tmp_path: pathlib.Path, code: str, name: str = "sample.tsx") -> list[str]:
    """把一段源码写成临时文件跑黑名单扫描,返回命中的词名。"""
    path = tmp_path / name
    path.write_text(code, encoding="utf-8")
    return [term for _line, term, _unit in guard.scan(path)]


def scan_fallback(tmp_path: pathlib.Path, code: str) -> list[str]:
    path = tmp_path / "fallback.tsx"
    path.write_text(code, encoding="utf-8")
    return [snippet for _line, snippet in guard.scan_raw_fallback(path)]


# --------------------------------------------------------------------------
# 1. 词表覆盖:守卫 ⊇ AGENTS.md 词汇表
# --------------------------------------------------------------------------

# 词汇表里**刻意不做 lint** 的词 → 理由。每一条都是评审过的决定,不是遗漏。
# 这个字典就是「守卫覆盖的是词表子集」这件事的显式账本:任何缩水都必须写在这里。
NOT_LINTABLE = {
    "节点": "图谱视图画出来的就是节点,属词汇表自己写明的「图谱技术上下文」;裸用靠人工把关",
    "知识节点": "同「节点」;复合形态 孤立节点 已入黑名单",
    "边": "与 旁边 / 边框 / 边距 / 边界 同形,词级 lint 判不了;复合形态 关系边 / 补连边 / 边审 已入黑名单",
    "构建": "裸「构建」是正常动词;黑话形态「构建·建立·重建 + 知识图谱」由 构建知识图谱 规则覆盖",
    "动作": "非词条——是「晋升：动作 / 状态 / 队列」那一行的三个方面,被表格的「：」切出来的解析产物",
    "状态": "同上",
    "队列": "同上",
}


def table_tokens() -> list[str]:
    """AGENTS.md 界面词汇表第一列(内部 / 黑话)拆成单个词。"""
    text = (_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    section = text.split("## 界面词汇表")[1].split("\n## ")[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("|") and "---" not in ln]
    assert len(rows) > 10, "词汇表没解析出足够的行——表格结构变了,本测试需同步"
    tokens: list[str] = []
    for row in rows[1:]:  # rows[0] 是表头
        col = re.sub(r"[（(][^）)]*[）)]", "", row.split("|")[1])  # 去括注
        tokens += [t.strip() for t in re.split(r"[/／、·]|：", col) if t.strip()]
    return tokens


def test_每个词表条目要么被守卫覆盖要么有豁免理由():
    uncovered = [
        tok
        for tok in table_tokens()
        if tok not in NOT_LINTABLE and not any(p.search(tok) for p in ALL_TERMS.values())
    ]
    assert not uncovered, (
        f"词汇表这些词没有对应的黑名单规则,也没登记豁免理由:{uncovered}。"
        "要么给 CJK_TERMS/ASCII_TERMS 加规则,要么在 NOT_LINTABLE 里写明为何不 lint。"
    )


def test_豁免清单本身不许含已被覆盖的词():
    """防止 NOT_LINTABLE 变成掩盖真实规则的垃圾桶:登记了豁免却其实能匹配 = 账本失真。"""
    bogus = [tok for tok in NOT_LINTABLE if any(p.search(tok) for p in ALL_TERMS.values())]
    assert not bogus, f"这些词其实已被黑名单覆盖,不该登记成豁免:{bogus}"


# --------------------------------------------------------------------------
# 2. 正例:每个黑名单词都真的会被抓到
# --------------------------------------------------------------------------


@pytest.mark.parametrize("term", sorted(ALL_TERMS))
def test_黑名单词出现在渲染文本里必被抓到(tmp_path, term):
    # 词名本身必须是它自己规则的合法样本,否则下面的断言测的就不是这条规则。
    assert ALL_TERMS[term].search(term), f"规则 {term} 匹配不了自己的名字,规则写坏了"
    assert term in scan_src(tmp_path, f'const t = "这里提到{term}了";')


@pytest.mark.parametrize(
    "code",
    [
        'const a = "重建投影完成";',                      # 字符串字面量
        "const b = `本次${n}条基准语料`;",                  # 模板串
        "export const c = <p>已完成边审</p>;",             # JSX 文本节点
        'const d = { title: "未入图的来源" };',             # 对象属性值
        'const e = <button title="补连边" />;',            # JSX 属性
        'const f = "共 3 个 chunk";',                     # ASCII 词紧邻中文
        'const g = "保存到 Memory";',                      # 残留英文散文
        'const h = "Schema 已更新";',                      # 大小写变体
    ],
)
def test_各类渲染位置都覆盖(tmp_path, code):
    assert scan_src(tmp_path, code), f"这段渲染文本没被抓到:{code}"


def test_同一单元里多个黑话全部报出(tmp_path):
    hits = scan_src(tmp_path, 'const t = "从基准库抽取 chunk";')
    assert {"基准库", "抽取", "chunk"} <= set(hits)


def test_报出的行号指向文案本身而不是上一行(tmp_path):
    """JSX 文本节点紧接上一个标签的 `>` 起算,天然以换行+缩进开头;若按 run 起点
    数行,报出来的会是文案上面那一行,大文件里很误导。"""
    code = (
        "export const view = (\n"          # 1
        "  <label>\n"                      # 2
        "    <input />\n"                  # 3
        "    同时抽取到知识图谱\n"            # 4 ← 文案真正所在行
        "  </label>\n"                     # 5
        ");\n"
    )
    path = tmp_path / "line.tsx"
    path.write_text(code, encoding="utf-8")
    hits = [(line, term) for line, term, _unit in guard.scan(path)]
    assert (4, "抽取") in hits, hits


# --------------------------------------------------------------------------
# 3. 反例:允许的上下文必须通过
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        # 注释里保留内部名是设计要求(内部词留在代码里)
        "// 从基准库抽取 chunk 后入图\nconst a = 1;",
        "/* 投影 / 边审 / 晋升 队列 */\nconst b = 2;",
        # 标识符:ASCII 词粘连字母 → 不算
        "const currentNotebook = 1; const memoryHash = 2; const schemaBusy = 3;",
        "const x = <PKG />; const scanner = new Scanner();",
        # 纯 ASCII 单元(没有中文)不是给用户看的中文文案
        'const id = "chunk"; const t = "notebook";',
        'const status = "deprecated";',
        # 插值里的标识符会被剥掉,不当渲染文本
        "const s = `共 ${chunkCount} 段`;",
        "export const e = <p>共 {notebookCount} 个</p>;",
        # 豁免上下文:「抽取字段」是词汇表给 schema 定的界面词,不能自己杀自己
        'const a = "内容类型 / 抽取字段";',
        # 豁免上下文:「加入图谱」是正常动宾,不是「入图 / 未入图」那个黑话
        'const b = "把这一行加入图谱";',
        # 刻意保留的用户词
        'const c = "知识图谱已就绪";',
        'const d = "建立快速查找结构";',
        'const f = "为本笔记本建立索引";',
        'const g = "知识库";',
        # 裸「节点」「边」不 lint(见 NOT_LINTABLE)
        'const h = "节点 12 · 边 30";',
        'const i = "右边的边框";',
    ],
)
def test_允许的上下文不误报(tmp_path, code):
    assert scan_src(tmp_path, code) == [], f"误报:{code}"


def test_正则字面量里的引号不会把代码拖进渲染文本(tmp_path):
    """回归:page.tsx sanitizeTableHtml 的正则含成对引号,曾让字符串扫描从正则内部
    一路匹配到后面的引号,把整段代码当成「渲染文本」报出来(补全黑名单后集中暴露)。
    正则体现在会被 blank_comments 抹成空白。"""
    code = (
        'function sanitize(html: string) {\n'
        '  const a = html.replace(/\\son\\w+\\s*=\\s*("[^"]*"|\'[^\']*\'|[^\\s>]+)/gi, "");\n'
        '  return <p>共 3 段原文</p>;\n'
        '}\n'
    )
    assert scan_src(tmp_path, code) == []


# --------------------------------------------------------------------------
# 4. raw enum fallback:「兜底即原值」
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "const a = RELATION_LABELS[edge.edge_type] ?? edge.edge_type;",
        "const b = FIELD_LABELS[key] ?? key;",
        "const c = STATUS_LABELS[s] || s;",
        "const d = label(TIER, tier, tier);",
        "const e = <span>{MAP[item.status] ?? item.status}</span>;",
        # 表挂在对象/命名空间上时同样要抓到(前缀 `.` 不能让规则漏过)
        "const f = vocab.STATUS_LABELS[s] ?? s;",
    ],
)
def test_兜底即原值会被抓到(tmp_path, code):
    assert scan_fallback(tmp_path, code), f"没抓到「兜底即原值」:{code}"


@pytest.mark.parametrize(
    "code",
    [
        # 正当兜底:退到中性词而非原值
        'const a = RELATION_LABELS[edge.edge_type] ?? "关联";',
        'const b = label(TIER, tier, "未知来源");',
        'const c = MAP[key] ?? "";',
        # 经评审的透出路径:自定义 object_type / 字段名原样显示(用户自己起的名字)。
        # 写成 Object.hasOwn 三元而非 ?? ——顺带免疫原型链白屏。
        "const d = Object.hasOwn(KG_TYPE_LABELS, type) ? KG_TYPE_LABELS[type] : type;",
        "const e = Object.hasOwn(FIELD_LABELS, key) ? FIELD_LABELS[key] : key;",
        # 不同的键与兜底,不是同一表达式
        "const f = MAP[a] ?? b;",
        # 注释里的反面教材不该让构建失败
        "// 别写 MAP[x] ?? x\nconst g = 1;",
    ],
)
def test_正当兜底不误报(tmp_path, code):
    assert scan_fallback(tmp_path, code) == [], f"误报:{code}"


# --------------------------------------------------------------------------
# 5. 端到端:真实仓库当前是干净的
# --------------------------------------------------------------------------


def test_真实前端源码通过守卫(capsys):
    assert guard.main() == 0, capsys.readouterr().err
