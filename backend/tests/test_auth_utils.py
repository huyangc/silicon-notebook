from app.services.auth_utils import (
    is_valid_username, normalize_username, hash_password, verify_password,
)


def test_username_regex_accepts_one_or_more_letters_00_six_digits():
    assert is_valid_username("zhang00123456")
    assert is_valid_username("a00000042")
    assert is_valid_username("ABc00999999")


def test_username_regex_rejects_bad_shapes():
    assert not is_valid_username("00123456")        # 缺字母
    assert not is_valid_username("zhang0123456")    # 只有一个 0
    assert not is_valid_username("zhang0012345")    # 5 位数字
    assert not is_valid_username("zhang001234567")  # 7 位数字
    assert not is_valid_username("zh4ng00123456")   # 字母段含数字
    assert not is_valid_username("zhang_00123456")  # 非法字符


def test_normalize_username_lowercases_and_strips():
    assert normalize_username("  ZHang00123456 ") == "zhang00123456"


def test_password_hash_roundtrip():
    h, salt, iters = hash_password("hunter2")
    assert h and salt and iters > 0
    assert verify_password("hunter2", h, salt, iters)
    assert not verify_password("wrong", h, salt, iters)


def test_password_hash_uses_random_salt():
    h1, s1, _ = hash_password("same")
    h2, s2, _ = hash_password("same")
    assert s1 != s2 and h1 != h2
