# Заметки для работы в репозитории

- Отвечать и писать документацию по-русски; документация — только о том, что есть и что нужно для дальнейшей работы.
- Спецификация: `doc/format.md`, `doc/api.md`, `doc/architecture.md`, `doc/rw.md`. Приоритет: `doc/format.md` → `reference/shmobj_ref.py` → `python/shmobj/__init__.pyi` (контракт API) → `doc/api.md`. Расхождение формата с эталоном — дефект обоих; решение записывается в `doc/format.md`.
- README — простые примеры (каждый есть рабочим в `examples/` и проверяется `tests/test_examples.py`); подробности — `doc/usage.md`.
- Состояние, решения и замеры: `doc/status.md` — обновлять при изменении возможностей или решения. Конфигурация и команды: `doc/dev.md` — обновлять при изменении конфигурации.
- Помощник — над вызывающим его кодом, константы — сразу после импортов (`doc/dev.md`, «Порядок в модуле»).
- `shmobj-core`: без `unsafe`, без паник и индексации без проверки (lints в `Cargo.toml`). `format/` не зависит от `ro/`.
- Сборка должна давать те же байты, что эталон (`tests/test_golden.py`, `tests/test_ref_compat.py`).
- Перед коммитом: `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test -p shmobj-core`, `maturin develop --release`, `pytest reference/`, `pytest`.
