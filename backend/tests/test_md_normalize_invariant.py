import unicodedata

from app.services.knowhow.md_normalize import (
    content_signature,
    content_invariant,
    rule_normalize,
    _image_refs,
    _inline_code_refs,
    _is_content_char,
)

def test_format_only_change_passes():
    before = "A. 考量\n\t• 增大 R： 变慢\n\t• 增大 C： 变化"
    after = "**A. 考量**\n\n- 增大 R:变慢\n- 增大 C:变化"   # 只改格式 + 半角冒号 + 去空格
    assert content_invariant(before, after) is True

def test_punctuation_normalization_is_allowed():
    assert content_invariant("增大 R： 导致（Transition）", "增大 R:导致(Transition)") is True

def test_dropped_sentence_is_rejected():
    assert content_invariant("第一句。第二句。", "第一句。") is False

def test_changed_number_is_rejected():
    assert content_invariant("频率1800M", "频率1000M") is False

def test_reordered_bullets_rejected():
    assert content_invariant("- 甲\n- 乙", "- 乙\n- 甲") is False

def test_dropped_word_cjk_rejected():
    assert content_invariant("不是让信号走得慢", "是让信号走得慢") is False

def test_image_ref_must_survive():
    assert content_invariant("见 ![图](asset://x1)", "见 图示") is False
    assert content_invariant("见 ![图](asset://x1)", "**见** ![图](asset://x1)") is True

def test_signature_drops_markers_and_punct_keeps_words():
    assert content_signature("\t• 增大 R： 变慢（快）") == content_signature("- 增大R:变慢(快)")

def test_section_header_letter_is_content_but_nested_letter_is_format():
    # 顶格 A. 的 A 算内容；缩进 a. 的 a 算格式
    assert content_signature("A. 考量") == content_signature("**A. 考量**")   # A 保留、两边一致
    assert content_signature("A. 考量") != content_signature("考量")            # 丢了 A → 不同
    # 缩进 a. 剥掉——但"缩进"是相对该 cell 自己的 cell_min 而言（I4：与
    # _normalize 的 depth==cell_min 对齐，不再硬编码 depth==0），所以要在一个
    # 真有顶格结构的 cell 里比较，nested a. 才真正相对"缩进"：
    assert content_signature("A. 头\n\ta. 子项") == content_signature("A. 头\n- 子项")

def test_cross_line_content_shift_rejected():
    # Critical:字符跨行迁移，即使全局字符序列不变（拼接后都是 "ABCD"），signature 也必须不同
    assert content_signature("AB\nCD") != content_signature("A\nBCD")

def test_cross_line_digit_shift_in_bullets_rejected():
    # Critical:数字 "8" 从第二条目开头跳到第一条目末尾，flat signature 曾经无法察觉
    assert content_invariant("- 频率18MHz\n- 增益0dB", "- 频率1\n- 8MHz增益0dB") is False

def test_cross_line_char_shift_in_bullets_rejected():
    # Critical:"慢" 从第一条目末尾跳到第二条目开头，flat signature 曾经无法察觉
    assert content_invariant(
        "- 增大电阻会变慢\n- 增大电容会变化",
        "- 增大电阻会变\n- 慢增大电容会变化",
    ) is False


# ---------------------------------------------------------------------------
# I1 (Important): false accepts -- relocated code/image (position invisible
# because the old signature stripped them into separate, unordered lists),
# and ordered-marker digits silently destroyed (stripped as "format" when
# they are actually content -- rule_normalize always preserves them verbatim,
# so an LLM renumbering is a real content change, not a reformat).
# ---------------------------------------------------------------------------


def test_relocated_code_block_rejected():
    before = "段落A\n```\nfoo\n```\n段落B"
    after = "```\nfoo\n```\n段落A\n段落B"          # same code, same prose, moved to front
    assert content_invariant(before, after) is False


def test_relocated_image_rejected():
    # a figure moving from step 3 to step 1 in a procedure column
    before = "步骤1\n步骤2\n步骤3\n![图](asset://x1)"
    after = "![图](asset://x1)\n步骤1\n步骤2\n步骤3"
    assert content_invariant(before, after) is False


def test_ordered_marker_renumbering_rejected():
    # 2018./2020. renumbered to 1./2. -- digits are content, not format.
    before = "2018. 第一件事\n2020. 第二件事"
    after = "1. 第一件事\n2. 第二件事"
    assert content_invariant(before, after) is False


def test_legitimate_reformat_with_image_and_fence_still_passes():
    """No over-rejection: a format-only reformat (bold + blank-line-before-
    fence) that keeps the image and the fence's content/position unchanged
    must still pass."""
    before = "见 ![图](asset://x1) 效果\n```\ncode\n```"
    after = "**见** ![图](asset://x1) 效果\n\n```\ncode\n```"
    assert content_invariant(before, after) is True


# ---------------------------------------------------------------------------
# P1 (real false accept): `_FENCE_RE` only matched backtick fences, even
# though `_normalize`/`_find_verbatim_spans` recognize ``~~~`` as an equally
# valid fence marker (see `_FENCE_OPEN_RE = re.compile(r"^(`{3,}|~{3,})")`).
# `_tokenize_blocks` therefore left ``~~~`` fence interiors untokenized, so an
# operator change *inside* a tilde-fenced code block was invisible to
# `content_signature` -- confirmed: `content_invariant("~~~\na+b\n~~~",
# "~~~\na-b\n~~~")` returned True (a `+` silently became a `-` inside code),
# while the byte-identical backtick case correctly returned False.
# ---------------------------------------------------------------------------


def test_tilde_fence_content_change_is_rejected():
    assert content_invariant("~~~\na+b\n~~~", "~~~\na-b\n~~~") is False


def test_backtick_fence_content_change_is_rejected():
    # the already-working case, pinned alongside the tilde fix so a future
    # regression that special-cases tilde without keeping backtick working
    # would be caught here too.
    assert content_invariant("```\na+b\n```", "```\na-b\n```") is False


def test_legitimate_reformat_with_image_and_tilde_fence_still_passes():
    """No over-rejection for the tilde variant either: mirrors
    test_legitimate_reformat_with_image_and_fence_still_passes above but with
    a ~~~ fence -- a format-only change around an untouched tilde-fenced
    block must still pass."""
    before = "见 ![图](asset://x1) 效果\n~~~\ncode\n~~~"
    after = "**见** ![图](asset://x1) 效果\n\n~~~\ncode\n~~~"
    assert content_invariant(before, after) is True


# ---------------------------------------------------------------------------
# I4 (Important): content_signature must use depth == cell_min (matching
# _normalize exactly), not a hardcoded depth == 0 -- otherwise a cell whose
# every line happens to be indented (e.g. a whole cell pasted one tab-stop
# deep) disagrees with what _normalize actually renders: _normalize DOES
# recognize the shared-baseline "A." line as a section header (cell_min-
# relative) and bolds it, but the old hardcoded-0 signature would not, so
# the legitimate round-trip was spuriously rejected.
# ---------------------------------------------------------------------------


def test_all_indented_cell_round_trips():
    raw = "\tA. 考量\n\t\t• 增大 R\n\t\t• 增大 C"
    assert content_invariant(raw, rule_normalize(raw)) is True


# ---------------------------------------------------------------------------
# content_signature must scan fences LINE-ORIENTED with CommonMark fence-length
# semantics. The old `_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)` pairs the
# NEAREST triple backtick, so a 4-backtick block whose interior contains an
# inner ``` example gets mis-sliced into fragments; an edit inside the code
# (a+b -> a-b) could then leave the fragmented code-block list AND the
# alphanumeric signature unchanged -> content_invariant falsely returns True on
# a real code change. Confirmed pre-fix on the UNCLOSED 4-backtick variant:
# the old regex matched only up to the inner ```, leaving a+b/a-b OUTSIDE the
# "code block", where +/- are stripped as punctuation, so the signatures were
# identical and content_invariant("````\n...a+b", "````\n...a-b") returned True.
#
# The fix: an opening fence is a line whose stripped form starts with a run of
# >= 3 backticks or >= 3 tildes (record char + run length); the closing fence
# is the SAME char with run length >= the opening run; the inner ``` (len 3)
# never closes a ```` (len 4) fence; an unclosed run extends to end of input.
# ---------------------------------------------------------------------------

_BT4 = "`" * 4
_BT3 = "`" * 3
_TL4 = "~" * 4
_TL3 = "~" * 3


def test_four_backtick_fence_with_inner_triple_content_change_rejected():
    before = f"{_BT4}\ncode {_BT3}x{_BT3} more\na+b\n{_BT4}"
    after = f"{_BT4}\ncode {_BT3}x{_BT3} more\na-b\n{_BT4}"
    assert content_invariant(before, after) is False


def test_four_tilde_fence_with_inner_triple_content_change_rejected():
    before = f"{_TL4}\ncode {_TL3}x{_TL3} more\na+b\n{_TL4}"
    after = f"{_TL4}\ncode {_TL3}x{_TL3} more\na-b\n{_TL4}"
    assert content_invariant(before, after) is False


def test_unclosed_four_backtick_fence_content_change_rejected():
    # the confirmed pre-fix false-accept (returned True before the fix).
    before = f"{_BT4}\ncode {_BT3}x{_BT3} more\na+b"
    after = f"{_BT4}\ncode {_BT3}x{_BT3} more\na-b"
    assert content_invariant(before, after) is False


def test_format_only_change_around_untouched_four_backtick_fence_passes():
    # No over-rejection: a format-only change (bold + blank line) AROUND an
    # untouched 4-backtick fence (inner ``` preserved) must still pass.
    before = f"见 ![图](asset://x1) 效果\n{_BT4}\ncode {_BT3}x{_BT3} more\n{_BT4}"
    after = f"**见** ![图](asset://x1) 效果\n\n{_BT4}\ncode {_BT3}x{_BT3} more\n{_BT4}"
    assert content_invariant(before, after) is True


# ---------------------------------------------------------------------------
# F2 — the image matcher must handle BALANCED PARENTHESES in the destination.
# The old `_IMAGE_RE`'s `\([^)]*\)` stops at the FIRST `)`, so
# `![x](https://a/b_(c).png)` is captured as `![x](https://a/b_(c)` -- the URL
# tail `.png)` falls OUTSIDE both the byte-exact image comparison AND the opaque
# IMG token, landing in loose text where the signature strips punctuation. Net
# effect: an LLM corrupting the tail (a punctuation-only edit the signature is
# blind to) passes the invariant. The fix replaces the destination match with a
# balanced-paren scanner (depth+1 on `(`, -1 on `)`, end at depth 0; unterminated
# -> not an image, treated as plain text) used EVERYWHERE the module matches
# images (`_image_refs` + the signature's in-place tokenization -- one matcher).
# ---------------------------------------------------------------------------


def test_balanced_paren_image_is_one_full_ref():
    # RED before the fix: the old regex truncates this to `![x](https://a/b_(c)`.
    md = "![x](https://a/b_(c).png)"
    assert _image_refs(md) == ["![x](https://a/b_(c).png)"]


def test_nested_parens_image_is_one_full_ref():
    md = "![y](http://a((b))c)"
    assert _image_refs(md) == ["![y](http://a((b))c)"]


def test_balanced_paren_image_tail_punctuation_corruption_rejected():
    # THE false accept: a PUNCTUATION-ONLY tail change ('.'->'-') is invisible to
    # the signature, and the tail was outside the old captured ref, so both checks
    # missed it -> old content_invariant returned True. The balanced scanner now
    # captures the whole URL, so the byte-exact ref comparison catches it.
    before = "![x](https://a/b_(c).png)"
    after = "![x](https://a/b_(c)-png)"
    assert content_invariant(before, after) is False


def test_balanced_paren_image_alnum_tail_change_rejected():
    # the task's named example: `.png` -> `.pn` inside the balanced-paren URL tail.
    before = "见 ![x](https://a/b_(c).png)"
    after = "见 ![x](https://a/b_(c).pn)"
    assert content_invariant(before, after) is False


def test_unterminated_image_paren_is_not_an_image():
    # unterminated destination -> no image match, treated as plain text (safer than
    # guessing a boundary). Both refs empty.
    assert _image_refs("![x](http://a") == []


def test_plain_asset_image_ref_unaffected_regression():
    # regression: ordinary asset:// images (no inner parens) are still one ref,
    # and a format-only reformat around them still passes.
    assert _image_refs("见 ![图](asset://x1)") == ["![图](asset://x1)"]
    assert content_invariant("见 ![图](asset://x1)", "**见** ![图](asset://x1)") is True


# ---------------------------------------------------------------------------
# F3 (review) — the invariant must BYTE-PROTECT inline-link and autolink
# DESTINATIONS, exactly as it already does for images. Before this fix ONLY
# images (`![alt](dest)`) got the byte-exact ref comparison + opaque token;
# plain links `[text](dest)` and autolinks `<scheme://...>` fell into loose
# prose where `content_signature` strips punctuation, so a punctuation-only
# edit to a URL (`https://host/a-b` -> `https://host/ab`) was invisible and the
# invariant falsely returned True. Fix: ONE shared scanner (the image matcher
# generalized, parameterized by the `!` prefix) also finds inline links and
# autolinks; `content_invariant` byte-compares the ordered link/autolink list
# AND the signature tokenizes them in place. Reference-style `[label]: dest`
# definitions are gate-refused for the rule path (`_starts_block_construct`'s
# `_REF_LINK_DEF_RE`) and rare in LLM inputs, and are a LINE construct the
# character scanner does not parse -- documented, not added.
# ---------------------------------------------------------------------------


def test_inline_link_dest_punctuation_edit_rejected():
    # THE codex example: a punctuation-only change inside the link destination
    # (`a-b` -> `ab`, dropping the `-`) is invisible to the punctuation-relaxed
    # signature. RED before the fix (links were unprotected) -> old True.
    before = "见 [docs](https://host/a-b)"
    after = "见 [docs](https://host/ab)"
    assert content_invariant(before, after) is False


def test_autolink_tail_change_rejected():
    # changing an autolink `<https://a/b-c>` tail by a PUNCTUATION-ONLY edit
    # (dropping the `-`) is invisible to the punctuation-relaxed signature -- RED
    # before the fix (autolinks were unprotected). The byte-exact autolink ref now
    # catches it.
    before = "参考 <https://a/b-c> 链接"
    after = "参考 <https://a/bc> 链接"
    assert content_invariant(before, after) is False


def test_format_only_reformat_around_untouched_link_passes():
    # a legitimate format-only change AROUND an unchanged link (bolding a word) must
    # still pass -- the link is byte-identical on both sides.
    before = "见 [文档](http://x/y) 效果"
    after = "**见** [文档](http://x/y) 效果"
    assert content_invariant(before, after) is True


def test_image_and_link_mixed_both_tokenized_order_preserved():
    # a cell with BOTH an image and a link: a PUNCTUATION-ONLY edit to the LINK's
    # destination (`y-z` -> `yz`) inside the mixed cell is invisible to the loose
    # signature -- RED before the fix -> caught by the link ref comparison now.
    before = "![图](asset://x1) 与 [文档](http://y-z)"
    after = "![图](asset://x1) 与 [文档](http://yz)"
    assert content_invariant(before, after) is False
    # a format-only reformat that touches neither the image nor the link passes.
    assert content_invariant(before, "**![图](asset://x1)** 与 [文档](http://y-z)") is True


def test_link_refs_captures_links_and_autolinks_not_images():
    # the shared scanner classifies: `_link_refs` returns inline links + autolinks
    # (images stay with `_image_refs`), each verbatim and in order.
    from app.services.knowhow.md_normalize import _link_refs
    md = "见 ![图](asset://x1) 与 [文档](http://y/z) 和 <https://a/b>"
    assert _link_refs(md) == ["[文档](http://y/z)", "<https://a/b>"]
    assert _image_refs(md) == ["![图](asset://x1)"]


def test_link_with_balanced_paren_dest_is_one_full_ref():
    # the link destination shares the image's balanced-paren + escaped-paren
    # scanner: an inner balanced `()` is part of the dest, so a tail edit past it is
    # caught.
    from app.services.knowhow.md_normalize import _link_refs
    assert _link_refs("[x](https://a/b_(c).png)") == ["[x](https://a/b_(c).png)"]
    before = "[x](https://a/b_(c).png)"
    after = "[x](https://a/b_(c)-png)"
    assert content_invariant(before, after) is False


# ---------------------------------------------------------------------------
# F3 — the balanced-paren scanner must HONOR ESCAPED PARENS. A backslash escapes
# the next char: `\)` and `\(` neither close nor open depth. The old scanner
# treats `\)` as a real terminator, so `![x](foo\)bar)` is captured as
# `![x](foo\)` -- the true tail `bar)` falls outside the IMG ref, so corrupting it
# (a change the signature is blind to) passes the invariant. Fix: skip the char
# after a backslash inside the destination scan. Unterminated still -> no match
# (plain text). Python-only (the module has no TS twin for the invariant).
# ---------------------------------------------------------------------------


def test_escaped_close_paren_does_not_terminate_destination():
    # RED before the fix: the old scanner stops at `\)` -> `![x](foo\)`.
    md = "![x](foo\\)bar)"
    assert _image_refs(md) == ["![x](foo\\)bar)"]


def test_escaped_close_paren_tail_corruption_rejected():
    # THE false accept: with the whole ref captured, deleting the REAL closing `)`
    # makes the candidate an UNTERMINATED image (no match) while the original is one
    # image -> the byte-exact ref lists differ -> invariant False. The old scanner
    # captured only `![x](foo\)`, leaving `bar)` in loose text where the tail edit
    # was invisible.
    before = "![x](foo\\)bar)"
    after = "![x](foo\\)bar"          # real closing `)` deleted
    assert content_invariant(before, after) is False


def test_escaped_open_paren_inside_destination_handled():
    # `\(` must not open depth either -> the first real `)` closes the whole ref.
    md = "![x](a\\(b)"
    assert _image_refs(md) == ["![x](a\\(b)"]


def test_escaped_paren_unterminated_is_not_an_image():
    # a lone escaped `\)` with no real terminator -> unterminated -> not an image.
    assert _image_refs("![x](foo\\)bar") == []


# ---------------------------------------------------------------------------
# F2 (this batch) — the invariant must treat SYMBOLS as CONTENT while keeping the
# user-approved PUNCTUATION relaxation. `_is_content_char` previously kept only
# alnum/CJK, so a semantic operator or emoji swap that leaves letters/CJK intact
# (`V=IR`->`V+IR`, `x<0`->`x>0`, `✅`->`❌`) had an identical signature and slipped
# through as "format-only". Fix: content = alnum/CJK PLUS every Unicode Symbol
# category (S*: Sm math, So other/emoji, Sc currency, Sk modifier). Relaxed
# (stripped) stays whitespace + all Punctuation categories (P*) exactly as before.
#
# Category table for the tricky ones (verified via unicodedata.category):
#   =  Sm   +  Sm   <  Sm   >  Sm   →  Sm      (math symbols -> CONTENT now)
#   ✅ So   ❌ So   ￥ Sc   $  Sc              (emoji/currency -> CONTENT now)
#   *  Po   \  Po   ：Po   、Po   （Ps  ）Pe   (punctuation -> still RELAXED)
# `*`/`_`/backtick are additionally pre-stripped by `_EMPHASIS_RE` before the
# char filter, so the S* rule never accidentally promotes an emphasis marker.
# ---------------------------------------------------------------------------


def test_category_table_is_accurate_for_tricky_chars():
    # pins the comment table above so a future Unicode/table drift is caught.
    for ch in "=+<>→":
        assert unicodedata.category(ch) == "Sm", ch
    for ch in "✅❌":
        assert unicodedata.category(ch) == "So", ch
    for ch in "￥$":
        assert unicodedata.category(ch) == "Sc", ch
    for ch in "*\\：、":
        assert unicodedata.category(ch).startswith("P"), ch


def test_symbol_categories_are_content_punctuation_is_relaxed():
    # every Unicode Symbol category counts as content; every Punctuation category
    # is relaxed (not content).
    for ch in "=+<>→✅❌￥$":
        assert _is_content_char(ch) is True, ch
    for ch in "*\\：、（）,.;:!":
        assert _is_content_char(ch) is False, ch
    # alnum/CJK unchanged.
    assert _is_content_char("R") is True
    assert _is_content_char("大") is True


def test_math_operator_swap_now_rejected():
    # the codex example: `V=IR` -> `V+IR`. Both sides keep V,I,R -- only the
    # operator changed, which the old alnum-only signature was blind to.
    assert content_invariant("V=IR", "V+IR") is False


def test_inequality_operator_swap_now_rejected():
    # `x<0` -> `x>0`: a semantic negation the old signature accepted.
    assert content_invariant("x<0", "x>0") is False


def test_emoji_swap_now_rejected():
    # `✅` -> `❌`: opposite meaning, same (empty) alnum signature before the fix.
    assert content_invariant("步骤一 ✅", "步骤一 ❌") is False


def test_currency_symbol_swap_now_rejected():
    assert content_invariant("成本 ￥100", "成本 $100") is False


def test_punctuation_relaxation_still_passes_after_symbol_split():
    # the user-approved punctuation relaxation must stay intact: full/half-width
    # colon, brackets, and comma normalization are format-only.
    assert content_invariant("增大 R：", "增大 R:") is True
    assert content_invariant("（1W1S）", "(1W1S)") is True
    assert content_invariant("甲、乙", "甲,乙") is True


def test_real_corpus_symbol_line_unchanged_passes():
    # a real-corpus formula-ish line the LLM keeps verbatim: symbols now count as
    # content, but unchanged->unchanged is trivially equal -> still passes.
    line = "setup_slack + delay > margin"
    assert content_invariant(line, line) is True


def test_symbol_preserved_reformat_still_passes():
    # a genuine format-only reformat that PRESERVES the symbols must still pass --
    # the symbol split must not over-reject legitimate formatting changes.
    before = "\t• x < 0 时\n\t• y > 0 时"
    after = "- x < 0 时\n- y > 0 时"
    assert content_invariant(before, after) is True


# ---------------------------------------------------------------------------
# Combining marks (Unicode category M*) are CONTENT in the invariant. The old
# `_is_content_char` kept only alnum/CJK/symbols, so every M* code point was
# stripped as "format-only" -- meaning a DECOMPOSED `é` (e + U+0301 combining
# acute) had the same signature as `e`, an Indic consonant kept its signature
# when its matra (U+093E, spacing combining mark Mc) was deleted, and an emoji
# lost its variation selector (VS16 U+FE0F, Mn) invisibly. Each is a real
# content change an LLM could sneak past. Fix: `_is_content_char` also returns
# True for any category starting with `M` (Mn nonspacing, Mc spacing combining,
# Me enclosing).
#
# The invariant compares RAW code points (no Unicode normalization), so NFC `é`
# (U+00E9, a letter Ll) and NFD `é` (e + U+0301) DIFFER -> content_invariant is
# False. That is FAIL-CLOSED (an LLM changing Unicode normalization is rejected
# and the deterministic rule path takes over) and is the intended semantics.
# ---------------------------------------------------------------------------


def test_combining_mark_categories_are_content():
    # pins the tricky category assignments the comment relies on, then that each
    # counts as content under the fixed classifier.
    assert unicodedata.category("́") == "Mn"   # combining acute accent
    assert unicodedata.category("️") == "Mn"   # VARIATION SELECTOR-16
    assert unicodedata.category("ा") == "Mc"   # Devanagari vowel sign AA (matra)
    for ch in ("́", "️", "ा"):
        assert _is_content_char(ch) is True, ch


def test_decomposed_accent_is_not_droppable():
    # decomposed `café` (e + U+0301) vs `cafe`: the combining acute is a real
    # character -> the two must differ (was a false accept before the fix).
    assert content_invariant("café", "cafe") is False


def test_indic_matra_is_not_droppable():
    # `पानी` minus its first matra (U+093E) -> different meaning, must be rejected.
    assert content_invariant("पानी", "पनी") is False


def test_emoji_variation_selector_is_not_droppable():
    # `☂️` (U+2602 + VS16 U+FE0F, the emoji presentation) vs bare `☂` (text
    # presentation): dropping the selector changes the rendered glyph.
    assert content_invariant("☂️", "☂") is False


def test_nfc_vs_nfd_normalization_change_is_fail_closed():
    # documented semantics: the invariant does NOT Unicode-normalize, so NFC `é`
    # (U+00E9, letter) and NFD `é` (e + U+0301) are different code point streams
    # -> content_invariant is False (fail-closed -> deterministic rule fallback).
    assert content_invariant("café", "café") is False


def test_combining_mark_preserved_reformat_still_passes():
    # no over-rejection: a format-only reformat that PRESERVES the combining mark
    # (bullet-glyph swap only) must still pass -- the M* content rule must not
    # reject legitimate formatting changes.
    before = "\t• café 说明"
    after = "- café 说明"
    assert content_invariant(before, after) is True


# ---------------------------------------------------------------------------
# Image alt text must parse BALANCED / ESCAPED brackets. The old alt matcher
# `!\[[^\]]*\]\(` stops at the FIRST `]`, so `![diagram[final]](asset://a123)`
# is not recognized as an image at all -- the alt regex fails (`]` is followed by
# `]`, not `(`), the whole construct falls into loose text, and deleting the
# image while keeping the alt text passes the invariant. Fix: `_find_images`
# parses the alt with a balanced-bracket scanner (depth counter, backslash
# escapes; unterminated -> no match) symmetric to the destination's paren scanner,
# then requires the immediate `](` before handing off to the destination scan.
# ---------------------------------------------------------------------------


def test_nested_bracket_alt_is_one_full_image():
    # RED before the fix: the old alt regex bailed at the first `]` -> [] (not an
    # image). The balanced-bracket alt scanner captures the whole construct.
    md = "![diagram[final]](asset://a123)"
    assert _image_refs(md) == ["![diagram[final]](asset://a123)"]


def test_nested_bracket_alt_image_deletion_rejected():
    # THE codex false accept: the image silently becomes a plain LINK (the leading
    # `!` deleted). Every content char is identical (`!`/`[`/`]`/`(`/`)` are all
    # punctuation the signature strips), so the OLD code -- which recognized NO
    # image on either side -- returned True. With the balanced-bracket alt scanner
    # `before` is one image and `after` is none -> the byte-exact ref list differs
    # -> the invariant correctly rejects the corruption.
    before = "![diagram[final]](asset://a123)"
    after = "[diagram[final]](asset://a123)"   # leading ! deleted: image -> link
    assert content_invariant(before, after) is False


def test_escaped_bracket_in_alt_is_one_full_image():
    # `\]` inside the alt is an escaped literal bracket -> does not close the alt;
    # the real `]` after `b` does. One full image.
    md = "![a\\]b](u)"
    assert _image_refs(md) == ["![a\\]b](u)"]


def test_nested_bracket_at_start_of_alt_is_one_full_image():
    # `![[x]](u)` -- the alt opens with a nested `[`; balanced depth requires both
    # `]` before the `](` handoff.
    md = "![[x]](u)"
    assert _image_refs(md) == ["![[x]](u)"]


def test_unterminated_bracket_alt_is_not_an_image():
    # an alt whose brackets never balance -> no match, treated as plain text
    # (safer than guessing a boundary), symmetric to the destination scanner.
    assert _image_refs("![a[b](u)") == []


def test_plain_alt_image_regression_still_one_ref():
    # regression: an ordinary alt with no nested brackets is still one ref, and a
    # format-only reformat around it still passes.
    assert _image_refs("见 ![图](asset://x1)") == ["![图](asset://x1)"]
    assert content_invariant("见 ![图](asset://x1)", "**见** ![图](asset://x1)") is True


# ---------------------------------------------------------------------------
# F4 — the SIGNATURE's line-oriented fence scanner must strip CommonMark
# blockquote container prefixes (`>` optionally followed by one space, repeated
# for nesting) before testing fence open/close. A fenced block nested inside a
# blockquote (`> ``` / `> a.b / `> ```) survives whitespace-stripping with its
# `>` intact, so the old scanner never recognized the fence; the quoted code got
# only the punctuation-relaxed signature, and `a.b` -> `a-b` inside quoted code
# (both `.` and `-` are punctuation P, stripped) passed content_invariant --
# confirmed a real false accept. The scanner now strips the `>` prefixes to see
# the fence and byte-compares the quoted block (prefixes included) in the
# code-block list. This is the SIGNATURE path only (the LLM path); the rule gate
# already refuses any blockquote cell for rule normalization.
# ---------------------------------------------------------------------------

_BT3S = "`" * 3


def test_blockquote_fenced_code_punct_change_rejected():
    # THE F4 hole: a `.` -> `-` inside a blockquote-nested fence. Both are
    # punctuation the signature relaxes, so ONLY fence protection (byte-compare of
    # the quoted block) catches it. Pre-fix returned True (false accept).
    before = f"> {_BT3S}\n> a.b\n> {_BT3S}"
    after = f"> {_BT3S}\n> a-b\n> {_BT3S}"
    assert content_invariant(before, after) is False


def test_nested_blockquote_fenced_code_change_rejected():
    # nested blockquote (`>> `): the scanner strips BOTH `>` markers before seeing
    # the fence. Same punctuation-only edit inside -> rejected.
    before = f">> {_BT3S}\n>> a.b\n>> {_BT3S}"
    after = f">> {_BT3S}\n>> a-b\n>> {_BT3S}"
    assert content_invariant(before, after) is False


def test_unquoted_fence_punct_change_still_rejected():
    # regression contrast: the SAME punctuation edit inside an UNQUOTED fence was
    # already rejected (fence recognized without any prefix stripping) -- pinned so
    # the blockquote fix does not accidentally narrow the unquoted path.
    before = f"{_BT3S}\na.b\n{_BT3S}"
    after = f"{_BT3S}\na-b\n{_BT3S}"
    assert content_invariant(before, after) is False


def test_blockquote_fence_untouched_while_prose_reformats_passes():
    # No over-rejection: the quoted fence stays byte-identical while the
    # surrounding prose is legitimately reformatted (bold + blank line) -> True.
    before = f"见 效果\n> {_BT3S}\n> a.b\n> {_BT3S}"
    after = f"**见** 效果\n\n> {_BT3S}\n> a.b\n> {_BT3S}"
    assert content_invariant(before, after) is True


def test_unquoted_fence_regression_content_still_byte_compared():
    # regression: an ordinary unquoted fence's word-level change stays rejected.
    before = f"{_BT3S}\ncode a\n{_BT3S}"
    after = f"{_BT3S}\ncode b\n{_BT3S}"
    assert content_invariant(before, after) is False


# ---------------------------------------------------------------------------
# F2 (code review): punctuation in a NUMERIC context is CONTENT, not relaxed.
# The blanket "all P* are relaxed" rule let engineering data silently mutate:
# a dropped decimal point (`-1.5 V` -> `15 V`), a mangled version string
# (`版本 1.2.3` -> `版本 123`), a flipped ratio (`3:1` -> `31`), a lost
# thousands separator (`1,000` -> `1000`) or percent sign (`50%` -> `50`) all
# kept the same alnum-only signature and passed as "format-only". The fix makes
# the signature scan neighbor-aware: `.` `,` `/` `:` between two digits, a `-`
# that is a unary minus / negative sign (its right neighbor a digit and its left
# neighbor not alphanumeric), and a `%` right after a digit are CONTENT. Every
# already-approved relaxation stays relaxed -- full/half-width colon in `R：`,
# brackets even next to digits in `（1W1S）`, the CJK `、`->`,` -- because none
# of those match a numeric context.
# ---------------------------------------------------------------------------


def test_dropped_decimal_point_rejected():
    # codex example: `-1.5 V` -> `15 V` drops BOTH the minus and the decimal.
    assert content_invariant("-1.5 V", "15 V") is False


def test_mangled_version_string_rejected():
    # codex example: `版本 1.2.3` -> `版本 123`; each `.` sits between digits.
    assert content_invariant("版本 1.2.3", "版本 123") is False


def test_ratio_colon_rejected():
    # codex example: `3:1` -> `31`; the digit:digit colon is a ratio, not format.
    assert content_invariant("3:1", "31") is False


def test_negative_number_variants_each_rejected():
    # the sign and the decimal each carry meaning: all three pairs differ.
    assert content_invariant("-1.5V", "-15V") is False   # decimal dropped
    assert content_invariant("-1.5V", "1.5V") is False   # sign dropped
    assert content_invariant("-15V", "1.5V") is False    # both differ


def test_unary_minus_after_equals_rejected():
    # `x = -3` -> `x = 3`: the `-` follows a non-alnum (space/`=`) then a digit,
    # so it is a negative sign -> content; dropping it flips the value.
    assert content_invariant("x = -3", "x = 3") is False


def test_thousands_separator_rejected():
    assert content_invariant("1,000", "1000") is False


def test_slash_ratio_between_digits_rejected():
    assert content_invariant("占空比 3/4", "占空比 34") is False


def test_percent_after_digit_rejected():
    assert content_invariant("良率 50%", "良率 50") is False


def test_prose_hyphen_between_letters_still_relaxed():
    # `a-b` (letters both sides) is a prose hyphen, NOT a unary minus -> relaxed.
    assert content_invariant("a-b", "ab") is True


def test_ratio_colon_rejected_while_prose_colon_relaxed():
    # the digit:digit ratio is content, but the full-width colon in `R：` (its
    # left neighbor a letter, not a digit) stays the user-approved relaxation.
    assert content_invariant("3:1", "31") is False
    assert content_invariant("增大 R：", "增大 R:") is True


def test_parens_next_to_digits_stay_relaxed_but_version_dots_are_content():
    # brackets are deliberately OUTSIDE the numeric set even flush against a
    # digit (`（1W1S）` normalization must still pass), while the dots in a
    # version string ARE content (each between two digits).
    assert content_invariant("（1W1S）", "(1W1S)") is True
    assert content_signature("1.2.3") != content_signature("123")


def test_numeric_punct_preserving_reformat_still_passes():
    # a genuine format-only reflow that KEEPS the negative number must not
    # over-reject: bullet -> dash marker, decimal/sign preserved on both sides.
    assert content_invariant("\t• 电压 -1.5 V", "- 电压 -1.5 V") is True


# ---------------------------------------------------------------------------
# F1 (review) — GFM BARE autolink LITERALS must be byte-protected, exactly like
# `<scheme:...>` angle autolinks and `[text](dest)` links already are. remark-gfm
# (the frontend renderer) turns bare URLs (`https://host/a-b`), `www.` links, and
# email addresses (`user.name@example.com`) in plain prose into clickable `<a>`.
# The old scanner only tokenized angle autolinks, so these bare literals fell into
# loose prose where `content_signature` relaxes punctuation -- a punctuation-only
# edit to a bare URL (`https://host/a-b` -> `https://host/ab`, dropping the `-`)
# was invisible and the invariant falsely returned True. Fix: the ONE shared
# scanner (`_find_link_constructs`) also finds bare literals (kind "autolink", so
# they share the "URL" token prefix + the `_link_refs` byte-exact comparison).
# Inline-link scan runs first (the cursor jumps past a consumed `[text](dest)`),
# bare-literal scan only sees the remainder -> no double-matching of a URL that is
# already inside a link/image/angle-autolink.
#
# Extent per GFM's simple rules: a bare URL starts at `http://`/`https://`/`www.`
# on a word boundary, runs to whitespace/`<`, then trailing punctuation
# (`?!.,:;` a `)` with no matching `(`, and -- a documented simplification for
# Chinese prose -- CJK sentence punctuation 。，、）！？) is trimmed back off as
# prose, NOT part of the URL. Python-only (the module has no TS twin for the
# invariant).
# ---------------------------------------------------------------------------


def test_bare_url_dest_punctuation_edit_rejected():
    # THE codex example: a bare URL in prose, a punctuation-only change inside it
    # (`a-b` -> `ab`, dropping the `-`) is invisible to the punctuation-relaxed
    # signature. RED before the fix (bare literals were unprotected) -> old True.
    before = "见 https://host/a-b"
    after = "见 https://host/ab"
    assert content_invariant(before, after) is False


def test_bare_www_literal_punct_edit_rejected():
    # a `www.` bare literal gets the same protection.
    before = "访问 www.example.com/a-b 了解"
    after = "访问 www.example.com/ab 了解"
    assert content_invariant(before, after) is False


def test_bare_email_literal_edit_rejected():
    # `user.name@example.com`: dropping the `.` in the local part is a
    # punctuation-only edit the loose signature was blind to (letters both sides,
    # not a numeric context) -- now the whole email is one byte-exact URL token.
    before = "联系 user.name@example.com 反馈"
    after = "联系 username@example.com 反馈"
    assert content_invariant(before, after) is False


def test_bare_url_trailing_period_is_not_part_of_the_token():
    # `见 https://a/b. 句号结尾` -- the trailing `.` (and the CJK `。`) are sentence
    # punctuation AROUND the URL, not part of it. Normalizing that punctuation
    # (`.`<->`。`) is a legit format-only change that must still pass...
    assert content_invariant("见 https://a/b. 句号结尾", "见 https://a/b。 句号结尾") is True
    # ...while a change INSIDE the URL body is still caught (the token differs).
    assert content_invariant("见 https://a/b. 句号结尾", "见 https://a/bc. 句号结尾") is False


def test_bare_url_format_only_reformat_around_it_passes():
    # a legit format-only change AROUND an unchanged bare URL (bolding a word) must
    # still pass -- the URL is byte-identical on both sides.
    assert content_invariant("见 https://a/b 效果", "**见** https://a/b 效果") is True


def test_link_refs_captures_bare_literals_in_order():
    # the shared scanner classifies bare literals as autolinks: `_link_refs`
    # returns them verbatim, in order, alongside inline links / angle autolinks.
    from app.services.knowhow.md_normalize import _link_refs
    md = "先 https://a/b 再 [文档](http://y/z) 又 <https://c/d> 邮箱 u@e.cn"
    assert _link_refs(md) == ["https://a/b", "[文档](http://y/z)", "<https://c/d>", "u@e.cn"]


def test_bare_literal_not_double_matched_inside_inline_link():
    # a URL inside `[text](url)` is consumed by the inline-link scan first; the
    # bare-literal scan runs on the remainder only -> it is NOT also emitted as a
    # standalone bare literal (no double counting).
    from app.services.knowhow.md_normalize import _link_refs
    assert _link_refs("[t](http://u/p)") == ["[t](http://u/p)"]
    # and one inside an image destination is likewise not double-matched.
    assert _link_refs("![a](https://img/x)") == []


def test_bare_url_not_matched_mid_word():
    # word-boundary start: `https://` embedded in a longer word (preceded by an
    # alphanumeric) is NOT a bare autolink literal.
    from app.services.knowhow.md_normalize import _link_refs
    assert _link_refs("xhttps://a/b") == []
    # but at a real boundary (string start / after a space) it is one literal.
    assert _link_refs("https://a/b") == ["https://a/b"]
    assert _link_refs("见 https://a/b") == ["https://a/b"]


def test_bare_url_and_numeric_context_punct_both_apply():
    # complementary, non-conflicting protections in ONE cell: the bare URL body
    # `b-c` is protected by the URL token (its `-` is between letters, NOT a
    # numeric context), while the prose version `1.2` is protected by the
    # numeric-context rule (its `.` is between digits). Neither breaks the other.
    before = "见 https://a/b-c 版本 1.2"
    # (a) a punctuation-only edit INSIDE the URL is caught by the URL token.
    assert content_invariant(before, "见 https://a/bc 版本 1.2") is False
    # (b) a punctuation-only edit to the prose version is caught by numeric context.
    assert content_invariant(before, "见 https://a/b-c 版本 12") is False
    # (c) a format-only reformat that keeps BOTH intact still passes.
    assert content_invariant(before, "**见** https://a/b-c 版本 1.2") is True


def test_bare_url_between_digits_inside_url_is_protected():
    # a URL body edit between digits (`1-2` -> `12`) -- the numeric-context rule
    # would NOT protect the `-` (its left neighbor is a digit, so not a unary
    # minus), but the URL token protects the whole body regardless.
    assert content_invariant("见 https://a/1-2-3", "见 https://a/123") is False


# ---------------------------------------------------------------------------
# F4 (this review) — the bare-autolink word-boundary check must use ASCII-alnum ONLY.
# `参见https://host/a-b` (CJK immediately before the URL, no space): remark-gfm treats
# the CJK char as a valid left boundary and autolinks the URL, but the matcher's
# `prev.isalnum()` (Unicode-aware) counted the CJK `见` as alphanumeric and SKIPPED the
# URL, leaving it in the punctuation-relaxed prose where a tail edit (`a-b` -> `ab`) was
# invisible. Fix: `prev.isascii() and prev.isalnum()` -- ASCII letters/digits are the
# only word chars that block a start; CJK / punctuation / whitespace all count as
# boundaries. ASCII-adjacent (`abchttps://x`) stays unmatched per GFM. Python-only.
# ---------------------------------------------------------------------------


def test_bare_url_after_cjk_boundary_is_protected():
    # THE codex example: a bare URL glued to CJK prose (no space). RED before the fix
    # (CJK counted as a word char -> URL skipped -> unprotected) -> old True.
    before = "参见https://host/a-b"
    after = "参见https://host/ab"
    assert content_invariant(before, after) is False


def test_bare_url_after_cjk_boundary_tokenized_in_link_refs():
    # the URL adjacent to CJK is now captured as one verbatim autolink ref (a CJK char
    # is a boundary, exactly like a space).
    from app.services.knowhow.md_normalize import _link_refs
    assert _link_refs("参见https://host/a-b") == ["https://host/a-b"]


def test_bare_url_after_ascii_alnum_still_not_matched():
    # regression guard: an ASCII-alnum char immediately before the scheme is NOT a
    # boundary per GFM (`abchttps://x` is one long word) -> still not matched.
    from app.services.knowhow.md_normalize import _link_refs
    assert _link_refs("abchttps://x") == []
    # and a real boundary (CJK / space / string-start) still matches.
    assert _link_refs("见https://x/y") == ["https://x/y"]
    assert _link_refs("https://x/y") == ["https://x/y"]


# ---------------------------------------------------------------------------
# F2 (this review) — the invariant must BYTE-PROTECT inline CODE-SPAN content,
# exactly as it already does for images/links/fenced blocks. Inline code content
# was landing in the punctuation-relaxed signature, so
# content_invariant("Run `a.b()` now", "Run `a-b[]` now") returned True: `a.b()`
# and `a-b[]` both reduce to signature `ab` (`.`,`-`,`(`,`)`,`[`,`]` are all
# stripped as punctuation). Fix: tokenize inline code spans per CommonMark (a
# backtick run of length N opens, the nearest same-length run closes, within the
# line) into content-hash tokens AND byte-compare the ordered span list --
# mirroring images/links/fences, reusing the same token machinery. Fences are
# excluded FIRST (a backtick inside a fenced block is _code_blocks' content).
# ---------------------------------------------------------------------------


def test_inline_code_content_punctuation_change_rejected():
    # THE codex example: `a.b()` -> `a-b[]`, a content change invisible to the
    # punctuation-relaxed signature (both reduce to `ab`). RED before the fix.
    assert content_invariant("Run `a.b()` now", "Run `a-b[]` now") is False


def test_inline_code_unchanged_around_reformat_passes():
    # No over-rejection: identical inline code span, only the surrounding prose
    # reformatted (bold + half-width colon) -> still format-only, passes.
    before = "前缀 `same()` 后缀：值"
    after = "**前缀** `same()` 后缀:值"
    assert content_invariant(before, after) is True


def test_inline_code_spans_are_order_sensitive():
    # swapping two inline code spans is a content reorder -> rejected (the ordered
    # ref list AND the in-place tokens both move).
    assert content_invariant("`a` 和 `b`", "`b` 和 `a`") is False


def test_inline_code_refs_lists_spans_verbatim():
    assert _inline_code_refs("Run `a.b()` now") == ["`a.b()`"]
    assert _inline_code_refs("`x` 与 `y.z`") == ["`x`", "`y.z`"]


def test_inline_code_double_backtick_run_closed_by_equal_run():
    # a length-2 backtick run is closed by the next length-2 run; a lone inner
    # backtick is literal span content, not a closer.
    assert _inline_code_refs("``a`b``") == ["``a`b``"]


def test_inline_code_refs_excludes_fenced_block_backticks():
    # backticks INSIDE a fenced block are the code block's content (protected by
    # _code_blocks), NOT inline spans -> not double-counted here.
    md = "```\ncode `not inline` here\n```"
    assert _inline_code_refs(md) == []


def test_inline_code_unclosed_run_is_not_a_span():
    # an unclosed backtick run -> no span (chars processed normally).
    assert _inline_code_refs("dangling ` backtick") == []


def test_inline_code_interaction_with_link_unaffected():
    # inline code next to a link: format-only still passes, AND a punctuation-only
    # edit to the link destination is still rejected (link protection intact).
    assert content_invariant("`c` 见 [d](https://h/a-b)", "**`c`** 见 [d](https://h/a-b)") is True
    assert content_invariant("`c` 见 [d](https://h/a-b)", "`c` 见 [d](https://h/ab)") is False
