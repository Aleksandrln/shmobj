"""Канал снимков `shmobj.channel`: memfd по сокету, последний снимок, освобождение, asyncio."""
import asyncio
import os
import socket
import subprocess
import sys

import pytest

import shmobj
from shmobj import channel

needs_channel = pytest.mark.skipif(not channel.SUPPORTED, reason="канал — только Linux")


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


@pytest.fixture
def ends(request):
    pub_sock, sub_sock = channel.socketpair()
    pub = channel.Publisher(pub_sock, name=f"shmobj-test-{request.node.name}")
    sub = channel.Subscriber(sub_sock)
    yield pub, sub
    pub.close()
    sub.close()


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
def test_only_last_of_queued_is_loaded_and_rest_closed(ends):
    pub, sub = ends
    for i in range(5):
        assert pub.publish([i])
    assert sub.receive()
    assert list(sub.latest) == [4]
    assert _memfd(pub._name) == (1, 1)


@needs_channel
def test_previous_snapshot_is_released(ends):
    pub, sub = ends
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


@needs_channel
def test_snapshot_is_sealed(ends):
    pub, sub = ends
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


@needs_channel
def test_unsealed_snapshot_is_rejected(ends):
    pub, sub = ends
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
def test_publisher_close_ends_channel(ends):
    pub, sub = ends
    pub.publish([1])
    pub.close()
    assert sub.receive()  # снимок, отправленный до закрытия, доходит
    assert sub.closed and list(sub.latest) == [1]
    assert not sub.receive()


@needs_channel
def test_publish_after_subscriber_close(ends):
    pub, sub = ends
    sub.close()
    with pytest.raises(BrokenPipeError):
        pub.publish([1])


@needs_channel
def test_verify_checks_checksum(ends):
    pub, sub = ends
    sub._verify = True
    assert pub.publish({"a": 1})
    assert sub.receive() and sub.latest["a"] == 1


@needs_channel
def test_wrong_socket_type():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    with a, b, pytest.raises(ValueError, match="SOCK_SEQPACKET"):
        channel.Subscriber(a)


def test_unsupported_platform(monkeypatch):
    monkeypatch.setattr(channel, "SUPPORTED", False)
    with pytest.raises(NotImplementedError):
        channel.socketpair()


@needs_channel
def test_asyncio_attach():
    async def main():
        pub_sock, sub_sock = channel.socketpair()
        pub, sub = channel.Publisher(pub_sock), channel.Subscriber(sub_sock)
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
def test_close_after_socket_closed_outside():
    async def main():
        pub_sock, sub_sock = channel.socketpair()
        with channel.Publisher(pub_sock):
            sub = channel.Subscriber(sub_sock)
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
"""


@needs_channel
def test_child_process_publisher():
    pub_sock, sub_sock = channel.socketpair()
    sub = channel.Subscriber(sub_sock)
    with pub_sock:
        proc = subprocess.run([sys.executable, "-c", _CHILD, str(pub_sock.fileno())],
                              pass_fds=[pub_sock.fileno()], timeout=60)
    assert proc.returncode == 0
    assert sub.receive()
    assert sub.latest["data"][999] == 999 and sub.latest["pid"] != os.getpid()
    assert sub.closed  # писатель вышел, свой конец родитель закрыл
