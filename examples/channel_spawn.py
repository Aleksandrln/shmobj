"""Канал снимков без asyncio: читатель ждёт снимки блокирующим `wait()`.

Писатель — дочерний процесс `channel_spawn_writer.py`. Читатель берёт только последний снимок, промежуточные
пропускаются. Получив снимок TICKS, читатель закрывает канал, и писатель выходит.
"""
import subprocess
import sys
from pathlib import Path

from shmobj import channel

WRITER = Path(__file__).with_name("channel_spawn_writer.py")
TICKS = 20


def main() -> None:
    pub_end, sub_end = channel.socketpair()
    with pub_end:  # копия у дочернего процесса; без закрытия здесь конец канала не дойдёт до читателя
        writer = subprocess.Popen([sys.executable, str(WRITER), str(pub_end.fileno())],
                                  **channel.spawn_kwargs(pub_end))

    with channel.Subscriber(sub_end) as sub:
        # True — пришёл новый снимок; False — писатель вышел, канал закрылся сам.
        # Ждать несколько сокетов разом — свой selectors по sub.fileno(), затем sub.receive() без ожидания.
        while sub.wait():
            root = sub.latest  # FrozenDict поверх общей памяти, без копирования
            print(f"tick={root['tick']} cpu[101]={root['vms'][101]['cpu']}")
            if root["tick"] >= TICKS:
                break
    writer.wait(timeout=10)

    print(f"последний: tick={sub.latest['tick']}")  # `latest` переживает закрытие канала


if __name__ == "__main__":
    main()
