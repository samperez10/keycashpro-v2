#!/usr/bin/env python3
"""Create a verified Keycash release folder and optional tar archive."""
import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assemble(output, binary, system, architecture):
    version = tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"]
    result = subprocess.run(
        [str(binary), "--root", str(ROOT), "--self-test"],
        check=True, capture_output=True, text=True,
    )
    check = json.loads(result.stdout)
    if check.get("embedded_python") != "ok" or check.get("network_calls") != 0:
        raise RuntimeError("offline embedded-Python self-test failed")
    output.mkdir(parents=True)
    target = output / "keycash-solver"
    shutil.copy2(binary, target)
    target.chmod(0o755)
    shutil.copy2(ROOT / "scripts/install_alpine.sh", output / "install.sh")
    (output / "install.sh").chmod(0o755)
    build = {
        "name": "keycash-solver", "version": version, "author": "xnote12", "platform": system,
        "architecture": architecture,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_soabi": sysconfig.get_config_var("SOABI"),
        "python_source": "embedded", "entrypoint": "keycash-solver",
    }
    (output / "build.json").write_text(json.dumps(build, indent=2) + "\n")
    rows = []
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS":
            rows.append(f"{digest(path)}  {path.name}")
    (output / "SHA256SUMS").write_text("\n".join(rows) + "\n")


def package(args):
    name = f"keycash-solver-{args.platform}-{args.architecture}"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    destination = dist / name
    archive = dist / f"{name}.tar.gz"
    if destination.exists() or archive.exists():
        raise RuntimeError(f"release already exists: {name}")
    with tempfile.TemporaryDirectory(prefix=".keycash-package-", dir=dist) as temporary:
        staged = Path(temporary) / name
        assemble(staged, args.binary.resolve(), args.platform, args.architecture)
        if args.archive:
            with tarfile.open(Path(temporary) / archive.name, "w:gz") as bundle:
                bundle.add(staged, arcname=name)
        staged.rename(destination)
        if args.archive:
            staged_archive = Path(temporary) / archive.name
            staged_archive.rename(archive)
            archive.with_name(archive.name + ".sha256").write_text(
                f"{digest(archive)}  {archive.name}\n"
            )
    print(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--archive", action="store_true")
    package(parser.parse_args())
