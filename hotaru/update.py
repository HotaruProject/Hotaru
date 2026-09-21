from __future__ import annotations

import datetime
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence


class UpdateError(RuntimeError):
    pass


class UpdateResult(NamedTuple):
    before: str
    after: str
    recovery: Optional[Path]
    local_applied: bool
    conflicts: List[str]
    history_replaced: bool


def _git(repo: Path, *args: str, check: bool = True, input_data: Optional[bytes] = None) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_data,
        capture_output=True,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()
        raise UpdateError(detail or "git command failed")
    return result


def _text(result: subprocess.CompletedProcess[bytes]) -> str:
    return result.stdout.decode("utf-8", "replace").strip()


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _untracked(repo: Path) -> List[str]:
    raw = _git(repo, "ls-files", "--others", "--exclude-standard", "-z").stdout
    values = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        value = item.decode("utf-8", "surrogateescape")
        if value != ".hotaru" and not value.startswith(".hotaru/"):
            values.append(value)
    return values


def _archive_untracked(repo: Path, recovery: Path, paths: Sequence[str]) -> None:
    if not paths:
        return
    with tarfile.open(str(recovery / "untracked.tar.gz"), "w:gz") as archive:
        for name in paths:
            path = repo / name
            if path.exists() or path.is_symlink():
                archive.add(str(path), arcname=name, recursive=True)


def _save_blob(repo: Path, revision: str, name: str, destination: Path) -> None:
    result = _git(repo, "show", revision + ":" + name, check=False)
    if result.returncode != 0:
        return
    target = destination / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(result.stdout)


def _apply_stash(repo: Path, stash: str, recovery: Path) -> tuple[bool, List[str]]:
    applied_result = _git(repo, "stash", "apply", "--index", stash, check=False)
    conflicts = [line for line in _text(_git(repo, "diff", "--name-only", "--diff-filter=U")).splitlines() if line]
    if conflicts:
        for name in conflicts:
            _save_blob(repo, stash, name, recovery / "conflicts" / "local")
        _git(repo, "checkout", "--ours", "--", *conflicts)
        _git(repo, "add", "--", *conflicts)
    elif applied_result.returncode != 0:
        raise UpdateError((applied_result.stderr or applied_result.stdout).decode("utf-8", "replace").strip() or "local changes could not be restored")
    return True, conflicts


def _resume_interrupted(repo: Path) -> Optional[UpdateResult]:
    base = repo / ".hotaru" / "recovery"
    if not base.is_dir():
        return None
    for recovery in sorted(base.iterdir(), reverse=True):
        manifest_path = recovery / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(manifest, dict) or manifest.get("stage") != "saved":
            continue
        before = str(manifest.get("before") or "")
        target = str(manifest.get("target") or "")
        stash = str(manifest.get("stash") or "")
        if not before or not target or not stash:
            continue
        _git(repo, "cat-file", "-e", target + "^{commit}")
        _git(repo, "reset", "--hard", target)
        _git(repo, "clean", "-fd", "-e", ".hotaru/")
        applied, conflicts = _apply_stash(repo, stash, recovery)
        manifest["stage"] = "complete"
        manifest["recovered"] = True
        manifest["conflicts"] = conflicts
        manifest["local_applied"] = applied
        _atomic_json(manifest_path, manifest)
        return UpdateResult(before, target, recovery, applied, conflicts, bool(manifest.get("history_replaced")))
    return None


def _acquire_update_lock(repo: Path) -> Path:
    lock = repo / ".git" / "hotaru-update.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        try:
            stale = time.time() - lock.stat().st_mtime > 900
        except OSError:
            stale = False
        if not stale:
            raise UpdateError("another update is already running")
        shutil.rmtree(str(lock), ignore_errors=True)
        lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()), encoding="ascii")
    return lock


def _preflight(repo: Path, target: str) -> None:
    worktree = Path(tempfile.mkdtemp(prefix="hotaru-preflight-"))
    try:
        _git(repo, "worktree", "add", "--detach", str(worktree), target)
        result = subprocess.run(
            [sys.executable, "-m", "compileall", "-q", "hotaru", "relay"],
            cwd=str(worktree),
            capture_output=True,
        )
        if result.returncode != 0:
            raise UpdateError((result.stderr or result.stdout).decode("utf-8", "replace").strip() or "update preflight failed")
        for path in (worktree / "constellations").glob("*.hmod"):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
    finally:
        _git(repo, "worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(str(worktree), ignore_errors=True)


def _update_repository(root: Any) -> UpdateResult:
    repo = Path(root).resolve()
    resumed = _resume_interrupted(repo)
    if resumed is not None:
        return resumed
    _git(repo, "fetch", "--prune", "origin", "+refs/heads/main:refs/remotes/origin/main")
    before = _text(_git(repo, "rev-parse", "HEAD"))
    target = _text(_git(repo, "rev-parse", "origin/main"))
    _git(repo, "cat-file", "-e", target + "^{commit}")
    if before == target:
        return UpdateResult(before, target, None, False, [], False)
    replaced = _git(repo, "merge-base", before, target, check=False).returncode != 0
    _preflight(repo, target)
    status = _git(repo, "status", "--porcelain=v1", "-z").stdout
    recovery = None
    stash = None
    if status:
        stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        recovery = repo / ".hotaru" / "recovery" / (stamp + "-" + before[:12])
        recovery.mkdir(parents=True, exist_ok=False)
        (recovery / "status.bin").write_bytes(status)
        (recovery / "tracked.patch").write_bytes(_git(repo, "diff", "--binary", "HEAD").stdout)
        (recovery / "staged.patch").write_bytes(_git(repo, "diff", "--binary", "--cached", "HEAD").stdout)
        paths = _untracked(repo)
        _archive_untracked(repo, recovery, paths)
        _git(repo, "bundle", "create", str(recovery / "repository.bundle"), "--all")
        ref = "refs/hotaru/backups/" + stamp
        _git(repo, "update-ref", ref, before)
        _git(repo, "stash", "push", "--include-untracked", "--message", "hotaru-update-" + stamp, "--", ".", ":(exclude).hotaru")
        stash = _text(_git(repo, "rev-parse", "stash@{0}"))
        _atomic_json(recovery / "manifest.json", {
            "before": before,
            "target": target,
            "history_replaced": replaced,
            "stash": stash,
            "backup_ref": ref,
            "untracked": paths,
            "stage": "saved",
        })
    _git(repo, "reset", "--hard", target)
    _git(repo, "clean", "-fd", "-e", ".hotaru/")
    conflicts: List[str] = []
    applied = False
    if stash is not None:
        assert recovery is not None
        applied, conflicts = _apply_stash(repo, stash, recovery)
    if recovery is not None:
        manifest = json.loads((recovery / "manifest.json").read_text(encoding="utf-8"))
        manifest["stage"] = "complete"
        manifest["conflicts"] = conflicts
        manifest["local_applied"] = applied
        _atomic_json(recovery / "manifest.json", manifest)
    return UpdateResult(before, target, recovery, applied, conflicts, replaced)


def update_repository(root: Any) -> UpdateResult:
    repo = Path(root).resolve()
    lock = _acquire_update_lock(repo)
    try:
        return _update_repository(repo)
    finally:
        shutil.rmtree(str(lock), ignore_errors=True)
