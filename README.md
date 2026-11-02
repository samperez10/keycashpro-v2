# Keycash Solver

Rust/Ratatui terminal application with its Python service modules embedded in the executable through PyO3. Account databases, sessions, and saved credentials remain external private data.

## Releases

GitHub Actions builds three packages after every push to `main`:

- `keycash-solver-alpine-x86_64.tar.gz` — native Alpine/musl executable and installer.
- `KeycashSolver-Portable-x86_64.exe` — single-file portable Windows app with its Python 3.12 runtime embedded.
- `keycash-solver-termux-aarch64.tar.gz` — native Android/Termux ARM64 executable with bundled Python packages.

Tagged pushes such as `v1.1.0` also publish all packages to a GitHub Release. Ordinary pushes rebuild and self-test all platforms without publishing downloads.

## Alpine installation

For a public repository, install the latest release directly:

```sh
curl -fsSL https://github.com/samperez10/keycashpro-v2/releases/latest/download/install.sh -o install-keycash.sh
sh install-keycash.sh
```

Extract the archive on a matching x86_64 Alpine system, then run:

```sh
tar -xzf keycash-solver-alpine-x86_64.tar.gz
cd keycash-solver-alpine-x86_64
sh install.sh --detect
sh install.sh
~/.local/bin/keycash
```

The installer verifies package checksums and runs an offline embedded-Python self-test before installation. A root installation can add missing runtime packages automatically. Non-root users should install `python3`, `py3-httpx`, `py3-beautifulsoup4`, and `ca-certificates` first.

Application files are installed under `~/.local/lib/keycash-solver`; private data is kept separately under `~/.local/share/keycash-solver` and is preserved during updates. Use `--prefix DIR` to choose another prefix.

## Android / Termux installation

On a 64-bit ARM Android device, run the same bootstrap command inside Termux:

```sh
pkg install -y curl
curl -fsSL https://github.com/samperez10/keycashpro-v2/releases/latest/download/install.sh -o install-keycash.sh
sh install-keycash.sh
```

The script automatically selects the Termux build, verifies both release checksums and the package manifest, installs Python if necessary, and exposes the portable command as `keycash`. Application code is installed under `$PREFIX/lib/keycash-solver`; private data stays under `~/.local/share/keycash-solver` and is preserved across upgrades.

## Windows portable executable

Download and run `KeycashSolver-Portable-x86_64.exe`. No installer, ZIP extraction, Python installation, or adjacent dependency folder is required. The EXE contains the Rust app, Python runtime, and third-party packages.

The Windows launcher opens a fixed 72-column × 36-row compact semi-square console, giving the interface more horizontal room while disabling classic-console resize and maximize controls.

On first launch it automatically expands an internal versioned runtime cache under `%LOCALAPPDATA%\KeycashSolver\portable-runtime`; this is an implementation detail and requires no setup. Portable accounts and settings are stored beside the downloaded EXE in `KeycashSolver-data`, so move that folder with the EXE when transferring the app. Each GitHub build runs the final one-file EXE and its offline self-test on Windows.

## Development

Python 3.12+ with `httpx` and `beautifulsoup4`, Rust 1.88+, and the Python development library are required.

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -q
PYO3_PYTHON=$(command -v python) cargo test --locked
sh scripts/build.sh
```

For Alpine, build inside Alpine:

```sh
apk add build-base cargo rust python3-dev python3 py3-httpx py3-beautifulsoup4 ca-certificates binutils
sh scripts/build_alpine.sh
```

No application Python source is included as loose files in any release package. The source is compiled into the Rust executable with `include_str!`. Windows embeds its Python runtime and dependencies in the portable EXE. Alpine uses its system Python packages. The Termux archive bundles pure-Python dependencies and uses Termux's Python runtime.
