# Разработка

## Окружение

Нужны Rust stable, Python ≥ 3.11 и `maturin`.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install maturin pytest hypothesis xxhash pydevd-pycharm
maturin develop --release        # собрать shmobj._core и поставить пакет в режиме editable
```

`maturin develop` без `--release` собирает debug-версию: она в ~10 раз медленнее,
для замеров скорости не подходит.

Rust без Python: `cargo build --release` собирает ядро (`crates/shmobj-core`) и расширение
(`crates/shmobj-py`, на Linux — `target/release/lib_core.so`). Эта библиотека — не пакет: модулем `shmobj._core` с Python-частью
из `python/` его делает maturin (`develop` или `build`). Колесо — `maturin build --release -o dist`, подробности —
[usage.md](usage.md#сборка).

Версия пакета — `version` в `crates/shmobj-py/Cargo.toml` (`pyproject.toml` берёт её оттуда, `dynamic`),
у `crates/shmobj-core` — та же. До первого выпуска — `0.1.0`.

## Релиз

CI (`.github/workflows/ci.yml`) на каждый пуш и PR гоняет только проверки (`rust`, `python`). Колёса (`wheels`) и
sdist (`sdist`) собираются для тега `v*` и при ручном запуске.

1. Поднять `version` в `crates/shmobj-py/Cargo.toml` и `crates/shmobj-core/Cargo.toml`, `cargo check` (обновит
   `Cargo.lock`), слить в `master`.
2. Тег на этом коммите `master`:

   ```bash
   git checkout master
   git pull
   git tag v0.1.0
   git push origin v0.1.0
   ```

3. CI прогоняет проверки, собирает колёса и sdist; задача `release` сверяет тег с версией в `Cargo.toml` и создаёт
   GitHub Release с этими файлами, описание — список PR с прошлого тега (`--generate-notes`). Результат — вкладка
   Releases репозитория.

Тег не совпал с версией или упала проверка — релиза нет; тег удаляют, исправляют и ставят заново:

```bash
git push origin :v0.1.0
git tag -d v0.1.0
```

Колёса без релиза — Actions → ci → Run workflow: файлы — в артефактах `wheel-*` и `sdist` этого прогона.

## Проверки (то же, что в CI)

```bash
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test -p shmobj-core
pytest reference/                 # тесты эталона
pytest                            # tests/: golden, сверка с эталоном, API, жизненный цикл
```

Перегенерация `golden/*.bin` эталоном: `python3 golden/gen_golden.py`.
Формат меняется только через `doc/spec/03-format.md` и `reference/shmobj_ref.py` одновременно.

## Порядок в модуле

Константы — сразу после импортов. Функция или метод стоит над тем, что её вызывает:
вызываемая из одного места — прямо над ним, общая — над первым вызовом, несколько — в порядке
вызова. Образец — `python/shmobj/channel.py`.

## Раскладка

| Путь | Содержание |
|---|---|
| `crates/shmobj-core` | Формат без Python и без `unsafe`: `format/` (общее с будущим RW), `ro/` (чтение, сборка, `validate`) |
| `crates/shmobj-py` | cdylib `shmobj._core` (PyO3): `Segment`, прокси, итераторы, сборка из Python-объектов; весь `unsafe` здесь |
| `python/shmobj` | Пакет: реэкспорт `_core`, регистрация в ABC, `create`/`attach` поверх `SharedMemory` (`_shm.py`), канал снимков (`channel.py`), последний снимок по имени (`latest.py`), `.pyi` |
| `tests/` | pytest; `conftest.py` добавляет `reference/` и `golden/` в `sys.path` |
| `examples/` | Запускаемые примеры канала (синхронный и asyncio-читатель, несколько читателей); их прогоняет `tests/test_examples.py` |
| `reference/`, `golden/` | Эталон и эталонные сегменты из ТЗ |
| `doc/spec/` | ТЗ (ссылки внутри поправлены под эту раскладку) |

## Конфигурация репозитория

Отличия от заготовок `skeleton/` из ТЗ:

- `rustfmt.toml`: `max_width = 120`.
- Зависимость `zerocopy` не используется: заголовок разбирается через `from_le_bytes` (допустимая замена по `doc/spec/06-references.md`).
- `criterion` убран из dev-зависимостей до появления `benches/`.
- `shmobj-py` не зависит от `xxhash-rust` напрямую: хэш считается в `shmobj-core`.
- `pyproject.toml`: `python-packages = ["pydevd_plugins"]` — плагин отладчика из `python/pydevd_plugins`
  едет в колесо рядом с `shmobj`. В `pydevd_plugins/` и `extensions/` нет `__init__.py`: пакеты
  принадлежат pydevd, плагин — только часть пространства имён. Проверка — `tests/test_debugger.py`
  (нужен `pydevd-pycharm`, без него тест пропускается).
- CI: задача `fuzz-smoke` убрана до появления `fuzz/` (cargo-fuzz); добавлены `sdist` и `release` (см. «Релиз»).
  Остальные задачи — как в заготовке.
