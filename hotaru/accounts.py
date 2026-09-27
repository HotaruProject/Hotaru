from __future__ import annotations

import os
import fcntl
import asyncio
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .state import StateStore


VAULT_RE = re.compile(r"(?:user|hotaru)-(\d+)\.vault")


def account_home(root: Path, user_id: int) -> Path:
    sanctuary = Path(root) / "sanctuary"
    old_home = Path(root) / f"account-{user_id}"
    new_home = sanctuary / f"account-{user_id}"
    if old_home.exists() and old_home.is_dir() and not new_home.exists():
        sanctuary.mkdir(parents=True, exist_ok=True)
        old_home.rename(new_home)
    return new_home


def user_vault_path(root: Path, user_id: int) -> Path:
    return account_home(root, user_id) / f"user-{user_id}.vault"


def bot_vault_path(root: Path, user_id: int) -> Path:
    return account_home(root, user_id) / f"bot-{user_id}.vault"


def account_db_path(root: Path, user_id: int) -> Path:
    home = account_home(root, user_id)
    dest = home / f"hotaru-{user_id}.sqlite3"
    legacy = home / f"state-{user_id}.sqlite3"
    if not dest.exists() and legacy.exists():
        legacy.replace(dest)
    return dest


def account_state_path(root: Path, user_id: int) -> Path:
    return account_db_path(root, user_id)


def parse_user_id(session_name: str) -> int | None:
    match = re.fullmatch(r"(?:user|hotaru)-(\d+)", Path(session_name).name)
    if match is None:
        return None
    return int(match.group(1))


def vault_name(session_name: str) -> str:
    return f"{session_name}.vault"


@dataclass(frozen=True)
class AccountProfile:
    user_id: int
    account_number: int
    session_name: str
    session_dir: Path
    enabled: bool = True

    @property
    def vault_name(self) -> str:
        return vault_name(self.session_name)

    @property
    def session_base(self) -> str:
        uid = parse_user_id(self.session_name) or self.user_id
        return str(user_vault_path(self.session_dir, uid).with_suffix(""))

    def validate(self) -> None:
        if self.user_id <= 0 or self.account_number <= 0:
            raise ValueError("account identity must be positive")
        if not self.session_name or Path(self.session_name).name != self.session_name:
            raise ValueError("account session name is invalid")
        if self.session_dir.name in {"", ".", ".."}:
            raise ValueError("account session directory is invalid")


class AccountError(RuntimeError):
    def __init__(self, code: str, **params: Any) -> None:
        super().__init__(code)
        self.code = code
        self.params = params


def account_config(config: Any, profile: AccountProfile) -> Any:
    from dataclasses import replace
    from .config import RuntimeConfig

    path = account_db_path(profile.session_dir, profile.user_id)
    state = StateStore(path)
    try:
        defaults = {
            'api-id': config.api_id, 'api-hash': config.api_hash,
            'bot-token': None, 'owner-id': profile.user_id, 'prefix': config.prefix,
            'session-name': profile.session_name, 'session-dir': str(profile.session_dir),
            'backup-keep': config.backup_keep,
            'sandbox': config.sandbox,
        }
        for key, value in defaults.items():
            if state.get_setting(key) is None:
                state.set_setting(key, value)
        state.set_setting('owner-id', profile.user_id)
        state.set_setting('session-name', profile.session_name)
        state.set_setting('session-dir', str(profile.session_dir))
        registry = StateStore(profile.session_dir / 'sanctuary/state.sqlite3')
        try:
            AccountManager(registry, profile.session_dir).protect(state, profile.user_id)
        finally:
            registry.close()
    finally:
        state.close()
    child = RuntimeConfig.from_database(path, discover=False)
    return replace(child, owner_id=profile.user_id, bot_token=None, session_name=profile.session_name,
                   session_dir=profile.session_dir, state_path=path)


class AccountLock:
    def __init__(self, root: Path, uid: int, fd: int | None = None) -> None:
        path = account_home(root, uid) / 'process.lock'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        self.fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600) if fd is None else fd
        try:
            if os.fstat(self.fd).st_ino != path.stat().st_ino:
                raise AccountError('invalid_lock')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.set_pid(os.getpid())
        except BlockingIOError:
            os.close(self.fd)
            self.fd = -1
            raise AccountError('running') from None
        except BaseException:
            os.close(self.fd)
            self.fd = -1
            raise

    def set_pid(self, pid: int) -> None:
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(pid).encode())

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class AccountManager:
    def __init__(self, state: StateStore, session_dir: str | Path) -> None:
        self.state = state
        self.session_dir = Path(session_dir).expanduser().resolve()
        self.owned = False
        self.actor: int | None = None
        self.children: dict[int, subprocess.Popen[bytes]] = {}
        with self.state.connection:
            self.state.connection.execute('CREATE TABLE IF NOT EXISTS account_removed (user_id INTEGER PRIMARY KEY)')
            self.state.connection.execute('CREATE TABLE IF NOT EXISTS account_links (user_id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL)')
            self.state.connection.execute('BEGIN IMMEDIATE')
            rows = self._rows()
            taken = {int(row[1]) for row in rows}
            seen: set[int] = set()
            for row in rows:
                number = int(row[1])
                if number in seen:
                    number = 1
                    while number in taken:
                        number += 1
                    self.state.connection.execute(
                        'UPDATE accounts SET account_number = ? WHERE user_id = ? AND account_number = ?',
                        (number, row[0], row[1]),
                    )
                    taken.add(number)
                seen.add(number)

    @classmethod
    def open(cls, state: StateStore, root: Path) -> AccountManager:
        registry = StateStore(root.resolve() / 'sanctuary/state.sqlite3')
        manager = cls(registry, root)
        manager.owned = True
        for key in ('api-id', 'api-hash', 'prefix', 'backup-keep', 'sandbox'):
            if registry.get_setting(key) is None:
                value = state.get_setting(key)
                if value is not None:
                    registry.set_setting(key, value)
        registry.set_setting('session-dir', str(root.resolve()))
        if state.path.resolve() != registry.path.resolve() and not state.get_setting('accounts-migrated', False):
            for row in state.connection.execute('SELECT user_id, account_number, enabled FROM accounts ORDER BY account_number').fetchall():
                uid, number, enabled = int(row[0]), int(row[1]), bool(row[2])
                if registry.connection.execute('SELECT 1 FROM account_removed WHERE user_id = ?', (uid,)).fetchone():
                    continue
                if manager.find_by_user(uid) is not None:
                    continue
                if manager.profile(number) is not None:
                    number = manager.next_free_number()
                profile = manager.register(uid, number)
                manager.set_enabled(profile.account_number, enabled)
            state.set_setting('accounts-migrated', True)
        root_id = registry.get_setting('account-root') or registry.get_setting('owner-id')
        if isinstance(root_id, int) and manager.find_by_user(root_id) is not None:
            manager.set_root(root_id)
        manager.actor = parse_user_id(str(state.get_setting('session-name', ''))) or state.get_setting('owner-id')
        return manager

    def set_root(self, user_id: int) -> None:
        self.state.set_setting('account-root', user_id)
        self.link(user_id, user_id)
        for profile in self.items():
            if self.state.connection.execute('SELECT 1 FROM account_links WHERE user_id = ?', (profile.user_id,)).fetchone() is None:
                self.link(profile.user_id, user_id)

    def link(self, user_id: int, owner_id: int) -> None:
        if user_id <= 0 or owner_id <= 0:
            raise AccountError('invalid_owner')
        with self.state.connection:
            self.state.connection.execute('BEGIN IMMEDIATE')
            row = self.state.connection.execute('SELECT owner_id FROM account_links WHERE user_id = ?', (user_id,)).fetchone()
            if row is not None:
                if row[0] != owner_id:
                    raise AccountError('no_permission')
                return
            if user_id != owner_id and user_id in self.owners(owner_id):
                raise AccountError('invalid_owner')
            self.state.connection.execute('INSERT INTO account_links VALUES (?, ?)', (user_id, owner_id))

    def owners(self, user_id: int | None) -> tuple[int, ...]:
        owners: list[int] = []
        seen = {user_id}
        while user_id is not None:
            row = self.state.connection.execute('SELECT owner_id FROM account_links WHERE user_id = ?', (user_id,)).fetchone()
            if row is None or row[0] in seen:
                break
            user_id = int(row[0])
            owners.append(user_id)
            seen.add(user_id)
        return tuple(owners)

    def parent(self, user_id: int) -> int | None:
        row = self.state.connection.execute('SELECT owner_id FROM account_links WHERE user_id = ?', (user_id,)).fetchone()
        return int(row[0]) if row is not None else None

    def protect(self, state: StateStore, user_id: int | None) -> None:
        owners = self.owners(user_id)
        if owners:
            state.set_setting('account-owners', list(owners))
            extra = state.get_setting('access:owners', [])
            state.set_setting('access:owners', list(dict.fromkeys([*owners, *extra])))

    def manageable(self, user_id: int) -> bool:
        return user_id not in self.owners(self.actor)

    def check(self, account_number: int, user_id: int | None = None) -> None:
        profile = self.profile(account_number)
        if user_id is not None and (profile is None or profile.user_id != user_id):
            raise AccountError('not_found', n=account_number)
        if profile is not None and not self.manageable(profile.user_id):
            raise AccountError('no_permission')

    def close(self) -> None:
        if self.owned:
            self.state.close()
            self.owned = False

    def _rows(self) -> list[tuple[Any, ...]]:
        return self.state.connection.execute(
            'SELECT user_id, account_number, vault_name, session_dir, enabled, pid FROM accounts ORDER BY account_number'
        ).fetchall()

    @staticmethod
    def _profile(row: tuple[Any, ...]) -> AccountProfile:
        vault = str(row[2])
        name = vault[:-6] if vault.endswith('.vault') else vault
        return AccountProfile(int(row[0]), int(row[1]), name, Path(str(row[3])), bool(row[4]))

    def items(self) -> tuple[AccountProfile, ...]:
        return tuple(self._profile(row) for row in self._rows())

    def profile(self, account_number: int) -> AccountProfile | None:
        return next((p for p in self.items() if p.account_number == account_number), None)

    def find_by_user(self, user_id: int) -> AccountProfile | None:
        return next((p for p in self.items() if p.user_id == user_id), None)

    def find_by_session(self, session_name: str) -> AccountProfile | None:
        uid = parse_user_id(session_name)
        if uid is not None:
            return self.find_by_user(uid)
        return next((p for p in self.items() if p.session_name == Path(session_name).name), None)

    def next_free_number(self) -> int:
        taken = {p.account_number for p in self.items()}
        number = 1
        while number in taken:
            number += 1
        return number

    def register(self, user_id: int, account_number: int | None = None, session_name: str | None = None, *, owner_id: int | None = None) -> AccountProfile:
        if user_id <= 0 or (account_number is not None and account_number <= 0):
            raise AccountError('invalid_id')
        name = session_name or f'user-{user_id}'
        if parse_user_id(name) != user_id:
            raise AccountError('wrong_session')
        with self.state.connection:
            self.state.connection.execute('BEGIN IMMEDIATE')
            if owner_id is not None:
                if owner_id <= 0 or (user_id != owner_id and user_id in self.owners(owner_id)):
                    raise AccountError('invalid_owner')
                parent = self.parent(user_id)
                if parent is not None and parent != owner_id:
                    raise AccountError('no_permission')
                self.state.connection.execute('INSERT OR IGNORE INTO account_links VALUES (?, ?)', (user_id, owner_id))
            existing = self.find_by_user(user_id)
            if existing is not None:
                return existing
            number = account_number if account_number is not None else self.next_free_number()
            if self.profile(number) is not None:
                raise AccountError('number_taken', n=number)
            profile = AccountProfile(user_id, number, f'user-{user_id}', self.session_dir)
            profile.validate()
            home = account_home(self.session_dir, user_id)
            home.mkdir(parents=True, exist_ok=True)
            home.chmod(0o700)
            self.state.connection.execute(
                'INSERT INTO accounts(user_id, account_number, vault_name, session_dir, enabled, pid) VALUES (?, ?, ?, ?, 1, NULL)',
                (user_id, number, profile.vault_name, str(self.session_dir)),
            )
            self.state.connection.execute('DELETE FROM account_removed WHERE user_id = ?', (user_id,))
        return profile

    def ensure_primary(self, user_id: int, session_name: str) -> AccountProfile:
        return self.register(user_id, session_name=session_name)

    def sync_vaults(self) -> list[AccountProfile]:
        discovered: list[AccountProfile] = []
        for pattern in ('hotaru-*.vault', 'user-*.vault', 'account-*/user-*.vault'):
            for path in sorted(self.session_dir.glob(pattern)):
                match = VAULT_RE.fullmatch(path.name)
                if match is None:
                    continue
                dest = user_vault_path(self.session_dir, int(match.group(1)))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.parent.chmod(0o700)
                if path.exists() and not dest.exists():
                    path.replace(dest)
        for path in sorted(self.session_dir.glob('sanctuary/account-*/user-*.vault')):
            uid = parse_user_id(path.stem)
            if uid is None or path.parent.name != f'account-{uid}' or not path.stat().st_size:
                continue
            if self.find_by_user(uid) is not None:
                continue
            if self.state.connection.execute('SELECT 1 FROM account_removed WHERE user_id = ?', (uid,)).fetchone():
                continue
            discovered.append(self.register(uid))
        return discovered

    def unregister(self, account_number: int) -> bool:
        self.check(account_number)
        profile = self.profile(account_number)
        if profile is None:
            return False
        if self.running_pid(account_number) is not None:
            raise AccountError('stop_first')
        with self.state.connection:
            self.state.connection.execute('INSERT OR IGNORE INTO account_removed(user_id) VALUES (?)', (profile.user_id,))
            self.state.connection.execute('DELETE FROM accounts WHERE account_number = ?', (account_number,))
            if self.state.connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_access'").fetchone():
                self.state.connection.execute('DELETE FROM account_access WHERE user_id = ?', (profile.user_id,))
        return True

    def set_enabled(self, account_number: int, enabled: bool) -> bool:
        self.check(account_number)
        with self.state.connection:
            result = self.state.connection.execute('UPDATE accounts SET enabled = ? WHERE account_number = ?', (int(enabled), account_number))
        return result.rowcount > 0

    def set_pid(self, account_number: int, pid: int | None) -> bool:
        with self.state.connection:
            result = self.state.connection.execute('UPDATE accounts SET pid = ? WHERE account_number = ?', (pid, account_number))
        return result.rowcount > 0

    def running_pid(self, account_number: int) -> int | None:
        child = self.children.get(account_number)
        if child is not None and child.poll() is not None:
            self.children.pop(account_number, None)
        profile = self.profile(account_number)
        if profile is None:
            return None
        path = account_home(profile.session_dir, profile.user_id) / 'process.lock'
        try:
            with path.open('r+') as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raw = handle.read().strip()
                    return int(raw) if raw.isdigit() and int(raw) > 0 else None
        except FileNotFoundError:
            pass
        return None

    def ready(self, account_number: int) -> bool:
        pid = self.running_pid(account_number)
        return pid is not None and self.state.get_setting(f'account-ready:{account_number}') == pid

    async def wait_ready(self, account_number: int, pid: int, *, timeout: float = 120.0) -> str:
        profile = self.profile(account_number)
        deadline = asyncio.get_running_loop().time() + timeout
        while profile is not None:
            current = self.profile(account_number)
            if current is None or current.user_id != profile.user_id or self.running_pid(account_number) != pid:
                return 'stopped'
            if self.state.get_setting(f'account-ready:{account_number}') == pid:
                return 'running'
            if asyncio.get_running_loop().time() >= deadline:
                return 'starting'
            await asyncio.sleep(1)
        return 'stopped'

    def vault_path(self, account_number: int) -> Path | None:
        profile = self.profile(account_number)
        return user_vault_path(profile.session_dir, profile.user_id) if profile is not None else None

    def vault_ready(self, account_number: int) -> bool:
        path = self.vault_path(account_number)
        return path is not None and path.is_file() and path.stat().st_size > 0

    def spawn(self, account_number: int) -> int:
        self.check(account_number)
        profile = self.profile(account_number)
        if profile is None:
            raise AccountError('not_found', n=account_number)
        running = self.running_pid(account_number)
        if running is not None:
            return running
        if not profile.enabled:
            raise AccountError('disabled', n=account_number)
        if not self.vault_ready(account_number):
            raise AccountError('login_first')
        from .config import RuntimeConfig
        account_config(RuntimeConfig.from_database(self.state.path, discover=False), profile)
        lock = AccountLock(profile.session_dir, profile.user_id)
        try:
            env = dict(os.environ)
            env['HOTARU_ACCOUNT_SESSION'] = profile.session_base
            env['HOTARU_ACCOUNT_ROOT'] = str(profile.session_dir)
            env['HOTARU_ACCOUNT_STATE'] = str(account_db_path(profile.session_dir, profile.user_id))
            env['HOTARU_ACCOUNT_LOCK'] = str(lock.fd)
            path = account_home(profile.session_dir, profile.user_id) / 'runtime.log'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'ab') as out:
                process = subprocess.Popen(
                    [sys.executable, '-m', 'hotaru', '--account', str(account_number)],
                    cwd=str(Path(__file__).resolve().parent.parent),
                    stdin=subprocess.DEVNULL, stdout=out, stderr=out, env=env,
                    pass_fds=(lock.fd,), start_new_session=True,
                )
            lock.set_pid(process.pid)
            self.children[account_number] = process
            self.set_pid(account_number, process.pid)
            self.state.set_setting(f'account-ready:{account_number}', None)
            return process.pid
        finally:
            lock.close()

    def stop(self, account_number: int) -> bool:
        self.check(account_number)
        pid = self.running_pid(account_number)
        if pid is None:
            self.set_pid(account_number, None)
            return False
        if pid == os.getpid():
            raise AccountError('current')
        try:
            os.kill(pid, 15)
        except ProcessLookupError:
            pass
        return True

    async def halt(self, account_number: int) -> bool:
        profile = self.profile(account_number)
        if profile is None:
            return False
        stopped = self.stop(account_number)
        for _ in range(50):
            self.check(account_number, profile.user_id)
            if self.running_pid(account_number) is None:
                self.set_pid(account_number, None)
                return stopped
            await asyncio.sleep(0.1)
        raise AccountError('stopping', n=account_number)
