# shmobj

**English** | [Русский](https://github.com/Aleksandrln/shmobj/blob/master/README.md)

[![PyPI](https://img.shields.io/pypi/v/shmobj)](https://pypi.org/project/shmobj/)
[![Python](https://img.shields.io/pypi/pyversions/shmobj)](https://pypi.org/project/shmobj/)

Arbitrarily nested `dict`/`list`-like structures in shared memory (via `mmap`), accessible from multiple processes without
copying or deserialization.

`shmobj` provides the `FrozenDict` (`Mapping`) and `FrozenList` (`Sequence`) proxy objects for accessing data in shared
memory. The data is stored in memory only once. All processes access the same shared structure, and Python objects are
created only for the values that are actually accessed.

`shmobj` also provides mechanisms for sharing up-to-date data snapshots between processes:
[`shmobj.latest`](#shmobjlatest) returns the latest snapshot published under a given name, and
[`shmobj.channel`](#pubsub-shmobjchannel) delivers new snapshots to subscribers (pub/sub).

## Why shmobj?

CPU-bound Python workloads often use multiple processes to run computations in parallel and work around the GIL. Each process
has its own memory space, so data loaded into each process is duplicated. For example, an 8-process workload using a 100 MB lookup table can consume around 800 MB.

`shmobj` solves this by storing the data once in shared memory. All processes access the same structure without copying or deserializing it.

But sharing data is only part of the problem: other processes also need to be notified when that data changes. Unlike
threads, processes do not share the same address space, so they cannot directly see changes in each other's memory.
Updates are typically sent through mechanisms such as `multiprocessing.Queue`, where data is serialized by the sending
process, transferred, and deserialized into a separate copy in the receiving process.

`shmobj` handles this too: the writer publishes a new snapshot once, and readers access it without copying — by name via
[`shmobj.latest`](#shmobjlatest) or by subscription via [`shmobj.channel`](#pubsub-shmobjchannel).

The measurements below refer to the shared dataset.

### Memory usage

Dataset: one million records with the structure `{"id", "name", "score", "tags": [...], "ok", "x"}`. Each process iterated over the entire dataset.

| Processes | `pickle` — total memory | `shmobj` — total memory |
|----------:|------------------------:|------------------------:|
|         1 |                  628 MB |                  168 MB |
|         4 |                  2.5 GB |                  168 MB |
|         8 |                  5.0 GB |                  168 MB |

### Comparison with standard tools

This benchmark compares both access speed and memory usage. Four reader processes ran concurrently. Each attached to the dataset, performed 100,000 random `d[k]["name"]` lookups, and then iterated over all records.

**Environment:** Linux x86_64, 4 cores, Python 3.13.  
Script: [bench/compare.py](https://github.com/Aleksandrln/shmobj/blob/master/bench/compare.py).

| Approach | Reader attach | `d[k]["name"]` | Full iteration | Total memory |
|---|--:|--:|--:|--:|
| `shmobj` | ~50 µs | 1.3 µs | `iter_values()` 0.26 s, `values()` 0.74 s | 210 MB |
| `pickle` + `multiprocessing.shared_memory` | 2.3 s | 1.0 µs | 0.07 s | 2.6 GB |
| `multiprocessing.Manager().dict()` | 1 ms | 290 µs | ≈ 5 min ¹ | 690 MB |
| `fork` + `gc.freeze()` | — ² | 3.3 µs ³ | 0.3–0.4 s | 1.9 GB |

Total memory includes the storage itself (the shared-memory segment, `pickle` buffer, `Manager` server process, or parent process in the `fork` approach) as well as the readers' own memory, including approximately 10 MB per Python interpreter.

**Notes:**

- ¹ Estimated from 5,000 lookups. Each `d[k]` requires an IPC request to the server process, followed by deserialization of the reply.
- ² The parent process loads the data before `fork`. Without a common parent process, this approach cannot share the dataset.
- ³ Initial accesses trigger copy-on-write page duplication: about 330 MB per reader.

The trade-off for storing a single shared copy is slower access: a key lookup is 3–6 times slower than in a regular `dict`, while a full iteration is about 4 times slower with `iter_values()` and 11 times slower with `values()`.

For workloads with frequent access, use the faster methods: `get_many()`, `iter_values()`, `iter_items()`, and `materialize()`.

More: [Access speed](https://github.com/Aleksandrln/shmobj/blob/master/doc/usage.md#скорость-доступа),
[comparison with alternatives](https://github.com/Aleksandrln/shmobj/blob/master/doc/comparison.md),
[measurements](https://github.com/Aleksandrln/shmobj/blob/master/doc/status.md#замеры).

The detailed documentation in `doc/` is currently available in Russian.

## Installation

```bash
pip install shmobj
```

Prebuilt wheels are available for:

- **Python:** CPython 3.11 and later;
- **Linux:** x86_64, aarch64 — glibc (manylinux) and musl (musllinux, Alpine);
- **Windows:** x64;
- **macOS:** arm64, x86_64 — partial support ([limitations](#limitations)).

On other platforms, pip builds the package from the source distribution (sdist), which requires Rust ([building from source](#building-from-source)).

The same wheels are available on the [releases page](https://github.com/Aleksandrln/shmobj/releases):

```bash
pip install ./shmobj-<version>-cp311-abi3-<platform>.whl
```

## Main use cases

| Use case                                         | API                                            | Example                                                                                           |
|--------------------------------------------------|------------------------------------------------|---------------------------------------------------------------------------------------------------|
| Share one dataset across multiple processes      | [`create()` / `attach()`](#shared-dataset)     | [shared_pool.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/shared_pool.py)       |
| Let readers access the latest published snapshot | [`shmobj.latest`](#shmobjlatest)               | [latest_pubsub.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/latest_pubsub.py)   |
| Receive new snapshots without polling            | [`shmobj.channel`](#pubsub-shmobjchannel)      | [channel_pubsub.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/channel_pubsub.py) |
| Read data from a file or `bytes`                 | [`dumps()` / `loads()`](#files-bytes-and-mmap) | [file_mmap.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/file_mmap.py)           |

All examples are in the [examples](https://github.com/Aleksandrln/shmobj/blob/master/examples/README.md) directory.
Install [`shmobj`](#installation) before running them.

### Shared dataset

`create()` creates a segment and writes the data structure to it. Other processes attach to the segment by name using
`attach()`. The memory is released after `unlink()` has been called and all processes have closed the segment (on Windows,
after the last handle is closed).

```python
import shmobj

# Writer process

segment = shmobj.create({
    "users": [
        {"id": 1, "name": "Ann", "score": 42},
        {"id": 2, "name": "Bob", "score": 37},
    ]
})

print(segment.name)  # pass the name to the reader processes

# Reader process
with shmobj.attach(segment.name) as segment:
    users = segment.root["users"]

    print(users[0].get_many(["id", "name"]))
    total = sum(user["score"] for user in users.iter_values())
```

`FrozenDict` and `FrozenList` implement the `Mapping` and `Sequence` interfaces, so you can use them much like regular
`dict` and `list` objects.

It also works with `multiprocessing` ([shared_pool.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/shared_pool.py)):

```python
import multiprocessing
import shmobj


def init(segment_name):
    global users
    users = shmobj.attach(segment_name).root["users"]  # The proxy keeps the segment open for the lifetime of the worker.


def score(index):
    return users[index]["score"]


if __name__ == "__main__":
    data = {"users": [{"id": i, "score": i % 100} for i in range(100_000)]}
    # Exiting the with block closes the segment and unlinks it from the OS.
    with shmobj.create(data, unlink_on_close=True) as segment:
        with multiprocessing.Pool(4, initializer=init, initargs=(segment.name,)) as pool:
            print(sum(pool.map(score, range(len(data["users"])), chunksize=10_000)))
```

### `shmobj.latest`

`shmobj.latest` lets you publish snapshots under a given name. The writer publishes a new snapshot in place of the
previous one, and readers get the current version by calling `get()`. No sockets are involved, and processes can start
in any order.

```python
from shmobj import latest

# Writer process
writer = latest.Writer("metrics")
writer.put({"vms": {101: {"cpu": 0.5}}})

# Reader process
reader = latest.Reader("metrics")
root = reader.get()  # None if no snapshot has been published yet; otherwise, the same object until it changes
```

As long as the snapshot remains unchanged, `get()` returns the same object. A reader only detects an update when it calls
`get()` again.

Example: [latest_pubsub.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/latest_pubsub.py).

**Cleanup after a crash.** On Linux and macOS, segment names may remain registered with the OS if the writer crashes.
On Linux, these segments are located in `/dev/shm`. They remain there until a reboot, a container restart, or the next
writer that uses the same name.

### Pub/Sub: `shmobj.channel`

One writer, multiple readers. Readers are notified of new snapshots without polling.

- A new subscriber immediately receives the latest snapshot.
- Intermediate snapshots may be skipped: readers get the current state, not a queue of every update.
- After all processes exit, the OS releases the channel resources; no names are left behind in `/dev/shm` either.
- The channel address remains stable, so readers can reconnect after the writer restarts.

On Linux, an address such as `"\0name"` uses the abstract Unix socket namespace. It is not a file, and the kernel releases
the address when the writer exits. Windows uses TCP loopback instead.

#### Synchronous API

```python
import itertools
import sys
import time

from shmobj import channel

ADDRESS = ("127.0.0.1", 50555) if sys.platform == "win32" else "\0shmobj-example"

# Writer
with channel.Publisher() as pub:
    pub.accept_in_thread(channel.listen(ADDRESS))  # a background thread accepts subscribers
    for tick in itertools.count():
        pub.publish({"tick": tick, "vms": {101: {"cpu": tick % 100}}})  # non-blocking
        time.sleep(1)

# Reader:
while True:
    # wait until the writer starts listening; wait_writer=30 — at most 30 seconds
    with channel.Subscriber(channel.connect(ADDRESS, wait_writer=True)) as sub:
        while sub.wait():  # True — a snapshot arrived; False — the writer closed or crashed
            print(sub.latest["vms"][101]["cpu"])  # FrozenDict backed by shared memory
```

Example: [channel_pubsub.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/channel_pubsub.py).

#### asyncio API

The channel supports asynchronous connections and waiting for updates. Waiting for the writer does not block the event
loop.

```python
import asyncio
import itertools
import sys

from shmobj import channel

ADDRESS = ("127.0.0.1", 50555) if sys.platform == "win32" else "\0shmobj-example"


# Writer
async def main():
    with channel.Publisher() as pub:
        pub.accept_in_loop(channel.listen(ADDRESS))  # the event loop accepts subscribers, without a separate thread
        for tick in itertools.count():
            pub.publish({"tick": tick, "vms": {101: {"cpu": tick % 100}}})
            await asyncio.sleep(1)


# Reader:
async def main():
    while True:
        sock = await channel.connect_async(ADDRESS, wait_writer=True)  # waiting does not block the event loop
        with channel.Subscriber(sock) as sub:
            while await sub.wait_async():
                print(sub.latest["vms"][101]["cpu"])


# Reader with a callback: called in the event loop for each new snapshot
def on_snapshot(root):
    print(root["vms"][101]["cpu"])


async def main():
    sock = await channel.connect_async(ADDRESS, wait_writer=True)
    with channel.Subscriber(sock) as sub:
        sub.attach(on_snapshot)  # calls on_snapshot(sub.latest) for each new snapshot
        while await sub.wait_async():  # also works with a callback; False — the writer closed or crashed
            pass
```

Example: [channel_pubsub_async.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/channel_pubsub_async.py).

Synchronous and asynchronous writers and readers are compatible. For example, an asyncio reader can connect to a
synchronous writer.

### Files, `bytes` and `mmap`

`dumps()` creates a buffer in the `shmobj` format, and `loads()` opens the structure directly from a supported buffer
(`bytes`, `mmap`, or another supported type) without copying the underlying data. When multiple processes read the same file, the OS
serves its pages from the shared file cache.

```python
import mmap
import shmobj

data = {"users": [{"id": 1, "name": "Ann", "score": 42}]}

# Save the structure to a file.
with open("data.shmobj", "wb") as file:
    file.write(shmobj.dumps(data))

with open("data.shmobj", "rb") as file:  # in each process, file pages come from the shared OS cache
    root = shmobj.loads(mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ))
```

## Segment lifecycle

- `close()` closes the segment in the current process.
- `unlink()` removes the segment's name from the OS. Exiting a `with` block calls `close()`. Automatic `unlink()` is
  performed only for segments created with `unlink_on_close=True`.
- Proxy objects keep the segment open. Accessing them after `close()` raises `SegmentClosedError`.
- **Linux and macOS:** a segment is a POSIX shared memory object. On Linux, it is represented by a file in `/dev/shm`
  and stored in RAM. The segment exists until `unlink()` is called, not merely until its creator exits.
- **Windows:** the segment is removed after the last handle is closed.

If the entire process group crashes on Linux or macOS, the segment name remains registered with the OS until a reboot
(or a container restart), or until you run `rm /dev/shm/<name>` on Linux.

More: [Segment lifecycle](https://github.com/Aleksandrln/shmobj/blob/master/doc/usage.md#жизненный-цикл-сегмента).

## Limitations

- **Read-only access.** Published data cannot be modified in place. To update it, publish a new snapshot via
  [`shmobj.latest`](#shmobjlatest) or [`shmobj.channel`](#pubsub-shmobjchannel).
- **Supported values:** `dict`, `list`, `tuple` (represented as a list when read), `None`, `bool`, arbitrary-precision
  `int`, `float`, `str` and `bytes`. Dictionary keys must be scalar values.
- **macOS — partial support:** `shmobj.channel` is not available (use `shmobj.latest` instead). The package passes the
  test suite but has not been used in production projects.
- **Format validation:** accessing corrupted data raises `FormatError`. `validate()` checks the entire buffer.
- **Docker:** by default, each container has its own `/dev/shm` (64 MB) and its own network. You can work around this
  by configuring shared IPC and `shm_size` in Compose, or by using a channel over a socket file in a shared volume —
  [Docker](https://github.com/Aleksandrln/shmobj/blob/master/doc/usage.md#docker).

## Why not use Python's standard tools?

- **`multiprocessing.shared_memory`.** It provides a shared byte buffer, but has no built-in support for nested Python
  structures. You must serialize the structure (for example, with `pickle`), and each process then deserializes it into
  its own copy.
- **`multiprocessing.Manager().dict()`.** The data is managed by a separate server process. Accessing a dictionary item
  requires an IPC request and serialization.
- **`fork`.** By itself, it does not guarantee memory savings for Python objects: reference-count updates can trigger
  copy-on-write, causing memory pages to be copied between processes.

More: [Comparison with alternatives](https://github.com/Aleksandrln/shmobj/blob/master/doc/comparison.md).

## Documentation

- [Guide](https://github.com/Aleksandrln/shmobj/blob/master/doc/usage.md) — segments, files, data validation, `channel`,
  `latest`, Docker and the debugger.
- [API Reference](https://github.com/Aleksandrln/shmobj/blob/master/doc/api.md) — the public API.
- [Build](https://github.com/Aleksandrln/shmobj/blob/master/doc/build.md) — wheel layout and portable builds.
- [Data format](https://github.com/Aleksandrln/shmobj/blob/master/doc/format.md) and [architecture](https://github.com/Aleksandrln/shmobj/blob/master/doc/architecture.md) — the binary format and internal design.
- [Development](https://github.com/Aleksandrln/shmobj/blob/master/doc/dev.md) — development environment, checks and repository layout.
- [Measurements and project status](https://github.com/Aleksandrln/shmobj/blob/master/doc/status.md).

## Building from source

### Quick install from Git

Install directly from the repository (`pip` downloads `maturin` and builds the wheel; Rust is required):

```bash
pip install git+https://github.com/Aleksandrln/shmobj
```

### Full build from a project checkout

Requires CPython 3.11+ and stable Rust ([rustup](https://rustup.rs)). On Windows, you also need Visual Studio Build Tools
with the C++ component. Install Rust on Linux and macOS with:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

To build a wheel from a local checkout, install `maturin` in a virtual environment (Debian and Ubuntu block `pip install`
outside a virtual environment under PEP 668):

```bash
git clone https://github.com/Aleksandrln/shmobj
cd shmobj
python3 -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install maturin
maturin build --release -o dist   # dist/shmobj-<version>-cp311-abi3-<platform>.whl
maturin sdist -o dist             # source distribution
```

The wheel can be installed in any CPython 3.11+ environment on the same platform: `pip install dist/shmobj-*.whl`.
Without `--release`, the build is about 10 times slower. `cargo build --release` builds only the library, not the
Python package.

Wheel layout and portable builds (older glibc, Windows builds from Linux): [doc/build.md](https://github.com/Aleksandrln/shmobj/blob/master/doc/build.md).

## License

Dual-licensed under [MIT](https://github.com/Aleksandrln/shmobj/blob/master/LICENSE-MIT) or
[Apache-2.0](https://github.com/Aleksandrln/shmobj/blob/master/LICENSE-APACHE), at your option.

## Author

Created by [AleksandrLn](https://github.com/Aleksandrln) with the help of [Claude](https://claude.ai) by Anthropic.
