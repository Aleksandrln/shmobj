"""Канал снимков по постоянному адресу, без asyncio.

`python examples/channel_pubsub.py writer` — писатель: снимок раз в секунду, подписчиков принимает фоновый поток.
`python examples/channel_pubsub.py reader` — читатель: ждёт писателя, печатает каждый снимок, после ухода писателя
подключается заново.

Читателей сколько угодно, порядок запуска любой; к этому писателю подключается и читатель из
`channel_pubsub_async.py`. Остановка — Ctrl+C.
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


def writer():
    with channel.Publisher() as pub:
        pub.accept_in_thread(channel.listen(ADDRESS))  # новый читатель сразу получает последний снимок
        print(f"писатель: слушаю {ADDRESS!r}")
        for tick in itertools.count():
            # Снимок собирается один раз на всех. Без читателей только запоминается для следующего;
            # False — кто-то отстал, ему этот снимок пропущен.
            delivered = pub.publish(snapshot(tick))
            print(f"писатель: tick={tick} подписчиков={pub.subscribers}{'' if delivered else ' (кто-то отстал)'}")
            time.sleep(INTERVAL)


def reader():
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
    role = {"writer": writer, "reader": reader}.get(sys.argv[1] if len(sys.argv) == 2 else None)
    if role is None:
        sys.exit("запуск: python examples/channel_pubsub.py writer|reader")
    try:
        role()
    except KeyboardInterrupt:
        pass
