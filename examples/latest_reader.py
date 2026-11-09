"""Читатель `shmobj.latest`: опрашивает имя и печатает каждый новый снимок.

Писатель — `latest_writer.py`; его может ещё не быть, перезапуск писателя чтению не мешает. Остановка — Ctrl+C.
"""
import time

from shmobj import latest

NAME = "shmobj-example"
POLL = 0.2  # секунд между опросами; проверка — чтение 40 байт


def main():
    reader = latest.Reader(NAME)
    seen = None
    while True:
        root = reader.get()  # None — снимков ещё не было; тот же объект, пока снимок не сменился
        if root is not None and root is not seen:
            seen = root
            print(f"tick={root['tick']} cpu[101]={root['vms'][101]['cpu']}")
        time.sleep(POLL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
