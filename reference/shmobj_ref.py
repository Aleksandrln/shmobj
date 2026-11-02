"""Эталонная реализация формата shmobj RO v1 (docs/format.md) на чистом Python.

Назначение: источник истины для формата и оракул для тестов Rust-реализации.
Скорость не цель. Зависимость: xxhash (тот же xxh3_64, что в крейте xxhash-rust).
"""
from __future__ import annotations

import math
import struct
from collections.abc import Iterator, Mapping, Sequence

import xxhash

# ---------------------------------------------------------------- константы формата
MAGIC = b"SHMO"
FORMAT_VERSION = 1
KIND_RO = 0
HASH_XXH3_64 = 1
HEADER_SIZE = 64
FLAG_CHECKSUM = 1 << 0
ALIGN = 8

HEADER = struct.Struct("<4sHBBIIQQQ")          # 40 байт + 24 байта резерва = 64

# теги слота (младшие 4 бита)
T_NONE, T_FALSE, T_TRUE, T_SMALLINT, T_INT64, T_BIGINT, T_FLOAT, T_STR, \
    T_BYTES, T_LIST, T_DICT, T_SHORTSTR = range(12)
REF_TAGS = {T_INT64, T_BIGINT, T_FLOAT, T_STR, T_BYTES, T_LIST, T_DICT}
# 12..15 зарезервированы под RW

# типы контейнерных узлов
N_LIST, N_DICT, N_KEYTABLE = 1, 2, 3
CONTAINER_HDR = struct.Struct("<BBHI")         # node_type, subtype, flags, len

# подтипы LIST
L_SLOTS, L_I8, L_U8, L_I16, L_U16, L_I32, L_U32, L_I64, L_F64, L_BOOL = range(10)
TYPED = {  # подтип -> (код struct, размер)
    L_I8: ("b", 1), L_U8: ("B", 1), L_I16: ("h", 2), L_U16: ("H", 2),
    L_I32: ("i", 4), L_U32: ("I", 4), L_I64: ("q", 8), L_F64: ("d", 8), L_BOOL: ("B", 1),
}
INT_WIDTHS = [  # порядок выбора минимальной ширины
    (L_U8, 0, 0xFF), (L_I8, -0x80, 0x7F), (L_U16, 0, 0xFFFF), (L_I16, -0x8000, 0x7FFF),
    (L_U32, 0, 0xFFFF_FFFF), (L_I32, -0x8000_0000, 0x7FFF_FFFF),
    (L_I64, -(1 << 63), (1 << 63) - 1),
]
TYPED_MIN_LEN = 2

# подтипы KEYTABLE
K_LINEAR, K_SORTED32 = 0, 1
LINEAR_MAX = 8

SMALLINT_MIN, SMALLINT_MAX = -(1 << 59), (1 << 59) - 1
SHORTSTR_MAX = 7
U64 = (1 << 64) - 1
MAX_LEN = 0xFFFF_FFFF


class FormatError(ValueError):
    """Данные не соответствуют формату."""


# ---------------------------------------------------------------- хэш ключей
def _int_bytes(v: int) -> bytes:
    return v.to_bytes((v.bit_length() + 8) // 8, "little", signed=True)


def key_hash64(key) -> int | None:
    """Стабильный хэш ключа. None — тип не поддерживается как ключ (поиск даёт KeyError)."""
    if key is None:
        return xxhash.xxh3_64_intdigest(b"\x00")
    if isinstance(key, bool):
        key = int(key)
    if isinstance(key, float):
        if math.isnan(key):
            return None
        if key.is_integer():
            key = int(key)
        else:
            return xxhash.xxh3_64_intdigest(b"\x02" + struct.pack("<d", key))
    if isinstance(key, int):
        return xxhash.xxh3_64_intdigest(b"\x01" + _int_bytes(key))
    if isinstance(key, str):
        return xxhash.xxh3_64_intdigest(b"\x03" + key.encode("utf-8", "surrogatepass"))
    if isinstance(key, bytes):
        return xxhash.xxh3_64_intdigest(b"\x04" + key)
    return None


def _is_key_type(k) -> bool:
    return k is None or type(k) in (bool, int, float, str, bytes) and not (
        type(k) is float and math.isnan(k))


# ---------------------------------------------------------------- сборка
class Builder:
    """Пишет узлы в пост-порядке: ссылка всегда указывает на меньшее смещение."""

    def __init__(self, *, dedup: bool = True, checksum: bool = True):
        self.out = bytearray(HEADER_SIZE)
        self.dedup = dedup
        self.checksum = checksum
        self._scalars: dict[tuple, int] = {}
        self._keytables: dict[tuple, int] = {}
        self._stack: set[int] = set()

    # -- низкий уровень
    def _alloc(self, data: bytes) -> int:
        off = len(self.out)
        self.out += data
        self.out += b"\0" * (-len(self.out) % ALIGN)
        return off

    def _scalar_node(self, dkey: tuple, data_fn) -> int:
        if self.dedup and dkey in self._scalars:
            return self._scalars[dkey]
        off = self._alloc(data_fn())
        if self.dedup:
            self._scalars[dkey] = off
        return off

    # -- значения
    def slot(self, v, path: str = "root") -> int:
        if v is None:
            return T_NONE
        t = type(v)
        if t is bool:
            return T_TRUE if v else T_FALSE
        if t is int:
            if SMALLINT_MIN <= v <= SMALLINT_MAX:
                return ((v << 4) | T_SMALLINT) & U64
            if -(1 << 63) <= v < (1 << 63):
                off = self._scalar_node(("i", v), lambda: struct.pack("<q", v))
                return (off << 4) | T_INT64
            b = _int_bytes(v)
            if len(b) > MAX_LEN:
                raise ValueError(f"{path}: int слишком большой")
            off = self._scalar_node(("I", v), lambda: struct.pack("<II", len(b), 0) + b)
            return (off << 4) | T_BIGINT
        if t is float:
            bits = struct.pack("<d", v)
            off = self._scalar_node(("f", bits), lambda: bits)
            return (off << 4) | T_FLOAT
        if t is str:
            b = v.encode("utf-8")            # суррогаты -> UnicodeEncodeError, как и должно быть
            if len(b) <= SHORTSTR_MAX:
                return T_SHORTSTR | (len(b) << 4) | (int.from_bytes(b, "little") << 8)
            if len(b) > MAX_LEN:
                raise ValueError(f"{path}: str длиннее 4 ГиБ")
            off = self._scalar_node(("s", v), lambda: struct.pack("<II", len(b), len(v)) + b)
            return (off << 4) | T_STR
        if t in (bytes, bytearray, memoryview):
            b = bytes(v)
            off = self._scalar_node(("b", b), lambda: struct.pack("<Q", len(b)) + b)
            return (off << 4) | T_BYTES
        if t in (list, tuple):
            return (self._list(v, path) << 4) | T_LIST
        if t is dict:
            return (self._dict(v, path) << 4) | T_DICT
        raise TypeError(f"{path}: неподдерживаемый тип {t.__name__}")

    def _enter(self, v, path):
        if id(v) in self._stack:
            raise ValueError(f"{path}: циклическая ссылка")
        if len(v) > MAX_LEN:
            raise ValueError(f"{path}: больше 2^32-1 элементов")
        self._stack.add(id(v))

    def _list(self, v, path) -> int:
        self._enter(v, path)
        try:
            n = len(v)
            sub = self._typed_subtype(v) if n >= TYPED_MIN_LEN else None
            if sub is not None:
                code, _ = TYPED[sub]
                body = struct.pack(f"<{n}{code}", *v)
            else:
                sub = L_SLOTS
                slots = [self.slot(x, f"{path}[{i}]") for i, x in enumerate(v)]
                body = struct.pack(f"<{n}Q", *slots)
            return self._alloc(CONTAINER_HDR.pack(N_LIST, sub, 0, n) + body)
        finally:
            self._stack.discard(id(v))

    @staticmethod
    def _typed_subtype(v) -> int | None:
        t0 = type(v[0])
        if any(type(x) is not t0 for x in v):
            return None
        if t0 is bool:
            return L_BOOL
        if t0 is float:
            return L_F64
        if t0 is int:
            lo, hi = min(v), max(v)
            for sub, a, b in INT_WIDTHS:
                if a <= lo and hi <= b:
                    return sub
        return None

    def _keytable(self, keys: list, path) -> int:
        dkey = tuple((type(k), k) for k in keys)
        if dkey in self._keytables:
            return self._keytables[dkey]
        for k in keys:
            if not _is_key_type(k):
                raise TypeError(f"{path}: неподдерживаемый тип ключа {type(k).__name__}")
        n = len(keys)
        slots = [self.slot(k, f"{path}.<key>") for k in keys]
        body = struct.pack(f"<{n}Q", *slots)
        if n > LINEAR_MAX:
            index = sorted(((key_hash64(k) >> 32) << 32) | i for i, k in enumerate(keys))
            body += struct.pack(f"<{n}Q", *index)
            sub = K_SORTED32
        else:
            sub = K_LINEAR
        off = self._alloc(CONTAINER_HDR.pack(N_KEYTABLE, sub, 0, n) + body)
        self._keytables[dkey] = off                 # таблицы ключей дедуплицируются всегда
        return off

    def _dict(self, v: dict, path) -> int:
        self._enter(v, path)
        try:
            keys = list(v.keys())
            kt = self._keytable(keys, path)
            vals = [self.slot(v[k], f"{path}[{k!r}]") for k in keys]
            n = len(keys)
            return self._alloc(CONTAINER_HDR.pack(N_DICT, 0, 0, n)
                               + struct.pack(f"<Q{n}Q", kt, *vals))
        finally:
            self._stack.discard(id(v))

    def finish(self, root_slot: int) -> bytes:
        data_size = len(self.out)
        flags = FLAG_CHECKSUM if self.checksum else 0
        csum = xxhash.xxh3_64_intdigest(bytes(self.out[HEADER_SIZE:])) if self.checksum else 0
        HEADER.pack_into(self.out, 0, MAGIC, FORMAT_VERSION, KIND_RO, HASH_XXH3_64,
                         HEADER_SIZE, flags, data_size, root_slot, csum)
        return bytes(self.out)


def build(obj, *, dedup: bool = True, checksum: bool = True) -> bytes:
    b = Builder(dedup=dedup, checksum=checksum)
    return b.finish(b.slot(obj))


# ---------------------------------------------------------------- чтение
class _Seg:
    __slots__ = ("buf", "size")

    def __init__(self, buf: memoryview, size: int):
        self.buf, self.size = buf, size

    def need(self, off: int, n: int):
        if off < HEADER_SIZE or off % ALIGN or off + n > self.size:
            raise FormatError(f"выход за границы: off={off} n={n}")

    def u32(self, off):
        return struct.unpack_from("<I", self.buf, off)[0]

    def u64(self, off):
        return struct.unpack_from("<Q", self.buf, off)[0]

    def container(self, off, expect):
        self.need(off, CONTAINER_HDR.size)
        nt, sub, flags, n = CONTAINER_HDR.unpack_from(self.buf, off)
        if nt != expect:
            raise FormatError(f"ожидался узел {expect}, найден {nt} по смещению {off}")
        return sub, n


def read_header(buf) -> tuple[int, int, int, int]:
    """-> (flags, data_size, root_slot, checksum)."""
    mv = memoryview(buf).cast("B")
    if len(mv) < HEADER_SIZE:
        raise FormatError("буфер меньше заголовка")
    magic, ver, kind, halg, hsize, flags, dsize, root, csum = HEADER.unpack_from(mv, 0)
    if magic != MAGIC:
        raise FormatError("неверный magic")
    if ver != FORMAT_VERSION:
        raise FormatError(f"неподдерживаемая версия формата {ver}")
    if kind != KIND_RO:
        raise FormatError(f"сегмент kind={kind} не является RO")
    if halg != HASH_XXH3_64 or hsize != HEADER_SIZE:
        raise FormatError("неподдерживаемый hash_algo/header_size")
    if flags & ~FLAG_CHECKSUM:
        raise FormatError("неизвестные биты flags")
    if any(mv[HEADER.size:HEADER_SIZE]):
        raise FormatError("резерв заголовка не нулевой")
    if not HEADER_SIZE <= dsize <= len(mv):
        raise FormatError("data_size вне буфера")
    return flags, dsize, root, csum


def load(buf, *, verify_checksum: bool = False):
    """Корневое значение поверх буфера (bytes, mmap, SharedMemory.buf). Без копирования."""
    flags, dsize, root, csum = read_header(buf)
    mv = memoryview(buf).cast("B")
    if verify_checksum and flags & FLAG_CHECKSUM:
        if xxhash.xxh3_64_intdigest(mv[HEADER_SIZE:dsize]) != csum:
            raise FormatError("контрольная сумма не совпала")
    return _decode(_Seg(mv, dsize), root)


def _decode(seg: _Seg, s: int):
    tag, p = s & 0xF, s >> 4
    if tag == T_NONE:
        return None
    if tag == T_FALSE:
        return False
    if tag == T_TRUE:
        return True
    if tag == T_SMALLINT:
        return (s - (1 << 64) if s >> 63 else s) >> 4
    if tag == T_SHORTSTR:
        n = p & 0xF
        if n > SHORTSTR_MAX or (p >> 4) >> (8 * n):
            raise FormatError("битый SHORTSTR")
        return (p >> 4).to_bytes(n, "little").decode("utf-8")
    if tag == T_INT64:
        seg.need(p, 8)
        return struct.unpack_from("<q", seg.buf, p)[0]
    if tag == T_FLOAT:
        seg.need(p, 8)
        return struct.unpack_from("<d", seg.buf, p)[0]
    if tag == T_BIGINT:
        seg.need(p, 8)
        n = seg.u32(p)
        seg.need(p, 8 + n)
        return int.from_bytes(seg.buf[p + 8:p + 8 + n], "little", signed=True)
    if tag == T_STR:
        seg.need(p, 8)
        n = seg.u32(p)
        seg.need(p, 8 + n)
        return str(seg.buf[p + 8:p + 8 + n], "utf-8")
    if tag == T_BYTES:
        seg.need(p, 8)
        n = seg.u64(p)
        seg.need(p, 8 + n)
        return bytes(seg.buf[p + 8:p + 8 + n])
    if tag == T_LIST:
        return FrozenList._at(seg, p)
    if tag == T_DICT:
        return FrozenDict._at(seg, p)
    raise FormatError(f"неизвестный тег {tag}")


_MISSING = object()


class FrozenList(Sequence):
    __slots__ = ("_seg", "_sub", "_data", "_rng")

    @classmethod
    def _at(cls, seg, off):
        self = cls.__new__(cls)
        sub, n = seg.container(off, N_LIST)
        size = 8 if sub == L_SLOTS else TYPED.get(sub, (None, None))[1]
        if size is None:
            raise FormatError(f"неизвестный подтип LIST {sub}")
        seg.need(off, 8 + n * size)
        code = "Q" if sub == L_SLOTS else TYPED[sub][0]
        self._seg, self._sub = seg, sub
        self._data = seg.buf[off + 8:off + 8 + n * size].cast(code)
        self._rng = range(n)
        return self

    def _item(self, i):
        raw = self._data[i]
        if self._sub == L_SLOTS:
            return _decode(self._seg, raw)
        if self._sub == L_BOOL:
            return bool(raw)
        return raw

    def __len__(self):
        return len(self._rng)

    def __getitem__(self, i):
        if isinstance(i, slice):
            view = FrozenList.__new__(FrozenList)
            view._seg, view._sub, view._data, view._rng = self._seg, self._sub, self._data, self._rng[i]
            return view
        return self._item(self._rng[i])

    def __iter__(self):
        return self.iter_values()

    def iter_values(self) -> Iterator:
        for i in self._rng:
            yield self._item(i)

    def get_many(self, indices, default=_MISSING) -> list:
        out = []
        for i in indices:
            try:
                out.append(self[i])
            except IndexError:
                if default is _MISSING:
                    raise
                out.append(default)
        return out

    def __eq__(self, other):
        if isinstance(other, (list, FrozenList)):
            return len(self) == len(other) and all(a == b for a, b in zip(self, other))
        return NotImplemented

    __hash__ = None

    def materialize(self) -> list:
        return [_materialize(x) for x in self]

    def __repr__(self):
        return f"FrozenList({self.materialize()!r})"


class FrozenDict(Mapping):
    __slots__ = ("_seg", "_n", "_kt", "_keys", "_vals", "_index")

    @classmethod
    def _at(cls, seg, off):
        self = cls.__new__(cls)
        _, n = seg.container(off, N_DICT)
        seg.need(off, 16 + 8 * n)
        kt = seg.u64(off + 8)
        if kt >= off:
            raise FormatError("ссылка вперёд (нарушен пост-порядок)")
        ksub, kn = seg.container(kt, N_KEYTABLE)
        if kn != n:
            raise FormatError("длина DICT не равна длине KEYTABLE")
        seg.need(kt, 8 + 8 * n * (2 if ksub == K_SORTED32 else 1))
        self._seg, self._n, self._kt = seg, n, kt
        self._keys = seg.buf[kt + 8:kt + 8 + 8 * n].cast("Q")
        self._vals = seg.buf[off + 16:off + 16 + 8 * n].cast("Q")
        if ksub == K_SORTED32:
            self._index = seg.buf[kt + 8 + 8 * n:kt + 8 + 16 * n].cast("Q")
        elif ksub == K_LINEAR:
            self._index = None
        else:
            raise FormatError(f"неизвестный подтип KEYTABLE {ksub}")
        return self

    def _find(self, key) -> int:
        hash(key)                                     # TypeError для нехэшируемых, как у dict
        h = key_hash64(key)
        if h is None:
            return -1
        if self._index is None:
            for i in range(self._n):
                if _decode(self._seg, self._keys[i]) == key:
                    return i
            return -1
        h32 = h >> 32
        lo, hi = 0, self._n
        while lo < hi:                               # lower_bound по старшим 32 битам
            mid = (lo + hi) // 2
            if self._index[mid] >> 32 < h32:
                lo = mid + 1
            else:
                hi = mid
        while lo < self._n and self._index[lo] >> 32 == h32:
            i = self._index[lo] & 0xFFFF_FFFF
            if _decode(self._seg, self._keys[i]) == key:
                return i
            lo += 1
        return -1

    def __getitem__(self, key):
        i = self._find(key)
        if i < 0:
            raise KeyError(key)
        return _decode(self._seg, self._vals[i])

    def __contains__(self, key):
        try:
            return self._find(key) >= 0
        except TypeError:
            raise

    def __len__(self):
        return self._n

    def __iter__(self):
        for s in self._keys:
            yield _decode(self._seg, s)

    def iter_values(self) -> Iterator:
        for s in self._vals:
            yield _decode(self._seg, s)

    def iter_items(self) -> Iterator:
        for k, v in zip(self._keys, self._vals):
            yield _decode(self._seg, k), _decode(self._seg, v)

    def get_many(self, keys, default=_MISSING) -> list:
        out = []
        for k in keys:
            i = self._find(k)
            if i < 0:
                if default is _MISSING:
                    raise KeyError(k)
                out.append(default)
            else:
                out.append(_decode(self._seg, self._vals[i]))
        return out

    def materialize(self) -> dict:
        return {k: _materialize(v) for k, v in self.iter_items()}

    def __repr__(self):
        return f"FrozenDict({self.materialize()!r})"


def _materialize(v):
    return v.materialize() if isinstance(v, (FrozenList, FrozenDict)) else v


# ---------------------------------------------------------------- полная проверка
def validate(buf) -> None:
    """Полный обход: границы, выравнивание, теги, UTF-8, сортировка индексов, пост-порядок."""
    flags, dsize, root, csum = read_header(buf)
    mv = memoryview(buf).cast("B")
    if flags & FLAG_CHECKSUM and xxhash.xxh3_64_intdigest(mv[HEADER_SIZE:dsize]) != csum:
        raise FormatError("контрольная сумма не совпала")
    seg = _Seg(mv, dsize)
    seen: set[int] = set()

    def walk(s: int, parent: int):
        tag, p = s & 0xF, s >> 4
        if tag >= 12:
            raise FormatError(f"тег {tag} недопустим в RO")
        if tag in REF_TAGS and p >= parent:
            raise FormatError("ссылка вперёд (нарушен пост-порядок)")
        v = _decode(seg, s)
        if tag in REF_TAGS:
            if p in seen:
                return
            seen.add(p)
        if isinstance(v, FrozenList):
            if v._sub == L_SLOTS:
                for x in v._data:
                    walk(x, p)
        elif isinstance(v, FrozenDict):
            keys = []
            for k in v._keys:
                kv = _decode(seg, k)
                if not _is_key_type(kv):
                    raise FormatError("недопустимый ключ (контейнер или NaN)")
                walk(k, v._kt)
                keys.append(kv)
            if len(set(keys)) != len(keys):
                raise FormatError("повторяющиеся ключи")
            if v._index is not None:
                idx = list(v._index)
                if idx != sorted(idx) or sorted(i & 0xFFFF_FFFF for i in idx) != list(range(v._n)):
                    raise FormatError("битый индекс KEYTABLE")
                for e in idx:
                    k = _decode(seg, v._keys[e & 0xFFFF_FFFF])
                    if key_hash64(k) >> 32 != e >> 32:
                        raise FormatError("хэш в индексе не совпадает с ключом")
            for x in v._vals:
                walk(x, p)

    walk(root, dsize)
