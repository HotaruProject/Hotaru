from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import secrets
import sqlite3
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from goygram.ext import aes_gcm_encrypt

from hotaru.backup import BackupError, BackupService, MAGIC, MAGIC_V2
from hotaru.botlogin import EchoBot
from hotaru.deps import satisfies
from hotaru.ratelimit import ApiRateLimiter
from hotaru.registry import CommandRegistry


MODULE = b'HOTARU = {"id": "example", "version": "1", "commands": [], "capabilities": []}\n'


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = BackupService(max_bytes=4096)

    def seal(self, payload: bytes, magic: bytes = MAGIC) -> Path:
        nonce = secrets.token_bytes(12)
        archive = self.root / "test.hbk"
        archive.write_bytes(magic + nonce + aes_gcm_encrypt(self.service._key("test"), nonce, payload, magic))
        return archive

    def archive(self, entries: list[tuple[str, bytes]], *, legacy: bool = False) -> Path:
        manifest = {
            "format": 1,
            "metadata": {"label": "test"},
            "files": {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in entries},
        }
        if not legacy:
            return self.seal(self.service._pack_container(manifest, entries))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, data in entries:
                archive.writestr(name, data)
        return self.seal(buffer.getvalue(), MAGIC_V2)

    def test_round_trip_both_formats(self) -> None:
        database = self.root / "state.sqlite3"
        with contextlib.closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE module_state (module_id TEXT, key TEXT, value TEXT)")
        entries = [("modules/example.hmod", MODULE), ("state/state.sqlite3", database.read_bytes())]
        self.service = BackupService()
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                archive = self.archive(entries, legacy=legacy)
                self.assertEqual(set(self.service.dry_run(archive, key="test")["files"]), {name for name, _ in entries})
                staged = self.service.stage(archive, self.root / str(legacy), key="test")
                for name, data in entries:
                    self.assertEqual((staged / name).read_bytes(), data)

    def test_unsafe_paths_rejected_before_staging(self) -> None:
        names = (
            "modules/../../escaped.hmod", "modules/../escaped.hmod", "modules//example.hmod",
            "modules/./example.hmod", "modules/nested/example.hmod", "/tmp/example.hmod",
            "modules/..\\example.hmod", "modules/example.py", "modules/example\x00.hmod",
        )
        for legacy in (False, True):
            for name in names:
                with self.subTest(legacy=legacy, name=name):
                    archive = self.archive([(name, MODULE)], legacy=legacy)
                    destination = self.root / "stage"
                    for action in (self.service.dry_run, lambda path, **kw: self.service.stage(path, destination, **kw)):
                        with self.assertRaises(BackupError):
                            action(archive, key="test")
                    self.assertFalse(destination.exists())
                    self.assertFalse((self.root / "escaped.hmod").exists())

    def test_expanded_limit_is_cumulative(self) -> None:
        for entries in (
            [("modules/example.hmod", MODULE + b" " * 100000)],
            [("modules/one.hmod", MODULE + b" " * 2500), ("modules/two.hmod", MODULE + b" " * 2500)],
        ):
            archive = self.archive(entries)
            self.assertLess(archive.stat().st_size, self.service.max_bytes)
            for action in (self.service.dry_run, self.service.stage):
                with self.assertRaises(BackupError):
                    action(archive, key="test")

    def test_malformed_containers_rejected(self) -> None:
        archive = self.archive([("modules/example.hmod", MODULE)])
        _, payload = self.service._unseal(archive, "test")
        bad_length = bytearray(payload)
        manifest_length, = struct.unpack_from(">I", payload, 2)
        record = 6 + manifest_length + 4 + 2 + len("modules/example.hmod")
        struct.pack_into(">Q", bad_length, record, 1)
        corrupted = payload[:-1] + bytes([payload[-1] ^ 1])
        bad_manifest = self.service._pack_container({"format": 1, "files": {}}, [("modules/example.hmod", MODULE)])
        unsupported = bytes([2]) + payload[1:]
        for raw in (bytes(bad_length), corrupted, payload + b"extra", bad_manifest, unsupported):
            with self.subTest(raw=raw[:6]):
                archive = self.seal(raw)
                for action in (self.service.dry_run, self.service.stage):
                    with self.assertRaises(BackupError):
                        action(archive, key="test")

    def test_duplicate_entries_rejected(self) -> None:
        archive = self.archive([("modules/example.hmod", MODULE)] * 2)
        with self.assertRaisesRegex(BackupError, "duplicate"):
            self.service.dry_run(archive, key="test")

    def test_expanded_limit_accepts_exact_boundary(self) -> None:
        data = MODULE + b" " * (self.service.max_bytes - len(MODULE))
        archive = self.archive([("modules/example.hmod", data)])
        staged = self.service.stage(archive, self.root / "stage", key="test")
        self.assertEqual((staged / "modules/example.hmod").read_bytes(), data)

    def test_staging_allows_symlink_ancestors(self) -> None:
        archive = self.archive([("modules/example.hmod", MODULE)])
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(real, target_is_directory=True)
        self.service.stage(archive, alias / "stage", key="test")
        self.assertEqual((real / "stage/modules/example.hmod").read_bytes(), MODULE)

    def test_staging_rejects_symlinks_and_preserves_existing_files(self) -> None:
        archive = self.archive([("modules/example.hmod", MODULE)])
        outside = self.root / "outside"
        outside.mkdir(mode=0o755)
        for child in (False, True):
            with self.subTest(child=child):
                destination = self.root / f"stage-{child}"
                if child:
                    destination.mkdir()
                    (destination / "modules").symlink_to(outside, target_is_directory=True)
                else:
                    destination.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(BackupError):
                    self.service.stage(archive, destination, key="test")
                self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
                self.assertFalse((outside / "example.hmod").exists())
        destination = self.root / "existing"
        (destination / "modules").mkdir(parents=True)
        existing = destination / "modules/example.hmod"
        existing.write_bytes(b"keep")
        with self.assertRaises(BackupError):
            self.service.stage(archive, destination, key="test")
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_create_respects_expanded_limit(self) -> None:
        module = self.root / "example.hmod"
        module.write_bytes(MODULE + b" " * 5000)
        archive = self.root / "output.hbk"
        with self.assertRaisesRegex(BackupError, "expanded"):
            self.service.create(archive, module_paths=[module], key="test")
        self.assertFalse(archive.exists())
        module.write_bytes(MODULE)
        self.service.create(archive, module_paths=[module], key="test")
        self.assertEqual(self.service.dry_run(archive, key="test")["files"], ("modules/example.hmod",))

    def test_create_rejects_duplicate_names(self) -> None:
        modules = [self.root / folder / "example.hmod" for folder in ("one", "two")]
        for module in modules:
            module.parent.mkdir()
            module.write_bytes(MODULE)
        with self.assertRaisesRegex(BackupError, "duplicate"):
            self.service.create(self.root / "output.hbk", module_paths=modules, key="test")


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_owner_private_replies_are_consumed(self) -> None:
        bot = EchoBot(1, "test", "test", Path("/tmp"))
        bot.owner = 111
        bot.app = SimpleNamespace(mt_messages_delete_messages=AsyncMock())
        bot._pending = asyncio.Queue()
        for sender, chat in ((222, 222), (111, -10), (111, None)):
            await bot._on_message(SimpleNamespace(from_id=sender, chat_id=chat, text="foreign", id=1))
        self.assertTrue(bot._pending.empty())
        bot.app.mt_messages_delete_messages.assert_not_awaited()
        await bot._on_message(SimpleNamespace(from_id=111, chat_id=111, text="answer", id=2))
        self.assertEqual(bot._pending.get_nowait(), "answer")
        bot.app.mt_messages_delete_messages.assert_awaited_once_with(id=[2], revoke=True)

    async def test_owner_discovery_requires_private_chat(self) -> None:
        bot = EchoBot(1, "test", "test", Path("/tmp"))
        bot.app = SimpleNamespace(send_msg=AsyncMock(return_value={}))
        await bot._on_message(SimpleNamespace(from_id=111, chat_id=-10, text="hello"))
        self.assertIsNone(bot.owner)
        await bot._on_message(SimpleNamespace(from_id=111, chat_id=111, text="hello"))
        self.assertEqual(bot.owner, 111)
        self.assertTrue(bot._event.is_set())


class RegistryTests(unittest.TestCase):
    def test_bot_collisions_preserve_original_handler(self) -> None:
        for kernel in (False, True):
            with self.subTest(kernel=kernel):
                registry = CommandRegistry()
                handler = lambda: None
                registry.register_bot("start", handler, module_id="original", kernel=kernel)
                with self.assertRaises(ValueError):
                    registry.register_bot("START", lambda: None, module_id="external", sandbox=True)
                self.assertIs(registry.resolve_bot_name("start").handler, handler)
                self.assertFalse(registry.unregister_bot("start", module_id="external"))
                self.assertTrue(registry.unregister_bot("start", module_id="original"))
                registry.register_bot("start", handler, module_id="original", kernel=kernel)


class RateLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_calls_share_cooldown_after_caller_cancellation(self) -> None:
        limiter = ApiRateLimiter(SimpleNamespace(state=None, observatory=None, responses=None))
        limiter.threshold = 1
        entered = asyncio.Event()
        release = asyncio.Event()

        async def cooldown(count: int) -> None:
            entered.set()
            await release.wait()

        limiter._trigger_cooldown = cooldown
        await limiter.check("first")
        trigger = asyncio.create_task(limiter.check("trigger"))
        await entered.wait()
        waiting = [asyncio.create_task(limiter.check("parallel")) for _ in range(3)]
        await asyncio.sleep(0)
        self.assertTrue(all(not task.done() for task in waiting))
        trigger.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await trigger
        self.assertTrue(all(not task.done() for task in waiting))
        release.set()
        await asyncio.wait_for(asyncio.gather(*waiting), 1)
        self.assertEqual(limiter.total_blocked, 1)

    async def test_disabled_and_suspended_protection(self) -> None:
        limiter = ApiRateLimiter(SimpleNamespace(state=None))
        limiter.threshold = 0
        limiter.enabled = False
        await limiter.check("disabled")
        limiter.enabled = True
        limiter.suspend(1)
        await limiter.check("suspended")
        self.assertEqual(limiter.total_blocked, 0)


class VersionTests(unittest.TestCase):
    def test_dependency_specifiers(self) -> None:
        cases = (
            ("1.2.3", "==", "1.*", True), ("1.2.3", "!=", "1.*", False),
            ("10.0", "==", "1.*", False), ("1.9.0", "~=", "1.4", True),
            ("2.0", "~=", "1.4", False), ("1.4.9", "~=", "1.4.5", True),
            ("1.5", "~=", "1.4.5", False), ("1.0.0rc1", ">=", "1.0.0", False),
            ("1.0.0", ">", "1.0.0rc1", True), ("1.0.post1", ">", "1.0", False),
            ("1.0.post1", ">=", "1.0", True),
            ("1.0.dev1", "<", "1.0a1", True), ("1!1.0", ">", "2.0", True),
            ("1.0+cpu", "==", "1.0", True), ("1.0", "==", "1.0.0", True),
        )
        for installed, operator, wanted, expected in cases:
            with self.subTest(installed=installed, operator=operator, wanted=wanted):
                self.assertEqual(satisfies(installed, operator, wanted), expected)


if __name__ == "__main__":
    unittest.main()
