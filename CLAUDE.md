# Заметки для работы в репозитории

- Документация — только о том, что есть и что нужно для дальнейшей работы.
- Спецификация: `doc/format.md`, `doc/api.md`, `doc/architecture.md`, `doc/rw.md`. Приоритет: `doc/format.md` → `reference/shmobj_ref.py` → `python/shmobj/__init__.pyi` (контракт API) → `doc/api.md`. Расхождение формата с эталоном — дефект обоих; решение записывается в `doc/format.md`.
- README — максимально простые примеры, в том числе разные способы сделать одно и то же; `examples/` — рабочие скрипты с обвязкой, их прогоняет `tests/test_examples.py`. Пример в README не обязан совпадать со скриптом: достаточно, что оба показывают одну механику библиотеки. Подробности — `doc/usage.md`.
- Состояние, решения и замеры: `doc/status.md` — обновлять при изменении возможностей или решения. Конфигурация и команды: `doc/dev.md` — обновлять при изменении конфигурации.
- Помощник — над вызывающим его кодом, константы — сразу после импортов (`doc/dev.md`, «Порядок в модуле»).
- `shmobj-core`: без `unsafe`, без паник и индексации без проверки (lints в `Cargo.toml`). `format/` не зависит от `ro/`.
- Сборка должна давать те же байты, что эталон (`tests/test_golden.py`, `tests/test_ref_compat.py`).
- Перед коммитом: `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test -p shmobj-core`, `maturin develop --release`, `pytest reference/`, `pytest`.
