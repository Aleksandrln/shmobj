"""Один набор данных на весь пул: создатель кладёт его в общую память, воркеры подключаются по имени."""
import functools
import multiprocessing

import shmobj


@functools.cache
def users(name):
    return shmobj.attach(name).root["users"]  # одно подключение на воркер; прокси держит сегмент открытым


def score(i, name):
    return users(name)[i]["score"]


if __name__ == "__main__":
    data = {"users": [{"id": i, "score": i % 100} for i in range(100_000)]}
    # unlink_on_close: выход из with удаляет сегмент из ОС, без него — только закрывает
    with shmobj.create(data, unlink_on_close=True) as segment:
        with multiprocessing.Pool(4) as pool:
            tasks = ((i, segment.name) for i in range(len(data["users"])))
            print(sum(pool.starmap(score, tasks, chunksize=10_000)))
