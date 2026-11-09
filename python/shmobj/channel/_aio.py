"""Канал в asyncio: подключение, приём подключений и приём снимков в цикле. Синхронная часть пакета
импортирует модуль только из своих asyncio-методов: без них `asyncio` не загружается."""
from __future__ import annotations

import asyncio
import socket
from asyncio import get_running_loop
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ._sock import (
    NO_WRITER,
    accept_pending,
    new_socket,
    next_try,
    require,
    writer_deadline,
)

if TYPE_CHECKING:
    from ._subscriber import SubscriberBase


async def connect(address: Any, *, wait_writer: bool | float = False) -> socket.socket:
    require()
    loop = get_running_loop()
    deadline = writer_deadline(wait_writer)
    while True:
        sock = new_socket()
        sock.setblocking(False)
        try:
            await loop.sock_connect(sock, address)
            return sock
        except NO_WRITER:
            sock.close()
            pause = next_try(deadline)
            if pause is None:
                raise
        except BaseException:
            sock.close()
            raise
        await asyncio.sleep(pause)


async def _accept_each(loop, listener: socket.socket, add: Callable[[socket.socket], None]) -> None:
    while True:
        try:
            conn = (await loop.sock_accept(listener))[0]
        except ConnectionError:  # читатель ушёл, не дождавшись accept
            continue
        try:
            add(conn)
        except ValueError:  # писатель закрыт
            conn.close()
            return


def accept(loop, listener: socket.socket, add: Callable[[socket.socket], None]) -> Callable[[], None]:
    """`add` на каждое подключение к `listener` в цикле asyncio; вернуть функцию снятия."""
    fd = listener.fileno()
    try:
        loop.add_reader(fd, accept_pending, listener, add)
    except NotImplementedError:  # ProactorEventLoop (Windows) не умеет add_reader
        return loop.create_task(_accept_each(loop, listener, add)).cancel
    return lambda: loop.remove_reader(fd)


def _watch_readable(loop, sock: socket.socket, on_readable: Callable[[], None]) -> Callable[[], None]:
    """Вызывать `on_readable()` в цикле asyncio, когда сокет готов к чтению; вернуть функцию снятия."""
    fd = sock.fileno()
    try:
        loop.add_reader(fd, on_readable)
    except NotImplementedError:  # ProactorEventLoop (Windows) не умеет add_reader
        async def poll() -> None:
            while sock.fileno() != -1:
                try:
                    await loop.sock_recv(sock, 0)  # IOCP: завершается, когда есть данные или конец
                except OSError:
                    pass  # конец канала или закрытый сокет разберёт on_readable
                on_readable()

        return loop.create_task(poll()).cancel
    return lambda: loop.remove_reader(fd)  # по запомненному дескриптору: сокет могли закрыть снаружи


class _Broadcast:
    """Событие в цикле asyncio, которое будит всех ждущих. Ждущий передаёт `fired`, увиденный в прошлый раз:
    события между ожиданиями не теряются."""

    def __init__(self, loop) -> None:
        self.fired = 0
        self.closed = False
        self._loop = loop
        self._waiters: list = []

    async def wait(self, seen: int, timeout: float | None) -> bool:
        """True — `fired` уже не `seen`; False — истёк `timeout` или `close`."""
        deadline = None if timeout is None else self._loop.time() + timeout
        while self.fired == seen:
            if self.closed:
                return False
            left = None if deadline is None else deadline - self._loop.time()
            if left is not None and left <= 0:
                return False
            waiter = self._loop.create_future()
            self._waiters.append(waiter)
            try:
                await asyncio.wait_for(waiter, left)
            except TimeoutError:
                pass  # срок проверит следующий проход
            finally:
                if waiter in self._waiters:
                    self._waiters.remove(waiter)
        return True

    def fire(self) -> None:
        self.fired += 1
        self._wake()

    def fail(self, error: Exception) -> bool:
        """Поднять `error` в ждущих; False — ждущих не было."""
        return self._wake(error)

    def close(self) -> None:
        self.closed = True
        self._wake()

    def _wake(self, error: Exception | None = None) -> bool:
        waiters, self._waiters = [w for w in self._waiters if not w.done()], []
        for waiter in waiters:
            if error is None:
                waiter.set_result(None)
            else:
                waiter.set_exception(error)
        return bool(waiters)


class LoopReceiver:
    """Приём снимков читателя в цикле asyncio, от `attach` или первого `wait_async` до `detach`: сокет читает
    только он, новый снимок будит всех ждущих `wait` и вызывает `callback`.

    Ошибку приёма получают ждущие `wait`; без них — следующий `wait`, если колбэка нет, иначе обработчик
    исключений цикла. Ему же уходит ошибка, которую до `stop` никто не забрал.
    """

    def __init__(self, sub: SubscriberBase, sock: socket.socket, loop,
                 callback: Callable[[Any], None] | None) -> None:
        self.loop = loop
        self.callback = callback
        self._sub = sub
        self._snapshots = _Broadcast(loop)
        self._seen = 0  # снимков, отданных wait
        self._error: Exception | None = None  # для следующего wait
        self._unwatch = _watch_readable(loop, sock, self._on_readable)

    async def wait(self, timeout: float | None) -> bool:
        if self._error is not None:
            error, self._error = self._error, None
            raise error
        self._on_readable(raise_errors=True)  # снимок уже в сокете, а приём цикла до него ещё не дошёл
        if not await self._snapshots.wait(self._seen, timeout):
            return False
        self._seen = self._snapshots.fired
        return True

    def stop(self) -> None:
        """Снять сокет с цикла; ждущие `wait` получают False."""
        if self._snapshots.closed:
            return
        self._unwatch()
        self._snapshots.close()
        if self._error is not None:
            self._report("ошибка приёма снимка", self._error)
            self._error = None

    def _report(self, message: str, exc: Exception) -> None:
        self.loop.call_exception_handler({"message": f"shmobj.channel: {message}", "exception": exc})

    def _on_readable(self, raise_errors: bool = False) -> None:
        try:
            changed = self._sub.receive()  # конец канала закрывает читателя, а с ним и этот приём
        except Exception as exc:
            if raise_errors:
                raise
            if self._snapshots.fail(exc):
                pass
            elif self.callback is None:
                self._error = exc
            else:
                self._report("ошибка приёма снимка", exc)
            return
        if not changed:
            return
        self._snapshots.fire()
        if self.callback is not None:
            try:
                self.callback(self._sub.latest)
            except Exception as exc:
                self._report("ошибка в колбэке", exc)
