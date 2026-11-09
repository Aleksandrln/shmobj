"""Последний снимок по имени: писатель кладёт (`Writer.put`), читатели забирают, когда нужно (`Reader.get`).

Снимки — именованные сегменты `<name>.<gen>` (`create`/`attach`), управляющий сегмент `<name>` хранит
поколение управляющего сегмента (`epoch`), номер текущего снимка (`gen`) и состояние писателя.
Сокетов нет: читатели подключаются по имени в любом порядке и переживают перезапуск писателя.
Работает везде, где есть SharedMemory, включая macOS.

`put`: новый снимок, его номер — в управляющий сегмент, прежний удаляется (POSIX — `unlink`,
Windows — закрывается handle). Открытые отображения удаление не трогает: читатель дочитывает свой.
`get`: (`epoch`, `gen`) не сменились — прежний корень; сменились — `attach`. Снимка уже нет
(писатель ушёл дальше) — номер перечитывается.

Имена не регистрируются в resource_tracker: его уборка при выходе писателя оставила бы читателей
на удалённом управляющем сегменте. Писатель удаляет свои имена в `close()` и при выходе
интерпретатора. После падения на POSIX остаются управляющий сегмент и последний снимок: следующий
писатель с тем же именем продолжает нумерацию и удаляет снимок при первом `put`. Windows: объект
живёт, пока открыт хоть один handle, после падения ничего не остаётся. Семантика — doc/spec/02-api.md §7.
"""
from __future__ import annotations

import os
import struct
import weakref
from typing import Any

from . import _shm
from ._core import FormatError

_MAGIC = b"SHMOLAT1"
_CONTROL = struct.Struct("<8sQQQQ")  # magic, epoch, gen, state, check
_CONTROL_SIZE = 64
_OPEN, _CLOSED = 1, 2
_MASK = (1 << 64) - 1
_CHECK_SALT = 0x5348_4D4F_4C41_5431
# попыток на столкновение имени снимка (`put`) и на гонку с удалением снимка (`get`)
_ATTEMPTS = 8


def _snapshot_name(name: str, gen: int) -> str:
    return f"{name}.{gen:x}"


def _check(epoch: int, gen: int, state: int) -> int:
    return (epoch ^ gen ^ (state << 56) ^ _CHECK_SALT) & _MASK


def _write(buf: memoryview, epoch: int, gen: int, state: int) -> None:
    _CONTROL.pack_into(buf, 0, _MAGIC, epoch, gen, state, _check(epoch, gen, state))


def _read(buf: memoryview) -> tuple[int, int, int] | None:
    """(epoch, gen, state); None — сегмент пустой, чужой или прочитан посреди записи."""
    magic, epoch, gen, state, check = _CONTROL.unpack_from(buf)
    if magic != _MAGIC or check != _check(epoch, gen, state):
        return None
    return epoch, gen, state


def _open_control(name: str):
    """Управляющий сегмент писателя и его прежнее содержимое (None — новый или пустой)."""
    try:
        return _shm.open_shm(name, create=True, size=_CONTROL_SIZE), None
    except FileExistsError:  # после падения писателя (POSIX) или его держат читатели (Windows)
        shm = _shm.open_shm(name)
    found = _read(shm.buf) if shm.size >= _CONTROL.size else None
    if found is None and (shm.size < _CONTROL.size or any(shm.buf[: _CONTROL.size])):
        shm.close()
        raise ValueError(f"shmobj.latest: имя {name!r} занято сегментом другого формата")
    return shm, found


class _WriterState:
    """Ресурсы писателя отдельно от него: `weakref.finalize` закрывает их и при выходе интерпретатора."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.control, found = _open_control(name)
        self.current = None  # Segment последнего снимка
        self.stale = None  # имя снимка прежнего писателя: удаляется при первом put
        if found is None:
            self.epoch, self.gen = int.from_bytes(os.urandom(8), "little"), 0
        else:
            self.epoch, self.gen, _ = found
            if self.gen:
                self.stale = _snapshot_name(name, self.gen)
        _write(self.control.buf, self.epoch, self.gen, _OPEN)

    def put(self, obj: Any, dedup: bool, checksum: bool) -> None:
        gen = self.gen
        for _ in range(_ATTEMPTS):
            gen += 1
            name = _snapshot_name(self.name, gen)
            try:
                seg = _shm.create(obj, name, dedup=dedup, checksum=checksum, track=False)
                break
            except FileExistsError:  # остался от упавшего писателя (POSIX) или его держит читатель (Windows)
                _shm.unlink_name(name)
        else:
            raise FileExistsError(f"shmobj.latest: имена снимков {self.name!r} заняты")
        self.gen = gen
        _write(self.control.buf, self.epoch, gen, _OPEN)
        previous, self.current = self.current, seg
        if previous is not None:
            _shm.unlink_name(previous.name)
            previous.close()
        if self.stale is not None:
            _shm.unlink_name(self.stale)
            self.stale = None

    def close(self) -> None:
        control, self.control = self.control, None
        if control is None:
            return
        try:
            _write(control.buf, self.epoch, self.gen, _CLOSED)
        finally:
            _shm.unlink_name(self.name)
            if self.current is not None:
                _shm.unlink_name(self.current.name)
                self.current.close()
                self.current = None
            if self.stale is not None:
                _shm.unlink_name(self.stale)
            control.close()


class Writer:
    """Писатель снимков под именем `name`; один на имя. Свои имена удаляет в `close()` и при выходе."""

    def __init__(self, name: str) -> None:
        self._state = _WriterState(name)
        self._finalizer = weakref.finalize(self, self._state.close)

    @property
    def name(self) -> str:
        return self._state.name

    @property
    def closed(self) -> bool:
        return self._state.control is None

    def put(self, obj: Any, *, dedup: bool = True, checksum: bool = True) -> None:
        """Собрать `obj` в новый снимок и сделать его последним; прежний удаляется."""
        if self.closed:
            raise ValueError("shmobj.latest: писатель закрыт")
        self._state.put(obj, dedup, checksum)

    def close(self) -> None:
        """Отметить закрытие для читателей и удалить свои имена; читатели дочитывают открытое. Идемпотентен."""
        self._finalizer()

    def __enter__(self) -> Writer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Reader:
    """Читатель снимков под именем `name`. Писателя может ещё не быть: `get()` вернёт None."""

    def __init__(self, name: str, *, verify: bool = False) -> None:
        self._name = name
        self._verify = verify
        self._control = None  # SharedMemory управляющего сегмента
        self._version = None  # (epoch, gen) снимка в _root
        self._root = None
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def closed(self) -> bool:
        return self._closed

    def _open_control(self) -> bool:
        try:
            shm = _shm.open_shm(self._name)
        except FileNotFoundError:
            return False
        if shm.size < _CONTROL.size:
            shm.close()
            raise ValueError(f"shmobj.latest: имя {self._name!r} занято сегментом другого формата")
        self._control = shm
        return True

    def _drop_control(self) -> None:
        if self._control is not None:
            self._control.close()
            self._control = None

    def get(self) -> Any:
        """Корень последнего снимка; тот же объект, пока снимок не сменился. None — снимков ещё не было.

        Писатель закрыт или упал — последний принятый корень.
        """
        if self._closed:
            return self._root
        reopened = False
        for _ in range(_ATTEMPTS):
            if self._control is None and not self._open_control():
                return self._root
            found = _read(self._control.buf)
            if found is None:  # пустой (писатель ещё не записал) или прочитан посреди записи
                continue
            epoch, gen, state = found
            if state == _CLOSED and not reopened:
                # POSIX: имя удалено, новый писатель создаст новый управляющий сегмент
                self._drop_control()
                reopened = True
                continue
            if gen == 0 or (epoch, gen) == self._version:
                return self._root
            try:
                seg = _shm.attach(_snapshot_name(self._name, gen), verify=self._verify)
            except FileNotFoundError:  # писатель уже заменил снимок
                continue
            except FormatError:  # Windows: снимок исчез между двумя шагами открытия — пустое отображение
                if _read(self._control.buf) == found:
                    raise
                continue
            self._root, self._version = seg.root, (epoch, gen)
            return self._root
        return self._root

    def close(self) -> None:
        """Закрыть управляющий сегмент; последний корень остаётся. Идемпотентен."""
        self._closed = True
        self._drop_control()

    def __enter__(self) -> Reader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
