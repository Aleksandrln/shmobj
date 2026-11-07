# shmobj

Дерево из `dict`, `list` и скаляров в одном сегменте общей памяти в компактном
бинарном формате. Процессы подключаются к сегменту по имени и читают данные через
прокси (`FrozenDict`, `FrozenList`) без десериализации и без копии в своей памяти.
Версия 1 — только чтение (RO). Ядро на Rust, привязки через PyO3, CPython ≥ 3.11 (abi3).

```python
import shmobj

seg = shmobj.create({"users": [{"id": 1, "name": "ann"}]})   # процесс-владелец
name = seg.name

with shmobj.attach(name) as s:                                # другой процесс
    users = s.root["users"]                                   # FrozenList
    users[0].get_many(["id", "name"])                         # [1, 'ann']

seg.close()
seg.unlink()
```

## Зачем

Несколько процессов (воркеры gunicorn/uvicorn, `multiprocessing`, планировщик и веб) читают одни и те
же большие справочные данные: конфиг, каталог, снимок метрик. Обычно каждый держит свою копию —
распакованный `pickle`/JSON в куче, — и память растёт с числом процессов, а `fork` не спасает:
счётчики ссылок CPython пишут в каждый объект, и страницы копируются.

`shmobj` кладёт данные в общую память один раз. Читатель подключается за десятки микросекунд, в его
куче появляются только объекты, к которым он обратился, и только пока он их держит.

## Сколько памяти экономит

Набор из ТЗ (`doc/spec/07-testing.md` §5): `{f"user{i}": {"id", "name", "score", "tags": [...], "ok", "x"}}`.
Linux x86_64, Python 3.13, release.

| Записей | Python-объекты | Сегмент | Читатель: `attach` + доступ | Читатель: `pickle.loads` |
|---|---|---|---|---|
| 10 000 | 3.6 МБ | 1.5 МБ (41%) | +0 МБ, 19 мкс | +6.3 МБ, 10 мс |
| 1 000 000 | 373 МБ | 168 МБ (45%) | +0 МБ, 51 мкс | +628 МБ, 1.5 с |

«Читатель» — прирост приватной памяти процесса (`Private_*` в `/proc/self/smaps_rollup`). Страницы
сегмента общие: ОС держит их одну копию на все процессы.

Пример: 8 воркеров с набором на миллион записей — около 5 ГБ, если каждый распаковывает `pickle`, и
около 170 МБ на всех с `shmobj`. Сегмент примерно вдвое меньше самих Python-объектов.

Цена — скорость доступа: обращение по ключу или индексу в 3–6 раз медленнее, чем к `dict`/`list`
([doc/status.md](doc/status.md#базовые-замеры-release-linux-x86_64-python-313-эталонный-набор-spec07-testingmd-5)).
Для горячих циклов — пакетные `get_many`, `iter_values`, `iter_items` или `materialize()` нужной ветки.

## Установка

Python ≥ 3.11 (CPython, abi3 — один wheel на все версии). Готовые wheels — артефакты `wheel-*`
задачи `wheels` в CI (Linux x86_64/aarch64, Windows x64, macOS arm64):

```bash
pip install shmobj-<версия>-cp311-abi3-<платформа>.whl
```

Из исходников (нужен Rust stable):

```bash
pip install git+https://github.com/Aleksandrln/shmobj
```

Сборка wheels — [doc/usage.md](doc/usage.md#сборка).

## Использование

Данные — `dict`, `list`, `tuple`, `None`, `bool`, `int` любой длины, `float`, `str`, `bytes`; ключи —
скаляры. Только чтение: изменить опубликованное нельзя, новые данные — новый сегмент.

**Общая память по имени** — пример выше. Имя сегмента передаётся читателям любым способом (аргумент,
переменная окружения). Прокси ведут себя как `Mapping`/`Sequence`: `[]`, `get`, `in`, `len`,
итерация, срезы списков без копирования; `materialize()` — копия в обычные `dict`/`list`.

**Без общей памяти** — тот же формат в любом буфере:

```python
blob = shmobj.dumps(data)                    # bytes: в файл, кэш, по сети
root = shmobj.loads(blob)                    # FrozenDict поверх bytes, без разбора

with open("data.bin", "rb") as f:            # файл, отображённый в память:
    m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    root = shmobj.loads(m)                   # процессы делят страницы файла
```

`dumps_into(obj, alloc)` пишет сразу в буфер `alloc(size)`, без промежуточных `bytes`.

**Канал снимков** (Linux, Windows): писатель периодически публикует данные целиком, читатель
держит последний снимок. После падения любой из сторон в ОС ничего не остаётся.

```python
pub = shmobj.channel.Publisher(sock)          # процесс-писатель
pub.publish({"vms": metrics})

sub = shmobj.channel.Subscriber(other_sock)   # процесс-читатель
sub.attach(on_snapshot)                       # в цикле asyncio; sub.latest — последний корень
```

Пара сокетов и запуск дочернего процесса — [doc/usage.md](doc/usage.md#канал-снимков-linux-windows).

**Проверка данных.** Испорченный сегмент даёт `FormatError` при обращении к испорченному месту, без
падения процесса. `attach(name, verify=True)` сверяет контрольную сумму, `validate(buf)` проверяет
всё целиком.

**Отладчик.** PyCharm и VS Code показывают содержимое прокси как обычных контейнеров — плагин pydevd
ставится вместе с пакетом.

## Жизненный цикл сегмента

- Прокси держат сильную ссылку на `Segment`: буфер не освобождается, пока жив хотя бы один прокси.
- `close()` закрывает сегмент в этом процессе; обращение через старые прокси даёт `SegmentClosedError`.
  `with` вызывает `close()`, но не `unlink()`.
- `unlink()` удаляет сегмент из ОС; вызывает владелец данных. Подключённые процессы работают до своего `close()`.
- `attach()` не регистрирует сегмент в `resource_tracker`: выход читателя не удаляет чужой сегмент.
- Windows: сегмент живёт, пока открыт хотя бы один дескриптор; `unlink()` ничего не делает.
- `loads(buf)` удерживает экспорт буфера: пока живы прокси, `mmap.close()` даёт `BufferError`.

## Сборка wheels

Из корня репозитория, на Linux; нужны Rust stable и Python ≥ 3.11. Подробности — [doc/usage.md](doc/usage.md#сборка).

1. Версия — `version` в `crates/shmobj-py/Cargo.toml` и `crates/shmobj-core/Cargo.toml`, затем
   `cargo update -w`. Wheel с прежней версией pip без `--force-reinstall` не переставит.
2. Проверки — список в [doc/dev.md](doc/dev.md#проверки-то-же-что-в-ci).
3. Один раз — инструменты:
   ```bash
   pip install maturin ziglang
   rustup target add x86_64-pc-windows-gnu
   apt-get install gcc-mingw-w64-x86-64
   ```
4. Linux x86_64, glibc ≥ 2.17:
   ```bash
   maturin build --release --zig --compatibility manylinux2014 --out dist
   ```
5. Windows x64:
   ```bash
   maturin build --release --strip --target x86_64-pc-windows-gnu --out dist
   ```
6. В `dist/` — `shmobj-<версия>-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` и
   `shmobj-<версия>-cp311-abi3-win_amd64.whl`, один wheel на все CPython ≥ 3.11. В каждом —
   `shmobj/` и `pydevd_plugins/extensions/pydevd_plugin_shmobj.py` (`unzip -l`).

На Windows wheel для неё же — `maturin build --release --out dist`. Wheels для всех платформ
собирает задача `wheels` в CI (артефакты `wheel-*`).

## Документация

- [doc/usage.md](doc/usage.md) — устройство пакета, сборка, использование, канал снимков.
- [doc/status.md](doc/status.md) — что реализовано, отклонения от ТЗ, замеры, следующие шаги.
- [doc/dev.md](doc/dev.md) — окружение, тесты, конфигурация репозитория.
- [doc/spec/](doc/spec/00-overview.md) — ТЗ: требования, API, формат, архитектура, тестирование.
- `reference/` — эталонная реализация формата на Python, `golden/` — эталонные сегменты.
