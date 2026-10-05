from __future__ import annotations

import asyncio
import ast
import gzip
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


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


def version_tuple(value: str) -> tuple[int | str, ...]:
    text = normalize_version(value)
    pieces: list[int | str] = []
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


def _recover_goygram_build(output: str) -> bool:
    text = output.lower()
    build = bool(re.search(
        r"(?:failed to build|failed building wheel for)\s+[`'\"]?goygram\b|"
        r"building wheel for goygram[^\n]*(?:error|failed)|"
        r"goygram[^\n/\\]*[/\\]ext_rust[/\\]cargo.toml", text,
    ))
    source = re.split(r"(?m)^(?:collecting|processing) ", text)[-1]
    if re.search(r"\bgoygram\b", source.splitlines()[0] if source else "") and re.search(
        r"(?:installing build dependencies|preparing metadata)[^\n]*(?:error|failed)", source,
    ):
        build = True
    missing = re.search(
        r"(?:can't find|cannot find|could not find|unable to find|no such file or directory)[^\n]*(?:rust compiler|rustc|cargo)|"
        r"(?:rust|cargo)[^\n]*(?:not installed|not found|not on path|not in.{0,20}path)|"
        r"linker [`'\"]?(?:cc|gcc|clang)[`'\"]? not found|"
        r"(?:command|executable) [`'\"]?(?:cc|gcc|clang)[`'\"]?[^\n]*(?:no such file|not found)", text,
    )
    if not build or not missing or re.search(
        r"failed to (?:download|fetch)|could not resolve|connection (?:refused|reset)|"
        r"network is unreachable|certificate verify failed|\b(?:401|403)\b|"
        r"no matching distribution|requires rustc|rustc[^\n]*not supported", text,
    ):
        return False
    if os.environ.get("TERMUX_VERSION") or "/com.termux/" in os.environ.get("PREFIX", ""):
        command = ["pkg", "install", "-y", "rust", "clang"]
        privileged = False
    else:
        try:
            release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
        except OSError:
            release = {}
        distro = release.get("ID", "").strip('"')
        commands = {
            "debian": ["apt-get", "install", "-y", "rustc", "cargo", "build-essential"],
            "ubuntu": ["apt-get", "install", "-y", "rustc", "cargo", "build-essential"],
            "alpine": ["apk", "add", "rust", "cargo", "build-base"],
            "fedora": ["dnf", "install", "-y", "rust", "cargo", "gcc"],
            "arch": ["pacman", "-S", "--needed", "--noconfirm", "rust", "base-devel"],
        }
        command = commands.get(distro, []) if sys.platform.startswith("linux") else []
        privileged = True
    if not command or not shutil.which(command[0]):
        print("goygram needs Rust/Cargo and a C linker; install the native toolchain for your platform and retry.", file=sys.stderr)
        return False
    if privileged and (not hasattr(os, "geteuid") or os.geteuid() != 0):
        print("goygram build prerequisites require administrator installation: " + " ".join(command), file=sys.stderr)
        return False
    print("goygram: installing missing native build prerequisites: " + " ".join(command), file=sys.stderr)
    try:
        return subprocess.run(command, timeout=600).returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"goygram build prerequisites failed: {exc}", file=sys.stderr)
        return False


def run_project_install(command: list[str], *, cwd: Path | None = None, timeout: float = 600.0) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode and _recover_goygram_build((result.stdout or "") + (result.stderr or "")):
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return result


async def run_install(command: list[str], timeout: float = 600.0, *, recover: bool = False) -> tuple[bool, str]:
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
    if process.returncode and recover:
        repaired = await asyncio.get_running_loop().run_in_executor(None, _recover_goygram_build, output)
        if repaired:
            return await run_install(command, timeout=timeout, recover=False)
    return process.returncode == 0, output


async def ensure(requirements: list[str], *, on_log: Any=None, timeout: float = 600.0, recover: bool = False) -> dict[str, Any]:
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
    ok, output = await run_install(command, timeout=timeout, recover=recover)
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
        asyncio.get_running_loop()
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
                value = cast(object, ast.literal_eval(project[start:index + 1]))
                if not isinstance(value, list) or not all(isinstance(item, str) for item in cast('list[object]', value)):
                    raise DependencyError("project dependencies must be a string list")
                return cast('list[str]', value)
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
        value = cast(object, json.loads(_dependency_marker(root).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return {}
    return cast('dict[str, Any]', value) if isinstance(value, dict) else {}


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
    repair = run_project_install(command)
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
            result = run_project_install(command, cwd=root)
            if result.returncode != 0:
                raise DependencyError(result.stderr.strip() or result.stdout.strip() or "uv sync failed")
            manager_name = "uv"
            changed = True
        else:
            result = asyncio.run(ensure(requirements, recover=True))
            manager_name = result.get("manager")
            changed = bool(result.get("installed") or result.get("upgraded"))
        if manager_name == "pip":
            _repair_environment_conflicts(requirements)
        unresolved: list[str] = []
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


_MODULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PACKAGE_SPEC_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?:(?:===|==|!=|>=|<=|>|<|~=)[A-Za-z0-9._*+!-]+"
    r"(?:,(?:===|==|!=|>=|<=|>|<|~=)[A-Za-z0-9._*+!-]+)*)?$"
)
_RESERVED_IMPORTS = frozenset({"hotaru", "relay", "goygram", "site", "sitecustomize", "usercustomize"})
_IMPORT_CACHE_NAME = ".import-names.json"
_INDEX_NAME = "pypi-index.txt"
_PROBE_TIMEOUT = 300.0
_INDEX_TIMEOUT = 15.0
_INDEX_FETCH_TIMEOUT = 300.0
_INDEX_MAX_AGE = 7 * 86400.0
_INDEX_CANDIDATES = 25
_PYPI_JSON = "https://pypi.org/pypi/{name}/json"
_PYPI_INDEX = "https://pypi.org/simple/"
_HTTP_AGENT = "hotaru-module-deps/1"
_CANDIDATE_TEMPLATES = ("{name}", "py{name}", "python-{name}", "{name}-py", "{name}-python")


def stdlib_names() -> frozenset[str]:
    declared = getattr(sys, "stdlib_module_names", None)
    if declared is not None:
        return frozenset(str(item) for item in cast("frozenset[str]", declared))
    base = Path(sysconfig.get_paths()["stdlib"])
    found = {str(item) for item in sys.builtin_module_names}
    for directory in (base, base / "lib-dynload"):
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir():
                if (entry / "__init__.py").is_file():
                    found.add(entry.name)
            elif entry.suffix in {".py", ".so", ".pyd"}:
                found.add(entry.stem if entry.suffix == ".py" else entry.name.split(".")[0])
    return frozenset(found)


def scan_imports(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise DependencyError(f"module source cannot be parsed: {exc.msg}") from exc
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.append(node.module)
    return list(dict.fromkeys(found))


def third_party_imports(source: str) -> list[str]:
    reserved = _RESERVED_IMPORTS | stdlib_names()
    return [item for item in scan_imports(source) if item.split(".")[0] not in reserved]


def safe_spec(line: str) -> str:
    text = line.strip()
    if not _PACKAGE_SPEC_RE.fullmatch(text):
        raise DependencyError(f"unsupported dependency requirement: {line}")
    if parse_requirement(text).canonical in _RESERVED_IMPORTS:
        raise DependencyError(f"reserved dependency name: {line}")
    return text


def _module_import_probe(target: Path, names: Sequence[str]) -> list[str]:
    if not names:
        return []
    code = (
        "import sys, importlib\n"
        "bad = []\n"
        "for name in sys.argv[1:]:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except BaseException:\n"
        "        bad.append(name)\n"
        "sys.stdout.write('\\n'.join(bad))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(target)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        result = subprocess.run([sys.executable, "-s", "-S", "-c", code, *names], capture_output=True, text=True, env=env, timeout=120.0)
    except (OSError, subprocess.SubprocessError):
        return list(names)
    if result.returncode != 0:
        return list(names)
    reported = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return [name for name in names if name in reported]


def _http_text(url: str, *, timeout: float, accept: str = "application/json") -> str | None:
    headers = {"User-Agent": _HTTP_AGENT, "Accept": accept, "Accept-Encoding": "gzip"}
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
    except (HTTPError, URLError, OSError, ValueError):
        return None


def _pypi_project(name: str, *, timeout: float = _INDEX_TIMEOUT) -> str | None:
    body = _http_text(_PYPI_JSON.format(name=quote(name, safe="")), timeout=timeout)
    if body is None:
        return None
    try:
        payload = cast(object, json.loads(body))
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    info = cast(dict[str, Any], payload).get("info")
    if isinstance(info, dict):
        project = cast(dict[str, Any], info).get("name")
        if isinstance(project, str) and project.strip():
            return project.strip()
    return name


def candidate_distributions(name: str) -> list[str]:
    base = name.casefold().replace("_", "-")
    candidates = [template.format(name=base) for template in _CANDIDATE_TEMPLATES]
    stemmed = re.sub(r"\d+$", "", base)
    if stemmed and stemmed != base:
        candidates.append(stemmed)
    return list(dict.fromkeys(candidates))


def flat_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def import_candidates(name: str) -> list[str]:
    """Local guesses for a dotted import path: the path itself plus its parts with py/python affixes."""
    parts = [part for part in name.split(".") if part]
    candidates: list[str] = []
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
        candidates.append(name.casefold().replace("_", "-"))
    for item in (parts[-1] if parts else "", parts[0] if parts else ""):
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item):
            candidates.extend(candidate_distributions(item))
    return list(dict.fromkeys(candidates))


def _pypi_index(root: Path, *, timeout: float = _INDEX_FETCH_TIMEOUT) -> Path | None:
    """Cached newline-separated snapshot of every distribution name on PyPI."""
    path = root / _INDEX_NAME
    try:
        if path.stat().st_mtime > time.time() - _INDEX_MAX_AGE:
            return path
    except OSError:
        pass
    body = _http_text(_PYPI_INDEX, timeout=timeout, accept="application/vnd.pypi.simple.v1+json")
    names: list[str] = []
    if body is not None:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            projects = cast("dict[str, Any]", payload).get("projects")
            if isinstance(projects, list):
                for item in cast("list[Any]", projects):
                    if isinstance(item, dict) and cast("dict[str, Any]", item).get("name"):
                        names.append(str(cast("dict[str, Any]", item)["name"]))
    if names:
        try:
            root.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".tmp")
            temp.write_text("\n".join(sorted(names)), encoding="utf-8")
            os.replace(str(temp), str(path))
            return path
        except OSError:
            return None
    return path if path.is_file() else None


def namespace_candidates(root: Path, name: str, *, limit: int = _INDEX_CANDIDATES) -> list[str]:
    """Distribution names from the whole PyPI namespace that contain the import name, best first."""
    key = flat_name(name)
    path = _pypi_index(root) if key else None
    if path is None:
        return []
    words = {word for word in re.split(r"[^a-z0-9]+", name.casefold()) if word}
    scored: list[tuple[tuple[int, int, int, str], str]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                project = line.strip()
                flat = flat_name(project)
                if not flat or key not in flat:
                    continue
                tokens = [word for word in re.split(r"[^a-z0-9]+", project.casefold()) if word]
                rank = 0 if flat == key else (1 if flat.startswith(key) or flat.endswith(key) else 2)
                scored.append(((rank, len([word for word in tokens if word not in words]), len(flat), project.casefold()), project))
    except OSError:
        return []
    scored.sort()
    return [project for _score, project in scored[:limit]]


def isolated_managers(target: Path) -> list[list[str]]:
    commands: list[list[str]] = []
    uv = _tool_path("uv")
    if uv:
        commands.append([uv, "pip", "install", "--target", str(target), "--no-build", "--python", sys.executable])
    commands.append([sys.executable, "-m", "pip", "install", "--target", str(target), "--only-binary", ":all:"])
    pdm = _tool_path("pdm")
    if pdm:
        commands.append([pdm, "run", "pip", "install", "--target", str(target), "--only-binary", ":all:"])
    return commands


def _install_into(target: Path, specs: Sequence[str], *, timeout: float) -> None:
    target.mkdir(parents=True, exist_ok=True)
    output = ""
    for prefix in isolated_managers(target):
        command = [*prefix, *specs]
        if not Path(command[0]).exists() and not shutil.which(command[0]):
            continue
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            output = str(exc)
            continue
        if result.returncode == 0:
            return
        output = ((result.stdout or "") + (result.stderr or "")).strip()
    raise DependencyError("module dependency install failed:\n" + "\n".join(output.splitlines()[-6:]))


def _probe_distribution(root: Path, candidate: str, verify: Sequence[str]) -> str | None:
    project = _pypi_project(candidate)
    if project is None:
        return None
    probe = Path(tempfile.mkdtemp(prefix="probe-", dir=str(root)))
    try:
        _install_into(probe, [project], timeout=_PROBE_TIMEOUT)
        if not _module_import_probe(probe, verify):
            return project
    except DependencyError:
        return None
    finally:
        shutil.rmtree(str(probe), ignore_errors=True)
    return None


def _resolve_distribution(root: Path, path: str) -> str | None:
    """Import path -> distribution, confirmed by importing the module from the installed candidate.

    ponytail: the namespace list is ranked and capped; each candidate costs one PyPI JSON check
    (which skips squatter names with no releases) and installs only when it actually has files.
    """
    for candidate in import_candidates(path):
        found = _probe_distribution(root, candidate, [path])
        if found:
            return found
    for candidate in namespace_candidates(root, path):
        found = _probe_distribution(root, candidate, [path])
        if found:
            return found
    return None


def _provided_modules(dist_info: Path) -> set[str]:
    """Top-level modules a distribution provides, from top_level.txt and its RECORD."""
    modules: set[str] = set()
    top_level = dist_info / "top_level.txt"
    try:
        if top_level.is_file():
            modules.update(line.strip() for line in top_level.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        pass
    try:
        lines = (dist_info / "RECORD").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        head = line.split(",", 1)[0].split("/", 1)[0]
        if not head or head.endswith((".dist-info", ".data")):
            continue
        module = head[:-3] if head.endswith(".py") else head
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module):
            modules.add(module)
    return modules


def _learn_distributions(target: Path, cache: dict[str, str]) -> None:
    """Remember which modules every installed distribution provides, so the next lookup is free."""
    for dist_info in sorted(target.glob("*.dist-info")):
        project = ""
        try:
            with (dist_info / "METADATA").open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("Name: "):
                        project = line[6:].strip()
                        break
        except OSError:
            continue
        if not project:
            continue
        for module in _provided_modules(dist_info):
            cache.setdefault(module.casefold(), project)


def environment_distributions() -> dict[str, str]:
    """Module -> distribution for everything already installed in this interpreter."""
    mapping: dict[str, str] = {}
    try:
        packages = importlib.metadata.packages_distributions()
    except Exception:
        return mapping
    for module, projects in packages.items():
        if projects:
            mapping.setdefault(str(module).casefold(), str(projects[0]))
    return mapping


def _import_cache(root: Path) -> dict[str, str]:
    try:
        value = cast(object, json.loads((root / _IMPORT_CACHE_NAME).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in cast("dict[object, object]", value).items()}


def _import_cache_write(root: Path, value: dict[str, str]) -> None:
    target = root / _IMPORT_CACHE_NAME
    root.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=target.name + ".", dir=str(root))
    path = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        os.replace(str(path), str(target))
    finally:
        path.unlink(missing_ok=True)


def target_versions(target: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for metadata in target.glob("*.dist-info/METADATA"):
        name = ""
        version = ""
        try:
            with metadata.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not name and line.startswith("Name: "):
                        name = line[6:].strip()
                    elif not version and line.startswith("Version: "):
                        version = line[9:].strip()
                    if name and version:
                        break
        except OSError:
            continue
        if name:
            versions[name.casefold().replace("_", "-")] = version
    return versions


def install_module_deps(root: Path, module_id: str, source: str, extra: Sequence[str] = (), *, timeout: float = 600.0) -> str | None:
    if not _MODULE_ID_RE.fullmatch(module_id):
        raise DependencyError(f"module id is not safe for dependency storage: {module_id}")
    explicit = [safe_spec(item) for item in extra]
    imports = third_party_imports(source)
    if not explicit and not imports:
        return None
    groups: dict[str, list[str]] = {}
    for path in imports:
        groups.setdefault(path.split(".")[0], []).append(path)
    target = root / module_id
    versions = target_versions(target) if target.is_dir() else {}
    pending = [spec for spec in explicit if not _spec_satisfied(spec, versions)]
    missing = set(_module_import_probe(target, imports)) if target.is_dir() else set(imports)
    cache = environment_distributions()
    for module, project in _import_cache(root).items():
        cache[module] = project
    if not pending and not missing:
        _learn_distributions(target, cache)
        _import_cache_write(root, cache)
        return str(target)
    root.mkdir(parents=True, exist_ok=True)
    unresolved: list[str] = []
    plan = list(pending)
    for paths in groups.values():
        for path in paths:
            if path not in missing:
                continue
            key = path.casefold()
            distribution = cache.get(key)
            if distribution is None:
                distribution = _resolve_distribution(root, path) or ""
            if not distribution:
                unresolved.append(path)
                continue
            cache[key] = distribution
            plan.append(distribution)
    if plan:
        _install_into(target, list(dict.fromkeys(plan)), timeout=timeout)
    _learn_distributions(target, cache)
    still = _module_import_probe(target, imports)
    if unresolved or still:
        raise DependencyError("module dependencies are unavailable: " + ", ".join(sorted({*unresolved, *still})))
    _import_cache_write(root, cache)
    return str(target)


def _spec_satisfied(spec: str, versions: dict[str, str]) -> bool:
    requirement = parse_requirement(spec)
    installed = versions.get(requirement.canonical)
    return installed is not None and requirement_satisfied(requirement, installed)


__all__ = ["DependencyError", "Requirement", "parse_requirement", "requirement_satisfied", "installed_version", "module_available", "import_name", "find_env_manager", "build_command", "ensure", "ensure_kernel", "ensure_project", "project_requirements", "version_tuple", "satisfies", "normalize_version", "stdlib_names", "scan_imports", "third_party_imports", "safe_spec", "candidate_distributions", "flat_name", "import_candidates", "namespace_candidates", "environment_distributions", "isolated_managers", "target_versions", "install_module_deps"]
