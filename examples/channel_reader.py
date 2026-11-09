"""Читатель канала без asyncio: ждёт писателя, печатает каждый снимок, после ухода писателя подключается заново.

Писатель — `channel_writer.py` или `channel_writer_async.py`. Остановка — Ctrl+C.
"""
import sys

from shmobj import channel

ADDRESS = ("127.0.0.1", 50555) if sys.platform == "win32" else "\0shmobj-example"


def main():
    while True:
        # Писатель ещё не запущен или перезапускается — ждать его. Ошибка в адресе тоже выглядит как ожидание:
        # wait_writer=30 — не дольше 30 секунд, затем ConnectionRefusedError.
        with channel.Subscriber(channel.connect(ADDRESS, wait_writer=True)) as sub:
            # wait() спит, пока не придёт снимок: True — sub.latest сменился, False — писатель закрылся или упал.
            while sub.wait():
                root = sub.latest  # FrozenDict поверх общей памяти, без копирования
                print(f"tick={root['tick']} cpu[101]={root['vms'][101]['cpu']}")
        print("писатель ушёл, жду нового")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
