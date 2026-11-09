"""Последний снимок по имени `shmobj.latest`: put/get, порядок запуска, перезапуск и падение писателя, уборка имён."""
import os
import subprocess
import sys

import pytest

import shmobj
from shmobj import _shm, latest

POSIX = os.name == "posix"
posix_only = pytest.mark.skipif(not POSIX, reason="имена без handle живут только на POSIX")


def _exists(name):
    try:
        _shm.open_shm(name).close()
    except FileNotFoundError:
        return False
    return True


def _snapshot(name, gen):
    return f"{name}.{gen:x}"


def _run(code):
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r


@pytest.fixture
def name():
    n = f"lt{os.urandom(4).hex()}"  # macOS: имя SharedMemory не длиннее 30 символов
    yield n
    for gen in range(1, 32):
        _shm.unlink_name(_snapshot(n, gen))
    _shm.unlink_name(n)


def test_put_get(name):
    with latest.Writer(name) as w, latest.Reader(name) as r:
        w.put({"vms": {101: {"cpu": 0.5}}})
        first = r.get()
        assert first["vms"][101]["cpu"] == 0.5
        assert r.get() is first  # снимок не сменился — тот же корень
        w.put({"vms": {101: {"cpu": 0.7}}})
        second = r.get()
        assert second is not first and second["vms"][101]["cpu"] == 0.7
        assert first["vms"][101]["cpu"] == 0.5  # прежний снимок дочитывается


def test_reader_before_writer(name):
    with latest.Reader(name) as r:
        assert r.get() is None
        with latest.Writer(name) as w:
            assert r.get() is None
            w.put([1, 2, 3])
            assert list(r.get()) == [1, 2, 3]


@posix_only
def test_put_and_close_remove_names(name):
    w = latest.Writer(name)
    w.put({"n": 1})
    w.put({"n": 2})
    assert _exists(name) and _exists(_snapshot(name, 2)) and not _exists(_snapshot(name, 1))
    w.close()
    w.close()  # идемпотентен
    assert not _exists(name) and not _exists(_snapshot(name, 2))
    with pytest.raises(ValueError):
        w.put({"n": 3})


def test_reader_keeps_root_after_writer_close(name):
    r = latest.Reader(name)
    with latest.Writer(name) as w:
        w.put({"n": 1})
        root = r.get()
    assert r.get() is root and root["n"] == 1
    r.close()
    assert r.get() is root


def test_writer_restart_after_close(name):
    with latest.Reader(name) as r:
        with latest.Writer(name) as w:
            w.put({"n": 1})
            assert r.get()["n"] == 1
        with latest.Writer(name) as w:  # POSIX — новый управляющий сегмент, нумерация с начала
            w.put({"n": 2})
            assert r.get()["n"] == 2


@pytest.mark.parametrize("finish", ["kill", "exit"])
def test_writer_process_end(name, finish):
    end = "os.kill(os.getpid(), 9)" if finish == "kill" else "pass"
    code = (f"import os, shmobj; w = shmobj.latest.Writer({name!r}); w.put({{'n': 1}}); w.put({{'n': 2}}); {end}")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert "resource_tracker" not in r.stderr
    if finish == "exit":
        assert r.returncode == 0, r.stderr
        assert not _exists(name)  # выход интерпретатора — как close()
        return
    if not POSIX:
        assert not _exists(name)  # Windows: handle закрыла ОС
        return
    assert _exists(name) and _exists(_snapshot(name, 2))  # падение: имена остались
    with latest.Reader(name) as reader:
        assert reader.get()["n"] == 2  # последний снимок упавшего писателя
        with latest.Writer(name) as w:  # подхватывает нумерацию
            w.put({"n": 3})
            assert not _exists(_snapshot(name, 2)) and _exists(_snapshot(name, 3))
            assert reader.get()["n"] == 3
    assert not _exists(name) and not _exists(_snapshot(name, 3))


@posix_only
def test_snapshot_name_taken(name):
    stale = _shm.create({"stale": True}, _snapshot(name, 1), dedup=True, checksum=True, track=False)
    stale.close()
    with latest.Writer(name) as w, latest.Reader(name) as r:
        w.put({"n": 1})
        assert r.get()["n"] == 1
        assert not _exists(_snapshot(name, 1))


def test_name_taken_by_other_segment(name):
    seg = shmobj.create({"other": 1}, name)
    try:
        with pytest.raises(ValueError, match="другого формата"):
            latest.Writer(name)
    finally:
        seg.close()
        seg.unlink()


@posix_only
def test_snapshot_gone_before_attach(name):
    with latest.Writer(name) as w, latest.Reader(name) as r:
        w.put({"n": 1})
        root = r.get()
        w.put({"n": 2})
        _shm.unlink_name(_snapshot(name, 2))  # как если бы писатель уже ушёл дальше
        assert r.get() is root


def test_reader_in_other_process(name):
    with latest.Writer(name) as w:
        w.put({"users": [{"id": i} for i in range(100)]})
        code = f"import shmobj; print(shmobj.latest.Reader({name!r}).get()['users'][42]['id'])"
        r = _run(code)
        assert r.stdout.strip() == "42" and "resource_tracker" not in r.stderr
        assert _exists(name)  # выход читателя имён не трогает
