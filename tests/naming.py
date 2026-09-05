import hashlib
from unittest.mock import AsyncMock, patch

from goygram import Session
from fixtures import Offline
from hotaru.config import RuntimeConfig
from hotaru.runtime import Runtime


class Naming(Offline):
    def login(self, runtime, *, userid=12345, source="interactive"):
        runtime.app.session.data = {"auth_key": "01" * 256, "user": {"id": userid}, "dc": 2}
        runtime.app.session.save()
        return {"source": source}

    async def testnewsession(self):
        runtime = self.build(owner_id=54321)
        previous = runtime.app.session.path
        with patch("goygram.security.bootstrap_session", new=AsyncMock(side_effect=lambda *args, **kwargs: self.login(runtime))):
            await runtime.authorize()
        target = self.config.session_dir / "hotaru-12345.vault"
        self.assertEqual(runtime.app.session.path, target)
        self.assertEqual(runtime.app.session.name, str(target.with_suffix("")))
        self.assertEqual(runtime.app.core.session_name, runtime.app.session.name)
        self.assertEqual(runtime.config.session_name, "hotaru-12345")
        self.assertEqual(runtime.state.get_setting("session-name"), "hotaru-12345")
        self.assertEqual(runtime.kernel.owner_id, 54321)
        self.assertFalse(previous.exists())
        self.assertEqual(Session.load(target).self_id, 12345)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        digest = hashlib.sha256(runtime.app.session.name.encode()).hexdigest()[:24]
        self.assertEqual(runtime.app.mt.cursor_path.name, f"{digest}.json")

    async def testqr(self):
        runtime = self.build()
        with patch("goygram.security.bootstrap_session", new=AsyncMock(side_effect=lambda *args, **kwargs: self.login(runtime, source="qr"))):
            await runtime.authorize()
        self.assertEqual(runtime.config.session_name, "hotaru-12345")

    async def testbootstrap(self):
        runtime = self.build(session_name="hotaru-pending-fixture")
        with patch("goygram.security._mt_auth_flow", new=AsyncMock(side_effect=lambda *args, **kwargs: self.login(runtime))) as login:
            await runtime.authorize()
        login.assert_awaited_once()
        self.assertEqual(runtime.app.session.self_id, 12345)
        self.assertEqual(runtime.config.session_name, "hotaru-12345")

    async def testmatchingname(self):
        runtime = self.build(session_name="hotaru-12345")
        with patch("goygram.security.bootstrap_session", new=AsyncMock(side_effect=lambda *args, **kwargs: self.login(runtime))):
            await runtime.authorize()
        self.assertTrue(runtime.app.session.path.exists())
        self.assertEqual(runtime.state.get_setting("session-name"), "hotaru-12345")

    async def testexisting(self):
        runtime = self.build()
        self.login(runtime)
        path = runtime.app.session.path
        before = path.read_bytes()
        with patch.object(runtime.app.mt, "ensure_auth_key", new_callable=AsyncMock), patch("goygram.security._mt_auth_flow", new_callable=AsyncMock) as login:
            await runtime.authorize()
        login.assert_not_awaited()
        self.assertEqual(runtime.app.session.path, path)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(runtime.config.session_name, "fixture")

    async def testcollision(self):
        runtime = self.build(session_name="hotaru-pending-fixture")
        target = self.config.session_dir / "hotaru-12345.vault"
        Session(name="existing", data={"marker": "preserve"}).save(target)
        before = target.read_bytes()
        with patch("goygram.security.bootstrap_session", new=AsyncMock(side_effect=lambda *args, **kwargs: self.login(runtime))), self.assertRaises(FileExistsError):
            await runtime.authorize()
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(runtime.config.session_name, "hotaru-pending-fixture")
        self.assertTrue(runtime.app.session.path.exists())
        self.assertIsNone(runtime.state.get_setting("session-name"))

    async def teststatefailure(self):
        runtime = self.build()
        previous = runtime.app.session.path
        with patch("goygram.security.bootstrap_session", new=AsyncMock(side_effect=lambda *args, **kwargs: self.login(runtime))), patch.object(runtime.state, "set_setting", side_effect=OSError("database unavailable")), self.assertRaises(OSError):
            await runtime.authorize()
        self.assertTrue(previous.exists())
        self.assertEqual(runtime.app.session.path, previous)
        self.assertFalse((self.config.session_dir / "hotaru-12345.vault").exists())

    async def testmissingid(self):
        runtime = self.build()
        with patch("goygram.security.bootstrap_session", new=AsyncMock(side_effect=lambda *args, **kwargs: self.login(runtime, userid=None))), self.assertRaisesRegex(RuntimeError, "no valid Telegram user ID"):
            await runtime.authorize()
        self.assertEqual(runtime.config.session_name, "fixture")
        self.assertTrue(runtime.app.session.path.exists())

    async def testpendingrestore(self):
        runtime = self.build(session_name="hotaru-pending-fixture")
        self.login(runtime)
        with patch.object(runtime.app.mt, "ensure_auth_key", new_callable=AsyncMock), patch("goygram.security._mt_auth_flow", new_callable=AsyncMock) as login:
            await runtime.authorize()
        login.assert_not_awaited()
        self.assertEqual(runtime.config.session_name, "hotaru-12345")

    async def testrestart(self):
        runtime = self.build(owner_id=None)
        for key, value in {"api-id": 12345, "api-hash": "offline-hash", "owner-id": None, "prefix": "!", "session-name": "fixture", "session-dir": str(self.config.session_dir), "backup-keep": 7}.items():
            runtime.state.set_setting(key, value)
        with patch("goygram.security.bootstrap_session", new=AsyncMock(side_effect=lambda *args, **kwargs: self.login(runtime))):
            await runtime.authorize()
        runtime.app.mt.update_cursor({"pts": 42, "seq": 7})
        await runtime.close()
        restarted = Runtime(RuntimeConfig.from_database(self.config.state_path))
        self.addAsyncCleanup(restarted.close)
        restarted.build()
        with patch.object(restarted.app.mt, "ensure_auth_key", new_callable=AsyncMock), patch("goygram.security._mt_auth_flow", new_callable=AsyncMock) as login:
            await restarted.authorize()
        login.assert_not_awaited()
        self.assertEqual(restarted.app.session.self_id, 12345)
        self.assertEqual(restarted.app.session.path.name, "hotaru-12345.vault")
        self.assertEqual(restarted.kernel.owner_id, 12345)
        self.assertEqual(restarted.app.mt.get_cursor(), {"pts": 42, "seq": 7})

    async def testsetup(self):
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=["12345", "", "", str(self.config.session_dir), ""]), patch("hotaru.config.getpass", return_value="offline-hash"):
            config = RuntimeConfig.from_database(self.config.state_path)
        self.assertRegex(config.session_name, r"^hotaru-pending-[0-9a-f]{16}$")
        self.assertIsNone(config.owner_id)
        stored = RuntimeConfig.from_database(self.config.state_path)
        self.assertEqual(stored, config)
