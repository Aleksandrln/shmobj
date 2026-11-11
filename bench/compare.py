"""Сравнение shmobj со стандартными средствами: время подключения, доступ, проход и память при N читателях.

    python bench/compare.py [--records 1000000] [--readers 4]

Только Linux (память — из /proc/<pid>/smaps). Память — сумма: хранилище (сегмент, буфер pickle, сервер Manager или
родитель fork) и приватные страницы читателей без общей памяти.
"""

import argparse
import gc
import multiprocessing as mp
import os
import pickle
import random
import time
from multiprocessing.shared_memory import SharedMemory

import shmobj

GET_KEYS = 100_000
MANAGER_KEYS = 5_000  # каждое обращение к Manager — запрос к серверу; проход оценивается по выборке
SHM_PATHS = ("/dev/shm/", "/memfd:", "/SYSV")


def make_data(n):
    return {
        f"user{i}": {
            "id": i,
            "name": f"name{i}",
            "score": i * 1.5,
            "tags": ["a", "bb", i],
            "ok": True,
            "x": None,
        }
        for i in range(n)
    }


def private_mb(pid, *, all_pages=False):
    """Приватные страницы процесса без отображений общей памяти; `all_pages` — весь RSS."""
    total = 0
    skip = False
    with open(f"/proc/{pid}/smaps") as f:
        for line in f:
            head = line.split(None, 1)[0]
            if not head.endswith(":"):
                parts = line.split(None, 5)
                skip = len(parts) == 6 and parts[5].startswith(SHM_PATHS)
            elif not skip and head in (
                ("Rss:",) if all_pages else ("Private_Clean:", "Private_Dirty:")
            ):
                total += int(line.split()[1])
    return total / 1024


def run_access(root, n, n_keys, scan):
    rnd = random.Random(os.getpid())
    keys = [f"user{rnd.randrange(n)}" for _ in range(n_keys)]
    t = time.perf_counter()
    for k in keys:
        root[k]["name"]
    get_us = (time.perf_counter() - t) / n_keys * 1e6
    scans = {}
    if scan:
        methods = (
            ("values", "iter_values") if hasattr(root, "iter_values") else ("values",)
        )
        for m in methods:
            t = time.perf_counter()
            s = 0.0
            for rec in getattr(root, m)():
                s += rec["score"]
            scans[m] = time.perf_counter() - t
    return get_us, scans


def reader(kind, arg, n, results, done):
    t = time.perf_counter()
    if kind == "shmobj":
        seg = shmobj.attach(arg)
        root = seg.root
    elif kind == "pickle":
        shm = SharedMemory(arg, track=False)
        root = pickle.loads(shm.buf)
    else:  # manager: arg — прокси dict
        root = arg
        len(root)
    open_s = time.perf_counter() - t
    if kind == "manager":
        get_us, _ = run_access(root, n, MANAGER_KEYS, scan=False)
        scans = {"оценка": get_us * n / 1e6}
    else:
        get_us, scans = run_access(root, n, GET_KEYS, scan=True)
    results.put((os.getpid(), open_s, get_us, scans))
    done.wait()


def fork_parent(n, readers, results, done):
    data = make_data(n)
    gc.collect()
    gc.freeze()
    ctx = mp.get_context("fork")
    children = []
    for _ in range(readers):
        p = ctx.Process(target=fork_child, args=(data, n, results, done))
        p.start()
        children.append(p)
    for p in children:
        p.join()


def fork_child(data, n, results, done):
    get_us, scans = run_access(data, n, GET_KEYS, scan=True)
    results.put((os.getpid(), 0.0, get_us, scans))
    done.wait()


def measure(name, start, n, readers, store_mb):
    ctx = mp.get_context("spawn")
    results, done = ctx.Queue(), ctx.Event()
    extra = start(ctx, results, done)
    rows = [results.get() for _ in range(readers)]
    mem = sum(private_mb(pid) for pid, *_ in rows) + store_mb(extra)
    done.set()
    for p in extra["procs"]:
        p.join()
    open_s = max(r[1] for r in rows)
    get_us = sum(r[2] for r in rows) / readers
    scan = ", ".join(f"{m}: {fmt_time(max(r[3][m] for r in rows))}" for m in rows[0][3])
    print(f"| {name} | {fmt_time(open_s)} | {get_us:.2f} мкс | {scan} | {mem:.0f} МБ |")


def fmt_time(s):
    if s == 0:
        return "—"
    return (
        f"{s * 1e6:.0f} мкс"
        if s < 1e-3
        else f"{s * 1e3:.0f} мс"
        if s < 1
        else f"{s:.2f} с"
    )


def spawn_readers(ctx, kind, arg, n, readers, results, done):
    procs = [
        ctx.Process(target=reader, args=(kind, arg, n, results, done))
        for _ in range(readers)
    ]
    for p in procs:
        p.start()
    return procs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", type=int, default=1_000_000)
    ap.add_argument("--readers", type=int, default=4)
    a = ap.parse_args()
    n, readers = a.records, a.readers

    data = make_data(n)
    print(f"{n} записей, {readers} читателя(ей), Python {os.sys.version.split()[0]}\n")
    print(
        '| Способ | Подключение | `d[k]["name"]` | Проход по всем записям | Память, сумма |'
    )
    print("|---|---|---|---|---|")

    seg = shmobj.create(data)
    measure(
        "shmobj",
        lambda ctx, r, d: {
            "procs": spawn_readers(ctx, "shmobj", seg.name, n, readers, r, d)
        },
        n,
        readers,
        lambda _: os.path.getsize(f"/dev/shm/{seg.name}") / 2**20,
    )
    seg.close()
    seg.unlink()

    blob = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
    blob_mb = len(blob) / 2**20
    shm = SharedMemory(create=True, size=len(blob))
    shm.buf[: len(blob)] = blob
    del blob
    measure(
        "pickle + `shared_memory`",
        lambda ctx, r, d: {
            "procs": spawn_readers(ctx, "pickle", shm.name, n, readers, r, d)
        },
        n,
        readers,
        lambda _: blob_mb,
    )
    shm.close()
    shm.unlink()

    with mp.get_context("spawn").Manager() as manager:
        proxy = manager.dict(data)
        server_pid = manager._process.pid
        measure(
            "`Manager().dict()`",
            lambda ctx, r, d: {
                "procs": spawn_readers(ctx, "manager", proxy, n, readers, r, d)
            },
            n,
            readers,
            lambda _: private_mb(server_pid),
        )

    del data
    gc.collect()

    def start_fork(ctx, results, done):
        p = ctx.Process(target=fork_parent, args=(n, readers, results, done))
        p.start()
        return {"procs": [p], "pid": p.pid}

    measure(
        "`fork` + `gc.freeze()`",
        start_fork,
        n,
        readers,
        lambda e: private_mb(e["pid"], all_pages=True),
    )


if __name__ == "__main__":
    main()
