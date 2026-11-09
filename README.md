# shmobj (Шмобж)

`dict`/`list`-подобные структуры с неограниченным уровнем вложенности в разделяемой памяти (через `mmap`) и доступ к ним
из нескольких процессов без копирования и десериализации.

`shmobj` предоставляет прокси-объекты `FrozenDict` (`Mapping`) и `FrozenList` (`Sequence`) для доступа к данным из общей
памяти.
Данные размещаются в памяти один раз. Все процессы обращаются к одной общей структуре, а Python-объекты создаются только
для значений, к которым обратились.

## Зачем нужен

В CPU-bound задачах на Python часто запускают несколько процессов, чтобы обойти ограничения GIL.
Каждый процесс при этом хранит свою изолированную копию данных.
Например, справочник, конфигурация или набор метрик размером 100 МБ на 8 воркерах потребляют около 800 МБ
памяти. `shmobj` позволяет хранить такой набор в одном общем экземпляре.

Для теста возьмем набор из миллиона записей со структурой вида `{"id", "name", "score", "tags": [...], "ok", "x"}`.
Каждый процесс проходил по всем записям.

| Число процессов | `pickle` — суммарно потреблённая память  | `shmobj` — суммарно потреблённая память |
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

Все примеры находятся в каталоге [examples](examples/README.md).  
Внимание! Перед запуском примеров нужно [установить `shmobj`](#установка).

### Общий набор данных

`create()` создаёт сегмент и записывает в него структуру. Другие процессы подключаются к нему через `attach()`,
используя имя сегмента.
Память освобождается после `unlink()` и закрытия сегмента всеми процессами (на Windows — после закрытия последнего
handle).

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

# Процесс-читатель
with shmobj.attach(segment.name) as segment:
    users = segment.root["users"]

    print(users[0].get_many(["id", "name"]))
    total = sum(user["score"] for user in users.iter_values())
```

`FrozenDict` и `FrozenList` реализуют интерфейсы `Mapping` и `Sequence`, поэтому с ними можно работать как с привычными
`dict` и `list`.

То же самое с `multiprocessing` ([shared_pool.py](examples/shared_pool.py)):

```python
import multiprocessing
import shmobj


def init(segment_name):
    global users
    users = shmobj.attach(segment_name).root["users"]  # Прокси удерживает сегмент открытым, пока жив воркер.


def score(index):
    return users[index]["score"]


if __name__ == "__main__":
    data = {"users": [{"id": i, "score": i % 100} for i in range(100_000)]}
    # При выходе из with сегмент будет закрыт и удалён из ОС.
    with shmobj.create(data, unlink_on_close=True) as segment:
        with multiprocessing.Pool(4, initializer=init, initargs=(segment.name,)) as pool:
            print(sum(pool.map(score, range(len(data["users"])), chunksize=10_000)))
```

### `shmobj.latest`

`shmobj.latest` позволяет публиковать снимки под заданным именем.   
Писатель заменяет предыдущий снимок новым, а читатель получает актуальную версию при вызове `get()`.
Сокеты для этого не используются; процессы могут запускаться в любом порядке.

```python
from shmobj import latest

# Процесс-писатель
writer = latest.Writer("metrics")
writer.put({"vms": {101: {"cpu": 0.5}}})

# Процесс-читатель
reader = latest.Reader("metrics")
root = reader.get()  # None — снимков ещё не было; тот же объект, пока снимок не сменился
```

Пока снимок не изменился, `get()` возвращает тот же объект. Читатель узнаёт об обновлении только при очередном вызове
`get()`.

Пример: [latest_pubsub.py](examples/latest_pubsub.py).

**Очистка после аварийного завершения.** На Linux и macOS имена сегментов могут оставаться в ОС после падения писателя.
На Linux они находятся в `/dev/shm`. Такие сегменты сохраняются до перезагрузки, перезапуска контейнера или следующего
писателя, использующего то же имя.

### Pub/Sub: `shmobj.channel`

Один писатель, много читателей; читатели узнают об обновлении снимка без опроса.

- Новый подписчик сразу получает последний снимок.
- Промежуточные снимки могут пропускаться: читатель получает актуальное состояние, а не очередь всех обновлений.
- После завершения процессов ресурсы канала освобождаются ОС (не остаётся даже имён в `/dev/shm`).
- Адрес канала остаётся постоянным, поэтому читатели могут повторно подключаться после перезапуска писателя.

На Linux адрес вида `"\0имя"` использует абстрактное пространство имён Unix-сокетов: это не файл, и ядро освобождает
адрес вместе с писателем. На Windows используется TCP loopback.

#### Синхронный API

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

Пример: [channel_pubsub.py](examples/channel_pubsub.py)

#### asyncio API

Канал поддерживает асинхронные подключения и ожидание обновлений. Ожидание писателя не блокирует event loop.

```python
import asyncio
import itertools
import sys

from shmobj import channel

ADDRESS = ("127.0.0.1", 50555) if sys.platform == "win32" else "\0shmobj-example"

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


# Читатель с колбэком: он вызывается в event loop на каждый новый снимок
def on_snapshot(root):
    print(root["vms"][101]["cpu"])

async def main():
    sock = await channel.connect_async(ADDRESS, wait_writer=True)
    with channel.Subscriber(sock) as sub:
        sub.attach(on_snapshot)  # on_snapshot(sub.latest) на каждый новый снимок
        while await sub.wait_async():  # работает и с колбэком; False — писатель закрылся или упал
            pass
```

Пример: [channel_pubsub_async.py](examples/channel_pubsub_async.py).

Синхронные и асинхронные писатели и читатели совместимы: например, asyncio-читатель может подключиться к синхронному
писателю.

### Файл, `bytes` и `mmap`

`dumps()` создаёт буфер в формате `shmobj`, а `loads()` открывает его без копирования поверх `bytes`, `mmap` или другого
поддерживаемого буфера.
При чтении одного файла из нескольких процессов страницы файла обслуживаются общим файловым кешем ОС.

```python
import mmap
import shmobj

data = {"users": [{"id": 1, "name": "Ann", "score": 42}]}

# Сохранение структуры в файл.
with open("data.shmobj", "wb") as file:
    file.write(shmobj.dumps(data))

with open("data.shmobj", "rb") as file:  # в каждом процессе: страницы файла из кеша ОС — одна копия на всех
    root = shmobj.loads(mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ))
```

## Жизненный цикл сегмента

- `close()` закрывает сегмент в текущем процессе.
- `unlink()` удаляет имя сегмента из ОС. Выход из контекста `with` вызывает `close()`. Автоматический `unlink()`
  выполняется только при создании сегмента с `unlink_on_close=True`.
- Прокси-объекты удерживают сегмент открытым. После `close()` обращение к ним приводит к `SegmentClosedError`.
- **Linux и macOS:** сегмент — объект POSIX shared memory. На Linux он представлен файлом в `/dev/shm` и хранится в RAM;
  сегмент существует до `unlink()`, а не до выхода создателя.
- **Windows:** сегмент удаляется после закрытия последнего handle.

Если на Linux или macOS аварийно завершилась вся группа процессов, имя сегмента остаётся в ОС до перезагрузки
(в контейнере — до его перезапуска) или `rm /dev/shm/<имя>` на Linux.

Подробнее: [Жизненный цикл сегмента](doc/usage.md#жизненный-цикл-сегмента).

## Ограничения

- **Доступ только для чтения.** Опубликованные данные нельзя изменить на месте: для обновления публикуется новый
  снимок через [`shmobj.latest`](#shmobjlatest) или [`shmobj.channel`](#pubsub-shmobjchannel).
- **Поддерживаемые значения:** `dict`, `list`, `tuple` (при чтении представляется как список), `None`, `bool`, `int`
  произвольной длины, `float`, `str` и `bytes`. Ключи словарей должны быть скалярными.
- **Поддержка платформ:** `shmobj.channel` доступен на Linux и Windows; на macOS используйте `shmobj.latest`.
- **Проверка формата:** повреждённые данные приводят к `FormatError` при обращении к ним. Метод `validate()` проверяет
  весь буфер.

## Почему не стандартные средства Python

- **`multiprocessing.shared_memory`.** Предоставляет разделяемый буфер байтов без поддержки вложенных структур
  Python. Структуру приходится сериализовать, например через `pickle`, и каждый процесс десериализует её в свою копию.
- **`multiprocessing.Manager().dict()`.** Данные обслуживаются отдельным процессом-сервером. Обращение к элементу
  словаря требует IPC-запроса и сериализации.
- **`fork`.** Сам по себе не гарантирует экономию памяти для Python-объектов: изменение счётчиков ссылок может приводить
  к copy-on-write и копированию страниц между процессами.

Подробнее: [Сравнение с альтернативами](doc/comparison.md).

## Документация

- [Руководство](doc/usage.md) — сегменты, файлы, проверка данных, `channel`, `latest` и отладчик.
- [API Reference](doc/api.md) — описание публичного API.
- [Сборка](doc/build.md) — устройство колеса и переносимые сборки.
- [Формат данных](doc/format.md) и [архитектура](doc/architecture.md) — бинарный формат и внутреннее устройство.
- [Разработка](doc/dev.md) — тесты и выпуск релизов.
- [Замеры и состояние проекта](doc/status.md).

## Сборка из исходников

### Вариант для ленивых

Прямо из репозитория. (pip сам скачает maturin и соберёт колесо):

```bash
pip install git+https://github.com/Aleksandrln/shmobj
```

## Полная сборка из каталога проекта

Нужны CPython 3.11+ и Rust stable ([rustup](https://rustup.rs)); на Windows ещё Visual Studio Build Tools с компонентом
C++. Rust на Linux и macOS:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
```

Колесо из клона; maturin — в venv (в Debian и Ubuntu `pip install` вне venv запрещён, PEP 668):

```bash
git clone https://github.com/Aleksandrln/shmobj
cd shmobj
python3 -m venv .venv
. .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install maturin
maturin build --release -o dist   # dist/shmobj-<версия>-cp311-abi3-<платформа>.whl
maturin sdist -o dist             # исходники
```

Колесо ставится в любое окружение с CPython 3.11+ на той же платформе: `pip install dist/shmobj-*.whl`. Без
`--release` сборка примерно в 10 раз медленнее. `cargo build --release` собирает только библиотеку, а не пакет.

Устройство колеса и переносимые сборки (старая glibc, Windows из Linux): [doc/build.md](doc/build.md).

## Лицензия

Двойное лицензирование: на выбор [MIT](LICENSE-MIT) или [Apache-2.0](LICENSE-APACHE).

## Авторство

Проект создан [AleksandrLn](https://github.com/Aleksandrln) при участии [Claude](https://claude.ai) от Anthropic.
