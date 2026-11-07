"""Канал снимков: последний снимок от писателя к читателям через общую память (Linux, Windows).

Писатель собирает снимок один раз и рассылает его всем подписчикам (сокетам `add`); каждый новый
подписчик сразу получает последний.

Linux: писатель собирает снимок прямо в memfd (`dumps_into`), запечатывает его от записи и изменения
размера и передаёт дескриптор по сокету AF_UNIX SOCK_SEQPACKET (SCM_RIGHTS). Читатель берёт из
очереди последний снимок (прочие закрывает), отображает его `mmap` только на чтение и открывает `loads`.
Снимок освобождается ядром, когда закрыт последний дескриптор и отпущен последний прокси.

Windows: снимок — именованная SharedMemory (`create`), по сокету (loopback TCP) идёт её имя строкой.
Читатель подключается к последнему (`attach`) и подтверждает имена; писатель держит снимок, пока
он последний или не подтверждён хоть одним подписчиком, иначе тот исчез бы раньше, чем его откроют. Объект ядра живёт, пока открыт хоть один
handle, поэтому `unlink` не нужен. Печатей нет: неизменность снимка — на писателе.

После падения любой из сторон в ОС ничего не остаётся. Семантика — doc/spec/02-api.md §6.
"""
from __future__ import annotations

import mmap
import os
import socket
import sys
from collections.abc import Callable
from typing import Any

from . import _shm
from ._core import dumps_into, open_segment

if sys.platform == "linux":
    import fcntl

# fcntl, send_fds и SOCK_SEQPACKET на Linux с Python ≥ 3.11 есть всегда; memfd_create нет у CPython,
# собранного с glibc < 2.27
_MEMFD = sys.platform == "linux" and hasattr(os, "memfd_create")
_NAMED = sys.platform == "win32"
SUPPORTED = _MEMFD or _NAMED

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
# Windows: очередь — снимки без подтверждения; каждый держит свою SharedMemory у писателя
_MAX_PENDING = 4
_EOL = b"\n"


def _require() -> None:
    if not SUPPORTED:
        raise NotImplementedError("shmobj.channel: нужен Linux с os.memfd_create или Windows")


def socketpair() -> tuple[socket.socket, socket.socket]:
    """Пара сокетов канала: AF_UNIX SOCK_SEQPACKET (Linux), loopback TCP (Windows); концы равноправны."""
    _require()
    if _MEMFD:
        return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    return socket.socketpair()


def spawn_kwargs(sock: socket.socket) -> dict[str, Any]:
    """Аргументы `subprocess.Popen` (и `asyncio.create_subprocess_exec`), с которыми дочерний процесс
    наследует `sock`; там — `socket.socket(fileno=<sock.fileno() родителя>)`."""
    _require()
    if _MEMFD:
        return {"pass_fds": (sock.fileno(),)}
    import subprocess

    sock.set_inheritable(True)  # handle_list принимает только наследуемые handle
    return {"startupinfo": subprocess.STARTUPINFO(lpAttributeList={"handle_list": [sock.fileno()]})}


def listen(address: Any, backlog: int = 16) -> socket.socket:
    """Слушающий сокет писателя: принятые соединения — в `Publisher.add`. Linux: `address` — путь
    AF_UNIX SOCK_SEQPACKET (файл не должен существовать; `"\\0имя"` — абстрактный, без файла).
    Windows: `(host, port)` TCP, порт 0 — свободный, узнать — `getsockname()`."""
    _require()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) if _MEMFD else socket.socket()
    try:
        sock.bind(address)
        sock.listen(backlog)
    except BaseException:
        sock.close()
        raise
    return sock


def connect(address: Any) -> socket.socket:
    """Сокет читателя, подключённый к `listen(address)` писателя: в `Subscriber`."""
    _require()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) if _MEMFD else socket.socket()
    try:
        sock.connect(address)
    except BaseException:
        sock.close()
        raise
    return sock


def _own(sock: socket.socket, sock_type: str) -> socket.socket:
    """Проверить тип сокета (`sock_type` — имя константы `socket`) и перевести его в неблокирующий режим."""
    if sock.type != getattr(socket, sock_type, None):
        raise ValueError(f"shmobj.channel: нужен сокет {sock_type}")
    sock.setblocking(False)
    return sock


class _End:
    """Конец читателя: владеет сокетом в неблокирующем режиме."""

    _SOCK_TYPE: str  # имя константы `socket`

    def __init__(self, sock: socket.socket) -> None:
        _require()
        self._sock = _own(sock, self._SOCK_TYPE)

    def fileno(self) -> int:
        return self._sock.fileno()

    @property
    def closed(self) -> bool:
        return self._sock.fileno() == -1

    def close(self) -> None:
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class _Subscriber(_End):
    """Общее у читателей: `latest` и приём в цикле asyncio; `receive` — у платформы."""

    def __init__(self, sock: socket.socket, *, verify: bool = False) -> None:
        super().__init__(sock)
        self._verify = verify
        self._latest: Any = None
        self._reader = None  # (цикл asyncio, дескриптор на нём, задача ожидания или None)

    @property
    def latest(self) -> Any:
        """Корень последнего принятого снимка; None — снимков ещё не было."""
        return self._latest

    def receive(self) -> bool:
        raise NotImplementedError

    def detach(self) -> None:
        """Снять сокет с цикла asyncio; сокет остаётся открытым."""
        reader, self._reader = self._reader, None
        if reader is None:
            return
        loop, fd, task = reader
        if task is not None:
            task.cancel()
        else:  # по запомненному дескриптору: сокет могли закрыть снаружи
            loop.remove_reader(fd)

    def close(self) -> None:
        self.detach()
        self._sock.close()

    def _on_readable(self, callback: Callable[[Any], None] | None) -> None:
        if self.receive() and callback is not None:
            callback(self._latest)

    async def _wait_readable(self, loop, callback) -> None:
        while not self.closed:
            try:
                await loop.sock_recv(self._sock, 0)  # IOCP: завершается, когда есть данные или конец
            except OSError:
                pass  # конец канала или закрытый сокет разберёт receive()
            try:
                self._on_readable(callback)
            except Exception as exc:
                loop.call_exception_handler({"message": "shmobj.channel: ошибка приёма снимка",
                                             "exception": exc})

    def attach(self, callback: Callable[[Any], None] | None = None, *, loop=None) -> None:
        """Принимать снимки в цикле asyncio (`loop` — по умолчанию текущий): `callback(latest)` на каждый новый.

        Исключения `receive` и `callback` уходят в обработчик исключений цикла; приём продолжается.
        """
        if loop is None:
            import asyncio

            loop = asyncio.get_running_loop()
        self.detach()
        fd = self._sock.fileno()
        try:
            loop.add_reader(fd, self._on_readable, callback)
            self._reader = loop, fd, None
        except NotImplementedError:  # ProactorEventLoop (Windows) не умеет add_reader
            self._reader = loop, fd, loop.create_task(self._wait_readable(loop, callback))


class _Publisher:
    """Общее у писателей: подписчики, последний снимок (его получает каждый новый), рассылка, закрытие.

    Платформа даёт `_subscribe(sock)` → подписчик, `_build(obj, dedup, checksum)` → снимок,
    `_send(sub, snap)` → True / False (очередь полна) / None (подписчик закрыл канал),
    `_retire(snap)` (снимок больше не последний), `_drop(sub)`, `_collect()` → кто закрыл канал.
    """

    _SOCK_TYPE: str  # имя константы `socket`

    def __init__(self, sock: socket.socket | None = None, *, sndbuf: int = 16 * 1024, name: str = "shmobj") -> None:
        _require()
        self._sndbuf = sndbuf
        self._name = name
        self._subs: list[Any] = []
        self._latest: Any = None
        self._closed = False
        if sock is not None:
            self.add(sock)

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    @property
    def closed(self) -> bool:
        return self._closed

    def add(self, sock: socket.socket) -> None:
        """Подписать сокет; последний опубликованный снимок уходит ему сразу."""
        if self._closed:
            raise ValueError("shmobj.channel: писатель закрыт")
        sub = self._subscribe(_own(sock, self._SOCK_TYPE))
        self._subs.append(sub)
        if self._latest is not None and self._send(sub, self._latest) is None:
            self._remove(sub)

    def _remove(self, sub: Any) -> None:
        self._subs.remove(sub)
        self._drop(sub)

    def publish(self, obj: Any, *, dedup: bool = True, checksum: bool = True) -> bool:
        """Собрать снимок `obj` один раз и отправить всем подписчикам. False — хотя бы у одного очередь
        полна, ему снимок пропущен. Последний подписчик закрыл канал — `BrokenPipeError`."""
        if self._closed:
            raise ValueError("shmobj.channel: писатель закрыт")
        gone = self._collect()
        for sub in gone:
            self._remove(sub)
        snap = self._build(obj, dedup, checksum)
        prev, self._latest = self._latest, snap
        if prev is not None:
            self._retire(prev)
        delivered = True
        for sub in list(self._subs):
            sent = self._send(sub, snap)
            if sent is None:
                self._remove(sub)
                gone.append(sub)
            elif not sent:
                delivered = False
        if gone and not self._subs:
            raise BrokenPipeError("shmobj.channel: подписчики закрыли канал")
        return delivered

    def close(self) -> None:
        """Закрыть сокеты подписчиков и отпустить снимки писателя; идемпотентен."""
        self._closed = True
        subs, self._subs = self._subs, []
        for sub in subs:
            self._drop(sub)
        latest, self._latest = self._latest, None
        if latest is not None:
            self._retire(latest)

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


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


class _MemfdPublisher(_Publisher):
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


class _MemfdSubscriber(_Subscriber):
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


class _NamedPublisher(_Publisher):
    """Снимок — имя SharedMemory; писатель держит её, пока она последняя или кем-то не подтверждена."""

    _SOCK_TYPE = "SOCK_STREAM"

    def __init__(self, sock: socket.socket | None = None, *, sndbuf: int = 16 * 1024, name: str = "shmobj") -> None:
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
        name = f"{self._name}-{os.getpid()}-{self._seq}"
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


class _NamedSubscriber(_Subscriber):
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


class Publisher(_NamedPublisher if _NAMED else _MemfdPublisher):
    """Писатель: один снимок на всех подписчиков. Владеет их сокетами: переводит в неблокирующий
    режим и закрывает, когда подписчик ушёл, и в `close()`."""


class Subscriber(_NamedSubscriber if _NAMED else _MemfdSubscriber):
    """Конец читателя. Владеет сокетом; `latest` переживает закрытие канала."""
