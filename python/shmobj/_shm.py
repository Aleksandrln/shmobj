"""Жизненный цикл SharedMemory: создание, подключение, close/unlink, resource_tracker."""
from __future__ import annotations

import os
import sys
from multiprocessing import shared_memory

from . import _core

_TRACK_PARAM = sys.version_info >= (3, 13)
_POSIX = os.name == "posix"


def _segment(shm: shared_memory.SharedMemory, unlink, verify: bool) -> _core.Segment:
    try:
        return _core.open_segment(shm.buf, name=shm.name, on_close=shm.close, on_unlink=unlink, verify=verify)
    except BaseException:
        shm.close()
        raise


def create(data: bytes, name: str | None = None) -> _core.Segment:
    """Новая SharedMemory с данными `data`; процесс-создатель — владелец сегмента."""
    shm = shared_memory.SharedMemory(name=name, create=True, size=max(len(data), 1))
    shm.buf[: len(data)] = data
    return _segment(shm, _unlinker(shm, tracked=True), verify=False)


def attach(name: str, verify: bool = False) -> _core.Segment:
    """Подключение без регистрации в resource_tracker: выход читателя не удаляет сегмент."""
    if _TRACK_PARAM:
        shm = shared_memory.SharedMemory(name=name, track=False)
    else:
        shm = shared_memory.SharedMemory(name=name)
        if _POSIX:
            from multiprocessing import resource_tracker

            resource_tracker.unregister(shm._name, "shared_memory")  # noqa: SLF001
    return _segment(shm, _unlinker(shm, tracked=False), verify=verify)


def _unlinker(shm: shared_memory.SharedMemory, *, tracked: bool):
    def unlink() -> None:
        if not _POSIX:
            return  # Windows: сегмент живёт, пока открыт хотя бы один дескриптор.
        if tracked or _TRACK_PARAM:
            shm.unlink()  # снимает регистрацию, только если сегмент отслеживался
        else:
            import _posixshmem

            _posixshmem.shm_unlink(shm._name)  # noqa: SLF001

    return unlink
