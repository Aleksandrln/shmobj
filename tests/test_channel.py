"""Канал снимков `shmobj.channel`: memfd по сокету (Linux) и именованные снимки с подтверждением
(Windows; на Linux — тем же кодом, для проверки протокола), последний снимок, освобождение, asyncio."""
import asyncio
import os
import socket
import subprocess
import sys
import threading
import time
from multiprocessing import shared_memory

import pytest

import shmobj
from shmobj import channel
from shmobj.channel import _memfd, _named, _sock

needs_channel = pytest.mark.skipif(not channel.SUPPORTED, reason="канал — Linux и Windows")
needs_memfd = pytest.mark.skipif(not _sock.MEMFD, reason="memfd — только Linux")
# именованный режим — платформенный на Windows; на Linux его тем же кодом гоняют тесты
MODES = (["memfd"] if _sock.MEMFD else []) + (["named"] if channel.SUPPORTED else [])
CLASSES = {"memfd": (_memfd.MemfdPublisher, _memfd.MemfdSubscriber),
           "named": (_named.NamedPublisher, _named.NamedSubscriber)}


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


def _memfd_refs(name):
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
    name = f"{pub._prefix}-{i}"
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
    monkeypatch.setattr(_sock, "SUPPORTED", False)
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
        assert sub.closed and sub._receiver is None
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
            assert sub.closed and sub._receiver is None

    asyncio.run(main())


_CHILD = """
import os, socket, sys
from shmobj import channel
from shmobj.channel import _memfd, _named, _sock
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


@pytest.fixture
def fanout(request, mode):
    """Писатель с тремя подписчиками."""
    pub_cls, sub_cls = CLASSES[mode]
    pub = pub_cls(name=f"shmobj-test-{os.getpid()}-{request.node.name}".replace("[", "-")[:60])
    subs = []
    for _ in range(3):
        pub_sock, sub_sock = _pair(mode)
        pub.add(pub_sock)
        subs.append(sub_cls(sub_sock))
    yield pub, subs
    pub.close()
    for sub in subs:
        sub.close()


@needs_channel
def test_fanout_all_get_snapshot(fanout):
    pub, subs = fanout
    assert pub.subscribers == 3
    assert pub.publish({"n": 1})
    assert all(sub.receive() for sub in subs)
    assert [sub.latest["n"] for sub in subs] == [1, 1, 1]


@needs_channel
def test_fanout_one_snapshot_for_all(fanout, mode):
    pub, subs = fanout
    pub.publish(list(range(1000)))
    for sub in subs:
        sub.receive()
    if mode == "named":
        assert pub._seq == 1  # одна SharedMemory, имя ушло всем
        return
    with open("/proc/self/maps") as maps:
        inodes = {line.split()[4] for line in maps if f"/memfd:{pub._name} " in line}
    assert len(inodes) == 1  # три отображения одного memfd


@needs_channel
def test_fanout_lagging_subscriber_is_skipped(fanout):
    pub, (slow, *rest) = fanout
    sent = []
    for i in range(500):
        sent.append(pub.publish([i]))
        for sub in rest:
            sub.receive()
    assert False in sent  # у slow очередь полна, остальным снимки идут
    assert [list(sub.latest) for sub in rest] == [[499], [499]]
    assert slow.receive() and list(slow.latest) != [499]
    assert pub.publish(["after"])
    assert all(sub.receive() for sub in (slow, *rest))
    assert [list(sub.latest) for sub in (slow, *rest)] == [["after"]] * 3


@needs_channel
def test_fanout_closed_subscriber_is_dropped(fanout):
    pub, subs = fanout
    subs[0].close()
    for _ in range(100):  # loopback TCP (Windows): конец приходит не мгновенно
        pub.publish({"n": 1})
        if pub.subscribers == 2:
            break
        time.sleep(0.01)
    assert pub.subscribers == 2
    assert all(sub.receive() for sub in subs[1:])
    subs[1].close()
    subs[2].close()
    with pytest.raises(BrokenPipeError):  # ушёл последний
        for _ in range(100):
            pub.publish({"n": 2})
            time.sleep(0.01)
    assert pub.subscribers == 0
    assert pub.publish({"n": 3})  # без подписчиков — только последний для следующего add


@needs_channel
def test_new_subscriber_gets_latest(mode):
    pub_cls, sub_cls = CLASSES[mode]
    with pub_cls(name=f"shmobj-test-{os.getpid()}-latest") as pub:
        assert pub.publish({"n": 1})  # подписчиков ещё нет
        pub_sock, sub_sock = _pair(mode)
        pub.add(pub_sock)
        with sub_cls(sub_sock) as sub:
            assert sub.receive() and sub.latest["n"] == 1


@needs_channel
def test_closed_publisher_rejects(mode):
    pub_cls, _sub_cls = CLASSES[mode]
    pub = pub_cls()
    pub.close()
    pub.close()
    assert pub.closed
    with pytest.raises(ValueError):
        pub.publish([1])
    pub_sock, sub_sock = _pair(mode)
    with pub_sock, sub_sock, pytest.raises(ValueError):
        pub.add(pub_sock)


@needs_channel
def test_listen_connect(tmp_path):
    address = str(tmp_path / "snap.sock") if _sock.MEMFD else ("127.0.0.1", 0)
    with channel.listen(address) as listener, channel.Publisher() as pub:
        pub.publish({"n": 1})
        with channel.Subscriber(channel.connect(listener.getsockname())) as sub:
            pub.add(listener.accept()[0])
            for _ in range(100):
                if sub.receive():
                    break
                time.sleep(0.01)
            assert sub.latest["n"] == 1


def _free_address(tmp_path):
    """Адрес, который никто не слушает; Windows — порт, только что свободный."""
    if _sock.MEMFD:
        return str(tmp_path / "snap.sock")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()


def _listen_later(address, listeners):
    timer = threading.Timer(0.3, lambda: listeners.append(channel.listen(address)))
    timer.start()
    return timer


@needs_channel
def test_connect_wait_writer(tmp_path):
    address = _free_address(tmp_path)
    with pytest.raises(_sock.NO_WRITER):
        channel.connect(address)
    start = time.monotonic()
    with pytest.raises(_sock.NO_WRITER):
        channel.connect(address, wait_writer=0.3)
    assert time.monotonic() - start >= 0.3
    listeners = []
    timer = _listen_later(address, listeners)
    try:
        with channel.connect(address, wait_writer=True):  # писателя ещё нет — ждёт, пока он не начнёт слушать
            assert listeners
    finally:
        timer.join()
        for listener in listeners:
            listener.close()


@needs_channel
def test_connect_async_wait_writer(tmp_path):
    address = _free_address(tmp_path)

    async def main():
        waiting = asyncio.create_task(channel.connect_async(address, wait_writer=True))
        await asyncio.sleep(0.3)
        waiting.cancel()  # ожидание писателя отменяется вместе с задачей
        with pytest.raises(asyncio.CancelledError):
            await waiting
        with pytest.raises(_sock.NO_WRITER):
            await channel.connect_async(address, wait_writer=0.1)
        listeners = []
        timer = _listen_later(address, listeners)
        try:
            with await channel.connect_async(address, wait_writer=True):
                assert listeners
        finally:
            timer.join()
            for listener in listeners:
                listener.close()

    asyncio.run(main())


def _listen(mode, tmp_path):
    if mode == "memfd":
        return channel.listen(str(tmp_path / "snap.sock"))
    return socket.create_server(("127.0.0.1", 0))  # протокол Windows на любой платформе — TCP


def _connect(mode, address):
    return channel.connect(address) if mode == "memfd" else socket.create_connection(address)


def _wait_receive(sub, n):
    for _ in range(200):
        if sub.receive() and sub.latest["n"] == n:
            return
        time.sleep(0.01)
    raise AssertionError(f"снимок {n} не пришёл")


@needs_channel
def test_wait(ends):
    pub, sub = ends
    started = time.monotonic()
    assert not sub.wait(0.1)  # снимков нет — истёк срок
    assert time.monotonic() - started >= 0.1
    threading.Timer(0.1, pub.publish, [{"n": 1}]).start()
    assert sub.wait(5) and sub.latest["n"] == 1  # проснулся на снимке из другого потока
    pub.publish({"n": 2})
    pub.publish({"n": 3})
    assert sub.wait(0) and sub.latest["n"] == 3  # готовый снимок — без ожидания, последний из очереди
    threading.Timer(0.1, pub.close).start()
    assert not sub.wait() and sub.closed  # конец канала будит ожидание без срока
    assert sub.latest["n"] == 3


@needs_channel
def test_wait_async(ends):
    pub, sub = ends

    async def main():
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        assert not await sub.wait_async(0.1)  # снимков нет — истёк срок
        assert time.monotonic() - started >= 0.1
        loop.call_later(0.1, pub.publish, {"n": 1})
        assert await sub.wait_async(5) and sub.latest["n"] == 1  # цикл не заблокирован: call_later сработал
        pub.publish({"n": 2})
        pub.publish({"n": 3})
        assert await sub.wait_async(0) and sub.latest["n"] == 3  # готовый снимок — последний из очереди
        task = asyncio.create_task(sub.wait_async())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        loop.call_later(0.1, pub.close)
        assert not await sub.wait_async() and sub.closed  # конец канала будит ожидание без срока
        assert sub.latest["n"] == 3

    asyncio.run(main())


@needs_channel
def test_wait_with_loop_receiver(ends):
    _pub, sub = ends

    async def main():
        sub.attach()
        with pytest.raises(ValueError):  # сокет читает цикл
            sub.wait(0)
        sub.detach()
        assert not sub.wait(0)
        assert not await sub.wait_async(0)  # включает приём в цикле
        with pytest.raises(ValueError):
            sub.wait(0)

    asyncio.run(main())


@needs_channel
def test_wait_async_with_attach(ends):
    pub, sub = ends

    async def main():
        loop = asyncio.get_running_loop()
        got = []
        sub.attach(lambda root: got.append(root["n"]))
        loop.call_later(0.05, pub.publish, {"n": 1})
        first, second = await asyncio.gather(sub.wait_async(5), sub.wait_async(5))
        assert first and second and got == [1]  # один снимок будит всех ждущих и колбэк
        assert not await sub.wait_async(0)  # с прошлого вызова снимков не было
        pub.publish({"n": 2})
        await asyncio.sleep(0.05)  # снимок принял колбэк, пока wait_async не ждал
        assert got == [1, 2] and await sub.wait_async(0) and sub.latest["n"] == 2

        task = asyncio.create_task(sub.wait_async())
        await asyncio.sleep(0.05)
        sub.detach()
        assert not await task  # приём снят

        sub.attach(lambda root: got.append(root["n"]))
        loop.call_later(0.05, pub.close)
        assert not await sub.wait_async() and sub.closed  # конец канала будит ожидание
        assert got == [1, 2]

    asyncio.run(main())


@needs_channel
def test_accept_in_thread(mode, tmp_path):
    pub_cls, sub_cls = CLASSES[mode]
    pub = pub_cls(name=f"shmobj-test-{os.getpid()}-thread-{mode}")
    listener = _listen(mode, tmp_path)
    pub.publish({"n": 1})
    address = pub.accept_in_thread(listener)
    assert address == listener.getsockname()
    with pytest.raises(ValueError):  # приём уже идёт
        pub.accept_in_thread(listener)
    subs = [sub_cls(_connect(mode, address)) for _ in range(3)]
    try:
        for sub in subs:
            _wait_receive(sub, 1)  # последний снимок — сразу при подключении, без нового publish
        assert pub.publish({"n": 2})
        for sub in subs:
            _wait_receive(sub, 2)
        pub.close()
        assert listener.fileno() == -1
        assert not any(t.name == f"{pub._name}-accept" for t in threading.enumerate())
        for sub in subs:
            for _ in range(200):
                sub.receive()
                if sub.closed:
                    break
                time.sleep(0.01)
            assert sub.closed  # close писателя закрыл и сокеты подписчиков
    finally:
        pub.close()
        for sub in subs:
            sub.close()


@needs_channel
def test_accept_last_subscriber_gone_no_error(mode, tmp_path):
    pub_cls, sub_cls = CLASSES[mode]
    with pub_cls(name=f"shmobj-test-{os.getpid()}-gone-{mode}") as pub:
        address = pub.accept_in_thread(_listen(mode, tmp_path))
        with sub_cls(_connect(mode, address)) as sub:
            pub.publish({"n": 1})
            _wait_receive(sub, 1)
        for _ in range(200):  # ушёл последний: без BrokenPipeError, пока идёт приём
            pub.publish({"n": 2})
            if pub.subscribers == 0:
                break
            time.sleep(0.01)
        assert pub.subscribers == 0
        with sub_cls(_connect(mode, address)) as sub:
            _wait_receive(sub, 2)  # новый подписчик получает последний снимок


@needs_channel
def test_accept_in_loop(mode, tmp_path):
    pub_cls, sub_cls = CLASSES[mode]

    async def main():
        loop = asyncio.get_running_loop()
        with pub_cls(name=f"shmobj-test-{os.getpid()}-loop-{mode}") as pub:
            listener = _listen(mode, tmp_path)
            pub.publish({"n": 1})
            address = pub.accept_in_loop(listener)
            assert address == listener.getsockname()
            got = asyncio.Queue()
            subs = [sub_cls(_connect(mode, address)) for _ in range(2)]
            for sub in subs:
                sub.attach(lambda root: got.put_nowait(root["n"]))
            assert [await asyncio.wait_for(got.get(), 5) for _ in subs] == [1, 1]
            pub.publish({"n": 2})
            assert [await asyncio.wait_for(got.get(), 5) for _ in subs] == [2, 2]
            fd = listener.fileno()
        assert listener.fileno() == -1
        for sub in subs:
            sub.close()
        try:
            assert not loop.remove_reader(fd)  # приём снят с цикла
        except NotImplementedError:  # ProactorEventLoop (Windows): приём — задача, она отменена
            others = asyncio.all_tasks() - {asyncio.current_task()}
            assert not others or not (await asyncio.wait(others, timeout=5))[1]

    asyncio.run(main())


@needs_channel
def test_named_waits_for_every_subscriber():
    pub = _named.NamedPublisher(name=f"shmobj-test-{os.getpid()}-acks")
    pairs = [socket.socketpair() for _ in range(2)]
    for pub_sock, _ in pairs:
        pub.add(pub_sock)
    a, b = (_named.NamedSubscriber(sub_sock) for _, sub_sock in pairs)
    try:
        pub.publish([0])
        a.receive()
        a._latest = None  # прокси отпущены: снимок держит только писатель
        pub.publish([1])
        assert _held(pub, 0)  # 0 подтвердил только a
        b.receive()  # b берёт 1 и подтверждает 0 и 1
        a.receive()
        a._latest = b._latest = None
        pub.publish([2])
        assert not _held(pub, 0) and not _held(pub, 1) and _held(pub, 2)
    finally:
        pub.close()
        a.close()
        b.close()


# memfd (Linux)

@pytest.fixture
def memfd_ends(request):
    pub_sock, sub_sock = channel.socketpair()
    pub = _memfd.MemfdPublisher(pub_sock, name=f"shmobj-test-{request.node.name}")
    sub = _memfd.MemfdSubscriber(sub_sock)
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
    assert _memfd_refs(pub._name) == (1, 2)  # отображение читателя и дескриптор последнего снимка у писателя


@needs_memfd
def test_memfd_previous_snapshot_is_released(memfd_ends):
    pub, sub = memfd_ends
    for i in range(3):
        pub.publish([i])
        sub.receive()
    assert _memfd_refs(pub._name) == (1, 2)  # писатель держит только последний
    held = sub.latest
    pub.publish([3])
    sub.receive()
    assert _memfd_refs(pub._name) == (2, 3)  # прокси прежнего снимка держит его отображение
    assert list(held) == [2]
    del held
    assert _memfd_refs(pub._name) == (1, 2)


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


def _send_unsealed(pub, obj):
    """Снимок в memfd без печатей — читатель его отвергает."""
    fd = os.memfd_create(pub._name, os.MFD_CLOEXEC)
    try:
        os.write(fd, shmobj.dumps(obj))
        socket.send_fds(pub._subs[0], [b"S"], [fd])
    finally:
        os.close(fd)


@needs_memfd
def test_wait_async_receive_error(memfd_ends):
    pub, sub = memfd_ends

    async def main():
        loop = asyncio.get_running_loop()
        handled = []
        loop.set_exception_handler(lambda _loop, context: handled.append(context["exception"]))
        pub.publish([1])
        assert await sub.wait_async(5)  # приём включил wait_async
        loop.call_later(0.05, _send_unsealed, pub, [2])
        with pytest.raises(ValueError, match="не запечатан"):  # ждущему — с трейсбеком приёма
            await sub.wait_async(5)
        _send_unsealed(pub, [3])
        await asyncio.sleep(0.05)  # принял цикл, пока никто не ждал
        with pytest.raises(ValueError, match="не запечатан"):
            await sub.wait_async(5)
        assert not await sub.wait_async(0)
        _send_unsealed(pub, [4])
        await asyncio.sleep(0.05)
        sub.detach()  # не забранная до снятия приёма — обработчику цикла
        assert [type(e) for e in handled] == [ValueError]

        sub.attach(lambda root: 1 / 0)  # приём включил attach
        _send_unsealed(pub, [5])
        await asyncio.sleep(0.05)  # отдельно: из очереди receive берёт только последний
        pub.publish([6])
        await asyncio.sleep(0.05)
        assert [type(e) for e in handled] == [ValueError] * 2 + [ZeroDivisionError]  # ждущих нет — обработчику цикла
        assert list(sub.latest) == [6]

    asyncio.run(main())


@needs_memfd
def test_memfd_unsealed_snapshot_is_rejected(memfd_ends):
    pub, sub = memfd_ends
    pub.publish([1])
    sub.receive()
    _send_unsealed(pub, [2])
    with pytest.raises(ValueError, match="не запечатан"):
        sub.receive()
    assert list(sub.latest) == [1]
    assert _memfd_refs(pub._name) == (1, 2)


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
    pub = _named.NamedPublisher(pub_sock, name=f"shmobj-test-{os.getpid()}")
    sub = _named.NamedSubscriber(sub_sock)
    yield pub, sub
    pub.close()
    sub.close()


@needs_channel
def test_named_publishers_same_name():
    pairs = [socket.socketpair() for _ in range(2)]
    pubs = [_named.NamedPublisher(pub_sock, name=f"shmobj-test-{os.getpid()}") for pub_sock, _ in pairs]
    subs = [_named.NamedSubscriber(sub_sock) for _, sub_sock in pairs]
    try:
        for i, (pub, sub) in enumerate(zip(pubs, subs)):
            pub.publish([i])
            assert sub.receive() and list(sub.latest) == [i]
    finally:
        for end in pubs + subs:
            end.close()


@needs_channel
def test_named_ack_releases_writer_copies(named_ends):
    pub, sub = named_ends
    for i in range(3):
        pub.publish([i])
    sub.receive()
    assert [_held(pub, i) for i in range(3)] == [True] * 3  # подтверждения ещё не прочитаны
    pub.publish([3])  # читает подтверждения 0–2
    assert list(pub._subs[0].pending) == [f"{pub._prefix}-3".encode()]
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
    pub._subs[0].sock.sendall(b"shmobj-test-gone\n")  # писатель вышел, не дождавшись читателя
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
