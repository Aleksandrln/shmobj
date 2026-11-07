"""Примеры из `examples/` запускаются и доходят до последнего снимка."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shmobj import channel

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.mark.skipif(not channel.SUPPORTED, reason="канал — Linux и Windows")
@pytest.mark.parametrize("script", ["channel_sync.py", "channel_async.py"])
def test_channel_example(script):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}  # Windows: вывод в канал иначе в cp1252
    out = subprocess.run([sys.executable, str(EXAMPLES / script)], capture_output=True, encoding="utf-8",
                         env=env, timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[-1].startswith("последний: tick=") and int(lines[-1].rsplit("=", 1)[1]) >= 20
    assert lines[-2].startswith("tick=")
