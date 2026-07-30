"""check_enumeration_list_labels_contract.py 的负向/正向测试。

镜像 test_object_type_labels_guard.py 的做法:不重新实现守卫的解析逻辑,一律用 tmp_path
造临时 answer-panel.tsx 替身,交给守卫的**真实函数**(frontend_labels() / main())去消费,
断言抛 GuardError 或退出码非零——测试如果自己重写一份解析器,就只证明了那份复制品会失败,
证明不了硬门会失败。

覆盖的场景(对应今天人工跑过的变异 + review 要求补的假绿路径):
  1. 改名 MISMATCH(值不满足「后端值+清单」)——main() 返回 1,frontend_labels() 不报错
  2. 缺声明                      → GuardError
  3. 双声明                      → GuardError(无法判定哪份生效)
  4. spread(...base)            → GuardError(无法零求值判定)
  5. computed key([k]: v)       → GuardError
  6. 重复键                      → GuardError(dict() 会静默折叠,显式拒绝)
  7. 模板串(`公式${x}`)          → GuardError
  8. 注释剥离顺序:正确声明藏在 /* */ 里,后面跟错误的真实声明 → 必须消费后者
  9. 两表撞名(跨元素/知识对象重名)→ main() 返回 1(uniqueness 断言,非 GuardError)
"""
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "enum_label_guard",
    ROOT / "scripts" / "check_enumeration_list_labels_contract.py",
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

ELEMENT_CANONICAL_BODY = """\
  formula: "公式清单",
  table: "表格清单",
  image: "图片清单",
  code_block: "代码块清单",
"""

KG_OBJECT_CANONICAL_BODY = """\
  concept: "概念知识对象清单",
  claim: "论断知识对象清单",
  formula: "公式知识对象清单",
  procedure: "过程知识对象清单",
"""

SOURCE_CANONICAL_BODY = """\
  sources: "来源清单",
"""


def element_declaration(body: str = ELEMENT_CANONICAL_BODY) -> str:
    return (
        "const ELEMENT_KIND_LIST_LABELS: Record<string, string> = {\n"
        + body + "};"
    )


def kg_object_declaration(body: str = KG_OBJECT_CANONICAL_BODY) -> str:
    return (
        "const KG_OBJECT_LIST_LABELS: Record<string, string> = {\n"
        + body + "};"
    )


def source_declaration(body: str = SOURCE_CANONICAL_BODY) -> str:
    return (
        "const SOURCE_LIST_LABELS: Record<string, string> = {\n"
        + body + "};"
    )


def write_tsx(
    tmp_path: pathlib.Path,
    element_decl: str,
    kg_decl: str,
    *,
    prelude: str = "",
    source_decl: str | None = None,
) -> pathlib.Path:
    """造一份最小 answer-panel.tsx 替身,只放守卫关心的三个声明。"""
    path = tmp_path / "answer-panel.tsx"
    path.write_text(
        "export const TRUNCATED_REASON_LABELS: Record<string, string> = {\n"
        '  budget: "已达本轮枚举上限",\n'
        "};\n\n"
        f"{prelude}"
        f"{element_decl}\n\n"
        f"{kg_decl}\n\n"
        f"{source_declaration() if source_decl is None else source_decl}\n\n"
        "function collectionResultTitle() { return null; }\n",
        encoding="utf-8",
    )
    return path


EXPECTED_ELEMENT_LABELS = {
    "formula": "公式清单",
    "table": "表格清单",
    "image": "图片清单",
    "code_block": "代码块清单",
}
EXPECTED_KG_OBJECT_LABELS = {
    "concept": "概念知识对象清单",
    "claim": "论断知识对象清单",
    "formula": "公式知识对象清单",
    "procedure": "过程知识对象清单",
}
EXPECTED_SOURCE_LABELS = {"sources": "来源清单"}


# --- 正向控制组:守卫不是「永远失败」--------------------------------------------


def test_canonical_fixture_passes_the_real_guard(tmp_path):
    """positive control——否则下面每条负向断言都可能只是因为守卫恒失败。"""
    path = write_tsx(tmp_path, element_declaration(), kg_object_declaration())
    assert guard.frontend_labels(guard.FRONTEND_ELEMENT_CONST, path) == EXPECTED_ELEMENT_LABELS
    assert guard.frontend_labels(guard.FRONTEND_KG_OBJECT_CONST, path) == EXPECTED_KG_OBJECT_LABELS
    assert guard.frontend_labels(guard.FRONTEND_SOURCE_CONST, path) == EXPECTED_SOURCE_LABELS
    assert guard.main(path) == 0


def test_live_frontend_labels_match_backend():
    """live 前后端一致(守卫默认路径,无参数,answer-panel.tsx + reasoning_retrieval.py)。"""
    assert guard.main() == 0


# --- 1. 改名 MISMATCH(值不满足「后端值+清单」) ------------------------------------


def test_renamed_frontend_value_is_a_mismatch_not_a_guard_error(tmp_path):
    """改名场景:解析本身合法(没有触发 GuardError),但值与「后端值+清单」不符。

    这正是今天人工做过的变异(backend 「表格」→「表」,或此处反过来在前端改名):
    main() 必须报非零退出,而不是被 frontend_labels() 的严格解析吞掉或放行。
    """
    bad_body = (
        '  formula: "公式清单",\n'
        '  table: "表清单",\n'  # 应为 "表格清单"
        '  image: "图片清单",\n'
        '  code_block: "代码块清单",\n'
    )
    path = write_tsx(tmp_path, element_declaration(bad_body), kg_object_declaration())
    # 解析成功——它是一份合法的对象字面量,只是值不对。
    assert guard.frontend_labels(guard.FRONTEND_ELEMENT_CONST, path)["table"] == "表清单"
    assert guard.main(path) == 1


# --- 2. 缺声明 -------------------------------------------------------------------


def test_missing_declaration_is_rejected(tmp_path):
    path = tmp_path / "answer-panel.tsx"
    path.write_text(kg_object_declaration() + "\n", encoding="utf-8")
    with pytest.raises(guard.GuardError):
        guard.frontend_labels(guard.FRONTEND_ELEMENT_CONST, path)
    assert guard.main(path) == 1


# --- 3. 双声明 -------------------------------------------------------------------


def test_multiple_declarations_of_the_same_name_are_rejected(tmp_path):
    second = element_declaration(
        '  formula: "Formula",\n'
        '  table: "Table",\n'
        '  image: "Image",\n'
        '  code_block: "Code",\n'
    )
    path = write_tsx(
        tmp_path,
        element_declaration() + "\n\n" + second,
        kg_object_declaration(),
    )
    with pytest.raises(guard.GuardError):
        guard.frontend_labels(guard.FRONTEND_ELEMENT_CONST, path)
    assert guard.main(path) == 1


# --- 4/5/7. 守卫无法零求值判定的写法一律硬失败 ------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("  ...BASE_LABELS,\n" + ELEMENT_CANONICAL_BODY, id="spread"),
        pytest.param(
            ELEMENT_CANONICAL_BODY + "  [customKind]: \"自定义\",\n",
            id="computed-key",
        ),
        pytest.param(
            '  formula: `公式${SUFFIX}`,\n'
            '  table: "表格清单",\n'
            '  image: "图片清单",\n'
            '  code_block: "代码块清单",\n',
            id="template-literal-value",
        ),
        pytest.param(
            '  formula,\n'
            '  table: "表格清单",\n'
            '  image: "图片清单",\n'
            '  code_block: "代码块清单",\n',
            id="shorthand",
        ),
        pytest.param(
            '  formula: FORMULA_LABEL,\n'
            '  table: "表格清单",\n'
            '  image: "图片清单",\n'
            '  code_block: "代码块清单",\n',
            id="identifier-value",
        ),
    ],
)
def test_non_canonical_entries_are_rejected_not_silently_skipped(tmp_path, body):
    """严格消费:这些写法下运行时对象可能已经变了,守卫必须报错而不是比一张残缺的表。"""
    path = write_tsx(tmp_path, element_declaration(body), kg_object_declaration())
    with pytest.raises(guard.GuardError):
        guard.frontend_labels(guard.FRONTEND_ELEMENT_CONST, path)
    assert guard.main(path) == 1


# --- 6. 重复键 -------------------------------------------------------------------


def test_duplicate_keys_are_rejected_not_folded(tmp_path):
    """dict() 与 JS 都会静默折叠成最后一个;守卫显式拒绝,否则漂移会被折叠掩盖。"""
    body = (
        '  formula: "公式清单",\n'
        '  table: "表格清单",\n'
        '  table: "表格清单",\n'
        '  image: "图片清单",\n'
        '  code_block: "代码块清单",\n'
    )
    path = write_tsx(tmp_path, element_declaration(body), kg_object_declaration())
    with pytest.raises(guard.GuardError):
        guard.frontend_labels(guard.FRONTEND_ELEMENT_CONST, path)
    assert guard.main(path) == 1


def test_duplicate_key_hiding_a_drift_is_rejected(tmp_path):
    """折叠后恰好等于预期值的最恶劣情形:第一份是错的,第二份把它盖住。"""
    body = (
        '  formula: "WRONG",\n'
        '  formula: "公式清单",\n'
        '  table: "表格清单",\n'
        '  image: "图片清单",\n'
        '  code_block: "代码块清单",\n'
    )
    path = write_tsx(tmp_path, element_declaration(body), kg_object_declaration())
    with pytest.raises(guard.GuardError):
        guard.frontend_labels(guard.FRONTEND_ELEMENT_CONST, path)
    assert guard.main(path) == 1


# --- 8. 注释剥离顺序:正确声明藏在 /* */ 里,后面跟错误的真实声明 -------------------


def test_correct_declaration_hidden_in_block_comment_does_not_shadow_the_real_one(tmp_path):
    """整份正确声明放进 /* */,后面再放错误的真实声明 —— 守卫必须消费后者。

    这正是「先剥整份文件注释、再搜索声明」要挡的洞:寻找声明的正则若跑在未剥注释的
    原文上,会先选中注释里那份、报绿。
    """
    hidden = "/*\n" + element_declaration() + "\n*/\n\n"
    wrong_body = (
        '  formula: "Formula",\n'
        '  table: "Table",\n'
        '  image: "Image",\n'
        '  code_block: "Code",\n'
    )
    path = write_tsx(
        tmp_path,
        element_declaration(wrong_body),
        kg_object_declaration(),
        prelude=hidden,
    )
    assert guard.frontend_labels(guard.FRONTEND_ELEMENT_CONST, path) == {
        "formula": "Formula",
        "table": "Table",
        "image": "Image",
        "code_block": "Code",
    }
    assert guard.main(path) == 1


def test_correct_declaration_hidden_in_line_comments_does_not_shadow_the_real_one(tmp_path):
    hidden = "".join(
        f"// {line}\n" for line in element_declaration().splitlines()
    ) + "\n"
    wrong_body = (
        '  formula: "公式清单",\n'
        '  table: "表格清单",\n'
        '  image: "图片清单",\n'
        '  code_block: "步骤清单",\n'  # 与后端不同
    )
    path = write_tsx(
        tmp_path,
        element_declaration(wrong_body),
        kg_object_declaration(),
        prelude=hidden,
    )
    assert guard.frontend_labels(guard.FRONTEND_ELEMENT_CONST, path)["code_block"] == "步骤清单"
    assert guard.main(path) == 1


# --- 9. 两表撞名(跨元素/知识对象重名) ----------------------------------------------


def test_cross_table_duplicate_name_is_rejected(tmp_path):
    """元素清单与知识对象清单的渲染值必须全域唯一;两者恰好同名时 main() 报错。

    这条钉住的是断言②(与断言①「值等于后端+清单」是两回事):即便两张表各自都满足
    「前端值==后端值+清单」,只要两张表的值集合有交集,依然必须报红——否则模型账目
    回喂与结果卡会把两个不同的集合叫同一个名字。
    """
    element_body = (
        '  formula: "公式清单",\n'
        '  table: "表格清单",\n'
        '  image: "图片清单",\n'
        '  code_block: "代码块清单",\n'
    )
    colliding_kg_body = (
        '  concept: "概念知识对象清单",\n'
        '  claim: "论断知识对象清单",\n'
        '  formula: "公式清单",\n'  # 与元素侧 formula 撞名
        '  procedure: "过程知识对象清单",\n'
    )
    path = write_tsx(
        tmp_path, element_declaration(element_body), kg_object_declaration(colliding_kg_body)
    )
    # 两张表各自解析都成功——撞名不是解析层面的问题。
    assert guard.frontend_labels(guard.FRONTEND_ELEMENT_CONST, path)["formula"] == "公式清单"
    assert guard.frontend_labels(guard.FRONTEND_KG_OBJECT_CONST, path)["formula"] == "公式清单"
    assert guard.main(path) == 1


# --- 10. 来源清单(第三张表)同样进两条断言 ---------------------------------------


def test_missing_source_declaration_is_rejected(tmp_path):
    """来源清单表缺失 → 硬失败。

    它只有一个键,最容易被顺手删掉或「反正只有一个标签」地内联成字符串;守卫必须
    像对另外两张表一样拒绝放行,否则 trace 的「来源清单」与卡片标题就没人对齐了。
    """
    path = write_tsx(
        tmp_path, element_declaration(), kg_object_declaration(), source_decl=""
    )
    with pytest.raises(guard.GuardError):
        guard.frontend_labels(guard.FRONTEND_SOURCE_CONST, path)
    assert guard.main(path) == 1


def test_renamed_source_value_is_a_mismatch(tmp_path):
    """前端把「来源清单」改成「文档清单」而后端没改 → main() 报非零。"""
    path = write_tsx(
        tmp_path,
        element_declaration(),
        kg_object_declaration(),
        source_decl=source_declaration('  sources: "文档清单",\n'),
    )
    assert guard.frontend_labels(guard.FRONTEND_SOURCE_CONST, path) == {
        "sources": "文档清单"
    }
    assert guard.main(path) == 1


def test_source_label_joins_the_global_uniqueness_check(tmp_path, monkeypatch):
    """来源清单与元素清单撞名 → main() 报非零(断言②覆盖第三张表)。

    这里把**后端**那张表也改成撞名的值(monkeypatch),于是断言①对三张表全部成立
    ——报红只可能来自唯一性检查本身。若只改前端,断言①同样会失败,这条测试就证明
    不了第三张表真的进了唯一性检查。
    """
    monkeypatch.setattr(guard, "BACKEND_SOURCE_LABELS", {"sources": "代码块"})
    path = write_tsx(
        tmp_path,
        element_declaration(),
        kg_object_declaration(),
        source_decl=source_declaration('  sources: "代码块清单",\n'),
    )
    # 断言①:三张表都与(被改过的)后端一致。
    assert guard.frontend_labels(guard.FRONTEND_SOURCE_CONST, path) == {
        "sources": "代码块清单"
    }
    assert guard.main(path) == 1


# --- 剥注释本身的单元覆盖 --------------------------------------------------------


def test_strip_ts_comments_keeps_comment_like_text_inside_strings():
    src = 'const a = "http://x/*y*/z"; // 注释\n/* 块 */ const b = 1;'
    stripped = guard.strip_ts_comments(src)
    assert "http://x/*y*/z" in stripped
    assert "注释" not in stripped
    assert "块" not in stripped
    assert "const b = 1;" in stripped
