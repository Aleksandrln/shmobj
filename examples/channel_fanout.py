"""Один писатель и несколько читателей (как воркеры Gunicorn/Uvicorn): снимок собирается один раз на всех.

Читатели — дочерние процессы этого же скрипта с `--reader <адрес>`. Подключившийся читатель сразу
получает последний снимок и выходит на снимке TICKS; писатель выходит вслед за всеми читателями.
"""
import os
import selectors
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
    with channel.Subscriber(channel.connect(address)) as sub, selectors.DefaultSelector() as sel:
        # Subscriber отдаёт fileno, поэтому его можно ждать селектором: сокет читаем — пришёл снимок или конец канала.
        sel.register(sub, selectors.EVENT_READ)
        # closed — писатель закрыл канал. receive() разбирает очередь без ожидания: True — sub.latest
        # сменился на свежий снимок (корень, FrozenDict поверх общей памяти, без копирования).
        while not sub.closed and not (sub.receive() and sub.latest["tick"] >= TICKS):
            sel.select(timeout=1)
        sys.stdout.write(f"читатель {os.getpid()}: tick={sub.latest['tick']}\n")  # одной записью: строки не смешаются


def _create_process(mode: str, address):
    return subprocess.Popen([sys.executable, __file__, mode, address])


def writer() -> None:
    # listen — слушающий сокет: путь AF_UNIX (файла ещё не должно быть) или TCP на loopback, порт 0 — любой свободный.
    # Publisher() без сокета — писатель без подписчиков; на выходе закроет сокеты подписчиков и отпустит снимки.
    with tempfile.TemporaryDirectory() as tmp, \
            channel.listen(("127.0.0.1", 0) if WINDOWS else os.path.join(tmp, "snapshots.sock")) as listener, \
            channel.Publisher() as pub:
        # не ждем. нет новых подключений raise BlockingIOError
        listener.setblocking(False)
        address = listener.getsockname()  # путь сокета или (host, port) с выданным портом
        readers = [
            _create_process(mode="--reader", address=str(address[1]) if WINDOWS else address) for _ in
            range(READERS)
        ]
        tick = 0
        while any(r.poll() is None for r in readers):
            while True:  # все, кто подключился с прошлого снимка
                try:
                    # add — привязать соеденение. В соеденение будет сразу же отправлено имя последнего снимка.
                    pub.add(listener.accept()[0])
                except BlockingIOError:
                    break
            try:
                # Снимок собирается один раз (Linux — запечатанный memfd, Windows — SharedMemory) и уходит всем
                # подписчикам без ожидания. False — у кого-то полна очередь, ему этот снимок пропущен.
                # Ушедшие подписчики убираются сами.
                pub.publish({"tick": tick, "vms": {vm: {"cpu": tick % 100} for vm in range(100, 110)}})
            except BrokenPipeError:
                pass  # ушёл последний подписчик; снимок всё равно стал последним для следующих add
            tick += 1
            time.sleep(0.05)
    print(f"писатель: последний tick={tick - 1}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--reader"]:
        reader(("127.0.0.1", int(sys.argv[2])) if WINDOWS else sys.argv[2])
    else:
        writer()
