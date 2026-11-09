"""Один набор данных на весь пул: создатель кладёт его в общую память, воркеры подключаются по имени."""
import multiprocessing

import shmobj


def init(name):
    global users
    users = shmobj.attach(name).root["users"]  # прокси держит сегмент открытым, пока жив воркер


def score(i):
    return users[i]["score"]


if __name__ == "__main__":
    data = {"users": [{"id": i, "score": i % 100} for i in range(100_000)]}
    # unlink_on_close: выход из with удаляет сегмент из ОС, без него — только закрывает
    with shmobj.create(data, unlink_on_close=True) as segment:
        with multiprocessing.Pool(4, initializer=init, initargs=(segment.name,)) as pool:
            print(sum(pool.map(score, range(len(data["users"])), chunksize=10_000)))
