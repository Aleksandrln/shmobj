# Сборка пакета и использование

## Как устроен пакет

Колесо (`.whl`) — zip-архив, который `pip` распаковывает в `site-packages` без компиляции:

```
shmobj/__init__.py       # Python-обёртка: реэкспорт, create/attach
shmobj/_shm.py           # SharedMemory, resource_tracker
shmobj/_core.abi3.so     # скомпилированные shmobj-core + shmobj-py (на Windows — _core.pyd)
shmobj/__init__.pyi, py.typed
```

Rust-часть компилируется один раз, при сборке колеса, в нативную библиотеку-модуль расширения
CPython. `import shmobj` выполняет `__init__.py`, который делает `from ._core import ...` —
интерпретатор загружает `_core.abi3.so` как обычный модуль расширения (PyO3 генерирует
функцию `PyInit__core`). Rust-тулчейн у пользователя не нужен.

Колесо привязано к ОС и архитектуре (тег вида `manylinux_2_34_x86_64`, `win_amd64`,
`macosx_11_0_arm64`), но не к версии Python: сборка с `abi3-py311` использует только стабильный
ABI, поэтому одно колесо подходит для CPython 3.11, 3.12, 3.13 и новее.

Если для платформы пользователя колеса нет, `pip` берёт sdist (`.tar.gz` с исходниками) и
собирает его через maturin — тогда нужен Rust stable (`rustup`).

## Сборка

Нужны Rust stable и `pip install maturin`.

```bash
maturin build --release --out dist   # колесо для текущей платформы
maturin sdist --out dist             # исходники
pip install dist/shmobj-*.whl
```

Колесо, собранное локально на Linux, получает тег по glibc этой машины (например,
`manylinux_2_34`) и не ставится на системы со старой glibc. Переносимое Linux-колесо (glibc ≥ 2.17)
локально — через zig, без Docker:

```bash
pip install maturin ziglang
maturin build --release --zig --compatibility manylinux2014 --out dist
```

Переносимые колёса для всех платформ
собирает задача `wheels` в CI (`PyO3/maturin-action`, Linux — в manylinux-контейнере,
x86_64/aarch64, Windows x64, macOS arm64); результат — артефакты `wheel-*` прогона.

Для разработки: `maturin develop --release` — сборка и установка в активный venv в режиме editable
(см. [dev.md](dev.md)).

## Использование

```python
import multiprocessing as mp
import shmobj

def worker(name):
    with shmobj.attach(name) as seg:          # подключение по имени, без копирования
        cfg = seg.root                        # FrozenDict
        return cfg["users"][1]["name"], cfg["limits"].get_many(["rps", "x"], 0)

if __name__ == "__main__":
    seg = shmobj.create({"users": [{"id": 1, "name": "ann"}, {"id": 2, "name": "bob"}],
                         "limits": {"rps": 100}})
    try:
        with mp.get_context("spawn").Pool(2) as pool:
            print(pool.map(worker, [seg.name] * 2))   # [('bob', [100, 0]), ('bob', [100, 0])]
        print(seg.root.materialize())                 # обычные dict/list
    finally:
        seg.close()
        seg.unlink()                                  # удаляет сегмент из ОС; вызывает владелец
```

- `create(obj)` сериализует дерево в новый сегмент общей памяти; `attach(name)` открывает его в
  другом процессе. `seg.name` передаётся читателям любым способом (аргумент, переменная окружения).
- `seg.root` — `FrozenDict`, `FrozenList` или скаляр. Прокси ведут себя как `Mapping`/`Sequence`
  только для чтения; срезы списков — представления без копирования.
- Пакетный доступ быстрее поэлементного: `get_many(keys, default)`, `iter_values()`, `iter_items()`.
- `materialize()` — копия в обычные `dict`/`list`.
- Без общей памяти: `shmobj.dumps(obj) -> bytes`, `shmobj.loads(buf)` поверх любого буфера
  (`bytes`, `mmap`, `memoryview`).
- Проверка данных. Без флагов `loads`/`attach` проверяют только заголовок, остальное — при каждом
  чтении: границы, типы узлов, пост-порядок. Испорченный сегмент даёт `FormatError` при обращении
  к испорченному месту, без падения процесса. `verify=True` дополнительно сверяет контрольную сумму
  всего тела (O(n), ловит случайную порчу; от намеренной подделки не защищает — сумму можно
  пересчитать). `validate(buf)` — полный обход: сумма, все узлы, UTF-8, ключи, индексы KEYTABLE;
  после него `FormatError` при чтении невозможен.
- Допустимые типы: `dict`, `list`, `tuple` (читается как список), `None`, `bool`, `int` (любой
  длины), `float`, `str`, `bytes`; ключи — скаляры. Иное — `TypeError` с путём до значения.

Правила закрытия и удаления сегмента — в [README](../README.md#жизненный-цикл-сегмента),
полная семантика — [spec/02-api.md](spec/02-api.md).
