"""Optional LLM semantic-equivalence judge. OFF by default (returns None).

A judge is any callable (gold_text, pred_text) -> bool. When enabled without a
custom backend, no real model is wired in (keeps the harness offline/zero-secret):
make_judge returns None unless a backend callable is supplied.
"""
import hashlib


class CachedJudge:
    def __init__(self, backend):
        self._backend = backend
        self._cache = {}

    def _key(self, g, p):
        return hashlib.sha256(f"{g}\x00{p}".encode("utf-8")).hexdigest()

    def __call__(self, gold_text, pred_text):
        k = self._key(gold_text, pred_text)
        if k not in self._cache:
            self._cache[k] = bool(self._backend(gold_text, pred_text))
        return self._cache[k]


def make_judge(enabled=False, backend=None):
    """Return a judge callable or None.

    enabled=False -> None (deterministic mode).
    enabled=True + backend -> CachedJudge(backend).
    enabled=True + no backend -> None (no model wired; caller logs a warning).
    """
    if not enabled or backend is None:
        return None
    return CachedJudge(backend)
