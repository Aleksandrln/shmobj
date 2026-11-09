"""Платформа канала и сокеты: проверка поддержки, пара сокетов, слушающий сокет, подключение с ожиданием писателя."""
from __future__ import annotations

import os
import socket
import sys
import time
from collections.abc import Callable
from typing import Any

# fcntl, send_fds и SOCK_SEQPACKET на Linux с Python ≥ 3.11 есть всегда; memfd_create нет у CPython,
# собранного с glibc < 2.27
MEMFD = sys.platform == "linux" and hasattr(os, "memfd_create")
NAMED = sys.platform == "win32"
SUPPORTED = MEMFD or NAMED
# адрес никто не слушает: нет слушающего сокета или файла сокета
NO_WRITER = (ConnectionRefusedError, FileNotFoundError)
_WAIT_WRITER_POLL = 0.2  # секунд между попытками подключения в ожидании писателя


def require() -> None:
    if not SUPPORTED:
        raise NotImplementedError("shmobj.channel: нужен Linux с os.memfd_create или Windows")


def own(sock: socket.socket, sock_type: str) -> socket.socket:
    """Проверить тип сокета (`sock_type` — имя константы `socket`) и перевести его в неблокирующий режим."""
    if sock.type != getattr(socket, sock_type, None):
        raise ValueError(f"shmobj.channel: нужен сокет {sock_type}")
    sock.setblocking(False)
    return sock


def socketpair() -> tuple[socket.socket, socket.socket]:
    """Пара сокетов канала: AF_UNIX SOCK_SEQPACKET (Linux), loopback TCP (Windows); концы равноправны."""
    require()
    if MEMFD:
        return socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    return socket.socketpair()


def spawn_kwargs(sock: socket.socket) -> dict[str, Any]:
    """Аргументы `subprocess.Popen` (и `asyncio.create_subprocess_exec`), с которыми дочерний процесс
    наследует `sock`; там — `socket.socket(fileno=<sock.fileno() родителя>)`."""
    require()
    if MEMFD:
        return {"pass_fds": (sock.fileno(),)}
    import subprocess

    sock.set_inheritable(True)  # handle_list принимает только наследуемые handle
    return {"startupinfo": subprocess.STARTUPINFO(lpAttributeList={"handle_list": [sock.fileno()]})}


def new_socket() -> socket.socket:
    return socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) if MEMFD else socket.socket()


def listen(address: Any, backlog: int = 16) -> socket.socket:
    """Слушающий сокет писателя: в `Publisher.accept_in_thread` / `accept_in_loop` или принятые
    соединения — в `Publisher.add`. Linux: `address` — путь
    AF_UNIX SOCK_SEQPACKET (файл не должен существовать; `"\\0имя"` — абстрактный, без файла).
    Windows: `(host, port)` TCP, порт 0 — свободный, узнать — `getsockname()`."""
    require()
    sock = new_socket()
    try:
        sock.bind(address)
        sock.listen(backlog)
    except BaseException:
        sock.close()
        raise
    return sock


def accept_pending(listener: socket.socket, add: Callable[[socket.socket], None]) -> None:
    """`add` на каждое ожидающее подключение, не блокируя; `add` бросил ValueError (писатель закрыт) — стоп."""
    while True:
        try:
            conn = listener.accept()[0]
        except BlockingIOError:
            return
        except ConnectionError:  # читатель ушёл, не дождавшись accept
            continue
        try:
            add(conn)
        except ValueError:
            conn.close()
            return


def writer_deadline(wait_writer: bool | float) -> float | None:
    """Срок ожидания писателя по `time.monotonic()`; None — без срока."""
    if wait_writer is True:
        return None
    return time.monotonic() + (wait_writer or 0)


def next_try(deadline: float | None) -> float | None:
    """Пауза перед следующей попыткой подключения; None — срок вышел."""
    if deadline is None:
        return _WAIT_WRITER_POLL
    left = deadline - time.monotonic()
    return min(_WAIT_WRITER_POLL, left) if left > 0 else None


def connect(address: Any, *, wait_writer: bool | float = False) -> socket.socket:
    """Сокет читателя, подключённый к `listen(address)` писателя: в `Subscriber`.
    Адрес никто не слушает: `wait_writer=False` — исключение сразу, `True` — ждать писателя без срока,
    число — не дольше стольких секунд. Ждёт, блокируя поток; в asyncio — `connect_async`."""
    require()
    deadline = writer_deadline(wait_writer)
    while True:
        sock = new_socket()
        try:
            sock.connect(address)
            return sock
        except NO_WRITER:
            sock.close()
            pause = next_try(deadline)
            if pause is None:
                raise
        except BaseException:
            sock.close()
            raise
        time.sleep(pause)
