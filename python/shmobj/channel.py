"""Канал снимков: последний снимок от писателя к читателю через общую память (Linux, Windows).

Linux: писатель собирает снимок прямо в memfd (`dumps_into`), запечатывает его от записи и изменения
размера и передаёт дескриптор по сокету AF_UNIX SOCK_SEQPACKET (SCM_RIGHTS). Читатель берёт из
очереди последний снимок (прочие закрывает), отображает его `mmap` только на чтение и открывает `loads`.
Снимок освобождается ядром, когда закрыт последний дескриптор и отпущен последний прокси.

Windows: снимок — именованная SharedMemory (`create`), по сокету (loopback TCP) идёт её имя строкой.
Читатель подключается к последнему (`attach`) и подтверждает имена; писатель держит снимок до
подтверждения, иначе тот исчез бы раньше, чем его откроют. Объект ядра живёт, пока открыт хоть один
handle, поэтому `unlink` не нужен. Печатей нет: неизменность снимка — на писателе.

После падения любой из сторон в ОС ничего не остаётся. Семантика — doc/spec/02-api.md §6.
"""
from __future__ import annotations

import mmap
import os
import socket
from collections.abc import Callable
from typing import Any

from . import _shm
from ._core import dumps_into, open_segment

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

_MEMFD = (fcntl is not None and hasattr(os, "memfd_create") and hasattr(os, "MFD_ALLOW_SEALING")
          and hasattr(socket, "send_fds") and hasattr(socket, "SOCK_SEQPACKET"))
_NAMED = os.name == "nt"
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
        raise NotImplementedError("shmobj.channel: нужны memfd_create и SCM_RIGHTS (Linux) или Windows")


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


class _End:
    """Конец канала: владеет сокетом в неблокирующем режиме."""

    _SOCK_TYPE: str  # имя константы `socket`

    def __init__(self, sock: socket.socket) -> None:
        _require()
        if sock.type != getattr(socket, self._SOCK_TYPE, None):
            raise ValueError(f"shmobj.channel: нужен сокет {self._SOCK_TYPE}")
        sock.setblocking(False)
        self._sock = sock

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


class _MemfdPublisher(_End):
    _SOCK_TYPE = "SOCK_SEQPACKET"

    def __init__(self, sock: socket.socket, *, sndbuf: int = 16 * 1024, name: str = "shmobj") -> None:
        super().__init__(sock)
        # буфер отправки ограничивает очередь непрочитанных снимков, каждый из которых держит свой memfd
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
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


class _NamedPublisher(_End):
    _SOCK_TYPE = "SOCK_STREAM"

    def __init__(self, sock: socket.socket, *, sndbuf: int = 16 * 1024, name: str = "shmobj") -> None:
        super().__init__(sock)
        self._name = name
        self._seq = 0
        self._pending: dict[bytes, Any] = {}  # имя → Segment: отправлен, не подтверждён
        self._acks = b""  # неполная строка подтверждения

    def _collect_acks(self) -> None:
        lines, self._acks, eof = _read_lines(self._sock, self._acks)
        for line in lines:  # чужие имена (прежний писатель того же сокета) пропускаются
            seg = self._pending.pop(line, None)
            if seg is not None:
                _release(seg)
        if eof:
            raise BrokenPipeError("shmobj.channel: читатель закрыл канал")

    def publish(self, obj: Any, *, dedup: bool = True, checksum: bool = True) -> bool:
        """Отправить снимок `obj`. False — очередь полна (читатель не разбирает её), снимок пропущен."""
        self._collect_acks()
        if len(self._pending) >= _MAX_PENDING:
            return False
        name = f"{self._name}-{os.getpid()}-{self._seq}"
        self._seq += 1
        seg = _shm.create(obj, name, dedup=dedup, checksum=checksum)
        try:
            self._sock.sendall(name.encode() + _EOL)  # строки короткие, а в очереди их не больше _MAX_PENDING
        except BaseException as exc:
            _release(seg)
            if isinstance(exc, ConnectionError):
                raise BrokenPipeError("shmobj.channel: читатель закрыл канал") from exc
            raise
        self._pending[name.encode()] = seg
        return True

    def close(self) -> None:
        """Закрыть сокет и неподтверждённые снимки: их читатель уже не откроет."""
        pending, self._pending = self._pending, {}
        for seg in pending.values():
            _release(seg)
        self._sock.close()


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
    """Конец писателя. Владеет сокетом: переводит его в неблокирующий режим и закрывает в `close()`."""


class Subscriber(_NamedSubscriber if _NAMED else _MemfdSubscriber):
    """Конец читателя. Владеет сокетом; `latest` переживает закрытие канала."""
