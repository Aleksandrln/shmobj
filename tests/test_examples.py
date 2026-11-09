"""Скрипты из `examples/` запускаются и работают так, как описано в examples/README.md."""
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from shmobj import channel

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
# Windows: вывод в канал иначе в cp1252; без буфера — строки видны сразу
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
LINE_TIMEOUT = 20

needs_channel = pytest.mark.skipif(not channel.SUPPORTED, reason="канал — Linux и Windows")


def _run(script):
    out = subprocess.run([sys.executable, str(EXAMPLES / script)], capture_output=True, encoding="utf-8", env=ENV,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout.splitlines()


@pytest.mark.parametrize("script", ["shared_pool.py", "file_mmap.py"])
def test_pool_example(script):
    assert _run(script) == [str(1000 * sum(range(100)))]


@needs_channel
@pytest.mark.parametrize("script", ["channel_spawn.py", "channel_spawn_async.py"])
def test_channel_spawn_example(script):
    lines = _run(script)
    assert lines[-1].startswith("последний: tick=") and int(lines[-1].rsplit("=", 1)[1]) >= 20
    assert lines[-2].startswith("tick=")


@needs_channel
def test_channel_fanout_example():
    lines = _run("channel_fanout.py")
    readers = [line for line in lines if line.startswith("читатель ")]
    assert len(readers) == 3 and all(int(line.rsplit("=", 1)[1]) >= 20 for line in readers)
    assert lines[-1].startswith("писатель: последний tick=")


class _Process:
    """Пример, который работает до остановки; строки вывода — из очереди с ожиданием."""

    def __init__(self, script, role):
        self.proc = subprocess.Popen([sys.executable, str(EXAMPLES / script), role], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, encoding="utf-8", env=ENV)
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))

    def line(self):
        try:
            return self.lines.get(timeout=LINE_TIMEOUT)
        except queue.Empty:
            self.kill()
            raise AssertionError(f"нет вывода: {self.proc.stderr.read()}") from None

    def interrupt(self):
        """Ctrl+C: пример выходит штатно (Windows — завершение процесса)."""
        if sys.platform == "win32":
            self.proc.terminate()
        else:
            self.proc.send_signal(signal.SIGINT)
        assert self.proc.wait(10) is not None

    def kill(self):
        self.proc.kill()
        self.proc.wait(10)


@pytest.fixture
def processes():
    started = []
    yield lambda script, role: started.append(_Process(script, role)) or started[-1]
    for proc in started:
        proc.kill()


def _tick(line):
    assert line.startswith("tick="), line
    return int(line.split()[0].split("=")[1])


@needs_channel
@pytest.mark.parametrize("writer, reader", [("channel_pubsub.py", "channel_pubsub.py"),
                                            ("channel_pubsub_async.py", "channel_pubsub_async.py"),
                                            ("channel_pubsub.py", "channel_pubsub_async.py")])
def test_channel_reader_reconnects(processes, writer, reader):
    sub = processes(reader, "reader")  # писателя ещё нет — читатель ждёт
    pub = processes(writer, "writer")
    assert _tick(sub.line()) == 0 and _tick(sub.line()) == 1
    pub.kill()  # падение писателя
    assert sub.line() == "писатель ушёл, жду нового"
    processes(writer, "writer")  # новый писатель на том же адресе
    assert _tick(sub.line()) == 0


def test_latest_example(processes):
    sub = processes("latest_pubsub.py", "reader")
    pub = processes("latest_pubsub.py", "writer")
    assert _tick(sub.line()) == 0 and _tick(sub.line()) == 1  # опрос чаще снимков — видны все
    pub.interrupt()  # имена писателя удалены; читатель держит последний снимок и ждёт
    pub = processes("latest_pubsub.py", "writer")
    assert _tick(sub.line()) == 0  # новый писатель нумерует с начала
    pub.interrupt()
