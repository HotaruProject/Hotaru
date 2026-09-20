from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def venv_python() -> Path:
    if os.name == "nt":
        return ROOT / ".venv" / "Scripts" / "python.exe"
    return ROOT / ".venv" / "bin" / "python"


def in_venv() -> bool:
    try:
        return Path(sys.executable).resolve() == venv_python().resolve()
    except OSError:
        return False


def is_cli() -> bool:
    orig = [str(part) for part in (getattr(sys, "orig_argv", None) or [])]
    for index, part in enumerate(orig):
        if part == "-m" and index + 1 < len(orig):
            mod = orig[index + 1]
            if mod == "hotaru" or mod.startswith("hotaru."):
                return True
    if not sys.argv:
        return False
    name = Path(sys.argv[0]).name
    if name in {"__main__.py", "hotaru"}:
        return True
    try:
        return Path(sys.argv[0]).resolve() == Path(__file__).resolve().parent / "__main__.py"
    except OSError:
        return False


def requirements() -> list[str]:
    try:
        import tomllib
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        specs = list(data.get("project", {}).get("dependencies", []))
        if specs:
            return specs
    except Exception:
        pass
    return ["goygram>=0.8.0", "basedpyright>=1.40.1"]


def _create_venv() -> None:
    dest = ROOT / ".venv"
    if venv_python().is_file():
        return
    uv = shutil.which("uv")
    if uv:
        result = subprocess.run([uv, "venv", str(dest)], cwd=ROOT)
        if result.returncode == 0 and venv_python().is_file():
            return
    try:
        import venv
        venv.create(dest, with_pip=True)
    except Exception:
        result = subprocess.run([sys.executable, "-m", "venv", str(dest)], cwd=ROOT)
        if result.returncode != 0:
            raise SystemExit("cannot create .venv (install python3-venv or uv)")
    subprocess.run([str(venv_python()), "-m", "ensurepip", "--upgrade"], cwd=ROOT, check=False)


def _install(specs: list[str]) -> None:
    if not specs:
        return
    py = str(venv_python())
    uv = shutil.which("uv")
    if uv:
        commands = [
            [uv, "pip", "install", "--python", py, *specs],
            [uv, "pip", "install", "--python", py, "-U", *specs],
        ]
    else:
        commands = [
            [py, "-m", "pip", "install", *specs],
            [py, "-m", "pip", "install", "-U", "--upgrade-strategy", "eager", *specs],
        ]
    output = ""
    for command in commands:
        proc = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        output = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            return
    tail = "\n".join(output.strip().splitlines()[-12:])
    raise SystemExit(f"dependency install failed:\n{tail}")


def maybe_reexec() -> None:
    if not is_cli():
        return
    if not in_venv():
        _create_venv()
        _install(requirements())
        py = str(venv_python())
        os.execv(py, [py, "-m", "hotaru", *sys.argv[1:]])
    try:
        import importlib.metadata as meta
        missing: list[str] = []
        for line in requirements():
            name = line.split(">=", 1)[0].split("==", 1)[0].split("[", 1)[0].strip()
            try:
                meta.version(name)
            except meta.PackageNotFoundError:
                missing.append(line)
        if missing:
            _install(missing)
    except SystemExit:
        raise
    except Exception:
        _install(requirements())
