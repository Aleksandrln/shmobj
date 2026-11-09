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


def open_shm(name: str | None, *, create: bool = False, size: int = 0, track: bool = False) -> shared_memory.SharedMemory:
    """SharedMemory; `track=False` — без регистрации в resource_tracker: выход процесса не удаляет имя."""
    if track:
        return shared_memory.SharedMemory(name=name, create=create, size=size)
    if _TRACK_PARAM:
        return shared_memory.SharedMemory(name=name, create=create, size=size, track=False)
    shm = shared_memory.SharedMemory(name=name, create=create, size=size)
    if _POSIX:
        from multiprocessing import resource_tracker

        resource_tracker.unregister(shm._name, "shared_memory")  # noqa: SLF001
    return shm


def unlink_name(name: str) -> None:
    """Удалить имя неотслеживаемого сегмента; его нет — без ошибки. Windows — ничего: имени без handle нет."""
    if not _POSIX:
        return
    import _posixshmem

    try:
        _posixshmem.shm_unlink("/" + name)
    except FileNotFoundError:
        pass


def create(obj, name: str | None, *, dedup: bool, checksum: bool, track: bool = True) -> _core.Segment:
    """Новая SharedMemory со сборкой `obj` (сразу в неё, без `bytes`); процесс-создатель — владелец сегмента.

    `track=False` — имя переживает выход процесса, удаляет его `unlink()`.
    """
    shm = None

    def alloc(size: int) -> memoryview:
        nonlocal shm
        shm = open_shm(name, create=True, size=size, track=track)
        return shm.buf

    try:
        _core.dumps_into(obj, alloc, dedup=dedup, checksum=checksum)
    except BaseException:
        if shm is not None:
            shm.close()
            _unlinker(shm, tracked=track)()
        raise
    return _segment(shm, _unlinker(shm, tracked=track), verify=False)


def attach(name: str, verify: bool = False) -> _core.Segment:
    """Подключение без регистрации в resource_tracker: выход читателя не удаляет сегмент."""
    shm = open_shm(name)
    return _segment(shm, _unlinker(shm, tracked=False), verify=verify)
