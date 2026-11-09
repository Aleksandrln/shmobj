"""Канал снимков без адреса в asyncio: `Subscriber.attach(callback)` вызывает колбэк на каждый новый снимок.

Писатель — дочерний процесс этого же скрипта с `--writer <номер сокета>`; `publish` не блокирует, поэтому
вызывается прямо из корутины. Цикл читателя не блокируется: приём идёт через `loop.add_reader` (на Windows, в
ProactorEventLoop, — через задачу с `sock_recv`). Получив снимок TICKS, читатель закрывает канал, и писатель выходит.
"""
import asyncio
import itertools
import socket
import sys

from shmobj import channel

TICKS = 20
INTERVAL = 0.05


def snapshot(tick: int) -> dict:
    return {"tick": tick, "vms": {vm: {"cpu": (vm * tick) % 100 / 100} for vm in range(100, 110)}}


async def writer(fileno: int) -> None:
    # Windows: снимок, не подтверждённый читателем до выхода писателя, теряется
    with channel.Publisher(socket.socket(fileno=fileno)) as pub:
        for tick in itertools.count():
            try:
                pub.publish(snapshot(tick))  # False — читатель отстал, снимок пропущен
            except BrokenPipeError:
                return  # читатель закрыл канал
            await asyncio.sleep(INTERVAL)


async def reader() -> None:
    pub_end, sub_end = channel.socketpair()
    with pub_end:  # копия у дочернего процесса; без закрытия здесь конец канала не дойдёт до читателя
        proc = await asyncio.create_subprocess_exec(sys.executable, __file__, "--writer", str(pub_end.fileno()),
                                                    **channel.spawn_kwargs(pub_end))

    done = asyncio.Event()

    def on_snapshot(root) -> None:  # исключения отсюда уходят в обработчик исключений цикла
        print(f"tick={root['tick']} cpu[101]={root['vms'][101]['cpu']}")
        if root["tick"] >= TICKS:
            done.set()

    with channel.Subscriber(sub_end) as sub:
        sub.attach(on_snapshot)
        await asyncio.wait_for(done.wait(), 30)
    await asyncio.wait_for(proc.wait(), 10)

    print(f"последний: tick={sub.latest['tick']}")  # `latest` переживает закрытие канала


if __name__ == "__main__":
    if sys.argv[1:2] == ["--writer"]:
        asyncio.run(writer(int(sys.argv[2])))
    else:
        asyncio.run(reader())
