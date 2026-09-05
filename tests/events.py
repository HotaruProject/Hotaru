import asyncio
from unittest.mock import AsyncMock, patch

from goygram.errors import FloodWaitError, MessageNotModifiedError
from goygram.types.cb import CbObj
from hotaru.callbacks import CallbackBinding, CallbackContext, CallbackDenied
from fixtures import Offline


class Events(Offline):
    async def command(self, runtime, kind, **values):
        message = self.message(runtime, **values)
        await runtime.app.core.disp.one({"src": message.src, "data": {**message.raw, "kind": kind}})
        await asyncio.sleep(0)

    def register(self, runtime):
        handler = AsyncMock(return_value="pong")
        runtime.kernel.registry.register("probe", handler, module_id="fixture", kernel=True)
        return handler

    async def testnewcommand(self):
        runtime = self.build(bot_token="654321:offline")
        request = self.request(runtime)
        handler = self.register(runtime)
        await self.command(runtime, "msg")
        handler.assert_awaited_once()
        self.assertEqual(request.await_args.args, ("messages.editMessage",))
        self.assertEqual(request.await_args.kwargs["id"], 71)
        self.assertEqual(request.await_args.kwargs["message"], "pong")
        self.assertEqual(request.await_args.kwargs["peer"], {"_": "inputPeerSelf"})

    async def testeditcommand(self):
        runtime = self.build()
        request = self.request(runtime)
        handler = self.register(runtime)
        await self.command(runtime, "msg", text="plain message")
        handler.assert_not_awaited()
        await self.command(runtime, "edit", text="!probe one two")
        invocation = handler.await_args.args[1]
        self.assertEqual(invocation.source, "edit")
        self.assertEqual(invocation.args, ("one", "two"))
        self.assertEqual(request.await_args.kwargs["id"], 71)

    async def testforeignsender(self):
        runtime = self.build()
        request = self.request(runtime)
        handler = self.register(runtime)
        await self.command(runtime, "edit", from_id=54321, is_me=False)
        handler.assert_not_awaited()
        request.assert_not_awaited()

    async def testduplicateevent(self):
        runtime = self.build()
        self.request(runtime)
        handler = self.register(runtime)
        await self.command(runtime, "msg")
        await self.command(runtime, "msg")
        handler.assert_awaited_once()

    async def testbotcommand(self):
        runtime = self.build(api_id=None, api_hash=None, bot_token="654321:offline")
        request = self.request(runtime)
        handler = self.register(runtime)
        await self.command(runtime, "edit", is_me=False)
        handler.assert_awaited_once()
        self.assertEqual(request.await_args.args, ("editMessageText",))
        self.assertEqual(request.await_args.kwargs["message_id"], 71)

    async def testauxiliaryevents(self):
        runtime = self.build()
        handlers = {family: AsyncMock() for family in ("update", "poll", "member")}
        for family, handler in handlers.items():
            runtime.event_router.register(family, handler)
        for kind in ("update", "poll", "member"):
            await runtime.app.core.disp.one({"src": "mt", "data": {"kind": kind, "update_type": "futureConstructor"}})
        handlers["poll"].assert_awaited_once()
        handlers["member"].assert_awaited_once()
        self.assertGreaterEqual(handlers["update"].await_count, 3)

    async def testunchangededit(self):
        runtime = self.build()
        request = self.request(runtime)
        request.side_effect = MessageNotModifiedError(400, "MESSAGE_NOT_MODIFIED")
        message = self.message(runtime)
        for output in ("edit", "auto"):
            with self.subTest(output=output):
                result = await runtime.responses.answer(message, text="same", output=output)
                self.assertTrue(result.delivered)
                self.assertEqual(result.action, "edit")
        self.assertEqual(request.await_count, 2)
        self.assertTrue(all(call.args == ("messages.editMessage",) for call in request.await_args_list))

    async def testfloodwait(self):
        runtime = self.build()
        request = self.request(runtime)
        request.side_effect = FloodWaitError(420, "FLOOD_WAIT_60", 60)
        with self.assertRaises(FloodWaitError):
            await runtime.responses.answer(self.message(runtime), text="test", output="auto")
        request.assert_awaited_once()

    def callback(self, runtime, handle, **values):
        query = {"id": "fixture-query", "from": {"id": 12345}, "inline_message_id": "fixture-inline", "data": handle}
        query.update(values)
        raw = runtime.app.bot.norm({"update_id": 7, "callback_query": query})
        return CbObj("bot", raw, runtime.app.core)

    async def testinlinecallback(self):
        runtime = self.build(api_id=None, api_hash=None, bot_token="654321:offline")
        request = self.request(runtime)

        async def handler(callback, payload):
            await callback.answer("accepted")
            return await callback.edit(payload)

        runtime.callbacks.register("fixture", handler)
        handle = runtime.callbacks.store.issue(CallbackBinding(12345, None, 0), {"action": "fixture", "payload": "updated"})
        callback = self.callback(runtime, handle)
        await runtime.callbacks.dispatch(callback)
        self.assertEqual(request.await_args.args, ("editMessageText",))
        self.assertEqual(request.await_args.kwargs["inline_message_id"], "fixture-inline")
        self.assertNotIn("chat_id", request.await_args.kwargs)
        with self.assertRaises(CallbackDenied):
            await runtime.callbacks.dispatch(callback)

    async def testcallbackowner(self):
        runtime = self.build(api_id=None, api_hash=None, bot_token="654321:offline")
        handler = AsyncMock()
        runtime.callbacks.register("fixture", handler)
        handle = runtime.callbacks.store.issue(CallbackBinding(12345, None, 0), {"action": "fixture"})
        callback = self.callback(runtime, handle, **{"from": {"id": 54321}})
        with self.assertRaises(CallbackDenied):
            await runtime.callbacks.dispatch(callback)
        handler.assert_not_awaited()

    async def testmessagecallback(self):
        runtime = self.build(api_id=None, api_hash=None, bot_token="654321:offline")
        request = self.request(runtime)
        callback = self.callback(runtime, "fixture", inline_message_id=None, message={"message_id": 71, "chat": {"id": 12345}})
        await CallbackContext(callback).edit("updated")
        self.assertEqual(request.await_args.kwargs["chat_id"], 12345)
        self.assertEqual(request.await_args.kwargs["message_id"], 71)
        self.assertNotIn("inline_message_id", request.await_args.kwargs)

    async def testdocumentdownload(self):
        runtime = self.build()
        document = {"id": 81, "access_hash": 82, "file_reference": "0102", "mime_type": "text/plain", "size": 5}
        message = self.message(runtime, raw_update={"message": {"media": {"document": document}}})
        context = runtime.context_factory.create("fixture", message)
        target = self.root / "document.txt"
        with patch.object(runtime.app.mt, "download_file", new_callable=AsyncMock) as download:
            result = await context.download(destination=target)
        self.assertEqual(result, target)
        download.assert_awaited_once_with({"_": "inputDocumentFileLocation", "id": 81, "access_hash": 82, "file_reference": b"\x01\x02", "thumb_size": ""}, str(target), limit=524288)

    async def testbotdownload(self):
        runtime = self.build(api_id=None, api_hash=None, bot_token="654321:offline")
        message = self.message(runtime, raw={"message": {"document": {"file_id": "fixture-file"}}})
        context = runtime.context_factory.create("fixture", message)
        target = self.root / "document.txt"
        with patch.object(runtime.app.core, "download_file", new_callable=AsyncMock) as download:
            await context.download(destination=target)
        download.assert_awaited_once_with("fixture-file", str(target))

    async def teststate(self):
        runtime = self.build()
        first = runtime.context_factory.create("first", self.message(runtime))
        second = runtime.context_factory.create("second", self.message(runtime))
        first.state.set("counter", 5)
        self.assertEqual(first.state.get("counter"), 5)
        self.assertIsNone(second.state.get("counter"))
