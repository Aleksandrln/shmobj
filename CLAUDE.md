# Заметки для работы в репозитории

- Отвечать и писать документацию по-русски; документация — только о том, что есть и что нужно для дальнейшей работы.
- ТЗ: `doc/spec/`. Приоритет: `03-format.md` → `reference/shmobj_ref.py` → `python/shmobj/__init__.pyi` (контракт API) → `02-api.md`. Расхождение формата с эталоном — дефект обоих; решение записывается в `03-format.md`.
- Состояние и отклонения: `doc/status.md` — обновлять при завершении этапа или изменении решения. Конфигурация и команды: `doc/dev.md` — обновлять при изменении конфигурации.
- Помощник — над вызывающим его кодом, константы — сразу после импортов (`doc/dev.md`, «Порядок в модуле»).
- `shmobj-core`: без `unsafe`, без паник и индексации без проверки (lints в `Cargo.toml`). `format/` не зависит от `ro/`.
- Сборка должна давать те же байты, что эталон (`tests/test_golden.py`, `tests/test_ref_compat.py`).
- Перед коммитом: `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test -p shmobj-core`, `maturin develop --release`, `pytest reference/`, `pytest`.
