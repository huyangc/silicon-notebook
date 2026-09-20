import logging

from app.core.auth_logging import AuthenticationAccessFilter


def test_authentication_callback_access_log_omits_query_credentials():
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1,
        '%s - "%s %s HTTP/%s" %d',
        ("client", "GET", "/api/auth/sso/callback?code=fake-code&state=fake-state", "1.1", 303), None)
    assert AuthenticationAccessFilter().filter(record)
    assert "fake-code" not in record.getMessage()
    assert "fake-state" not in record.getMessage()
    assert "/api/auth/sso/callback" in record.getMessage()


def test_unrelated_access_logs_remain_unchanged():
    args = ("client", "GET", "/api/search?q=public", "1.1", 200)
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1, "%s %s %s %s %s", args, None)
    assert AuthenticationAccessFilter().filter(record)
    assert record.args == args
