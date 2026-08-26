from __future__ import annotations

import hashlib
import json
import os
import stat
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
        files.append(("state/state.sqlite3", state))
        for path in sorted((Path(item) for item in module_paths), key=lambda item: str(item)):
            module = self._regular(path)
            if module.suffix != ".hmod":
                raise BackupError("backup accepts only .hmod modules")
            files.append((f"modules/{module.name}", module))
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
            return {"format": 1, "files": tuple(sorted(records)), "metadata": manifest.get("metadata", {})}

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

    @staticmethod
    def _regular(path: Path) -> Path:
        if path.is_symlink() or not path.is_file():
            raise BackupError("backup input must be a regular file")
        return path

    @staticmethod
    def _digest(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
