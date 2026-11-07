# shmobj

Дерево из `dict`, `list` и скаляров в одном сегменте общей памяти в компактном
бинарном формате. Процессы подключаются к сегменту по имени и читают данные через
прокси (`FrozenDict`, `FrozenList`) без десериализации и без копии в своей памяти.
Версия 1 — только чтение (RO). Ядро на Rust, привязки через PyO3, CPython ≥ 3.11 (abi3).

```python
import shmobj

seg = shmobj.create({"users": [{"id": 1, "name": "ann"}]})   # процесс-владелец
name = seg.name

with shmobj.attach(name) as s:                                # другой процесс
    users = s.root["users"]                                   # FrozenList
    users[0].get_many(["id", "name"])                         # [1, 'ann']

seg.close()
seg.unlink()
```

Без общей памяти: `shmobj.dumps(obj) -> bytes`, `shmobj.loads(buf)` поверх `bytes`/`mmap`/любого буфера.

Канал снимков (Linux, Windows): `shmobj.channel.Publisher.publish(obj)` в одном процессе,
`Subscriber.latest` в другом — на Linux запечатанный memfd по сокету, на Windows именованный снимок
с подтверждением; после падения сторон в ОС ничего не остаётся ([doc/usage.md](doc/usage.md#канал-снимков-linux-windows)).

## Жизненный цикл сегмента

- Прокси держат сильную ссылку на `Segment`: буфер не освобождается, пока жив хотя бы один прокси.
- `close()` закрывает сегмент в этом процессе; любое обращение через старые прокси даёт `SegmentClosedError`. Повторный `close()` ничего не делает. `with` вызывает `close()`, но не `unlink()`.
- `unlink()` удаляет сегмент из ОС; вызывает тот, кто решает, что данные больше не нужны (обычно создатель). Уже подключённые процессы работают до своего `close()`.
- `attach()` не регистрирует сегмент в `resource_tracker` (3.13+: `track=False`, 3.11–3.12: снятие регистрации), поэтому выход читателя не удаляет чужой сегмент.
- Windows: сегмент живёт, пока открыт хотя бы один дескриптор; `unlink()` ничего не делает.
- `loads(buf)` удерживает экспорт буфера: пока живы прокси, `mmap.close()` / `SharedMemory.close()` у исходного объекта дают `BufferError`.
- Данные сегмента после публикации никто не меняет — это контракт; граничные проверки при чтении сохраняются всегда.

## Сборка wheels

Из корня репозитория, на Linux; нужны Rust stable и Python ≥ 3.11. Подробности — [doc/usage.md](doc/usage.md#сборка).

1. Версия — `version` в `crates/shmobj-py/Cargo.toml` и `crates/shmobj-core/Cargo.toml`, затем
   `cargo update -w`. Wheel с прежней версией pip без `--force-reinstall` не переставит.
2. Проверки — список в [doc/dev.md](doc/dev.md#проверки-то-же-что-в-ci).
3. Один раз — инструменты:
   ```bash
   pip install maturin ziglang
   rustup target add x86_64-pc-windows-gnu
   apt-get install gcc-mingw-w64-x86-64
   ```
4. Linux x86_64, glibc ≥ 2.17:
   ```bash
   maturin build --release --zig --compatibility manylinux2014 --out dist
   ```
5. Windows x64:
   ```bash
   maturin build --release --strip --target x86_64-pc-windows-gnu --out dist
   ```
6. В `dist/` — `shmobj-<версия>-cp311-abi3-manylinux2014_x86_64.manylinux_2_17_x86_64.whl` и
   `shmobj-<версия>-cp311-abi3-win_amd64.whl`, один wheel на все CPython ≥ 3.11. В каждом —
   `shmobj/` и `pydevd_plugins/extensions/pydevd_plugin_shmobj.py` (`unzip -l`).

На Windows wheel для неё же — `maturin build --release --out dist`. Wheels для всех платформ
собирает задача `wheels` в CI (артефакты `wheel-*`).

## Документация

- [doc/usage.md](doc/usage.md) — как устроен пакет, сборка колеса, использование.
- [doc/status.md](doc/status.md) — что реализовано, отклонения от ТЗ, базовые замеры, следующие шаги.
- [doc/dev.md](doc/dev.md) — окружение, сборка, тесты, конфигурация репозитория.
- [doc/spec/](doc/spec/00-overview.md) — ТЗ: требования, API, нормативный формат, архитектура, тестирование, этапы.
- `reference/` — эталонная реализация формата на Python (оракул), `golden/` — эталонные сегменты.
