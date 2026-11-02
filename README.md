# Keycash Solver

Rust/Ratatui terminal application with its Python service modules embedded in the executable through PyO3. Account databases, sessions, and saved credentials remain external private data.

## Releases

GitHub Actions builds two packages after every push to `main`:

- `keycash-solver-alpine-x86_64.tar.gz` — native Alpine/musl executable and installer.
- `keycash-solver-windows-x86_64.zip` — portable Windows executable with a private Python 3.12 runtime.

Tagged pushes such as `v0.1.0` also publish both packages to a GitHub Release. Ordinary pushes retain them as workflow artifacts.

## Alpine installation

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

## Windows portable package

Extract the entire ZIP. Run `keycash-solver.exe` for portable use, or run this from PowerShell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

The installer copies the complete runtime to `%LOCALAPPDATA%\Programs\KeycashSolver`, preserves `.state`, verifies the executable, and creates a Start-menu shortcut. The Windows build is produced and self-tested by GitHub Actions, but should remain marked experimental until exercised on a real Windows terminal.

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

No application Python source is included as loose files in either release package. The source is compiled into the Rust executable with `include_str!`; Python and its third-party libraries remain runtime components.
