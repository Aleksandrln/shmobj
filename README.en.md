# shmobj

**English** | [Русский](https://github.com/Aleksandrln/shmobj/blob/master/README.md)

`dict`/`list`-like structures of unlimited nesting depth in shared memory (via `mmap`), accessible from multiple
processes without copying or deserialization.

`shmobj` provides proxy objects `FrozenDict` (`Mapping`) and `FrozenList` (`Sequence`) for accessing data in shared
memory.
The data is placed in memory once. All processes work with the same shared structure, and Python objects are created
only for the values that are actually accessed.

## Why

CPU-bound Python workloads often run several processes to get around the GIL.
Each process then holds its own isolated copy of the data.
For example, a 100 MB lookup table, config or metrics set on 8 workers takes about 800 MB of memory. `shmobj` keeps
a single shared instance of such data.

Test data: one million records shaped like `{"id", "name", "score", "tags": [...], "ok", "x"}`.
Each process iterated over all records.

| Processes | `pickle` — total memory used | `shmobj` — total memory used |
|----------:|-----------------------------:|-----------------------------:|
|         1 |                       628 MB |                       168 MB |
|         4 |                       2.5 GB |                       168 MB |
|         8 |                       5.0 GB |                       168 MB |

More measurements: [doc/status.md](https://github.com/Aleksandrln/shmobj/blob/master/doc/status.md).

Attaching to an existing segment takes about 50 µs, while `pickle.loads` of the same data takes about 1.5 s.
The trade-off: key lookup is 3–6 times slower than in a plain `dict`. `get_many()`, `iter_values()`, `iter_items()`
and `materialize()` speed up sequential reads and frequent access.

More on performance and access patterns: [Access speed](https://github.com/Aleksandrln/shmobj/blob/master/doc/usage.md#скорость-доступа).

The detailed documentation in `doc/` is in Russian.

## Installation

```bash
pip install shmobj
```

Prebuilt wheels:

- **Python:** CPython 3.11 and newer;
- **Linux:** x86_64, aarch64;
- **Windows:** x64;
- **macOS:** arm64, x86_64 — partial support ([limitations](#limitations)).

On other platforms pip builds the package from the sdist, which requires Rust ([building from source](#building-from-source)).

The same wheels are available on the [releases page](https://github.com/Aleksandrln/shmobj/releases):

```bash
pip install ./shmobj-<version>-cp311-abi3-<platform>.whl
```

## Main use cases

| Use case                                       | API                     | Example                                                                                           |
|------------------------------------------------|-------------------------|---------------------------------------------------------------------------------------------------|
| One data set shared by several processes       | `create()` / `attach()` | [shared_pool.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/shared_pool.py)       |
| Readers need the latest published snapshot     | `shmobj.latest`         | [latest_pubsub.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/latest_pubsub.py)   |
| Subscribe to new snapshots without polling     | `shmobj.channel`        | [channel_pubsub.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/channel_pubsub.py) |
| Read data from a file or `bytes`               | `dumps()` / `loads()`   | [file_mmap.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/file_mmap.py)           |

All examples are in the [examples](https://github.com/Aleksandrln/shmobj/blob/master/examples/README.md) directory.  
Note: [install `shmobj`](#installation) before running the examples.

### Shared data set

`create()` creates a segment and writes the structure into it. Other processes attach to it by the segment name with
`attach()`.
The memory is freed after `unlink()` and once all processes have closed the segment (on Windows — after the last handle
is closed).

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

`FrozenDict` and `FrozenList` implement the `Mapping` and `Sequence` interfaces, so they work like the familiar `dict`
and `list`.

The same with `multiprocessing` ([shared_pool.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/shared_pool.py)):

```python
import multiprocessing
import shmobj


def init(segment_name):
    global users
    users = shmobj.attach(segment_name).root["users"]  # The proxy keeps the segment open while the worker lives.


def score(index):
    return users[index]["score"]


if __name__ == "__main__":
    data = {"users": [{"id": i, "score": i % 100} for i in range(100_000)]}
    # Leaving the with block closes the segment and removes it from the OS.
    with shmobj.create(data, unlink_on_close=True) as segment:
        with multiprocessing.Pool(4, initializer=init, initargs=(segment.name,)) as pool:
            print(sum(pool.map(score, range(len(data["users"])), chunksize=10_000)))
```

### `shmobj.latest`

`shmobj.latest` publishes snapshots under a given name.  
The writer replaces the previous snapshot with a new one, and the reader gets the current version when it calls `get()`.
No sockets are involved; processes can start in any order.

```python
from shmobj import latest

# Writer process
writer = latest.Writer("metrics")
writer.put({"vms": {101: {"cpu": 0.5}}})

# Reader process
reader = latest.Reader("metrics")
root = reader.get()  # None — no snapshots yet; the same object until the snapshot changes
```

While the snapshot is unchanged, `get()` returns the same object. The reader learns about an update only on its next
`get()` call.

Example: [latest_pubsub.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/latest_pubsub.py).

**Cleanup after a crash.** On Linux and macOS, segment names can remain in the OS after the writer crashes.
On Linux they live in `/dev/shm`. Such segments persist until a reboot, a container restart or the next writer using
the same name.

### Pub/Sub: `shmobj.channel`

One writer, many readers; readers learn about a new snapshot without polling.

- A new subscriber immediately receives the latest snapshot.
- Intermediate snapshots may be skipped: the reader gets the current state, not a queue of all updates.
- When the processes exit, the OS frees the channel resources (not even names in `/dev/shm` are left behind).
- The channel address stays the same, so readers can reconnect after the writer restarts.

On Linux an address like `"\0name"` uses the abstract Unix socket namespace: it is not a file, and the kernel frees the
address together with the writer. On Windows TCP loopback is used.

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
        while sub.wait():  # True — a snapshot arrived, False — the writer closed or crashed
            print(sub.latest["vms"][101]["cpu"])  # FrozenDict over shared memory
```

Example: [channel_pubsub.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/channel_pubsub.py)

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
        pub.accept_in_loop(channel.listen(ADDRESS))  # the event loop accepts subscribers, no thread
        for tick in itertools.count():
            pub.publish({"tick": tick, "vms": {101: {"cpu": tick % 100}}})
            await asyncio.sleep(1)


# Reader:
async def main():
    while True:
        sock = await channel.connect_async(ADDRESS, wait_writer=True)  # waiting does not block the loop
        with channel.Subscriber(sock) as sub:
            while await sub.wait_async():
                print(sub.latest["vms"][101]["cpu"])


# Reader with a callback: called in the event loop for each new snapshot
def on_snapshot(root):
    print(root["vms"][101]["cpu"])


async def main():
    sock = await channel.connect_async(ADDRESS, wait_writer=True)
    with channel.Subscriber(sock) as sub:
        sub.attach(on_snapshot)  # on_snapshot(sub.latest) for each new snapshot
        while await sub.wait_async():  # works with a callback too; False — the writer closed or crashed
            pass
```

Example: [channel_pubsub_async.py](https://github.com/Aleksandrln/shmobj/blob/master/examples/channel_pubsub_async.py).

Synchronous and asynchronous writers and readers are compatible: for example, an asyncio reader can connect to a
synchronous writer.

### File, `bytes` and `mmap`

`dumps()` builds a buffer in the `shmobj` format, and `loads()` opens it without copying over `bytes`, `mmap` or another
supported buffer.
When several processes read the same file, its pages are served from the shared OS file cache.

```python
import mmap
import shmobj

data = {"users": [{"id": 1, "name": "Ann", "score": 42}]}

# Save the structure to a file.
with open("data.shmobj", "wb") as file:
    file.write(shmobj.dumps(data))

with open("data.shmobj", "rb") as file:  # in every process: file pages from the OS cache — one copy for all
    root = shmobj.loads(mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ))
```

## Segment lifecycle

- `close()` closes the segment in the current process.
- `unlink()` removes the segment name from the OS. Leaving a `with` block calls `close()`. `unlink()` is called
  automatically only for segments created with `unlink_on_close=True`.
- Proxy objects keep the segment open. After `close()` accessing them raises `SegmentClosedError`.
- **Linux and macOS:** a segment is a POSIX shared memory object. On Linux it is a file in `/dev/shm` stored in RAM;
  the segment exists until `unlink()`, not until its creator exits.
- **Windows:** the segment is removed after the last handle is closed.

If the whole process group crashes on Linux or macOS, the segment name stays in the OS until a reboot (in a container —
until it restarts) or `rm /dev/shm/<name>` on Linux.

More: [Segment lifecycle](https://github.com/Aleksandrln/shmobj/blob/master/doc/usage.md#жизненный-цикл-сегмента).

## Limitations

- **Read-only access.** Published data cannot be modified in place: to update it, publish a new snapshot via
  [`shmobj.latest`](#shmobjlatest) or [`shmobj.channel`](#pubsub-shmobjchannel).
- **Supported values:** `dict`, `list`, `tuple` (read back as a list), `None`, `bool`, arbitrary-precision `int`,
  `float`, `str` and `bytes`. Dictionary keys must be scalars.
- **macOS — partial support:** no `shmobj.channel` (use `shmobj.latest`); the package passes the tests in CI
  (arm64) but has not been used in production projects; the x86_64 wheel is not tested.
- **Format checks:** corrupted data raises `FormatError` when accessed. `validate()` checks the whole buffer.
- **Docker:** by default each container has its own `/dev/shm` (64 MB) and its own network. This is solved by
  configuration: shared IPC and `shm_size` in compose, or a channel over a socket file in a shared volume —
  [Docker](https://github.com/Aleksandrln/shmobj/blob/master/doc/usage.md#docker).

## Why not the standard Python tools

- **`multiprocessing.shared_memory`.** Provides a shared byte buffer with no support for nested Python structures.
  The structure has to be serialized, e.g. with `pickle`, and each process deserializes it into its own copy.
- **`multiprocessing.Manager().dict()`.** The data is served by a separate server process. Accessing a dictionary item
  takes an IPC request and serialization.
- **`fork`.** By itself does not guarantee memory savings for Python objects: reference count updates can trigger
  copy-on-write and copying of pages between processes.

More: [Comparison with alternatives](https://github.com/Aleksandrln/shmobj/blob/master/doc/comparison.md).

## Documentation

In Russian.

- [Guide](https://github.com/Aleksandrln/shmobj/blob/master/doc/usage.md) — segments, files, data validation, `channel`, `latest`, Docker and the debugger.
- [API Reference](https://github.com/Aleksandrln/shmobj/blob/master/doc/api.md) — the public API.
- [Build](https://github.com/Aleksandrln/shmobj/blob/master/doc/build.md) — wheel layout and portable builds.
- [Data format](https://github.com/Aleksandrln/shmobj/blob/master/doc/format.md) and [architecture](https://github.com/Aleksandrln/shmobj/blob/master/doc/architecture.md) — the binary format and internals.
- [Development](https://github.com/Aleksandrln/shmobj/blob/master/doc/dev.md) — environment, checks and repository layout.
- [Measurements and project status](https://github.com/Aleksandrln/shmobj/blob/master/doc/status.md).

## Building from source

### The lazy way

Straight from the repository (pip downloads maturin and builds the wheel; Rust is required):

```bash
pip install git+https://github.com/Aleksandrln/shmobj
```

### Full build from a project checkout

Requires CPython 3.11+ and stable Rust ([rustup](https://rustup.rs)); on Windows also Visual Studio Build Tools with
the C++ component. Rust on Linux and macOS:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

A wheel from a clone; maturin goes into a venv (Debian and Ubuntu forbid `pip install` outside a venv, PEP 668):

```bash
git clone https://github.com/Aleksandrln/shmobj
cd shmobj
python3 -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install maturin
maturin build --release -o dist   # dist/shmobj-<version>-cp311-abi3-<platform>.whl
maturin sdist -o dist             # source distribution
```

The wheel installs into any CPython 3.11+ environment on the same platform: `pip install dist/shmobj-*.whl`. Without
`--release` the build is about 10 times slower. `cargo build --release` builds only the library, not the package.

Wheel layout and portable builds (old glibc, Windows from Linux): [doc/build.md](https://github.com/Aleksandrln/shmobj/blob/master/doc/build.md).

## License

Dual-licensed: [MIT](https://github.com/Aleksandrln/shmobj/blob/master/LICENSE-MIT) or [Apache-2.0](https://github.com/Aleksandrln/shmobj/blob/master/LICENSE-APACHE), at your option.

## Authors

Created by [AleksandrLn](https://github.com/Aleksandrln) with [Claude](https://claude.ai) by Anthropic.
