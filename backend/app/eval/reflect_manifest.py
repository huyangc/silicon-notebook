"""PR-5 实验通道共用的运行 manifest:纯构造 + 闭集 + 隐私断言。

设计真源:`docs/superpowers/specs/2026-09-09-reflect-prefix-cache-final-design_zh.md`
§8.2/§13(交付物「冻结 manifest」);实施真源:
`docs/superpowers/specs/2026-09-11-reflect-prefix-experiments-plan_zh.md` Q9(键集
与落点)、T-EX1(本模块的要点与验收)。**评审后修正**(ex1 修正轮,已回填 Q9):
闭集扩到 18 键(+`case_set_digest`、+`sample_digest`)、`matrix` 子键契约收进本
模块、加一张全通道共同必填表——见下文三处常量各自的说明。

**这个模块纯逻辑、零 I/O、零 `git`、零 `Settings`**:`code_sha` / 时间戳 / 语料签名
全部由调用方(`scripts/reflect_shadow_rig.py` 三条通道各自的收尾处)算好后当关键
字参数传入——这里只管「一份 manifest 长什么样才算合法」,不管怎么把它算出来或
写到哪个文件。

`MANIFEST_KEYS` 是三条通道(E1/E2/E3)**共用**的顶层键闭集(18 键);每条通道
再各自要求其中几个键必须**在场**(值可以是 `None`,例如 E3 的 `arm_order_seed`
——M4 明确臂序按 run 无种子随机,`None` 合法,但键本身不许被漏写)。

`assert_manifest_values` 复用 `app.domain.reasoning_trace_stats._is_short_code`
——**同一个**短码判据(字符集、长度上限),不重写一份正则;导入的是私有名而不是
公开的 `assert_projection_values` 本身,因为那边的字典值只许数值(给
`actions_by_type` 这类计数表用),这边的字典天然需要「短码 → 短码」——
`corpus_signature_by_cell`(`scripts/reflect_shadow_rig.py` 的 `_ab_corpus_signature`
返回十六位十六进制摘要)、`case_set_digest`、`sample_digest`、
`intent_contract_digest_by_question`、`optimization_by_arm` 都是
「格/题/臂/样本 → 一个短码摘要」的形状,整体复用 `assert_projection_values` 会
连带拒掉这些合法值。导入私有名的代价是可控的——同一个对象、同一把尺子,改名
会在导入期响亮失败而不是静默改变校验口径;仓库里已有同形先例
(`backend/app/services/source_embedding.py`)。允许的值形状因此是一棵递归树:
标量(`bool` / `int` / `float` / `None` / 短码字符串)、这棵树的列表、或者键为
短码字符串、值递归属于同一棵树的字典。生产凭据、`postgresql://` 连接串、
`http(s)://` 地址、题面原文——这些形状里含有短码字符集
(`[A-Za-z0-9_:\\-.+→]`)之外的字符(空白、`/`、`@`、中文标点等),在这一层递归到
底都过不了 `_is_short_code`,不需要为每一种红线单独写判断,与投影行的隐私守卫
共用同一把尺子。字典**键**同样要过 `_is_short_code`——污染点落在键而不是值时
(例如 `{"budgets": {"题面原文": 1}}`)同样被拒,见 `_assert_manifest_value`。
"""
from __future__ import annotations

from typing import Any, Mapping

from app.domain.reasoning_trace_stats import _is_short_code

#: 三条实验通道的短码(计划 §0 的三层)。
CHANNELS: tuple[str, ...] = ("e1", "e2", "e3")

#: manifest 顶层键闭集(计划 Q9,评审后修正为 18 键)。三条通道共用同一张表——
#: 具体某条通道用不到的键就不写进它自己的 manifest,而不是给每条通道单开一张
#: 闭集。
MANIFEST_KEYS: frozenset[str] = frozenset({
    "code_sha", "channel", "arms", "optimization_by_arm", "common_baseline",
    "corpus_signature_by_cell", "case_set_digest", "sample_digest",
    "intent_contract_digest_by_question",
    "model_contract", "seed", "arm_order_seed", "order", "matrix", "budgets",
    "started_at", "finished_at", "stopped_by_budget",
})

#: 全部三条通道共同必填的键(评审 F3、拍板收窄到本期范围)。`started_at` /
#: `finished_at` / `code_sha` 是 §13「冻结 manifest」的冻结锚点本体;
#: `stopped_by_budget` 记录这次跑是不是被预算掐停的——E1/E2 没有整批预算概念,
#: 调用方写死 `False`,键本身仍然必须在场,不许省略。
REQUIRED_KEYS_ALL_CHANNELS: frozenset[str] = frozenset({
    "code_sha", "started_at", "finished_at", "stopped_by_budget",
})

#: 每条通道各自的必填键(T-EX1 要点 + 评审后修正)。`channel` 本身不在这张表
#: 里——它是路由到这张表的钥匙,`build_manifest` 单独校验它是否在场、是否属于
#: `CHANNELS`。三条通道各自还共同欠 `REQUIRED_KEYS_ALL_CHANNELS`(上面那张
#: 表),`build_manifest` 把两张表取并集再核对。
#:
#: * E1(前缀敏感性探针)—— `seed`(§9.1 区组随机必须有种子,M4「E1 的区组
#:   随机则必须有种子」)+ `matrix`(三个长度档各自的计数,子键契约见
#:   `REQUIRED_MATRIX_KEYS_BY_CHANNEL["e1"]`)+ `sample_digest`(这次跑用的
#:   上下文样本的短码摘要——Q3 要求「记录用了哪一份,不记正文」,样本的
#:   *长度档*进 `matrix.tiers`,样本的*身份*进这个独立键,两者不合并);
#: * E2(固定状态真实决策)—— `corpus_signature_by_cell`(测试库语料的指纹,
#:   `scripts/reflect_shadow_rig.py` 的 `_ab_corpus_signature`,按语料格
#:   A_nokg/B_kg 划分)+ `case_set_digest`(12 例剧本 JSON 本身的摘要短码——
#:   与语料签名是两个独立事实:case 集是仓库内自包含剧本,Q5「一个 case 不含
#:   任何数据库 id」,对语料签名的输入零贡献,因此单开一个键,不与语料签名
#:   合并)+ `matrix`(状态点表,子键契约见
#:   `REQUIRED_MATRIX_KEYS_BY_CHANNEL["e2"]`);
#: * E3(真实自主循环 + 完整 Ask)—— `intent_contract_digest_by_question`
#:   (既有 `ab` 每题一个冻结意图契约的摘要)+ `arm_order_seed`(允许 `None`,
#:   但键必须在)+ `matrix`(题/精力档/臂/轮次各自的计数,子键契约见
#:   `REQUIRED_MATRIX_KEYS_BY_CHANNEL["e3"]`)。
REQUIRED_KEYS_BY_CHANNEL: Mapping[str, frozenset[str]] = {
    "e1": frozenset({"seed", "matrix", "sample_digest"}),
    "e2": frozenset({"corpus_signature_by_cell", "matrix", "case_set_digest"}),
    "e3": frozenset(
        {"intent_contract_digest_by_question", "arm_order_seed", "matrix"}
    ),
}

#: `matrix` 本身不是标量,它是一张计数表——只要求键在场挡不住「计数表是空的」
#: 或「子键名字每条通道各写一套,谁都不知道该读哪个」。这张表钉死每条通道
#: `matrix` 里必须出现的子键名字(值本身仍然只受 `assert_manifest_values` 的
#: 形状闸约束,可以是任意计数);T-EX4/T-EX7/T-EX8 写 `matrix` 时必须用这些
#: 名字,不能各写各的。
#:
#: * E1 —— `tiers`(三个长度档各自的计数);
#: * E2 —— `state_points`(三个状态点各自的计数);
#: * E3 —— `questions` / `efforts` / `arms` / `repeats`(题数、精力档数、臂数、
#:   每格重复轮数——四个维度都要能在 manifest 里对出账,少一个都不许通过)。
REQUIRED_MATRIX_KEYS_BY_CHANNEL: Mapping[str, frozenset[str]] = {
    "e1": frozenset({"tiers"}),
    "e2": frozenset({"state_points"}),
    "e3": frozenset({"questions", "efforts", "arms", "repeats"}),
}


def assert_manifest_closed(row: Mapping) -> None:
    """manifest 的形状自检:闭集外的键当场报错。rig 写文件之前调它一次。"""
    extra = set(row) - MANIFEST_KEYS
    if extra:
        raise ValueError(
            "manifest carries keys outside MANIFEST_KEYS: "
            + ", ".join(sorted(extra))
        )


def _is_manifest_scalar(value: object) -> bool:
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return True
    return _is_short_code(value)


def _assert_manifest_value(path: str, value: object) -> None:
    if _is_manifest_scalar(value):
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_manifest_value(f"{path}[{index}]", item)
        return
    if isinstance(value, Mapping):
        for inner_key, inner_value in value.items():
            if not _is_short_code(inner_key):
                raise ValueError(
                    f"manifest value at {path!r} carries a non-short-code "
                    f"dict key: {inner_key!r}"
                )
            _assert_manifest_value(f"{path}.{inner_key}", inner_value)
        return
    raise ValueError(
        f"manifest value at {path!r} has an unsupported shape: {value!r}"
    )


def assert_manifest_values(row: Mapping) -> None:
    """manifest 的**值**形状自检(与 `assert_manifest_closed` 互补)。

    允许的值形状(闭集,不允许别的,递归):

    - 标量:`bool` / `int` / `float` / `None`,或一个短码字符串;
    - 上述标量的列表(`arms` / `order` 这类枚举);
    - 键为短码字符串、值递归属于这棵树的字典(`matrix` / `budgets` 这类计数表,
      也包括 `corpus_signature_by_cell` / `case_set_digest` /
      `intent_contract_digest_by_question` / `optimization_by_arm` 这类
      「格/题/臂/样本 → 短码摘要」表)。

    往任何一个允许的键里塞一段题面原文、一个 `postgresql://` 连接串、一个
    `http(s)://` 地址,都会在递归到底时撞上 `_is_short_code` 的字符集/长度门槛
    ——不需要为每一种红线单独写判断。字典**键**同样受这道闸约束,污染点落在
    键上(而不是值上)同样被拒。
    """
    for key, value in row.items():
        _assert_manifest_value(key, value)


def _assert_matrix_shape(channel: str, matrix: object) -> None:
    """`matrix` 必须是非空计数表,且含这条通道约定的子键。

    只校验「必填键在场」挡不住「`matrix` 是空字典」或「子键名字每条通道各写
    一套」——这两种都会让 §13「冻结 manifest」这项交付物对真实跑的是哪个矩阵
    零信息,却不会触发任何一条既有校验。见 `REQUIRED_MATRIX_KEYS_BY_CHANNEL`
    上方的注释。
    """
    if not isinstance(matrix, Mapping) or not matrix:
        raise ValueError(
            f"manifest for channel {channel!r} requires 'matrix' to be a "
            f"non-empty mapping, got {matrix!r}"
        )
    required = REQUIRED_MATRIX_KEYS_BY_CHANNEL[channel]
    missing = required - set(matrix)
    if missing:
        raise ValueError(
            f"manifest 'matrix' for channel {channel!r} is missing required "
            "key(s): " + ", ".join(sorted(missing))
        )


def build_manifest(**facts: Any) -> dict[str, Any]:
    """从调用方传入的事实构造一份闭集校验过的 manifest。

    调用方(三条通道各自的 rig 收尾处)负责算好每一项事实——`code_sha` 来自
    一次 `git rev-parse HEAD`,时间戳来自调用方自己的时钟,语料签名/意图摘要
    来自各自通道的既有函数。这个函数只管形状:缺了这条通道的必填键(含全通道
    共同必填、`matrix` 子键)、多了闭集外的键、或者哪个值形状不对,一律在这里
    响亮失败,不静默兜底、不猜省略值。
    """
    row: dict[str, Any] = dict(facts)
    assert_manifest_closed(row)
    if "channel" not in row:
        raise ValueError("manifest is missing required key(s): channel")
    channel = row["channel"]
    if channel not in CHANNELS:
        raise ValueError(
            f"manifest channel must be one of {sorted(CHANNELS)}, got {channel!r}"
        )
    required = REQUIRED_KEYS_BY_CHANNEL[channel] | REQUIRED_KEYS_ALL_CHANNELS
    missing = required - set(row)
    if missing:
        raise ValueError(
            f"manifest for channel {channel!r} is missing required key(s): "
            + ", ".join(sorted(missing))
        )
    assert_manifest_values(row)
    _assert_matrix_shape(channel, row["matrix"])
    return row
