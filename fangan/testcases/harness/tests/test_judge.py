from harness import judge


def test_make_judge_none_when_disabled():
    assert judge.make_judge(enabled=False) is None


def test_cached_judge_calls_backend_once():
    calls = []

    def backend(g, p):
        calls.append((g, p))
        return True

    j = judge.CachedJudge(backend)
    assert j("a", "b") is True
    assert j("a", "b") is True
    assert len(calls) == 1  # second call served from cache
