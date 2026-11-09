# Примеры

Запуск из корня репозитория: `python examples/<файл>`. Скрипты в парах «писатель — читатель» запускаются в разных
терминалах в любом порядке и работают до Ctrl+C.

| Файл | Что показывает |
|---|---|
| `shared_pool.py` | Один набор на пул: `create` в создателе, имя сегмента — аргументом задачи, `attach` один раз на воркер (`functools.cache`) |
| `file_mmap.py` | Набор в файле: `dumps` один раз, `loads` поверх `mmap` в каждом воркере |
| `channel_writer.py` + `channel_reader.py` | Канал без asyncio: адрес, приём подписчиков в потоке, `connect(wait_writer=True)`, `wait()`, переподключение после ухода писателя |
| `channel_writer_async.py` + `channel_reader_async.py` | То же в asyncio: `accept_in_loop`, `connect_async`, `wait_async()` |
| `channel_spawn.py` | Без адреса: читатель запускает писателя (`channel_spawn_writer.py`) и передаёт ему конец `socketpair()` через `spawn_kwargs` |
| `channel_spawn_async.py` | То же в asyncio: `create_subprocess_exec`, колбэк `attach(callback)` |
| `channel_fanout.py` | Один писатель и три читателя-процесса; снимок собирается один раз на всех |
| `latest_writer.py` + `latest_reader.py` | `shmobj.latest`: снимок по имени, опрос `get()`, перезапуск писателя |

Писатели и читатели канала взаимозаменяемы: к `channel_writer.py` подключается и `channel_reader_async.py`.
Канал — Linux и Windows; `latest` — и macOS.
