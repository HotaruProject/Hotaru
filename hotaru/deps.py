import asyncio
import importlib
import importlib.metadata
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DependencyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
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


def version_tuple(value: str) -> tuple:
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
        base = floor[:2] if len(floor) == 2 else list(floor[: len(floor) - 1])
        base[-1] = base[-1] + 1
        return floor <= version_tuple(installed) < tuple(base)
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


def installed_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    except Exception:
        return None


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
    conda = "CONDA_PREFIX" in os.environ
    uv = _tool_path("uv")
    pdm = _tool_path("pdm")
    if conda and shutil.which("conda"):
        return "conda", [shutil.which("conda"), "install", "-y"]
    if uv:
        return "uv", [uv, "pip", "install", "--python", sys.executable]
    if pdm:
        return "pdm", [pdm, "add"]
    if importlib.util.find_spec("pip") is not None:
        command = [sys.executable, "-m", "pip", "install"]
        if not virtual and _is_externally_managed():
            command.append("--break-system-packages")
        return "pip", command
    raise DependencyError("no supported package manager found: uv, pdm, pip or conda")


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


async def ensure(requirements: list[str], *, on_log=None, timeout: float = 600.0) -> dict[str, Any]:
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


__all__ = ["DependencyError", "Requirement", "parse_requirement", "requirement_satisfied", "installed_version", "module_available", "import_name", "find_env_manager", "build_command", "ensure", "ensure_kernel", "version_tuple", "satisfies", "normalize_version"]
