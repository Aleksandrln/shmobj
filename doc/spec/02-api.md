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
| `dumps_into(obj, alloc, *, dedup=True, checksum=True) -> int` | Собрать сегмент и записать в начало буфера `alloc(size)` (записываемый, C-непрерывный, не короче `size`, иначе `ValueError`); возвращает `size`. Без промежуточных `bytes`; экспорт буфера отпускается до возврата. Ошибка сборки — до вызова `alloc` |
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

## 6. Канал снимков `shmobj.channel` (Linux, Windows)

Один писатель отдаёт читателю снимки целиком; читателю нужен только последний. Контракт с
типами: `python/shmobj/channel.pyi`.

```python
pub_sock, sub_sock = shmobj.channel.socketpair()     # pub_sock — в дочерний процесс: spawn_kwargs(pub_sock)
pub = shmobj.channel.Publisher(pub_sock)
pub.publish({"vms": {101: {"cpu": 0.5}}})            # True; False — очередь полна, снимок пропущен

sub = shmobj.channel.Subscriber(sub_sock)
sub.attach(lambda root: ...)                         # asyncio: колбэк на каждый новый снимок
sub.latest["vms"].get_many([101, 102], None)         # корень последнего снимка
```

Передача на Linux:
- снимок собирается `dumps_into` прямо в memfd, затем memfd запечатывается `F_SEAL_SEAL | SHRINK | GROW | WRITE`;
- дескриптор идёт по сокету AF_UNIX SOCK_SEQPACKET (SCM_RIGHTS), одно сообщение на снимок,
  тело — один байт;
- читатель принимает только memfd с `F_SEAL_WRITE` и `F_SEAL_SHRINK` и отображает его на чтение.
  Без печатей снимок могли бы изменить или укоротить под читателем, и чтение отображения дало бы SIGBUS;
- имён в ОС нет: снимок освобождается, когда закрыт последний дескриптор и отпущен последний
  прокси.

Передача на Windows (нет memfd, печатей и передачи handle в сообщении):
- снимок — `create(obj, "<name>-<pid>-<номер>")`; по сокету (`socket.socketpair()`, loopback TCP)
  идёт имя строкой с `\n`;
- читатель разбирает очередь, открывает (`attach`) последний снимок, который ещё есть, и отвечает
  именами всех принятых строками с `\n`. Писатель закрывает подтверждённые снимки при следующем
  `publish`: до подтверждения снимок держит только он, и без этого снимок исчезал бы до открытия;
- объект ядра живёт, пока открыт хоть один handle: после падения любой из сторон ничего не
  остаётся, `unlink` не нужен. Снимок, который писатель закрыл до открытия (вышел), читатель
  пропускает;
- неизменность снимка держится на писателе: печатей нет, читатель открывает снимок на запись
  (`SharedMemory`). Чужие подтверждения (прежний писатель того же сокета) писатель пропускает.

После падения любой из сторон в ОС ничего не остаётся.

| Член | Описание |
|---|---|
| `SUPPORTED: bool` | Linux с `memfd_create`, печатями и SCM_RIGHTS или Windows. Иначе (macOS) конструкторы, `socketpair()` и `spawn_kwargs()` дают `NotImplementedError` |
| `socketpair()` | Linux — AF_UNIX SOCK_SEQPACKET, Windows — loopback TCP; концы равноправны, не наследуются |
| `spawn_kwargs(sock) -> dict` | Аргументы `Popen`/`create_subprocess_exec` для наследования `sock`: `pass_fds` (Linux), `startupinfo` с `handle_list` (Windows; `sock` становится наследуемым). В дочернем процессе — `socket.socket(fileno=…)` с номером из родителя |
| `Publisher(sock, *, sndbuf=16384, name="shmobj")` | Владеет `sock`, переводит в неблокирующий режим. Linux: `SO_SNDBUF = sndbuf`, `name` — имя memfd (видно в `/proc/<pid>/maps`). Windows: `sndbuf` не используется, `name` — префикс имён снимков |
| `Publisher.publish(obj, *, dedup=True, checksum=True) -> bool` | Не блокирует. `False` — очередь полна (читатель не разбирает её), снимок пропущен. Читатель закрыл канал — `BrokenPipeError` |
| `Publisher.close()` | Закрывает сокет. Windows: и неподтверждённые снимки — читатель их не получит |
| `Subscriber(sock, *, verify=False)` | Владеет `sock`. `verify` передаётся в `loads`/`attach` для каждого снимка |
| `Subscriber.latest` | Корень последнего принятого снимка, `None` — снимков не было. Переживает закрытие канала |
| `Subscriber.receive() -> bool` | Разбирает очередь без ожидания и берёт последний снимок, прочие закрывает (Windows — подтверждает). `True` — `latest` сменился. Конец канала закрывает сокет. Linux: незапечатанный снимок — `ValueError`, `latest` прежний |
| `Subscriber.attach(callback=None, *, loop=None)` | По умолчанию текущий цикл. `loop.add_reader`, а в цикле без него (ProactorEventLoop, Windows) — задача, которая ждёт `sock_recv(sock, 0)` (IOCP завершает его, когда есть данные или конец). `receive()` по готовности сокета, `callback(latest)` на каждый новый снимок. Исключения уходят в обработчик исключений цикла, приём продолжается. Конец канала снимает сокет с цикла |
| `Subscriber.detach()` | Снять с цикла, сокет открыт |
| `fileno()`, `closed`, `close()`, `__enter__`/`__exit__` | У обоих концов; `close()` идемпотентен |

Очередь ограничена: на Linux — `sndbuf` (каждое непрочитанное сообщение держит свой memfd), на
Windows — 4 неподтверждённых снимка. Писатель не копит снимки, а пропускает их. Прежний снимок
освобождается, когда отпущены его прокси. На Linux отображение держит копию дескриптора (`mmap`),
так что живой снимок — это одно отображение и один дескриптор.

## 7. Зарезервированные имена (RW, не реализуются в v1)

`SharedDict`, `SharedList`, `create_rw`, `attach_rw`, `freeze`, `thaw`, модуль `shmobj.abc`.
