"""Писатель `shmobj.latest`: раз в секунду кладёт снимок под именем, прежний удаляется.

Читатель — `latest_reader.py`, процессы запускаются в любом порядке. Ctrl+C и выход удаляют имена писателя;
после падения (`kill -9`) имена на Linux и macOS остаются до следующего писателя с тем же именем.
"""
import itertools
import time

from shmobj import latest

NAME = "shmobj-example"
INTERVAL = 1.0


def snapshot(tick):
    return {"tick": tick, "vms": {vm: {"cpu": (vm * tick) % 100 / 100} for vm in range(100, 110)}}


def main():
    with latest.Writer(NAME) as writer:
        for tick in itertools.count():
            writer.put(snapshot(tick))
            time.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
