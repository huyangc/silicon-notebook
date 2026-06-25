from app.models.schemas import UserProfile, AuthRequest, AuthResult


def test_userprofile_has_username_default():
    u = UserProfile(id="user-local", email="x@y.z", display_name="Admin", role="admin")
    assert u.username == ""


def test_auth_request_and_result():
    req = AuthRequest(username="zhang00123456", password="pw")
    assert req.username == "zhang00123456" and req.password == "pw"
    res = AuthResult(token="tok", user=UserProfile(
        id="u1", email="x@y.z", display_name="z", role="user", username="zhang00123456"))
    assert res.token == "tok" and res.user.username == "zhang00123456"
