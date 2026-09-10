"""E1(前缀复用敏感性探针)的纯计划与纯分析:五个纯函数,零 I/O、零模型、零 DB。

设计真源:`docs/superpowers/specs/2026-09-09-reflect-prefix-cache-final-design_zh.md`
§9.1(两臂构造、区组平衡随机、首次观测不是已验证冷缓存、预热单列、结论三格);
实施真源:`docs/superpowers/specs/2026-09-11-reflect-prefix-experiments-plan_zh.md`
Q1-Q4(接缝与两臂形状)、T-EX3(本模块的五个函数、验收、用例 a-i)、T-EX4(它是
本模块的**唯一**消费者,读它才知道 rig 该怎么调用这些函数、行里该填什么)。

**这个模块纯逻辑、零 I/O、零模型、零 DB、零 `Settings`**:调用真实模型、把行写
成 `.jsonl`、算 `code_sha`、连接 `RuntimeModelProvider`,全部是 `scripts/
reflect_shadow_rig.py` 的 `cmd_prefix_probe`(T-EX4)的事——这里只管「一批 E1
调用长什么样、两臂标记怎么构造、结果怎么汇总」,不管怎么把调用真的发出去。
唯一的例外是 `load_prefix_probe_sample`:它是本模块**唯一**碰文件系统的函数,
只读一份 JSON,不做别的。

**分工边界(评审拍板,见 pr5-followups.md「T-EX2 quality 评审 → 拍板」P2-2)**:
E1 的分析只读 per-call 表(`app/eval/reflect_context_bench.py` 产出的
`call_wall_ms`/`status`/`attempts`/`usage.cached_tokens`/`usage.prompt_tokens`
这类既有键),**禁止**读 reflect trace 的 `ctx_bytes_total`/`message_prefix_bytes`
——那两列在标记接缝打开时对标记**盲**(测量层不知道有标记这回事,重建时不带
标记),会把两臂的观测悄悄拉平。`PROBE_ROW_KEYS` 因此不含这两个名字,`summarize_probe`
也无从读到它们。

**命名红线**(设计 §0 review 调整第 4 条 + 本计划反复重申):这个模块里没有、
也不许有任何 `cache_hit` / 命中率 / hit rate 形状的字段名。provider 侧的本地
缓存出口用 `local_cache_exit_rows` 记数——这是一个「不应出现的计数」,不是
「命中率」,两者的区别就是本期整份计划要守住的那条线。`PROBE_ROW_KEYS` 里的
`status` 字段值可能等于 llm.py 写下的 `"cache_hit"`(该值本身不是本模块起的
名字,是既有传输层的既有事实字段值),但这个值只会被 `summarize_probe` 单列
计数,绝不参与主统计、绝不被当成时间收益的证据。

**结论词面**(§9.1 原文「可下结论:稳定前缀在这个端点/负载/长度下有、无可辨认
或不确定的时间收益」):`summarize_probe` 的 `verdict` 只能是 `VERDICTS` 三格
之一,且这个模块**不产出任何比率型结论字段**——`median_wall_ms_ratio` 是一个
观测量(配对比值),不是结论;它与 `verdict` 是两个不同的键,前者可以是任意
浮点数,后者只能是三格闭集里的一个短码。
"""
from __future__ import annotations

import hashlib
import json
import random
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.domain.reasoning_trace_stats import assert_projection_values

# --- 两臂标记 -----------------------------------------------------------------

#: 两臂的短码名字。design §9.1:stable 开头标记序列内固定、末尾标记逐次变化;
#: disturbed 开头标记逐次变化、末尾标记序列内固定。两个字符串本身也是**唯一**
#: 合法的 `arm` 取值——`probe_plan`/`build_marker_pair` 都靠 `==` 这两个字面量
#: 判定角色,不接受调用方自定义的第三个名字。
ARM_STABLE = "stable"
ARM_DISTURBED = "disturbed"
DEFAULT_ARMS: tuple[str, str] = (ARM_STABLE, ARM_DISTURBED)

#: 每次调用的标记代码长度(十六进制字符数,不含前缀字母)。定宽是设计 §9.1的字面
#: 要求——两臂的标记必须「相同数量和长度」,这里用同一个常量生成头尾两个标记,
#: 长度天然相等,不需要额外的等长判断。
_MARKER_CODE_LEN = 12


def _marker_code(
    *, seed: int, tier: str, block_index: int, arm: str, component: str,
    index: int, marker_variant: int,
) -> str:
    """一个定宽、无语义的十六进制短码。

    `sha256` 只是一个方便的确定性摘要函数,不是在暗示这里有任何安全属性——
    这串字符除了「同输入产同输出、不同输入大概率产不同输出」之外不需要任何
    密码学性质,`component`(`"head"` 或 `"tail"`)与 `arm` 都进摘要输入,是
    为了让 stable 的 head 序列、stable 的 tail 序列、disturbed 的 head 序列、
    disturbed 的 tail 序列这四条流互不相交(§9.1「两臂使用不相同的标识,减少
    彼此污染」)。`tier` 也进摘要输入(codex #T-EX3 F1):一个「序列」的身份是
    `(tier, block_index, arm)`,不是 `(block_index, arm)`——三个长度档的调用
    在计划里顺序执行(§9.1「各序列内部保持连续」是 tier 外层),`tier` 缺席时
    不同档位的同一 `(block_index, arm)` 会复用同一批标记值与
    (`render_sample` 三档互为前缀的)同一段正文前缀,后一档的调用因此不再是
    「全新前缀」,把两臂的时间差系统性压小、也污染 `first_observation` 的
    读法。
    """
    payload = (
        f"{seed}:{tier}:{block_index}:{arm}:{component}:{index}:{marker_variant}"
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:_MARKER_CODE_LEN]


def build_marker_pair(
    seed: int, tier: str, block_index: int, arm: str, call_index: int,
    *, marker_variant: int = 0,
) -> tuple[str, str]:
    """一次调用要发的 `(head, tail)` 两个标记短码(design §9.1 的两臂构造)。

    `arm` 只接受 `ARM_STABLE` / `ARM_DISTURBED` 两个字面量之一,别的值当场
    响亮报错——两臂在这个函数里的差别**只有**「哪一端固定、哪一端逐次变化」,
    没有第三种布局。

    一个「序列」的身份是 `(tier, block_index, arm)`(codex #T-EX3 F1;不是
    `(block_index, arm)`——三个长度档各跑一遍同样的 `(block_index, arm)`
    组合,`tier` 缺席会让三档共用同一条标记流):

    * **stable**:同一个 `(tier, block_index)` 序列内,`head` 对所有
      `call_index` 恒定(`head_index = 0`),`tail` 逐次不同
      (`tail_index = call_index`);
    * **disturbed**:反过来,`head` 逐次不同、`tail` 序列内恒定。

    `marker_variant` 是「换标记值重复验证」的开关(§9.1「更换标记值重复验证」):
    同一批 `(seed, tier, block_index, arm, call_index)` 换一个
    `marker_variant` 就拿到一组全新的值,而计划形状(哪端固定哪端变)不变。

    只声明字符/字节匹配,不声明 token 匹配:这里没有可用的 tokenizer,`head`/
    `tail` 全部由 ASCII 十六进制字符 + 一个字母前缀拼成,`len(s) ==
    len(s.encode())` 对它们恒成立,所以「字符数」和「字节数」在这里是同一件事,
    但都不是「token 数」——真的要比较 token 长度需要接一个真实 tokenizer,
    这不是这个函数的职责。
    """
    if arm == ARM_STABLE:
        head_index, tail_index = 0, call_index
    elif arm == ARM_DISTURBED:
        head_index, tail_index = call_index, 0
    else:
        raise ValueError(
            f"build_marker_pair: unknown arm {arm!r}; expected "
            f"{ARM_STABLE!r} or {ARM_DISTURBED!r}"
        )
    head = "H" + _marker_code(
        seed=seed, tier=tier, block_index=block_index, arm=arm,
        component="head", index=head_index, marker_variant=marker_variant,
    )
    tail = "T" + _marker_code(
        seed=seed, tier=tier, block_index=block_index, arm=arm,
        component="tail", index=tail_index, marker_variant=marker_variant,
    )
    return head, tail


# --- 计划 --------------------------------------------------------------------

#: 默认三个长度档、默认区组数与默认每序列调用数(design §9.1:
#: 「3 个长度档 × 4 个配对区组 × 2 臂 × 每臂连续 4 次 = 96 次逻辑调用」)。
#: `SMOKE_BLOCKS` 是计划 U4 拍板的 `--smoke` 规模(区组数砍半 ⇒ 48 次)——字面量
#: 只在这里出现一次,T-EX4 的 dry-run 与 `--smoke` 分支都从这里读,不各写一份。
DEFAULT_TIERS: tuple[str, ...] = ("short", "medium", "long")
DEFAULT_BLOCKS = 4
DEFAULT_CALLS_PER_SERIES = 4
SMOKE_BLOCKS = 2


def _stable_first_flags(n_blocks: int, seed: int, tier: str) -> list[bool]:
    """`n_blocks` 个区组里,哪些用 stable→disturbed 顺序、哪些用反过来。

    「整体平衡」是一句**计数**要求,不是一句概率要求:恰好
    `n_blocks // 2` 个区组是 `True`(stable 先),其余是 `False`——不管种子
    是什么,这个计数永远精确对半(`n_blocks` 为奇数时多出的一个恒分给
    `False`,四个区组、两个区组这两个默认规模都是偶数,不触发这条边界)。
    种子只决定**哪几个**区组分到 `True`,用一个由 `(seed, tier)` 键出的
    `random.Random` 洗牌——同 seed 同 tier 逐字重现,换 seed 通常换一种分法
    (`shuffle` 的排列空间很小时"通常"不是"总是",用例只断言至少有一个
    种子对产生不同分法,不断言全部种子两两不同)。
    """
    half = n_blocks // 2
    flags = [True] * half + [False] * (n_blocks - half)
    # `random.Random` only accepts int/float/str/bytes/bytearray seeds — a
    # tuple has to be flattened into one of those first. A colon-joined
    # string keeps the three inputs from colliding into the same seed the
    # way naive string concatenation could (e.g. seed=1, tier="2x" vs.
    # seed=12, tier="x").
    rng = random.Random(f"{seed}:{tier}:arm_order")
    rng.shuffle(flags)
    return flags


def probe_plan(
    *,
    seed: int,
    tiers: Sequence[str] = DEFAULT_TIERS,
    blocks: int = DEFAULT_BLOCKS,
    arms: Sequence[str] = DEFAULT_ARMS,
    calls_per_series: int = DEFAULT_CALLS_PER_SERIES,
) -> list[dict]:
    """这一批 E1 要打的**全部**逻辑调用,一条一行,顺序就是要跑的顺序。

    默认 3 档 × 4 区组 × 2 臂 × 4 次 = 96 格;`blocks=SMOKE_BLOCKS`(2)⇒ 48 格
    ——两个规模用的是**同一份**函数,`--dry-run` 与真跑读同一份计划(照抄
    `reflect_ab.ab_plan`/`ab_arms` 的既有纪律,见计划 Q9 前一段的引用)。

    顺序是 `tier → block_index → (区组内的臂序) → call_index`,序列内部
    **连续**(同一个 `(tier, block_index, arm)` 的四次调用挨在一起),不同
    区组之间也不交错——design §9.1「各序列内部保持连续,记录间隔,不插入
    并发负载」。区组内的臂序由 `_stable_first_flags` 按 `(seed, tier)`
    平衡随机决定,`series_index` 是整份计划里第几个序列(全局单调递增),
    用来在分析侧把一个序列的四行认成一组,不需要从 `(tier, block_index,
    arm)` 反推。

    `arms` 目前只接受 `{ARM_STABLE, ARM_DISTURBED}` 这个集合(顺序不敏感——
    区组内谁先谁后由种子决定,不由这个参数的书写顺序决定);给别的集合
    响亮报错,不是因为将来一定不会有第三臂,而是因为「两臂标记不相交」
    (`build_marker_pair`)与「区组内两种顺序各半」(`_stable_first_flags`)
    这两条性质都是按**两臂**推导的,加一条臂不是加一个参数就能对的事。
    """
    if set(arms) != {ARM_STABLE, ARM_DISTURBED}:
        raise ValueError(
            f"probe_plan: arms must be exactly {{{ARM_STABLE!r}, "
            f"{ARM_DISTURBED!r}}}, got {list(arms)!r}"
        )
    if not tiers:
        raise ValueError("probe_plan: tiers must be non-empty")
    if len(set(tiers)) != len(tiers):
        raise ValueError(f"probe_plan: tiers must be unique, got {list(tiers)!r}")
    if blocks < 1:
        raise ValueError(f"probe_plan: blocks must be >= 1, got {blocks!r}")
    if calls_per_series < 1:
        raise ValueError(
            f"probe_plan: calls_per_series must be >= 1, got {calls_per_series!r}"
        )

    rows: list[dict] = []
    series_counter = 0
    for tier in tiers:
        flags = _stable_first_flags(blocks, seed, tier)
        for block_index in range(blocks):
            order = (
                (ARM_STABLE, ARM_DISTURBED) if flags[block_index]
                else (ARM_DISTURBED, ARM_STABLE)
            )
            for arm in order:
                series_index = series_counter
                series_counter += 1
                for call_index in range(calls_per_series):
                    head, tail = build_marker_pair(seed, tier, block_index, arm, call_index)
                    rows.append({
                        "tier": tier,
                        "block_index": block_index,
                        "arm": arm,
                        "call_index": call_index,
                        "series_index": series_index,
                        "head": head,
                        "tail": tail,
                    })
    return rows


# --- 样本渲染 ------------------------------------------------------------------

#: 三档目标字符数之外,固定不随档位变化的四块内容的键名(design §9.1「有用正文
#: 完全相同」);`GROWING_BLOCK_KEY` 是唯一随长度档伸缩的区块,类比真实 reflect
#: 的证据池(K)是随预算伸缩、其余(S/C/D/T)按各自边界保留的那一半。
FIXED_BLOCK_KEYS: tuple[str, ...] = (
    "static_instructions", "capability_catalog", "delta_notes", "task_target",
)
GROWING_BLOCK_KEY = "evidence_cards"

#: 固定的输出任务与 schema hint(design §9.1「输出固定小 JSON,例如
#: `{"ok":true}`；两臂用相同指令、相同输出上限与相同 schema」)。两臂、三档
#: 都是同一份——只有 `render_sample` 返回的正文长度随档位变化,任务本身不变。
PROBE_OUTPUT_INSTRUCTION = (
    '仅返回如下 JSON,不要输出其它文字或代码块标记:{"ok": true}'
)
PROBE_SCHEMA_HINT = (
    '{"type":"object","properties":{"ok":{"type":"boolean"}},'
    '"required":["ok"],"additionalProperties":false}'
)


def sample_tier_chars(sample: Mapping) -> dict[str, int]:
    """`sample["tier_chars"]` 的校验过副本:三档各一个正整数目标字符数。"""
    tier_chars = sample.get("tier_chars")
    if not isinstance(tier_chars, Mapping) or not tier_chars:
        raise ValueError(
            "sample_tier_chars: sample is missing a non-empty 'tier_chars' mapping"
        )
    result: dict[str, int] = {}
    for tier, chars in tier_chars.items():
        if isinstance(chars, bool) or not isinstance(chars, int) or chars <= 0:
            raise ValueError(
                f"sample_tier_chars: tier {tier!r} has a non-positive-int "
                f"char target: {chars!r}"
            )
        result[tier] = chars
    return result


def sample_digest(sample: Mapping) -> str:
    """样本的短码摘要(manifest 的 `sample_digest`:「记录用了哪一份,不记正文」）。

    对整份样本字典做一次规范化 JSON 序列化(键排序、无多余空白)再取
    `sha256` 前 16 位十六进制——16 位与 `reflect_manifest` 测试用例里其它
    `*_digest` 字段的既有长度一致,足够在同一批实验里区分「哪一份样本文件」，
    不需要也不应该拿它反推样本内容。
    """
    canonical = json.dumps(
        sample, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _fill_to_length(seed_text: str, target_chars: int) -> str:
    """把 `seed_text` 重复拼接到至少 `target_chars` 字符,再截到恰好这么长。

    每次重复前加一个 `[NNNN]` 序号前缀,纯粹是为了不让最终文本变成同一个
    子串的裸重复(那种文本对某些 provider 端的规范化/去重策略太友好,会
    干扰而不是隔离要测的东西)。这不是在向 provider 的 cache 行为讨好或
    对抗——只是让「确定性地填满一个目标长度」这件事有一个可读的实现。
    """
    if target_chars <= 0:
        return ""
    pieces: list[str] = []
    total = 0
    index = 0
    while total < target_chars:
        piece = f"[{index:04d}] {seed_text}"
        pieces.append(piece)
        total += len(piece) + 1  # 加入下面 "\n".join 的分隔符
        index += 1
    return "\n".join(pieces)[:target_chars]


def render_sample(tier: str, sample: Mapping) -> list[dict]:
    """一个长度档的固定正文:五块合成上下文 + 固定输出任务,两臂共用同一份。

    返回值是 `provider_messages` 的 `messages` 入参形状——`[{"role": "user",
    "content": ...}]`,不含 wrapper、不含标记:those 由 `app.core.llm.
    provider_messages`/`experiment_message_markers` 在调用方装配,这个函数
    只负责「有用正文長什么样」。

    五块顺序固定为 静态指令(S)→ 动态能力(C)→ 证据卡池(K)→ 增量提示(D)→
    任务目标(T),只有 K(`GROWING_BLOCK_KEY`)按 `sample_tier_chars(sample)[tier]`
    伸缩,其余四块字符数恒定——因此三档的**总**长度单调递增(用例 g),且
    S/C/D/T 在三档之间逐字相同,唯一变化的是 K 块被 `_fill_to_length` 重复
    拼接到的长度。
    """
    tier_chars = sample_tier_chars(sample)
    if tier not in tier_chars:
        raise ValueError(
            f"render_sample: unknown tier {tier!r}; expected one of "
            f"{sorted(tier_chars)}"
        )
    paragraphs = sample.get("seed_paragraphs")
    if not isinstance(paragraphs, Mapping):
        raise ValueError("render_sample: sample is missing 'seed_paragraphs'")
    missing = [k for k in (*FIXED_BLOCK_KEYS, GROWING_BLOCK_KEY) if k not in paragraphs]
    if missing:
        raise ValueError(
            "render_sample: sample['seed_paragraphs'] is missing key(s): "
            + ", ".join(missing)
        )

    fixed_len = sum(len(paragraphs[key]) for key in FIXED_BLOCK_KEYS)
    # 五块之间用 "\n\n" 连接(4 个连接处),外加正文与固定任务指令之间再一个
    # "\n\n"、加上任务指令本身的长度——这里把这份账全部预留出来,好让 K 块的
    # 目标长度尽量对齐 `sample_tier_chars` 声明的整体目标,而不是让分隔符与
    # 固定任务指令的字节悄悄从 K 的预算里"偷"走一部分。
    separator_len = 2 * 5
    target = tier_chars[tier]
    growing_target = max(
        0, target - fixed_len - separator_len - len(PROBE_OUTPUT_INSTRUCTION)
    )
    growing_block = _fill_to_length(paragraphs[GROWING_BLOCK_KEY], growing_target)

    ordered = [
        paragraphs["static_instructions"],
        paragraphs["capability_catalog"],
        growing_block,
        paragraphs["delta_notes"],
        paragraphs["task_target"],
    ]
    body = "\n\n".join(ordered)
    content = f"{body}\n\n{PROBE_OUTPUT_INSTRUCTION}"
    return [{"role": "user", "content": content}]


def load_prefix_probe_sample(path: "str | Path") -> dict:
    """从磁盘读一份样本 JSON。**这个模块唯一碰文件系统的函数**,只读、不解释。

    默认样本落在 `backend/app/eval/reflect_t0/prefix_probe_sample.json`;
    `--sample-file` 让操作者指向 `.local` 下的真实样本时,走的还是这一个
    函数,不是另开一条读法。
    """
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# --- 行闭集 --------------------------------------------------------------------

#: 每格调用结果行允许出现的**全部**顶层键(T-EX3 要点 4)。计划里没有的两个
#: 名字——`ctx_bytes_total` / `message_prefix_bytes`——是刻意的排除,不是遗漏
#: (见模块 docstring「分工边界」)。`gap_ms`(codex #T-EX3 F3)是设计 §9.1
#: 「各序列内部保持连续,记录间隔」要求的落点:与上一次调用的间隔毫秒,序列
#: 首格没有「上一次」,值是 `None`,其余格是数值——T-EX4 补记这一列时不需要
#: 改这份闭集。
PROBE_ROW_KEYS: frozenset[str] = frozenset({
    "tier", "block_index", "arm", "call_index", "series_index",
    "status", "call_wall_ms", "attempts", "finish_reason", "response_chars",
    "prompt_tokens", "cached_tokens", "completion_tokens",
    "head_chars", "tail_chars", "message_bytes_total", "is_warmup", "gap_ms",
})


def assert_probe_row_closed(row: Mapping) -> None:
    """一行探针结果的形状自检:闭集外的键、或非法值形状,当场报错。

    与 `reflect_context_bench.assert_call_row_closed` 同一条纪律(只挡多出
    的键),外加复用 `assert_projection_values` 挡值形状——这一行的值全是
    标量(短码字符串 / int / bool / `None`),不需要重写一遍判据。
    """
    extra = set(row) - PROBE_ROW_KEYS
    if extra:
        raise ValueError(
            "probe row carries keys outside PROBE_ROW_KEYS: "
            + ", ".join(sorted(extra))
        )
    assert_projection_values(row)


# --- 汇总 --------------------------------------------------------------------

#: `status` 取值里,provider 侧本地缓存出口的字面量(`app/core/llm.py` 既有
#: 事实字段的既有取值,不是本模块起的名字)。design Q2:E1 全程
#: `bypass_cache=True`,这一格结构上不应出现;出现了也不进主统计,单列成
#: `local_cache_exit_rows`(命名红线:字段名不许含 `cache_hit`/命中率)。
_LOCAL_CACHE_EXIT_STATUS = "cache_hit"
_SUCCESS_STATUS = "ok"

#: 结论词面闭集(design §9.1「可下结论:有、无可辨认或不确定的时间收益」）。
#: `summarize_probe` 的 `verdict` 只能是这三格之一——它是**唯一**的结论字段,
#: 与 `median_wall_ms_delta`/`median_wall_ms_ratio` 这类观测量分属两个键。
VERDICTS: tuple[str, ...] = (
    "time_benefit", "no_discernible_benefit", "undetermined",
)

#: 判据的两个阈值,字面量只在这里出现一次(见 `_verdict` 的说明）。
_MIN_PAIRED_REGIONS_FOR_VERDICT = 2
_CONSISTENCY_THRESHOLD = 0.75


def _numeric_wall_ms(row: Mapping) -> float | None:
    wall = row.get("call_wall_ms")
    if isinstance(wall, bool) or not isinstance(wall, (int, float)):
        return None
    return float(wall)


def _region_key(row: Mapping) -> tuple:
    return (row.get("tier"), row.get("block_index"))


def _summarize_region_pairs(rows: Sequence[Mapping]) -> dict:
    """`rows`(通常是某一档或全部的**重复**观测)按 `(tier, block_index)`
    区组配对两臂,产出中位差/中位比值/一致性,以及首次观测各臂中位墙钟。

    「配对」在这里是「同一区组内,stable 一侧的中位墙钟」与「disturbed 一侧
    的中位墙钟」相减/相除——先各自在区组内取中位数,再跨区组比较,这与
    §10.1「先对同题同档的重复汇总,再按……比较」是同一条纪律的 E1 版本。
    """
    by_region_arm: dict[tuple, list[float]] = {}
    for row in rows:
        wall = _numeric_wall_ms(row)
        if wall is None:
            continue
        key = (_region_key(row), row.get("arm"))
        by_region_arm.setdefault(key, []).append(wall)

    regions = sorted(
        {region for (region, _arm) in by_region_arm},
        key=lambda r: (str(r[0]), r[1] if r[1] is not None else -1),
    )
    deltas: list[float] = []
    ratios: list[float] = []
    for region in regions:
        stable_vals = by_region_arm.get((region, ARM_STABLE))
        disturbed_vals = by_region_arm.get((region, ARM_DISTURBED))
        if not stable_vals or not disturbed_vals:
            continue
        stable_med = statistics.median(stable_vals)
        disturbed_med = statistics.median(disturbed_vals)
        deltas.append(disturbed_med - stable_med)
        if stable_med:
            ratios.append(disturbed_med / stable_med)

    if deltas:
        median_delta = statistics.median(deltas)
        median_sign = 1 if median_delta > 0 else (-1 if median_delta < 0 else 0)
        agreeing = sum(
            1 for d in deltas
            if (1 if d > 0 else (-1 if d < 0 else 0)) == median_sign
        )
        consistency_ratio = agreeing / len(deltas)
    else:
        median_delta = None
        consistency_ratio = None

    return {
        "n_regions_paired": len(deltas),
        "median_wall_ms_delta": median_delta,
        "median_wall_ms_ratio": statistics.median(ratios) if ratios else None,
        "consistency_ratio": consistency_ratio,
    }


def _arm_medians(rows: Sequence[Mapping]) -> dict:
    """`rows` 里各臂的中位墙钟——不看 `call_index`,调用方决定喂哪一批行。

    `_summarize_scope` 拿它算两格对称的报告:`first_observation`(喂首次
    观测)与 `repeat_observation`(喂重复观测,codex #T-EX3 F4)——design
    §9.1「并比较后续相对首次的变化」要求能直接读到重复观测各臂的绝对中位数,
    不是只有配对后的差值/比值,否则「重复观测比首次快了几倍」这个最能说明
    前缀确实被复用的信号读不出来。
    """
    by_arm: dict[str, list[float]] = {}
    for row in rows:
        wall = _numeric_wall_ms(row)
        if wall is None:
            continue
        by_arm.setdefault(row.get("arm"), []).append(wall)
    return {
        "stable_median_wall_ms": (
            statistics.median(by_arm[ARM_STABLE]) if by_arm.get(ARM_STABLE) else None
        ),
        "disturbed_median_wall_ms": (
            statistics.median(by_arm[ARM_DISTURBED])
            if by_arm.get(ARM_DISTURBED) else None
        ),
    }


def _summarize_scope(
    *, repeat_rows: Sequence[Mapping], first_rows: Sequence[Mapping],
) -> dict:
    pairs = _summarize_region_pairs(repeat_rows)
    pairs["first_observation"] = _arm_medians(first_rows)
    pairs["repeat_observation"] = _arm_medians(repeat_rows)
    return pairs


def _verdict(overall: Mapping, *, total_ok: int, total_expected: int) -> str:
    """三格结论的判据(design §9.1 三格结论)。

    判据本身——用两臂配对差的中位数与区组间一致性、样本不足/失败过半判
    `undetermined`——两份 spec(design §9.1、计划 T-EX3)都没有给出具体的
    一致性阈值或「过半失败」的精确定义,这是实现自定(spec 对此沉默,自定
    本身合法,但不该冒充 spec 原文引用;codex #T-EX3 F7)。阈值的文档落点
    留给 T-EX11 写进 README,不在这里重复。

    * `total_expected == 0`(没有非预热行可看)或**过半**非预热行不是
      `"ok"`(含失败与本地缓存出口都不算数,`_SUCCESS_STATUS` 之外的一切)⇒
      `"undetermined"`——样本被污染到不足以支撑任何结论;
    * 可配对的区组数 < `_MIN_PAIRED_REGIONS_FOR_VERDICT`(两个)⇒
      `"undetermined"`——連一个跨区组的一致性都算不出来;
    * 否则:区组间中位差的**符号**一致(`consistency_ratio` ≥ 0.75)且
      `disturbed` 比 `stable` 慢(`median_wall_ms_delta > 0`)⇒
      `"time_benefit"`;否则(差值为负、为零,或跨区组方向不一致)⇒
      `"no_discernible_benefit"`。
    """
    if total_expected == 0:
        return "undetermined"
    if total_ok < total_expected / 2:
        return "undetermined"
    n_paired = overall.get("n_regions_paired") or 0
    if n_paired < _MIN_PAIRED_REGIONS_FOR_VERDICT:
        return "undetermined"
    median_delta = overall.get("median_wall_ms_delta")
    consistency = overall.get("consistency_ratio")
    if median_delta is None or consistency is None:
        return "undetermined"
    if median_delta > 0 and consistency >= _CONSISTENCY_THRESHOLD:
        return "time_benefit"
    return "no_discernible_benefit"


def summarize_probe(rows: Sequence[Mapping]) -> dict:
    """一批探针结果行的汇总报告。零假设检验、零外部依赖,纯算术 + 分组。

    分桶顺序(每一行恰好落进其中一桶,互斥):

    1. `is_warmup` 为真 ⇒ 预热行,单列 `warmup_row_count`,**不进任何统计**
       (design §9.1「预热成本单列」);
    2. `status == "cache_hit"` ⇒ 本地缓存出口,单列 `local_cache_exit_rows`
       (P3-3 拍板:这是"不应出现"的计数,不是命中率,也不进主统计);
    3. `status != "ok"`(含 `"error"`/`"cancelled"`/`None`/任何非 `"ok"` 的值)
       ⇒ 失败或格式不符,单列 `failed_row_count`;
    4. 其余(`status == "ok"`)按 `call_index == 0` 分「首次观测」与「重复
       观测」——首次观测**不是**已验证冷缓存(§9.1),只在 `first_observation`
       里单独报中位墙钟,不与重复观测混在一起算主统计。

    主统计(`overall`/`by_tier`)只用第 4 类里 `call_index >= 1` 的重复观测,
    按 `(tier, block_index)` 区组配对两臂——`by_tier` 是每个长度档各自的
    这份报告,`overall` 是把全部档位的行铺平后重新配对(区组因此变成
    `(tier, block_index)` 的笛卡尔积,不是把三档的中位数再取一次中位数）。

    `cached_tokens_observed`:provider 不回 `usage.cached_tokens` 时整批
    都是 `None`,一律折成 `None`——**不折成 0**;有观测时报 `{"n": ...,
    "sum": ...}` 这一对计数,不是"命中率"(设计 §10.1「没有 token/cached
    数据时……保留 unknown；不输出估计"缓存节约费用""）。这个计数横跨全部
    非预热行(含失败行——它们仍然可能带回一个 `usage`),因为它只是一份
    附录统计,与「这次调用算不算成功」是两件独立的事。

    `verdict` 见 `_verdict`。
    """
    warmup_rows = [r for r in rows if r.get("is_warmup")]
    non_warmup = [r for r in rows if not r.get("is_warmup")]
    local_cache_exit = [
        r for r in non_warmup if r.get("status") == _LOCAL_CACHE_EXIT_STATUS
    ]
    remaining = [
        r for r in non_warmup if r.get("status") != _LOCAL_CACHE_EXIT_STATUS
    ]
    ok_rows = [r for r in remaining if r.get("status") == _SUCCESS_STATUS]
    failed_rows = [r for r in remaining if r.get("status") != _SUCCESS_STATUS]

    first_obs = [r for r in ok_rows if r.get("call_index") == 0]
    repeat_obs = [
        r for r in ok_rows
        if isinstance(r.get("call_index"), int)
        and not isinstance(r.get("call_index"), bool)
        and r["call_index"] >= 1
    ]

    tiers = sorted({r["tier"] for r in rows if r.get("tier") is not None})
    by_tier = {
        tier: _summarize_scope(
            repeat_rows=[r for r in repeat_obs if r.get("tier") == tier],
            first_rows=[r for r in first_obs if r.get("tier") == tier],
        )
        for tier in tiers
    }
    overall = _summarize_scope(repeat_rows=repeat_obs, first_rows=first_obs)

    cached_values = [
        r["cached_tokens"] for r in non_warmup
        if isinstance(r.get("cached_tokens"), int)
        and not isinstance(r.get("cached_tokens"), bool)
    ]
    cached_tokens_observed = (
        {"n": len(cached_values), "sum": sum(cached_values)}
        if cached_values else None
    )

    verdict = _verdict(overall, total_ok=len(ok_rows), total_expected=len(non_warmup))
    if verdict not in VERDICTS:
        # A runtime invariant, not just a docstring promise: `_verdict` is
        # private and every branch in it today returns one of `VERDICTS`, but
        # a future edit that adds a fourth word (or a typo) must fail loudly
        # here rather than hand a report-writer an out-of-band string that
        # slides past every consumer's `in VERDICTS` check silently.
        raise ValueError(
            f"summarize_probe: _verdict returned {verdict!r}, not one of "
            f"{VERDICTS!r}"
        )

    return {
        "row_count_total": len(rows),
        "warmup_row_count": len(warmup_rows),
        "local_cache_exit_rows": len(local_cache_exit),
        "failed_row_count": len(failed_rows),
        "first_observation_row_count": len(first_obs),
        "repeat_observation_row_count": len(repeat_obs),
        "cached_tokens_observed": cached_tokens_observed,
        "by_tier": by_tier,
        "overall": overall,
        "verdict": verdict,
    }


# --- manifest 拼装 -------------------------------------------------------------

def probe_manifest_facts(
    plan: Sequence[Mapping], sample: Mapping, seed: int,
    *, arms: Sequence[str] = DEFAULT_ARMS,
    calls_per_series: int = DEFAULT_CALLS_PER_SERIES,
) -> dict:
    """`reflect_manifest.build_manifest(channel="e1", **facts)` 缺的那一半事实。

    这个函数只管**这个模块能算出来**的那部分:`seed`/`sample_digest`/
    `matrix`(四个必填基数 + 一个额外的 `tier_chars` 分格明细)/`arms`/
    `order`/`optimization_by_arm`/`common_baseline`/`arm_order_seed`。
    T-EX4 把返回值与自己算出来的 `channel`/`code_sha`/`started_at`/
    `finished_at`/`stopped_by_budget`/`budgets` 合并后调
    `reflect_manifest.build_manifest`——那才是**全量**必填键校验的地方,这个
    函数本身不知道、也不需要知道 `reflect_manifest` 的必填键表。

    `optimization_by_arm` 与 `common_baseline` 在 E1 里恒是
    `"not_applicable"`:E1 不跑 reflect,两臂的"策略"是同一个 reflect 协议
    (`REASONING_REFLECT_V2_ENABLED` 与 `reasoning_reflect_optimization` 这
    两个字段在 E1 的 `RuntimeModelProvider` 调用路径上根本不参与)，写成一个
    短码而不是留空,是因为 `reflect_manifest` 的隐私闸不认 `None` 之外的
    "空值占位"，而这两个键在 `MANIFEST_KEYS` 闭集里、又不在 E1 的必填集里,
    留着不写也合法——写上是为了让读 manifest 的人一眼看出"这两个键对 E1
    没有意义",而不是怀疑遗漏。`arm_order_seed` 在 E1 里就是 `seed` 本身
    (design M4:「E1 的区组随机则必须有种子」，与 E3 的"无种子"是两回事)。
    """
    tiers = sorted({row["tier"] for row in plan})
    blocks = len({row["block_index"] for row in plan})
    all_tier_chars = sample_tier_chars(sample)
    matrix = {
        "tiers": len(tiers),
        "blocks": blocks,
        "arms": len(arms),
        "calls_per_series": calls_per_series,
        # codex #T-EX3 F13: 只写 `plan` 实际跑过的档位,不是样本声明的全部
        # 档位——`matrix["tiers"]` 数的是前者,`tier_chars` 曾经报后者,一份
        # `probe_plan(tiers=("short",))` 的计划会让这两个数字对不上账。
        "tier_chars": {tier: all_tier_chars[tier] for tier in tiers},
    }
    return {
        "seed": seed,
        "sample_digest": sample_digest(sample),
        "matrix": matrix,
        "arms": list(arms),
        "order": ["tier", "block_index", "series_index", "call_index"],
        "optimization_by_arm": {arm: "not_applicable" for arm in arms},
        "common_baseline": "not_applicable",
        "arm_order_seed": seed,
    }
