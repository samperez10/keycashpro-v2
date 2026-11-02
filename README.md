# Keycash Pro

**Keycash Pro v1.1.0 by xnote12**

Terminal-based Keycash task solver for:

- Typing tasks
- Math tasks
- Emoji tasks

## Sneak peek

<img src="assets/keycash-sneak-peek.jpg" alt="Keycash Pro running in Termux" width="480">

## Termux installation

Supported platform: ARM64 Android using Termux.

```sh
pkg install -y curl
curl -fsSL https://github.com/samperez10/keycashpro-v2/releases/latest/download/install.sh -o install-keycash.sh
bash install-keycash.sh
keycash
```

The installer adds the `keycash` command. Account data is preserved when upgrading.

## Alpine Linux installation

Supported platform: Alpine Linux x86_64.

```sh
curl -fsSL https://github.com/samperez10/keycashpro-v2/releases/latest/download/install.sh -o install-keycash.sh
bash install-keycash.sh
keycash
```

If `keycash` is not immediately found after a non-root installation:

```sh
export PATH="$HOME/.local/bin:$PATH"
keycash
```

## Windows portable executable

[Download KeycashSolver-Portable-x86_64.exe](https://github.com/samperez10/keycashpro-v2/releases/latest/download/KeycashSolver-Portable-x86_64.exe)

No installer, ZIP extraction, Python installation, or adjacent dependency folder is required. The window uses a fixed, non-resizable 72 × 36 compact layout.
