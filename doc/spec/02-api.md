# 02. Python API

Контракт с типами: `python/shmobj/__init__.pyi`.

## 1. Модуль

```python
import shmobj

seg = shmobj.create(data)                 # новая SharedMemory, имя генерируется
seg.name                                  # передать в другие процессы

# другой процесс
with shmobj.attach(name) as seg:
    users = seg.root["users"]             # FrozenList
    names = users[0].get_many(["id", "name"])
    total = sum(u["score"] for u in users.iter_values())
```

| Функция | Описание |
|---|---|
| `dumps(obj, *, dedup=True, checksum=True) -> bytes` | Собрать сегмент в `bytes` |
| `loads(buf, *, verify=False) -> Value` | Корень поверх буфера без копирования. `verify=True` дополнительно проверяет контрольную сумму, O(n) |
| `create(obj, name=None, *, dedup=True, checksum=True) -> Segment` | `dumps` и запись в новую `SharedMemory`. Процесс-создатель становится владельцем сегмента |
| `attach(name, *, verify=False) -> Segment` | Подключение к существующему сегменту |
| `validate(buf) -> None` | Полная проверка формата, иначе `FormatError` |

`Value = None | bool | int | float | str | bytes | FrozenList | FrozenDict`

Параметры сборки:
- `dedup` — дедупликация одинаковых скаляров. Таблицы ключей дедуплицируются всегда.
- `checksum` — записать в заголовок xxh3_64 тела сегмента.

## 2. `Segment`

| Член | Описание |
|---|---|
| `root: Value` | Корневое значение; при каждом обращении создаётся новый прокси |
| `name: str \| None` | Имя `SharedMemory`; `None` для `loads` поверх произвольного буфера |
| `size: int` | `data_size` из заголовка |
| `close()` | Помечает сегмент закрытым, освобождает буфер и закрывает `SharedMemory` в этом процессе. Идемпотентен |
| `unlink()` | Удаляет именованный сегмент из ОС (на Windows ничего не делает, см. ниже) |
| `closed: bool` | |
| `__enter__` / `__exit__` | `close()` на выходе; `unlink()` не вызывается |

Жизненный цикл:
- Прокси держат сильную ссылку на `Segment`, поэтому сегмент не освобождается, пока жив хотя бы один прокси.
- После `close()` любое обращение через старые прокси даёт `SegmentClosedError`.
- `unlink()` вызывает тот, кто решает, что данные больше не нужны: обычно создатель.
  Процессы, которые уже подключились, продолжают работать до своего `close()`.
- Windows: сегмент существует, пока открыт хотя бы один дескриптор; `unlink()` ничего не делает.
- 3.11–3.12: `attach` снимает регистрацию сегмента в `resource_tracker`,
  в 3.13+ используется `SharedMemory(track=False)`.
- `loads(buf)` удерживает экспорт буфера (`PyBuffer`). Пока живы прокси, `mmap.close()`
  или `SharedMemory.close()` у исходного объекта вызовут `BufferError`. Это штатное
  поведение buffer protocol.

## 3. `FrozenDict` — `collections.abc.Mapping`

| Метод | Семантика |
|---|---|
| `d[key]` | Значение или `KeyError`. Для нехэшируемого ключа — `TypeError`, как у `dict`. Для хэшируемого, но неподдерживаемого типа (например, `tuple`) — `KeyError` |
| `get(key, default=None)`, `key in d`, `len(d)` | Как у `dict` |
| `iter(d)`, `keys()`, `values()`, `items()` | Порядок вставки исходного `dict`. `keys()`/`values()`/`items()` возвращают представления (`KeysView`/`ValuesView`/`ItemsView`-совместимые) без копирования |
| `get_many(keys, default=<нет>) -> list` | Значения для итерируемого `keys` в его порядке. Без `default` первый отсутствующий ключ даёт `KeyError`; с `default` он подставляется |
| `iter_values() -> Iterator[Value]` | Итератор значений в порядке вставки |
| `iter_items() -> Iterator[tuple[Value, Value]]` | Итератор пар |
| `materialize() -> dict` | Глубокая копия в обычные объекты |
| `==` | Равен любому `Mapping` с теми же парами; порядок не важен, как у `dict` |
| `hash()` | `TypeError` (`__hash__ = None`) |
| `repr()` | `<FrozenDict len=N>`, без обхода данных |
| `pickle` | v1: `TypeError` с подсказкой использовать `materialize()` или передать имя сегмента |

## 4. `FrozenList` — `collections.abc.Sequence`

| Метод | Семантика |
|---|---|
| `l[i]` | Отрицательные индексы поддерживаются; `IndexError` |
| `l[a:b:c]` | `FrozenList`-представление без копирования |
| `len`, `iter`, `in`, `index`, `count`, `reversed` | Как у `list` (`index`/`count` по `==`) |
| `get_many(indices, default=<нет>) -> list` | Значения по индексам. Без `default` выход за границы даёт `IndexError` |
| `iter_values() -> Iterator[Value]` | То же, что `iter(l)`, явным методом для симметрии с `FrozenDict` |
| `materialize() -> list` | Глубокая копия; `tuple` из исходных данных возвращается как `list` |
| `==` | Равен `list` или `FrozenList` с равными элементами; `tuple` не равен (как у `list`) |
| `hash`, `repr`, `pickle` | Как у `FrozenDict` |

Типизированные списки (однородные `int`/`float`/`bool`, см. формат) возвращают
`int`/`float`/`bool`, и снаружи ничем не отличаются от обычных.

## 5. Исключения

| Исключение | База | Когда |
|---|---|---|
| `FormatError` | `ValueError` | Нарушение формата, неверный magic, версия или kind |
| `SegmentClosedError` | `ValueError` | Обращение к закрытому сегменту |
| `TypeError` | — | Неподдерживаемый тип при сборке; в сообщении путь, например `root['users'][3]['tags']` |
| `ValueError` | — | Цикл в данных или превышение лимитов |

## 6. Зарезервированные имена (RW, не реализуются в v1)

`SharedDict`, `SharedList`, `create_rw`, `attach_rw`, `freeze`, `thaw`, модуль `shmobj.abc`.
