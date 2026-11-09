"""Писатель канала по постоянному адресу, без asyncio: снимок раз в секунду, подписчиков принимает фоновый поток.

Читатели — `channel_reader.py` или `channel_reader_async.py`, сколько угодно, в любом порядке запуска.
Остановка — Ctrl+C.
"""
import itertools
import sys
import time

from shmobj import channel

# Linux — абстрактный AF_UNIX: файла нет, имя освобождается вместе с писателем. Windows — TCP на loopback.
ADDRESS = ("127.0.0.1", 50555) if sys.platform == "win32" else "\0shmobj-example"
INTERVAL = 1.0


def snapshot(tick):
    return {"tick": tick, "vms": {vm: {"cpu": (vm * tick) % 100 / 100} for vm in range(100, 110)}}


def main():
    with channel.Publisher() as pub:
        pub.accept_in_thread(channel.listen(ADDRESS))  # новый читатель сразу получает последний снимок
        for tick in itertools.count():
            # Снимок собирается один раз на всех. Без читателей только запоминается для следующего;
            # False — кто-то отстал, ему этот снимок пропущен.
            pub.publish(snapshot(tick))
            time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
