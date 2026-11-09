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

Колесо, sdist и переносимые сборки — [build.md](build.md).

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
pytest                            # tests/: golden, сверка с эталоном, API, жизненный цикл, канал, latest, примеры
```

Перегенерация `golden/*.bin` эталоном: `python3 golden/gen_golden.py`. Что покрыто тестами и чего нет —
[status.md](status.md#тесты).

## Источники истины

1. [format.md](format.md) — формат.
2. `reference/shmobj_ref.py` — исполняемая трактовка формата: сборка, чтение, `get_many`, `iter_*`, `materialize`,
   `validate`. Расхождение с `format.md` — дефект обоих, решение записывается в `format.md`; формат меняется только в
   обоих сразу.
3. `python/shmobj/__init__.pyi`, `channel/__init__.pyi`, `latest.pyi` — контракт API; [api.md](api.md) — семантика. Жизненный
   цикл `Segment`, `repr`, отказ `pickle`, представления `keys/values/items`, канал и `latest` есть только там.

## Порядок в модуле

Константы — сразу после импортов. Функция или метод стоит над тем, что её вызывает:
вызываемая из одного места — прямо над ним, общая — над первым вызовом, несколько — в порядке
вызова. Образец — `python/shmobj/channel/`.

## Раскладка

| Путь | Содержание |
|---|---|
| `crates/shmobj-core` | Формат без Python и без `unsafe`: `format/` (общее с будущим RW), `ro/` (чтение, сборка, `validate`) |
| `crates/shmobj-py` | cdylib `shmobj._core` (PyO3): `Segment`, прокси, итераторы, сборка из Python-объектов; весь `unsafe` здесь |
| `python/shmobj` | Пакет: реэкспорт `_core`, регистрация в ABC, `create`/`attach` поверх `SharedMemory` (`_shm.py`), канал снимков (`channel/`), последний снимок по имени (`latest.py`), `.pyi` |
| `tests/` | pytest; `conftest.py` добавляет `reference/` и `golden/` в `sys.path` |
| `examples/` | Рабочие скрипты с обвязкой ([examples/README.md](../examples/README.md)) — та же механика, что в примерах README; их прогоняет `tests/test_examples.py` |
| `reference/`, `golden/` | Эталон формата на Python (`pytest reference/`) и эталонные сегменты с входными данными (`cases.py`) |
| `doc/` | Руководство, справочник API, формат, архитектура, сборка, разработка, состояние |

## Конфигурация репозитория

- Лицензия `MIT OR Apache-2.0`: `license` в `Cargo.toml` (workspace) и `pyproject.toml`; тексты — `LICENSE-MIT`,
  `LICENSE-APACHE`, `license-files` кладёт их в колесо (`.dist-info/licenses/`) и sdist. Автор и ссылки на GitHub —
  строка copyright в `LICENSE-MIT`, `authors`/`repository` в `Cargo.toml` (workspace), `authors` и `[project.urls]`
  в `pyproject.toml`; текст `LICENSE-APACHE` не меняется.
- `rustfmt.toml`: `max_width = 120`.
- `shmobj-core`: `[lints]` запрещают `unsafe`, `panic`, `unwrap`, индексацию без проверки.
- `shmobj-py` не зависит от `xxhash-rust` напрямую: хэш считается в `shmobj-core`.
- `pyproject.toml`: `python-packages = ["pydevd_plugins"]` — плагин отладчика из `python/pydevd_plugins`
  едет в колесо рядом с `shmobj`. В `pydevd_plugins/` и `extensions/` нет `__init__.py`: пакеты
  принадлежат pydevd, плагин — только часть пространства имён. Проверка — `tests/test_debugger.py`
  (нужен `pydevd-pycharm`, без него тест пропускается).
- CI (`.github/workflows/ci.yml`): `rust`, `python` на каждый пуш и PR; `wheels`, `sdist`, `release` — см. «Релиз».
