"""单元测试:`scripts/check_ui_vocabulary.py`(界面词汇守卫,不属于 app 包)。

按文件路径直接 import(同 test_mineru_probe.py)。不触网、不 import app。

为什么必须有这个文件:上一轮守卫是靠「手工跑 22 个正反例」验证的,手工 probe 防不住
回归——事实上它当时就漏了自己声明的词表的一大半(抽取 / 入图 / 预审 / 晋升 / Memory /
schema / deprecated 全没进黑名单),而守卫退出码仍是 0。所以这里钉三件事:

  1. **词表覆盖**:`docs/ui-vocabulary.md`「界面词汇表」第一列的每个词,要么被某条黑名单规则命中,
     要么在 NOT_LINTABLE 里有白纸黑字的豁免理由。新增一行却不加规则 → 红。
  2. **正例**:每个黑名单词在渲染文本里都真的会被抓到(没有写了不生效的死规则)。
  3. **反例**:注释 / 标识符 / 插值 / 纯 ASCII / 豁免上下文都不误报。
  4. **跨层**:后端被 `user_error()` 标记为「可展示」的文案同样受词表约束
     (第二轮 review 阻塞 2——作用域跟信任边界走,不跟目录走)。

「兜底即原值」(`MAP[x] ?? x`)那条检查已改用 TypeScript AST 并搬去
`frontend/tests/guards/raw-enum-fallback.test.mjs`,正反样例在那边;这里只留两条断言钉住
「搬走了、没丢、也没退回正则」。
"""
from __future__ import annotations

import importlib.util
import json
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


# --------------------------------------------------------------------------
# 1. 词表覆盖:守卫 ⊇ docs/ui-vocabulary.md 词汇表
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
    "画像": "图谱质量分析视图已把「来源画像」用作合法界面词(source profile),裸「画像」子串"
    "匹配会连它一起误杀;与「节点」/「边」同一处理,靠人工把关。无歧义复合形态"
    "底座画像/覆盖层画像 已按「孤立节点」同款政策入黑名单,不在此豁免",
}


def table_tokens() -> list[str]:
    """docs/ui-vocabulary.md 界面词汇表第一列(内部 / 黑话)拆成单个词。"""
    text = (_ROOT / "docs/ui-vocabulary.md").read_text(encoding="utf-8")
    section = text.split("## 界面词汇表")[1].split("\n## ")[0]
    rows = [ln for ln in section.splitlines() if ln.startswith("|") and "---" not in ln]
    assert len(rows) > 10, "词汇表没解析出足够的行——表格结构变了,本测试需同步"
    tokens: list[str] = []
    for row in rows[1:]:  # rows[0] 是表头
        col = re.sub(r"[（(][^）)]*[）)]", "", row.split("|")[1])  # 去括注
        tokens += [t.strip() for t in re.split(r"[/／、·]|：", col) if t.strip()]
    return tokens


def test_词汇文档与守卫登记相同的前端生产扫描面():
    expected = {
        _ROOT / "frontend" / "app",
        _ROOT / "frontend" / "features",
    }
    assert set(guard.FRONTEND_PRODUCTION_DIRS) == expected
    for relative_path in (
        "docs/ui-vocabulary.md",
        "docs/product-and-api.md",
        "docs/product-and-api_zh.md",
    ):
        text = (_ROOT / relative_path).read_text(encoding="utf-8")
        assert "frontend/app" in text
        assert "frontend/features" in text


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


def test_豁免清单不许挂着词表里已不存在的词():
    """反向孤儿检查:词表删行后,对应豁免必须一起删——否则 NOT_LINTABLE 变成
    无人认领的登记(镜像 test_user_error 的 stale 半边;「动作/状态/队列」是
    表格「：」切分的解析产物,同样应随其宿主行存在)。"""
    tokens = set(table_tokens())
    orphans = [tok for tok in NOT_LINTABLE if tok not in tokens]
    assert not orphans, f"这些豁免在词汇表里已没有对应词,应一起删除:{orphans}"


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
        'const i = "这条观察队列记录已同步";',                # Agentic Memory P3
    ],
)
def test_各类渲染位置都覆盖(tmp_path, code):
    assert scan_src(tmp_path, code), f"这段渲染文本没被抓到:{code}"


@pytest.mark.parametrize(
    "code",
    [
        'const a = "保存完成后再插入图片。";',   # 「插入图片」含「入图」子串
        'const b = "点这里插入图片";',
        'const c = "已加入图谱";',              # 既有排除项,一并钉住
        'const d = "本次分析观察到三处异常";',    # 「观察」单独出现,不含「队列」
        'const e = "后台任务队列已清空";',       # 「队列」单独出现,不含「观察」
    ],
)
def test_正常词组不因子串被误报(tmp_path, code):
    """中文没有词边界,子串匹配的词条必须两侧都挡住正常词组。

    REGRESSION:「入图」曾把「插入图片」判为黑话,让编辑器里三句最普通的提示
    (『保存完成后再插入图片』等)全部报错——那是完全合格的界面中文,该改的是
    守卫不是文案。"""
    assert not scan_src(tmp_path, code), f"这段正常文案被误报了:{code}"


def test_图谱Schema放行但裸schema仍抓(tmp_path):
    """产品决策(2026-07-23)放行「图谱 Schema」这一个界面短语;放行必须**收窄**——
    只剥这个复合短语,裸 schema / 裸 Schema(不带「图谱」前缀)照抓,否则守卫被掏空。

    没有这条,「放行了图谱 Schema」与「把整条 schema 规则删了」在绿灯下无法区分。
    真源:docs/ui-vocabulary.md schema 行放行注记 + check_ui_vocabulary.SANCTIONED_UI。"""
    # 1) 放行:界面名「图谱 Schema」及其常见后缀不报
    assert scan_src(tmp_path, 'const a = "图谱 Schema";') == []
    assert scan_src(tmp_path, 'const b = "图谱 Schema 已更新";') == []
    assert scan_src(tmp_path, "export const c = <button>图谱 Schema</button>;") == []
    # 2) 收窄:裸 schema / 裸 Schema(无「图谱」前缀)仍必被抓——这批若不报=规则被掏空
    assert "schema" in scan_src(tmp_path, 'const d = "schema 版本回滚";')
    assert "schema" in scan_src(tmp_path, 'const e = "Schema 已更新";')
    # 3) 同一单元里放行短语旁边另有裸 schema:只剥放行的那个,另一个仍抓
    assert "schema" in scan_src(tmp_path, 'const f = "图谱 Schema 用的 schema 版本";')
    # 4) 收窄(评审 P2):放行短语的「图谱」前不能紧挨 CJK,否则会吃掉「知识图谱」尾字,
    #    把「构建知识图谱 Schema」里的 构建知识图谱 违规一起剥掉而漏报。这批必须仍被抓。
    assert "构建知识图谱" in scan_src(tmp_path, 'const g = "构建知识图谱 Schema";')
    assert "构建知识图谱" in scan_src(tmp_path, 'const h = "重建知识图谱Schema入口";')
    # 5) 多空格 / 跨行 JSX 文本仍放行——钉住 \s* 的宽度,防「收紧成单空格」的移动变异。
    assert scan_src(tmp_path, 'const i = "图谱  Schema";') == []
    assert scan_src(tmp_path, "export const j = <span>图谱\n  Schema</span>;") == []
    # 6) 后置边界(codex 第 2/3 轮 P2):只放行完整单词 Schema,做**标识符前缀**的更长
    #    形态一律不放行——字母/数字/下划线后缀都要让内部的 schema 黑话仍被抓到。
    assert "schema" in scan_src(tmp_path, 'const k = "图谱 Schemas 已更新";')
    assert "schema" in scan_src(tmp_path, 'const l = "图谱 Schema2 上线";')
    assert "schema" in scan_src(tmp_path, 'const m = "图谱 Schema_v2 已更新";')


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
# 4. raw enum fallback:「兜底即原值」—— 已搬去前端,这里只钉住「搬走了,没丢」
#
# 第二轮 review 阻塞 3:正则版同时漏报(`M?.[x] ?? x`、`label(M, x, x)`、
# `f()[x] ?? x`)和误报(`ALIASES[v] ?? v` 这种纯内部归一化)。误报补不掉——它与
# 真正的渲染查表**语法形状完全一致**,只有上下文能区分,而正则没有上下文。改用
# TypeScript AST 后,实现只能落在 JS 侧,故整体搬去
# frontend/tests/guards/raw-enum-fallback.test.mjs(npm run test 递归收集 → 仍是
# scripts/check.sh 的硬门)。正反样例连同那五条探针都在那边。
# --------------------------------------------------------------------------


def test_兜底即原值检查已搬去前端且仍挂在硬门上():
    """搬家最容易出的事故是「搬走了但没接上」——两边都不跑,还都是绿的。"""
    mjs = (
        _ROOT / "frontend" / "tests" / "guards"
        / "raw-enum-fallback.test.mjs"
    )
    assert mjs.exists(), f"{mjs} 不见了:兜底即原值检查搬走后没落地"
    # npm run test 组合纯逻辑与组件交互两条 lane；raw-enum guard 属于前者。
    pkg = json.loads(
        (_ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    assert "find tests/unit tests/guards" in pkg["scripts"]["test:node"], (
        "前端测试收集方式变了,新守卫可能没被 check.sh 跑到"
    )
    assert "test:node" in pkg["scripts"]["test"]
    assert "npm run test" in (
        _ROOT / "scripts" / "check_frontend.sh"
    ).read_text(encoding="utf-8")


def test_python_守卫不再自带正则版兜底检查():
    """留着旧的等于留着那条已知误报,而且两套规则会打架。"""
    assert not hasattr(guard, "scan_raw_fallback")
    assert not hasattr(guard, "RAW_FALLBACK")


# --------------------------------------------------------------------------
# 5. 跨层:后端被标记为「可展示」的文案同样受词表约束
#
# 这一节是第二轮 review 阻塞 2 的守卫。上一轮守卫按**目录**划作用域(只扫
# frontend/app),而信任边界并不在目录上:`user_error()` 给 4xx 打了
# `X-User-Message: 1`,前端 errors.ts 就把 detail 原样上屏。于是
# 「仅管理员可设置基准库」「仅管理员可管理晋升队列」四条 403 一直在给用户看
# 黑名单词,守卫却全绿。作用域必须跟着信任边界走。
# --------------------------------------------------------------------------


def scan_py(tmp_path: pathlib.Path, code: str) -> list[str]:
    """把一段后端源码写成临时文件跑 user_error 扫描,返回命中的词名。"""
    path = tmp_path / "sample.py"
    path.write_text(code, encoding="utf-8")
    return [term for _line, term, _msg in guard.scan_user_error(path)[1]]


def user_error_sites(tmp_path: pathlib.Path, code: str) -> int:
    path = tmp_path / "sites.py"
    path.write_text(code, encoding="utf-8")
    return guard.scan_user_error(path)[0]


@pytest.mark.parametrize(
    "code",
    [
        # 真实回归样本:这四条就是漏出去的那批
        'raise user_error(403, "仅管理员可设置基准库")',
        'raise user_error(403, "仅管理员可管理晋升队列")',
        # 关键字实参写法
        'raise user_error(status_code=400, message="重建投影失败")',
        # 表挂在模块上:deps.user_error(...) 同样是标记
        'raise deps.user_error(400, "共 3 个 chunk 未入图")',
        # f-string:字面量部分照查,插值剥掉
        'raise user_error(400, f"{n} 个来源待抽取")',
        # 跨行隐式拼接(Python 解析阶段就并成一个字面量)
        'raise user_error(400, "本次从基准库"\n                  "读取失败")',
    ],
)
def test_后端可展示文案里的黑话会被抓到(tmp_path, code):
    assert scan_py(tmp_path, code), f"没抓到后端 user_error 里的黑话:{code}"


def test_f_string_插值不当渲染文本(tmp_path):
    """插值里的标识符是代码,不是文案——与前端 `${…}` 同一规则。"""
    assert scan_py(tmp_path, 'raise user_error(400, f"共 {chunk_count} 段")') == []


def test_图谱Schema放行对后端user_error也生效(tmp_path):
    """codex 第 2 轮 P2:「图谱 Schema」是全局界面词,守卫按信任边界(而非目录)划范围——
    后端 user_error() 的可展示文案与前端渲染文本同受词表约束,故放行也必须对称:

      * 后端 user_error 里的「图谱 Schema」放行(不因含 schema 被拦);
      * 但后端裸 schema 仍照抓(放行是收窄的,不是把后端 schema 规则关掉)。

    没有这条,前端放行、后端却拦「图谱 Schema」= 契约自相矛盾,合法后端文案过不了 CI。"""
    assert scan_py(tmp_path, 'raise user_error(400, "图谱 Schema 已更新")') == []
    assert "schema" in scan_py(tmp_path, 'raise user_error(400, "schema 版本回滚")')


@pytest.mark.parametrize(
    "code",
    [
        # 裸 HTTPException 不在扫描面内:它永远不上屏(前端按状态码给通用文案),
        # detail 是给日志 / MCP 的诊断契约。这条反例钉住这个**刻意的**边界。
        'raise HTTPException(status_code=400, detail="从基准库抽取 chunk 失败")',
        # 变量 / 调用做 message:静态查不了,放行而非瞎猜(auth_routes.py 那处),
        # 由 tests/test_user_error.py 的运行时用例覆盖。
        'detail = "基准库不可用"\nraise user_error(400, detail)',
        'raise user_error(400, _msg("基准库"))',
        # 位置不对的字符串(status_code 位)不是 message
        'raise user_error("基准库", 400)',
        # 同名但不是标记函数的调用
        'log.user_error_count("基准库抽取")',
    ],
)
def test_不在信任边界上的后端字符串不误报(tmp_path, code):
    assert scan_py(tmp_path, code) == [], f"误报:{code}"


def test_扫描面塌了要红而不是静默放行(tmp_path, monkeypatch, capsys):
    """helper 改名 / 换调用写法 → 匹配数掉到 0,而退出码仍是 0,正是本轮要根治的
    那类假绿。非空性下限把它变成硬失败。"""
    (tmp_path / "empty.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "page.tsx").write_text(
        "export default function Page() { return <p>公共知识库</p> }\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "FRONTEND_PRODUCTION_DIRS", (tmp_path,))
    monkeypatch.setattr(guard, "BACKEND_APP", tmp_path)
    assert guard.main() == 1
    assert "call sites found" in capsys.readouterr().err


@pytest.mark.architecture_contract
def test_真实后端确有可展示文案站点(tmp_path):
    """非空性:守卫真的在后端 app 树上找到了标记站点(不是扫了个空目录)。"""
    total = sum(
        guard.scan_user_error(path)[0] for path in guard.BACKEND_APP.rglob("*.py")
    )
    assert total >= guard.MIN_USER_ERROR_SITES, (
        f"只找到 {total} 处 {guard.USER_ERROR}() —— 扫描面塌了"
    )


def test_后端真实文案确实曾经违规过(tmp_path):
    """变异验证:把 notebook_routes.py 的一条 403 改回「基准库」,守卫必须红。

    没有这条,「守卫扫了后端」和「守卫扫后端但规则对后端不生效」在绿灯下无法区分。
    """
    routes = guard.BACKEND_APP / "api" / "notebook_routes.py"
    src = routes.read_text(encoding="utf-8")
    assert "仅管理员可设为公共知识库" in src, "文案又漂了,本变异样本需同步"
    mutated = tmp_path / "routes_mutated.py"
    mutated.write_text(
        src.replace("仅管理员可设为公共知识库", "仅管理员可设置基准库"), encoding="utf-8"
    )
    hits = [term for _line, term, _msg in guard.scan_user_error(mutated)[1]]
    assert "基准库" in hits, "改回黑名单词后守卫仍不报——规则没作用到后端"


# --------------------------------------------------------------------------
# 6. 端到端归属:真实源码扫描只在 contracts lane 执行一次
# --------------------------------------------------------------------------


@pytest.mark.architecture_contract
def test_真实源码守卫接在_contracts_lane():
    contracts = (_ROOT / "scripts" / "check_contracts.sh").read_text(
        encoding="utf-8"
    )
    invocation = (
        '"$ROOT_DIR/scripts/check_ui_vocabulary.py" \\\n'
        '  --extra-root "$ROOT_DIR/examples/extensions/arxiv-search/src"'
    )
    assert contracts.count(invocation) == 1
