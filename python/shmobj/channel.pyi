"""Канал снимков: последний снимок от писателя к читателям через общую память (Linux, Windows).

Писатель собирает снимок один раз и рассылает всем подписчикам; новый подписчик сразу получает последний.
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
"""Linux (CPython с os.memfd_create) или Windows. Иначе (macOS) конструкторы,
`socketpair()` и `spawn_kwargs()` — `NotImplementedError`."""

def socketpair() -> tuple[socket.socket, socket.socket]:
    """Пара сокетов канала: AF_UNIX SOCK_SEQPACKET (Linux), loopback TCP (Windows); концы равноправны.
    В дочерний процесс — `spawn_kwargs()`."""

def spawn_kwargs(sock: socket.socket) -> dict[str, Any]:
    """Аргументы `subprocess.Popen` и `asyncio.create_subprocess_exec`, с которыми дочерний процесс
    наследует `sock`: `pass_fds` (Linux), `startupinfo` с `handle_list` (Windows; делает `sock` наследуемым).
    В дочернем — `socket.socket(fileno=<sock.fileno() родителя>)`."""

def listen(address: Any, backlog: int = 16) -> socket.socket:
    """Слушающий сокет писателя: в `Publisher.accept_in_thread` / `accept_in_loop` или принятые
    соединения — в `Publisher.add`. Linux: `address` — путь
    AF_UNIX SOCK_SEQPACKET (файл не должен существовать; `"\\0имя"` — абстрактный, без файла).
    Windows: `(host, port)` TCP, порт 0 — свободный, узнать — `getsockname()`."""

def connect(address: Any) -> socket.socket:
    """Сокет читателя, подключённый к `listen(address)` писателя: в `Subscriber`."""

class Publisher:
    """Писатель: один снимок на всех подписчиков. Владеет их сокетами: переводит в неблокирующий
    режим и закрывает, когда подписчик ушёл, и в `close()`."""
    def __init__(self, sock: socket.socket | None = None, *, sndbuf: int = 16384, name: str = "shmobj") -> None:
        """`sock` — первый подписчик (`add`). Linux: `sndbuf` — SO_SNDBUF сокета каждого подписчика,
        ограничивает его очередь непрочитанных снимков; `name` — имя memfd (видно в `/proc/<pid>/maps`).
        Windows: `sndbuf` не используется, очередь — 4 неподтверждённых снимка на подписчика; `name` —
        префикс имён SharedMemory (`<name>-<pid>-<номер>`)."""
    def add(self, sock: socket.socket) -> None:
        """Подписать сокет; последний опубликованный снимок уходит ему сразу. Писатель закрыт — `ValueError`."""
    def accept_in_thread(self, listener: socket.socket) -> Any:
        """Подписывать (`add`) подключения к `listener` (`listen`) в фоновом потоке. Возвращает
        `listener.getsockname()` — адрес для `connect` читателей. Владеет `listener`:
        поток останавливается и `listener` закрывается в `close()`. `add`, `publish` и `close` — под
        замком, звать их можно из любого потока. Приём уже идёт или писатель закрыт — `ValueError`."""
    def accept_in_loop(self, listener: socket.socket, *, loop: asyncio.AbstractEventLoop | None = None) -> Any:
        """Подписывать (`add`) подключения к `listener` (`listen`) в цикле asyncio (`loop` — по умолчанию
        текущий), без потока: `loop.add_reader`, в цикле без него (ProactorEventLoop) — задача с
        `sock_accept`. Возвращает адрес, как `accept_in_thread`. Владеет `listener`: приём снимается с цикла и `listener` закрывается в `close()`.
        Приём уже идёт или писатель закрыт — `ValueError`."""
    @property
    def subscribers(self) -> int:
        """Число подписчиков; ушедшие убираются при `publish`."""
    def publish(self, obj: object, *, dedup: bool = True, checksum: bool = True) -> bool:
        """Собрать снимок `obj` один раз и отправить всем подписчикам, не блокируя. False — хотя бы
        у одного очередь полна, ему снимок пропущен. Снимок остаётся последним для следующих `add`,
        в том числе без подписчиков. Последний подписчик закрыл канал — `BrokenPipeError`; при приёме
        подключений (`accept_in_thread` / `accept_in_loop`) — без ошибки, следующий подписчик получит последний снимок.
        Писатель закрыт — `ValueError`."""
    @property
    def closed(self) -> bool: ...
    def close(self) -> None:
        """Остановить приём подключений, закрыть сокеты подписчиков и `listener`, отпустить снимки
        писателя; идемпотентен. Windows:
        неподтверждённые снимки закрываются, читатели их не получат."""
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
    def wait(self, timeout: float | None = None) -> bool:
        """Ждать нового снимка, блокируя поток (сон в `selectors`, без опроса): True — `latest` сменился;
        False — истёк `timeout` (None — без срока) или канал закрыт (`closed`). Исключения — как у
        `receive`. После `attach` (до `detach`) — `ValueError`."""
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
