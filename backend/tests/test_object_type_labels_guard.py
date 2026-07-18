"""check_object_type_labels_contract.py 的负向/正向测试。

review 揪出的关键点:测试如果**重新实现**守卫的解析逻辑,就只证明了那份复制品会失败,
证明不了硬门会失败。所以这里一律用 tmp_path 造临时 kg-type-mark.tsx 内容,交给守卫的
**真实函数**(frontend_labels() / main())去消费,断言抛 GuardError 或退出码非零。

覆盖的假绿路径(逐条对应 review 列的场景):
  1. 注释掉真实条目            → 前端少一个 key → mismatch
  2. 注释里藏一份正确声明      → 必须看后面那份错误的真实声明
  3. spread(...base)          → 无法零求值判定 → GuardError
  4. computed key([k]: v)     → 同上
  5. 非字面量值(concept: v)   → 同上
  6. 多份声明                  → 无法判定哪份生效 → GuardError
  7. 重复键                    → dict() 会静默折叠 → 显式拒绝
"""
import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "otl_guard", ROOT / "scripts" / "check_object_type_labels_contract.py"
)
otl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(otl)

CANONICAL_BODY = """\
  concept: "概念 Concept",
  claim: "论断 Claim",
  formula: "公式 Formula",
  procedure: "过程 Procedure",
"""


def write_tsx(tmp_path: pathlib.Path, declaration: str, prelude: str = "") -> pathlib.Path:
    """造一份最小 kg-type-mark.tsx 替身,只放守卫关心的部分。"""
    path = tmp_path / "kg-type-mark.tsx"
    path.write_text(
        "export const KG_TYPE_STYLE: Record<string, string> = {\n"
        '  concept: "#2f80ed",\n'
        "};\n\n"
        f"{prelude}"
        f"{declaration}\n\n"
        "export function kgTypeLabel(type: string): string {\n"
        "  return Object.hasOwn(KG_TYPE_LABELS, type) ? KG_TYPE_LABELS[type] : type;\n"
        "}\n",
        encoding="utf-8",
    )
    return path


def canonical_declaration(body: str = CANONICAL_BODY) -> str:
    return "const KG_TYPE_LABELS: Record<string, string> = {\n" + body + "};"


# --- 正向控制组:守卫不是「永远失败」--------------------------------------------


def test_canonical_fixture_passes_the_real_guard(tmp_path):
    """positive control——否则下面每条负向断言都可能只是因为守卫恒失败。"""
    path = write_tsx(tmp_path, canonical_declaration())
    assert otl.frontend_labels(path) == dict(otl.OBJECT_TYPE_LABELS)
    assert otl.main(path) == 0


def test_live_frontend_labels_match_backend():
    """live 前后端一致(守卫默认路径,无参数)。"""
    assert otl.frontend_labels() == dict(otl.OBJECT_TYPE_LABELS)
    assert otl.main() == 0


# --- 1. 注释掉真实条目 -----------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            '  concept: "概念 Concept",\n'
            '  // claim: "论断 Claim",\n'
            '  formula: "公式 Formula",\n'
            '  procedure: "过程 Procedure",\n',
            id="line-comment",
        ),
        pytest.param(
            '  concept: "概念 Concept",\n'
            '  /* claim: "论断 Claim", */\n'
            '  formula: "公式 Formula",\n'
            '  procedure: "过程 Procedure",\n',
            id="block-comment",
        ),
    ],
)
def test_commented_out_entry_fails_the_guard(tmp_path, body):
    path = write_tsx(tmp_path, canonical_declaration(body))
    assert "claim" not in otl.frontend_labels(path)
    assert otl.main(path) == 1


# --- 2. 注释里藏一份正确声明,真实声明是错的 --------------------------------------


def test_correct_declaration_hidden_in_block_comment_does_not_shadow_the_real_one(tmp_path):
    """整份正确声明放进 /* */,后面再放错误的真实声明 —— 守卫必须消费后者。

    这正是「先剥整份文件注释、再搜索声明」要挡的洞:寻找声明的正则若跑在未剥注释的
    原文上,会先选中注释里那份、报绿。
    """
    hidden = "/*\n" + canonical_declaration() + "\n*/\n\n"
    wrong_body = (
        '  concept: "Concept",\n'
        '  claim: "Claim",\n'
        '  formula: "Formula",\n'
        '  procedure: "Procedure",\n'
    )
    path = write_tsx(tmp_path, canonical_declaration(wrong_body), prelude=hidden)
    assert otl.frontend_labels(path) == {
        "concept": "Concept",
        "claim": "Claim",
        "formula": "Formula",
        "procedure": "Procedure",
    }
    assert otl.main(path) == 1


def test_correct_declaration_hidden_in_line_comments_does_not_shadow_the_real_one(tmp_path):
    hidden = "".join(
        f"// {line}\n" for line in canonical_declaration().splitlines()
    ) + "\n"
    wrong_body = (
        '  concept: "概念 Concept",\n'
        '  claim: "论断 Claim",\n'
        '  formula: "公式 Formula",\n'
        '  procedure: "步骤 Procedure",\n'  # 与后端不同
    )
    path = write_tsx(tmp_path, canonical_declaration(wrong_body), prelude=hidden)
    assert otl.frontend_labels(path)["procedure"] == "步骤 Procedure"
    assert otl.main(path) == 1


# --- 3/4/5. 守卫无法零求值判定的写法一律硬失败 ------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("  ...BASE_LABELS,\n" + CANONICAL_BODY, id="spread"),
        pytest.param(CANONICAL_BODY + "  ...extra,\n", id="spread-trailing"),
        pytest.param(CANONICAL_BODY + "  [customType]: \"自定义\",\n", id="computed-key"),
        pytest.param(
            '  concept: CONCEPT_LABEL,\n'
            '  claim: "论断 Claim",\n'
            '  formula: "公式 Formula",\n'
            '  procedure: "过程 Procedure",\n',
            id="identifier-value",
        ),
        pytest.param(
            '  concept: `概念 ${SUFFIX}`,\n'
            '  claim: "论断 Claim",\n'
            '  formula: "公式 Formula",\n'
            '  procedure: "过程 Procedure",\n',
            id="template-literal-value",
        ),
        pytest.param(
            '  concept: cond ? "A" : "B",\n'
            '  claim: "论断 Claim",\n'
            '  formula: "公式 Formula",\n'
            '  procedure: "过程 Procedure",\n',
            id="ternary-value",
        ),
        pytest.param(
            '  concept,\n'
            '  claim: "论断 Claim",\n'
            '  formula: "公式 Formula",\n'
            '  procedure: "过程 Procedure",\n',
            id="shorthand",
        ),
    ],
)
def test_non_canonical_entries_are_rejected_not_silently_skipped(tmp_path, body):
    """严格消费:这些写法下运行时对象已经变了,守卫必须报错而不是比一张残缺的表。"""
    path = write_tsx(tmp_path, canonical_declaration(body))
    with pytest.raises(otl.GuardError):
        otl.frontend_labels(path)
    assert otl.main(path) == 1


def test_spread_only_object_would_have_looked_canonical_to_a_lax_guard(tmp_path):
    """`{...BACKEND_LABELS}` 对宽松的 pair 正则来说「零条目」,曾会静默通过成空 dict。"""
    path = write_tsx(tmp_path, canonical_declaration("  ...BACKEND_LABELS,\n"))
    with pytest.raises(otl.GuardError):
        otl.frontend_labels(path)
    assert otl.main(path) == 1


# --- 6. 多份声明 -----------------------------------------------------------------


def test_multiple_declarations_are_rejected(tmp_path):
    second = canonical_declaration(
        '  concept: "Concept",\n'
        '  claim: "Claim",\n'
        '  formula: "Formula",\n'
        '  procedure: "Procedure",\n'
    )
    path = write_tsx(tmp_path, canonical_declaration() + "\n\n" + second)
    with pytest.raises(otl.GuardError):
        otl.frontend_labels(path)
    assert otl.main(path) == 1


def test_second_declaration_via_reassignment_form_is_rejected(tmp_path):
    """第二份即使不是对象字面量(守卫读不了),也必须报错而不是回落到第一份。"""
    path = write_tsx(
        tmp_path, canonical_declaration() + "\nconst KG_TYPE_LABELS = buildLabels();"
    )
    with pytest.raises(otl.GuardError):
        otl.frontend_labels(path)
    assert otl.main(path) == 1


def test_non_literal_declaration_is_rejected(tmp_path):
    path = write_tsx(tmp_path, "const KG_TYPE_LABELS = buildLabels();")
    with pytest.raises(otl.GuardError):
        otl.frontend_labels(path)
    assert otl.main(path) == 1


def test_missing_declaration_is_rejected(tmp_path):
    path = tmp_path / "kg-type-mark.tsx"
    path.write_text("export const KG_TYPE_STYLE = {};\n", encoding="utf-8")
    with pytest.raises(otl.GuardError):
        otl.frontend_labels(path)
    assert otl.main(path) == 1


# --- 7. 重复键 -------------------------------------------------------------------


def test_duplicate_keys_are_rejected_not_folded(tmp_path):
    """dict() 与 JS 都会静默折叠成最后一个;守卫显式拒绝,否则漂移会被折叠掩盖。"""
    body = (
        '  concept: "概念 Concept",\n'
        '  claim: "论断 Claim",\n'
        '  claim: "论断 Claim",\n'
        '  formula: "公式 Formula",\n'
        '  procedure: "过程 Procedure",\n'
    )
    path = write_tsx(tmp_path, canonical_declaration(body))
    with pytest.raises(otl.GuardError):
        otl.frontend_labels(path)
    assert otl.main(path) == 1


def test_duplicate_key_hiding_a_drift_is_rejected(tmp_path):
    """折叠后恰好等于后端的最恶劣情形:第一份是错的,第二份把它盖住。"""
    body = (
        '  concept: "WRONG",\n'
        '  concept: "概念 Concept",\n'
        '  claim: "论断 Claim",\n'
        '  formula: "公式 Formula",\n'
        '  procedure: "过程 Procedure",\n'
    )
    path = write_tsx(tmp_path, canonical_declaration(body))
    with pytest.raises(otl.GuardError):
        otl.frontend_labels(path)
    assert otl.main(path) == 1


# --- 剥注释本身的单元覆盖 --------------------------------------------------------


def test_strip_ts_comments_keeps_comment_like_text_inside_strings():
    src = 'const a = "http://x/*y*/z"; // 注释\n/* 块 */ const b = 1;'
    stripped = otl.strip_ts_comments(src)
    assert "http://x/*y*/z" in stripped
    assert "注释" not in stripped
    assert "块" not in stripped
    assert "const b = 1;" in stripped
