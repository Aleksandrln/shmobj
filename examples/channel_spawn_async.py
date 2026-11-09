"""Канал снимков в asyncio: `Subscriber.attach(callback)` вызывает колбэк на каждый новый снимок.

Писатель — дочерний процесс `channel_spawn_writer.py --async`. Цикл не блокируется: приём идёт через
`loop.add_reader` (на Windows, в ProactorEventLoop, — через задачу с `sock_recv`).
Получив снимок TICKS, читатель закрывает канал, и писатель выходит.
"""
import asyncio
import sys
from pathlib import Path

from shmobj import channel

WRITER = Path(__file__).with_name("channel_spawn_writer.py")
TICKS = 20


async def main() -> None:
    pub_end, sub_end = channel.socketpair()
    with pub_end:
        writer = await asyncio.create_subprocess_exec(sys.executable, str(WRITER), str(pub_end.fileno()), "--async",
                                                      **channel.spawn_kwargs(pub_end))

    done = asyncio.Event()

    def on_snapshot(root) -> None:  # исключения отсюда уходят в обработчик исключений цикла
        print(f"tick={root['tick']} cpu[101]={root['vms'][101]['cpu']}")
        if root["tick"] >= TICKS:
            done.set()

    with channel.Subscriber(sub_end) as sub:
        sub.attach(on_snapshot)
        await asyncio.wait_for(done.wait(), 30)
    await asyncio.wait_for(writer.wait(), 10)

    print(f"последний: tick={sub.latest['tick']}")


if __name__ == "__main__":
    asyncio.run(main())
