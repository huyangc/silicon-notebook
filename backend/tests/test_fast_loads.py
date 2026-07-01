"""_fast_loads: orjson-accelerated JSON parse used on the hot vector/payload
sites in _stream_seed_reps. Must be behavior-identical to json.loads on the
values we actually parse, and must fall back to stdlib json for values orjson
rejects (NaN/Infinity in legacy vectors)."""
import json

from app.services.sqlite_repository import _fast_loads


def test_fast_loads_matches_json_on_float_array():
    arr = [float(i) * 0.001 for i in range(1024)]
    s = json.dumps(arr)
    assert _fast_loads(s) == json.loads(s)


def test_fast_loads_matches_json_on_payload_dict():
    payload = {"name": "MOSFET", "section_path": "1.2", "aliases": ["nmos", "n-mos"]}
    s = json.dumps(payload)
    assert _fast_loads(s) == json.loads(s)


def test_fast_loads_empty_object():
    assert _fast_loads("{}") == {} == json.loads("{}")


def test_fast_loads_falls_back_on_nan():
    # orjson rejects NaN/Infinity; stdlib json accepts them. _fast_loads must
    # not raise and must return the stdlib-parsed value (NaN != NaN, so compare
    # structurally via math.isnan).
    import math
    s = '[1.0, NaN, 3.0]'
    out = _fast_loads(s)
    assert out[0] == 1.0
    assert math.isnan(out[1])
    assert out[2] == 3.0
