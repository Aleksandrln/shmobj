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
    with channel.Subscriber(channel.connect(address)) as sub, selectors.DefaultSelector() as sel:
        sel.register(sub, selectors.EVENT_READ)
        while not sub.closed and not (sub.receive() and sub.latest["tick"] >= TICKS):
            sel.select(timeout=1)
        sys.stdout.write(f"читатель {os.getpid()}: tick={sub.latest['tick']}\n")  # одной записью: строки не смешаются


def writer() -> None:
    with tempfile.TemporaryDirectory() as tmp, \
            channel.listen(("127.0.0.1", 0) if WINDOWS else os.path.join(tmp, "snapshots.sock")) as listener, \
            channel.Publisher() as pub:
        listener.setblocking(False)
        address = listener.getsockname()
        arg = str(address[1]) if WINDOWS else address
        readers = [subprocess.Popen([sys.executable, __file__, "--reader", arg]) for _ in range(READERS)]
        tick = 0
        while any(r.poll() is None for r in readers):
            while True:  # все, кто подключился с прошлого снимка
                try:
                    pub.add(listener.accept()[0])
                except BlockingIOError:
                    break
            try:
                pub.publish({"tick": tick, "vms": {vm: {"cpu": tick % 100} for vm in range(100, 110)}})
            except BrokenPipeError:
                pass  # подписчиков не осталось
            tick += 1
            time.sleep(0.05)
    print(f"писатель: последний tick={tick - 1}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--reader"]:
        reader(("127.0.0.1", int(sys.argv[2])) if WINDOWS else sys.argv[2])
    else:
        writer()
