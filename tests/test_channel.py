"""Канал снимков `shmobj.channel`: memfd по сокету (Linux) и именованные снимки с подтверждением
(Windows; на Linux — тем же кодом, для проверки протокола), последний снимок, освобождение, asyncio."""
import asyncio
import os
import socket
import subprocess
import sys
import time
from multiprocessing import shared_memory

import pytest

import shmobj
from shmobj import channel

needs_channel = pytest.mark.skipif(not channel.SUPPORTED, reason="канал — Linux и Windows")
needs_memfd = pytest.mark.skipif(not channel._MEMFD, reason="memfd — только Linux")
# именованный режим — платформенный на Windows; на Linux его тем же кодом гоняют тесты
MODES = (["memfd"] if channel._MEMFD else []) + (["named"] if channel.SUPPORTED else [])
CLASSES = {"memfd": (channel._MemfdPublisher, channel._MemfdSubscriber),
           "named": (channel._NamedPublisher, channel._NamedSubscriber)}


def _pair(mode):
    return channel.socketpair() if mode == "memfd" else socket.socketpair()


@pytest.fixture(params=MODES)
def mode(request):
    return request.param


@pytest.fixture
def ends(request, mode):
    pub_cls, sub_cls = CLASSES[mode]
    pub_sock, sub_sock = _pair(mode)
    pub = pub_cls(pub_sock, name=f"shmobj-test-{os.getpid()}-{request.node.name}".replace("[", "-")[:60])
    sub = sub_cls(sub_sock)
    yield pub, sub
    pub.close()
    sub.close()


def _memfd(name):
    """(отображения, дескрипторы) memfd с именем `name` в этом процессе; `mmap` держит копию дескриптора."""
    with open("/proc/self/maps") as maps:
        mappings = sum(f"/memfd:{name} " in line for line in maps)
    fds = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            fds += os.readlink(f"/proc/self/fd/{fd}").startswith(f"/memfd:{name} ")
        except OSError:
            pass
    return mappings, fds


def _held(pub, i):
    """Жив ли i-й снимок именованного писателя `pub` (его держит кто-то в этом процессе)."""
    name = f"{pub._name}-{os.getpid()}-{i}"
    if os.name != "nt":  # POSIX: имя удаляется при подтверждении, отображение остаётся
        with open("/proc/self/maps") as maps:
            return any(f"/dev/shm/{name}" in line for line in maps)
    try:
        shared_memory.SharedMemory(name=name).close()
    except FileNotFoundError:
        return False
    return True


@needs_channel
def test_roundtrip(ends):
    pub, sub = ends
    assert sub.latest is None and not sub.receive()
    assert pub.publish({"vms": {101: {"cpu": 0.5}}})
    assert sub.receive()
    assert isinstance(sub.latest, shmobj.FrozenDict)
    assert sub.latest["vms"][101].materialize() == {"cpu": 0.5}
    assert not sub.receive()  # очередь пуста, `latest` прежний
    assert sub.latest["vms"][101]["cpu"] == 0.5


@needs_channel
def test_only_last_of_queued_is_loaded(ends):
    pub, sub = ends
    for i in range(3):
        assert pub.publish([i])
    assert sub.receive()
    assert list(sub.latest) == [2]


@needs_channel
def test_full_queue_skips_snapshots(ends):
    pub, sub = ends
    sent = [pub.publish([i]) for i in range(500)]
    assert False in sent and sent.index(False) < 100
    accepted = sent.index(False) - 1
    assert sub.receive()
    assert list(sub.latest) == [accepted]
    assert pub.publish(["after"])  # очередь разобрана — снова принимает


@needs_channel
def test_publish_after_subscriber_close(ends):
    pub, sub = ends
    sub.close()
    with pytest.raises(BrokenPipeError):
        for _ in range(100):  # loopback TCP (Windows): конец приходит не мгновенно
            pub.publish([1])
            time.sleep(0.01)


@needs_channel
def test_verify_checks_checksum(ends):
    pub, sub = ends
    sub._verify = True
    assert pub.publish({"a": 1})
    assert sub.receive() and sub.latest["a"] == 1


@needs_channel
def test_wrong_socket_type(mode):
    pub_cls, sub_cls = CLASSES[mode]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(ValueError, match=pub_cls._SOCK_TYPE):
        sub_cls(s)


def test_unsupported_platform(monkeypatch):
    monkeypatch.setattr(channel, "SUPPORTED", False)
    with pytest.raises(NotImplementedError):
        channel.socketpair()


@needs_channel
def test_asyncio_attach(mode):
    pub_cls, sub_cls = CLASSES[mode]

    async def main():
        pub_sock, sub_sock = _pair(mode)
        pub, sub = pub_cls(pub_sock), sub_cls(sub_sock)
        got, event = [], asyncio.Event()

        def on_snapshot(root):
            got.append(root.materialize())
            event.set()

        sub.attach(on_snapshot)
        for i in range(3):
            pub.publish({"i": i})
        await asyncio.wait_for(event.wait(), 5)
        await asyncio.sleep(0.05)
        assert got == [{"i": 2}]  # очередь разобрана одним пробуждением

        pub.close()
        for _ in range(100):
            if sub.closed:
                break
            await asyncio.sleep(0.01)
        assert sub.closed and sub._reader is None
        assert sub.latest["i"] == 2

    asyncio.run(main())


@needs_channel
def test_close_after_socket_closed_outside(mode):
    pub_cls, sub_cls = CLASSES[mode]

    async def main():
        pub_sock, sub_sock = _pair(mode)
        with pub_cls(pub_sock):
            sub = sub_cls(sub_sock)
            sub.attach()
            sub_sock.close()  # чужой код закрыл сокет, не сняв его с цикла
            sub.close()
            assert sub.closed and sub._reader is None

    asyncio.run(main())


_CHILD = """
import os, socket, sys
from shmobj import channel
pub = channel.Publisher(socket.socket(fileno=int(sys.argv[1])))
assert pub.publish({"pid": os.getpid(), "data": list(range(1000))})
sys.stdin.read()  # Windows: снимок живёт, пока писатель его держит
"""


@needs_channel
def test_child_process_publisher():
    pub_sock, sub_sock = channel.socketpair()
    sub = channel.Subscriber(sub_sock)
    with pub_sock:
        proc = subprocess.Popen([sys.executable, "-c", _CHILD, str(pub_sock.fileno())],
                                stdin=subprocess.PIPE, **channel.spawn_kwargs(pub_sock))
    try:
        for _ in range(600):
            if sub.receive() or sub.closed:
                break
            time.sleep(0.05)
        assert sub.latest["data"][999] == 999 and sub.latest["pid"] != os.getpid()
    finally:
        proc.communicate(timeout=60)
    assert proc.returncode == 0
    for _ in range(100):
        sub.receive()
        if sub.closed:
            break
        time.sleep(0.05)
    assert sub.closed  # писатель вышел, свой конец родитель закрыл
    assert sub.latest["data"][999] == 999


# memfd (Linux)

@pytest.fixture
def memfd_ends(request):
    pub_sock, sub_sock = channel.socketpair()
    pub = channel._MemfdPublisher(pub_sock, name=f"shmobj-test-{request.node.name}")
    sub = channel._MemfdSubscriber(sub_sock)
    yield pub, sub
    pub.close()
    sub.close()


@needs_memfd
def test_memfd_queued_are_closed(memfd_ends):
    pub, sub = memfd_ends
    for i in range(5):
        assert pub.publish([i])
    assert sub.receive()
    assert list(sub.latest) == [4]
    assert _memfd(pub._name) == (1, 1)


@needs_memfd
def test_memfd_previous_snapshot_is_released(memfd_ends):
    pub, sub = memfd_ends
    for i in range(3):
        pub.publish([i])
        sub.receive()
    assert _memfd(pub._name) == (1, 1)
    held = sub.latest
    pub.publish([3])
    sub.receive()
    assert _memfd(pub._name) == (2, 2)  # прокси прежнего снимка держит его отображение
    assert list(held) == [2]
    del held
    assert _memfd(pub._name) == (1, 1)


@needs_memfd
def test_memfd_snapshot_is_sealed(memfd_ends):
    pub, sub = memfd_ends
    pub.publish([1])
    _msg, fds, _flags, _addr = socket.recv_fds(sub._sock, 16, 1)
    try:
        with pytest.raises(PermissionError), open(fds[0], "r+b", closefd=False) as f:
            f.write(b"x")
        with pytest.raises(PermissionError):
            os.ftruncate(fds[0], 1)
    finally:
        for fd in fds:
            os.close(fd)


@needs_memfd
def test_memfd_unsealed_snapshot_is_rejected(memfd_ends):
    pub, sub = memfd_ends
    pub.publish([1])
    sub.receive()
    fd = os.memfd_create(pub._name, os.MFD_CLOEXEC)
    try:
        os.write(fd, shmobj.dumps([2]))
        socket.send_fds(pub._sock, [b"S"], [fd])
    finally:
        os.close(fd)
    with pytest.raises(ValueError, match="не запечатан"):
        sub.receive()
    assert list(sub.latest) == [1]
    assert _memfd(pub._name) == (1, 1)


@needs_memfd
def test_memfd_publisher_close_ends_channel(memfd_ends):
    pub, sub = memfd_ends
    pub.publish([1])
    pub.close()
    assert sub.receive()  # снимок, отправленный до закрытия, доходит
    assert sub.closed and list(sub.latest) == [1]
    assert not sub.receive()


# именованные снимки (Windows)

@pytest.fixture
def named_ends():
    pub_sock, sub_sock = socket.socketpair()
    pub = channel._NamedPublisher(pub_sock, name=f"shmobj-test-{os.getpid()}")
    sub = channel._NamedSubscriber(sub_sock)
    yield pub, sub
    pub.close()
    sub.close()


@needs_channel
def test_named_ack_releases_writer_copies(named_ends):
    pub, sub = named_ends
    for i in range(3):
        pub.publish([i])
    sub.receive()
    assert [_held(pub, i) for i in range(3)] == [True] * 3  # подтверждения ещё не прочитаны
    pub.publish([3])  # читает подтверждения 0–2
    assert list(pub._pending) == [f"{pub._name}-{os.getpid()}-3".encode()]
    assert [_held(pub, i) for i in range(4)] == [False, False, True, True]  # 2 — у читателя
    sub.receive()
    pub.publish([4])
    assert [_held(pub, i) for i in range(5)] == [False, False, False, True, True]


@needs_channel
def test_named_previous_snapshot_is_released(named_ends):
    pub, sub = named_ends
    pub.publish([0])
    sub.receive()
    held = sub.latest
    pub.publish([1])
    sub.receive()
    pub.publish([2])  # писатель закрыл свои копии 0 и 1
    assert _held(pub, 0) and list(held) == [0]
    del held
    assert not _held(pub, 0)


@needs_channel
def test_named_missing_snapshot_is_skipped(named_ends):
    pub, sub = named_ends
    pub.publish([1])
    sub.receive()
    pub._sock.sendall(b"shmobj-test-gone\n")  # писатель вышел, не дождавшись читателя
    assert not sub.receive()
    assert list(sub.latest) == [1]


@needs_channel
def test_named_publisher_close_ends_channel(named_ends):
    pub, sub = named_ends
    pub.publish([1])
    sub.receive()
    pub.publish([2])
    pub.close()  # неподтверждённый снимок закрыт вместе с писателем
    for _ in range(100):
        assert not sub.receive()
        if sub.closed:
            break
        time.sleep(0.01)
    assert sub.closed and list(sub.latest) == [1]
