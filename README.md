# shmobj (Шмобж)

`dict`/`list` подобные структуры с неограниченным уровнем вложенности в разделяемой памяти (через `mmap`) и доступ к ним
из нескольких процессов без копирования и десериализации.

`shmobj` предоставляет прокси-объекты `FrozenDict` (`Mapping`) и `FrozenList` (`Sequence`) для доступа к данным из общей
памяти.
Данные размещаются в памяти один раз. Все процессы обращаются к одной общей структуре, а Python-объекты создаются только
для значений к которым обратились.

## Зачем нужен

В CPU-bound задачах Python часто спавнят несколько процессов, чтобы обойти ограничения GIL.
Каждый процесс при этом хранит свою изолированную копию данных.
Например, справочник, конфигурация или набор метрик размером 100 МБ на 8 воркерах потребляют около 800 МБ
памяти. `shmobj` позволяет хранить такой набор в одном общем экземпляре.

Для теста возьмем набор из миллиона записей со структурой вида `{"id", "name", "score", "tags": [...], "ok", "x"}`.
Каждый процесс проходил по всем записям.

| Число процессов | `pickle` — суммарно протребленная память | `shmobj` — суммарно потребленная память |
|----------------:|-----------------------------------------:|----------------------------------------:|
|               1 |                                   628 МБ |                                  168 МБ |
|               4 |                                   2,5 ГБ |                                  168 МБ |
|               8 |                                   5,0 ГБ |                                  168 МБ |

Дополнительные замеры: [doc/status.md](doc/status.md).

Подключение к готовому сегменту занимает около 50 мкс, тогда как `pickle.loads` для того же набора — около 1,5 с.
Плата — доступ по ключу в 3–6 раз медленнее, чем у обычного `dict`. Для ускорения последовательного чтения и частого
доступа предусмотрены `get_many()`, `iter_values()`, `iter_items()` и `materialize()`.

Подробнее о производительности и способах доступа: [Скорость доступа](doc/usage.md#скорость-доступа).

## Установка

Готовые колёса доступны на [странице релизов](https://github.com/Aleksandrln/shmobj/releases).   
Скачайте колесо для своей платформы и установите его:

```bash
pip install ./shmobj-<version>-cp311-abi3-<platform>.whl
```

Можно собрать [из исходников](#сборка-из-исходников).

- **Python:** CPython 3.11 и новее;
- **Linux:** x86_64, aarch64;
- **Windows:** x64;
- **macOS:** arm64.

## Основные сценарии

| Сценарий                                        | API                     | Пример                                          |
|-------------------------------------------------|-------------------------|-------------------------------------------------|
| Один набор данных для нескольких процессов      | `create()` / `attach()` | [shared_pool.py](examples/shared_pool.py)       |
| Читателям нужен последний опубликованный снимок | `shmobj.latest`         | [latest_pubsub.py](examples/latest_pubsub.py)   |
| Подписка на новые снимки без постоянного опроса | `shmobj.channel`        | [channel_pubsub.py](examples/channel_pubsub.py) |
| Чтение данных из файла или `bytes`              | `dumps()` / `loads()`   | [file_mmap.py](examples/file_mmap.py)           |

Все примеры находятся в каталоге [examples/](examples/README.md).

### Общий набор данных

`create()` создаёт сегмент и записывает в него структуру. Другие процессы подключаются к нему через `attach()`,
используя имя сегмента.
Создайте сегмент в процессе-писателе и передайте его имя процессам-читателям.  
Сегмент должен оставаться открытым, пока читатели работают.

```python
import shmobj

# Процесс-писатель

segment = shmobj.create({
    "users": [
        {"id": 1, "name": "Ann", "score": 42},
        {"id": 2, "name": "Bob", "score": 37},
    ]
})

print(segment.name)  # имя передать процессам-читателям

# в процессе-читателе
with shmobj.attach(segment.name) as segment:
    users = segment.root["users"]

    print(users[0].get_many(["id", "name"]))
    total = sum(user["score"] for user in users.iter_values())
```

То же самое с обвязкой `multiprocessing` ([shared_pool.py](examples/shared_pool.py)):
`FrozenDict` и `FrozenList` реализуют интерфейсы `Mapping` и `Sequence`, поэтому с ними можно работать как с привычными
`dict` и `list`.

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
На Linux `"\0имя"` — адрес в абстрактном пространстве имён Unix-сокетов (abstract socket namespace): не файл, ядро
освобождает его вместе с писателем. На Windows — TCP на loopback.

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

### Файл и bytes

`dumps` даёт тот же формат, `loads` открывает его поверх `bytes`, `mmap` (отображение файла в память) или любого буфера
без копирования:

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
