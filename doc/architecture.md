# Архитектура

## Слои

| Слой | Путь | Что делает |
|---|---|---|
| Ядро | `crates/shmobj-core` | Формат без Python: читает `&[u8]`, пишет в `Vec<u8>`. Без `unsafe`, паник, `unwrap` и индексации без проверки (`[lints]` в `Cargo.toml`); все ошибки — `Result<_, FormatError>` |
| Привязки | `crates/shmobj-py` | cdylib `shmobj._core` на PyO3: `Segment`, прокси, итераторы, сборка из Python-объектов. Весь `unsafe` здесь — `&[u8]` из `PyBuffer` |
| Пакет | `python/shmobj` | `create`/`attach` поверх `SharedMemory`, канал снимков, `latest`, `.pyi` |

`SharedMemory`, `mmap`, `memfd_create` и `socket.send_fds` уже есть в Python, поэтому жизненный цикл, канал и
`latest` написаны на нём: в Rust для этого понадобились бы `unsafe` или зависимость вроде `rustix`.

```
crates/shmobj-core/src/
  format/           # общее с будущим RW, не зависит от ro/ (rw.md §2)
    header.rs       #   заголовок, проверка kind
    slot.rs         #   слот, теги, SMALLINT/SHORTSTR
    scalar.rs       #   узлы INT64/FLOAT/BIGINT/STR/BYTES
    hash.rs         #   key_hash64 (xxh3) и нормализация чисел
    error.rs        #   FormatError с причиной и смещением
  ro/
    reader.rs       #   доступ к узлам
    list.rs, dict.rs  # LIST; DICT и KEYTABLE (LINEAR, SORTED32)
    writer.rs       #   сборка: дедупликация, KEYTABLE, типизированные списки
    validate.rs     #   полный обход, инварианты format.md §9
crates/shmobj-py/src/
  segment.rs        # Segment: PyBuffer + флаг closed
  frozen_dict.rs, frozen_list.rs, iter.rs   # прокси, представления, итераторы
  decode.rs         # слот → Python-объект
  keyarg.rs         # Python-ключ → ключ формата без аллокаций
  build.rs          # Python-объект → Writer: пути в ошибках, циклы
python/shmobj/
  __init__.py       # реэкспорт _core, регистрация в Mapping/Sequence
  _shm.py           # SharedMemory и resource_tracker
  channel.py        # канал снимков: memfd + SCM_RIGHTS (Linux), именованные снимки (Windows)
  latest.py         # последний снимок по имени
python/pydevd_plugins/extensions/pydevd_plugin_shmobj.py   # прокси в отладчике PyCharm/VS Code
```

## Решения

**Доступ к памяти.** `Segment` хранит `PyBuffer<u8>` от `SharedMemory.buf`, `mmap` или `bytes`; `&[u8]` строится на
время вызова. Контракт: опубликованный сегмент никто не меняет. Защиты от записи чужим процессом нет, но все
обращения проверяют границы — за пределы буфера чтение не выходит.

**Закрытие.** `Segment` хранит флаг `closed` и `Option<PyBuffer>`; каждый метод прокси сначала проверяет флаг
(`SegmentClosedError`). `close()` и доступ идут под GIL, гонки нет. `close()` во время обращения к данным (например,
из финализатора) откладывает освобождение буфера до конца обращения.

**Прокси.** `#[pyclass(frozen)]`: `Py<Segment>` и смещение узла. Вложенный контейнер — новый прокси при каждом
доступе, кеша нет: память читателя растёт только на затронутые страницы.

**Строки.** `PyString::from_bytes` с проверкой UTF-8; `char_count` не используется (format.md §3).

**Ключ запроса.** `str` берётся как UTF-8 без копии (`PyUnicode_AsUTF8AndSize` кеширует его в объекте), числа
нормализуются (`1`, `1.0`, `True` — один ключ), хэш xxh3 считается потоково. Сравнение с ключом таблицы — по тегу и
байтам, без Python-объектов.

**Пакетные методы.** `get_many` заполняет один `list`; `iter_values`/`iter_items` — итераторы на Rust, без
Python-кадров на шаг.

**Сборка.** Один проход в пост-порядке в `Vec<u8>`, затем `dumps_into` копирует результат в буфер вызывающего:
`create` — в `SharedMemory`, канал — в memfd. Пик памяти — около двух размеров сегмента. Дедупликация скаляров —
по (тип, байты), KEYTABLE — по последовательности ключей. Циклы — по `id` контейнеров на текущем пути; путь в
сообщении об ошибке строится только при ошибке.

**ABC.** `Mapping.register(FrozenDict)`, `Sequence.register(FrozenList)`. Миксины при регистрации не наследуются,
поэтому `keys/values/items/get/__contains__/__eq__/index/count/__reversed__` реализованы в Rust.

**Версии.** Версия пакета и `format_version` независимы; версия формата — `shmobj.FORMAT_VERSION`.

## Источники идей

Код не заимствован.

| Источник | Что взято |
|---|---|
| [sharedbuffers](https://github.com/jampp/sharedbuffers) | Отсортированный индекс хэшей с бинарным поиском, минимальная ширина однородных int-массивов, хэш `float`, совпадающий с `int`, прокси «буфер + смещение». Не взято: дедупликация по `id()` |
| [PEP 412](https://peps.python.org/pep-0412/) | Общая таблица ключей для словарей с одинаковыми ключами — KEYTABLE |
| [CPython `shared_memory.py`](https://github.com/python/cpython/blob/main/Lib/multiprocessing/shared_memory.py) | `SharedMemory` как носитель, `track` и `resource_tracker`; ошибки `ShareableList` (потеря `\0` в конце строк), которых нет в формате |
| [FlexBuffers](https://flatbuffers.dev/flexbuffers/), [rkyv](https://rkyv.org) | Бессхемный zero-copy формат с типизированными векторами; относительные смещения и проверка перед доступом |
| [orjson](https://github.com/ijl/orjson) | Быстрое создание `str` из UTF-8 — образец для будущего быстрого пути |
