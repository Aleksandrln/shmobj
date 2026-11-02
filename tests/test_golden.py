"""golden/*.bin: validate, чтение и побайтовое совпадение сборки с эталоном."""
import pathlib

import pytest
from cases import CASES

import shmobj

GOLDEN = pathlib.Path(__file__).resolve().parent.parent / "golden"


def _plain(v):
    return v.materialize() if hasattr(v, "materialize") else v


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_reads(name):
    buf = (GOLDEN / f"{name}.bin").read_bytes()
    shmobj.validate(buf)
    assert _plain(shmobj.loads(buf, verify=True)) == CASES[name]


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_bytes_match(name):
    assert shmobj.dumps(CASES[name]) == (GOLDEN / f"{name}.bin").read_bytes()
