from __future__ import annotations

import logging
import os
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, cast
from collections.abc import Awaitable

from goygram.errors import FloodWaitError, MessageNotModifiedError
from relay.inline import InlineError

from .state import StateNamespace
from .plainfmt import rich_to_plain
from relay.firewall import trusted_scope
from relay.files import document, put, take
from goygram.rich import rich_html
from goygram.sugar import html_to_entities, split_html_text
from relay.proxies import (
    Gateway,
    RichGateway,
    BotGateway,
    InlineHelper,
    HtmlHelper,
    AssetsHelper,
    ModulesHelper,
    UiHelper,
    ForumHelper,
)
from relay.toolkit import TOOLS, buttons_html, needs_form, needs_callback
from .tl import Button, TL, as_tl

log = logging.getLogger(__name__)


OutputMode = Literal["edit", "reply", "auto"]


class ResponseError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Response:
    delivered: bool
    action: Literal["edit", "reply", "none"]
    transport: str | None
    message: Any = None


class FormHandle:
    def __init__(self, runtime: Any, source: Any, value: Any = None, key: str | None = None) -> None:
        self._runtime = runtime
        self._source = source
        self._record = None
        self._value = value
        self._key = key or secrets.token_urlsafe(12)

    async def edit(self, text: str | None = None, buttons: Any = None, **kwargs: Any) -> "FormHandle":
        if self._runtime is None:
            raise ResponseError("form runtime is unavailable")
        self._value = await self._runtime.edit_form(self, text, buttons, **kwargs)
        return self

    async def refresh(self) -> "FormHandle":
        if self._runtime is None:
            raise ResponseError("form runtime is unavailable")
        await self._runtime.edit_form(self, None, None)
        return self

    async def replace(self, text: str, buttons: Any = None, **kwargs: Any) -> "FormHandle":
        return await self.edit(text, buttons, **kwargs)

    async def expire(self) -> bool:
        return await self.unload()

    async def is_alive(self) -> bool:
        return self.snapshot() is not None

    async def set_ttl(self, ttl: float) -> "FormHandle":
        await self.edit(None, None, ttl=ttl)
        return self

    async def update(self, text: str | None = None, buttons: Any = None, **kwargs: Any) -> "FormHandle":
        return await self.edit(text, buttons, **kwargs)

    async def show(self) -> "FormHandle":
        return await self.refresh()

    async def hide(self) -> bool:
        return await self.delete()

    async def close_and_delete(self) -> bool:
        return await self.delete()

    async def set_buttons(self, buttons: Any) -> "FormHandle":
        return await self.edit_buttons(buttons)

    async def set_text(self, text: str) -> "FormHandle":
        return await self.edit(text)

    async def edit_buttons(self, buttons: Any) -> "FormHandle":
        if self._runtime is None:
            raise ResponseError("form runtime is unavailable")
        self._value = await self._runtime.edit_form(self, None, buttons)
        return self

    async def delete(self) -> bool:
        if self._runtime is None:
            return False
        return await self._runtime.delete_form(self)

    async def close(self) -> bool:
        return await self.delete()

    async def unload(self) -> bool:
        if self._runtime is None:
            return False
        return await self._runtime.delete_form(self)

    @property
    def value(self) -> Any:
        return self._value

    @property
    def message(self) -> Any:
        return self._value

    @property
    def transport(self) -> str | None:
        return getattr(self._source, "src", None)

    def snapshot(self) -> dict[str, Any] | None:
        if self._runtime is None:
            return None
        return self._runtime.form_snapshot(self)

    @property
    def key(self) -> str | None:
        return self._key

    def __bool__(self) -> bool:
        return bool(self._value)

    def __await__(self) -> Any:
        async def resolve() -> Any:
            return self
        return resolve().__await__()


class ResponseService:
    def __init__(self, rich_sender: Callable[..., Any] | None = None) -> None:
        self.rich_sender = rich_sender

    async def answer(
        self,
        message: Any,
        *,
        text: str | None = None,
        rich_message: Any = None,
        media: Any = None,
        buttons: Any = None,
        output: OutputMode = "edit",
        reply_to: int | None = None,
        topic_id: int | None = None,
        parse_mode: str | None = "HTML",
    ) -> Response:
        if text is None and rich_message is None and media is None:
            raise ValueError("response requires text, rich_message, or media")
        if text is not None and rich_message is not None:
            raise ValueError("text and rich_message are mutually exclusive")
        if output not in ("edit", "reply", "auto"):
            raise ValueError("output mode is invalid")
        if rich_message is not None:
            if self.rich_sender is None:
                raise ResponseError("rich transport is not configured")
            result = self.rich_sender(
                message,
                rich_message=rich_message,
                buttons=buttons,
                output=output,
                reply_to=reply_to,
                topic_id=topic_id,
            )
            if hasattr(result, "__await__"):
                result = await result
            return Response(True, "edit" if output == "edit" else "reply", getattr(message, "src", None), result)
        payload = {"kbd": buttons}
        if parse_mode is not None:
            payload["parse_mode"] = parse_mode
        if media is not None:
            payload["media"] = media
        if reply_to is not None:
            payload["reply_to"] = reply_to
        if topic_id is not None:
            payload["topic_id"] = topic_id
        if text is not None and str(payload.get("parse_mode", "")).lower() == "html":
            if getattr(message, "src", "mt") == "bot":
                payload["parse_mode"] = "HTML"
            else:
                payload.pop("parse_mode", None)
                plain, entities = html_to_entities(text)
                payload["entities"] = entities
                text = plain
        if output in ("edit", "auto") and hasattr(message, "edit"):
            try:
                result = await message.edit(text or "", **payload)
            except MessageNotModifiedError:
                return Response(True, "edit", getattr(message, "src", None), message)
            except FloodWaitError:
                raise
            except Exception:
                if output == "edit":
                    raise
            else:
                return Response(True, "edit", getattr(message, "src", None), result)
        if output == "edit":
            return Response(False, "none", None)
        if not hasattr(message, "reply"):
            return Response(False, "none", None)
        result = await message.reply(text or "", **payload)
        return Response(True, "reply", getattr(message, "src", None), result)

    async def smart(
        self,
        message: Any,
        content: Any = None,
        *,
        output: OutputMode = "auto",
        inline: bool = False,
        bot: bool = False,
        form: Any = None,
        buttons: Any = None,
        file: Any = None,
        media: Any = None,
        rich_message: Any = None,
        **kwargs: Any,
    ) -> Any:
        if content is not None:
            if isinstance(content, str):
                kwargs.setdefault("text", content)
            elif isinstance(content, dict) and content.get("_") == "inputRichMessageHTML":
                rich_message = content
            else:
                media = content
        if file is not None:
            media = file
        if kwargs.get("topic_id") is None:
            for name in ("topic_id", "message_thread_id", "top_msg_id"):
                candidate = getattr(message, name, None)
                if isinstance(candidate, int) and candidate > 0:
                    kwargs["topic_id"] = candidate
                    break
                getter = getattr(message, "get", None)
                if callable(getter):
                    candidate = getter(name)
                    if isinstance(candidate, int) and candidate > 0:
                        kwargs["topic_id"] = candidate
                        break
        if inline or form is not None or buttons is not None:
            data = form if isinstance(form, dict) else {"text": kwargs.pop("text", ""), "buttons": buttons}
            if hasattr(message, "form"):
                return await message.form(data.get("text", ""), data.get("buttons"), **kwargs)
            return await self.answer(message, text=data.get("text", ""), buttons=data.get("buttons"), output="reply")
        if bot:
            return await kwargs.pop("bot_gateway").send_message(getattr(message, "chat_id", None), kwargs.pop("text", ""), buttons=buttons, **kwargs)
        if rich_message is not None:
            return await self.answer(message, rich_message=rich_message, output=output, **kwargs)
        return await self.answer(message, text=kwargs.pop("text", None), media=media, output=output, buttons=buttons, **kwargs)

    async def split(self, message: Any, text: str, *, limit: int = 4096, **kwargs: Any) -> list[Any]:
        if limit < 1:
            raise ValueError("limit must be positive")
        parts = []
        rest = text
        while len(rest) > limit:
            cut = max(rest.rfind("\n", 0, limit + 1), rest.rfind(" ", 0, limit + 1))
            if cut < max(1, limit // 2):
                cut = limit
            parts.append(rest[:cut].rstrip())
            rest = rest[cut:].lstrip()
        parts.append(rest)
        result = []
        for index, part in enumerate(parts):
            options = dict(kwargs)
            options["output"] = "edit" if index == 0 and kwargs.get("output", "auto") == "auto" else kwargs.get("output", "reply")
            result.append(await self.answer(message, text=part, **options))
        return result

    async def split_html(self, message: Any, text: str, *, limit: int = 4096, max_parts: int = 20, **kwargs: Any) -> list[Any]:
        if limit < 256:
            raise ValueError("limit must be at least 256")
        parts = [part for part in split_html_text(text, limit) if part.strip()]
        if len(parts) > max_parts:
            return await self.fallback_file(message, text, **kwargs)
        result = []
        for index, part in enumerate(parts):
            options = dict(kwargs)
            options["output"] = "edit" if index == 0 and kwargs.get("output", "auto") == "auto" else "reply"
            result.append(await self.answer(message, text=part, **options))
        return result

    async def fallback_file(self, message: Any, text: str, *, filename: str = "response.html", **kwargs: Any) -> Any:
        fd, raw = tempfile.mkstemp(prefix="hotaru-response-", suffix=".html")
        os.close(fd)
        path = Path(raw)
        path.write_text(text, encoding="utf-8")
        try:
            if hasattr(message, "reply"):
                return await message.reply(path, filename=filename, **kwargs)
            return await self.answer(message, media=path, output="reply", **kwargs)
        finally:
            path.unlink(missing_ok=True)

    async def smart_split(self, message: Any, text: str, *, limit: int = 4096, file_limit: int = 200000, filename: str = "response.html", **kwargs: Any) -> Any:
        if len(text) <= limit:
            return [await self.answer(message, text=text, **kwargs)]
        if len(text) > file_limit:
            kwargs.pop("preserve_html", None)
            return await self.fallback_file(message, text, filename=filename, **kwargs)
        if kwargs.pop("preserve_html", True) and limit >= 256:
            return await self.split_html(message, text, limit=limit, **kwargs)
        return await self.split(message, text, limit=limit, **kwargs)

    async def dispatch(self, message: Any, content: Any = None, **kwargs: Any) -> Any:
        return await self.smart(message, content, **kwargs)

    async def respond(self, message: Any, content: Any = None, **kwargs: Any) -> Any:
        return await self.smart(message, content, **kwargs)


@dataclass(frozen=True, slots=True)
class Attachment:
    kind: str
    file_name: str | None
    mime_type: str | None
    size: int | None
    raw: Any


class ModuleMessage:
    def __init__(self, source: Any, responses: ResponseService) -> None:
        self._source = source
        self._responses = responses

    def __getattr__(self, name: str) -> Any:
        if name in {"app", "raw", "net", "_resolve_peer", "_source"}:
            raise AttributeError(name)
        if name in {"id", "msg_id", "chat_id", "from_id", "text", "is_me", "src"}:
            return getattr(self._source, name, None)
        raise AttributeError(name)

    def get(self, key: str, default: Any = None) -> Any:
        if key in {"raw", "app", "net", "_resolve_peer"}:
            return default
        return self._source.get(key, default) if hasattr(self._source, "get") else default

    async def edit(self, text: str, **kwargs: Any) -> Any:
        return await self._responses.answer(self._source, text=text, output="edit", **kwargs)

    async def reply(self, text: str, **kwargs: Any) -> Any:
        kwargs.setdefault("output", "reply")
        return await self._responses.answer(self._source, text=text, **kwargs)

    async def download(self, destination: str | Path | None = None) -> Any:
        if not hasattr(self._source, "download"):
            raise ResponseError("media download is unavailable")
        return await self._source.download(destination)


@dataclass(slots=True)
class ModuleContext:
    module_id: str
    _source: Any
    state: StateNamespace
    responses: ResponseService
    cap_host: Any = None
    callback_router: Any = None
    inline_manager: Any = None
    form_sender: Any = None
    runtime: Any = None
    _premium: bool | None = None

    def __repr__(self) -> str:
        return f"ModuleContext({self.module_id!r})"

    @property
    def message(self) -> ModuleMessage:
        return ModuleMessage(self._source, self.responses)

    @property
    def tg(self) -> Any:
            return Gateway(self.cap_host, self.module_id)

    @property
    def rich(self) -> Any:
            return RichGateway(self.tg, self._source)

    @property
    def bot(self) -> Any:
            return BotGateway(self.inline_manager)

    @property
    def forum(self) -> Any:
        helper = getattr(self.runtime, "_forum_helper", None)
        if helper is None:
            helper = ForumHelper(self.runtime)
            self.runtime._forum_helper = helper
        return helper

    @property
    def inline(self) -> Any:
            return InlineHelper(self.inline_manager)

    @property
    def html(self) -> Any:
            return HtmlHelper()

    @property
    def i18n(self) -> Any:
        if self.runtime is None or not hasattr(self.runtime, "translator"):
            raise ResponseError("translation runtime is unavailable")
        return self.runtime.translator()

    def t(self, key: str, default: str | None = None, **params: Any) -> str:
        try:
            value = self.i18n.t(key, default, **params)
        except Exception:
            value = default if isinstance(default, str) else key
            try:
                return value.format(**params)
            except (KeyError, IndexError, ValueError):
                return value
        if value == key and default is not None:
            try:
                return default.format(**params)
            except (KeyError, IndexError, ValueError):
                return default
        return value

    def _is_kernel_module(self) -> bool:
        runtime = self.runtime
        if runtime is None or getattr(runtime, "modules", None) is None:
            return False
        binding = runtime.modules._bindings.get(self.module_id)
        return bool(binding and binding[2])

    @property
    def accounts(self) -> Any:
        if not self._is_kernel_module():
            raise RuntimeError("account management is available to kernel modules only")
        runtime = self.runtime
        manager = getattr(runtime, "account_manager", None) if runtime is not None else None
        if manager is None:
            raise RuntimeError("account manager is unavailable")
        return manager

    @property
    def access_admin(self) -> Any:
        if not self._is_kernel_module():
            raise RuntimeError("access administration is available to kernel modules only")
        runtime = self.runtime
        access = getattr(runtime, "access", None) if runtime is not None else None
        if access is None:
            raise RuntimeError("access manager is unavailable")
        return access

    @property
    def tools(self) -> Any:
        return TOOLS

    @property
    def assets(self) -> Any:
            return AssetsHelper(self)

    @property
    def modules(self) -> Any:
        return ModulesHelper(self)

    @property
    def ui(self) -> Any:
        owner_id = getattr(self._source, "from_id", None)
        if not isinstance(owner_id, int) and self.runtime is not None:
            owner_id = getattr(getattr(self.runtime, "kernel", None), "owner_id", None)
        if not isinstance(owner_id, int):
            owner_id = None
        return UiHelper(self.module_id, self.callback_router.store, owner_id, getattr(self._source, "chat_id", None), getattr(self._source, "id", 0), self.callback_router)

    async def cap(self, capability: str, payload: dict[str, Any] | None = None) -> Any:
        if self.cap_host is None:
            raise ResponseError("capabilities are not available")
        return await self.cap_host.call(self.module_id, capability, payload or {})

    async def mt(self, method: str, **kwargs: Any) -> Any:
        return await self.cap("mt", {"method": method, "kwargs": kwargs})

    async def net(self, url: str, *, data: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        return await self.cap("net", {"url": url, "data": data, "timeout": timeout})

    async def premium(self) -> bool:
        if self._premium is not None:
            return self._premium
        app = getattr(self.runtime, "app", None) if self.runtime is not None else None
        if app is None or getattr(app, "mt", None) is None:
            return False
        try:
            with trusted_scope():
                result = await app.mt_users_get_users( id=[{"_": "inputUserSelf"}])
            body = result.get("result", result) if isinstance(result, dict) else result
            if isinstance(body, dict):
                users = body.get("users") or body.get("result") or []
            else:
                users = body
            first = users[0] if isinstance(users, list) and users else None
            if not isinstance(first, dict):
                first = body if isinstance(body, dict) else {}
            self._premium = bool(first.get("premium"))
        except Exception:
            log.error("premium check failed", exc_info=True)
            return False
        return self._premium

    @property
    def is_premium(self) -> bool:
        if self._premium is not None:
            return self._premium
        runtime = self.runtime
        cached = getattr(runtime, "_premium_cache", None) if runtime is not None else None
        return bool(cached)

    async def _via_bot(self, buttons: Any, kind: str | None) -> bool:
        if not await self.premium():
            return True
        return needs_callback(buttons)

    async def _bot_form(self, text: Any, buttons: Any, kwargs: dict[str, Any]) -> Any:
        if self.form_sender is None:
            raise ResponseError("bot form transport is not available")
        return await self.form_sender(self._source, text, buttons or [], kwargs)

    async def _deliver(self, text: str | None = None, **kwargs: Any) -> Any:
        if text is not None:
            kwargs["text"] = text
        media = kwargs.pop("media", None)
        if media is not None:
            return await self.send_file(media, kwargs.pop("text", None), **kwargs)
        use_rich = kwargs.pop("rich", False)
        fb = kwargs.pop("rich_fallback", "plain")
        if fb not in {"plain", "bot"}:
            raise ResponseError("rich_fallback must be plain or bot")
        if kwargs.get("buttons"):
            kwargs["buttons"] = self._normalize_buttons(kwargs["buttons"])
        kind = kwargs.pop("buttons_as", None)
        if kind is not None and kind not in {"inline", "page", "text"}:
            raise ResponseError("buttons_as must be inline, page, or text")
        if use_rich:
            allowed = self.is_premium
            if not allowed:
                if fb == "bot":
                    return await self._bot_form(kwargs.get("text", ""), kwargs.get("buttons"), kwargs)
                value = kwargs.pop("text", "")
                if value:
                    kwargs["text"] = rich_to_plain(value)
                use_rich = False
        if kwargs.get("text") is not None:
            kwargs.setdefault("parse_mode", "HTML")
        if kwargs.get("buttons") and await self._via_bot(kwargs["buttons"], kind or ("page" if use_rich else "inline")):
            return await self._bot_form(kwargs.get("text", ""), kwargs["buttons"] if (kind or "inline") == "inline" or needs_form(kwargs["buttons"]) else None, kwargs)
        if kwargs.get("buttons") and kind in {"page", "text"}:
            kwargs["text"] = str(kwargs.get("text") or "") + buttons_html(kwargs.pop("buttons"), kind=kind)
            use_rich = True
        if kwargs.get("text") is not None and use_rich and self.cap_host is not None:
            value = kwargs.pop("text")
            limit = int(kwargs.pop("split_limit", 4096))
            file_limit = int(kwargs.pop("file_limit", 200000))
            if len(value) > file_limit:
                return await self.responses.fallback_file(self._source, value, filename=kwargs.pop("filename", "response.html"), **kwargs)
            if len(value) > limit:
                if kwargs.pop("preserve_html", True):
                    return await self.responses.split_html(self._source, value, limit=limit, **kwargs)
                return await self.responses.split(self._source, value, limit=limit, **kwargs)
            return await self.send_rich(value, **kwargs)
        parse_mode = kwargs.pop("parse_mode", None)
        return await self.responses.answer(self._source, parse_mode=parse_mode, **kwargs)

    async def respond(self, content: Any = None, **kwargs: Any) -> Any:
        mode = kwargs.pop("mode", kwargs.pop("output", "auto"))
        delete_source = kwargs.pop("delete_source", False)
        if kwargs.pop("force_reply", False):
            mode = "reply"
        if mode == "auto":
            mode = "edit" if self._outgoing else "reply"
        if mode not in {"edit", "reply"}:
            raise ResponseError("response mode must be edit, reply, or auto")
        if delete_source == "auto":
            delete_source = self._outgoing and mode == "reply"
        file_value = kwargs.pop("file", None)
        if file_value is not None:
            kwargs.setdefault("media", file_value)
        topic_id = kwargs.pop("topic_id", None)
        if topic_id is None:
            topic_id = self.topic_id
        if topic_id is not None:
            kwargs["topic_id"] = topic_id
        if content is not None:
            if isinstance(content, dict) and content.get("_") == "inputRichMessageHTML":
                kwargs["rich_message"] = content
            elif isinstance(content, str):
                kwargs.setdefault("text", content)
            else:
                kwargs.setdefault("media", content)
        buttons = kwargs.pop("buttons", None)
        if kwargs.pop("inline", False):
            self.runtime.purge_forms() if self.runtime is not None else None
            try:
                result = await self.inline_form(kwargs.pop("text", ""), buttons, **kwargs)
            except (InlineError, RuntimeError, ResponseError):
                kwargs["buttons"] = buttons
                kwargs["buttons_as"] = "inline"
                kwargs["output"] = mode
                result = await self._deliver(**kwargs)
        elif kwargs.pop("bot", False):
            chat_id = kwargs.pop("chat_id", getattr(self._source, "chat_id", None))
            if kwargs.get("rich_message") is not None:
                result = await self.bot.rich_send(chat_id, kwargs.pop("rich_message"), buttons=buttons, **kwargs)
            elif kwargs.get("media") is not None:
                result = await self.bot.send_photo(chat_id, kwargs.pop("media"), caption=kwargs.pop("text", ""), buttons=buttons, **kwargs)
            else:
                result = await self.bot.send_message(chat_id, kwargs.pop("text", ""), buttons=buttons, **kwargs)
        elif kwargs.get("form") is not None:
            form = kwargs.pop("form")
            result = await self.form(form.get("text", ""), form.get("buttons"), output=mode, **kwargs)
        elif buttons is not None:
            buttons = self._normalize_buttons(buttons)
            kind = kwargs.pop("buttons_as", None)
            if kind is not None and kind not in {"inline", "page", "text"}:
                raise ResponseError("buttons_as must be inline, page, or text")
            place = kind or ("page" if kwargs.get("rich") else "inline")
            kwargs["output"] = mode
            if await self._via_bot(buttons, place):
                text = kwargs.pop("text", "")
                if place in {"page", "text"}:
                    text = str(text or "") + buttons_html(buttons, kind=place)
                    form_buttons = buttons if needs_form(buttons) else None
                else:
                    form_buttons = buttons
                result = await self.form(text, form_buttons, output=mode, **kwargs)
            else:
                kwargs["text"] = str(kwargs.get("text") or "") + buttons_html(buttons, kind=place)
                kwargs["rich"] = True
                result = await self._deliver(**kwargs)
        else:
            kwargs["output"] = mode
            if kwargs.get("rich_message") is not None:
                result = await self._deliver(rich_message=kwargs.pop("rich_message"), **kwargs)
            else:
                result = await self._deliver(**kwargs)
        if delete_source:
            await self._delete_source()
        return result

    async def smart_respond(self, content: Any = None, **kwargs: Any) -> Any:
        return await self.respond(content, **kwargs)

    @property
    def _outgoing(self) -> bool:
        value = getattr(self._source, "is_me", None)
        if isinstance(value, bool):
            return value
        value = getattr(self._source, "out", None)
        return bool(value)

    @property
    def topic_id(self) -> int | None:
        for name in ("topic_id", "message_thread_id", "top_msg_id"):
            value = getattr(self._source, name, None)
            if isinstance(value, int) and value > 0:
                return value
        getter = getattr(self._source, "get", None)
        if callable(getter):
            for name in ("topic_id", "message_thread_id", "top_msg_id"):
                value = getter(name)
                if isinstance(value, int) and value > 0:
                    return value
        return None

    async def _delete_source(self) -> None:
        deleter = getattr(self._source, "delete", None)
        if callable(deleter):
            result = deleter()
            if hasattr(result, "__await__"):
                await cast(Awaitable[Any], result)

    async def send_file(self, file: Any, caption: str | None = None, **kwargs: Any) -> Response:
        app = getattr(self.runtime, "app", None) if self.runtime is not None else None
        if app is None or getattr(app, "mt", None) is None:
            raise ResponseError("upload is unavailable")
        file_name = kwargs.pop("file_name", None)
        mime = kwargs.pop("mime_type", None) or kwargs.pop("mime", None) or "application/octet-stream"
        if isinstance(file, dict) and str(as_tl(file).get("_", "")).startswith("inputMedia"):
            media = file
        else:
            up = await put(app, file, file_name=file_name)
            media = document(up, mime=mime, file_name=file_name)
        peer = kwargs.pop("peer", None) or kwargs.pop("chat_id", None) or getattr(self._source, "chat_id", None)
        message = caption or ""
        data: dict[str, Any] = {"peer": peer, "media": media, "message": message, "random_id": secrets.randbits(63)}
        if message:
            plain, ents = html_to_entities(str(message))
            data["message"] = plain
            if ents:
                data["entities"] = ents
        reply_to = kwargs.pop("reply_to", None)
        topic_id = kwargs.pop("topic_id", self.topic_id)
        if reply_to is None:
            mid = getattr(self._source, "id", None)
            if isinstance(mid, int) and mid > 0:
                header: dict[str, Any] = {"_": "inputReplyToMessage", "reply_to_msg_id": mid}
                if isinstance(topic_id, int) and topic_id > 0:
                    header["top_msg_id"] = topic_id
                data["reply_to"] = header
        elif isinstance(reply_to, int):
            header = {"_": "inputReplyToMessage", "reply_to_msg_id": int(reply_to)}
            if isinstance(topic_id, int) and topic_id > 0:
                header["top_msg_id"] = topic_id
            data["reply_to"] = header
        elif reply_to:
            data["reply_to"] = reply_to
        with trusted_scope():
            result = await app.mt_messages_send_media(**data)
        return Response(True, "reply", getattr(self._source, "src", None), result)

    async def upload_file(self, source: Any, **kwargs: Any) -> Any:
        app = getattr(self.runtime, "app", None)
        if app is None:
            raise ResponseError("upload is unavailable")
        return await put(app, source, file_name=kwargs.get("file_name"), **{k: v for k, v in kwargs.items() if k != "file_name"})

    async def download_file(self, source: Any, destination: str | Path, **kwargs: Any) -> Any:
        app = getattr(self.runtime, "app", None)
        if app is None:
            raise ResponseError("download is unavailable")
        return await take(app, source, destination, **kwargs)

    def file_media(self, up: dict[str, Any], *, mime: str = "application/octet-stream", file_name: str | None = None, force_file: bool = True) -> dict[str, Any]:
        return document(up, mime=mime, file_name=file_name, force_file=force_file)

    async def send_media(self, media: Any, caption: str | None = None, **kwargs: Any) -> Response:
        return await self.send_file(media, caption, **kwargs)

    async def form(self, text: str, buttons: Any = None, *rows: Any, **kwargs: Any) -> FormHandle:
        if isinstance(buttons, dict) and "buttons" in buttons:
            text = str(buttons.get("text", text))
            buttons = buttons["buttons"]
        if buttons is None:
            buttons = list(rows)
        elif rows:
            buttons = [buttons, *rows]
        if buttons and isinstance(buttons[0], dict):
            buttons = [buttons]
        buttons = self._normalize_buttons(buttons)
        kwargs["buttons"] = buttons
        kwargs["text"] = text
        kwargs.setdefault("module_id", self.module_id)
        result = await self._deliver(**kwargs)
        handle = FormHandle(self.runtime, self._source, result)
        if self.runtime is not None:
            self.runtime.register_form(handle, self._source, text, buttons, kwargs)
        return handle

    def _normalize_buttons(self, buttons: Any) -> Any:
        if not buttons or self.callback_router is None:
            return buttons
        ui = self.ui
        result = []
        for row in buttons:
            current = []
            for button in row:
                if isinstance(button, dict) and callable(button.get("handler")) and isinstance(button.get("input"), str):
                    current.append(button)
                    continue
                if isinstance(button, dict) and callable(button.get("handler")) and "callback" not in button:
                    button = {**button, "callback": button["handler"]}
                if not isinstance(button, dict) or "callback" not in button:
                    current.append(button)
                    continue
                current.append(ui.button(str(button.get("text", "")), button["callback"], button.get("payload"), style=button.get("style")))
            result.append(current)
        return result

    @staticmethod
    def escape(value: Any) -> str:
        from html import escape
        return escape(str(value), quote=False)

    async def inline_form(self, text: str, buttons: Any = None, *rows: Any, **kwargs: Any) -> Any:
        if self.form_sender is None:
            raise ResponseError("inline form transport is not available")
        value = list(rows) if buttons is None else ([buttons, *rows] if rows else buttons)
        if value and isinstance(value[0], dict):
            value = [value]
        if self.callback_router is not None:
            value = self._normalize_buttons(value)
        kwargs.setdefault("module_id", self.module_id)
        result = await self.form_sender(self._source, text, value, kwargs)
        key = getattr(self._source, "_hotaru_inline_form_id", None)
        handle = FormHandle(self.runtime, self._source, result, key=key)
        if self.runtime is not None:
            self.runtime.register_form(handle, self._source, text, value, kwargs)
        return handle

    async def reply_html(self, text: str, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "reply")
        return await self._deliver(text=text, **kwargs)

    async def edit_html(self, text: str, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "edit")
        return await self._deliver(text=text, **kwargs)

    async def respond_file(self, media: Any, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "reply")
        return await self._deliver(media=media, **kwargs)

    async def respond_media(self, media: Any, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "reply")
        return await self._deliver(media=media, **kwargs)

    async def respond_rich(self, rich_message: Any, **kwargs: Any) -> Response:
        if isinstance(rich_message, str):
            return await self.send_rich(rich_message, **kwargs)
        if not isinstance(rich_message, dict):
            raise ResponseError("rich_message must be HTML text or a native input object")
        buttons = kwargs.pop("buttons", None)
        if buttons is not None and self.form_sender is not None:
            return await self.form_sender(self._source, rich_message, buttons, kwargs)
        return await self._context_tg_call("messages.sendMessage", {"peer": self._source.chat_id, "message": "", "rich_message": rich_message, **kwargs})

    async def send_rich(self, html: str, **kwargs: Any) -> Response:
        buttons = kwargs.pop("buttons", None)
        kind = kwargs.pop("buttons_as", "page")
        fb = kwargs.pop("rich_fallback", "plain")
        if kind not in {"inline", "page", "text"}:
            raise ResponseError("buttons_as must be inline, page, or text")
        if fb not in {"plain", "bot"}:
            raise ResponseError("rich_fallback must be plain or bot")
        if buttons is not None:
            buttons = self._normalize_buttons(buttons)
            if await self._via_bot(buttons, kind):
                if kind in {"page", "text"}:
                    html = str(html) + buttons_html(buttons, kind=kind)
                    buttons = buttons if needs_form(buttons) else None
                result = await self._bot_form(html, buttons, kwargs)
                return result if isinstance(result, Response) else Response(True, "reply", getattr(self._source, "src", None), result)
            if kind == "inline":
                from goygram.types.kbd import kbd_to_tl
                kwargs["reply_markup"] = kbd_to_tl({"inline_keyboard": buttons})
            else:
                html = str(html) + buttons_html(buttons, kind=kind)
        if not self.is_premium:
            if fb == "bot":
                result = await self._bot_form(html, None, kwargs)
                return result if isinstance(result, Response) else Response(True, "reply", getattr(self._source, "src", None), result)
            kwargs.pop("parse_mode", None)
            return await self._deliver(text=rich_to_plain(html), **kwargs)
        peer = getattr(self._source, "chat_id", None)
        if peer is None:
            raise ResponseError("rich message target is missing")
        output = kwargs.pop("output", "reply")
        message_id = getattr(self._source, "id", None)
        if self.runtime is not None and getattr(self.runtime, "app", None) is not None:
            return await self._trusted_send_rich(html, peer, output=output, message_id=message_id, **kwargs)
        data = {"peer": peer, "message": "", "random_id": secrets.randbits(63), "rich_message": {"_": "inputRichMessageHTML", **rich_html(html)}}
        kwargs.pop("parse_mode", None)
        if output == "edit" and message_id is not None:
            data["id"] = int(message_id)
            data.pop("peer", None)
            result = await self._context_tg_call("messages.editMessage", {"peer": peer, **data})
            return Response(True, "edit", getattr(self._source, "src", None), result)
        reply_to = kwargs.pop("reply_to", None) or message_id
        topic_id = kwargs.pop("topic_id", None) or self.topic_id
        if reply_to is not None:
            data["reply_to"] = {"_": "inputReplyToMessage", "reply_to_msg_id": int(reply_to), **( {"top_msg_id": int(topic_id)} if topic_id is not None else {})}
        data.update(kwargs)
        result = await self._context_tg_call("messages.sendMessage", data)
        return Response(True, "reply", getattr(self._source, "src", None), result)

    async def _trusted_send_rich(self, html: str, peer: Any, *, output: str = "reply", message_id: int | None = None, **kwargs: Any) -> Response:
        import secrets as _secrets
        app = self.runtime.app
        rich_message = {"_": "inputRichMessageHTML", **rich_html(html)}
        if output == "edit" and message_id is not None:
            with trusted_scope():
                result = await app.mt_messages_edit_message( peer=peer, id=int(message_id), message="", rich_message=rich_message)
            return Response(True, "edit", getattr(self._source, "src", None), result)
        reply_to = kwargs.pop("reply_to", None) or message_id
        topic_id = kwargs.pop("topic_id", None) or self.topic_id
        data = {"peer": peer, "message": "", "random_id": _secrets.randbits(63), "rich_message": rich_message}
        if reply_to is not None:
            reply_to_data = {"_": "inputReplyToMessage", "reply_to_msg_id": int(reply_to)}
            if topic_id is not None:
                reply_to_data["top_msg_id"] = int(topic_id)
            data["reply_to"] = reply_to_data
        kwargs.pop("parse_mode", None)
        data.update(kwargs)
        with trusted_scope():
            result = await app.mt_messages_send_message( **data)
        return Response(True, "reply", getattr(self._source, "src", None), result)

    async def _context_tg_call(self, method: str, data: dict[str, Any]) -> Any:
        host = getattr(self, "cap_host", None)
        if host is None:
            raise ResponseError("capabilities are not available")
        return await host.call(self.module_id, "mt", {"method": method, "kwargs": data})

    async def send(self, text: str, **kwargs: Any) -> Response:
        kwargs.setdefault("output", "reply")
        return await self._deliver(text=text, **kwargs)

    async def edit(self, text: str, **kwargs: Any) -> Response:
        return await self._deliver(text=text, output="edit", **kwargs)

    @property
    def reply_message(self) -> Any | None:
        candidate = self.message.get("reply_to_message") or self.message.get("reply")
        if candidate is not None:
            return candidate
        return None

    async def reply(self, message: Any = None) -> Any | None:
        target = message if message is not None else self._source
        if target is None:
            return None
        reply_to = target.get("reply_to") if hasattr(target, "get") else None
        if reply_to is not None:
            fetched = await self.cap("fetch", {"op": "reply", "peer": getattr(target, "chat_id", None), "reply_to": reply_to})
            if fetched is not None:
                return fetched
        direct = self.reply_message
        return direct

    async def msg(self, chat_id: Any, message_id: int) -> Any | None:
        return await self.cap("fetch", {"op": "message", "peer": chat_id, "id": int(message_id)})

    async def resolve(self, value: Any) -> Any | None:
        return await self.cap("fetch", {"op": "entity", "value": value})

    async def reply_text(self, message: Any = None) -> str | None:
        reply = await self.reply(message)
        if reply is None:
            return None
        if hasattr(reply, "get"):
            text = reply.get("message") or reply.get("text") or reply.get("caption")
            return str(text) if text is not None else None
        text = getattr(reply, "text", None) or getattr(reply, "message", None) or getattr(reply, "caption", None)
        return str(text) if text else None

    @property
    def has_media(self) -> bool:
        return self.attachment is not None

    @property
    def attachment(self) -> Attachment | None:
        return self._attachment_of(self.message)

    @property
    def reply_attachment(self) -> Attachment | None:
        reply = self.reply_message
        if reply is None:
            return None
        return self._attachment_of(reply)

    @staticmethod
    def _attachment_of(message: Any) -> Attachment | None:
        if not hasattr(message, "get"):
            return None
        for kind in ("document", "photo", "video", "audio", "voice", "animation", "video_note", "sticker"):
            media = message.get(kind)
            if media is None:
                continue
            if isinstance(media, list):
                media = media[-1] if media else None
            if not isinstance(media, dict):
                continue
            return Attachment(
                kind=kind,
                file_name=media.get("file_name") or media.get("name"),
                mime_type=media.get("mime_type"),
                size=media.get("size") if isinstance(media.get("size"), int) else None,
                raw=media,
            )
        media_wrap = message.get("media")
        if isinstance(media_wrap, dict):
            document = media_wrap.get("document")
            if isinstance(document, dict):
                return Attachment(
                    kind="document",
                    file_name=document.get("file_name"),
                    mime_type=document.get("mime_type"),
                    size=document.get("size") if isinstance(document.get("size"), int) else None,
                    raw=document,
                )
            photo = media_wrap.get("photo")
            if isinstance(photo, dict):
                return Attachment(kind="photo", file_name=None, mime_type="image/jpeg", size=None, raw=photo)
        return None

    async def download(self, attachment: Attachment | None = None, destination: str | Path | None = None) -> Path:
        target = attachment or self.attachment or self.reply_attachment
        if target is None and self.message.get("reply_to") is not None:
            fetched = await self.reply()
            if fetched is not None:
                target = self._attachment_of(fetched)
        if target is None:
            raise ResponseError("no attachment to download")
        if destination is None:
            fd, raw = tempfile.mkstemp(prefix="hotaru-dl-")
            os.close(fd)
            destination = raw
        path = Path(destination)
        source = self.message
        if self.attachment is None and target is not self.attachment:
            if not hasattr(source, "get") or source.get("reply_to") is not None:
                fetched = await self.reply()
                if fetched is not None:
                    source = fetched
        if getattr(source, "src", None) == "bot" and hasattr(source, "download"):
            await source.download(str(path))
            return path
        document = target.raw if isinstance(target.raw, dict) else None
        if document is None:
            raise ResponseError("attachment is not downloadable")
        app = getattr(self.runtime, "app", None) or getattr(self._source, "app", None)
        if app is None:
            raise ResponseError("download is unavailable")
        await take(app, document, str(path))
        return path


class ModuleContextFactory:
    def __init__(self, state: Any, responses: ResponseService, runtime: Any = None) -> None:
        self._state = state
        self._responses = responses
        self._runtime = runtime
        self.runtime = runtime
        self.cap_host: Any = None
        self.callback_router: Any = None
        self.inline_manager: Any = None
        self.form_sender: Any = None
        self.runtime: Any = runtime

    def create(self, module_id: str, message: Any) -> ModuleContext:
        cached = getattr(self.runtime, "_premium_cache", None) if self.runtime is not None else None
        return ModuleContext(module_id, message, self._state.namespace(module_id), self._responses, self.cap_host, self.callback_router, self.inline_manager, self.form_sender, self.runtime, cached)
