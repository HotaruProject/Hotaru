from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import os
import secrets
import stat
import shutil
import sqlite3
import tempfile
import zipfile
import struct
import zlib
from dataclasses import dataclass
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Awaitable, Callable, TypeVar, TypedDict, cast

from goygram.ext import aes_gcm_decrypt, aes_gcm_encrypt

from .modules import HmodLoader, ModuleValidationError

T = TypeVar("T")
MAGIC = b"HBK3"
MAGIC_V2 = b"HBK2"
NONCE_SIZE = 12
KEY_CONTEXT = b"hotaru-backup-v1"

log = logging.getLogger(__name__)


class BackupError(ValueError):
    pass


class DryRunResult(TypedDict):
    format: int
    files: tuple[str, ...]
    metadata: dict[str, object]


@dataclass(frozen=True)
class RestorePlan:
    archive: Path
    files: tuple[str, ...]
    metadata: dict[str, object]


class BackupService:
    def __init__(self, *, max_bytes: int = 64 * 1024 * 1024) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes

    def create(
        self,
        output: str | Path,
        *,
        state_path: str | Path | None = None,
        module_paths: Sequence[str | Path] = (),
        metadata: dict[str, object] | None = None,
        key: str | bytes | None = None,
    ) -> Path:
        files: list[tuple[str, Path]] = []
        snapshot: Path | None = None
        if state_path is not None:
            state = self._regular(Path(state_path))
            snapshot = self._snapshot_state(state, dir=state.parent)
            files.append(("state/state.sqlite3", snapshot))
        for path in sorted((Path(item) for item in module_paths), key=lambda item: str(item)):
            module = self._regular(path)
            if module.suffix != ".hmod":
                raise BackupError("backup accepts only .hmod modules")
            files.append((f"modules/{module.name}", module))
        if not files:
            raise BackupError("backup requires state or modules")
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
        fd, sealed = tempfile.mkstemp(prefix=".hotaru-", suffix=".hbk", dir=destination.parent)
        os.close(fd)
        sealed_path = Path(sealed)
        try:
            raw_entries: list[tuple[str, bytes]] = [(name, path.read_bytes()) for name, path in files]
            packed = self._pack_container(manifest, raw_entries)
            if len(packed) > self.max_bytes:
                raise BackupError("backup exceeds the size limit")
            nonce = secrets.token_bytes(NONCE_SIZE)
            try:
                ciphertext = aes_gcm_encrypt(self._key(key), nonce, packed, MAGIC)
            except Exception as exc:
                log.error("backup encryption failed for %s: %s: %s", destination, type(exc).__name__, exc)
                raise BackupError(f"backup encryption failed: {type(exc).__name__}: {exc}") from exc
            sealed_path.write_bytes(MAGIC + nonce + ciphertext)
            if sealed_path.stat().st_size > self.max_bytes:
                raise BackupError("backup exceeds the size limit")
            os.replace(sealed_path, destination)
            destination.chmod(0o600)
            log.info("backup created at %s (%d bytes)", destination, destination.stat().st_size)
            return destination
        finally:
            sealed_path.unlink(missing_ok=True)
            if snapshot is not None:
                snapshot.unlink(missing_ok=True)

    @staticmethod
    def _pack_container(manifest: Mapping[str, object], files: Sequence[tuple[str, bytes]]) -> bytes:
        manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        out = bytearray()
        out.extend(struct.pack(">BBI", 1, 1, len(manifest_bytes)))
        out.extend(manifest_bytes)
        out.extend(struct.pack(">I", len(files)))
        for name, data in files:
            name_bytes = name.encode("utf-8")
            comp = zlib.compress(data, level=9)
            sha = hashlib.sha256(data).digest()
            out.extend(struct.pack(">H", len(name_bytes)))
            out.extend(name_bytes)
            out.extend(struct.pack(">QQ32s", len(data), len(comp), sha))
            out.extend(comp)
        return bytes(out)

    @staticmethod
    def _unpack_container(raw: bytes) -> tuple[dict[str, object], dict[str, bytes]]:
        if len(raw) < 6:
            raise BackupError("backup container header is truncated")
        _ver, _comp_type, mlen = struct.unpack_from(">BBI", raw, 0)
        pos = 6
        if len(raw) < pos + mlen + 4:
            raise BackupError("backup manifest is truncated")
        manifest = json.loads(raw[pos:pos + mlen].decode("utf-8"))
        pos += mlen
        nfiles, = struct.unpack_from(">I", raw, pos)
        pos += 4
        files: dict[str, bytes] = {}
        for _ in range(nfiles):
            if len(raw) < pos + 2:
                raise BackupError("backup record is truncated")
            nlen, = struct.unpack_from(">H", raw, pos)
            pos += 2
            if len(raw) < pos + nlen + 48:
                raise BackupError("backup record metadata is truncated")
            name = raw[pos:pos + nlen].decode("utf-8")
            pos += nlen
            orig_len, comp_len, sha = struct.unpack_from(">QQ32s", raw, pos)
            pos += 48
            if len(raw) < pos + comp_len:
                raise BackupError("backup record payload is truncated")
            comp_data = raw[pos:pos + comp_len]
            pos += comp_len
            decomp = zlib.decompress(comp_data)
            if len(decomp) != orig_len or hashlib.sha256(decomp).digest() != sha:
                raise BackupError(f"checksum verification failed for {name}")
            files[name] = decomp
        return manifest, files

    def dry_run(self, archive_path: str | Path, key: str | bytes | None = None) -> DryRunResult:
        archive = self._regular(Path(archive_path))
        if archive.suffix != ".hbk":
            raise BackupError("backup must use the .hbk extension")
        if archive.stat().st_size > self.max_bytes:
            raise BackupError("backup exceeds the size limit")
        fmt, payload = self._unseal(archive, key=key)
        if fmt == 3:
            manifest, files = self._unpack_container(payload)
            records_value = manifest.get("files")
            if not isinstance(records_value, dict) or not files:
                raise BackupError("backup manifest or files are missing")
            for name, data in files.items():
                if name == "state/state.sqlite3":
                    self._validate_state(data)
                elif name.startswith("modules/"):
                    self._validate_module(data)
                else:
                    raise BackupError("backup contains an unsafe path")
            metadata = manifest.get("metadata", {})
            self._validate_metadata(metadata)
            return {"format": 1, "files": tuple(sorted(files)), "metadata": cast('dict[str, object]', metadata) if isinstance(metadata, dict) else {}}
        else:
            fd, temporary = tempfile.mkstemp(prefix=".hotaru-open-", suffix=".zip")
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
            plain = Path(temporary)
            try:
                with zipfile.ZipFile(plain) as source:
                    return self._inspect(source)
            except BackupError:
                raise
            except Exception as exc:
                log.error("backup inspection failed for %s: %s: %s", archive, type(exc).__name__, exc)
                raise BackupError(f"backup archive is invalid: {type(exc).__name__}: {exc}") from exc
            finally:
                plain.unlink(missing_ok=True)

    def _unseal(self, archive: Path, key: str | bytes | None = None) -> tuple[int, bytes]:
        data = archive.read_bytes()
        if data.startswith(MAGIC):
            magic = MAGIC
            fmt = 3
        elif data.startswith(MAGIC_V2):
            magic = MAGIC_V2
            fmt = 2
        else:
            raise BackupError("backup is not encrypted")
        if len(data) <= len(magic) + NONCE_SIZE:
            raise BackupError("backup is truncated")
        nonce = data[len(magic):len(magic) + NONCE_SIZE]
        ciphertext = data[len(magic) + NONCE_SIZE:]
        plaintext: bytes | None = None
        if key:
            try:
                plaintext = aes_gcm_decrypt(self._key(key), nonce, ciphertext, magic)
            except Exception as exc:
                log.error("backup decryption failed for %s: %s: %s", archive, type(exc).__name__, exc)
                raise BackupError(f"backup decryption failed: {type(exc).__name__}: {exc}") from exc
        else:
            try:
                plaintext = aes_gcm_decrypt(self._key(None), nonce, ciphertext, magic)
            except Exception as exc:
                log.error("backup decryption failed for %s: %s: %s", archive, type(exc).__name__, exc)
                raise BackupError(f"backup decryption failed: {type(exc).__name__}: {exc}") from exc
        if len(plaintext) > self.max_bytes:
            raise BackupError("backup exceeds the expanded size limit")
        return fmt, plaintext

    @staticmethod
    def _key(custom_key: str | bytes | None = None) -> bytes:
        if custom_key:
            raw = custom_key.encode("utf-8") if isinstance(custom_key, str) else custom_key
            return hashlib.sha256(raw).digest()
        from .state import master_key

        return hmac.new(master_key(), KEY_CONTEXT, hashlib.sha256).digest()

    def _inspect(self, source: zipfile.ZipFile) -> DryRunResult:
        names = source.namelist()
        if len(names) != len(set(names)):
            raise BackupError("backup contains duplicate paths")
        if "manifest.json" not in names or not any(name == "state/state.sqlite3" or name.startswith("modules/") for name in names):
            raise BackupError("backup manifest or content is missing")
        if sum(info.file_size for info in source.infolist()) > self.max_bytes:
            raise BackupError("backup exceeds the expanded size limit")
        for info in source.infolist():
            name = info.filename
            if name not in {"manifest.json", "state/state.sqlite3"} and not (
                name.startswith("modules/") and name.count("/") == 1
                and "\\" not in name and Path(name).suffix == ".hmod"
            ):
                raise BackupError("backup contains an unsafe path")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise BackupError("backup contains a symlink")
        try:
            decoded = cast(object, json.loads(source.read("manifest.json")))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise BackupError("backup manifest is invalid") from exc
        if not isinstance(decoded, dict):
            raise BackupError("backup manifest is unsupported")
        manifest = cast('dict[str, object]', decoded)
        records_value = manifest.get("files")
        if manifest.get("format") != 1 or not isinstance(records_value, dict):
            raise BackupError("backup manifest is unsupported")
        records = cast('dict[str, object]', records_value)
        expected = {"manifest.json", *records}
        if set(names) != expected:
            raise BackupError("backup archive contains unexpected files")
        for name, record_value in records.items():
            record = cast('dict[str, object]', record_value) if isinstance(record_value, dict) else None
            if name not in names or name == "manifest.json" or record is None:
                raise BackupError("backup manifest does not match archive")
            data = source.read(name)
            if len(data) != record.get("size") or hashlib.sha256(data).hexdigest() != record.get("sha256"):
                raise BackupError("backup checksum verification failed")
            if name == "state/state.sqlite3":
                self._validate_state(data)
            elif name.startswith("modules/"):
                self._validate_module(data)
        metadata = manifest.get("metadata", {})
        self._validate_metadata(metadata)
        return {"format": 1, "files": tuple(sorted(records)), "metadata": cast('dict[str, object]', metadata) if isinstance(metadata, dict) else {}}

    @classmethod
    def _validate_metadata(cls, value: object, depth: int = 0) -> None:
        if depth > 4:
            raise BackupError("backup metadata is too deep")
        if isinstance(value, dict):
            mapping = cast('dict[object, object]', value)
            if len(mapping) > 64:
                raise BackupError("backup metadata has too many fields")
            for key, item in mapping.items():
                if not isinstance(key, str):
                    raise BackupError("backup metadata keys must be strings")
                lowered = key.casefold()
                if any(secret in lowered for secret in ("token", "password", "api_hash", "auth_key", "vault")):
                    raise BackupError("backup metadata contains a sensitive key")
                cls._validate_metadata(item, depth + 1)
        elif isinstance(value, list):
            values = cast('list[object]', value)
            if len(values) > 64:
                raise BackupError("backup metadata list is too large")
            for item in values:
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

    def plan(self, archive_path: str | Path, key: str | bytes | None = None) -> RestorePlan:
        archive = self._regular(Path(archive_path))
        result = self.dry_run(archive, key=key)
        return RestorePlan(archive, result["files"], result["metadata"])

    def stage(self, archive_path: str | Path, directory: str | Path | None = None, key: str | bytes | None = None) -> Path:
        archive = self._regular(Path(archive_path))
        if archive.suffix != ".hbk" or archive.stat().st_size > self.max_bytes:
            raise BackupError("backup extension or size is invalid")
        fmt, payload = self._unseal(archive, key=key)
        destination = Path(directory) if directory is not None else Path(tempfile.mkdtemp(prefix="hotaru-restore-"))
        if destination.is_symlink():
            raise BackupError("restore destination is a symlink")
        destination.mkdir(parents=True, exist_ok=True)
        destination.chmod(0o700)
        if fmt == 3:
            _manifest, files = self._unpack_container(payload)
            for name, data in files.items():
                target = destination / name
                if target.parent.is_symlink():
                    raise BackupError("restore directory is a symlink")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.parent.chmod(0o700)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
            return destination
        else:
            fd, temporary = tempfile.mkstemp(prefix=".hotaru-stage-", suffix=".zip")
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
            plain = Path(temporary)
            try:
                with zipfile.ZipFile(plain) as source:
                    self._inspect(source)
                    for info in source.infolist():
                        name = info.filename
                        if name == "manifest.json":
                            continue
                        target = destination / name
                        if target.parent.is_symlink():
                            raise BackupError("restore directory is a symlink")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.parent.chmod(0o700)
                        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(source.read(name))
                return destination
            except BackupError:
                raise
            except Exception as exc:
                log.error("backup staging failed for %s: %s: %s", archive, type(exc).__name__, exc)
                raise BackupError(f"backup archive is invalid: {type(exc).__name__}: {exc}") from exc
            finally:
                plain.unlink(missing_ok=True)

    def activate_staged(
        self,
        staged: str | Path,
        *,
        state_path: str | Path | None = None,
        modules_path: str | Path | None = None,
        only: str = "all",
    ) -> None:
        source = Path(staged)
        scope = only.casefold()
        restore_state = scope in ("all", "full", "state", "db", "database")
        restore_modules = scope in ("all", "full", "modules", "module", "mods", "mod")

        state_source = source / "state/state.sqlite3"
        modules_source = source / "modules"

        if restore_state and state_path is not None and state_source.is_file():
            self._validate_state(state_source.read_bytes())
        elif restore_state and scope in ("state", "db", "database"):
            raise BackupError("staged restore is missing state database")
        else:
            restore_state = False

        if restore_modules and modules_path is not None and modules_source.is_dir():
            pass
        elif restore_modules and scope in ("modules", "module", "mods", "mod"):
            raise BackupError("staged restore is missing modules")
        else:
            restore_modules = False

        if not restore_state and not restore_modules:
            raise BackupError("staged restore has no matching components")

        target_state = Path(state_path) if restore_state and state_path is not None else None
        target_modules = Path(modules_path) if restore_modules and modules_path is not None else None

        state_temp: Path | None = None
        modules_temp: Path | None = None
        state_backup: Path | None = None
        modules_backup: Path | None = None
        state_replaced = False
        modules_replaced = False

        try:
            if restore_state and target_state is not None:
                target_state.parent.mkdir(parents=True, exist_ok=True)
                state_fd, state_temp_name = tempfile.mkstemp(prefix=".hotaru-activate-", suffix=".sqlite3", dir=target_state.parent)
                os.close(state_fd)
                state_temp = Path(state_temp_name)
                state_backup = target_state.with_name(f".{target_state.name}.rollback")
                shutil.copy2(state_source, state_temp)
                state_temp.chmod(0o600)

            if restore_modules and target_modules is not None:
                target_modules.parent.mkdir(parents=True, exist_ok=True)
                modules_temp = Path(tempfile.mkdtemp(prefix=".hotaru-modules-", dir=target_modules.parent))
                modules_backup = target_modules.with_name(f".{target_modules.name}.rollback")
                for item in modules_source.iterdir():
                    if item.is_symlink() or not item.is_file() or item.suffix != ".hmod":
                        raise BackupError("staged module tree contains an unsafe file")
                    shutil.copy2(item, modules_temp / item.name)
                    (modules_temp / item.name).chmod(0o600)

            if restore_state and target_state is not None and state_temp is not None:
                if target_state.exists():
                    rollback_state = self._snapshot_state(self._regular(target_state), dir=target_state.parent)
                    if state_backup is not None:
                        if state_backup.exists():
                            state_backup.unlink(missing_ok=True)
                        try:
                            os.replace(rollback_state, state_backup)
                        except OSError as err:
                            if err.errno == 18:
                                shutil.copy2(rollback_state, state_backup)
                                rollback_state.unlink(missing_ok=True)
                            else:
                                raise
                    self._remove_sqlite_sidecars(target_state)
                else:
                    self._remove_sqlite_sidecars(target_state)
                try:
                    os.replace(state_temp, target_state)
                except OSError as err:
                    if err.errno == 18:
                        shutil.copy2(state_temp, target_state)
                        state_temp.unlink(missing_ok=True)
                    else:
                        raise
                state_replaced = True

            if restore_modules and target_modules is not None and modules_temp is not None:
                if target_modules.exists():
                    if target_modules.is_symlink():
                        raise BackupError("module target is a symlink")
                    if modules_backup is not None:
                        if modules_backup.exists():
                            shutil.rmtree(modules_backup, ignore_errors=True)
                        try:
                            os.replace(target_modules, modules_backup)
                        except OSError as err:
                            if err.errno == 18:
                                shutil.copytree(target_modules, modules_backup)
                                shutil.rmtree(target_modules, ignore_errors=True)
                            else:
                                raise
                try:
                    os.replace(modules_temp, target_modules)
                except OSError as err:
                    if err.errno == 18:
                        if target_modules.exists():
                            shutil.rmtree(target_modules, ignore_errors=True)
                        shutil.copytree(modules_temp, target_modules)
                        shutil.rmtree(modules_temp, ignore_errors=True)
                    else:
                        raise
                modules_replaced = True
        except Exception as exc:
            if modules_replaced and target_modules is not None:
                shutil.rmtree(target_modules, ignore_errors=True)
            if modules_backup is not None and modules_backup.exists() and target_modules is not None:
                try:
                    os.replace(modules_backup, target_modules)
                except OSError:
                    shutil.copytree(modules_backup, target_modules, dirs_exist_ok=True)
            if state_replaced and target_state is not None:
                target_state.unlink(missing_ok=True)
            if state_backup is not None and state_backup.exists() and target_state is not None:
                try:
                    os.replace(state_backup, target_state)
                except OSError:
                    shutil.copy2(state_backup, target_state)
            raise BackupError(f"atomic restore activation failed: {exc}") from exc
        finally:
            if state_temp is not None:
                state_temp.unlink(missing_ok=True)
            if modules_temp is not None and modules_temp.exists():
                shutil.rmtree(modules_temp, ignore_errors=True)
            if state_backup is not None:
                state_backup.unlink(missing_ok=True)
            if modules_backup is not None and modules_backup.exists():
                shutil.rmtree(modules_backup, ignore_errors=True)

    async def restore(
        self,
        archive_path: str | Path | RestorePlan,
        activate: Callable[[Path], T | Awaitable[T]],
        *,
        rollback: Callable[[], object | Awaitable[object]] | None = None,
        timeout: float = 10.0,
        key: str | bytes | None = None,
    ) -> T:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        plan = archive_path if isinstance(archive_path, RestorePlan) else self.plan(archive_path, key=key)
        staged = self.stage(plan.archive, key=key)
        try:
            result = activate(staged)
            if inspect.isawaitable(result):
                result = await asyncio.wait_for(result, timeout=timeout)
            return result
        except Exception as exc:
            log.error("restore activation failed: %s: %s", type(exc).__name__, exc, exc_info=True)
            if rollback is not None:
                recovery = rollback()
                if inspect.isawaitable(recovery):
                    await asyncio.wait_for(recovery, timeout=timeout)
            cause = getattr(exc, "__cause__", None)
            err_msg = f"{exc}" if not cause or str(cause) == str(exc) else f"{exc}: {cause}"
            raise BackupError(f"restore activation failed: {err_msg}") from exc
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
    def _validate_module(data: bytes) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".hotaru-module-", suffix=".hmod")
        os.close(fd)
        path = Path(temporary)
        try:
            path.write_bytes(data)
            HmodLoader().load(path)
        except (ModuleValidationError, OSError) as exc:
            raise BackupError("backup module is invalid") from exc
        finally:
            path.unlink(missing_ok=True)

    @staticmethod
    def _remove_sqlite_sidecars(path: Path) -> None:
        for suffix in ("-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)

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
    def _snapshot_state(path: Path, dir: Path | None = None) -> Path:
        fd, temporary = tempfile.mkstemp(prefix=".hotaru-state-", suffix=".sqlite3", dir=dir)
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
