"""Канал снимков без asyncio: читатель ждёт готовности сокета в `selectors` и разбирает очередь `receive()`.

Писатель — дочерний процесс `writer.py`. Читатель берёт только последний снимок, промежуточные
пропускаются. Получив снимок TICKS, читатель закрывает канал, и писатель выходит.
"""
import selectors
import subprocess
import sys
from pathlib import Path

from shmobj import channel

WRITER = Path(__file__).with_name("writer.py")
TICKS = 20


def main() -> None:
    pub_end, sub_end = channel.socketpair()
    with pub_end:  # копия у дочернего процесса; без закрытия здесь конец канала не дойдёт до читателя
        writer = subprocess.Popen([sys.executable, str(WRITER), str(pub_end.fileno())],
                                  **channel.spawn_kwargs(pub_end))

    with channel.Subscriber(sub_end) as sub, selectors.DefaultSelector() as sel:
        sel.register(sub, selectors.EVENT_READ)
        while not sub.closed:  # закрывается сам, когда писатель вышел
            sel.select(timeout=1)
            if not sub.receive():  # без ожидания: True — пришёл новый снимок
                continue
            root = sub.latest  # FrozenDict поверх общей памяти, без копирования
            print(f"tick={root['tick']} cpu[101]={root['vms'][101]['cpu']}")
            if root["tick"] >= TICKS:
                break
    writer.wait(timeout=10)

    print(f"последний: tick={sub.latest['tick']}")  # `latest` переживает закрытие канала


if __name__ == "__main__":
    main()
