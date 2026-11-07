"""Писатель канала снимков: отдаёт снимок каждые INTERVAL секунд, пока читатель не закроет канал.

Запускают читатели `channel_sync.py` и `channel_async.py`: `writer.py <номер сокета> [--async]`.
`publish` не блокирует, поэтому в asyncio вызывается прямо из корутины.
"""
import asyncio
import socket
import sys
import time

from shmobj import channel

INTERVAL = 0.05


def snapshot(tick: int) -> dict:
    return {"tick": tick, "vms": {vm: {"cpu": (vm * tick) % 100 / 100} for vm in range(100, 110)}}


def run_sync(pub: channel.Publisher) -> None:
    tick = 0
    while True:
        try:
            pub.publish(snapshot(tick))  # False — читатель отстал, снимок пропущен
        except BrokenPipeError:
            return  # читатель закрыл канал
        tick += 1
        time.sleep(INTERVAL)


async def run_async(pub: channel.Publisher) -> None:
    tick = 0
    while True:
        try:
            pub.publish(snapshot(tick))
        except BrokenPipeError:
            return
        tick += 1
        await asyncio.sleep(INTERVAL)


def main() -> None:
    # Windows: снимок, не подтверждённый читателем до выхода писателя, теряется
    with channel.Publisher(socket.socket(fileno=int(sys.argv[1]))) as pub:
        if "--async" in sys.argv[2:]:
            asyncio.run(run_async(pub))
        else:
            run_sync(pub)


if __name__ == "__main__":
    main()
