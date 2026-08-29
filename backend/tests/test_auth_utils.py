from app.services.auth_utils import (
    is_valid_username, normalize_username, hash_password, verify_password,
)


def test_username_regex_accepts_single_lowercase_letter_and_eight_digits():
    assert is_valid_username("a12345678")
    assert is_valid_username("b01999999")
    assert is_valid_username("m00000042")


def test_username_regex_rejects_bad_shapes():
    assert not is_valid_username("00123456")        # 缺字母
    assert not is_valid_username("A00123456")       # 大写（须小写）
    assert not is_valid_username("Z00000042")       # 大写
    assert not is_valid_username("ab00123456")      # 多个字母
    assert not is_valid_username("a1234567")        # 7 位数字
    assert not is_valid_username("a123456789")      # 9 位数字
    assert not is_valid_username("a_00123456")      # 非法字符
    assert not is_valid_username("a１２３４５６７８")  # 只接受 ASCII 数字


def test_normalize_username_lowercases_and_strips():
    assert normalize_username("  A00123456 ") == "a00123456"


def test_password_hash_roundtrip():
    h, salt, iters = hash_password("hunter2")
    assert h and salt and iters > 0
    assert verify_password("hunter2", h, salt, iters)
    assert not verify_password("wrong", h, salt, iters)


def test_password_hash_uses_random_salt():
    h1, s1, _ = hash_password("same")
    h2, s2, _ = hash_password("same")
    assert s1 != s2 and h1 != h2
