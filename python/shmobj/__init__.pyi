"""shmobj: dict/list/скаляры в общей памяти, доступ без десериализации (RO v1).

Сегмент собирается один раз (`dumps`, `create`), читается без копирования (`loads`, `attach`):
контейнеры приходят прокси `FrozenDict`/`FrozenList`, скаляры — обычными объектами Python.
Контракт; семантика — doc/spec/02-api.md.
"""
from collections.abc import Callable, Iterable, Iterator, ItemsView, KeysView, ValuesView
from typing import Any, Final, TypeAlias, TypeVar, overload

from _typeshed import ReadableBuffer, WriteableBuffer

from . import channel as channel

FORMAT_VERSION: Final[int]  # 1

Scalar: TypeAlias = None | bool | int | float | str | bytes
Value: TypeAlias = Scalar | "FrozenList" | "FrozenDict"
Key: TypeAlias = None | bool | int | float | str | bytes

_D = TypeVar("_D")

class FormatError(ValueError):
    """Нарушение формата: неверный magic, версия, kind, смещения или контрольная сумма."""

class SegmentClosedError(ValueError):
    """Обращение к данным закрытого сегмента."""

def dumps(obj: Any, *, dedup: bool = True, checksum: bool = True) -> bytes:
    """Собрать сегмент из `obj` (dict, list, tuple, скаляры) в `bytes`.

    `dedup` — общие копии одинаковых скаляров; `checksum` — xxh3_64 тела в заголовке.
    Неподдерживаемый тип — `TypeError` с путём (`root['users'][3]`), цикл или лимиты — `ValueError`.
    """

def dumps_into(obj: Any, alloc: Callable[[int], WriteableBuffer], *, dedup: bool = True,
               checksum: bool = True) -> int:
    """Собрать сегмент и записать в начало буфера `alloc(size)`; возвращает `size`.

    Буфер — записываемый, C-непрерывный, не короче `size` (иначе `ValueError`). Ошибка сборки
    случается до вызова `alloc`.
    """

def loads(buf: ReadableBuffer, *, verify: bool = False) -> Value:
    """Корневое значение поверх буфера без копирования; `verify=True` проверяет контрольную сумму (O(n)).

    Прокси держат экспорт буфера: пока они живы, `close()` у `mmap`/`SharedMemory` даёт `BufferError`.
    """

def create(obj: Any, name: str | None = None, *, dedup: bool = True, checksum: bool = True) -> Segment:
    """Собрать `obj` в новую SharedMemory (`name=None` — имя генерируется). Создатель отвечает за `unlink()`."""

def attach(name: str, *, verify: bool = False) -> Segment:
    """Подключиться к существующему сегменту по имени; читается только заголовок."""

def validate(buf: ReadableBuffer) -> None:
    """Полная проверка формата буфера (O(n)); нарушение — `FormatError`."""

class Segment:
    """Сегмент в памяти. Прокси держат его ссылкой; после `close()` они дают `SegmentClosedError`."""
    @property
    def root(self) -> Value:
        """Корневое значение; каждое обращение — новый прокси."""
    @property
    def name(self) -> str | None:
        """Имя SharedMemory; None — сегмент поверх произвольного буфера (`loads`)."""
    @property
    def size(self) -> int:
        """Размер данных из заголовка, байт."""
    @property
    def closed(self) -> bool: ...
    def close(self) -> None:
        """Закрыть в этом процессе: буфер освобождается, SharedMemory закрывается. Идемпотентен."""
    def unlink(self) -> None:
        """Удалить именованный сегмент из ОС; подключённые процессы работают до своего `close()`.
        На Windows ничего не делает: сегмент живёт, пока открыт хоть один дескриптор."""
    def __enter__(self) -> Segment: ...
    def __exit__(self, *exc: object) -> None:
        """`close()`; `unlink()` не вызывается."""

class FrozenDict:
    """Словарь только для чтения поверх сегмента; `collections.abc.Mapping` (через register).

    Порядок — порядок вставки исходного dict. Значения-контейнеры — новые прокси, скаляры —
    объекты Python. Неизменяем, не хэшируется, не сериализуется pickle (`materialize()`).
    """
    def __getitem__(self, key: object) -> Value:
        """Значение или `KeyError`; нехэшируемый ключ — `TypeError`, как у dict."""
    @overload
    def get(self, key: object) -> Value: ...
    @overload
    def get(self, key: object, default: _D) -> Value | _D: ...
    def __contains__(self, key: object) -> bool: ...
    def __len__(self) -> int: ...
    def __iter__(self) -> Iterator[Key]: ...
    def keys(self) -> KeysView[Key]:
        """Представление ключей без копирования."""
    def values(self) -> ValuesView[Value]:
        """Представление значений без копирования."""
    def items(self) -> ItemsView[Key, Value]:
        """Представление пар без копирования."""
    @overload
    def get_many(self, keys: Iterable[object]) -> list[Value]:
        """Значения по `keys` в их порядке; первый отсутствующий ключ — `KeyError`."""
    @overload
    def get_many(self, keys: Iterable[object], default: _D) -> list[Value | _D]:
        """Значения по `keys` в их порядке; на место отсутствующего — `default`."""
    def iter_values(self) -> Iterator[Value]:
        """Итератор значений в порядке вставки."""
    def iter_items(self) -> Iterator[tuple[Key, Value]]:
        """Итератор пар (ключ, значение) в порядке вставки."""
    def materialize(self) -> dict[Key, Any]:
        """Глубокая копия в обычные dict/list: от сегмента не зависит, изменяема, сериализуется pickle.
        Обходит всё поддерево, O(размер)."""
    def __eq__(self, other: object) -> bool:
        """Равен любому Mapping с теми же парами; порядок не важен."""
    __hash__: None  # type: ignore[assignment]

class FrozenList:
    """Список только для чтения поверх сегмента; `collections.abc.Sequence` (через register).

    Срез — представление без копирования. Неизменяем, не хэшируется, не сериализуется pickle
    (`materialize()`).
    """
    @overload
    def __getitem__(self, i: int) -> Value:
        """Элемент; отрицательные индексы поддерживаются, выход за границы — `IndexError`."""
    @overload
    def __getitem__(self, s: slice) -> FrozenList:
        """Представление среза без копирования."""
    def __len__(self) -> int: ...
    def __iter__(self) -> Iterator[Value]: ...
    def __reversed__(self) -> Iterator[Value]: ...
    def __contains__(self, value: object) -> bool: ...
    def index(self, value: object, start: int = 0, stop: int = ...) -> int:
        """Индекс первого элемента, равного `value` (`==`); нет — `ValueError`."""
    def count(self, value: object) -> int:
        """Число элементов, равных `value` (`==`)."""
    @overload
    def get_many(self, indices: Iterable[int]) -> list[Value]:
        """Значения по индексам в их порядке; выход за границы — `IndexError`."""
    @overload
    def get_many(self, indices: Iterable[int], default: _D) -> list[Value | _D]:
        """Значения по индексам в их порядке; на место отсутствующего — `default`."""
    def iter_values(self) -> Iterator[Value]:
        """То же, что `iter(l)`, для симметрии с `FrozenDict`."""
    def materialize(self) -> list[Any]:
        """Глубокая копия в обычные list/dict (tuple исходных данных — list); от сегмента не зависит.
        Обходит всё поддерево, O(размер)."""
    def __eq__(self, other: object) -> bool:
        """Равен list или FrozenList с равными элементами; tuple не равен, как у list."""
    __hash__: None  # type: ignore[assignment]
