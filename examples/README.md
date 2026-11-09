# Примеры

Запуск из корня репозитория: `python examples/<файл>`. Скрипты `*_pubsub` запускаются с ролью `writer` или `reader`
в разных терминалах, в любом порядке, и работают до Ctrl+C.

| Файл | Что показывает |
|---|---|
| `shared_pool.py` | Один набор на пул: `create` в создателе, `attach` в `initializer` воркеров |
| `file_mmap.py` | Набор в файле: `dumps` один раз, `loads` поверх `mmap` в каждом воркере |
| `channel_pubsub.py` | Канал без asyncio: адрес, приём подписчиков в потоке, `connect(wait_writer=True)`, `wait()`, переподключение после ухода писателя |
| `channel_pubsub_async.py` | То же в asyncio: `accept_in_loop`, `connect_async`, `wait_async()` |
| `channel_spawn.py` | Без адреса: читатель запускает писателя (этот же скрипт) и передаёт ему конец `socketpair()` через `spawn_kwargs` |
| `channel_spawn_async.py` | То же в asyncio: `create_subprocess_exec`, колбэк `attach(callback)` |
| `channel_fanout.py` | Один писатель и три читателя-процесса; снимок собирается один раз на всех |
| `latest_pubsub.py` | `shmobj.latest`: снимок по имени, опрос `get()`, перезапуск писателя |

Писатели и читатели канала взаимозаменяемы: к `channel_pubsub.py writer` подключается и
`channel_pubsub_async.py reader`. Канал — Linux и Windows; `latest` — и macOS.
