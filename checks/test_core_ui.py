"""Run with python -m unittest discover -s checks -v."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
from pathlib import Path
import sqlite3
import struct
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

# These tests do not use POSIX account locks.
if sys.platform == "win32":
    sys.modules.setdefault("fcntl", types.ModuleType("fcntl"))

from goygram import ext
from goygram.schema_manager import init_schema
from hotaru.branding import Branding, banner
from hotaru.callbacks import CallbackBinding, CallbackDenied, CallbackStore
from hotaru.modules import HmodLoader
from hotaru.screens import Kit, ScreenEngine

_root = Path(__file__).resolve().parents[1]


def module(name):
    loaded = HmodLoader().load(_root / "constellations" / (name + ".hmod"))
    namespace = {"__name__": "test_" + name}
    exec(compile(loaded.source, str(loaded.path), "exec"), namespace)
    return loaded, namespace


class Memory:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    get_setting = get
    set_setting = set


class Callbacks(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.runtime = types.SimpleNamespace(state=types.SimpleNamespace(connection=self.db), language=lambda: "en")
        self.engine = ScreenEngine(self.runtime, "test-secret")

    def tearDown(self):
        self.db.close()

    def test_encrypted_roundtrip_restart_and_tampering(self):
        token = self.engine.encode(12345, 17, 9)
        self.assertEqual(len(token), 49)
        self.assertNotEqual(token, self.engine.encode(12345, 17, 9))
        self.assertEqual(ScreenEngine(self.runtime, "test-secret").decode(token), (12345, 17, 9))
        self.assertIsNone(ScreenEngine(self.runtime, "other-secret").decode(token))
        raw = bytearray(base64.urlsafe_b64decode(token[1:]))
        for i in range(len(raw)):
            changed = raw.copy()
            changed[i] ^= 1
            self.assertIsNone(self.engine.decode("~" + base64.urlsafe_b64encode(changed).decode()))

    def test_old_signed_tokens_and_settings_migration(self):
        form = self.engine._create("config", 10, "conf", 10, bot=False, ttl=3600)
        form.actions = [{"m": "config", "f": "home", "k": "go"}]
        self.engine._save(form)
        body = struct.pack("<BIHB", 1, form.id, 0, 0)
        tag = hmac.new(self.engine._key, body, hashlib.sha256).digest()[:8]
        old = "~" + base64.urlsafe_b64encode(body + tag).decode().rstrip("=")
        restarted = ScreenEngine(self.runtime, "test-secret")
        self.assertEqual(restarted.decode(old), (form.id, 0, 0))
        restored = restarted.load(form.id)
        self.assertEqual(restored.module, "settings")
        self.assertEqual(restored.actions[0]["m"], "settings")

    def test_core_store_still_binds_actor_chat_and_message(self):
        store = CallbackStore(secret=b"x" * 32, connection=self.db)
        binding = CallbackBinding(10, 20, 30)
        token = store.issue(binding, {"action": "test"})
        for wrong in [CallbackBinding(11, 20, 30), CallbackBinding(10, 21, 30), CallbackBinding(10, 20, 31)]:
            with self.assertRaises(CallbackDenied):
                store.peek(token, wrong)
        restarted = CallbackStore(secret=b"x" * 32, connection=self.db)
        self.assertEqual(restarted.consume(token, binding), {"action": "test"})
        with self.assertRaises(CallbackDenied):
            restarted.consume(token, binding)

    def test_text_keyboard_and_input_buttons_share_codec(self):
        form = self.engine._create("settings", 10, "settings", 10, bot=False, ttl=3600)
        kit = Kit(self.engine, None)
        screen = kit.screen(kit.tap("Inline", "home"), [[kit.button("Keyboard", "home"), kit.input("Input", "home", "Enter")]])
        text, rows, actions, gen = self.engine._compile(form, screen)
        import re
        tokens = [rows[0][0]["callback_data"], rows[0][1]["switch_inline_query_current_chat"].strip(), re.search(r'data="([^"]+)"', text)[1]]
        self.assertEqual(len(actions), 3)
        for index, token in enumerate(tokens):
            self.assertEqual(self.engine.decode(token), (form.id, gen, index))

    def test_merged_settings_commands_are_bound(self):
        loaded, ns = module("settings")
        self.assertFalse((_root / "constellations/config.hmod").exists())
        self.assertIn("conf", loaded.manifest.commands)
        for command in loaded.manifest.commands:
            self.assertTrue(callable(ns["command_" + command]), command)


class Backups(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _, self.ns = module("backups")
        self.ctx = types.SimpleNamespace(
            config=Memory(enabled=True, interval_hours=24), state=Memory(),
            forum=types.SimpleNamespace(ensure_topic=AsyncMock(return_value=44), send_file=AsyncMock()),
            runtime=types.SimpleNamespace(create_backup=Mock(return_value=Path("test.hbk")), language=lambda: "en"),
        )

    async def test_failed_delivery_is_retried_without_advancing_schedule(self):
        self.ctx.forum.send_file.side_effect = RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            await self.ns["_send"](self.ctx, automatic=True)
        self.assertIsNone(self.ctx.state.get("last_sent"))
        self.ctx.forum.send_file.side_effect = None
        await self.ns["_send"](self.ctx, automatic=True)
        self.assertGreater(self.ctx.state.get("last_sent"), 0)
        self.ctx.forum.send_file.assert_awaited_with(44, "test.hbk", "Hotaru backup", file_name="test.hbk")

    async def test_schedule_disable_and_concurrent_ticks(self):
        await asyncio.gather(self.ns["_send"](self.ctx, automatic=True), self.ns["_send"](self.ctx, automatic=True))
        self.ctx.forum.send_file.assert_awaited_once()
        self.ctx.state.set("last_sent", 0)
        self.ctx.config.set("enabled", False)
        self.assertFalse(await self.ns["_send"](self.ctx, automatic=True))
        self.ctx.forum.send_file.assert_awaited_once()

    async def test_no_archive_created_when_forum_unavailable(self):
        self.ctx.forum.ensure_topic.return_value = None
        with self.assertRaises(RuntimeError):
            await self.ns["_send"](self.ctx)
        self.ctx.runtime.create_backup.assert_not_called()


class Artwork(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        init_schema(ext)

    async def asyncSetUp(self):
        def uploaded(**kw):
            kind = "photo" if kw["media"]["_"] == "inputMediaUploadedPhoto" else "document"
            return {"ok": True, "result": {kind: {"id": 100, "access_hash": 200, "file_reference": b"test"}}}
        self.app = types.SimpleNamespace(
            mt=types.SimpleNamespace(resolve_peer=AsyncMock(return_value=ext.serialize_constructor("inputPeerUser", {"user_id": 22, "access_hash": 3}))),
            charged_upload=AsyncMock(return_value={"id": 1, "parts": 1, "name": "asset", "md5": ""}),
            mt_messages_upload_media=AsyncMock(side_effect=uploaded),
            mt_channels_edit_photo=AsyncMock(), mt_photos_upload_profile_photo=AsyncMock(),
        )
        self.runtime = types.SimpleNamespace(
            app=self.app, state=Memory(), observatory=None,
            inline=types.SimpleNamespace(bot_app=self.app, info=types.SimpleNamespace(bot_id=22, username="testbot")),
        )
        self.brand = Branding(self.runtime)

    async def test_bot_avatar_before_forum_exists(self):
        await self.brand.apply_bot_avatar(self.runtime.inline.info)
        await self.brand.apply_bot_avatar(self.runtime.inline.info)
        self.app.mt_photos_upload_profile_photo.assert_awaited_once()
        self.app.mt_channels_edit_photo.assert_not_awaited()
        self.assertEqual(self.app.mt_photos_upload_profile_photo.call_args.kwargs["bot"]["user_id"], 22)

    async def test_creation_flow_applies_avatar(self):
        from relay.inline import InlineManager, InlineBotInfo
        manager = InlineManager.__new__(InlineManager)
        manager.runtime = self.runtime
        self.runtime.branding = self.brand
        manager._owner_id = Mock(return_value=1)
        manager._find_existing_bot = AsyncMock(return_value=None)
        manager._create_gate = Mock()
        manager._provision_lock = asyncio.Lock()
        manager._create_bot = AsyncMock(return_value=InlineBotInfo("test", "testbot", 22))
        manager._tune_bot = AsyncMock()
        manager._configure = AsyncMock()
        manager._start_bot_chat = AsyncMock()
        with patch("relay.inline.BotFatherGuard", return_value=AsyncMock()), patch("relay.inline.BotFatherConversation", return_value=AsyncMock()):
            result = await manager.ensure_bot()
        await asyncio.sleep(0)
        self.assertEqual(result.bot_id, 22)
        manager._create_bot.assert_awaited_once()
        self.app.mt_photos_upload_profile_photo.assert_awaited_once()

    async def test_native_rich_files_and_cache(self):
        text = banner("help") + banner()
        first = await self.brand.rich(text)
        self.assertEqual(len(first["files"]), 2)
        self.assertTrue(ext.serialize_constructor("inputRichMessageHTML", first))
        await self.brand.rich(text)
        self.assertEqual(self.app.mt_messages_upload_media.await_count, 2)
        self.brand._cache.clear()
        await self.brand.rich(text)
        self.assertEqual(self.app.mt_messages_upload_media.await_count, 4)

    async def test_banner_failure_keeps_menu_content(self):
        self.app.mt_messages_upload_media.side_effect = RuntimeError("offline")
        payload = await self.brand.rich(banner("help") + "<b>Menu</b>")
        self.assertIn("<b>Menu</b>", payload["html"])
        self.assertNotIn("tg://video", payload["html"])
        self.assertNotIn("files", payload)

    async def test_avatars_once_per_target_and_retry_failed_target(self):
        forum = types.SimpleNamespace(_user_channel=lambda _: {"_": "inputChannel", "channel_id": 1, "access_hash": 2}, _user_bot=AsyncMock(return_value={"_": "inputUser", "user_id": 22, "access_hash": 3}))
        self.app.mt_photos_upload_profile_photo.side_effect = RuntimeError("offline")
        await self.brand.apply_avatars(forum, -1000000000001)
        self.app.mt_photos_upload_profile_photo.side_effect = None
        await self.brand.apply_avatars(forum, -1000000000001)
        await self.brand.apply_avatars(forum, -1000000000001)
        self.app.mt_channels_edit_photo.assert_awaited_once()
        self.assertEqual(self.app.mt_photos_upload_profile_photo.await_count, 2)
        self.assertEqual(self.app.mt_photos_upload_profile_photo.call_args.kwargs["bot"]["user_id"], 22)


if __name__ == "__main__":
    unittest.main()
