"""Один набор данных на весь пул: создатель кладёт его в общую память, воркеры подключаются по имени.

Воркерам сегмент нужен только на время подключения: подключившийся процесс читает его и после `unlink()`
создателя, до своего `close()`.
"""
import multiprocessing
import os
import sys

import shmobj


def init(name):
    global users
    users = shmobj.attach(name).root["users"]  # прокси держит сегмент открытым, пока жив воркер
    sys.stdout.write(f"воркер {os.getpid()}: подключён к {name}\n")  # одной записью: строки не смешаются


def score(i):
    return users[i]["score"]


if __name__ == "__main__":
    data = {"users": [{"id": i, "score": i % 100} for i in range(100_000)]}
    # unlink_on_close: выход из with удаляет сегмент из ОС, без него — только закрывает
    with shmobj.create(data, unlink_on_close=True) as segment:
        print(f"создатель: сегмент {segment.name}, {segment.size} байт")
        with multiprocessing.Pool(4, initializer=init, initargs=(segment.name,)) as pool:
            total = sum(pool.map(score, range(len(data["users"])), chunksize=10_000))
        print(f"сумма score: {total}")
    print("создатель: сегмент закрыт и удалён")
