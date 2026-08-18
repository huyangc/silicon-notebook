# backend/tests/test_notebook_update_authorization_free.py
"""反向护栏:`NotebookUpdate` 的每个字段都与**授权判定无关**(codex #519 R10)。

P2 的能力翻转让 `notebook:manage` 解析到 admin 档,于是 `PATCH /notebooks/{id}` 对**组
管理员**开放。那个端点收的不只是 `name`,而是整份描述性画像(八个字段)。这件事之所以
安全,靠的是一条**性质**而不是运气:这些字段没有一个进入授权判定——访问权只由
`access_sql.py` 的三条谓词(外加 `mount_sql.py` 的可挂载性)决定。

护栏的触发条件是**有人往 `NotebookUpdate` 里加字段**。那一刻必须有人回来想清楚
「这个字段会不会让 admin 档提权」,而不是让新字段静默继承旧结论。所以这里冻结字段集合,
并对每个字段逐一说明它凭什么是安全的。

判据用 **Pydantic 的运行时自省**(`model_fields`)而不是解析源码文本:模型自己就是真源,
文本匹配既会在无害重排上误红,也拦不住换个写法的同一件事(与前端 `static-source-policy`
禁止裸读生产源码是同一条道理)。

⚠ 冻结集合**本身不是证明**,它只是一道闸。真正的证明是下面第二条测试:把字段名拿去和
授权谓词的实际 SQL 比对。两条缺一不可——只冻结集合,改谓词的人不会被通知;只比对 SQL,
加一个「不在 SQL 里但另有别的提权路径」的字段仍然静默通过。
"""
from __future__ import annotations

import re

import pytest

from app.models.notebooks import NotebookUpdate


#: 字段 -> 它凭什么与授权无关。加字段时**必须**在这里写一行,写不出来就说明该停下来想。
AUTHORIZATION_FREE_FIELDS: dict[str, str] = {
    "name": "展示名。授权谓词里唯一出现 name 的地方是 mount_sql.MOUNT_ORDER 的 ORDER BY"
            "(排序键,不是判定);改名只改挂载候选列表的显示顺序。",
    "purpose": "这个库是干什么的(散文)。会经 MCP list_notebooks/select_notebook 提供给"
               "外部 Agent(截断 500 字符),属内容邻接的描述,不参与任何判定。",
    "primary_domain": "领域描述。库内搜索框(GET /notebooks/{id}/search)拿它做可匹配字段,"
                      "命中只影响搜索结果,不影响谁能进来。",
    "target_users": "面向谁写的(散文)。只被目录投影读回。",
    "expected_questions": "预期问题清单(散文)。只被目录投影读回;不进任何 prompt。",
    "source_types": "预期资料类型(散文)。只被目录投影读回。",
    "taxonomy": "自定义分类词(散文)。只被目录投影读回。",
    "access_scope": "**「这个库是给谁用的」这句描述文字,不是访问控制列。** 全仓只有两个"
                    "消费者:notebook_store 的 update 写它、notebook_catalog 读回投影。"
                    "授权全部在 access_sql.py —— 名字像权限,行为不是。",
}

#: 真正做**授权判定**的 SQL 常量(逐个点名,别用「模块里所有 str」——那会把 ORDER BY、
#: JOIN 骨架和投影列一起扫进来,`name` 就会因为 MOUNT_ORDER 的排序键被误判成参与授权)。
_PREDICATE_CONSTANTS = (
    ("app.repositories.sqlite.access_sql", "NOTEBOOK_READ_SQL"),
    ("app.repositories.sqlite.access_sql", "NOTEBOOK_ADMIN_SQL"),
    ("app.repositories.sqlite.access_sql", "NOTEBOOK_WRITE_SQL"),
    ("app.repositories.sqlite.access_sql", "MEMBER_PROBE_SQL"),
    ("app.repositories.sqlite.access_sql", "GRANT_PROBE_SQL"),
    ("app.repositories.sqlite.access_sql", "ADMIN_GRANT_PROBE_SQL"),
    ("app.repositories.sqlite.mount_sql", "MOUNT_VALID"),
    ("app.repositories.sqlite.mount_sql", "MOUNT_VALID_EXPR"),
    ("app.repositories.sqlite.mount_sql", "MOUNT_GATE_CLOSED_EXPR"),
)

#: 空转保护:授权谓词里**确实**会出现的列。扫不到它们就说明上面那张表指错了地方,
#: 而不是「所有字段都干净」。
_COLUMNS_THAT_DO_DECIDE = ("created_by", "tier")


def _predicate_sql() -> str:
    from importlib import import_module

    chunks = []
    for module_name, constant in _PREDICATE_CONSTANTS:
        value = getattr(import_module(module_name), constant)
        assert isinstance(value, str), f"{module_name}.{constant} 不再是 SQL 字符串"
        chunks.append(value)
    return "\n".join(chunks)


def test_notebook_update_field_set_is_frozen():
    """字段集合一变就报红,逼加字段的人回来回答「它会不会让 admin 档提权」。"""
    actual = set(NotebookUpdate.model_fields)
    expected = set(AUTHORIZATION_FREE_FIELDS)
    added = sorted(actual - expected)
    removed = sorted(expected - actual)
    assert actual == expected, (
        f"NotebookUpdate 的字段集合变了(新增:{added};删除:{removed})。\n"
        "\n"
        "**这不是让你把名字补进表里就完事的断言。** `PATCH /notebooks/{id}` 挂的是\n"
        "`notebook:manage`,P2 之后它解析到 **admin 档** —— 也就是说**组管理员(不是库主)**\n"
        "能改这些字段。新增字段前请逐条回答:\n"
        "  1. 它会不会进入 `access_sql.py` / `mount_sql.py` 的任何一条谓词?进了就是提权:\n"
        "     一个组管理员可以改写决定「谁能读这个库」的输入。\n"
        "  2. 它是不是 repository 私有的生命周期状态(`status` / `tier` / `created_by` /\n"
        "     `is_shared`)?那些绝不能经公开 API 写,`extra=\"forbid\"` 正是为此。\n"
        "  3. 它会不会喂进某条 prompt 或检索决策?那就不再是纯描述性元数据,权限归属要重新判。\n"
        "\n"
        "答完之后把结论(而不只是字段名)写进 AUTHORIZATION_FREE_FIELDS 的说明里;\n"
        "如果答案是「它确实参与授权」,那就该改代码而不是改这张表。"
    )


def test_no_notebook_update_field_appears_in_any_authorization_predicate():
    """承重的那一条:把字段名拿去和授权谓词的**实际 SQL** 比对。

    冻结集合只能通知「有人加了字段」,通知不了「有人把某个既有字段引进了谓词」——那种改动
    在另一个文件里发生,字段集合一个字都不变。
    """
    sql = _predicate_sql()
    # 空转保护:证明扫的确实是授权谓词,而不是一段碰巧不含这些词的字符串。
    missing = [col for col in _COLUMNS_THAT_DO_DECIDE if not re.search(rf"\b{col}\b", sql)]
    assert missing == [], (
        f"授权谓词里扫不到 {missing} —— _PREDICATE_CONSTANTS 指错了地方,"
        "下面那条断言现在是空转的。"
    )

    offenders = [
        field for field in sorted(NotebookUpdate.model_fields)
        if re.search(rf"\b{re.escape(field)}\b", sql)
    ]
    assert offenders == [], (
        f"这些 NotebookUpdate 字段出现在授权谓词里:{offenders}。\n"
        "`PATCH /notebooks/{id}` 是 admin 档能力 —— 组管理员能改它们。一个既能被组管理员\n"
        "改写、又参与「谁能读这个库」判定的字段,就是一条提权路径:他可以把自己或别人写进\n"
        "访问集合。要么把该字段移出 NotebookUpdate,要么把判定改成不依赖它。"
    )


def test_lifecycle_state_cannot_be_smuggled_through_the_update_model():
    """`extra="forbid"` 是第二道性质:未知键必须 422,不是静默忽略。

    少了它,`{"status": "copying"}` 这类 payload 会被 Pydantic 丢掉(看起来无害),而一旦
    将来某个写路径改成透传 `model_dump()`,仓库私有的生命周期列就跟着可写了。
    """
    assert NotebookUpdate.model_config.get("extra") == "forbid"
    for smuggled in ("status", "tier", "created_by", "is_shared"):
        with pytest.raises(Exception) as excinfo:
            NotebookUpdate(**{smuggled: "x"})
        assert "extra" in str(excinfo.value).lower() or "forbid" in str(excinfo.value).lower(), (
            f"{smuggled} 没有被 extra=forbid 挡住"
        )
