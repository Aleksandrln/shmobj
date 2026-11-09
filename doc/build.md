# Сборка и устройство пакета

## Что внутри колеса

Колесо (`.whl`) — zip-архив, который `pip` распаковывает в `site-packages` без компиляции:

```
shmobj/__init__.py       # реэкспорт, create/attach
shmobj/_shm.py           # SharedMemory, resource_tracker
shmobj/channel/          # канал снимков (Linux, Windows): __init__.py и модули _*.py
shmobj/latest.py         # последний снимок по имени (любая ОС)
shmobj/_core.abi3.so     # скомпилированные shmobj-core + shmobj-py (на Windows — _core.pyd)
shmobj/__init__.pyi, channel/__init__.pyi, latest.pyi, py.typed   # типы и докстринги для IDE
pydevd_plugins/extensions/pydevd_plugin_shmobj.py   # прокси в отладчике PyCharm/VS Code
```

Rust-часть компилируется при сборке колеса в модуль расширения CPython: `import shmobj` загружает `_core.abi3.so`
как обычный модуль (PyO3 генерирует `PyInit__core`). Rust у пользователя не нужен.

Колесо привязано к ОС и архитектуре (`manylinux_2_34_x86_64`, `win_amd64`, `macosx_11_0_arm64`), но не к версии
Python: сборка с `abi3-py311` использует только стабильный ABI, одно колесо подходит для CPython 3.11 и новее.
Нет колеса для платформы — `pip` берёт sdist (`.tar.gz`) и собирает его через maturin, тогда нужен Rust.

Подсказки типов и докстринги для IDE — `.pyi`: PyCharm и Pylance берут их, а не `__doc__` модуля расширения.
Докстринги `///` в `crates/shmobj-py` видны в `help()`.

## Сборка из исходников

Установка Rust, `pip install` из репозитория и `maturin build` из клона — [README](../README.md#сборка-из-исходников).
`maturin build` вызывает cargo для `crates/shmobj-py`, кладёт библиотеку в пакет как `shmobj/_core.abi3.so` вместе с
`python/` и упаковывает колесо. `cargo build --release` даёт только `target/release/lib_core.so` (Linux) — это не пакет.

## Переносимые колёса

Колесо, собранное на Linux, получает тег по glibc этой машины (например, `manylinux_2_34`) и не ставится на системы
со старой glibc. Переносимое (glibc ≥ 2.17) — через zig, без Docker:

```bash
pip install maturin ziglang
maturin build --release --zig --compatibility manylinux2014 -o dist
```

Колесо Windows x64 из Linux (mingw-w64):

```bash
rustup target add x86_64-pc-windows-gnu
apt-get install gcc-mingw-w64-x86-64
maturin build --release --strip --target x86_64-pc-windows-gnu -o dist   # shmobj-*-cp311-abi3-win_amd64.whl
```

`_core.pyd` зависит только от `python3.dll` и системных dll (`objdump -p`). `--zig` для этой цели не годится:
maturin 1.16 пишет в `.def` экспорт `PyInit_shmobj._core`, и линковка падает.

Колёса всех платформ (Linux x86_64/aarch64 в manylinux-контейнере, Windows x64, macOS arm64) собирает CI —
см. [dev.md](dev.md#релиз).
