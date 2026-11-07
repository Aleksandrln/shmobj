"""Один писатель, несколько читателей (как воркеры Gunicorn/Uvicorn): снимок собирается один раз.

Писатель слушает сокет `channel.listen`, каждое принятое соединение подписывает `Publisher.add`;
подключившийся читатель сразу получает последний снимок. Читатели — дочерние процессы
`channel_fanout.py --reader <адрес>`: подключаются `channel.connect`, ждут снимок TICKS и выходят.
Писатель выходит, когда вышли все читатели.
"""
import os
import selectors
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from shmobj import channel

READERS = 3
TICKS = 20
INTERVAL = 0.05


def snapshot(tick: int) -> dict:
    return {"tick": tick, "vms": {vm: {"cpu": (vm * tick) % 100 / 100} for vm in range(100, 110)}}


def address(arg: str):
    """Адрес из командной строки: путь AF_UNIX (Linux) или порт на loopback (Windows)."""
    return ("127.0.0.1", int(arg)) if sys.platform == "win32" else arg


def reader(arg: str) -> None:
    with channel.Subscriber(channel.connect(address(arg))) as sub, selectors.DefaultSelector() as sel:
        sel.register(sub, selectors.EVENT_READ)
        while not sub.closed:
            sel.select(timeout=1)
            if sub.receive() and sub.latest["tick"] >= TICKS:
                break
        print(f"читатель {os.getpid()}: tick={sub.latest['tick']}")


def accept_all(listener, pub: channel.Publisher) -> None:
    while True:
        try:
            conn, _ = listener.accept()
        except BlockingIOError:
            return
        pub.add(conn)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bind = ("127.0.0.1", 0) if sys.platform == "win32" else str(Path(tmp) / "snapshots.sock")
        with channel.listen(bind) as listener, channel.Publisher() as pub:
            listener.setblocking(False)
            arg = str(listener.getsockname()[1]) if sys.platform == "win32" else bind
            readers = [subprocess.Popen([sys.executable, __file__, "--reader", arg]) for _ in range(READERS)]
            tick = 0
            while any(r.poll() is None for r in readers):
                accept_all(listener, pub)
                try:
                    pub.publish(snapshot(tick))  # False — кто-то отстал, ему снимок пропущен
                except BrokenPipeError:
                    pass  # ушёл последний подписчик; новые подключатся через listener
                tick += 1
                time.sleep(INTERVAL)
    print(f"писатель: последний tick={tick - 1}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--reader"]:
        reader(sys.argv[2])
    else:
        main()
