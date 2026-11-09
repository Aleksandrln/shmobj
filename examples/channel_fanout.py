"""Один писатель и несколько читателей (как воркеры Gunicorn/Uvicorn): снимок собирается один раз на всех.

Читатели — дочерние процессы этого же скрипта с `--reader <адрес>`. Подключившийся читатель сразу
получает последний снимок и выходит на снимке TICKS; писатель выходит вслед за всеми читателями.
"""
import os
import subprocess
import sys
import tempfile
import time

from shmobj import channel

READERS = 3
TICKS = 20
WINDOWS = sys.platform == "win32"  # Windows — TCP на loopback, Linux — файл сокета AF_UNIX


def reader(address) -> None:
    # channel.connect — возвращает сокет, подключённый к address писателя; Subscriber забирает его себе и закроет на выходе.
    # Последний снимок писатель отправляет сразу при подключении.
    with channel.Subscriber(channel.connect(address)) as sub:
        # wait() спит, пока не придёт снимок: True — sub.latest сменился на свежий (корень, FrozenDict поверх
        # общей памяти, без копирования); False — писатель закрыл канал.
        while sub.wait():
            if sub.latest["tick"] >= TICKS:
                break
        sys.stdout.write(f"читатель {os.getpid()}: tick={sub.latest['tick']}\n")  # одной записью: строки не смешаются


def _create_process(mode: str, address):
    return subprocess.Popen([sys.executable, __file__, mode, address])


def writer() -> None:
    # Publisher() без сокета — писатель без подписчиков; на выходе остановит приём, закроет сокеты и отпустит снимки.
    with tempfile.TemporaryDirectory() as tmp, channel.Publisher() as pub:
        # listen — слушающий сокет: путь AF_UNIX (файла ещё не должно быть) или TCP на loopback, порт 0 — любой свободный.
        listener = channel.listen(("127.0.0.1", 0) if WINDOWS else os.path.join(tmp, "snapshots.sock"))
        # Publisher забирает listener и в своём потоке подписывает (add) каждое подключение:
        # новому читателю сразу уходит последний снимок. В asyncio — accept_in_loop, без потока.
        # Возвращает адрес для connect: путь сокета или (host, port) с выданным портом.
        address = pub.accept_in_thread(listener)
        print(f"писатель: слушаю {address!r}, запускаю читателей: {READERS}", flush=True)
        readers = [
            _create_process(mode="--reader", address=str(address[1]) if WINDOWS else address) for _ in
            range(READERS)
        ]
        tick = 0
        while any(r.poll() is None for r in readers):  # пока живы дочерние процессы-читатели
            # Снимок собирается один раз (Linux — запечатанный memfd, Windows — SharedMemory) и уходит всем
            # подписчикам без ожидания. False — у кого-то полна очередь, ему этот снимок пропущен.
            # Ушедшие подписчики убираются сами; при приёме подключений уход последнего — не ошибка.
            pub.publish({"tick": tick, "vms": {vm: {"cpu": tick % 100} for vm in range(100, 110)}})
            tick += 1
            time.sleep(0.05)
    print(f"писатель: последний tick={tick - 1}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--reader"]:
        reader(("127.0.0.1", int(sys.argv[2])) if WINDOWS else sys.argv[2])
    else:
        writer()
