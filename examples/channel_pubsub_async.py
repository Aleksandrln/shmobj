"""Канал снимков по постоянному адресу в asyncio.

`python examples/channel_pubsub_async.py writer` — писатель: подписчиков принимает цикл, без потока.
`python examples/channel_pubsub_async.py reader` — читатель: ожидание писателя и снимков не блокирует цикл, после
ухода писателя — новое подключение.

Читателей сколько угодно, порядок запуска любой; к этому писателю подключается и читатель из `channel_pubsub.py`.
Колбэк на каждый снимок вместо цикла ожидания — `Subscriber.attach(callback)`, см. `channel_spawn_async.py`.
Остановка — Ctrl+C.
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


async def writer():
    with channel.Publisher() as pub:
        pub.accept_in_loop(channel.listen(ADDRESS))  # подключения подписывает текущий цикл
        for tick in itertools.count():
            pub.publish(snapshot(tick))  # не блокирует: вызывается прямо из корутины
            await asyncio.sleep(INTERVAL)


async def reader():
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
    role = {"writer": writer, "reader": reader}.get(sys.argv[1] if len(sys.argv) == 2 else None)
    if role is None:
        sys.exit("запуск: python examples/channel_pubsub_async.py writer|reader")
    try:
        asyncio.run(role())
    except KeyboardInterrupt:
        pass
