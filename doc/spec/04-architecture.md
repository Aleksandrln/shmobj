# 04. Архитектура

## 1. Структура репозитория

```
shmobj/
├── Cargo.toml                    # workspace
├── pyproject.toml                # maturin, python-source = "python"
├── crates/
│   ├── shmobj-core/              # формат, без pyo3, #![forbid(unsafe_code)]
│   │   └── src/
│   │       ├── lib.rs
│   │       ├── format/           # общее для RO и будущего RW
│   │       │   ├── header.rs     # Header: parse/write, kind-диспетчеризация
│   │       │   ├── slot.rs       # Slot, Tag, кодирование SMALLINT/SHORTSTR
│   │       │   ├── scalar.rs     # чтение/запись узлов INT64/FLOAT/BIGINT/STR/BYTES
│   │       │   ├── hash.rs       # key_hash64 (xxh3) + нормализация чисел
│   │       │   └── error.rs      # FormatError с причиной и смещением
│   │       └── ro/
│   │           ├── reader.rs     # Segment<'a>(&[u8]): безопасный доступ к узлам
│   │           ├── list.rs       # ListView: subtype, len, get(i)
│   │           ├── dict.rs       # DictView, KeyTable: find(KeyRef) -> Option<usize>
│   │           ├── writer.rs     # Writer: alloc, дедуп-таблицы, write_* примитивы
│   │           └── validate.rs   # полный обход (инварианты 03-format.md §9)
│   └── shmobj-py/                # cdylib, pyo3
│       └── src/
│           ├── lib.rs            # #[pymodule] _core
│           ├── segment.rs        # PySegment: PyBuffer + closed-флаг
│           ├── frozen_dict.rs    # FrozenDict, представления keys/values/items
│           ├── frozen_list.rs    # FrozenList, срезы
│           ├── iter.rs           # итераторы iter_values / iter_items / iter keys
│           ├── decode.rs         # Slot -> PyObject (быстрые пути str/int)
│           ├── keyarg.rs         # PyAny -> KeyRef (ключ запроса без аллокаций)
│           ├── build.rs          # обход PyAny -> Writer, пути в ошибках, циклы
│           └── errors.rs         # FormatError, SegmentClosedError
├── python/shmobj/
│   ├── __init__.py               # реэкспорт _core, create/attach, регистрация в abc
│   ├── _shm.py                   # жизненный цикл SharedMemory, resource_tracker
│   ├── channel.py, channel.pyi   # канал снимков: memfd + SCM_RIGHTS (Linux), именованные снимки (Windows)
│   ├── __init__.pyi              # из skeleton/
│   └── py.typed
├── reference/                    # копия эталона из ТЗ (оракул для тестов)
├── golden/                       # копия golden из ТЗ
├── tests/                        # pytest
├── benches/                      # criterion (core) + pytest-benchmark (py)
└── fuzz/                         # cargo-fuzz: validate и случайный доступ
```

## 2. Разделение ответственности

- **shmobj-core** ничего не знает о Python. Читает `&[u8]`, пишет в `Vec<u8>`.
  Тестируется через `cargo test`, `proptest` и fuzz. Все ошибки — `Result<_, FormatError>`;
  `panic!`, `unwrap` на данных и индексация без проверки запрещены (`clippy::indexing_slicing`
  = deny в `ro/` и `format/`).
- **shmobj-py** — тонкий слой: превращает `PyAny` в вызовы `Writer`, а слоты в `PyObject`.
  Здесь сосредоточен весь `unsafe`: получение `&[u8]` из `PyBuffer` и быстрое создание `str`.
- **python/shmobj** — `SharedMemory`, канал снимков и платформенные детали жизненного цикла.
  Это проще и надёжнее сделать на Python, где уже есть `SharedMemory`, `mmap`, `memfd_create`
  и `socket.send_fds`; в Rust для этого понадобился бы `unsafe` или зависимость вроде `rustix`.

## 3. Ключевые решения

**Доступ к памяти.** `Segment` (Rust) хранит `PyBuffer<u8>` от `SharedMemory.buf`, `mmap`
или `bytes`. Указатель и длина берутся один раз; `&[u8]` строится на время вызова.
Допущение безопасности: после публикации RO-сегмент никто не меняет. Это записано в
документации как контракт. Защита от одновременной записи злонамеренного процесса в
задачу v1 не входит; при этом граничные проверки сохраняются, так что даже в этом
случае не будет чтения за пределами буфера.

**Закрытие.** `PySegment` хранит флаг `closed` и `Option<PyBuffer>`. Каждый метод
прокси сначала проверяет флаг и вызывает `SegmentClosedError`. `close()` выставляет
флаг, отпускает `PyBuffer` и закрывает `SharedMemory`. В GIL-сборках гонки нет: и
`close`, и доступ выполняются под GIL. Для free-threaded в будущем это заменяется на
`RwLock` без смены API.

**Прокси.** `#[pyclass(frozen)]`. Поля: `Py<PySegment>` и смещение узла. Длину и
подтип можно хранить в прокси, чтобы не перечитывать заголовок. При каждом доступе
создаётся новый прокси вложенного контейнера; кэша нет (требование N5).

**Декодирование `str`.**
- ASCII-строки создаются через `PyUnicode_New(n, 127)` и `memcpy`: STR с `byte_len == char_count` и SHORTSTR, у которого все байты < 0x80.
- Остальные — через `PyUnicode_DecodeUTF8`; UTF-8 при сборке уже проверен, но повторная проверка CPython допустима.
- Образец быстрого пути — orjson (см. `06-references.md`).

**Ключ запроса (`keyarg.rs`).**
- `str` → UTF-8 через `PyUnicode_AsUTF8AndSize`: без копии, CPython кэширует результат.
- `int` → i64 или байты.
- `float` → целое или биты; `bool` → int.
- Хэш считается потоково (`Xxh3::update(type_byte)`, затем `update(payload)`), без аллокаций.
- Сравнение с ключом в таблице идёт по тегу и байтам, без создания Python-объектов.

**Поиск в SORTED32.**
- lower_bound по `h32` в `index`.
- Стартовую точку можно угадать интерполяцией, `hint = (h32 * len) >> 32`, как в sharedbuffers; на равномерных хэшах это почти одно попадание.
- Затем линейно идут записи с тем же `h32`.

**Пакетные методы.**
- `get_many` принимает любой iterable; для `list`/`tuple` есть быстрый путь через `PySequence_Fast`.
- Результат — `PyList` нужной длины, заполняемый на месте.
- `iter_values`/`iter_items` — `#[pyclass]`-итераторы с индексом; `__next__` декодирует один слот.
  На каждом шаге нет Python-кадров и проверки флага закрытия в Python-коде.

**Сборка (`build.rs`).**
- Один проход в пост-порядке в `Vec<u8>`; затем `dumps_into` копирует результат в буфер нужного размера от вызывающего: `create()` — в `SharedMemory`, канал — в memfd. Промежуточных `bytes` нет.
- Пиковая память при сборке примерно равна 2 × размеру сегмента. Двухпроходная сборка сразу в целевой буфер — оптимизация на потом.
- Дедупликация: `HashMap<ScalarKey, u64>` по (тип, байты значения), отдельная таблица для KEYTABLE по последовательности (тип, ключ).
- Циклы обнаруживаются по множеству `id` контейнеров на текущем пути.
- Путь в сообщении об ошибке строится только при ошибке: стек индексов и ключей.

**Регистрация в ABC.** В `__init__.py`: `Mapping.register(FrozenDict)`,
`Sequence.register(FrozenList)`. Миксины ABC при регистрации не наследуются, поэтому
`keys/values/items/get/__contains__/__eq__/index/count/__reversed__` реализуются в Rust.

## 4. Сборка и выпуск

- `maturin`, `abi3-py311`, смешанный layout (Rust-модуль `shmobj._core` + Python-пакет `shmobj`).
- CI (`.github/workflows/ci.yml`):
  - `cargo fmt` и `clippy -D warnings`, `cargo test`;
  - pytest на 3.11 и последнем CPython под Linux, Windows и macOS;
  - сборка wheel через `maturin-action`;
  - короткий fuzz (60 с) на каждый PR, долгий — по ночам.
- Версия пакета и `format_version` независимы. В `shmobj.FORMAT_VERSION` публикуется версия формата.
