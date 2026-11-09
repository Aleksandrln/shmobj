"""`shmobj.latest`: последний снимок по имени, без сокетов.

`python examples/latest_pubsub.py writer` — писатель: раз в секунду кладёт снимок под именем, прежний удаляется.
`python examples/latest_pubsub.py reader` — читатель: опрашивает имя и печатает каждый новый снимок.

Процессы запускаются в любом порядке, перезапуск писателя чтению не мешает. Ctrl+C и выход писателя удаляют его
имена; после падения (`kill -9`) имена на Linux и macOS остаются до следующего писателя с тем же именем.
"""
import itertools
import sys
import time

from shmobj import latest

NAME = "shmobj-example"
INTERVAL = 1.0
POLL = 0.2  # секунд между опросами читателя; проверка — чтение 40 байт


def snapshot(tick):
    return {"tick": tick, "vms": {vm: {"cpu": (vm * tick) % 100 / 100} for vm in range(100, 110)}}


def writer():
    with latest.Writer(NAME) as w:
        for tick in itertools.count():
            w.put(snapshot(tick))
            print(f"писатель: tick={tick} под именем {NAME!r}")
            time.sleep(INTERVAL)


def reader():
    r = latest.Reader(NAME)
    seen = None
    while True:
        root = r.get()  # None — снимков ещё не было; тот же объект, пока снимок не сменился
        if root is not None and root is not seen:
            seen = root
            print(f"tick={root['tick']} cpu[101]={root['vms'][101]['cpu']}")
        time.sleep(POLL)


if __name__ == "__main__":
    role = {"writer": writer, "reader": reader}.get(sys.argv[1] if len(sys.argv) == 2 else None)
    if role is None:
        sys.exit("запуск: python examples/latest_pubsub.py writer|reader")
    try:
        role()
    except KeyboardInterrupt:
        pass
