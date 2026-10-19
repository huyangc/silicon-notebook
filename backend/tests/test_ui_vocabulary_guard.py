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
from types import SimpleNamespace

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
    "promotion": "来源类型的内部枚举值(PR-E8),前端经 sourceTypeTag 映射为「收录」;裸英文词在"
    "标识符、协议与 CSS 类名里大量合法出现,词级 lint 判不了。界面兜底由 raw-enum-fallback 守卫负责",
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
    # The vocabulary reference owns guard implementation details; product
    # references route readers there instead of maintaining another scope list.
    vocabulary = (_ROOT / "docs/ui-vocabulary.md").read_text(encoding="utf-8")
    assert "frontend/app" in vocabulary
    assert "frontend/features" in vocabulary


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
        # 静态查不了的 message 放行而非瞎猜,由 tests/test_user_error.py 的运行时
        # 用例覆盖:未在本模块定义的 helper、异常属性、str(exc)、跨模块导入的常量。
        'raise user_error(400, _msg("基准库"))',
        'raise user_error(400, exc.user_message)',
        'raise user_error(400, str(exc))',
        'from app.messages import BASE_MSG\nraise user_error(400, BASE_MSG)',
        # 形参遮蔽同名模块常量(Python 作用域):形参的值静态未知,不拿模块常量顶替。
        'MSG = "基准库不可用"\ndef f(MSG):\n    raise user_error(400, MSG)',
        # 条件表达式两个分支各算一个单元,不跨分支拼出「社区」。
        'raise user_error(400, "社" if c else "区")',
        # 拼接中解析不了的一段当插值(空白),两侧字面量不会被拼成「社区」。
        'raise user_error(400, "社" + x + "区")',
        # 作用域遮蔽:for 循环变量、lambda 形参、except … as、推导式变量、函数内 import
        # 都是局部名,值静态未知,不拿同名模块常量顶替。
        'MSG = "基准库不可用"\ndef f(xs):\n    for MSG in xs:\n        raise user_error(400, MSG)',
        'def f():\n    msg = "基准库"\n    g = lambda msg: user_error(400, msg)',
        'exc = "基准库"\ndef f():\n    try:\n        pass\n    except Exception as exc:\n'
        '        raise user_error(400, exc)',
        'MSG = "基准库"\nerrs = [user_error(400, MSG) for MSG in xs]',
        'MSG = "基准库"\ndef f():\n    from app.messages import MSG\n    raise user_error(400, MSG)',
        # 参数遮蔽同名模块 helper:调用的是传入的可调用对象,不跟进模块 helper 的 return
        'def message():\n    return "基准库"\n'
        'def route(message):\n    raise user_error(400, message())',
        # 嵌套 / 星号解包里的名字仍是绑定,遮蔽同名模块常量
        'MSG = "基准库"\ndef f(x):\n    (a, [MSG, b]), *c = x\n    raise user_error(400, MSG)',
        'MSG = "基准库"\ndef f(x):\n    a, *MSG = x\n    raise user_error(400, MSG)',
        # 局部可调用对象同理
        'def message():\n    return "基准库"\n'
        'def route(factory):\n    message = factory\n    raise user_error(400, message())',
        # 位置不对的字符串(status_code 位)不是 message
        'raise user_error("基准库", 400)',
        # 同名但不是标记函数的调用
        'log.user_error_count("基准库抽取")',
    ],
)
def test_不在信任边界上的后端字符串不误报(tmp_path, code):
    assert scan_py(tmp_path, code) == [], f"误报:{code}"


@pytest.mark.parametrize(
    "code",
    [
        # 变量:先查同函数内的赋值
        'def f(c):\n    msg = "仅管理员可设置基准库"\n    raise user_error(403, msg)',
        # 变量:再查模块级常量
        'MSG = "仅管理员可设置基准库"\ndef f():\n    raise user_error(403, MSG)',
        # 条件表达式:任一分支违规都报
        'raise user_error(400, "保存失败" if c else f"{n} 个来源待抽取")',
        # `+` 拼接:两侧并成一个单元
        'raise user_error(400, "本次从" + f"基准库{x}读取失败")',
        # 拼接的一侧静态未知:当作插值(空白),另一侧照查
        'raise user_error(400, str(exc) + "(基准库)")',
        # `%` 与 `.format`
        'raise user_error(400, "共 %d 个 chunk" % n)',
        'raise user_error(400, "共 {} 个 chunk".format(n))',
        # `X.get(k, 默认)`:字典值与默认值都查
        '_MSGS = {"a": "基准库不可用"}\nraise user_error(400, _MSGS.get(k, "稍后再试"))',
        '_MSGS = {"a": "稍后再试"}\nraise user_error(400, _MSGS.get(k, "基准库不可用"))',
        # 本模块 helper 的每一个 return
        'def _msg(n):\n    if n:\n        return "稍后再试"\n    return f"{n} 个 chunk"\n'
        'raise user_error(400, _msg(n))',
        # helper 里再经一层模块常量
        'BASE = "基准库"\ndef _msg():\n    return BASE + "不可用"\nraise user_error(400, _msg())',
        # 下标 / 属性赋值读取接收者而不绑定它:模块常量在函数里仍可解析
        'MESSAGES = {"bad": "基准库不可用"}\ndef f():\n    MESSAGES["ok"] = "完成"\n'
        '    raise user_error(400, MESSAGES.get("bad", "失败"))',
        'MSG = "基准库不可用"\ndef f(obj):\n    MSG.attr = 1\n    raise user_error(400, MSG)',
        # 长拼接链:最左边的前缀不因链长被丢掉(结构节点不扣深度)
        'raise user_error(400, "基准库 " + a + "，" + b + "，" + c + "。")',
        'raise user_error(400, "基准库 " + a + "，" + b + "，" + c + "，" + a + "，" + b + "。")',
        # 长拼接链 + 变量中转
        'def f(a, b):\n    m = "基准库：" + a + "，" + b + "，" + a + "。"\n'
        '    raise user_error(400, m)',
        # 长拼接链 + helper 中转 + 变量中转
        'def _h(a, b):\n    return "基准库 " + a + " / " + b + " / " + a + "。"\n'
        'def f():\n    m = _h(1, 2)\n    raise user_error(400, m)',
        # `+=` 追加的文案同样是候选
        'def f():\n    msg = "保存"\n    msg += "基准库"\n    raise user_error(400, msg)',
        # 小组合跨 `+` 两侧拼出的黑话照报(积未超上限时两侧确实拼接)
        'raise user_error(400, ("基" if c else "x") + ("准库" if d else "y"))',
    ],
)
def test_后端可展示文案的间接写法也会被抓到(tmp_path, code):
    """「先赋给变量 / 条件表达式 / 拼接 / 模块常量 / 模块 helper 返回」这几种间接写法
    曾经整条被跳过——「横向对比:纳入 … 个同社区实体」就是这样上屏的。"""
    assert scan_py(tmp_path, code), f"没抓到间接写法里的黑话:{code}"


def test_间接写法解析有深度上限且不因自引用卡死(tmp_path):
    code = 'MSG = MSG + "基准库"\nraise user_error(400, MSG)'
    assert "基准库" in scan_py(tmp_path, code)
    deep = "\n".join(
        ['A0 = "基准库"', *(f"A{i} = A{i - 1}" for i in range(1, 12))]
    ) + "\nraise user_error(400, A11)"
    assert scan_py(tmp_path, deep) == [], "超过深度上限的链条应放弃解析而不是无限追"


def test_拼接组合超上限时退化为两侧各自检查而不是截断(tmp_path):
    """状态标签字典 + 后缀是现实形态:组合数超 MAX_UNITS_PER_EXPR 时不能静默丢掉后面
    的单元——退化为左右两侧各自检查,单侧黑话一条不漏。"""
    n = guard.MAX_UNITS_PER_EXPR + 6
    entries = ", ".join(f'"k{i}": "状态{i}"' for i in range(n - 1))
    code = (
        f'_L = {{{entries}, "last": "基准库"}}\n'
        'raise user_error(400, _L.get(k, "未知") + "失败")'
    )
    assert "基准库" in scan_py(tmp_path, code)
    # 后缀一侧的黑话同样不丢
    code_rhs = (
        f'_L = {{{entries}, "last": "正常"}}\n'
        'raise user_error(400, _L.get(k, "未知") + "（基准库）")'
    )
    assert "基准库" in scan_py(tmp_path, code_rhs)


def test_自引用链的解析代价是线性的(tmp_path, monkeypatch):
    """只有名字跳转扣深度以后,`x = x + "…"` 这种自引用若没有 (节点, 深度) 级 memo,
    调用次数按 赋值数^深度 增长。这里数 _compute 的调用次数钉住 memo。"""
    calls = 0
    real = guard._IndirectCopy._compute

    def counting(self, node, depth):
        nonlocal calls
        calls += 1
        return real(self, node, depth)

    monkeypatch.setattr(guard._IndirectCopy, "_compute", counting)
    code = 'MSG = "基准库"\n' + 'MSG = MSG + "。"\n' * 12 + "raise user_error(400, MSG)"
    assert "基准库" in scan_py(tmp_path, code)
    assert calls < 2000, f"自引用解析调用了 {calls} 次——memo 失效了"


def test_环境变量名在后端通道里不当黑话但裸词照抓(tmp_path):
    """`SILICON_NOTEBOOK_AUTH_OPTIONAL` 是配置键名(admin_routes 统一登录开关的拒绝文案),
    不是界面黑话;剥离只吃**已登记**的部署配置键(从 config.py 推出),裸 notebook / KG、
    以及长得像环境变量的内部码照抓。"""
    assert scan_py(
        tmp_path, 'raise user_error(409, "请先关闭 SILICON_NOTEBOOK_AUTH_OPTIONAL 再启用")'
    ) == []
    assert scan_trace(
        tmp_path,
        'TraceStep(step_type="skip", summary="已按 NOTEBOOK_QUESTION_SOURCE_LIMIT 截断")',
    ) == []
    # 形状像环境变量、但部署并不读的内部码:不放行。
    assert "KG" in scan_py(tmp_path, 'raise user_error(409, "失败:KG_NOT_READY")')
    assert "Memory" in scan_trace(
        tmp_path, 'TraceStep(step_type="skip", summary="超过 MEMORY_LIMIT")'
    )
    assert "notebook" in scan_py(tmp_path, 'raise user_error(409, "请先打开 notebook")')
    assert "notebook" in scan_py(
        tmp_path, 'raise user_error(409, "请检查 Silicon_Notebook_Auth 设置")'
    )
    assert "KG" in scan_trace(tmp_path, 'TraceStep(step_type="skip", summary="KG 未就绪")')


def test_环境变量白名单从部署配置推出():
    registered = guard._registered_env_vars()
    assert "SILICON_NOTEBOOK_AUTH_OPTIONAL" in registered
    assert "AUTH_PUBLIC_BASE_URL" in registered
    assert "KG_NOT_READY" not in registered and "MEMORY_LIMIT" not in registered


def test_环境变量名剥离只作用于后端两条通道(tmp_path):
    """剥离刻意不放进 terms_in():前端渲染文本没有这种写法,放宽只会少报。"""
    assert "notebook" in scan_src(tmp_path, 'const t = "请设置 SILICON_NOTEBOOK_AUTH";')


# --------------------------------------------------------------------------
# 4b. `guard.main()` 的自检用例:跑在一棵最小 fixture 树上,不扫真实仓库
# --------------------------------------------------------------------------
#
# 真实仓库的全通道扫描只在 contracts lane 的 `scripts/check_ui_vocabulary.py` 里
# 跑一次(`check_contracts.sh`,由 `test_真实源码守卫接在_contracts_lane` 钉住);这里的
# 变异用例要证明的是「某条通道的违规 / 扫描面塌了会让守卫红」,不需要每条再把
# 整个 backend/app 与 frontend 解析一遍(每次约 2.5 s)。
#
# 最小树把 `main()` 用到的每一个根都重定向到 tmp 目录,每条通道恰好有一个干净、
# 非空的扫描面,`MIN_*` 下限降到这棵树的实际站点数(下限仍然是「扫描面塌了」的哨兵)。
# `test_最小树上守卫全绿` 钉住基线;每条变异用例除了目标通道的报错文案,还要断言
# **只有目标通道**在报错,防止别的通道塌了而「碰巧变红」。

# 每条通道在 stderr 里留下的标记(违规行 + 扫描面塌了/缺文件的哨兵)。
_CHANNEL_MARKERS = {
    "frontend": ("in rendered text", "no frontend production sources"),
    "user_error": ("in user_error message", "call sites found in backend/app", "no backend/app sources"),
    "trace": ("in trace summary", "trace summaries found", "trace-summary module missing"),
    "display": ("in display field", "display fields found", "display-field module missing"),
    "workload": ("in workload label", "workload labels found in", "workload-label module missing"),
    "service": (
        "in model service label",
        "shipped model-service labels",
        "service-label source missing",
    ),
    "release": ("in release note",),
}


def _failed_channels(err: str) -> set[str]:
    return {
        channel
        for channel, markers in _CHANNEL_MARKERS.items()
        if any(marker in err for marker in markers)
    }


def _assert_only_channel(capsys, channel: str, expected: str) -> str:
    """守卫已返回非 0:目标通道的文案在,且没有任何别的通道在报错。"""
    err = capsys.readouterr().err
    assert expected in err, err
    assert _failed_channels(err) == {channel}, (
        f"应当只有 {channel} 通道失败,实际 {sorted(_failed_channels(err))}:\n{err}"
    )
    return err


def _rewrite(path: pathlib.Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"最小树的样本变了,变异需同步:{old!r} 不在 {path.name}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def _write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


_MIN_HANDLERS = (
    "from app.api.deps import user_error\n"
    "\n"
    "\n"
    "def pick(notebook):\n"
    '    if notebook is None:\n'
    '        raise user_error(400, "请先选择一个笔记本")\n'
    '    raise user_error(403, "请设置 SILICON_NOTEBOOK_AUTH_MODE 后重试")\n'
)


def _minimal_guard_tree(tmp_path: pathlib.Path, monkeypatch) -> SimpleNamespace:
    """把 `guard.main()` 的每个扫描根都重定向到一棵每条通道都非空且干净的最小树。

    布局镜像真实仓库(`backend/app/repositories/*/kg_build_job_store.py` 等),所以按
    相对路径认「作业存储模块」的那条逻辑也走得到。返回各文件路径,供变异用例改写。
    """
    frontend = tmp_path / "frontend" / "app"
    app = tmp_path / "backend" / "app"
    sqlite_store = app / "repositories" / "sqlite" / "kg_build_job_store.py"
    postgres_store = app / "repositories" / "postgres" / "kg_build_job_store.py"
    tree = SimpleNamespace(
        root=tmp_path,
        frontend=frontend,
        app=app,
        page=_write(
            frontend / "page.tsx",
            "export default function Page() { return <p>公共知识库</p> }\n",
        ),
        handlers=_write(app / "api" / "handlers.py", _MIN_HANDLERS),
        config=_write(
            app / "core" / "config.py",
            'import os\n\nAUTH_MODE = os.getenv("SILICON_NOTEBOOK_AUTH_MODE")\n',
        ),
        trace=_write(
            app / "services" / "trace.py",
            'def step():\n    return TraceStep(summary="已检索到相关段落")\n',
        ),
        indexing=_write(
            app / "services" / "indexing_pipeline.py",
            'IndexingPipelineOption(label="内建", description="按段落切分")\n',
        ),
        sqlite_store=_write(
            sqlite_store,
            'class Store:\n    def fail(self, job):\n'
            '        self.finish(job, "failed", error_message="分析失败")\n',
        ),
        postgres_store=_write(
            postgres_store,
            'class Store:\n    def fail(self, job):\n'
            '        self.finish(job, "failed", error_message="分析失败")\n',
        ),
        registry=_write(
            app / "services" / "model_registry.py",
            '_WORKLOAD_LABELS = {"a": "通用问答", "b": "文档整理"}\n',
        ),
        migrate=_write(
            tmp_path / "scripts" / "migrate_legacy_model_env.py",
            'ROLE_LABELS = {"general": "通用模型", "kg": "知识分析模型"}\n',
        ),
        template=_write(
            tmp_path / "model-services.example.toml",
            '[services.general]\ndisplay_name = "通用模型"\n\n'
            '[services.kg]\ndisplay_name = "知识分析模型"\n',
        ),
        notes=tmp_path / "release-notes",
    )
    # README 允许写黑话(写法约定),它在基线里就是「README 被排除」的证据。
    _write(tree.notes / "README.md", "写法约定里可以提到 chunk 与 projection。\n")
    _write(tree.notes / "ok.md", "现在可以把回答分享给同事了。\n")

    stores = (sqlite_store, postgres_store)
    for name, value in {
        "FRONTEND_PRODUCTION_DIRS": (frontend,),
        "BACKEND_APP": app,
        "DISPLAY_FIELD_ROOT": app,
        "DISPLAY_FIELD_ANCHOR_MODULES": (tree.indexing, *stores),
        "KG_BUILD_JOB_STORE_MODULES": stores,
        "_STORE_MODULES_RELATIVE": frozenset(p.relative_to(app) for p in stores),
        "TRACE_SUMMARY_MODULES": (tree.trace,),
        "SETTINGS_MODULE": tree.config,
        # 环境变量白名单是进程级缓存:清空,让它从最小树的 config 重新读(用例结束还原)。
        "_ENV_VAR_CACHE": None,
        "WORKLOAD_LABEL_MODULE": tree.registry,
        "SERVICE_LABEL_DICTS": ((tree.migrate, "ROLE_LABELS"),),
        "SERVICE_LABEL_TEMPLATE": tree.template,
        "RELEASE_NOTES_DIR": tree.notes,
        # 下限降到这棵树的实际站点数:2 个 user_error、1 条轨迹摘要、4 个展示字段
        # (索引管线 label+description,两个作业存储各 1 个 error_message)、2 个工作负载
        # 显示名、4 个出厂模型服务显示名(迁移脚本 2 + 模板 2)。
        "MIN_USER_ERROR_SITES": 2,
        "MIN_TRACE_SUMMARY_SITES": 1,
        "MIN_DISPLAY_FIELD_SITES": 4,
        "MIN_WORKLOAD_LABELS": 2,
        "MIN_SERVICE_LABELS": 4,
    }.items():
        monkeypatch.setattr(guard, name, value)
    return tree


def test_最小树上守卫全绿(tmp_path, monkeypatch, capsys):
    """基线:变异用例的「只有目标通道失败」只有在这里全绿时才有意义。"""
    _minimal_guard_tree(tmp_path, monkeypatch)
    assert guard.main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert (
        "scanned 1 frontend production files + 2 backend user_error() messages + "
        "1 trace summaries + 4 display fields + 2 workload labels + "
        "4 model service labels"
    ) in captured.out


@pytest.mark.parametrize(
    ("channel", "target", "old", "new", "expected"),
    [
        ("frontend", "page", "公共知识库", "基准库", "page.tsx:1: 「基准库」in rendered text"),
        ("user_error", "handlers", "请先选择一个笔记本", "重建投影失败", "handlers.py:6: 「投影」in user_error message"),
        ("trace", "trace", "已检索到相关段落", "已抽取 3 个 chunk", "trace.py:2: 「chunk」in trace summary"),
    ],
    ids=["frontend", "user_error", "trace"],
)
def test_前端_后端文案_轨迹通道的违规只让自己红(
    tmp_path, monkeypatch, capsys, channel, target, old, new, expected
):
    tree = _minimal_guard_tree(tmp_path, monkeypatch)
    _rewrite(getattr(tree, target), old, new)
    assert guard.main() == 1
    _assert_only_channel(capsys, channel, expected)


def test_轨迹摘要扫描面塌了要红(tmp_path, monkeypatch, capsys):
    _minimal_guard_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(guard, "TRACE_STEP", "RenamedTraceStep")
    assert guard.main() == 1
    _assert_only_channel(capsys, "trace", "trace summaries found")


def test_扫描面塌了要红而不是静默放行(tmp_path, monkeypatch, capsys):
    """helper 改名 / 换调用写法 → 匹配数掉到 0,而退出码仍是 0,正是本轮要根治的
    那类假绿。非空性下限把它变成硬失败。"""
    tree = _minimal_guard_tree(tmp_path, monkeypatch)
    tree.handlers.write_text("x = 1\n", encoding="utf-8")
    assert guard.main() == 1
    _assert_only_channel(capsys, "user_error", "call sites found")


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
# 5b. 第三条通道:服务端写、前端逐字渲染的推理轨迹摘要
#
# 这条通道补的是与「后端 user_error」同构的一个缺口:`TraceStep.summary` 与结束
# 原因短句表都由服务端写,前端 answer-panel.tsx 原样打印,没有任何前端映射——
# 它们是用户文案,却从不经过 frontend/app,于是「必答方面」这个服务端记账名一路
# 上屏两次(收尾 skip 步的 summary + 合成终步的 termination_summary),而守卫全绿。
# --------------------------------------------------------------------------


def scan_trace(tmp_path: pathlib.Path, code: str) -> list[str]:
    path = tmp_path / "trace_sample.py"
    path.write_text(code, encoding="utf-8")
    return [term for _line, term, _text in guard.scan_trace_summaries(path)[1]]


def trace_sites(tmp_path: pathlib.Path, code: str) -> int:
    path = tmp_path / "trace_sites.py"
    path.write_text(code, encoding="utf-8")
    return guard.scan_trace_summaries(path)[0]


@pytest.mark.parametrize(
    "code",
    [
        # 真实回归样本:接入时这两句就是这么写的。
        'record(TraceStep(step_type="skip", summary="跳过必答方面复核"))',
        # f-string:字面量部分照查,插值剥掉(同 user_error 规则)。
        'TraceStep(step_type="skip", summary=f"跳过 {n} 个必答方面")',
        # 真实回归样本(reasoning_retrieval 横向对比):条件表达式先赋给变量再传入。
        'def f(src, n):\n'
        '    x = (f"横向对比(共提):纳入 {n} 个同类实体" if src == "comention"\n'
        '         else f"横向对比:纳入 {n} 个同社区实体")\n'
        '    record(TraceStep(step_type="expand_community", summary=x))',
        # 模块级常量
        'SKIP = "跳过必答方面复核"\nTraceStep(step_type="skip", summary=SKIP)',
        # `+` 拼接
        'TraceStep(step_type="skip", summary="前缀" + f"{n} 个社区")',
        # 本模块 helper 的 return
        'def _summary(n):\n    return f"跳过 {n} 个必答方面"\n'
        'TraceStep(step_type="skip", summary=_summary(n))',
    ],
)
def test_轨迹摘要里的黑话会被抓到(tmp_path, code):
    assert scan_trace(tmp_path, code), f"没抓到轨迹摘要里的黑话:{code}"


@pytest.mark.parametrize(
    "code",
    [
        # 静态查不了的摘要放行而非瞎猜(同 user_error 的同类边界):未在本模块定义
        # 的 helper、下标取值、形参。
        'TraceStep(step_type="skip", summary=_summary("必答方面"))',
        '_T = {"a": "必答方面"}\nTraceStep(step_type="skip", summary=_T[k])',
        'def f(text):\n    TraceStep(step_type="skip", summary=text)',
        # summary 之外的 kwarg 不是上屏文案(detail 是诊断契约,不上屏)。
        'TraceStep(step_type="skip", detail={"reason": "必答方面"})',
        # 同名 kwarg 但不是 TraceStep 的调用
        'log.event(summary="必答方面已记账")',
        # 另一张同名 key 的表不在登记的表名里
        '_INTERNAL_NOTES = {"a": "必答方面"}',
    ],
)
def test_不在轨迹摘要扫描面上的字符串不误报(tmp_path, code):
    assert scan_trace(tmp_path, code) == [], f"误报:{code}"


def test_无图披露步文案改回构建会重新违规(tmp_path):
    """变异验证:这条文案曾是唯一的登记例外(逐字全串豁免)。例外撤掉之后,把
    `kg_unavailable` 披露步改回「尚未构建知识图谱」,守卫必须红——否则说明豁免
    又以别的形式留了下来。"""
    module = next(
        p for p in guard.TRACE_SUMMARY_MODULES if p.name == "reasoning_retrieval.py"
    )
    src = module.read_text(encoding="utf-8")
    assert "本笔记本尚未整理知识图谱,本轮只用原文检索与集合清单" in src, (
        "文案又漂了,本变异样本需同步")
    assert not hasattr(guard, "GRANDFATHERED_TRACE_SUMMARIES")
    mutated = tmp_path / "reasoning_retrieval_mutated.py"
    mutated.write_text(
        src.replace("尚未整理知识图谱,本轮", "尚未构建知识图谱,本轮"), encoding="utf-8"
    )
    hits = [term for _line, term, _text in guard.scan_trace_summaries(mutated)[1]]
    assert hits, "改回「构建知识图谱」后守卫应报红"


def test_间接写法的轨迹摘要计入站点数(tmp_path):
    """站点计数只增不减:能解析的间接写法同样计入非空性下限的分子。"""
    assert trace_sites(
        tmp_path, 'X = "已完成"\nTraceStep(step_type="skip", summary=X)'
    ) == 1
    assert trace_sites(
        tmp_path, 'def f(x):\n    TraceStep(step_type="skip", summary=x)'
    ) == 0


def test_真实轨迹文案确实曾经违规过(tmp_path):
    """变异验证:把 reasoning_retrieval 横向对比的社区回退文案改回「同社区实体」,
    守卫必须红——这条文案是条件表达式赋给变量再传 summary=,没有间接写法解析时
    它整条被跳过。"""
    module = next(
        p for p in guard.TRACE_SUMMARY_MODULES if p.name == "reasoning_retrieval.py"
    )
    src = module.read_text(encoding="utf-8")
    assert "个同主题板块实体,新增候选" in src, "文案又漂了,本变异样本需同步"
    assert guard.scan_trace_summaries(module)[1] == []
    mutated = tmp_path / "reasoning_retrieval_mutated.py"
    mutated.write_text(
        src.replace("个同主题板块实体,新增候选", "个同社区实体,新增候选"), encoding="utf-8"
    )
    hits = [term for _line, term, _text in guard.scan_trace_summaries(mutated)[1]]
    assert hits == ["社区"], f"改回「同社区实体」后守卫应只报这一条:{hits}"


@pytest.mark.architecture_contract
def test_真实轨迹摘要扫描面非空且登记的模块都在(tmp_path):
    """非空性 + 登记表不许指向已经不存在的模块。"""
    total = 0
    for path in guard.TRACE_SUMMARY_MODULES:
        assert path.exists(), f"TRACE_SUMMARY_MODULES 指向了不存在的模块:{path}"
        total += guard.scan_trace_summaries(path)[0]
    assert total >= guard.MIN_TRACE_SUMMARY_SITES, (
        f"只找到 {total} 条字面量轨迹摘要 —— 扫描面塌了"
    )




# --------------------------------------------------------------------------
# 5c. 第五条通道:登记的后端展示字段(索引管线选项、知识图谱分析作业的失败文案)
# --------------------------------------------------------------------------


def scan_fields(tmp_path: pathlib.Path, code: str, *, store_module: bool = False) -> list[str]:
    path = tmp_path / "fields_sample.py"
    path.write_text(code, encoding="utf-8")
    return [
        term for _line, term, _text in
        guard.scan_display_fields(path, store_module=store_module)[1]
    ]


@pytest.mark.parametrize(
    "code",
    [
        # 索引管线选项:label 与 description 都上屏
        'IndexingPipelineOption(pipeline_id="", label="内建", description="知识抽取策略")',
        'IndexingPipelineOption(label=f"{name} notebook 管线", description="")',
        # 作业失败文案:模块常量
        'MSG = "知识抽取未通过校验"\n'
        'self.kg_build_jobs.finish(job_id, "failed", error_code="x", error_message=MSG)',
        # f-string;接收者是裸名字也认
        'kg_build_jobs.finish(job_id, "failed", error_message=f"{n} 个来源抽取失败")',
        # set_stage 写的是同一列
        'self._runtime.kg_build_jobs.set_stage(job_id, "stopping", error_message="建图中断")',
        # 接收者经一跳别名
        'def f(r):\n    jobs = r.kg_build_jobs\n'
        '    jobs.finish(job_id, "failed", error_message="知识抽取失败")',
        # 启动兜底的 SQL 字面量(相邻字符串在解析期就并成一个)
        '_write("UPDATE kg_build_jobs SET status=\'failed\', "\n'
        '       "error_message=\'服务重启导致本次抽取中断\', updated_at=? WHERE status=\'running\'")',
    ],
)
def test_展示字段里的黑话会被抓到(tmp_path, code):
    assert scan_fields(tmp_path, code), f"没抓到展示字段里的黑话:{code}"


@pytest.mark.parametrize(
    "code",
    [
        # 同名 kwarg 但接收者不是 kg_build_jobs:删库作业、来源表的 error_message 是不上屏的诊断原文
        'self.notebook_delete_jobs.finish(job_id, "failed", error_message="抽取失败")',
        'jobs.finish(job_id, "failed", error_message="抽取失败")',
        # 作业存储之外的 self.finish 不认
        'self.finish(job_id, "failed", error_message="抽取失败")',
        # 登记形状之外的 kwarg
        'self.kg_build_jobs.finish(job_id, "failed", error_code="抽取失败")',
        'IndexingPipelineOption(pipeline_id="抽取")',
        # 别名指向的不是 kg_build_jobs
        'def f(r):\n    jobs = r.notebook_delete_jobs\n'
        '    jobs.finish(job_id, "failed", error_message="抽取失败")',
        # 已知边界:**kwargs 展开静态解析不了,刻意跳过
        'self.kg_build_jobs.finish(job_id, "failed", **{"error_message": "抽取失败"})',
        # 别的表的 SQL 字面量、占位符写法都不是作业表的展示文案
        '_write("UPDATE sources SET error_message=\'抽取失败\' WHERE id=?")',
        # 语句里提到作业表,但被更新的是别的表
        '_write("UPDATE sources SET error_message=\'抽取失败\' "\n'
        '       "WHERE id IN (SELECT source_id FROM kg_build_jobs)")',
        '_write("UPDATE kg_build_jobs SET error_message=? WHERE id=?", ("抽取失败", 1))',
    ],
)
def test_不在展示字段登记表上的写入不误报(tmp_path, code):
    assert scan_fields(tmp_path, code) == [], f"误报:{code}"


def fields_sites(tmp_path: pathlib.Path, code: str) -> int:
    path = tmp_path / "fields_sites.py"
    path.write_text(code, encoding="utf-8")
    return guard.scan_display_fields(path)[0]


def test_展示字段只计可解析站点(tmp_path):
    """同轨迹通道:非空性下限的分子只计能解析出文案的站点。"""
    assert fields_sites(
        tmp_path, 'self.kg_build_jobs.finish(j, "failed", error_message="分析已中断")'
    ) == 1
    assert fields_sites(
        tmp_path, 'self.kg_build_jobs.finish(j, "failed", error_message=str(exc))'
    ) == 0


def test_作业存储模块内的_self_finish_也算展示字段(tmp_path):
    code = 'self.finish(job_id, "failed", error_message="分析任务抽取失败")'
    assert scan_fields(tmp_path, code) == []
    assert scan_fields(tmp_path, code, store_module=True) == ["抽取"]


def test_真实展示字段确实曾经违规过(tmp_path):
    """变异验证:把 knowledge_lifecycle 的作业失败文案改回「知识抽取」,守卫必须红。"""
    module = next(
        p for p in guard.DISPLAY_FIELD_ANCHOR_MODULES if p.name == "knowledge_lifecycle.py"
    )
    src = module.read_text(encoding="utf-8")
    assert "索引管线知识分析未通过校验" in src, "文案又漂了,本变异样本需同步"
    assert guard.scan_display_fields(module)[1] == []
    mutated = tmp_path / "knowledge_lifecycle_mutated.py"
    mutated.write_text(
        src.replace("索引管线知识分析未通过校验", "索引管线知识抽取未通过校验"),
        encoding="utf-8",
    )
    hits = [term for _line, term, _text in guard.scan_display_fields(mutated)[1]]
    assert hits == ["抽取"], f"改回「知识抽取」后守卫应只报这一条:{hits}"


@pytest.mark.architecture_contract
def test_真实展示字段扫描面非空且登记的模块都在():
    total = 0
    for path in guard.DISPLAY_FIELD_ANCHOR_MODULES:
        assert path.exists(), f"DISPLAY_FIELD_ANCHOR_MODULES 指向了不存在的模块:{path}"
    for path in guard.DISPLAY_FIELD_ROOT.rglob("*.py"):
        total += guard.scan_display_fields(
            path, store_module=path in guard.KG_BUILD_JOB_STORE_MODULES
        )[0]
    assert total >= guard.MIN_DISPLAY_FIELD_SITES, f"只找到 {total} 个展示字段站点"


@pytest.mark.parametrize("shared", [True, False])
def test_展示字段违规让守卫红_两种扫描路径(tmp_path, monkeypatch, capsys, shared):
    """默认 BACKEND_APP 与展示字段扫描面是同一棵树,在同一次遍历里扫;BACKEND_APP
    被重定向时单独扫。两条路径都必须把违规报出来。"""
    tree = _minimal_guard_tree(tmp_path, monkeypatch)
    if not shared:
        # 后端根指到另一棵干净的树(自带 user_error 站点,保持原下限),展示字段扫描面
        # 仍是最小树的 app:只有「单独再扫一遍」那条路径看得到下面的违规。
        other = tmp_path / "other_backend"
        _write(other / "handlers.py", _MIN_HANDLERS)
        monkeypatch.setattr(guard, "BACKEND_APP", other)
    _rewrite(tree.indexing, 'description="按段落切分"', 'description="知识抽取策略"')
    assert guard.main() == 1
    _assert_only_channel(
        capsys, "display", "indexing_pipeline.py:1: 「抽取」in display field"
    )


def test_展示字段在作业存储模块里的_self_finish_也被抓到(tmp_path, monkeypatch, capsys):
    tree = _minimal_guard_tree(tmp_path, monkeypatch)
    _rewrite(tree.sqlite_store, 'error_message="分析失败"', 'error_message="知识抽取失败"')
    assert guard.main() == 1
    _assert_only_channel(capsys, "display", "kg_build_job_store.py:3: 「抽取」in display field")


def test_展示字段扫描面塌了要红(tmp_path, monkeypatch, capsys):
    _minimal_guard_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(guard, "DISPLAY_FIELD_CALLS", {})
    monkeypatch.setattr(guard, "DISPLAY_FIELD_METHODS", {})
    monkeypatch.setattr(guard, "_DISPLAY_FIELD_TOKENS", ())
    assert guard.main() == 1
    _assert_only_channel(capsys, "display", "display fields found")


# --------------------------------------------------------------------------
# 第六条通道:模型工作负载显示名(model_registry._WORKLOAD_LABELS)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        '_WORKLOAD_LABELS = MappingProxyType({\n    "a": "问答回答",\n    "b": "知识抽取",\n})\n',
        '_WORKLOAD_LABELS = {\n    "a": "问答回答",\n    "b": "知识抽取",\n}\n',
        '_WORKLOAD_LABELS: dict = {\n    "a": "问答回答",\n    "b": "知识抽取",\n}\n',
    ],
)
def test_工作负载显示名里的黑话会被抓到(tmp_path, code):
    module = tmp_path / "model_registry.py"
    module.write_text(code, encoding="utf-8")
    sites, hits = guard.scan_workload_labels(module)
    assert sites == 2
    assert [(line, term) for line, term, _ in hits] == [(3, "抽取")]


def test_工作负载显示名之外的同文件字符串不扫(tmp_path):
    module = tmp_path / "model_registry.py"
    module.write_text(
        'OTHER = {"a": "知识抽取"}\n_WORKLOAD_LABELS = {"a": "问答回答"}\n',
        encoding="utf-8",
    )
    assert guard.scan_workload_labels(module) == (1, [])


def test_真实工作负载显示名确实曾经违规过(tmp_path):
    """变异验证:把「知识分析」「知识补充分析」改回旧名,守卫必须各报一条。"""
    src = guard.WORKLOAD_LABEL_MODULE.read_text(encoding="utf-8")
    assert '"kg_extract": "知识分析"' in src and '"kg_glean": "知识补充分析"' in src, (
        "显示名又漂了,本变异样本需同步"
    )
    sites, hits = guard.scan_workload_labels(guard.WORKLOAD_LABEL_MODULE)
    assert hits == [] and sites >= guard.MIN_WORKLOAD_LABELS
    mutated = tmp_path / "model_registry_mutated.py"
    mutated.write_text(
        src.replace('"kg_extract": "知识分析"', '"kg_extract": "知识抽取"').replace(
            '"kg_glean": "知识补充分析"', '"kg_glean": "知识补充抽取"'
        ),
        encoding="utf-8",
    )
    assert [term for _l, term, _t in guard.scan_workload_labels(mutated)[1]] == [
        "抽取",
        "抽取",
    ]


def test_工作负载显示名违规让守卫红(tmp_path, monkeypatch, capsys):
    tree = _minimal_guard_tree(tmp_path, monkeypatch)
    _rewrite(tree.registry, '"通用问答"', '"知识社区摘要"')
    assert guard.main() == 1
    _assert_only_channel(capsys, "workload", "model_registry.py:1: 「社区」in workload label")


def test_工作负载显示名扫描面塌了要红(tmp_path, monkeypatch, capsys):
    _minimal_guard_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(guard, "WORKLOAD_LABEL_NAME", "_RENAMED_LABELS")
    assert guard.main() == 1
    _assert_only_channel(capsys, "workload", "workload labels found")


def test_出厂模型服务显示名里的黑话会被抓到(tmp_path):
    script = tmp_path / "migrate.py"
    script.write_text(
        'ROLE_LABELS = {\n    "general": "通用模型",\n    "kg": "知识抽取模型",\n}\n',
        encoding="utf-8",
    )
    assert guard.scan_workload_labels(script, "ROLE_LABELS")[0] == 2
    assert [t for _l, t, _x in guard.scan_workload_labels(script, "ROLE_LABELS")[1]] == ["抽取"]
    template = tmp_path / "model-services.example.toml"
    template.write_text(
        '[services.general]\ndisplay_name = "通用模型"\n\n'
        '[services.kg]\ndisplay_name = "知识抽取模型"\nkind = "chat"\n',
        encoding="utf-8",
    )
    sites, hits = guard.scan_service_label_template(template)
    assert sites == 2
    assert [(line, term) for line, term, _ in hits] == [(4, "抽取")]  # 表头所在行


def test_真实出厂模型服务显示名确实曾经违规过(tmp_path):
    """变异验证:迁移脚本的 kg 角色名改回「知识抽取模型」,守卫必须报。"""
    (script, name), = guard.SERVICE_LABEL_DICTS
    src = script.read_text(encoding="utf-8")
    assert '"kg": "知识分析模型"' in src, "显示名又漂了,本变异样本需同步"
    assert guard.scan_workload_labels(script, name)[1] == []
    assert guard.scan_service_label_template(guard.SERVICE_LABEL_TEMPLATE)[1] == []
    mutated = tmp_path / "migrate_mutated.py"
    mutated.write_text(
        src.replace('"kg": "知识分析模型"', '"kg": "知识抽取模型"'), encoding="utf-8"
    )
    assert [t for _l, t, _x in guard.scan_workload_labels(mutated, name)[1]] == ["抽取"]


def test_出厂模型服务显示名违规让守卫红(tmp_path, monkeypatch, capsys):
    tree = _minimal_guard_tree(tmp_path, monkeypatch)
    _rewrite(tree.template, 'display_name = "通用模型"', 'display_name = "知识社区模型"')
    assert guard.main() == 1
    _assert_only_channel(
        capsys, "service", "model-services.example.toml:1: 「社区」in model service label"
    )


def test_出厂模型服务显示名迁移脚本违规让守卫红(tmp_path, monkeypatch, capsys):
    tree = _minimal_guard_tree(tmp_path, monkeypatch)
    _rewrite(tree.migrate, '"知识分析模型"', '"知识社区模型"')
    assert guard.main() == 1
    _assert_only_channel(
        capsys, "service", "migrate_legacy_model_env.py:1: 「社区」in model service label"
    )


def test_出厂模型服务显示名扫描面塌了要红(tmp_path, monkeypatch, capsys):
    tree = _minimal_guard_tree(tmp_path, monkeypatch)
    tree.template.write_text("[bindings]\n", encoding="utf-8")
    monkeypatch.setattr(guard, "SERVICE_LABEL_DICTS", ())
    assert guard.main() == 1
    _assert_only_channel(capsys, "service", "shipped model-service labels")


# 评审 P2-2 的三种漏报(V2 拼接、V3 模块常量、V4 行尾注释)与「解析不了就报」。


def test_显示名拼接写法也会被抓到_V2(tmp_path):
    module = tmp_path / "model_registry.py"
    module.write_text(
        '_WORKLOAD_LABELS = MappingProxyType({\n'
        '    "a": "问答回答",\n'
        '    "kg_extract": "知识" + "抽取",\n'
        '})\n',
        encoding="utf-8",
    )
    sites, hits = guard.scan_workload_labels(module)
    assert sites == 2
    assert [(line, term) for line, term, _ in hits] == [(3, "抽取")]


def test_显示名经模块常量引用也会被抓到_V3(tmp_path):
    module = tmp_path / "model_registry.py"
    module.write_text(
        '_KG_EXTRACT_LABEL = "知识抽取"\n'
        '_PREFIX = "知识"\n'
        '_WORKLOAD_LABELS = {\n'
        '    "kg_extract": _KG_EXTRACT_LABEL,\n'
        '    "kg_glean": _PREFIX + "补充抽取",\n'
        '}\n',
        encoding="utf-8",
    )
    sites, hits = guard.scan_workload_labels(module)
    assert sites == 2
    assert [(line, term) for line, term, _ in hits] == [(4, "抽取"), (5, "抽取")]


def test_显示名解析不了就报而不是静默跳过(tmp_path):
    module = tmp_path / "model_registry.py"
    module.write_text(
        'from elsewhere import LABEL\n'
        '_WORKLOAD_LABELS = {\n'
        '    "a": LABEL,\n'
        '    "b": "知识" + LABEL,\n'
        '    "c": f"知识{LABEL}",\n'
        '    "d": label_for("d"),\n'
        '    **OTHER,\n'
        '    "e": "问答回答",\n'
        '}\n',
        encoding="utf-8",
    )
    sites, hits = guard.scan_workload_labels(module)
    assert sites == 6
    assert [(line, term) for line, term, _ in hits] == [
        (3, guard.UNRESOLVED_LABEL),
        (4, guard.UNRESOLVED_LABEL),
        (5, guard.UNRESOLVED_LABEL),
        (6, guard.UNRESOLVED_LABEL),
        (7, guard.UNRESOLVED_LABEL),
    ]


def test_模板显示名带行尾注释也会被抓到_V4(tmp_path):
    template = tmp_path / "model-services.example.toml"
    template.write_text(
        '[services.general]\ndisplay_name = "通用模型"\n\n'
        '[services.kg]\ndisplay_name = "知识抽取模型"  # kg\nkind = "chat"\n\n'
        '[services.rerank]\nkind = "rerank"\n',
        encoding="utf-8",
    )
    sites, hits = guard.scan_service_label_template(template)
    assert sites == 3  # 站点数 = 服务个数
    assert [(line, term) for line, term, _ in hits] == [
        (4, "抽取"),
        (8, guard.UNRESOLVED_LABEL),  # 缺 display_name 的服务也报
    ]


def test_真实模板站点数等于服务个数():
    import tomllib

    template = guard.SERVICE_LABEL_TEMPLATE
    services = tomllib.loads(template.read_text(encoding="utf-8"))["services"]
    assert guard.scan_service_label_template(template) == (len(services), [])


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


# --------------------------------------------------------------------------
# 5. 第四条通道:release-notes/*.md(打包清单原样展示给用户)
# --------------------------------------------------------------------------


def test_更新说明里的黑话会被抓到且README除外(tmp_path, monkeypatch, capsys):
    tree = _minimal_guard_tree(tmp_path, monkeypatch)
    notes = tree.notes
    (notes / "good.md").write_text("现在可以把回答分享给同事了。\n", encoding="utf-8")
    (notes / "bad.md").write_text("第一行正常。\n新增了 3 个 chunk 的重建。\n", encoding="utf-8")

    assert [p.name for p in guard._release_note_files()] == ["bad.md", "good.md", "ok.md"]
    assert [(line, term) for line, term, _ in guard.scan_release_note(notes / "bad.md")] == [(2, "chunk")]
    assert guard.scan_release_note(notes / "good.md") == []

    assert guard.main() == 1
    err = _assert_only_channel(capsys, "release", "bad.md:2")
    assert "in release note" in err
    assert "good.md" not in err and "ok.md" not in err and "README.md" not in err


def test_更新说明干净时守卫通过(tmp_path, monkeypatch, capsys):
    tree = _minimal_guard_tree(tmp_path, monkeypatch)
    (tree.notes / "extra.md").write_text("现在可以把回答分享给同事了。\n", encoding="utf-8")
    assert guard.main() == 0
    assert capsys.readouterr().err == ""


def test_更新说明文件头的纯英文键值行不触发而中文标题会被扫到(tmp_path, monkeypatch):
    notes = tmp_path / "release-notes"
    notes.mkdir()
    (notes / "h.md").write_text(
        "---\nlevel: feature\naudience: admin\ntitle: 新增 chunk 的重建\n---\n正文正常。\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "RELEASE_NOTES_DIR", notes)
    # 只有含中文的 title 行(第 4 行)被扫;level/audience 这类 ASCII 行不会。
    assert [(line, term) for line, term, _ in guard.scan_release_note(notes / "h.md")] == [(4, "chunk")]
