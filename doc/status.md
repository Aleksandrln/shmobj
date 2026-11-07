# Состояние реализации

Этапы — по [spec/08-roadmap.md](spec/08-roadmap.md).

| # | Этап | Состояние |
|---|---|---|
| 0 | Каркас | Готово: workspace, maturin, CI (fmt, clippy, `cargo test`, pytest на 3 ОС × 3.11/3.13, wheels). Нет fuzz |
| 1 | Ядро: чтение | Готово: заголовок, слоты, скаляры, LIST (все подтипы), DICT/KEYTABLE (LINEAR, SORTED32), `validate`. Нет fuzz-целей и proptest |
| 2 | Привязки: чтение | Готово функционально: `Segment`, `FrozenDict`, `FrozenList`, срезы, `keys/values/items`, `get_many`, `iter_*`, `materialize`, `==`, `repr`, отказ `hash`/`pickle`, `loads`/`validate` |
| 3 | Сборка | Готово: `Writer` + обход Python-объектов, `dumps_into` (сборка в буфер вызывающего; через него `create` и канал), дедупликация, общие KEYTABLE, типизированные списки, пути в ошибках, циклы. Байты совпадают с эталоном |
| 4 | Жизненный цикл | Готово базово: `create`/`attach`/`close`/`unlink`, `resource_tracker` 3.11–3.12, `track=False` 3.13+. На Windows/macOS не проверялось локально (только CI) |
| 4a | Канал снимков | Готово: `shmobj.channel` (`02-api.md` §6): Linux — memfd, Windows — именованные снимки с подтверждением (проверяется в CI); на macOS `SUPPORTED = False` |
| 5 | Производительность | Не начато |
| 6 | Выпуск | Не начато |

## Проверено тестами

- Все `golden/*.bin` проходят `validate`, читаются в `golden/cases.py`, и `dumps` даёт побайтово те же файлы.
- hypothesis: `dumps(v) == ref.build(v)`, взаимное чтение, `validate` на результате.
- Повреждённые сегменты: вердикт `validate` совпадает с эталоном (5000 вариантов); в Rust все однобайтовые изменения `records.bin` без паник.
- Python 3.11 и 3.13 на Linux, межпроцессное чтение через `spawn` и `fork`, отсутствие предупреждений `resource_tracker`.
- Канал (`tests/test_channel.py`): последний из очереди, закрытие пропущенных и освобождение прежних снимков (`/proc/self/maps`, `/proc/self/fd`), печати, отказ от незапечатанного memfd, пропуск при полной очереди, конец канала с обеих сторон, asyncio, писатель в дочернем процессе. Протокол Windows (`_NamedPublisher`/`_NamedSubscriber`) гоняется и на Linux поверх `/dev/shm`: имя снимка удаляется при подтверждении, поэтому освобождение там видно по `/proc/self/maps`.
- Примеры `examples/channel_sync.py` и `examples/channel_async.py` (`tests/test_examples.py`): писатель в дочернем процессе, выход писателя после закрытия канала читателем.

## Решения и отклонения

- **Исправлен эталон** (`reference/shmobj_ref.py`, `validate`): ключ-контейнер или NaN в KEYTABLE давал `TypeError` вместо `FormatError`, а слоты ключей проверялись относительно смещения DICT, а не KEYTABLE. Правило записано в `spec/03-format.md` §6. Golden не изменились.
- Дедупликация KEYTABLE, как в эталоне, считает `0.0` и `-0.0` одним ключом.
- Вложенность контейнеров при сборке и `materialize` ограничена `MAX_DEPTH = 1000` (`ValueError`); `validate` итеративный и глубину не ограничивает.
- `keys()/values()/items()` возвращают `collections.abc.KeysView/ValuesView/ItemsView` поверх прокси.
- Строки декодируются через `PyUnicode_FromStringAndSize` (`PyString::from_bytes`) с проверкой UTF-8; `char_count` не используется и не проверяется `validate` (как в эталоне). Быстрый ASCII-путь из `spec/04-architecture.md` требует доверять `char_count` — при его добавлении нужна проверка в `validate` или при декодировании.
- `close()` во время обращения к данным (например, из финализатора) откладывает освобождение буфера до конца обращения.
- Канал на Python, не в Rust: только системные вызовы и asyncio, а `shmobj-core` без `unsafe`. Константы печатей заданы числами (`linux/fcntl.h`): в `fcntl` их может не быть.
- Канал на Windows — по имени, а не `DuplicateHandle`: объект ядра Windows живёт, пока открыт handle, поэтому имя после падения не остаётся, а `attach` уже есть. Подтверждение нужно, потому что без handle писателя снимок исчезает до открытия читателем. `SharedMemory` на Windows открывает имя в два шага (`OpenFileMapping`, затем `mmap` с `tagname`); исчезни снимок между ними — `mmap` создаст пустой, и `attach` даст `FormatError`.
- Внутренняя функция `shmobj._core.open_segment(buf, *, name, on_close, on_unlink, verify)` создаёт `Segment`; `create`/`attach` в `python/shmobj/_shm.py` передают в неё колбэки `SharedMemory`.

## Базовые замеры (release, Linux x86_64, Python 3.13, эталонный набор `spec/07-testing.md` §5)

| Метрика | Сейчас | Цель |
|---|---|---|
| Размер / Python-объекты | 41% (1.57 МБ, как у эталона) | ≤ 45% |
| `fd[k]`, словарь ≤ 8 ключей | 4.7× `dict` | ≤ 3× |
| `fd[k]`, 10 000 ключей | 6.3× | ≤ 3× |
| `fl[i]` | 3.2× `list` | ≤ 3× |
| `get_many`, на ключ | 9.0× | ≤ 2× |
| `iter_values`, на элемент | 12.8× | ≤ 2.5× |
| `dumps` | 4.0× `pickle.dumps` | ≤ 3× |

## Следующие шаги

1. fuzz-цели (`validate`, случайный доступ) и возврат `fuzz-smoke` в CI; proptest для круговой сборки в ядре.
2. Производительность: прокси без повторного разбора заголовка узла на каждом доступе, быстрые пути `str`/`int`, интерполяционный поиск в SORTED32, `get_many` без промежуточных прокси, итераторы без `with_seg` на шаг, дедупликация без аллокаций.
3. Тесты утечек (10⁶ операций), бенчмарки `tests/bench_*.py` и `benches/`, полный перебор повреждений golden (≥ 10⁵).
