"""Перегенерация golden/*.bin эталонной реализацией: python3 golden/gen_golden.py"""
import pathlib, sys
here = pathlib.Path(__file__).parent
sys.path.insert(0, str(here.parent / "reference"))
import shmobj_ref as ref
from cases import CASES

for name, obj in CASES.items():
    buf = ref.build(obj)
    ref.validate(buf)
    r = ref.load(buf)
    assert (r.materialize() if hasattr(r, "materialize") else r) == obj, name
    (here / f"{name}.bin").write_bytes(buf)
    print(f"{name}.bin  {len(buf)} B")
