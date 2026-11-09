"""Семантика API (doc/api.md)."""
import math
import pickle
from collections.abc import Mapping, Sequence

import pytest

import shmobj


def L(v, **kw):
    return shmobj.loads(shmobj.dumps(v, **kw))


def test_abc_registration():
    assert isinstance(L({}), Mapping)
    assert isinstance(L([]), Sequence)


@pytest.mark.parametrize("d", [{1: "a", 2.5: "b", None: "n"}, {i: i for i in range(20)}])
def test_numeric_key_equivalence(d):
    fd = L(d)
    assert fd[True] == d[1] and fd[1.0] == d[1] and 1 in fd
    with pytest.raises(KeyError):
        fd[math.nan]
    with pytest.raises(KeyError):
        fd[(1,)]
    with pytest.raises(TypeError):
        fd[[1]]


def test_big_int_and_bytes_keys():
    d = {2**70: "big", -2**63: "min", b"\x00": "z", "": "empty"}
    fd = L(d)
    assert [fd[k] for k in d] == list(d.values())
    assert fd.get(2**71) is None and fd.get("x", 5) == 5


def test_dict_views_and_order():
    d = {"b": 1, "a": [1, 2], "c": {"x": None}}
    fd = L(d)
    assert list(fd) == list(fd.keys()) == ["b", "a", "c"]
    assert list(fd.values())[0] == 1
    assert ("b", 1) in fd.items()
    assert [k for k, _ in fd.iter_items()] == list(d)
    assert len(list(fd.iter_values())) == 3


def test_get_many():
    fd = L({"a": 1, "b": 2})
    assert fd.get_many(["b", "a"]) == [2, 1]
    assert fd.get_many(["a", "x"], None) == [1, None]
    assert fd.get_many(["a", "x"], default=0) == [1, 0]
    with pytest.raises(KeyError):
        fd.get_many(["a", "x"])
    fl = L([10, 20, 30])
    assert fl.get_many([0, -1, 5], default=None) == [10, 30, None]
    with pytest.raises(IndexError):
        fl.get_many([3])


def test_list_indexing_and_slices():
    src = list(range(100))
    fl = L(src)
    assert fl[-1] == 99 and len(fl) == 100
    with pytest.raises(IndexError):
        fl[100]
    with pytest.raises(IndexError):  # как у list: не влезает в isize
        fl[10**30]
    with pytest.raises(TypeError):
        fl["0"]
    for s in [slice(10, 20, 3), slice(None, None, -1), slice(-5, None), slice(90, 10, -7), slice(5, 5)]:
        assert list(fl[s]) == src[s]
    assert list(fl[::-1][::3]) == src[::-1][::3]
    assert isinstance(fl[1:3], shmobj.FrozenList)
    assert list(reversed(fl[10:20])) == src[19:9:-1]
    assert fl.index(42) == 42 and fl.count(7) == 1 and 55 in fl and 1000 not in fl
    with pytest.raises(ValueError):
        fl.index(5, 10)


def test_typed_lists_return_plain_types():
    for v in ([True, False], [0.5, 2.0], [0, 255], [-1, 300], [2**40, -1]):
        fl = L(v)
        assert list(fl) == v and [type(x) for x in fl] == [type(x) for x in v]


def test_equality():
    assert L({"a": 1, "b": [1, 2]}) == {"b": [1, 2], "a": 1}
    assert L({"a": 1}) != {"a": 2}
    assert L([1, 2]) == [1, 2] and L([1, 2]) != (1, 2) and L([1, 2]) == L((1, 2))
    assert L({"x": {"y": [1]}}) == L({"x": {"y": [1]}})


def test_repr_hash_pickle():
    fd, fl = L({"a": 1}), L([1, 2, 3])
    assert repr(fd) == "<FrozenDict len=1>" and repr(fl) == "<FrozenList len=3>"
    for obj in (fd, fl):
        with pytest.raises(TypeError):
            hash(obj)
        with pytest.raises(TypeError, match="materialize"):
            pickle.dumps(obj)


def test_materialize_deep():
    v = {"a": [[], {}, [[]], {"b": {"c": [1, 2.5, {"d": None}]}}], "t": (1, 2)}
    m = L(v).materialize()
    assert m == {**v, "t": [1, 2]}
    assert type(m["a"][3]["b"]) is dict and type(m["t"]) is list


@pytest.mark.parametrize("v", [None, True, 0, -2**200, 1.5, -0.0, "", "😀" * 3, b"\x00\xff\x00"])
def test_scalar_root(v):
    r = L(v)
    assert r == v and type(r) is type(v)


def test_nan_value_preserved():
    assert math.isnan(L([math.nan, 1])[0])


def test_build_errors():
    with pytest.raises(TypeError, match=r"root\[0\]\['s'\]"):
        shmobj.dumps([{"s": {1, 2}}])
    with pytest.raises(TypeError):
        shmobj.dumps({(1, 2): 1})
    with pytest.raises(TypeError):
        shmobj.dumps({math.nan: 1})

    class D(dict):
        pass

    with pytest.raises(TypeError):
        shmobj.dumps(D())
    a = []
    a.append(a)
    with pytest.raises(ValueError, match="цикл"):
        shmobj.dumps(a)
    with pytest.raises(UnicodeEncodeError):
        shmobj.dumps("\ud800")


def test_format_errors():
    with pytest.raises(shmobj.FormatError):
        shmobj.loads(b"")
    good = bytearray(shmobj.dumps({"a": 1}))
    good[6] = 1  # kind = RW
    with pytest.raises(shmobj.FormatError, match="RW"):
        shmobj.loads(bytes(good))
    bad = bytearray(shmobj.dumps({"a": "x" * 50}))
    bad[-10] ^= 0xFF
    with pytest.raises(shmobj.FormatError):
        shmobj.loads(bytes(bad), verify=True)
    with pytest.raises(shmobj.FormatError):
        shmobj.validate(bytes(bad))


def test_dumps_into():
    import mmap

    obj = {"users": [{"id": i, "name": f"u{i}"} for i in range(100)], "x": None}
    bufs = []

    def alloc(size):
        bufs.append(bytearray(b"\xff" * (size + 7)))  # длиннее size — сегмент пишется в начало
        return bufs[-1]

    n = shmobj.dumps_into(obj, alloc, dedup=False, checksum=False)
    expected = shmobj.dumps(obj, dedup=False, checksum=False)
    assert len(bufs) == 1 and n == len(expected)
    assert bufs[0] == expected + b"\xff" * 7
    bufs[0].extend(b"x")  # экспорт буфера отпущен, иначе BufferError

    m = mmap.mmap(-1, n)
    assert shmobj.dumps_into(obj, lambda size: m, dedup=False, checksum=False) == n
    assert m[:] == expected
    m.close()


def test_dumps_into_errors():
    obj = [1, 2, 3]
    with pytest.raises(ValueError, match="только для чтения"):
        shmobj.dumps_into(obj, lambda size: bytes(size))
    with pytest.raises(ValueError, match="короче"):
        shmobj.dumps_into(obj, lambda size: bytearray(size - 1))
    with pytest.raises(OSError):
        shmobj.dumps_into(obj, lambda size: (_ for _ in ()).throw(OSError("нет памяти")))
    called = []
    with pytest.raises(TypeError, match=r"root\[0\]"):
        shmobj.dumps_into([object()], called.append)
    assert called == []  # ошибка сборки — до выделения буфера
