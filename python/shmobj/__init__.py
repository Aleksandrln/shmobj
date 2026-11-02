"""shmobj: dict/list/скаляры в общей памяти, доступ без десериализации (RO v1)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from . import _shm
from ._core import (
    FORMAT_VERSION,
    FormatError,
    FrozenDict,
    FrozenList,
    Segment,
    SegmentClosedError,
    dumps,
    validate,
)
from ._core import open_segment as _open_segment

Mapping.register(FrozenDict)
Sequence.register(FrozenList)

__all__ = [
    "FORMAT_VERSION",
    "FormatError",
    "FrozenDict",
    "FrozenList",
    "Segment",
    "SegmentClosedError",
    "attach",
    "create",
    "dumps",
    "loads",
    "validate",
]


def loads(buf: Any, *, verify: bool = False) -> Any:
    """Корневое значение поверх буфера без копирования; `verify=True` проверяет контрольную сумму."""
    return _open_segment(buf, verify=verify).root


def create(obj: Any, name: str | None = None, *, dedup: bool = True, checksum: bool = True) -> Segment:
    """Собрать `obj` в новую SharedMemory. Создатель отвечает за `unlink()`."""
    return _shm.create(dumps(obj, dedup=dedup, checksum=checksum), name)


def attach(name: str, *, verify: bool = False) -> Segment:
    """Подключиться к существующему сегменту по имени; читается только заголовок."""
    return _shm.attach(name, verify)
