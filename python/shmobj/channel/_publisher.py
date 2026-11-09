"""Общее у писателей: подписчики, последний снимок, рассылка, приём подключений в потоке и в asyncio."""
from __future__ import annotations

import selectors
import socket
import threading
from collections.abc import Callable
from typing import Any

from ._sock import accept_pending, own, require


class PublisherBase:
    """Общее у писателей: подписчики, последний снимок (его получает каждый новый), рассылка, закрытие.

    Платформа даёт `_subscribe(sock)` → подписчик, `_build(obj, dedup, checksum)` → снимок,
    `_send(sub, snap)` → True / False (очередь полна) / None (подписчик закрыл канал),
    `_retire(snap)` (снимок больше не последний), `_drop(sub)`, `_collect()` → кто закрыл канал.

    `add`, `publish` и `close` — под `_lock`: `accept_in_thread` зовёт `add` из своего потока.
    """

    _SOCK_TYPE: str  # имя константы `socket`

    def __init__(self, sock: socket.socket | None = None, *, sndbuf: int = 16 * 1024, name: str = "shmobj") -> None:
        require()
        self._sndbuf = sndbuf
        self._name = name
        self._subs: list[Any] = []
        self._latest: Any = None
        self._closed = False
        self._lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._stop_accept: Callable[[], None] | None = None
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
        with self._lock:
            if self._closed:
                raise ValueError("shmobj.channel: писатель закрыт")
            sub = self._subscribe(own(sock, self._SOCK_TYPE))
            self._subs.append(sub)
            if self._latest is not None and self._send(sub, self._latest) is None:
                self._remove(sub)

    def _remove(self, sub: Any) -> None:
        self._subs.remove(sub)
        self._drop(sub)

    def _take_listener(self, listener: socket.socket) -> Any:
        with self._lock:
            if self._closed:
                raise ValueError("shmobj.channel: писатель закрыт")
            if self._listener is not None:
                raise ValueError("shmobj.channel: подключения уже принимаются")
            listener.setblocking(False)
            self._listener = listener
            return listener.getsockname()

    def _accept_loop(self, listener: socket.socket, wake: socket.socket) -> None:
        with selectors.DefaultSelector() as sel:
            sel.register(listener, selectors.EVENT_READ)
            sel.register(wake, selectors.EVENT_READ)
            while not any(key.fileobj is wake for key, _ in sel.select()):
                accept_pending(listener, self.add)

    def accept_in_thread(self, listener: socket.socket) -> Any:
        """Подписывать подключения к `listener` (`listen`) в фоновом потоке; вернуть его адрес для `connect`.
        Владеет `listener`: поток останавливается и `listener` закрывается в `close()`."""
        address = self._take_listener(listener)
        wake_r, wake_w = socket.socketpair()
        thread = threading.Thread(target=self._accept_loop, args=(listener, wake_r),
                                  name=f"{self._name}-accept", daemon=True)

        def stop() -> None:
            wake_w.close()  # конец канала — wake_r читаем, select просыпается
            thread.join()
            wake_r.close()

        self._stop_accept = stop
        thread.start()
        return address

    def accept_in_loop(self, listener: socket.socket, *, loop=None) -> Any:
        """Подписывать подключения к `listener` (`listen`) в цикле asyncio (`loop` — по умолчанию текущий),
        без потока; вернуть адрес `listener` для `connect`. Владеет `listener`: приём снимается с цикла и `listener` закрывается в `close()`."""
        from ._aio import accept, get_running_loop

        if loop is None:
            loop = get_running_loop()
        address = self._take_listener(listener)
        self._stop_accept = accept(loop, listener, self.add)
        return address

    def publish(self, obj: Any, *, dedup: bool = True, checksum: bool = True) -> bool:
        """Собрать снимок `obj` один раз и отправить всем подписчикам. False — хотя бы у одного очередь
        полна, ему снимок пропущен. Последний подписчик закрыл канал — `BrokenPipeError`, кроме приёма
        подключений (`accept_in_*`)."""
        with self._lock:
            return self._publish(obj, dedup, checksum)

    def _publish(self, obj: Any, dedup: bool, checksum: bool) -> bool:
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
        if gone and not self._subs and self._listener is None:  # при приёме подключений подписчики ещё придут
            raise BrokenPipeError("shmobj.channel: подписчики закрыли канал")
        return delivered

    def close(self) -> None:
        """Остановить приём подключений, закрыть сокеты подписчиков и `listener`, отпустить снимки писателя;
        идемпотентен."""
        with self._lock:
            self._closed = True
            subs, self._subs = self._subs, []
            for sub in subs:
                self._drop(sub)
            latest, self._latest = self._latest, None
            if latest is not None:
                self._retire(latest)
            stop, self._stop_accept = self._stop_accept, None
            listener, self._listener = self._listener, None
        if stop is not None:
            stop()  # без замка: поток приёма может ждать его в add
        if listener is not None:
            listener.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
