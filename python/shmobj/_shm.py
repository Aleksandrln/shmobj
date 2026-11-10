"""Жизненный цикл SharedMemory: создание, подключение, close/unlink, resource_tracker."""
from __future__ import annotations

import contextlib
import mmap
import os
import sys
from multiprocessing import shared_memory

from . import _core

_TRACK_PARAM = sys.version_info >= (3, 13)
_POSIX = os.name == "posix"


def _segment(shm: shared_memory.SharedMemory, unlink, verify: bool, on_close=None) -> _core.Segment:
    try:
        return _core.open_segment(shm.buf, name=shm.name, on_close=on_close or shm.close, on_unlink=unlink, verify=verify)
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


class _Mapped:
    """Существующий POSIX-сегмент только на чтение: `buf`, `size`, `close()` как у SharedMemory."""

    def __init__(self, mm: mmap.mmap) -> None:
        self._mmap = mm
        self.buf = memoryview(mm)
        self.size = len(mm)

    def close(self) -> None:
        self.buf.release()
        self._mmap.close()


def open_existing(name: str):
    """Существующий сегмент на чтение; None — имени нет или создатель ещё не задал размер.

    POSIX — без SharedMemory: сегмент между `shm_open` и `ftruncate` создателя имеет длину 0, её mmap — ошибка,
    а при OSError (macOS — EINVAL) SharedMemory удаляет имя, то есть сегмент создателя.
    """
    if not _POSIX:
        try:
            return open_shm(name)
        except FileNotFoundError:
            return None
    import _posixshmem

    try:
        fd = _posixshmem.shm_open("/" + name, os.O_RDONLY, mode=0o600)
    except FileNotFoundError:
        return None
    try:
        size = os.fstat(fd).st_size
        return _Mapped(mmap.mmap(fd, size, prot=mmap.PROT_READ)) if size else None
    finally:
        os.close(fd)


def unlink_name(name: str) -> None:
    """Удалить имя неотслеживаемого сегмента; его нет — без ошибки. Windows — ничего: имени без handle нет."""
    if not _POSIX:
        return
    import _posixshmem

    try:
        _posixshmem.shm_unlink("/" + name)
    except FileNotFoundError:
        pass


def _closer(shm: shared_memory.SharedMemory, unlink):
    def close() -> None:
        shm.close()
        with contextlib.suppress(FileNotFoundError):  # имя уже удалено: `unlink()` до `close()`
            unlink()

    return close


def create(
    obj, name: str | None, *, dedup: bool, checksum: bool, track: bool = True, unlink_on_close: bool = False
) -> _core.Segment:
    """Новая SharedMemory со сборкой `obj` (сразу в неё, без `bytes`); процесс-создатель — владелец сегмента.

    `track=False` — имя переживает выход процесса, удаляет его `unlink()`.
    `unlink_on_close=True` — `close()` удаляет и имя.
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
    unlink = _unlinker(shm, tracked=track)
    return _segment(shm, unlink, verify=False, on_close=_closer(shm, unlink) if unlink_on_close else None)


def attach(name: str, verify: bool = False) -> _core.Segment:
    """Подключение без регистрации в resource_tracker: выход читателя не удаляет сегмент."""
    shm = open_shm(name)
    return _segment(shm, _unlinker(shm, tracked=False), verify=verify)
