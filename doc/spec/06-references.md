# 06. Внешние пакеты и источники

Версии проверены на 2026-11-03. В зависимостях фиксируйте актуальные минорные версии.

## 1. Зависимости проекта

| Пакет | Ссылка | Что берём |
|---|---|---|
| PyO3 (0.30) | https://github.com/PyO3/pyo3, руководство https://pyo3.rs | `#[pyclass(frozen)]`, `#[pymethods]` с протоколами `__getitem__`/`__len__`/`__iter__`/`__contains__`/`__eq__`; `pyo3::buffer::PyBuffer<u8>` для доступа к `SharedMemory.buf`; Bound API; `create_exception!` для `FormatError`/`SegmentClosedError`; feature `abi3-py311`. Разделы руководства: «Class customizations», «Buffer protocol», «Free-threading» (на будущее) |
| maturin (1.16) | https://github.com/PyO3/maturin, https://www.maturin.rs | Смешанный проект Rust + Python (`python-source`, `module-name = "shmobj._core"`), `maturin develop`, сборка abi3-wheel |
| maturin-action | https://github.com/PyO3/maturin-action | Сборка wheel в CI: manylinux x86_64/aarch64, Windows, macOS |
| xxhash-rust (0.8) | https://github.com/DoumanAsh/xxhash-rust | feature `xxh3`: `xxh3_64` и потоковый `Xxh3` для хэша ключа без аллокаций |
| python-xxhash (4.x) | https://pypi.org/project/xxhash/ | `xxh3_64_intdigest` в эталоне и тестах; это тот же алгоритм, что в Rust |
| zerocopy (0.8), опционально | https://github.com/google/zerocopy | `byteorder::little_endian::{U16,U32,U64}` и `FromBytes`/`IntoBytes` для `Header` без `unsafe`. Допустимая замена — ручной `u64::from_le_bytes` |

## 2. Инструменты тестирования

| Пакет | Ссылка | Что берём |
|---|---|---|
| hypothesis | https://hypothesis.readthedocs.io | `st.recursive` для случайных вложенных структур; сверка Rust ⇄ эталон |
| pytest-benchmark | https://pypi.org/project/pytest-benchmark/ | Python-бенчмарки против `dict`/`list` (`07-testing.md` §5) |
| criterion | https://github.com/criterion-rs/criterion.rs | Бенчмарки ядра: поиск в KEYTABLE, декодирование слотов |
| proptest | https://github.com/proptest-rs/proptest | Круговое преобразование в `shmobj-core` на собственном enum значений |
| cargo-fuzz / libfuzzer-sys | https://github.com/rust-fuzz/cargo-fuzz | Цели fuzz: `validate(&[u8])` и случайный доступ без `validate`; требование — ни одной паники |

## 3. Источники идей (код не копируется)

| Источник | Ссылка | Что берём | Что не берём |
|---|---|---|---|
| sharedbuffers (BSD-3) | https://github.com/jampp/sharedbuffers, файл `sharedbuffers/mapped_struct.py` | Отсортированный индекс хэшей с бинарным поиском и интерполяционной подсказкой (`ObjectIdMapper._search_hkey`); выбор минимальной ширины для однородных int-массивов (`mapped_tuple.pack_into`); стабильный хэш с разбором `float`, совпадающим с `int` (`_stable_hash`); прокси «буфер + смещение» (`proxied_dict`, `proxied_list`) | Дедупликацию по `id()` (в README сами предупреждают о тихой порче данных); зависимость от numpy; материализацию вложенных tuple при доступе |
| CPython `shared_memory.py` | https://github.com/python/cpython/blob/main/Lib/multiprocessing/shared_memory.py, документация https://docs.python.org/3/library/multiprocessing.shared_memory.html | `SharedMemory` как носитель; параметр `track` (3.13+) и поведение `resource_tracker` в 3.11–3.12; схема заголовка `ShareableList` (число элементов + таблица смещений) | Формат `ShareableList`: 9 байт метаданных на элемент, потеря завершающих `\0` в строках из-за `rstrip`, фиксированный размер строки |
| PEP 412 (key-sharing dicts) | https://peps.python.org/pep-0412/ | Идея общей таблицы ключей для словарей с одинаковым набором ключей, отсюда KEYTABLE | — |
| CPython `dictobject.c` | https://github.com/python/cpython/blob/main/Objects/dictobject.c | Для RW: компактная хэш-таблица `indices` + `entries`, порядок вставки | В RO не используется |
| orjson | https://github.com/ijl/orjson | Быстрое создание `str` из UTF-8 через `PyUnicode_New` с определением max char; приёмы работы с FFI CPython в PyO3-проекте | Формат JSON |
| FlexBuffers | https://flatbuffers.dev/flexbuffers/, крейт https://crates.io/crates/flexbuffers | Подтверждение подхода: бессхемный zero-copy формат с типизированными векторами и автоширинами. На тестовых данных 1.3 МБ против 3.8 МБ у Python-объектов | Сам формат: ключи только строки, нет задела под запись. Python-реализация не принимает `memoryview` и тратит около 880 мкс на поиск |
| rkyv | https://rkyv.org | Относительные смещения и подход к валидации перед доступом (bytecheck) | Сам крейт: рассчитан на статические Rust-типы, а не на динамические Python-значения |
| memmap2 | https://github.com/RazrFalcon/memmap2-rs | В v1 только в тестах (mmap файла с golden). В RW — открытие сегмента в Rust | — |
| windows-sys | https://github.com/microsoft/windows-rs | В RW: `CreateFileMappingW`/`OpenFileMappingW` | — |
| shared_memory (крейт) | https://github.com/elast0ny/shared_memory-rs | Только как пример кроссплатформенного открытия именованной памяти | Как зависимость: последний релиз 0.12.4 вышел в 2022 году |

## 4. Базовые решения для сравнения в бенчмарках

| Пакет | Ссылка | Зачем |
|---|---|---|
| InterProcessPyObjects | https://github.com/FI-Mihej/InterProcessPyObjects | Ближайший аналог с настоящим общим хранением; сравнение скорости чтения и памяти. Для RW — идеи синхронизации |
| UltraDict2 | https://pypi.org/project/UltraDict2/ | Показывает, что потоковая синхронизация не экономит память: у каждого процесса своя копия |
| `pickle` | stdlib | Нижняя граница размера (0.63 МБ на эталонном наборе) и скорость сборки |
