"""Windows: снимок — именованная SharedMemory (`create`), по сокету (loopback TCP) идёт её имя строкой. Читатель
открывает последний (`attach`) и подтверждает имена; писатель держит снимок, пока он последний или не подтверждён хоть
одним подписчиком, иначе тот исчез бы раньше, чем его откроют. Объект ядра живёт, пока открыт хоть один handle, поэтому
`unlink` не нужен. Печатей нет: неизменность снимка — на писателе. На Linux протокол работает поверх `/dev/shm` (тесты)."""
from __future__ import annotations

import itertools
import os
import socket
from typing import Any

from .. import _shm
from ._publisher import PublisherBase
from ._subscriber import SubscriberBase

# очередь — снимки без подтверждения; каждый держит свою SharedMemory у писателя
_MAX_PENDING = 4
_EOL = b"\n"
# номер писателя в процессе: имена снимков разных писателей не совпадают и при одном `name`
_PUBLISHERS = itertools.count()


def _release(seg) -> None:
    seg.unlink()  # Windows — ничего; POSIX (режим в тестах) — имя из /dev/shm
    seg.close()


def _read_lines(sock: socket.socket, tail: bytes) -> tuple[list[bytes], bytes, bool]:
    """Всё, что есть в сокете, без ожидания: полные строки, неполный хвост, признак конца канала."""
    eof = False
    chunks = [tail]
    while True:
        try:
            chunk = sock.recv(4096)
        except BlockingIOError:
            break
        except OSError:
            eof = True
            break
        if not chunk:
            eof = True
            break
        chunks.append(chunk)
    *lines, tail = b"".join(chunks).split(_EOL)
    return lines, tail, eof


class _NamedSub:
    """Подписчик именованного писателя: сокет, неполная строка подтверждения, отправленные без подтверждения имена."""

    __slots__ = ("sock", "acks", "pending")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.acks = b""
        self.pending: set[bytes] = set()


class NamedPublisher(PublisherBase):
    """Снимок — имя SharedMemory; писатель держит её, пока она последняя или кем-то не подтверждена."""

    _SOCK_TYPE = "SOCK_STREAM"

    def __init__(self, sock: socket.socket | None = None, *, sndbuf: int = 16 * 1024, name: str = "shmobj") -> None:
        self._prefix = f"{name}-{os.getpid()}-{next(_PUBLISHERS)}"  # имя снимка — `<prefix>-<номер>`
        self._seq = 0
        self._segments: dict[bytes, Any] = {}  # имя → Segment
        super().__init__(sock, sndbuf=sndbuf, name=name)

    def _subscribe(self, sock: socket.socket) -> _NamedSub:
        return _NamedSub(sock)

    def _collect(self) -> list[Any]:
        gone = []
        for sub in self._subs:  # чужие имена (прежний писатель того же сокета) пропускаются
            lines, sub.acks, eof = _read_lines(sub.sock, sub.acks)
            sub.pending.difference_update(lines)
            if eof:
                gone.append(sub)
        self._release_unused()
        return gone

    def _build(self, obj: Any, dedup: bool, checksum: bool) -> bytes:
        name = f"{self._prefix}-{self._seq}"
        self._seq += 1
        self._segments[name.encode()] = _shm.create(obj, name, dedup=dedup, checksum=checksum)
        return name.encode()

    def _send(self, sub: _NamedSub, name: bytes) -> bool | None:
        if len(sub.pending) >= _MAX_PENDING:
            return False
        try:
            sub.sock.sendall(name + _EOL)  # строки короткие, а в очереди их не больше _MAX_PENDING
        except OSError:
            return None
        sub.pending.add(name)
        return True

    def _retire(self, name: bytes) -> None:
        self._release_unused()

    def _drop(self, sub: _NamedSub) -> None:
        sub.sock.close()
        self._release_unused()

    def _release_unused(self) -> None:
        """Закрыть снимки, которые не последние и которых не ждёт ни один подписчик."""
        held = set().union(*(sub.pending for sub in self._subs))
        held.add(self._latest)
        for name in [n for n in self._segments if n not in held]:
            _release(self._segments.pop(name))


class NamedSubscriber(SubscriberBase):
    _SOCK_TYPE = "SOCK_STREAM"

    def __init__(self, sock: socket.socket, *, verify: bool = False) -> None:
        super().__init__(sock, verify=verify)
        self._tail = b""  # неполная строка имени

    def _attach_last(self, names: list[bytes]) -> Any:
        """Корень последнего снимка, который ещё есть; None — писатель их уже закрыл (вышел)."""
        for name in reversed(names):
            try:
                seg = _shm.attach(name.decode(errors="replace"), verify=self._verify)
            except FileNotFoundError:
                continue
            return seg.root  # прокси держат Segment; отпущен последний — SharedMemory закрывается
        return None

    def receive(self) -> bool:
        """Разобрать очередь без ожидания: True — `latest` сменился. Конец канала закрывает сокет."""
        if self.closed:
            return False
        names, self._tail, eof = _read_lines(self._sock, self._tail)
        names = [n for n in names if n]
        try:
            root = self._attach_last(names)
        finally:
            if names and not eof:
                try:  # снимок открыт — писатель может закрыть свои копии всех принятых
                    self._sock.sendall(b"".join(n + _EOL for n in names))
                except OSError:
                    pass  # писатель ушёл: его снимки освободила ОС
            if eof:
                self.close()
        if root is None:
            return False
        self._latest = root
        return True
