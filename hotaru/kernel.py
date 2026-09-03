from __future__ import annotations

import asyncio
import inspect
from collections import OrderedDict
from relay.denylist import is_blocked_peer
from relay.firewall import module_scope
from typing import Any

from .commands import CommandInvocation, CommandParser
from .registry import CommandRegistry
from .security import AccessVerdict, SecurityGate


class Kernel:
    def __init__(
        self,
        registry: CommandRegistry | None = None,
        *,
        parser: CommandParser | None = None,
        owner_id: int | None = None,
        context_factory: Any = None,
        response_service: Any = None,
        form_sender: Any = None,
        seen_limit: int = 4096,
        command_timeout: float = 60.0,
    ) -> None:
        if seen_limit < 1:
            raise ValueError("seen_limit must be positive")
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        self.registry = registry or CommandRegistry()
        self.parser = parser or CommandParser()
        self.owner_id = owner_id
        self.context_factory = context_factory
        self.response_service = response_service
        self.form_sender = form_sender
        self.command_timeout = command_timeout
        self.security: SecurityGate | None = None
        self.sandbox: Any = None
        self._seen: OrderedDict[tuple[str, int | str | None, int], None] = OrderedDict()
        self._seen_limit = seen_limit
        self._running: dict[tuple[int | str | None, int], asyncio.Task[Any]] = {}
        self.suspended = False

    def attach(self, app: Any) -> None:
        app.on_edit(self._on_edit)
        app.on_msg(self._on_msg)

    async def _on_edit(self, message: Any) -> object | None:
        return await self.dispatch(message, source="edit")

    async def _on_msg(self, message: Any) -> object | None:
        asyncio.create_task(self.dispatch_watchers(message), name="hotaru:dispatch_watchers")
        return await self.dispatch(message, source="new")

    async def dispatch_watchers(self, message: Any) -> None:
        if self._is_blocked_peer(message):
            return
        if self.suspended:
            return
        
        watchers = self.registry.get_watchers()
        if not watchers:
            return
            
        payload: dict[str, Any] = {
            "source": "new",
            "message_id": self._message_id(message),
            "chat_id": getattr(message, "chat_id", None),
            "attachment": self._sandbox_attachment(message),
            "reply_attachment": self._sandbox_attachment(self._reply_of(message)),
        }
        topic_id = getattr(message, "topic_id", None) or getattr(message, "message_thread_id", None)
        if isinstance(topic_id, int) and topic_id > 0:
            payload["topic_id"] = topic_id

        for watcher in watchers:
            if self.sandbox is not None and watcher.sandbox:
                asyncio.create_task(
                    self.sandbox.call(watcher.module_id, watcher.name, [], payload, source=message, target=f"watcher_{watcher.name}"),
                    name=f"hotaru:watcher:{watcher.name}"
                )
            else:
                if self.context_factory is not None:
                    context = self.context_factory.create(watcher.module_id, message)
                    asyncio.create_task(
                        self._invoke_watcher(watcher, context, message),
                        name=f"hotaru:watcher:{watcher.name}"
                    )

    async def _invoke_watcher(self, watcher: Any, context: Any, message: Any) -> None:
        try:
            with module_scope():
                result = watcher.handler(context, message)
                if inspect.isawaitable(result):
                    await result
        except Exception:
            pass

    async def dispatch(self, message: Any, *, source: str) -> object | None:
        if self._is_blocked_peer(message):
            return None
        if not self._is_owner(message):
            return None
        if self.security is not None:
            verdict = self.security.check(message, transport="mt", is_group=self._is_group(message))
            if verdict is not AccessVerdict.ALLOW:
                return None
        message_id = self._message_id(message)
        chat_id = getattr(message, "chat_id", None)
        key = (source, chat_id, message_id)
        if source != "edit" and key in self._seen:
            return None
        invocation = self.parser.parse(
            getattr(message, "text", None),
            source=source,
            message_id=message_id,
            chat_id=chat_id,
            message=message,
        )
        if invocation is None:
            return None
        self._remember(key)
        spec = self.registry.resolve(invocation)
        if spec is None:
            swapped = self.parser.swap_invocation(invocation)
            if swapped is not None:
                spec = self.registry.resolve(swapped)
                if spec is not None:
                    invocation = swapped
        if spec is None:
            return None
        if self.suspended and not spec.kernel:
            return None
        task_key = (chat_id, message_id)
        previous = self._running.get(task_key)
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.create_task(
            self._execute(spec, invocation, message),
            name=f"hotaru:cmd:{spec.name}",
        )
        self._running[task_key] = task
        try:
            return await task
        finally:
            if self._running.get(task_key) is task:
                self._running.pop(task_key, None)

    async def _execute(self, spec: Any, invocation: CommandInvocation, message: Any) -> object | None:
        try:
            result = await asyncio.wait_for(
                self._invoke(spec, invocation, message),
                timeout=self.command_timeout,
            )
        except asyncio.TimeoutError:
            if self.response_service is not None:
                return await self.response_service.answer(
                    message,
                    text=f"command timed out after {self.command_timeout:.0f}s: {spec.name}",
                    output="edit",
                )
            return None
        except asyncio.CancelledError:
            return None
        if spec.kernel and self.response_service is not None:
            if isinstance(result, tuple) and len(result) == 2:
                if self.form_sender is not None and result[1]:
                    return await self.form_sender(message, result[0], result[1])
                return await self.response_service.answer(message, text=result[0], buttons=result[1], output="edit")
            if isinstance(result, str):
                return await self.response_service.answer(message, text=result, output="edit")
        return result

    async def _invoke(self, spec: Any, invocation: CommandInvocation, message: Any) -> object:
        if self.sandbox is not None and spec.sandbox:
            result = await self.sandbox_dispatch(spec, invocation)
        else:
            if self.context_factory is None:
                raise RuntimeError("module context factory is not configured")
            context = self.context_factory.create(spec.module_id, message)
            with module_scope():
                result = spec.handler(context, invocation)
        if inspect.isawaitable(result):
            with module_scope():
                return await result
        return result

    async def sandbox_dispatch(self, spec: Any, invocation: CommandInvocation) -> object:
        payload: dict[str, Any] = {
            "source": invocation.source,
            "message_id": invocation.message_id,
            "chat_id": invocation.chat_id,
            "attachment": self._sandbox_attachment(invocation.message),
            "reply_attachment": self._sandbox_attachment(self._reply_of(invocation.message)),
        }
        message = invocation.message
        topic_id = getattr(message, "topic_id", None) or getattr(message, "message_thread_id", None)
        if isinstance(topic_id, int) and topic_id > 0:
            payload["topic_id"] = topic_id
        result = await self.sandbox.call(
            spec.module_id,
            invocation.name,
            list(invocation.args),
            payload,
            source=message,
        )
        if isinstance(result, str):
            return result
        if result is None:
            return None
        return str(result)

    def running(self) -> int:
        return sum(1 for task in self._running.values() if not task.done())

    async def cancel_all(self) -> None:
        tasks = [task for task in self._running.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()

    def register_module_command(self, module_id: str, name: str, handler: Any) -> None:
        if self.context_factory is None:
            raise RuntimeError("module context factory is not configured")
        self.registry.register(name, handler, module_id=module_id)

    def unregister_module_command(self, module_id: str, name: str) -> bool:
        return self.registry.unregister(name, module_id=module_id)

    def _is_owner(self, message: Any) -> bool:
        if bool(getattr(message, "is_me", False)):
            return True
        if self.owner_id is None:
            return False
        if getattr(message, "from_id", None) == self.owner_id:
            return True
        chat_id = getattr(message, "chat_id", None)
        return chat_id == self.owner_id

    @staticmethod
    def _is_blocked_peer(message: Any) -> bool:
        return is_blocked_peer(getattr(message, "from_id", None)) or is_blocked_peer(getattr(message, "chat_id", None))

    @staticmethod
    def _is_group(message: Any) -> bool:
        chat_id = getattr(message, "chat_id", None)
        return isinstance(chat_id, int) and (chat_id < 0 or chat_id > 1000000000000)

    @staticmethod
    def _message_id(message: Any) -> int:
        value = getattr(message, "id", None)
        if value is None:
            value = getattr(message, "message_id", None)
        if not isinstance(value, int):
            raise ValueError("message must expose an integer id")
        return value

    def _remember(self, key: tuple[str, int | str | None, int]) -> None:
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > self._seen_limit:
            self._seen.popitem(last=False)

    @staticmethod
    def _reply_of(message: Any) -> Any:
        reply = getattr(message, "reply_to_message", None)
        if reply is not None:
            return reply
        return getattr(message, "reply", None)

    @classmethod
    def _sandbox_attachment(cls, message: Any) -> dict[str, Any] | None:
        if message is None or not hasattr(message, "get"):
            return None
        for kind in ("document", "photo", "video", "audio", "voice", "animation", "video_note", "sticker"):
            media = message.get(kind)
            if media is None:
                continue
            if isinstance(media, list):
                media = media[-1] if media else None
            if not isinstance(media, dict):
                continue
            return {
                "kind": kind,
                "file_name": media.get("file_name") or media.get("name"),
                "mime_type": media.get("mime_type"),
                "size": media.get("size") if isinstance(media.get("size"), int) else None,
            }
        media_wrap = message.get("media")
        if isinstance(media_wrap, dict):
            document = media_wrap.get("document")
            if isinstance(document, dict):
                return {"kind": "document", "file_name": document.get("file_name"), "mime_type": document.get("mime_type"), "size": document.get("size") if isinstance(document.get("size"), int) else None}
            photo = media_wrap.get("photo")
            if isinstance(photo, dict):
                return {"kind": "photo", "file_name": None, "mime_type": "image/jpeg", "size": None}
        return None
