# Разработка

## Окружение

Нужны Rust stable, Python ≥ 3.11 и `maturin`.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install maturin pytest hypothesis xxhash
maturin develop --release        # собрать shmobj._core и поставить пакет в режиме editable
```

`maturin develop` без `--release` собирает debug-версию: она в ~10 раз медленнее,
для замеров скорости не подходит.

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

## Раскладка

| Путь | Содержание |
|---|---|
| `crates/shmobj-core` | Формат без Python и без `unsafe`: `format/` (общее с будущим RW), `ro/` (чтение, сборка, `validate`) |
| `crates/shmobj-py` | cdylib `shmobj._core` (PyO3): `Segment`, прокси, итераторы, сборка из Python-объектов; весь `unsafe` здесь |
| `python/shmobj` | Пакет: реэкспорт `_core`, регистрация в ABC, `create`/`attach` поверх `SharedMemory` (`_shm.py`), канал снимков (`channel.py`), `.pyi` |
| `tests/` | pytest; `conftest.py` добавляет `reference/` и `golden/` в `sys.path` |
| `reference/`, `golden/` | Эталон и эталонные сегменты из ТЗ |
| `doc/spec/` | ТЗ (ссылки внутри поправлены под эту раскладку) |

## Конфигурация репозитория

Отличия от заготовок `skeleton/` из ТЗ:

- `rustfmt.toml`: `max_width = 120`.
- Зависимость `zerocopy` не используется: заголовок разбирается через `from_le_bytes` (допустимая замена по `doc/spec/06-references.md`).
- `criterion` убран из dev-зависимостей до появления `benches/`.
- `shmobj-py` не зависит от `xxhash-rust` напрямую: хэш считается в `shmobj-core`.
- CI: задача `fuzz-smoke` убрана до появления `fuzz/` (cargo-fuzz). Остальные задачи — как в заготовке.
