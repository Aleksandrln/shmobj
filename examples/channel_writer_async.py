"""Писатель канала по постоянному адресу в asyncio: подписчиков принимает цикл, без потока.

Читатели — `channel_reader_async.py` или `channel_reader.py`. Остановка — Ctrl+C.
"""
import asyncio
import itertools
import sys

from shmobj import channel

# Linux — абстрактный AF_UNIX: файла нет, имя освобождается вместе с писателем. Windows — TCP на loopback.
ADDRESS = ("127.0.0.1", 50555) if sys.platform == "win32" else "\0shmobj-example"
INTERVAL = 1.0


def snapshot(tick):
    return {"tick": tick, "vms": {vm: {"cpu": (vm * tick) % 100 / 100} for vm in range(100, 110)}}


async def main():
    with channel.Publisher() as pub:
        pub.accept_in_loop(channel.listen(ADDRESS))  # подключения подписывает текущий цикл
        for tick in itertools.count():
            pub.publish(snapshot(tick))  # не блокирует: вызывается прямо из корутины
            await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
