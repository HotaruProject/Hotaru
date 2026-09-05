import asyncio
from unittest.mock import AsyncMock, patch

from fixtures import Offline
from relay.inline import InlineBotInfo, InlineError, InlineManager


class Inline(Offline):
    def metadata(self, **values):
        result = {"id": 654321, "username": "fixture_bot", "is_bot": True}
        result.update(values)
        return {"ok": True, "result": result}

    async def testconfiguredtoken(self):
        runtime = self.build(bot_token="654321:offline")
        with patch("goygram.client.AppCore.bot_req", new=AsyncMock(return_value=self.metadata())) as request, patch.object(InlineManager, "_find_existing_bot", new_callable=AsyncMock) as find:
            info = await runtime.inline.ensure_bot()
        request.assert_awaited_once_with("getMe")
        find.assert_not_awaited()
        self.assertEqual(info.token, "654321:offline")
        self.assertEqual(runtime.state.get_setting("inline-bot-id"), 654321)
        self.assertIsNone(runtime.app.core.bot_token)

    async def teststoredtokenwins(self):
        runtime = self.build(bot_token="123456:offline")
        runtime.state.set_setting("inline-bot-token", "654321:stored")
        with patch.object(InlineManager, "getbot", new=AsyncMock(return_value=InlineBotInfo("654321:stored", "fixture_bot", 654321))) as inspect:
            await runtime.inline.ensure_bot()
        inspect.assert_awaited_once_with("654321:stored")

    async def testincompleteconfig(self):
        runtime = self.build()
        runtime.state.set_setting("inline-bot-token", "654321:offline")
        with patch("goygram.client.AppCore.bot_req", new=AsyncMock(return_value=self.metadata())):
            await runtime.inline.ensure_bot()
        self.assertEqual(runtime.state.get_setting("inline-bot-username"), "fixture_bot")

    async def testrenamedbot(self):
        runtime = self.build()
        runtime.state.set_setting("inline-bot-token", "654321:offline")
        runtime.state.set_setting("inline-bot-id", 654321)
        runtime.state.set_setting("inline-bot-username", "previous_bot")
        with patch("goygram.client.AppCore.bot_req", new=AsyncMock(return_value=self.metadata())):
            info = await runtime.inline.ensure_bot()
        self.assertEqual(info.username, "fixture_bot")

    async def testinvalidtoken(self):
        runtime = self.build(bot_token="654321:offline")
        with patch("goygram.client.AppCore.bot_req", new=AsyncMock(side_effect=RuntimeError("secret token"))), patch.object(InlineManager, "_create_bot", new_callable=AsyncMock) as create, patch.object(InlineManager, "_find_existing_bot", new_callable=AsyncMock) as find:
            with self.assertRaises(InlineError) as error:
                await runtime.inline.ensure_bot()
        self.assertNotIn("secret", str(error.exception))
        create.assert_not_awaited()
        find.assert_not_awaited()
        self.assertIsNone(runtime.state.get_setting("inline-bot-token"))

    async def testidentitymismatch(self):
        runtime = self.build()
        runtime.state.set_setting("inline-bot-token", "654321:offline")
        runtime.state.set_setting("inline-bot-id", 111111)
        with patch("goygram.client.AppCore.bot_req", new=AsyncMock(return_value=self.metadata())), self.assertRaises(InlineError):
            await runtime.inline.ensure_bot()
        self.assertEqual(runtime.state.get_setting("inline-bot-id"), 111111)

    async def testrejectuser(self):
        runtime = self.build()
        with patch("goygram.client.AppCore.bot_req", new=AsyncMock(return_value=self.metadata(is_bot=False))), self.assertRaises(InlineError):
            await runtime.inline.getbot("654321:offline")

    async def testseparateclient(self):
        runtime = self.build(bot_token="654321:offline")
        runtime.inline.info = InlineBotInfo("654321:offline", "fixture_bot", 654321)
        with patch.object(InlineManager, "_run", new_callable=AsyncMock):
            await runtime.inline.start()
            await asyncio.sleep(0)
        self.assertIsNot(runtime.inline.bot_app, runtime.app)
        self.assertIsNone(runtime.inline.bot_app.mt)
        self.assertEqual(runtime.inline.bot_app.core.default_transport, "api")

    async def testduplicatepoller(self):
        runtime = self.build(api_id=None, api_hash=None, bot_token="654321:offline")
        runtime.inline.info = InlineBotInfo("654321:offline", "fixture_bot", 654321)
        with self.assertRaises(InlineError):
            await runtime.inline.start()

    async def testselfshutdown(self):
        runtime = self.build()
        runtime.inline._task = asyncio.current_task()
        runtime.inline.ready.set()
        await runtime.inline.stop_polling()
        self.assertFalse(runtime.inline.ready.is_set())
        self.assertIsNone(runtime.inline._task)

    async def testshutdown(self):
        runtime = self.build()
        runtime.inline.info = InlineBotInfo("654321:offline", "fixture_bot", 654321)
        with patch.object(InlineManager, "_run", new_callable=AsyncMock):
            await runtime.inline.start()
        runtime.inline.ready.set()
        app = runtime.inline.bot_app
        with patch.object(app, "close", new_callable=AsyncMock) as close:
            await runtime.inline.stop()
        close.assert_awaited_once()
        self.assertIsNone(runtime.inline._task)
        self.assertFalse(runtime.inline.ready.is_set())
