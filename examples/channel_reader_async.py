"""Читатель канала в asyncio: ожидание писателя и снимков не блокирует цикл, после ухода писателя — новое
подключение.

Писатель — `channel_writer_async.py` или `channel_writer.py`. Остановка — Ctrl+C.
Колбэк на каждый снимок вместо цикла ожидания — `Subscriber.attach(callback)`, см. `channel_spawn_async.py`.
"""
import asyncio
import sys

from shmobj import channel

ADDRESS = ("127.0.0.1", 50555) if sys.platform == "win32" else "\0shmobj-example"


async def main():
    while True:
        # Ждать писателя: попытки подключения идут в цикле, остальные задачи работают.
        sock = await channel.connect_async(ADDRESS, wait_writer=True)
        with channel.Subscriber(sock) as sub:
            # True — sub.latest сменился, False — писатель закрылся или упал.
            while await sub.wait_async():
                root = sub.latest  # FrozenDict поверх общей памяти, без копирования
                print(f"tick={root['tick']} cpu[101]={root['vms'][101]['cpu']}")
        print("писатель ушёл, жду нового")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
