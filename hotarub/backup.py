from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import stat
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any


class BackupError(ValueError):
    pass


class BackupService:
    def __init__(self, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes

    def create(
        self,
        output: str | Path,
        *,
        state_path: str | Path,
        module_paths: list[str | Path],
        metadata: dict[str, Any] | None = None,
    ) -> Path:
        files: list[tuple[str, Path]] = []
        state = self._regular(Path(state_path))
        snapshot = self._snapshot_state(state)
        files.append(("state/state.sqlite3", snapshot))
        for path in sorted((Path(item) for item in module_paths), key=lambda item: str(item)):
            module = self._regular(path)
            if module.suffix != ".hmod":
                raise BackupError("backup accepts only .hmod modules")
            files.append((f"modules/{module.name}", module))
        self._validate_metadata(metadata or {})
        manifest = {
            "format": 1,
            "metadata": metadata or {},
            "files": {
                name: {"sha256": self._digest(path), "size": path.stat().st_size}
                for name, path in files
            },
        }
        destination = Path(output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".hotaru-", suffix=".hbk", dir=destination.parent)
        os.close(fd)
        temporary_path = Path(temporary)
        try:
            with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, separators=(",", ":")))
                for name, path in files:
                    archive.write(path, name)
            if temporary_path.stat().st_size > self.max_bytes:
                raise BackupError("backup exceeds the size limit")
            os.replace(temporary_path, destination)
            destination.chmod(0o600)
            return destination
        finally:
            temporary_path.unlink(missing_ok=True)
            snapshot.unlink(missing_ok=True)

    def dry_run(self, archive_path: str | Path) -> dict[str, Any]:
        archive = self._regular(Path(archive_path))
        if archive.suffix != ".hbk":
            raise BackupError("backup must use the .hbk extension")
        with zipfile.ZipFile(archive) as source:
            names = source.namelist()
            if "manifest.json" not in names:
                raise BackupError("backup manifest is missing")
            if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
                raise BackupError("backup contains an unsafe path")
            try:
                manifest = json.loads(source.read("manifest.json"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BackupError("backup manifest is invalid") from exc
            records = manifest.get("files")
            if manifest.get("format") != 1 or not isinstance(records, dict):
                raise BackupError("backup manifest is unsupported")
            expected = {"manifest.json", *records}
            if set(names) != expected:
                raise BackupError("backup archive contains unexpected files")
            for name, record in records.items():
                if name not in names or not isinstance(record, dict):
                    raise BackupError("backup manifest does not match archive")
                data = source.read(name)
                if len(data) != record.get("size") or hashlib.sha256(data).hexdigest() != record.get("sha256"):
                    raise BackupError("backup checksum verification failed")
                if name == "state/state.sqlite3":
                    self._validate_state(data)
            return {"format": 1, "files": tuple(sorted(records)), "metadata": manifest.get("metadata", {})}

    @classmethod
    def _validate_metadata(cls, value: Any, depth: int = 0) -> None:
        if depth > 4:
            raise BackupError("backup metadata is too deep")
        if isinstance(value, dict):
            if len(value) > 64:
                raise BackupError("backup metadata has too many fields")
            for key, item in value.items():
                if not isinstance(key, str):
                    raise BackupError("backup metadata keys must be strings")
                lowered = key.casefold()
                if any(secret in lowered for secret in ("token", "password", "api_hash", "auth_key", "vault")):
                    raise BackupError("backup metadata contains a sensitive key")
                cls._validate_metadata(item, depth + 1)
        elif isinstance(value, list):
            if len(value) > 64:
                raise BackupError("backup metadata list is too large")
            for item in value:
                cls._validate_metadata(item, depth + 1)
        elif not isinstance(value, (str, int, float, bool)) and value is not None:
            raise BackupError("backup metadata is not JSON-compatible")

    def prune(self, directory: str | Path, *, keep: int = 7) -> tuple[Path, ...]:
        if keep < 1:
            raise ValueError("keep must be positive")
        root = Path(directory)
        if not root.is_dir():
            return ()
        archives = sorted(
            (path for path in root.glob("*.hbk") if path.is_file() and not path.is_symlink()),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        removed: list[Path] = []
        for path in archives[keep:]:
            path.unlink()
            removed.append(path)
        return tuple(removed)

    def stage(self, archive_path: str | Path, directory: str | Path | None = None) -> Path:
        self.dry_run(archive_path)
        destination = Path(directory) if directory is not None else Path(tempfile.mkdtemp(prefix="hotaru-restore-"))
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(Path(archive_path)) as source:
            for info in source.infolist():
                name = info.filename
                if name == "manifest.json":
                    continue
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise BackupError("backup contains a symlink")
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(source.read(name))
        return destination

    async def restore(
        self,
        archive_path: str | Path,
        activate: Any,
        *,
        rollback: Any | None = None,
        timeout: float = 10.0,
    ) -> Any:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        staged = self.stage(archive_path)
        try:
            result = activate(staged)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=timeout)
            return result
        except Exception as exc:
            if rollback is not None:
                recovery = rollback()
                if inspect.isawaitable(recovery):
                    await asyncio.wait_for(recovery, timeout=timeout)
            raise BackupError("restore activation failed") from exc
        finally:
            self._remove_tree(staged)

    @staticmethod
    def _regular(path: Path) -> Path:
        if path.is_symlink() or not path.is_file():
            raise BackupError("backup input must be a regular file")
        return path

    @staticmethod
    def _remove_tree(path: Path) -> None:
        if not path.exists():
            return
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_dir() and not child.is_symlink():
                child.rmdir()
            else:
                child.unlink()
        path.rmdir()

    @staticmethod
    def _validate_state(data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".hotaru-check-", suffix=".sqlite3")
        os.close(fd)
        path = Path(temporary)
        try:
            path.write_bytes(data)
            with sqlite3.connect(path) as connection:
                check = connection.execute("PRAGMA integrity_check").fetchone()
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'module_state'"
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise BackupError("backup state database is invalid") from exc
        finally:
            path.unlink(missing_ok=True)
        if check != ("ok",) or table != (1,):
            raise BackupError("backup state schema is invalid")

    @staticmethod
    def _snapshot_state(path: Path) -> Path:
        fd, temporary = tempfile.mkstemp(prefix=".hotaru-state-", suffix=".sqlite3")
        os.close(fd)
        snapshot = Path(temporary)
        try:
            with sqlite3.connect(path) as source, sqlite3.connect(snapshot) as target:
                source.backup(target)
                check = target.execute("PRAGMA integrity_check").fetchone()
                if check != ("ok",):
                    raise BackupError("SQLite integrity check failed")
            return snapshot
        except BackupError:
            snapshot.unlink(missing_ok=True)
            raise
        except (sqlite3.DatabaseError, OSError) as exc:
            snapshot.unlink(missing_ok=True)
            raise BackupError("SQLite snapshot failed") from exc

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
