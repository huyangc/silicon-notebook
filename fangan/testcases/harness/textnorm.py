"""Deterministic text normalization + equivalence (judge-free fallback)."""
import re

_WS = re.compile(r"\s+")


def norm_text(s):
    if s is None:
        return ""
    s = str(s).strip()
    # strip a single layer of surrounding quotes
    if len(s) >= 2 and s[0] in "\"'" and s[-1] == s[0]:
        s = s[1:-1]
    s = _WS.sub(" ", s)
    return s.strip().lower()


def text_equiv(gold, pred, judge=None):
    """Deterministic equivalence: equal after norm, or short side contained in long side.

    `judge` (optional callable(gold, pred)->bool) is consulted only when the
    deterministic check fails; default None => purely deterministic.
    """
    g, p = norm_text(gold), norm_text(pred)
    if not g and not p:
        return True
    if not g or not p:
        return False
    if g == p:
        return True
    shorter, longer = (g, p) if len(g) <= len(p) else (p, g)
    if len(shorter) >= 4 and shorter in longer:
        return True
    if judge is not None:
        return bool(judge(gold, pred))
    return False


def payload_values(payload):
    """Flatten a payload dict into a list of scalar string values (keys dropped)."""
    out = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)
        elif v is not None:
            out.append(str(v))

    walk(payload or {})
    return out
