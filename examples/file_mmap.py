"""Набор в файле: собирается один раз, воркеры отображают файл в память и читают страницы из кеша ОС.

Файл переживает перезапуск, его можно выкладывать вместе с приложением.
"""
import mmap
import multiprocessing
import os
import sys
import tempfile

import shmobj


def init(path):
    global users
    with open(path, "rb") as file:
        mapped = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)  # отображение переживает закрытие файла
    users = shmobj.loads(mapped)["users"]  # прокси поверх mmap, без копии
    sys.stdout.write(f"воркер {os.getpid()}: файл отображён в память\n")  # одной записью: строки не смешаются


def score(i):
    return users[i]["score"]


if __name__ == "__main__":
    data = {"users": [{"id": i, "score": i % 100} for i in range(100_000)]}
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "users.shmobj")
        with open(path, "wb") as file:
            file.write(shmobj.dumps(data))
        print(f"создатель: {path}, {os.path.getsize(path)} байт")
        with multiprocessing.Pool(4, initializer=init, initargs=(path,)) as pool:
            print(f"сумма score: {sum(pool.map(score, range(len(data['users'])), chunksize=10_000))}")
