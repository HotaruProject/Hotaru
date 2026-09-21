from __future__ import annotations

import asyncio
import ast
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


class DependencyError(RuntimeError):
    pass


@dataclass(frozen=True)
class Requirement:
    name: str
    extras: tuple[str, ...]
    specifier: str
    url: str | None

    @property
    def canonical(self) -> str:
        return self.name.lower().replace("_", "-")


def parse_requirement(line: str) -> Requirement:
    text = line.strip()
    if not text or text.startswith("#"):
        raise DependencyError("requirement is empty")
    url = None
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$", text)
    if match is None:
        raise DependencyError(f"requirement is invalid: {line}")
    name = match.group(1)
    extras = tuple(part.strip() for part in (match.group(2) or "").strip("[]").split(",") if part.strip())
    rest = match.group(3).strip()
    if "@ " in rest:
        head, tail = rest.split("@ ", 1)
        url = tail.strip()
        specifier = head.strip()
    else:
        specifier = rest
    return Requirement(name, extras, specifier, url)


_VERSION_RE = re.compile(r"^\s*v?(\d+(?:\.\d+){0,3})\s*(?:(a|b|rc|\.dev|\.post)\s*(\d+)?)?\s*$")
_SPEC_RE = re.compile(r"(===|==|!=|>=|<=|>|<|~=)\s*([^,;\s]+)")
_MARKER_RE = re.compile(r"^(python_version|python_full_version)\s*(>=|<=|==|!=|>|<)\s*(['\"])([^'\"]+)\3$")


def normalize_version(raw: str) -> str:
    text = raw.strip().lower()
    parts = text.split(".*")
    if len(parts) == 2 and parts[0].isdigit() is False:
        pass
    match = _VERSION_RE.match(text)
    if match is None:
        return text
    numbers = match.group(1).split(".")
    while len(numbers) < 3:
        numbers.append("0")
    suffix = ""
    if match.group(2):
        suffix = f"{match.group(2)}{match.group(3) or 0}"
    return ".".join(numbers) + suffix


def version_tuple(value: str) -> tuple[Any, ...]:
    text = normalize_version(value)
    pieces = []
    current = ""
    kind = None
    digits = re.match(r"^(\d+(?:\.\d+){0,3})(.*)$", text)
    if digits:
        for part in digits.group(1).split("."):
            pieces.append(int(part))
        tail = digits.group(2)
        order = {"a": -3, "b": -2, "rc": -1, "": 0, ".post": 1, ".dev": -4}
        match = re.match(r"^(a|b|rc|\.dev|\.post)(\d*)$", tail)
        if match:
            kind = match.group(1)
            number = int(match.group(2) or 0)
            pieces.append(order.get(kind, 0) * 1000 + number if kind != ".post" else 1000 + number)
        elif tail:
            pieces.append(tail)
    else:
        pieces.append(text)
    return tuple(pieces)


def satisfies(installed: str, operator: str, wanted: str) -> bool:
    if operator == "~=":
        floor = version_tuple(wanted)
        if len(floor) < 2:
            return False
        base_parts: list[Any] = list(floor[:2] if len(floor) == 2 else floor[: len(floor) - 1])
        base_parts[-1] = int(base_parts[-1]) + 1
        return floor <= version_tuple(installed) < tuple(base_parts)
    left = version_tuple(installed)
    right = version_tuple(wanted)
    if operator == "==":
        if wanted.endswith(".*"):
            prefix = wanted[:-2]
            return normalize_version(installed).startswith(normalize_version(prefix))
        return left == right
    if operator == "===":
        return installed.strip() == wanted.strip()
    if operator == "!=":
        if wanted.endswith(".*"):
            prefix = wanted[:-2]
            return not normalize_version(installed).startswith(normalize_version(prefix))
        return left != right
    if operator == ">=":
        return left >= right
    if operator == "<=":
        return left <= right
    if operator == ">":
        return left > right
    if operator == "<":
        return left < right
    raise DependencyError(f"operator is unsupported: {operator}")


def requirement_satisfied(requirement: Requirement, installed: str) -> bool:
    if requirement.url is not None:
        return False
    if not requirement.specifier:
        return True
    for operator, wanted in _SPEC_RE.findall(requirement.specifier):
        if not satisfies(installed, operator, wanted.strip()):
            return False
    return True


def requirement_applies(line: str) -> bool:
    if ";" not in line:
        return True
    marker = line.split(";", 1)[1].strip()
    for expression in re.split(r"\s+and\s+", marker):
        match = _MARKER_RE.fullmatch(expression.strip())
        if match is None:
            return True
        width = 2 if match.group(1) == "python_version" else 3
        current = ".".join(str(part) for part in sys.version_info[:width])
        if not satisfies(current, match.group(2), match.group(4)):
            return False
    return True


def installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


def _installed_version_fresh(name: str) -> str | None:
    code = "import importlib.metadata,sys; print(importlib.metadata.version(sys.argv[1]))"
    result = subprocess.run([sys.executable, "-c", code, name], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def module_available(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


_IMPORT_ALIASES = {
    "msgpack": "msgpack",
    "pillow": "PIL",
    "beautifulsoup4": "bs4",
    "pyyaml": "yaml",
    "opencv-python": "cv2",
    "scikit-image": "skimage",
    "pymongo": "pymongo",
    "aiohttp": "aiohttp",
}


def import_name(requirement: Requirement) -> str:
    return _IMPORT_ALIASES.get(requirement.canonical, requirement.name)


def _tool_path(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for base in (Path.home() / ".local" / "bin", Path("/usr/local/bin"), Path("/opt")):
        candidate = base / name
        if base.name == "opt":
            candidate = base / name / "bin" / name
        try:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def find_env_manager() -> tuple[str, list[str]]:
    prefix = getattr(sys, "prefix", "")
    base = getattr(sys, "base_prefix", "")
    virtual = prefix != base
    if not virtual:
        raise DependencyError("refusing to install into system Python")
    uv = _tool_path("uv")
    if uv:
        return "uv", [uv, "pip", "install", "--python", sys.executable]
    if importlib.util.find_spec("pip") is not None:
        return "pip", [sys.executable, "-m", "pip", "install"]
    raise DependencyError("no supported package manager found: uv or pip")


def _is_externally_managed() -> bool:
    try:
        marker = Path(sys.prefix).joinpath("lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "EXTERNALLY-MANAGED")
        return marker.exists()
    except OSError:
        return False


def build_command(manager: tuple[str, list[str]], specs: list[str], *, upgrade: bool) -> list[str]:
    tool, prefix = manager
    command = list(prefix)
    if tool == "conda":
        command.extend(["install", "-y"])
        command.extend(specs)
        return command
    command.extend(specs)
    if upgrade and tool != "pdm":
        command.append("-U")
    return command


async def run_install(command: list[str], timeout: float = 600.0) -> tuple[bool, str]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        raw, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise DependencyError("dependency install timed out")
    output = raw.decode("utf-8", errors="replace")
    return process.returncode == 0, output


async def ensure(requirements: list[str], *, on_log: Any=None, timeout: float = 600.0) -> dict[str, Any]:
    if not requirements:
        return {"checked": 0, "installed": [], "upgraded": [], "manager": None, "resolved": {}}
    manager = find_env_manager()
    resolved: dict[str, str] = {}
    missing: list[str] = []
    upgrades: list[str] = []
    for line in requirements:
        requirement = parse_requirement(line)
        canonical = requirement.canonical
        current = installed_version(canonical)
        if current is None:
            resolved[canonical] = "missing"
            missing.append(requirement.name if not requirement.url else f"{requirement.name}@{requirement.url}")
            continue
        resolved[canonical] = current
        if not requirement_satisfied(requirement, current):
            resolved[canonical] = f"{current} -> {requirement.specifier}"
            upgrades.append(f"{canonical}{requirement.specifier}")
    if not missing and not upgrades:
        return {"checked": len(requirements), "installed": [], "upgraded": [], "manager": manager[0], "resolved": resolved}
    specs = list(dict.fromkeys(missing + upgrades))
    command = build_command(manager, specs, upgrade=bool(upgrades))
    if on_log is not None:
        if asyncio.iscoroutinefunction(on_log):
            await on_log(f"deps: installing {', '.join(specs)} via {manager[0]}")
        else:
            on_log(f"deps: installing {', '.join(specs)} via {manager[0]}")
    ok, output = await run_install(command, timeout=timeout)
    importlib.invalidate_caches()
    if not ok:
        tail = "\n".join(output.strip().splitlines()[-6:])
        raise DependencyError(f"dependency install failed:\n{tail}")
    if on_log is not None:
        if asyncio.iscoroutinefunction(on_log):
            await on_log(f"deps: {', '.join(specs)} ready")
    return {"checked": len(requirements), "installed": missing, "upgraded": upgrades, "manager": manager[0], "resolved": resolved}


def ensure_kernel() -> dict[str, Any]:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(ensure(["goygram>=0.7.78"]))
    raise DependencyError("call ensure() from async context, ensure_kernel() is blocking")


def project_requirements(root: Path) -> list[str]:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("[project]", 1)[1].split("\n[", 1)[0] if "[project]" in text else ""
    start = project.find("dependencies")
    if start < 0:
        return []
    start = project.find("[", start)
    if start < 0:
        return []
    quote = ""
    escaped = False
    depth = 0
    for index in range(start, len(project)):
        char = project[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                value = ast.literal_eval(project[start:index + 1])
                if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                    raise DependencyError("project dependencies must be a string list")
                return value
    raise DependencyError("project dependencies are incomplete")


def _dependency_fingerprint(root: Path, requirements: list[str], locked: bool) -> str:
    digest = hashlib.sha256()
    digest.update(sys.version.encode("utf-8"))
    digest.update(os.name.encode("ascii"))
    for item in requirements:
        digest.update(item.encode("utf-8"))
        digest.update(b"\0")
    for name in (["pyproject.toml", "uv.lock"] if locked else ["pyproject.toml"]):
        path = root / name
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _dependency_marker(root: Path) -> Path:
    return root / ".venv" / ".hotaru-dependencies.json"


def _read_dependency_marker(root: Path) -> dict[str, Any]:
    try:
        value = json.loads(_dependency_marker(root).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_dependency_marker(root: Path, value: dict[str, Any]) -> None:
    target = _dependency_marker(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    path = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(path), str(target))
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _acquire_dependency_lock(root: Path, timeout: float = 600.0) -> Path:
    lock = root / ".venv" / ".hotaru-dependencies.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock.mkdir()
            (lock / "pid").write_text(str(os.getpid()), encoding="ascii")
            return lock
        except FileExistsError:
            try:
                pid = int((lock / "pid").read_text(encoding="ascii"))
                os.kill(pid, 0)
                alive = True
            except (OSError, ValueError):
                alive = False
            stale = not alive
            if stale:
                shutil.rmtree(str(lock), ignore_errors=True)
                continue
            if time.monotonic() >= deadline:
                raise DependencyError("dependency sync lock timed out")
            time.sleep(0.25)


def _requirements_satisfied(requirements: Sequence[str]) -> bool:
    for line in requirements:
        requirement = parse_requirement(line)
        current = installed_version(requirement.canonical)
        if current is None or not requirement_satisfied(requirement, current):
            return False
    return True


def _repair_environment_conflicts(requirements: Sequence[str]) -> None:
    check = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    if check.returncode == 0:
        return
    command = [sys.executable, "-m", "pip", "install", "--upgrade", "--upgrade-strategy", "eager", *requirements]
    repair = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if repair.returncode != 0:
        raise DependencyError(repair.stderr.strip() or repair.stdout.strip() or "dependency conflict repair failed")
    check = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    if check.returncode != 0:
        raise DependencyError(check.stdout.strip() or check.stderr.strip() or "dependency conflicts remain")


def ensure_project(root: Path) -> dict[str, Any]:
    requirements = [line.split(";", 1)[0].strip() for line in project_requirements(root) if requirement_applies(line)]
    uv = _tool_path("uv")
    locked = bool(uv and (root / "uv.lock").is_file())
    fingerprint = _dependency_fingerprint(root, requirements, locked)
    marker = _read_dependency_marker(root)
    satisfied = _requirements_satisfied(requirements)
    if marker.get("fingerprint") == fingerprint and satisfied:
        return {"changed": False, "manager": marker.get("manager"), "requirements": requirements}
    lock = _acquire_dependency_lock(root)
    try:
        marker = _read_dependency_marker(root)
        if marker.get("fingerprint") == fingerprint and _requirements_satisfied(requirements):
            return {"changed": False, "manager": marker.get("manager"), "requirements": requirements}
        if locked and uv is not None:
            command = [uv, "sync", "--locked", "--no-dev", "--python", sys.executable]
            result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=600)
            if result.returncode != 0:
                raise DependencyError(result.stderr.strip() or result.stdout.strip() or "uv sync failed")
            manager_name = "uv"
            changed = True
        else:
            result = asyncio.run(ensure(requirements))
            manager_name = result.get("manager")
            changed = bool(result.get("installed") or result.get("upgraded"))
        if manager_name == "pip":
            _repair_environment_conflicts(requirements)
        unresolved = []
        for line in requirements:
            requirement = parse_requirement(line)
            current = _installed_version_fresh(requirement.canonical)
            if current is None or not requirement_satisfied(requirement, current):
                unresolved.append(line)
        if unresolved:
            raise DependencyError("dependency resolver left requirements unsatisfied: " + ", ".join(unresolved))
        _write_dependency_marker(root, {
            "fingerprint": fingerprint,
            "manager": manager_name,
            "python": sys.version,
            "requirements": requirements,
        })
        return {"changed": changed, "manager": manager_name, "requirements": requirements}
    finally:
        shutil.rmtree(str(lock), ignore_errors=True)


__all__ = ["DependencyError", "Requirement", "parse_requirement", "requirement_satisfied", "installed_version", "module_available", "import_name", "find_env_manager", "build_command", "ensure", "ensure_kernel", "ensure_project", "project_requirements", "version_tuple", "satisfies", "normalize_version"]
