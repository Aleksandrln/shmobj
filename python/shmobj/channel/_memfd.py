"""Linux: снимок собирается прямо в memfd (`dumps_into`), запечатывается от записи и изменения размера, дескриптор
идёт по сокету AF_UNIX SOCK_SEQPACKET (SCM_RIGHTS). Читатель берёт из очереди последний (прочие закрывает), отображает
его `mmap` только на чтение и открывает `open_segment`. Снимок освобождает ядро, когда закрыт последний дескриптор
и отпущен последний прокси."""
from __future__ import annotations

import mmap
import os
import socket
import sys
from typing import Any

from .._core import dumps_into, open_segment
from ._publisher import PublisherBase
from ._subscriber import SubscriberBase

if sys.platform == "linux":
    import fcntl

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


class MemfdPublisher(PublisherBase):
    """Подписчик — сокет, снимок — дескриптор запечатанного memfd: один на всех, пока он последний."""

    _SOCK_TYPE = "SOCK_SEQPACKET"

    def _subscribe(self, sock: socket.socket) -> socket.socket:
        # буфер отправки ограничивает очередь непрочитанных снимков, каждый из которых держит свой memfd
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self._sndbuf)
        return sock

    def _collect(self) -> list[Any]:
        return []  # закрытие подписчика видно по EPIPE при отправке

    def _build(self, obj: Any, dedup: bool, checksum: bool) -> int:
        return _sealed_memfd(self._name, obj, dedup, checksum)

    def _send(self, sock: socket.socket, fd: int) -> bool | None:
        try:
            # MSG_NOSIGNAL: ушедший подписчик — EPIPE без SIGPIPE, даже если процесс не игнорирует сигнал
            socket.send_fds(sock, [_MSG], [fd], socket.MSG_NOSIGNAL)  # пока сообщение в очереди, memfd держит ядро
        except BlockingIOError:
            return False
        except OSError:
            return None
        return True

    def _retire(self, fd: int) -> None:
        os.close(fd)

    def _drop(self, sock: socket.socket) -> None:
        sock.close()


def _load_memfd(fd: int, verify: bool) -> Any:
    try:
        if fcntl.fcntl(fd, _F_GET_SEALS) & _REQUIRED_SEALS != _REQUIRED_SEALS:
            raise ValueError("shmobj.channel: снимок не запечатан от записи и укорачивания")
        buf = mmap.mmap(fd, os.fstat(fd).st_size, prot=mmap.PROT_READ)
    finally:
        os.close(fd)
    return open_segment(buf, verify=verify).root  # прокси держат mmap; отпущен последний — память уходит ядру


class MemfdSubscriber(SubscriberBase):
    _SOCK_TYPE = "SOCK_SEQPACKET"

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

    def receive(self) -> bool:
        """Разобрать очередь без ожидания: True — `latest` сменился. Конец канала закрывает сокет."""
        if self.closed:
            return False
        fd, eof = self._drain()
        if eof:
            self.close()
        if fd is None:
            return False
        self._latest = _load_memfd(fd, self._verify)
        return True
