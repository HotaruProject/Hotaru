from dataclasses import replace
from unittest.mock import AsyncMock, patch

from goygram import Session
from goygram.security import bootstrap_session
from fixtures import Offline


class Transport(Offline):
    async def testuser(self):
        runtime = self.build()
        self.assertIsNone(runtime.app.bot)
        self.assertIsNotNone(runtime.app.mt)
        self.assertEqual(runtime.app.via(12345), "mt")
        self.assertIsInstance(runtime.app.session, Session)
        self.assertEqual(runtime.app.session.path, self.root / "sessions/fixture.vault")
        self.assertIs(runtime.build(), runtime.app)

    async def testuserwithtoken(self):
        runtime = self.build(bot_token="123456:offline")
        core = runtime.app.core
        self.assertIsNone(core.bot)
        self.assertIsNone(core.bot_token)
        self.assertEqual(core.default_transport, "mtproto")
        with patch("goygram.security._mt_auth_flow", new_callable=AsyncMock) as user, patch("goygram.security._mt_bot_auth_flow", new_callable=AsyncMock) as bot:
            await bootstrap_session(core, api_id=core.api_id, api_hash=core.api_hash, bot_token=core.bot_token, session=core.session)
        user.assert_awaited_once()
        bot.assert_not_awaited()

    async def testbotonly(self):
        runtime = self.build(api_id=None, api_hash=None, bot_token="123456:offline")
        self.assertIsNone(runtime.app.mt)
        self.assertIsNotNone(runtime.app.bot)
        self.assertEqual(runtime.app.via(12345), "bot")
        self.assertEqual(runtime.app.core.default_transport, "api")

    async def testpartialcredentials(self):
        for values in ({"api_id": None}, {"api_hash": None}, {"api_hash": " "}, {"bot_token": " "}):
            with self.subTest(values=tuple(values)), self.assertRaises(ValueError):
                replace(self.config, **values).validate()

    async def testaliases(self):
        runtime = self.build(bot_token="123456:offline")
        for value in ("mt", "mtproto"):
            self.assertEqual(runtime.app.via(12345, via=value), "mt")
        for value in ("api", "bot"):
            with self.assertRaises(RuntimeError):
                runtime.app.via(12345, via=value)
        with self.assertRaises(ValueError):
            runtime.app.via(12345, via="invalid")

    async def testexactrpc(self):
        runtime = self.build(bot_token="123456:offline")
        request = self.request(runtime)
        await runtime.app.mt_req("updates.getState")
        request.assert_awaited_once_with("updates.getState", api_id=12345, api_hash="offline-hash")

    async def testvault(self):
        runtime = self.build()
        session = runtime.app.session
        session.data = {"user": {"id": 12345}, "auth_key": "01" * 256, "dc": 2}
        path = session.save()
        restored = Session.load(path, name=session.name)
        self.assertEqual(restored.to_dict(), session.to_dict())
        self.assertEqual(path.read_bytes()[:4], b"GGV2")
        renamed = self.root / "renamed.vault"
        path.rename(renamed)
        self.assertEqual(Session.load(renamed).to_dict(), session.to_dict())

    async def teststring(self):
        session = Session(name="fixture", data={"user": {"id": 12345}, "auth_key": "01" * 256, "dc": 2})
        restored = Session.from_string(session.export_string(), name="renamed")
        self.assertEqual(restored.to_dict(), session.to_dict())

    async def testrestore(self):
        runtime = self.build()
        session = runtime.app.session
        session.data = {"user": {"id": 12345}, "auth_key": "01" * 256, "dc": 2}
        session.save()
        session.data = {}
        with patch.object(runtime.app.mt, "ensure_auth_key", new_callable=AsyncMock), patch("goygram.security._mt_auth_flow", new_callable=AsyncMock) as login:
            await bootstrap_session(runtime.app.core, session=session)
        login.assert_not_awaited()
        self.assertEqual(session.self_id, 12345)
        self.assertEqual(runtime.app.core.self_id, 12345)
        self.assertEqual(runtime.app.mt.auth_key, bytes.fromhex("01" * 256))
