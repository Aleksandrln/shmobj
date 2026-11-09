# shmobj

Позволяет создавать сложные `dict`/`list` структуры в разделяемой памяти и передавать ссылки на них между
процессами.
Одна структура на весь пул процессов: это сильно экономит память и убирает затраты на сериализацию и
десериализацию.

## Зачем это нужно

Главная проблема параллельного программирования на Python — GIL: интерпретатор в один момент времени исполняет
только один поток.
Ради реальной конкурентности запускают несколько процессов. Но процессы изолированы и не разделяют память так, как
это делают потоки: каждый процесс — отдельный экземпляр интерпретатора со своим GIL и своей копией памяти.

Часто разным процессам одного пула нужны общие данные: справочник, настройки системы, общие метрики. Копии всего
этого живут в каждом процессе, и свободная память убывает с ростом числа процессов.
Например, если запущено 8 воркеров и каждому нужен один и тот же набор данных весом 100 МБ, у каждого воркера своя
копия объектов: восемь копий, всего 800 МБ.

### Прокси без полной копии

`shmobj` кладёт структуру в общую память один раз, процессы подключаются к ней по имени и читают одни и те же
страницы.
Читатель получает прокси, которые берут значения прямо из общего буфера: `FrozenDict` реализует `Mapping`, а
`FrozenList` — `Sequence`.
Доступны привычные операции: `[]`, `get()`, `in`, `len()`, итерация и срезы списков. Срез `FrozenList` тоже остаётся
представлением без копирования.
Структура не распаковывается: Python-объекты создаются только для значений, к которым процесс обратился, а
подключение занимает микросекунды при любом объёме.

Для обмена ссылками на новые наборы данных — [Pub/Sub](#pubsub) или
[последний снимок по имени](#последний-снимок-по-имени).

## Экономия памяти

Набор из миллиона записей вида `{"id", "name", "score", "tags": [...], "ok", "x"}` (эталон из
`doc/spec/07-testing.md` §5).
Каждый процесс загружает данные и один раз проходит по всем записям. Linux x86_64, Python 3.13, release-сборка.

| Процессов | Без `shmobj` (`pickle` в каждом) | С `shmobj` |
|----------:|---------------------------------:|-----------:|
|         1 |                           628 МБ |     168 МБ |
|         4 |                           2.5 ГБ |     168 МБ |
|         8 |                           5.0 ГБ |     168 МБ |

Подключение к сегменту — около 50 мкс, `pickle.loads` того же набора — 1.5 с.

### Компромисс: память вместо скорости доступа

Обращение по ключу или индексу в `shmobj` примерно в **3–6 раз медленнее**, чем в обычных `dict` и `list`. Для горячих
циклов есть пакетные операции и итераторы:

- `get_many()` — несколько значений за одно обращение;
- `iter_values()` и `iter_items()` — перебор значений и пар;
- `materialize()` — глубокая копия ветки в обычные `dict`/`list`, если с ней предстоит много работать.

Подробные замеры: [doc/status.md](doc/status.md#базовые-замеры-release-linux-x86_64-python-313-эталонный-набор-spec07-testingmd-5).

## Быстрый старт

Процесс-владелец создаёт сегмент и передаёт его имя другим процессам любым удобным способом — например, через
аргументы запуска или переменную окружения:

```python
import shmobj

segment = shmobj.create({
    "users": [
        {"id": 1, "name": "Ann", "score": 42},
        {"id": 2, "name": "Bob", "score": 37},
    ]
})

print(segment.name)  # имя передать процессам-читателям
```

Читатель подключается к уже созданному сегменту:

```python
import shmobj

with shmobj.attach(segment_name) as segment:
    users = segment.root["users"]

    print(users[0].get_many(["id", "name"]))
    total = sum(user["score"] for user in users.iter_values())
```

### Пул воркеров

Каждый воркер подключается к сегменту один раз, в `initializer`:

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
    with shmobj.create(data) as segment:
        with multiprocessing.Pool(8, initializer=init, initargs=(segment.name,)) as pool:
            print(sum(pool.map(score, range(len(data["users"])), chunksize=10_000)))
        segment.unlink()  # удалить имя из ОС; выход из with только закрывает сегмент
```

## Обновление данных

Набор данных доступен только для чтения. Чтобы его изменить, публикуют новый набор целиком (снимок), а читатели
переключаются на него.

### Pub/Sub

`shmobj.channel` — Pub/Sub поверх сокетов: один писатель (`Publisher`), сколько угодно читателей (`Subscriber`).
Снимок собирается один раз на всех. Читатель держит ссылку на последний принятый: `subscriber.latest`, либо получает
колбэк на каждый новый.
Каждый снимок — отдельный кусок общей памяти, по сокету идёт только ссылка на него (на Linux — дескриптор memfd, на
Windows — имя). Старые снимки освобождаются, когда отпущены их прокси.

Писатель не ждёт читателей: если читатель отстал, `publish()` возвращает `False`, и ему этот снимок пропускается.
Подключившийся читатель сразу получает последний снимок. Поддерживается на **Linux и Windows**. После завершения или
падения любой из сторон ресурсы освобождает ОС.

Один писатель и несколько читателей — например, воркеры Gunicorn или Uvicorn. Писатель:

```python
import contextlib
import itertools
import os
import sys
import tempfile
import time

from shmobj import channel

WINDOWS = sys.platform == "win32"

with tempfile.TemporaryDirectory() as tmp, channel.Publisher() as pub:
    # Linux — путь сокета AF_UNIX, Windows — TCP на loopback, порт 0 — любой свободный.
    listener = channel.listen(("127.0.0.1", 0) if WINDOWS else os.path.join(tmp, "snapshots.sock"))
    address = pub.accept_in_thread(listener)  # подписывать подключения в фоновом потоке
    print(address, flush=True)  # передать читателям: путь или (host, port)
    for tick in itertools.count():
        with contextlib.suppress(BrokenPipeError):  # ушёл последний читатель; новые ещё подключатся
            pub.publish({"tick": tick, "vms": {vm: {"cpu": tick % 100} for vm in range(100, 110)}})
        time.sleep(1)
```

Читатель:

```python
import sys

from shmobj import channel

address = sys.argv[1]  # на Windows — ("127.0.0.1", порт)

with channel.Subscriber(channel.connect(address)) as sub:
    # wait() спит, пока не придёт снимок: True — sub.latest сменился, False — писатель закрыл канал.
    while sub.wait():
        root = sub.latest  # FrozenDict поверх общей памяти, без копирования
        print(f"tick={root['tick']} cpu[101]={root['vms'][101]['cpu']}")
```

Писатель упал или перезапустился — у читателя канал закрыт (`sub.closed`), подключаться нужно заново.

#### Писатель запускает читателя сам

Пара сокетов, второй конец дочерний процесс наследует (`spawn_kwargs` — аргументы `subprocess.Popen` и
`asyncio.create_subprocess_exec`). Адрес и файл сокета не нужны:

```python
import subprocess
import sys
import time

from shmobj import channel

pub_end, sub_end = channel.socketpair()
with sub_end:  # копия остаётся у ребёнка; без закрытия своей ребёнок не узнает о закрытии канала
    child = subprocess.Popen([sys.executable, "child.py", str(sub_end.fileno())], **channel.spawn_kwargs(sub_end))

with channel.Publisher(pub_end) as pub:
    for tick in range(5):
        pub.publish({"tick": tick})
        time.sleep(0.1)
child.wait()  # выход из with закрыл канал, wait() у ребёнка вернул False
```

`child.py`:

```python
import socket
import sys

from shmobj import channel

with channel.Subscriber(socket.socket(fileno=int(sys.argv[1]))) as sub:
    while sub.wait():
        print("tick", sub.latest["tick"])
```

#### Абстрактный адрес (Linux)

Адрес `"\0имя"` не создаёт файла и исчезает вместе с писателем; читатель знает имя заранее:

```python
# писатель
pub = channel.Publisher(name="metrics")  # name — имя memfd, видно в /proc/<pid>/maps
pub.accept_in_thread(channel.listen("\0metrics"))
pub.publish({"vms": {101: {"cpu": 0.5}}})

# читатель, когда нужно
sub = channel.Subscriber(channel.connect("\0metrics"))
sub.wait(timeout=1)  # первый снимок мог ещё не дойти; дальше — sub.receive() без ожидания
print(sub.latest["vms"][101]["cpu"])
```

#### asyncio

Писатель принимает подключения в цикле (`accept_in_loop`, без потока), читатель получает колбэк на каждый снимок
(`attach`):

```python
import asyncio

from shmobj import channel


async def writer(address):
    with channel.Publisher() as pub:
        pub.accept_in_loop(channel.listen(address))
        for tick in range(5):
            pub.publish({"tick": tick})
            await asyncio.sleep(0.1)


async def reader(address):
    with channel.Subscriber(channel.connect(address)) as sub:
        sub.attach(lambda root: print("tick", root["tick"]))  # исключения — в обработчик исключений цикла
        await asyncio.sleep(1)
```

#### Несколько каналов в одном потоке

`Subscriber` отдаёт `fileno()`: его можно ждать в своём `selectors` вместе с другими сокетами, а принимать —
`receive()` без ожидания:

```python
import selectors

from shmobj import channel

with selectors.DefaultSelector() as sel:
    for name in ("metrics", "prices"):
        sel.register(channel.Subscriber(channel.connect("\0" + name)), selectors.EVENT_READ, name)
    while sel.get_map():
        for key, _ in sel.select():
            sub = key.fileobj
            if sub.receive():  # True — sub.latest сменился
                print(key.data, sub.latest["tick"])
            if sub.closed:  # писатель закрыл канал
                sel.unregister(sub)
```

Запускаемые примеры — в каталоге [examples](examples), устройство канала — в
[doc/usage.md](doc/usage.md#канал-снимков-linux-windows).

### Последний снимок по имени

`shmobj.latest` — без сокетов: писатель кладёт снимок под именем, читатель забирает последний, когда нужен.
Процессы запускаются в любом порядке, писателя может ещё не быть, его перезапуск читателям не мешает.
Работает на **Linux, Windows и macOS**.

```python
from shmobj import latest

# писатель
writer = latest.Writer("metrics")
writer.put({"vms": {101: {"cpu": 0.5}}})  # прежний снимок удаляется

# читатель, в любом процессе
reader = latest.Reader("metrics")
root = reader.get()  # None — снимков ещё не было; тот же объект, пока снимок не сменился
```

Читатель, который реагирует на смену снимка:

```python
import time

from shmobj import latest

reader = latest.Reader("metrics")
seen = None
while True:
    root = reader.get()
    if root is not None and root is not seen:  # тот же объект — снимок не сменился
        seen = root
        print(root["vms"][101]["cpu"])
    time.sleep(1)
```

Снимки — именованные сегменты, номер текущего лежит в маленьком управляющем сегменте с тем же именем.
О новом снимке читатель узнаёт только опросом `get()` (проверка — чтение 40 байт). `writer.close()` и выход
интерпретатора удаляют имена писателя; читатели дочитывают уже открытое. После падения писателя на Linux и
macOS имена остаются (см. [жизненный цикл](#жизненный-цикл-сегмента)) — следующий `Writer` с тем же именем
подхватывает их и удаляет.

### Что выбрать

| Нужно | Чем |
|---|---|
| Набор данных задаётся один раз | `create` / `attach` по имени или файл + `mmap` |
| Писатель запускает читателя сам, уведомление о каждом снимке | `channel`: `socketpair` + `spawn_kwargs` |
| Один писатель, много воркеров, уведомление о каждом снимке | `channel`: `listen` / `connect` |
| Процессы живут независимо, читатель берёт последний снимок, когда нужно; macOS | `latest` |

Канал после падения любой стороны ничего не оставляет в ОС, и снимок на Linux запечатан от изменения.
У `latest` уведомления нет, а неизменность снимка держится на писателе.

## Есть же `multiprocessing.shared_memory`

`SharedMemory` — по сути массив байт, а `shmobj` даёт `dict`/`list`-подобные объекты поверх него. `ShareableList`
хранит только плоский список скаляров. `multiprocessing.Manager().dict()` держит данные в отдельном процессе: каждое
обращение — запрос к нему с `pickle` туда и обратно.

## Почему не хватает `fork`

Дочерний процесс после `fork` получает память родителя без копирования, пока страницы не меняются (copy-on-write). Для
Python-объектов это не работает:

- **Чтение — тоже запись.** Каждое обращение к объекту меняет его счётчик ссылок, а он лежит в заголовке объекта.
  Страница с объектом копируется в память ребёнка. Один проход по миллиону записей копирует около **340 МБ в каждый**
  дочерний процесс — даже после `gc.freeze()`, который убирает записи сборщика мусора.
- **Данные фиксируются в момент `fork`.** Обновление в родителе дети не увидят; новый набор — только перезапуском
  процессов.
- **Нужен общий родитель с уже загруженными данными.** Независимые процессы и сервисы так данные не разделят.
- **`fork` есть не везде.** На Windows его нет, на macOS `multiprocessing` по умолчанию использует `spawn`, на Linux
  с Python 3.14 — `forkserver`: при них данные в каждом процессе загружаются заново.

Страницы сегмента `shmobj` никто не меняет: счётчики ссылок есть только у прокси и прочитанных значений, а не у данных
в общей памяти.

## Возможности

### Поддерживаемые данные

`dict`, `list`, `tuple`, `None`, `bool`, `int` любой длины, `float`, `str` и `bytes`. Ключи словарей должны быть
скалярными значениями.

Контейнеры доступны только для чтения. Для изменения данных нужно создать и опубликовать новый сегмент.

### Работа с буферами и файлами

`dumps` даёт тот же формат, что и сегмент, а `loads` открывает его поверх любого буфера: `bytes`, `mmap`,
`SharedMemory`. Именованный сегмент для этого не нужен:

- **Файл и `mmap`.** Набор собирается один раз и пишется в файл; процессы отображают файл в память и читают его
  страницы из кеша ОС — тоже одна копия на всех. Файл переживает перезапуск и перезагрузку, его можно выкладывать
  вместе с приложением. Подключение по-прежнему мгновенное: ОС подгружает с диска только прочитанные страницы.
- **Общий файловый дескриптор.** Буфер передаётся другому процессу дескриптором (memfd, файл, унаследованный при
  запуске), а не по имени; после закрытия всех дескрипторов ОС освобождает память сама. Так устроен Pub/Sub на Linux.
- **`bytes` из кеша или сети.** Большой блоб из Redis, с диска или по сокету читается выборочно: нужные поля без
  распаковки всего, как пришлось бы с `pickle` или JSON.

```python
import mmap
import shmobj

blob = shmobj.dumps(data)
root = shmobj.loads(blob)  # прокси поверх bytes

with open("data.bin", "wb") as file:  # один раз, при сборке набора
    file.write(blob)

with open("data.bin", "rb") as file:  # в каждом процессе
    mapped = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
    root = shmobj.loads(mapped)
```

`dumps_into(obj, alloc)` записывает данные непосредственно в предоставленный буфер без промежуточного объекта
`bytes`.

### Проверка целостности и отладка

- `attach(name, verify=True)` проверяет контрольную сумму сегмента при подключении.
- `validate(buf)` проверяет весь буфер; повреждённые данные также обнаруживаются при обращении к ним через
  `FormatError`.
- PyCharm и VS Code показывают прокси как обычные контейнеры — плагин `pydevd` устанавливается вместе с пакетом.

## Установка

- Ядро на Rust, Python bindings через PyO3.
- **CPython 3.11 или новее**, ABI3: один wheel для разных версий CPython.
- Версия 1 — только чтение: опубликованные данные нельзя изменить.

### Готовые wheels

CI собирает wheels для следующих платформ:

- Linux x86_64 и aarch64;
- Windows x64;
- macOS arm64.

Готовые файлы доступны в артефактах задачи `wheels` в GitHub Actions. Для установки скачанного wheel:

```bash
pip install ./shmobj-<version>-cp311-abi3-<platform>.whl
```

> `shmobj.channel` поддерживается на Linux и Windows. На macOS конструкторы канала и `socketpair()` возвращают
`NotImplementedError`.

### Сборка из исходников

Нужен Rust stable:

```bash
pip install git+https://github.com/Aleksandrln/shmobj
```

## Жизненный цикл сегмента

- `close()` закрывает сегмент в текущем процессе. Контекстный менеджер вызывает `close()` автоматически.
- `unlink()` удаляет именованный сегмент из ОС и обычно вызывается процессом-владельцем, когда данные больше не нужны.
  Выход из `with` сам по себе не вызывает `unlink()`.
- Прокси удерживают ссылку на `Segment`: память не освобождается, пока существует хотя бы один прокси.
- После `close()` обращение через ранее созданные прокси вызывает `SegmentClosedError`.
- Процессы, уже подключившиеся к сегменту, могут продолжать читать его после `unlink()` до собственного `close()`.
- В Windows сегмент существует, пока открыт хотя бы один дескриптор; `unlink()` ничего не делает.
- На Linux и macOS именованный сегмент — файл в `/dev/shm`, то есть в оперативной памяти. Он живёт до `unlink()`,
  а не до выхода создателя. Если создатель завершился без `unlink()`, сегмент удаляет `resource_tracker` —
  отдельный процесс Python — с предупреждением о leaked shared_memory. Если убита вся группа процессов (остановка
  контейнера, systemd по cgroup, OOM), сегмент остаётся до перезагрузки или ручного удаления
  (`rm /dev/shm/<имя>` на Linux). Канал снимков на Linux имён не создаёт (memfd) и после себя ничего не оставляет.

Полное описание API и особенностей владения буферами: [02-api.md](doc/spec/02-api.md).

## Документация

- [doc/usage.md](doc/usage.md) — использование, устройство пакета, Pub/Sub (`shmobj.channel`) и сборка.
- [doc/status.md](doc/status.md) — текущее состояние, отклонения от ТЗ и замеры производительности.
- [doc/dev.md](doc/dev.md) — окружение разработки, тесты и конфигурация репозитория.
- [doc/spec/](doc/spec/00-overview.md) — требования, API, бинарный формат, архитектура и тестирование.
- `reference/` — эталонная реализация формата на Python; `golden/` — эталонные сегменты.

## Статус проекта

Текущая версия — **v1, read-only**. Зарезервированные имена для будущего API с изменяемыми структурами (`SharedDict`,
`SharedList`, `create_rw`, `attach_rw`, `freeze`, `thaw`) пока не реализованы.
