"""Сверка с эталоном: взаимная читаемость, совпадение байт и вердиктов validate."""
import random

import shmobj_ref as ref
from hypothesis import given, settings
from test_ref import values  # стратегия из reference/test_ref.py

import shmobj


def _plain(v):
    return v.materialize() if hasattr(v, "materialize") else v


@settings(max_examples=300, deadline=None)
@given(values)
def test_roundtrip_both_ways(v):
    buf = shmobj.dumps(v)
    assert buf == ref.build(v)
    assert _plain(ref.load(buf)) == v
    assert _plain(shmobj.loads(ref.build(v))) == v
    shmobj.validate(buf)


def _verdict(fn, buf):
    try:
        fn(buf)
        return True
    except (ValueError, UnicodeDecodeError):  # FormatError — подкласс ValueError
        return False


def test_validate_agrees_with_reference_on_corruption():
    rnd = random.Random(7)
    good = shmobj.dumps({"k": [1, "долгая строка", {"z": 2.5}], "n": list(range(30)),
                         "d": {f"key{i}": [i, f"v{i}"] for i in range(20)}, "b": 2**70}, checksum=False)
    for _ in range(5000):
        b = bytearray(good)
        for _ in range(rnd.randint(1, 4)):
            b[rnd.randrange(24, len(b))] = rnd.randrange(256)
        b = bytes(b)
        assert _verdict(shmobj.validate, b) == _verdict(ref.validate, b), b.hex()
