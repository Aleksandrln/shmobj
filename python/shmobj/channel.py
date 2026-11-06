"""Канал снимков: последний снимок от писателя к читателю через память без имени (Linux).

Писатель собирает снимок прямо в memfd (`dumps_into`), запечатывает его от записи и изменения размера
и передаёт дескриптор по сокету AF_UNIX SOCK_SEQPACKET (SCM_RIGHTS). Читатель берёт из очереди
последний снимок (прочие закрывает), отображает его `mmap` только на чтение и открывает `loads`.
Снимок освобождается ядром, когда закрыт последний дескриптор и отпущен последний прокси, поэтому
после падения любой из сторон в ОС ничего не остаётся. Семантика — doc/spec/02-api.md §6.
"""
from __future__ import annotations

import mmap
import os
import socket
from collections.abc import Callable
from typing import Any

from ._core import dumps_into, open_segment

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

SUPPORTED = (fcntl is not None and hasattr(os, "memfd_create") and hasattr(os, "MFD_ALLOW_SEALING")
             and hasattr(socket, "send_fds") and hasattr(socket, "SOCK_SEQPACKET"))

# linux/fcntl.h: в модуле fcntl констант печатей может не быть
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_SEAL_SEAL, _SEAL_SHRINK, _SEAL_GROW, _SEAL_WRITE = 0x1, 0x2, 0x4, 0x8
_SEALS = _SEAL_SEAL | _SEAL_SHRINK | _SEAL_GROW | _SEAL_WRITE
# без них снимок могут изменить под читателем или укоротить (SIGBUS при чтении отображения)
_REQUIRED_SEALS = _SEAL_SHRINK | _SEAL_WRITE
# сообщение не пустое: пустое в SOCK_SEQPACKET неотличимо от закрытия канала
_MSG = b"S"
_MAX_FDS = 4


def socketpair() -> tuple[socket.socket, socket.socket]:
    """Пара сокетов канала (AF_UNIX, SOCK_SEQPACKET); концы равноправны. В дочерний процесс — `pass_fds`."""
    _require()
    return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)


class Publisher:
    """Конец писателя. Владеет сокетом: переводит его в неблокирующий режим и закрывает в `close()`."""

    def __init__(self, sock: socket.socket, *, sndbuf: int = 16 * 1024, name: str = "shmobj") -> None:
        _require()
        _check_socket(sock)
        sock.setblocking(False)
        # буфер отправки ограничивает очередь непрочитанных снимков, каждый из которых держит свой memfd
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
        self._sock = sock
        self._name = name

    def publish(self, obj: Any, *, dedup: bool = True, checksum: bool = True) -> bool:
        """Отправить снимок `obj`. False — очередь полна (читатель не разбирает её), снимок пропущен."""
        fd = _sealed_memfd(self._name, obj, dedup, checksum)
        try:
            socket.send_fds(self._sock, [_MSG], [fd])
        except BlockingIOError:
            return False
        finally:
            os.close(fd)  # пока сообщение в очереди, memfd держит ядро
        return True

    def fileno(self) -> int:
        return self._sock.fileno()

    @property
    def closed(self) -> bool:
        return self._sock.fileno() == -1

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> Publisher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Subscriber:
    """Конец читателя. Владеет сокетом; `latest` переживает закрытие канала."""

    def __init__(self, sock: socket.socket, *, verify: bool = False) -> None:
        _require()
        _check_socket(sock)
        sock.setblocking(False)
        self._sock = sock
        self._verify = verify
        self._latest: Any = None
        self._loop = None

    @property
    def latest(self) -> Any:
        """Корень последнего принятого снимка; None — снимков ещё не было."""
        return self._latest

    def receive(self) -> bool:
        """Разобрать очередь без ожидания: True — `latest` сменился. Конец канала закрывает сокет."""
        if self.closed:
            return False
        fd, eof = self._drain()
        if eof:
            self.close()
        if fd is None:
            return False
        self._latest = _load(fd, self._verify)
        return True

    def attach(self, callback: Callable[[Any], None] | None = None, *, loop=None) -> None:
        """Принимать снимки в цикле asyncio (`loop` — по умолчанию текущий): `callback(latest)` на каждый новый.

        Исключения `receive` и `callback` уходят в обработчик исключений цикла; приём продолжается.
        """
        if loop is None:
            import asyncio
            loop = asyncio.get_running_loop()
        self.detach()
        loop.add_reader(self._sock.fileno(), self._on_readable, callback)
        self._loop = loop

    def detach(self) -> None:
        """Снять сокет с цикла asyncio; сокет остаётся открытым."""
        loop, self._loop = self._loop, None
        if loop is not None:
            loop.remove_reader(self._sock.fileno())

    def fileno(self) -> int:
        return self._sock.fileno()

    @property
    def closed(self) -> bool:
        return self._sock.fileno() == -1

    def close(self) -> None:
        self.detach()
        self._sock.close()

    def __enter__(self) -> Subscriber:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _on_readable(self, callback: Callable[[Any], None] | None) -> None:
        if self.receive() and callback is not None:
            callback(self._latest)

    def _drain(self) -> tuple[int | None, bool]:
        """Дескриптор последнего снимка в очереди (прежние закрываются) и признак конца канала."""
        last = None
        while True:
            try:
                msg, fds, _flags, _addr = socket.recv_fds(self._sock, 16, _MAX_FDS)
            except BlockingIOError:
                return last, False
            except OSError:
                return last, True
            if not msg and not fds:
                return last, True
            if fds:
                if last is not None:
                    os.close(last)
                *skipped, last = fds
                for fd in skipped:
                    os.close(fd)


def _require() -> None:
    if not SUPPORTED:
        raise NotImplementedError("shmobj.channel: нужны memfd_create и SCM_RIGHTS (Linux)")


def _check_socket(sock: socket.socket) -> None:
    if sock.family != socket.AF_UNIX or sock.type != socket.SOCK_SEQPACKET:
        raise ValueError("shmobj.channel: нужен сокет AF_UNIX SOCK_SEQPACKET")


def _sealed_memfd(name: str, obj: Any, dedup: bool, checksum: bool) -> int:
    fd = os.memfd_create(name, os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC)
    out = None

    def alloc(size: int) -> mmap.mmap:
        nonlocal out
        os.ftruncate(fd, size)
        out = mmap.mmap(fd, size)
        return out

    try:
        try:
            dumps_into(obj, alloc, dedup=dedup, checksum=checksum)  # сборка сразу в memfd
        finally:
            if out is not None:
                out.close()  # F_SEAL_WRITE не ставится, пока есть записываемое отображение
        fcntl.fcntl(fd, _F_ADD_SEALS, _SEALS)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _load(fd: int, verify: bool) -> Any:
    try:
        if fcntl.fcntl(fd, _F_GET_SEALS) & _REQUIRED_SEALS != _REQUIRED_SEALS:
            raise ValueError("shmobj.channel: снимок не запечатан от записи и укорачивания")
        buf = mmap.mmap(fd, os.fstat(fd).st_size, prot=mmap.PROT_READ)
    finally:
        os.close(fd)
    return open_segment(buf, verify=verify).root  # прокси держат mmap; отпущен последний — память уходит ядру
