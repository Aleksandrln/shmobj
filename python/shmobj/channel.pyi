"""Канал снимков: последний снимок от писателя к читателю через общую память (Linux, Windows).

Linux: снимок собирается в memfd, запечатывается и уходит дескриптором по AF_UNIX SOCK_SEQPACKET.
Windows: снимок — именованная SharedMemory, по loopback TCP идёт имя, читатель подтверждает приём.
Читатель берёт из очереди последний. После падения любой из сторон в ОС ничего не остаётся.
Контракт; семантика — doc/spec/02-api.md §6.
"""
import asyncio
import socket
from collections.abc import Callable
from typing import Any, Final

from . import Value

SUPPORTED: Final[bool]
"""Linux с memfd_create, печатями и SCM_RIGHTS или Windows. Иначе (macOS) конструкторы,
`socketpair()` и `spawn_kwargs()` — `NotImplementedError`."""

def socketpair() -> tuple[socket.socket, socket.socket]:
    """Пара сокетов канала: AF_UNIX SOCK_SEQPACKET (Linux), loopback TCP (Windows); концы равноправны.
    В дочерний процесс — `spawn_kwargs()`."""

def spawn_kwargs(sock: socket.socket) -> dict[str, Any]:
    """Аргументы `subprocess.Popen` и `asyncio.create_subprocess_exec`, с которыми дочерний процесс
    наследует `sock`: `pass_fds` (Linux), `startupinfo` с `handle_list` (Windows; делает `sock` наследуемым).
    В дочернем — `socket.socket(fileno=<sock.fileno() родителя>)`."""

class Publisher:
    """Конец писателя. Владеет сокетом: переводит его в неблокирующий режим и закрывает в `close()`."""
    def __init__(self, sock: socket.socket, *, sndbuf: int = 16384, name: str = "shmobj") -> None:
        """Linux: `sndbuf` — SO_SNDBUF, ограничивает очередь непрочитанных снимков; `name` — имя memfd
        (видно в `/proc/<pid>/maps`). Windows: `sndbuf` не используется, очередь — 4 неподтверждённых
        снимка; `name` — префикс имён SharedMemory (`<name>-<pid>-<номер>`)."""
    def publish(self, obj: object, *, dedup: bool = True, checksum: bool = True) -> bool:
        """Отправить снимок `obj`, не блокируя. False — очередь полна, снимок пропущен.
        Читатель закрыл канал — `BrokenPipeError`."""
    def fileno(self) -> int: ...
    @property
    def closed(self) -> bool: ...
    def close(self) -> None:
        """Закрыть сокет; идемпотентен. Windows: неподтверждённые снимки закрываются, читатель их не получит."""
    def __enter__(self) -> Publisher: ...
    def __exit__(self, *exc: object) -> None: ...

class Subscriber:
    """Конец читателя. Владеет сокетом; `latest` переживает закрытие канала."""
    def __init__(self, sock: socket.socket, *, verify: bool = False) -> None:
        """`verify` — проверять контрольную сумму каждого снимка (`loads`)."""
    @property
    def latest(self) -> Value:
        """Корень последнего принятого снимка; None — снимков ещё не было."""
    def receive(self) -> bool:
        """Разобрать очередь без ожидания: True — `latest` сменился. Конец канала закрывает сокет.
        Linux: незапечатанный снимок — `ValueError`, `latest` прежний. Windows: снимок, который
        писатель уже закрыл (вышел), пропускается."""
    def attach(self, callback: Callable[[Value], None] | None = None, *,
               loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Принимать снимки в цикле asyncio (`loop` — по умолчанию текущий): `callback(latest)` на каждый новый.
        `loop.add_reader`; цикл без него (ProactorEventLoop) — задача с `sock_recv(sock, 0)`.

        Исключения `receive` и `callback` уходят в обработчик исключений цикла; приём продолжается.
        """
    def detach(self) -> None:
        """Снять сокет с цикла asyncio; сокет остаётся открытым."""
    def fileno(self) -> int: ...
    @property
    def closed(self) -> bool: ...
    def close(self) -> None:
        """Снять с цикла и закрыть сокет; идемпотентен."""
    def __enter__(self) -> Subscriber: ...
    def __exit__(self, *exc: object) -> None: ...
