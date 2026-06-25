from app.services.auth_utils import (
    is_valid_username, normalize_username, hash_password, verify_password,
)


def test_username_regex_accepts_single_letter_00_six_digits():
    assert is_valid_username("a00123456")
    assert is_valid_username("Z00000042")   # 大小写均可
    assert is_valid_username("b00999999")


def test_username_regex_rejects_bad_shapes():
    assert not is_valid_username("00123456")        # 缺字母
    assert not is_valid_username("ab00123456")      # 多个字母（须恰好 1 个）
    assert not is_valid_username("a0123456")        # 只有一个 0
    assert not is_valid_username("a0012345")        # 5 位数字
    assert not is_valid_username("a001234567")      # 7 位数字
    assert not is_valid_username("a_00123456")      # 非法字符


def test_normalize_username_lowercases_and_strips():
    assert normalize_username("  Z00123456 ") == "z00123456"


def test_password_hash_roundtrip():
    h, salt, iters = hash_password("hunter2")
    assert h and salt and iters > 0
    assert verify_password("hunter2", h, salt, iters)
    assert not verify_password("wrong", h, salt, iters)


def test_password_hash_uses_random_salt():
    h1, s1, _ = hash_password("same")
    h2, s2, _ = hash_password("same")
    assert s1 != s2 and h1 != h2
