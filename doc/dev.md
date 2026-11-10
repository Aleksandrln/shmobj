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

Сборка колеса и sdist — [README](../README.md#сборка-из-исходников), переносимые сборки — [build.md](build.md).

Версия пакета — `version` в `crates/shmobj-py/Cargo.toml` (`pyproject.toml` берёт её оттуда, `dynamic`),
у `crates/shmobj-core` — та же. Выпуск — [CI и релиз](#ci-и-релиз).

## Проверки

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
- `pyproject.toml`: `readme = "README.md"` — описание пакета в метаданных колеса и sdist, его показывает PyPI.
  Ссылки на файлы репозитория в README — абсолютные (`https://github.com/Aleksandrln/shmobj/blob/master/...`):
  на PyPI относительные битые. `description` совпадает с About на GitHub; `classifiers` без `License ::` —
  лицензия задана выражением (PEP 639); `[project.urls]` — боковая панель на PyPI.
- `rustfmt.toml`: `max_width = 120`.
- `shmobj-core`: `[lints]` запрещают `unsafe`, `panic`, `unwrap`, индексацию без проверки.
- `shmobj-py` не зависит от `xxhash-rust` напрямую: хэш считается в `shmobj-core`.
- `pyproject.toml`: `python-packages = ["pydevd_plugins"]` — плагин отладчика из `python/pydevd_plugins`
  едет в колесо рядом с `shmobj`. В `pydevd_plugins/` и `extensions/` нет `__init__.py`: пакеты
  принадлежат pydevd, плагин — только часть пространства имён. Проверка — `tests/test_debugger.py`
  (нужен `pydevd-pycharm`, без него тест пропускается).

## CI и релиз

`.github/workflows/ci.yml`:

| Job | Когда | Что |
|---|---|---|
| `rust`, `python` | push в `master`, PR, тег, ручной запуск | fmt, clippy, тесты ядра; эталон и pytest на Linux, Windows, macOS × Python 3.11, 3.14 |
| `wheels`, `sdist` | тег `v*`, ручной запуск | Колёса abi3 cp311: Linux x86_64/aarch64, Windows x64, macOS arm64/x86_64 (x86_64 — кросс-сборка на arm-раннере); sdist |
| `version` | тег `v*` | Коммит тега входит в `master`, версия тега = `version` в `crates/shmobj-py/Cargo.toml` |
| `release` | тег `v*`, после `version`, `wheels`, `sdist` | Файлы в GitHub Release; релиз, созданный в интерфейсе вместе с тегом, дополняется |
| `pypi` | тег `v*`, после `version`, `wheels`, `sdist` | Выкладка на pypi.org, окружение `pypi` |
| `testpypi` | ручной запуск с `publish=testpypi` | Выкладка на test.pypi.org, окружение `testpypi`; уже выложенные файлы пропускаются |

Публикация — Trusted Publishing (OIDC), токенов в секретах нет; `pypa/gh-action-pypi-publish` проверяет метаданные
и прикладывает аттестации (PEP 740). Версию на PyPI нельзя перезаписать, номер удалённой нельзя использовать повторно.

Настройка вне репозитория:

- pypi.org и test.pypi.org, *Publishing*: проект `shmobj`, owner `Aleksandrln`, repository `shmobj`, workflow
  `ci.yml`, environment `pypi` и `testpypi` соответственно.
- GitHub, *Settings → Environments*: `pypi` (*Deployment branches and tags* — только теги `v*`; по желанию
  *Required reviewers*) и `testpypi`.
- GitHub, *Settings → Rules → Rulesets*: tag ruleset на `v*` — *Restrict creations*, *updates*, *deletions*, в
  *Bypass list* — Repository admin. Правило окружения проверяет только имя тега; принадлежность коммита `master` —
  job `version`.

Порядок релиза:

1. Поднять `version` в `crates/shmobj-py/Cargo.toml` и `crates/shmobj-core/Cargo.toml` (и в `Cargo.lock`), коммит
   в `master`.
2. *Actions → ci → Run workflow* с `publish=testpypi`, проверка установки в чистом venv:
   `pip install -i https://test.pypi.org/simple/ shmobj`.
3. `git tag v<версия> && git push origin v<версия>` → GitHub Release и PyPI.
