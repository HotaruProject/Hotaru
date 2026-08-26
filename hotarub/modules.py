from __future__ import annotations

import ast
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ModuleValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    module_id: str
    version: str
    description: str
    commands: tuple[str, ...]
    capabilities: tuple[str, ...]


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
            compile(tree, str(candidate), "exec")
        except (SyntaxError, ValueError, TypeError) as exc:
            raise ModuleValidationError("module failed validation") from exc
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return LoadedModule(candidate, digest, source, manifest)

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
        return ModuleManifest(module_id, version, description, tuple(commands), tuple(capabilities))

    @staticmethod
    def _strings(value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


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
