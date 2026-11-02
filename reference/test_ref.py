"""Тесты эталонной реализации. Запуск: pytest reference/"""
import math
import random

import pytest
from hypothesis import given, settings, strategies as st

import shmobj_ref as ref

keys = st.one_of(st.none(), st.booleans(), st.integers(), st.text(),
                 st.binary(), st.floats(allow_nan=False))
scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.integers(-2**70, 2**70),
                    st.floats(allow_nan=False), st.text(), st.binary())
values = st.recursive(scalars, lambda c: st.lists(c, max_size=12) | st.dictionaries(keys, c, max_size=12),
                      max_leaves=60)


@settings(max_examples=400, deadline=None)
@given(values)
def test_roundtrip(v):
    buf = ref.build(v)
    ref.validate(buf)
    r = ref.load(buf, verify_checksum=True)
    m = r.materialize() if hasattr(r, "materialize") else r
    assert m == v


@settings(max_examples=200, deadline=None)
@given(st.dictionaries(keys, scalars, max_size=40))
def test_dict_lookup_like_dict(d):
    fd = ref.load(ref.build(d))
    assert len(fd) == len(d)
    assert list(fd) == list(d)                     # порядок вставки сохраняется
    for k, v in d.items():
        assert fd[k] == v and k in fd
    assert fd.get_many(list(d)) == list(d.values())
    assert list(fd.iter_values()) == list(d.values())


def test_numeric_key_equivalence():
    big = {i: i for i in range(20)}                # SORTED32
    small = {1: "a", 2.5: "b", None: "n"}          # LINEAR
    for d in (big, small):
        fd = ref.load(ref.build(d))
        assert fd[True] == d[1] and fd[1.0] == d[1]
    assert ref.load(ref.build(small))[2.5] == "b"
    with pytest.raises(KeyError):
        ref.load(ref.build(small))[math.nan]
    with pytest.raises(TypeError):
        ref.load(ref.build(small))[[1]]
    with pytest.raises(KeyError):
        ref.load(ref.build(small))[(1,)]


def test_get_many_default_and_missing():
    fd = ref.load(ref.build({"a": 1, "b": 2}))
    assert fd.get_many(["a", "x"], default=None) == [1, None]
    with pytest.raises(KeyError):
        fd.get_many(["a", "x"])
    fl = ref.load(ref.build([10, 20, 30]))
    assert fl.get_many([0, -1, 5], default=0) == [10, 30, 0]


def test_list_typed_and_slices():
    cases = {ref.L_U8: [0, 255], ref.L_I8: [-1, 5], ref.L_I16: [-300, 1], ref.L_I64: [2**40, -1],
             ref.L_F64: [1.5, 2.0], ref.L_BOOL: [True, False]}
    for sub, v in cases.items():
        fl = ref.load(ref.build(v))
        assert fl._sub == sub and list(fl) == v and [type(x) for x in fl] == [type(x) for x in v]
    fl = ref.load(ref.build(list(range(100))))
    assert list(fl[10:20:3]) == list(range(10, 20, 3)) and fl[::-1][0] == 99
    assert ref.load(ref.build([1, True]))._sub == ref.L_SLOTS


def test_keytable_and_scalar_dedup():
    recs = [{"id": i, "name": "same_long_name", "tags": ["x"]} for i in range(100)]
    with_d, without = ref.build(recs), ref.build(recs, dedup=False)
    assert len(with_d) < len(without)
    fl = ref.load(with_d)
    assert fl.materialize() == recs


def test_errors():
    with pytest.raises(TypeError, match=r"root\[0\]\['s'\]"):
        ref.build([{"s": {1, 2}}])
    with pytest.raises(TypeError):
        ref.build({(1, 2): 1})
    a = []
    a.append(a)
    with pytest.raises(ValueError, match="цикл"):
        ref.build(a)


def test_corruption_never_crashes():
    rnd = random.Random(1)
    good = ref.build({"k": [1, "долгая строка", {"z": 2.5}], "n": list(range(30)),
                      "d": {f"key{i}": [i, f"v{i}"] for i in range(20)}}, checksum=False)
    for _ in range(20000):
        b = bytearray(good)
        for _ in range(rnd.randint(1, 4)):
            b[rnd.randrange(ref.HEADER_SIZE - 40, len(b))] = rnd.randrange(256)
        try:
            ref.validate(bytes(b))
        except (ref.FormatError, UnicodeDecodeError):
            pass


def test_shared_memory_two_processes():
    import multiprocessing as mp
    from multiprocessing import shared_memory
    data = {"users": [{"id": i, "name": f"user_{i:05d}"} for i in range(1000)]}
    buf = ref.build(data)
    shm = shared_memory.SharedMemory(create=True, size=len(buf))
    try:
        shm.buf[:len(buf)] = buf
        ctx = mp.get_context("spawn")
        with ctx.Pool(1) as pool:
            assert pool.apply(_child_read, (shm.name,)) == "user_00777"
    finally:
        shm.close()
        shm.unlink()


def _child_read(name):
    from multiprocessing import shared_memory
    try:
        shm = shared_memory.SharedMemory(name=name, track=False)
    except TypeError:
        shm = shared_memory.SharedMemory(name=name)
    r = ref.load(shm.buf)
    out = r["users"][777]["name"]
    del r
    shm.close()
    return out
