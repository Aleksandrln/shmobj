"""Жизненный цикл Segment и межпроцессное чтение."""
import mmap
import multiprocessing as mp
import os
import subprocess
import sys

import pytest

import shmobj

DATA = {"users": [{"id": i, "name": f"user_{i:05d}"} for i in range(1000)]}


def test_close_then_access():
    seg = shmobj.create(DATA)
    try:
        users = seg.root["users"]
        u = users[3]
        assert u["name"] == "user_00003"
        it = users.iter_values()
        seg.close()
        seg.close()  # идемпотентен
        assert seg.closed
        for op in (lambda: users[0], lambda: u["id"], lambda: len(u), lambda: next(it), lambda: seg.root):
            with pytest.raises(shmobj.SegmentClosedError):
                op()
    finally:
        seg.unlink()


def test_context_manager_and_unlink():
    seg = shmobj.create([1, 2, 3])
    name = seg.name
    with shmobj.attach(name) as other:
        assert list(other.root) == [1, 2, 3] and other.size == seg.size
    assert other.closed
    seg.unlink()
    seg.close()
    if os.name == "posix":
        with pytest.raises(FileNotFoundError):
            shmobj.attach(name)


def test_loads_over_mmap_holds_export(tmp_path):
    p = tmp_path / "seg.bin"
    p.write_bytes(shmobj.dumps({"a": [1, 2]}))
    with open(p, "rb") as f:
        m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    root = shmobj.loads(m)
    with pytest.raises(BufferError):
        m.close()
    assert root["a"][1] == 2
    del root
    m.close()


def _child(name):
    with shmobj.attach(name) as seg:
        return seg.root["users"][777]["name"]


@pytest.mark.parametrize("method", ["spawn", "fork"] if os.name == "posix" else ["spawn"])
def test_readers_in_other_processes(method):
    seg = shmobj.create(DATA)
    try:
        with mp.get_context(method).Pool(2) as pool:
            assert pool.map(_child, [seg.name] * 4) == ["user_00777"] * 4
        assert seg.root["users"][1]["id"] == 1  # читатели не удалили сегмент
        shmobj.attach(seg.name).close()
    finally:
        seg.close()
        seg.unlink()


def test_reader_exit_keeps_segment_and_no_tracker_warning():
    seg = shmobj.create(DATA)
    try:
        code = f"import shmobj; s = shmobj.attach({seg.name!r}); print(s.root['users'][5]['name'])"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert r.stdout.strip() == "user_00005"
        assert "resource_tracker" not in r.stderr
        shmobj.attach(seg.name).close()
    finally:
        seg.close()
        seg.unlink()
