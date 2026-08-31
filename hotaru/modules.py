from __future__ import annotations

import ast
import hashlib
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ModuleValidationError(ValueError):
    pass


FORBIDDEN_IMPORTS = frozenset({
    "telethon",
    "pyrogram",
    "tgcrypto",
    "herokutl",
    "hikkatlobs",
    "kurimypyrogram",
    "mtproto",
    "gramjs",
    "telegram",
    "aiogram",
    "goygram",
    "relay",
    "hotaru",
})

FORBIDDEN_NAME_PARTS = (
    "telethon",
    "pyrogram",
    "herokutl",
    "gramjs",
    "goygram-fork",
    "goygram_fork",
)


def _module_chain(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return [node.module] if node.module else []


def _is_forbidden(chain: list[str]) -> bool:
    for name in chain:
        root = name.split(".")[0].casefold()
        if root in FORBIDDEN_IMPORTS:
            return True
        if any(part in name.casefold() for part in FORBIDDEN_NAME_PARTS):
            return True
    return False


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    module_id: str
    version: str
    description: str
    commands: tuple[str, ...]
    capabilities: tuple[str, ...]
    sandbox: bool = False


@dataclass(frozen=True, slots=True)
class LoadedModule:
    path: Path
    digest: str
    source: str
    manifest: ModuleManifest


class HmodLoader:
    def __init__(self, *, max_bytes: int = 256 * 1024) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes

    def load(self, path: str | Path) -> LoadedModule:
        candidate = Path(path)
        self._validate_path(candidate)
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise ModuleValidationError("module cannot be inspected") from exc
        if size > self.max_bytes:
            raise ModuleValidationError("module exceeds the size limit")
        try:
            source = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ModuleValidationError("module is not valid UTF-8") from exc
        try:
            tree = ast.parse(source, filename=str(candidate), mode="exec")
            manifest = self._manifest(tree)
            self._validate_imports(tree)
            compile(tree, str(candidate), "exec")
        except (SyntaxError, ValueError, TypeError) as exc:
            raise ModuleValidationError("module failed validation") from exc
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return LoadedModule(candidate, digest, source, manifest)

    @staticmethod
    def _validate_imports(tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if _is_forbidden(_module_chain(node)):
                    raise ModuleValidationError(
                        "module imports a Telegram client library that could hijack the session; "
                        "use ctx.mt / cap('mt') instead"
                    )

    def _validate_path(self, path: Path) -> None:
        if path.suffix != ".hmod":
            raise ModuleValidationError("module must use the .hmod extension")
        if path.is_symlink() or not path.is_file():
            raise ModuleValidationError("module path must be a regular file")

    @staticmethod
    def _manifest(tree: ast.Module) -> ModuleManifest:
        if not tree.body or not isinstance(tree.body[0], (ast.Assign, ast.AnnAssign)):
            raise ModuleValidationError("HOTARU must be the first statement")
        statement = tree.body[0]
        if isinstance(statement, ast.Assign):
            if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                raise ModuleValidationError("first statement must assign HOTARU")
            if statement.targets[0].id != "HOTARU":
                raise ModuleValidationError("first statement must assign HOTARU")
            value = statement.value
        else:
            if not isinstance(statement.target, ast.Name) or statement.target.id != "HOTARU":
                raise ModuleValidationError("first statement must assign HOTARU")
            value = statement.value
        try:
            raw = ast.literal_eval(value)
        except (ValueError, TypeError) as exc:
            raise ModuleValidationError("HOTARU must be a literal mapping") from exc
        if not isinstance(raw, dict):
            raise ModuleValidationError("HOTARU must be a mapping")
        return HmodLoader._build_manifest(raw)

    @staticmethod
    def _build_manifest(raw: dict[Any, Any]) -> ModuleManifest:
        required = ("id", "version", "description", "commands", "capabilities")
        if any(key not in raw for key in required):
            raise ModuleValidationError("manifest is missing required fields")
        module_id = raw["id"]
        version = raw["version"]
        description = raw["description"]
        commands = raw["commands"]
        capabilities = raw["capabilities"]
        if not isinstance(module_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", module_id):
            raise ModuleValidationError("manifest id is invalid")
        if not isinstance(version, str) or not version:
            raise ModuleValidationError("manifest version is invalid")
        if not isinstance(description, str):
            raise ModuleValidationError("manifest description is invalid")
        if not HmodLoader._strings(commands) or not HmodLoader._strings(capabilities):
            raise ModuleValidationError("manifest lists must contain strings")
        sandbox = bool(raw.get("sandbox", False))
        return ModuleManifest(module_id, version, description, tuple(commands), tuple(capabilities), sandbox)

    @staticmethod
    def _strings(value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


class ModuleFetchError(ValueError):
    pass


class ModuleStager:
    def __init__(self, loader: HmodLoader | None = None) -> None:
        self.loader = loader or HmodLoader()

    @staticmethod
    def normalize_url(value: str) -> str:
        parsed = urllib.parse.urlparse(value)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ModuleFetchError("module URL must be a plain HTTPS URL") from exc
        if parsed.scheme != "https" or parsed.username or parsed.password or port or parsed.query or parsed.fragment:
            raise ModuleFetchError("module URL must be a plain HTTPS URL")
        host = (parsed.hostname or "").casefold()
        parts = [urllib.parse.unquote(part) for part in parsed.path.split("/") if part]
        if any(part in {".", ".."} or "\\" in part for part in parts):
            raise ModuleFetchError("module URL path is unsafe")
        if host == "github.com" and len(parts) >= 5 and parts[2] == "blob":
            owner, repo, _, ref, *filename = parts
            if not filename or not filename[-1].endswith(".hmod"):
                raise ModuleFetchError("module URL must target a .hmod file")
            return "https://raw.githubusercontent.com/" + "/".join([owner, repo, ref, *filename])
        if host == "raw.githubusercontent.com" and len(parts) >= 4 and parts[-1].endswith(".hmod"):
            return urllib.parse.urlunparse(("https", host, "/" + "/".join(parts), "", "", ""))
        raise ModuleFetchError("module URL host or path is not allowed")

    def stage_url(self, value: str, destination: str | Path, *, timeout: float = 10.0) -> LoadedModule:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        url = self.normalize_url(value)
        try:
            request = urllib.request.Request(url, headers={"Accept": "text/plain"}, method="GET")
            with urllib.request.urlopen(request, timeout=timeout) as response:
                final = urllib.parse.urlparse(response.geturl())
                if final.scheme != "https" or (final.hostname or "").casefold() != "raw.githubusercontent.com":
                    raise ModuleFetchError("module URL redirected to an unsafe host")
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > self.loader.max_bytes:
                    raise ModuleFetchError("module download exceeds the size limit")
                data = response.read(self.loader.max_bytes + 1)
        except (OSError, urllib.error.URLError) as exc:
            raise ModuleFetchError("module download failed") from exc
        if len(data) > self.loader.max_bytes:
            raise ModuleFetchError("module download exceeds the size limit")
        fd, temporary = tempfile.mkstemp(prefix=".hotaru-source-", suffix=".hmod")
        path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(path, 0o600)
            return self.stage(path, destination)
        finally:
            path.unlink(missing_ok=True)

    def stage_text(self, source: str, destination: str | Path) -> LoadedModule:
        fd, temporary = tempfile.mkstemp(prefix=".hotaru-source-", suffix=".hmod")
        path = Path(temporary)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(source)
            os.chmod(path, 0o600)
            return self.stage(path, destination)
        finally:
            path.unlink(missing_ok=True)

    def stage(self, source: str | Path, destination: str | Path) -> LoadedModule:
        loaded = self.loader.load(source)
        root = Path(destination)
        if root.exists():
            if root.is_symlink() or not root.is_dir():
                raise ModuleValidationError("staging destination must be a regular directory")
        else:
            root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        fd, temporary = tempfile.mkstemp(prefix=".hotaru-module-", suffix=".hmod", dir=root)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(loaded.source.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            target = root / f"{loaded.manifest.module_id}.hmod"
            os.replace(temporary_path, target)
            return self.loader.load(target)
        finally:
            temporary_path.unlink(missing_ok=True)


class ModuleCatalog:
    def __init__(self, loader: HmodLoader | None = None) -> None:
        self.loader = loader or HmodLoader()
        self._items: dict[str, LoadedModule] = {}

    def discover(self, root: str | Path) -> tuple[LoadedModule, ...]:
        directory = Path(root)
        if not directory.is_dir():
            return ()
        found: list[LoadedModule] = []
        for path in sorted(directory.glob("*.hmod")):
            loaded = self.loader.load(path)
            if loaded.manifest.module_id in self._items or any(
                item.manifest.module_id == loaded.manifest.module_id for item in found
            ):
                raise ModuleValidationError("duplicate module id")
            found.append(loaded)
        for loaded in found:
            self._items[loaded.manifest.module_id] = loaded
        return tuple(found)

    def get(self, module_id: str) -> LoadedModule | None:
        return self._items.get(module_id)

    def items(self) -> tuple[LoadedModule, ...]:
        return tuple(self._items.values())
