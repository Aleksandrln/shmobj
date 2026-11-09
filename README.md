# shmobj

Вложенные `dict`/`list` в общей памяти: структура лежит в памяти один раз, процессы читают её без копирования и
десериализации.

## Зачем

Из-за GIL параллельную работу в Python запускают несколькими процессами, а у каждого процесса своя копия данных.
Справочник, настройки или метрики на 100 МБ в 8 воркерах занимают 800 МБ.

`shmobj` собирает структуру в компактный формат в общей памяти. Процессы подключаются к ней по имени за
микросекунды и получают прокси: `FrozenDict` (`Mapping`) и `FrozenList` (`Sequence`) читают значения прямо из общего
буфера. Python-объекты создаются только для значений, к которым обратились.

Миллион записей вида `{"id", "name", "score", "tags": [...], "ok", "x"}`, каждый процесс проходит по всем записям:

| Процессов | `pickle` в каждом | `shmobj` |
|----------:|------------------:|---------:|
|         1 |            628 МБ |   168 МБ |
|         4 |            2.5 ГБ |   168 МБ |
|         8 |            5.0 ГБ |   168 МБ |

Подключение — около 50 мкс, `pickle.loads` того же набора — 1.5 с. Плата — доступ по ключу в 3–6 раз медленнее, чем
у `dict`; для горячих циклов есть `get_many()`, `iter_values()`, `iter_items()` и `materialize()`
([подробнее](doc/usage.md#скорость-доступа)).

## Установка

Колесо под свою платформу (CPython 3.11+; Linux x86_64 и aarch64, Windows x64, macOS arm64) — со страницы
[релизов](https://github.com/Aleksandrln/shmobj/releases)
или [из исходников](#сборка-из-исходников).

```bash
pip install ./shmobj-<версия>-cp311-abi3-<платформа>.whl
```

## Использование

| Когда нужно                                                                 | Метод               | Пример                                          |
|-----------------------------------------------------------------------------|---------------------|-------------------------------------------------|
| Один набор данных на все процессы                                           | `create` / `attach` | [shared_pool.py](examples/shared_pool.py)       |
| Набор в файле или в `bytes`                                                 | `dumps` / `loads`   | [file_mmap.py](examples/file_mmap.py)           |
| Писатель обновляет данные, читатели получают каждый снимок (Linux, Windows) | `shmobj.channel`    | [channel_pubsub.py](examples/channel_pubsub.py) |
| Независимые процессы берут последний снимок по имени (любая ОС)             | `shmobj.latest`     | [latest_pubsub.py](examples/latest_pubsub.py)   |

Все примеры — в [examples/](examples/README.md).

### Общий набор данных

```python
import shmobj

segment = shmobj.create({
    "users": [
        {"id": 1, "name": "Ann", "score": 42},
        {"id": 2, "name": "Bob", "score": 37},
    ]
})

print(segment.name)  # имя передать процессам-читателям

# в процессе-читателе
with shmobj.attach(segment_name) as segment:
    users = segment.root["users"]

    print(users[0].get_many(["id", "name"]))
    total = sum(user["score"] for user in users.iter_values())
```

То же самое с обвязкой `multiprocessing` ([shared_pool.py](examples/shared_pool.py)):

```python
import multiprocessing
import shmobj


def init(name):
    global users
    users = shmobj.attach(name).root["users"]  # прокси держит сегмент открытым, пока жив воркер


def score(i):
    return users[i]["score"]


if __name__ == "__main__":
    data = {"users": [{"id": i, "score": i % 100} for i in range(100_000)]}
    # unlink_on_close: выход из with удаляет сегмент из ОС, без него — только закрывает
    with shmobj.create(data, unlink_on_close=True) as segment:
        with multiprocessing.Pool(4, initializer=init, initargs=(segment.name,)) as pool:
            print(sum(pool.map(score, range(len(data["users"])), chunksize=10_000)))
```

### Последний снимок по имени

`shmobj.latest` — без сокетов: писатель кладёт снимок под именем, читатель забирает последний, когда нужен.
Процессы запускаются в любом порядке, перезапуск писателя читателям не мешает.

```python
from shmobj import latest

# писатель
writer = latest.Writer("metrics")
writer.put({"vms": {101: {"cpu": 0.5}}})  # прежний снимок удаляется

# читатель, в любом процессе
reader = latest.Reader("metrics")
root = reader.get()  # None — снимков ещё не было; тот же объект, пока снимок не сменился
```

Пример: [latest_pubsub.py](examples/latest_pubsub.py)

О новом снимке читатель узнаёт только опросом `get()`. После падения писателя на Linux и macOS имена остаются в ОС
(на Linux — в `/dev/shm`) до перезагрузки, перезапуска контейнера или следующего писателя с тем же именем.

### Pub/Sub

`shmobj.channel` — один писатель и сколько угодно читателей. Снимок собирается один раз на всех, по сокету идёт только
ссылка на него.
Подключившийся читатель сразу получает последний снимок, промежуточные пропускаются.
После завершения процессов в ОС ничего не остаётся (даже имён в `/dev/shm`).

Адрес постоянный: к перезапущенному писателю читатели подключаются по нему же.
На Linux `"\0имя"` — адрес в абстрактном пространстве имён Unix-сокетов (abstract socket namespace): не файл, ядро освобождает его вместе с писателем. На Windows — TCP на loopback.

#### Синхронно

```python
import itertools
import sys
import time

from shmobj import channel

ADDRESS = ("127.0.0.1", 50555) if sys.platform == "win32" else "\0shmobj-example"

# Писатель

with channel.Publisher() as pub:
    pub.accept_in_thread(channel.listen(ADDRESS))  # подписчиков принимает фоновый поток
    for tick in itertools.count():
        pub.publish({"tick": tick, "vms": {101: {"cpu": tick % 100}}})  # не блокирует
        time.sleep(1)

# Читатель:
while True:
    # ждать, пока писатель не начнёт слушать адрес; wait_writer=30 — не дольше 30 секунд
    with channel.Subscriber(channel.connect(ADDRESS, wait_writer=True)) as sub:
        while sub.wait():  # True — пришёл снимок, False — писатель закрылся или упал
            print(sub.latest["vms"][101]["cpu"])  # FrozenDict поверх общей памяти
```

Примеры: [channel_pubsub.py](examples/channel_pubsub.py)

#### asyncio версия

```python

# Писатель
async def main():
    with channel.Publisher() as pub:
        pub.accept_in_loop(channel.listen(ADDRESS))  # подписчиков принимает цикл, без потока
        for tick in itertools.count():
            pub.publish({"tick": tick, "vms": {101: {"cpu": tick % 100}}})
            await asyncio.sleep(1)


# Читатель:
async def main():
    while True:
        sock = await channel.connect_async(ADDRESS, wait_writer=True)  # ожидание не блокирует цикл
        with channel.Subscriber(sock) as sub:
            while await sub.wait_async():
                print(sub.latest["vms"][101]["cpu"])


# Читатель с колбэком: снимки принимает цикл, остальной код работает как обычно (воркер Uvicorn)
def on_snapshot(root):
    print(root["vms"][101]["cpu"])


async def main():
    sock = await channel.connect_async(ADDRESS, wait_writer=True)
    with channel.Subscriber(sock) as sub:
        sub.attach(on_snapshot)  # on_snapshot(sub.latest) на каждый новый снимок
        while await sub.wait_async():  # работает и с колбэком; False — писатель закрылся или упал
            ...  # своя работа; в обработчике запроса — sub.latest
```

Примеры: [channel_pubsub_async.py](examples/channel_pubsub_async.py)

Писатели и читатели взаимозаменяемы: к синхронному писателю подключается и asyncio-читатель.

Канал без адреса (пара сокетов, писатель — дочерний процесс), несколько читателей, очередь и ограничения
абстрактного адреса — [doc/usage.md](doc/usage.md#канал-снимков).

### Файл и bytes

`dumps` даёт тот же формат, `loads` открывает его поверх `bytes`, `mmap` (отображение файла в память) или любого буфера без копирования:

```python
import mmap

import shmobj

with open("data.shmobj", "wb") as file:  # один раз
    file.write(shmobj.dumps(data))

with open("data.shmobj", "rb") as file:  # в каждом процессе: страницы файла из кеша ОС — одна копия на всех
    root = shmobj.loads(mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ))
```

## Жизненный цикл сегмента

- `close()` закрывает сегмент в процессе, `unlink()` удаляет его имя из ОС. Выход из `with` вызывает `close()`, а
  `unlink()` — только у `create(..., unlink_on_close=True)`.
- Прокси держат сегмент открытым; после `close()` обращение к ним — `SegmentClosedError`.
- Linux и macOS: сегмент — объект POSIX shared memory (на Linux — файл в `/dev/shm`, в RAM), живёт до `unlink()`.
  Если убита вся группа процессов (остановка контейнера, OOM), он остаётся до перезагрузки (на Linux — или
  `rm /dev/shm/<имя>`). Windows: сегмент исчезает с последним handle.

Подробно — [doc/usage.md](doc/usage.md#жизненный-цикл-сегмента).

## Ограничения

- Только чтение: изменить опубликованные данные нельзя, публикуется новый снимок.
- Типы: `dict`, `list`, `tuple` (читается как список), `None`, `bool`, `int` любой длины, `float`, `str`, `bytes`;
  ключи — скаляры. Остальное — `TypeError` с путём до значения.
- `shmobj.channel` — Linux и Windows; на macOS — `shmobj.latest`.
- Повреждённые данные дают `FormatError` при обращении, без падения процесса; `validate()` проверяет весь буфер.

## Почему не готовые решения

- `multiprocessing.shared_memory` хранит байты: структуру туда кладут через `pickle`, и каждый читатель держит копию.
- `Manager().dict()` — сервер с сокетом: каждое `d[key]` — запрос и `pickle`.
- `fork` не спасает: обращение к объекту меняет счётчик ссылок, и страницы копируются в каждый процесс.

Подробно — [doc/comparison.md](doc/comparison.md).

## Документация

- [doc/usage.md](doc/usage.md) — руководство: сегменты, файлы, проверка данных, канал, `latest`, отладчик.
- [doc/api.md](doc/api.md) — справочник API.
- [doc/build.md](doc/build.md) — устройство колеса и сборка.
- [doc/format.md](doc/format.md), [doc/architecture.md](doc/architecture.md) — бинарный формат и устройство.
- [doc/dev.md](doc/dev.md) — разработка, тесты, релиз; [doc/status.md](doc/status.md) — состояние и замеры.

## Сборка из исходников

Нужен Rust stable ([rustup](https://rustup.rs)); колесо одно (abi3) на все версии CPython от 3.11:

```bash
pip install git+https://github.com/Aleksandrln/shmobj
```

Сборка колеса из клона и переносимые колёса — [doc/build.md](doc/build.md).

## Лицензия

На выбор: [MIT](LICENSE-MIT) или [Apache-2.0](LICENSE-APACHE).

Автор — [AleksandrLn](https://github.com/Aleksandrln), разработано совместно с [Claude](https://claude.ai) (Anthropic).
