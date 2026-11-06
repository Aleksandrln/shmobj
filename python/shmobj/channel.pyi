"""Канал снимков: последний снимок от писателя к читателю через память без имени (Linux).

Снимок собирается в memfd, запечатывается и уходит дескриптором по AF_UNIX SOCK_SEQPACKET.
Читатель берёт из очереди последний. После падения любой из сторон в ОС ничего не остаётся.
Контракт; семантика — doc/spec/02-api.md §6.
"""
import asyncio
import socket
from collections.abc import Callable
from typing import Final

from . import Value

SUPPORTED: Final[bool]
"""Есть memfd_create, печати и SCM_RIGHTS (Linux). Иначе конструкторы и `socketpair()` — `NotImplementedError`."""

def socketpair() -> tuple[socket.socket, socket.socket]:
    """Пара сокетов канала (AF_UNIX, SOCK_SEQPACKET); концы равноправны. В дочерний процесс — `pass_fds`."""

class Publisher:
    """Конец писателя. Владеет сокетом: переводит его в неблокирующий режим и закрывает в `close()`."""
    def __init__(self, sock: socket.socket, *, sndbuf: int = 16384, name: str = "shmobj") -> None:
        """`sndbuf` — SO_SNDBUF, ограничивает очередь непрочитанных снимков; `name` — имя memfd
        (видно в `/proc/<pid>/maps`)."""
    def publish(self, obj: object, *, dedup: bool = True, checksum: bool = True) -> bool:
        """Отправить снимок `obj`, не блокируя. False — очередь полна, снимок пропущен.
        Читатель закрыл канал — `BrokenPipeError`."""
    def fileno(self) -> int: ...
    @property
    def closed(self) -> bool: ...
    def close(self) -> None:
        """Закрыть сокет; идемпотентен."""
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
        Незапечатанный снимок — `ValueError`, `latest` прежний."""
    def attach(self, callback: Callable[[Value], None] | None = None, *,
               loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Принимать снимки в цикле asyncio (`loop` — по умолчанию текущий): `callback(latest)` на каждый новый.

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
