from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "sanctuary" / "typecheck.json"
_PREAMBLE = "from __future__ import annotations\nfrom hotaru.response import ModuleContext\nctx: ModuleContext\n"


class TypeCheckError(RuntimeError):
    pass


def _run(paths: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "basedpyright", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stdout or proc.stderr or "pyright strict failed").strip()
        raise TypeCheckError(detail)


def _cache() -> dict[str, object]:
    try:
        value = json.loads(CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"hmods": {}}
    if not isinstance(value, dict):
        return {"hmods": {}}
    if not isinstance(value.get("hmods"), dict):
        value["hmods"] = {}
    return value


def _save(value: dict[str, object]) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.parent.chmod(0o700)
    fd, raw = tempfile.mkstemp(prefix="typecheck-", suffix=".json", dir=CACHE.parent)
    path = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
        os.replace(path, CACHE)
    finally:
        path.unlink(missing_ok=True)


def _environment() -> bytes:
    digest = hashlib.sha256()
    digest.update(sys.version.encode())
    digest.update(importlib.metadata.version("basedpyright").encode())
    digest.update(_PREAMBLE.encode())
    for name in ("pyrightconfig.json", "pyproject.toml", "uv.lock"):
        path = ROOT / name
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return digest.digest()


def _digest(parts: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256(_environment())
    for name, data in parts:
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _hmod_key(source: str, path: Path) -> str:
    return _digest([(path.name, (_PREAMBLE + source).encode("utf-8"))])


def check_kernel() -> None:
    paths = sorted((ROOT / "hotaru").rglob("*.py")) + sorted((ROOT / "relay").rglob("*.py"))
    key = _digest([(str(path.relative_to(ROOT)), path.read_bytes()) for path in paths])
    cache = _cache()
    if cache.get("kernel") == key:
        return
    _run(["hotaru", "relay"])
    cache["kernel"] = key
    _save(cache)


def check_hmod(source: str, path: Path) -> None:
    key = _hmod_key(source, path)
    cache = _cache()
    hmods = cache["hmods"]
    assert isinstance(hmods, dict)
    if hmods.get(str(path.resolve())) == key:
        return
    with tempfile.TemporaryDirectory(prefix="hotaru-pyright-") as tmp:
        dest = Path(tmp) / f"{path.stem}.py"
        dest.write_text(_PREAMBLE + source, encoding="utf-8")
        _run([str(dest)])
    hmods[str(path.resolve())] = key
    _save(cache)


def check_hmods(paths: list[Path]) -> None:
    cache = _cache()
    hmods = cache["hmods"]
    assert isinstance(hmods, dict)
    pending: list[tuple[Path, str, str]] = []
    for path in paths:
        source = path.read_text(encoding="utf-8")
        key = _hmod_key(source, path)
        if hmods.get(str(path.resolve())) != key:
            pending.append((path, source, key))
    if not pending:
        return
    with tempfile.TemporaryDirectory(prefix="hotaru-pyright-") as tmp:
        files = []
        for index, (path, source, _) in enumerate(pending):
            dest = Path(tmp) / f"{index}-{path.stem}.py"
            dest.write_text(_PREAMBLE + source, encoding="utf-8")
            files.append(str(dest))
        _run(files)
    for path, _, key in pending:
        hmods[str(path.resolve())] = key
    _save(cache)
