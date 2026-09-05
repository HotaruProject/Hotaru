from unittest.mock import AsyncMock, PropertyMock, patch

from fixtures import Offline
from hotaru.runtime import Runtime
from relay.inline import InlineManager


class Lifecycle(Offline):
    def restore(self, runtime, **values):
        runtime.app.session.data = {"auth_key": "01" * 256, "user": {"id": 12345}, "dc": 2, **values}
        return {"source": "vault"}

    async def testowner(self):
        runtime = self.build(owner_id=None, bot_token="654321:offline")
        with patch("goygram.security.bootstrap_session", new=AsyncMock(side_effect=lambda *args, **kwargs: self.restore(runtime))) as authorize:
            await runtime.authorize()
        self.assertEqual(runtime.kernel.owner_id, 12345)
        self.assertEqual(runtime.security.owner_id, 12345)
        self.assertNotIn("bot_token", authorize.await_args.kwargs)
        self.assertIs(authorize.await_args.kwargs["session"], runtime.app.session)

    async def testconfiguredowner(self):
        runtime = self.build(owner_id=54321)
        with patch("goygram.security.bootstrap_session", new=AsyncMock(side_effect=lambda *args, **kwargs: self.restore(runtime))):
            await runtime.authorize()
        self.assertEqual(runtime.kernel.owner_id, 54321)

    async def testrejectbotsession(self):
        runtime = self.build()
        with patch("goygram.security.bootstrap_session", new=AsyncMock(side_effect=lambda *args, **kwargs: self.restore(runtime, is_bot=True))), self.assertRaisesRegex(RuntimeError, "must belong to a user"):
            await runtime.authorize()

    async def testauthfailure(self):
        runtime = self.build()
        with patch("goygram.security.bootstrap_session", new=AsyncMock(return_value=None)), patch.object(InlineManager, "start", new_callable=AsyncMock) as inline, self.assertRaisesRegex(RuntimeError, "did not complete"):
            await runtime.run()
        inline.assert_not_awaited()
        self.assertTrue(runtime.closed)

    async def testbotonly(self):
        runtime = self.build(api_id=None, api_hash=None, bot_token="654321:offline")
        with patch("goygram.security.bootstrap_session", new_callable=AsyncMock) as authorize:
            await runtime.authorize()
        authorize.assert_not_awaited()

    async def teststartuporder(self):
        runtime = self.build()
        order = []

        async def authorize(*args, **kwargs):
            order.append("authorize")
            return self.restore(runtime)

        async def inline():
            order.append("inline")

        async def run():
            order.append("run")

        with patch("goygram.security.bootstrap_session", new=authorize), patch.object(InlineManager, "start", new=AsyncMock(side_effect=inline)), patch.object(runtime.app, "run", new=run), patch.object(Runtime, "constellations_dir", new_callable=PropertyMock, return_value=self.root / "modules"):
            await runtime.run()
        self.assertEqual(order, ["authorize", "inline", "run"])
