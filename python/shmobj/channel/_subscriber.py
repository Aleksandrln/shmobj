"""Общее у читателей: сокет, `latest`, синхронный `wait`; приём в asyncio — `_aio.LoopReceiver`."""
from __future__ import annotations

import selectors
import socket
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ._sock import own, require

if TYPE_CHECKING:
    from ._aio import LoopReceiver


class SubscriberBase:
    """Владеет сокетом в неблокирующем режиме; `receive` и тип сокета `_SOCK_TYPE` — у платформы."""

    _SOCK_TYPE: str  # имя константы `socket`

    def __init__(self, sock: socket.socket, *, verify: bool = False) -> None:
        require()
        self._sock = own(sock, self._SOCK_TYPE)
        self._verify = verify
        self._latest: Any = None
        self._receiver: LoopReceiver | None = None  # приём в цикле asyncio: attach, wait_async
        self._selector: selectors.BaseSelector | None = None  # для wait, создаётся при первом ожидании

    @property
    def latest(self) -> Any:
        """Корень последнего принятого снимка; None — снимков ещё не было."""
        return self._latest

    @property
    def closed(self) -> bool:
        return self._sock.fileno() == -1

    def fileno(self) -> int:
        return self._sock.fileno()

    def receive(self) -> bool:
        raise NotImplementedError

    def wait(self, timeout: float | None = None) -> bool:
        """Ждать нового снимка, блокируя поток: True — `latest` сменился; False — истёк `timeout`
        или канал закрыт (`closed`). Не вместе с приёмом в цикле asyncio (`attach`, `wait_async`)."""
        if self._receiver is not None:
            raise ValueError("shmobj.channel: читатель принимает в цикле asyncio (attach, wait_async); "
                             "снять — detach()")
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.receive():
            if self.closed:
                return False
            left = None if deadline is None else deadline - time.monotonic()
            if left is not None and left <= 0:
                return False
            if self._selector is None:
                self._selector = selectors.DefaultSelector()
                self._selector.register(self._sock, selectors.EVENT_READ)
            self._selector.select(left)  # спит в ядре, пока не придёт снимок или конец канала
        return True

    async def wait_async(self, timeout: float | None = None) -> bool:
        """`wait` для asyncio: ждать нового снимка, не блокируя цикл. True — `latest` сменился с прошлого
        `wait_async`; False — истёк `timeout`, канал закрыт (`closed`) или приём снят `detach`.

        Работает вместе с `attach` и в нескольких задачах сразу; без `attach` включает приём без колбэка.
        """
        from ._aio import LoopReceiver, get_running_loop

        loop = get_running_loop()
        if self._receiver is None:
            if self.closed:
                return False
            self._receiver = LoopReceiver(self, self._sock, loop, None)
        elif self._receiver.loop is not loop:
            raise ValueError("shmobj.channel: читатель принимает в другом цикле asyncio")
        return await self._receiver.wait(timeout)

    def attach(self, callback: Callable[[Any], None] | None = None, *, loop=None) -> None:
        """Принимать снимки в цикле asyncio (`loop` — по умолчанию текущий): `callback(latest)` на каждый новый.

        Ошибку `receive` получают ждущие `wait_async`; без них при `callback` — обработчик исключений цикла (как и
        ошибки `callback`), без `callback` — следующий `wait_async`. Приём продолжается.
        """
        from ._aio import LoopReceiver, get_running_loop

        if loop is None:
            loop = get_running_loop()
        receiver = self._receiver
        if receiver is not None and receiver.loop is loop:
            receiver.callback = callback  # ждущие wait_async ждут дальше
        else:
            self.detach()
            self._receiver = LoopReceiver(self, self._sock, loop, callback)

    def detach(self) -> None:
        """Снять сокет с цикла asyncio; сокет остаётся открытым, ждущие `wait_async` получают False."""
        receiver, self._receiver = self._receiver, None
        if receiver is not None:
            receiver.stop()

    def close(self) -> None:
        self.detach()
        if self._selector is not None:
            self._selector.close()
            self._selector = None
        self._sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
