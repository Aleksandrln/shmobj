"""Канал снимков без адреса и без asyncio: читатель запускает писателя и передаёт ему конец `socketpair()`.

Писатель — дочерний процесс этого же скрипта с `--writer <номер сокета>`. Читатель ждёт снимки блокирующим `wait()`
и берёт только последний, промежуточные пропускаются. Получив снимок TICKS, читатель закрывает канал, и писатель
выходит.
"""
import itertools
import socket
import subprocess
import sys
import time

from shmobj import channel

TICKS = 20
INTERVAL = 0.05


def snapshot(tick: int) -> dict:
    return {"tick": tick, "vms": {vm: {"cpu": (vm * tick) % 100 / 100} for vm in range(100, 110)}}


def writer(fileno: int) -> None:
    # Windows: снимок, не подтверждённый читателем до выхода писателя, теряется
    with channel.Publisher(socket.socket(fileno=fileno)) as pub:
        for tick in itertools.count():
            try:
                pub.publish(snapshot(tick))  # False — читатель отстал, снимок пропущен
            except BrokenPipeError:
                return  # читатель закрыл канал
            time.sleep(INTERVAL)


def reader() -> None:
    pub_end, sub_end = channel.socketpair()
    with pub_end:  # копия у дочернего процесса; без закрытия здесь конец канала не дойдёт до читателя
        proc = subprocess.Popen([sys.executable, __file__, "--writer", str(pub_end.fileno())],
                                **channel.spawn_kwargs(pub_end))

    with channel.Subscriber(sub_end) as sub:
        # True — пришёл новый снимок; False — писатель вышел, канал закрылся сам.
        # Ждать несколько сокетов разом — свой selectors по sub.fileno(), затем sub.receive() без ожидания.
        while sub.wait():
            root = sub.latest  # FrozenDict поверх общей памяти, без копирования
            print(f"tick={root['tick']} cpu[101]={root['vms'][101]['cpu']}")
            if root["tick"] >= TICKS:
                break
    proc.wait(timeout=10)

    print(f"последний: tick={sub.latest['tick']}")  # `latest` переживает закрытие канала


if __name__ == "__main__":
    if sys.argv[1:2] == ["--writer"]:
        writer(int(sys.argv[2]))
    else:
        reader()
