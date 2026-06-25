from app.services.kg import scheduler
from app.services.sqlite_repository import _REQUEST_USER


def test_submit_job_propagates_contextvar():
    scheduler.reset()
    token = _REQUEST_USER.set("USER-X")   # 用裸值即可验证传播
    try:
        fut = scheduler.submit_job(lambda: _REQUEST_USER.get())
        assert fut.result(timeout=5) == "USER-X"
    finally:
        _REQUEST_USER.reset(token)
        scheduler.reset()
