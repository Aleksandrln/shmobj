"""Канал снимков: последний снимок от писателя к читателям через общую память (Linux, Windows).

Писатель собирает снимок один раз и рассылает его всем подписчикам (сокетам `add`); каждый новый подписчик сразу
получает последний. После падения любой из сторон в ОС ничего не остаётся. Семантика — doc/api.md §6.

Модули: `_sock` — платформа и сокеты; `_publisher`, `_subscriber` — общее у писателей и читателей; `_memfd` (Linux) и
`_named` (Windows) — протоколы; `_aio` — всё, что требует asyncio, загружается при первом asyncio-вызове.
"""
from __future__ import annotations

import socket
from typing import Any

from ._sock import NAMED as _NAMED
from ._sock import SUPPORTED, connect, listen, socketpair, spawn_kwargs

if _NAMED:
    from ._named import NamedPublisher as _Publisher
    from ._named import NamedSubscriber as _Subscriber
else:
    from ._memfd import MemfdPublisher as _Publisher
    from ._memfd import MemfdSubscriber as _Subscriber

__all__ = ["SUPPORTED", "Publisher", "Subscriber", "connect", "connect_async", "listen", "socketpair", "spawn_kwargs"]


async def connect_async(address: Any, *, wait_writer: bool | float = False) -> socket.socket:
    """`connect` в текущем цикле asyncio: ожидание писателя не блокирует цикл и отменяется вместе с задачей."""
    from ._aio import connect as connect_in_loop

    return await connect_in_loop(address, wait_writer=wait_writer)


class Publisher(_Publisher):
    """Писатель: один снимок на всех подписчиков. Владеет их сокетами: переводит в неблокирующий
    режим и закрывает, когда подписчик ушёл, и в `close()`."""


class Subscriber(_Subscriber):
    """Конец читателя. Владеет сокетом; `latest` переживает закрытие канала."""
