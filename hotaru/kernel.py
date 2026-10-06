from __future__ import annotations

import asyncio
import inspect
import logging
from collections import OrderedDict
from relay.denylist import is_blocked_peer
from relay.firewall import module_scope
from typing import Any, Protocol, cast

from .commands import CommandInvocation, CommandParser
from .registry import CommandRegistry, CommandSpec
from .inline_registry import InlineRegistry
from .security import AccessVerdict, SecurityGate
from .access import AccessManager, Permission
from .tasks import TaskLimitError, TaskSupervisor

log = logging.getLogger(__name__)


class SupportsGet(Protocol):
    def get(self, key: str, default: object = None) -> object: ...


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
    ) -> None:
        if seen_limit < 1:
            raise ValueError("seen_limit must be positive")
        self.registry = registry or CommandRegistry()
        self.inline_registry = InlineRegistry()
        self.parser = parser or CommandParser()
        self.owner_id = owner_id
        self.access: AccessManager | None = None
        self.context_factory = context_factory
        self.response_service = response_service
        self.form_sender = form_sender
        self.security: SecurityGate | None = None
        self.sandbox: Any = None
        self.tasks = TaskSupervisor()
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
        await self.dispatch_watchers(message)
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
        reply_header = message.get("reply_to") if hasattr(message, "get") else None
        if isinstance(reply_header, dict):
            payload["reply_to"] = reply_header
        topic_id = getattr(message, "topic_id", None) or getattr(message, "message_thread_id", None)
        if isinstance(topic_id, int) and topic_id > 0:
            payload["topic_id"] = topic_id

        for watcher in watchers:
            try:
                if self.sandbox is not None and watcher.sandbox:
                    self.tasks.spawn(
                        watcher.module_id,
                        self._invoke_sandbox_watcher(watcher, payload, message),
                        name=f"hotaru:watcher:{watcher.name}"
                    )
                elif self.context_factory is not None:
                    context = self.context_factory.create(watcher.module_id, message)
                    self.tasks.spawn(
                        watcher.module_id,
                        self._invoke_watcher(watcher, context, message),
                        name=f"hotaru:watcher:{watcher.name}"
                    )
            except TaskLimitError:
                continue

    async def _invoke_sandbox_watcher(self, watcher: Any, payload: Any, message: Any) -> None:
        try:
            assert self.sandbox is not None
            await self.sandbox.call(watcher.module_id, watcher.name, [], payload, source=message, target=f"watcher_{watcher.name}")
        except Exception as exc:
            log.error("sandbox watcher failed: %s", type(exc).__name__)

    async def _invoke_watcher(self, watcher: Any, context: Any, message: Any) -> None:
        try:
            with module_scope(getattr(watcher, "module_id", "")):
                result = watcher.handler(context, message)
                if inspect.isawaitable(result):
                    await result
        except Exception as exc:
            log.error("watcher failed: %s", type(exc).__name__)

    def command_name(self, text: str | None) -> str | None:
        invocation = self.parser.parse(text, source="command", message_id=0, chat_id=None, aliases=self.registry.alias_names())
        if invocation is None:
            return None
        spec = self.registry.resolve(invocation)
        if spec is None:
            swapped = self.parser.swap_invocation(invocation)
            if swapped is not None:
                spec = self.registry.resolve(swapped)
        return spec.name if spec is not None else None

    async def dispatch(self, message: Any, *, source: str) -> object | None:
        if self._is_blocked_peer(message):
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
            aliases=self.registry.alias_names(),
        )
        if invocation is None:
            return None
        spec = self.registry.resolve(invocation)
        if spec is None:
            swapped = self.parser.swap_invocation(invocation)
            if swapped is not None:
                spec = self.registry.resolve(swapped)
                if spec is not None:
                    invocation = swapped
        if spec is None:
            return None
        if not self.is_authorized(message, spec):
            return None
        if self.security is not None:
            verdict = self.security.check(message, transport="mt", module_id=spec.module_id, is_group=self._is_group(message), authorized=True)
            if verdict is not AccessVerdict.ALLOW:
                return None
        self._remember(key)
        if self.suspended and not spec.kernel:
            return None
        task_key = (chat_id, message_id)
        previous = self._running.get(task_key)
        if previous is not None and not previous.done():
            previous.cancel()
        context = self.context_factory.create(spec.module_id, message) if self.context_factory is not None else None
        try:
            work = self.tasks.spawn(
                spec.module_id,
                self._invoke(spec, invocation, message, context),
                name=f"hotaru:cmd:{spec.name}",
            )
        except TaskLimitError:
            return None
        task = asyncio.create_task(self._execute(spec, message, work, context), name=f"hotaru:response:{spec.name}")
        task.add_done_callback(lambda _: work.cancel() if not work.done() else None)
        self._running[task_key] = task
        task.add_done_callback(lambda t: self._running.pop(task_key, None) if self._running.get(task_key) is t else None)

    async def _execute(self, spec: Any, message: Any, task: asyncio.Task[Any], context: Any = None) -> object | None:
        try:
            result = await task
        except asyncio.CancelledError:
            return None
        except Exception as exc:
            log.error("command failed: %s", type(exc).__name__)
            if self.response_service is not None:
                try:
                    text = type(exc).__name__
                    return await context.respond(text) if context is not None else await self.response_service.respond(message, text=text, output="auto")
                except Exception as response_exc:
                    log.error("command error response failed: %s", type(response_exc).__name__)
                    return None
            return None
        if spec.kernel and self.response_service is not None:
            if isinstance(result, tuple):
                values = cast('tuple[object, ...]', result)
                if len(values) == 2:
                    text, buttons = values
                    if context is not None:
                        return await context.respond(text, buttons=buttons)
                    return await self.response_service.respond(message, text=text, buttons=buttons, output="auto")
            if isinstance(result, str):
                return await context.respond(result) if context is not None else await self.response_service.respond(message, text=result, output="auto")
        return cast(object, result)

    async def _invoke(self, spec: Any, invocation: CommandInvocation, message: Any, context: Any = None) -> object:
        if self.sandbox is not None and spec.sandbox:
            result = await self.sandbox_dispatch(spec, invocation, context)
        else:
            if self.context_factory is None:
                raise RuntimeError("module context factory is not configured")
            context = context or self.context_factory.create(spec.module_id, message)
            with module_scope(spec.module_id):
                result = spec.handler(context, invocation)
        if inspect.isawaitable(result):
            with module_scope(spec.module_id):
                return await result
        return result

    async def sandbox_dispatch(self, spec: Any, invocation: CommandInvocation, context: Any = None) -> object:
        payload: dict[str, Any] = {
            "source": invocation.source,
            "raw_args": invocation.raw_args,
            "message_id": invocation.message_id,
            "chat_id": invocation.chat_id,
            "attachment": self._sandbox_attachment(invocation.message),
            "reply_attachment": self._sandbox_attachment(self._reply_of(invocation.message)),
        }
        message = invocation.message
        reply_header = message.get("reply_to") if hasattr(message, "get") else None
        if isinstance(reply_header, dict):
            payload["reply_to"] = reply_header
        runtime = getattr(self.context_factory, "runtime", None)
        lexicon = getattr(runtime, "lexicon", None)
        if runtime is not None and lexicon is not None:
            language = runtime.language()
            payload["language"] = language
            payload["translations"] = lexicon.bundle(language)
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
            target=f"command_{spec.name}",
            context=context,
        )
        if isinstance(result, str):
            return result
        if result is None:
            return None
        return str(result)

    def running(self) -> int:
        return sum(1 for task in self._running.values() if not task.done())

    def cancel(self, chat_id: int | None, message_id: int) -> bool:
        task = self._running.get((chat_id, message_id))
        if task is None or task.done():
            return False
        task.cancel()
        return True

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

    def is_authorized(self, message: Any, spec: CommandSpec | None = None) -> bool:
        user_id = getattr(message, "from_id", None)
        if not isinstance(user_id, int):
            user_id = None
        if spec is None:
            name = self.command_name(getattr(message, "text", None) or "")
            spec = self.registry.resolve_name(name) if name is not None else None
        if self.access is not None:
            if spec is not None and spec.kernel and not self.access.is_owner(user_id):
                return spec.module_id == "accounts" and self.access.check_tsec(user_id, spec.module_id, spec.name, getattr(message, "chat_id", None))
            required = self._required_permission(message, spec)
            if self.access.allows(user_id, required):
                return True
            chat_id = getattr(message, "chat_id", None)
            if spec is not None and self.access.check_tsec(user_id, spec.module_id, spec.name, chat_id):
                return True
            return False
        if bool(getattr(message, "is_me", False)):
            return True
        return self.owner_id is not None and user_id == self.owner_id

    _is_authorized = is_authorized

    def _required_permission(self, message: Any, spec: CommandSpec | None = None) -> Permission:
        from .access import DEFAULT_COMMAND_PERMISSION
        if spec is None:
            name = self.command_name(getattr(message, "text", None) or "")
            spec = self.registry.resolve_name(name) if name is not None else None
        if spec is None:
            return DEFAULT_COMMAND_PERMISSION
        if spec.kernel:
            return DEFAULT_COMMAND_PERMISSION
        if self.access is None:
            return DEFAULT_COMMAND_PERMISSION
        return self.access.default_permission(spec.module_id, spec.name)

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
        source = cast(SupportsGet, message)
        for kind in ("document", "photo", "video", "audio", "voice", "animation", "video_note", "sticker"):
            media = source.get(kind)
            if media is None:
                continue
            if isinstance(media, list):
                values = cast('list[object]', media)
                media = values[-1] if values else None
            if not isinstance(media, dict):
                continue
            record = cast('dict[str, object]', media)
            size = record.get("size")
            return {
                "kind": kind,
                "file_name": record.get("file_name") or record.get("name"),
                "mime_type": record.get("mime_type"),
                "size": size if isinstance(size, int) else None,
            }
        media_wrap = source.get("media")
        if isinstance(media_wrap, dict):
            wrapper = cast('dict[str, object]', media_wrap)
            document = wrapper.get("document")
            if isinstance(document, dict):
                record = cast('dict[str, object]', document)
                size = record.get("size")
                return {"kind": "document", "file_name": record.get("file_name"), "mime_type": record.get("mime_type"), "size": size if isinstance(size, int) else None}
            photo = wrapper.get("photo")
            if isinstance(photo, dict):
                return {"kind": "photo", "file_name": None, "mime_type": "image/jpeg", "size": None}
        return None
