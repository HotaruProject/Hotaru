from __future__ import annotations

import secrets
import mimetypes
from typing import Any, Awaitable, Callable, cast

_SENTINEL = object()


def _msg_field(value: Any, key: str, default: Any = None) -> Any:
    return value.get(key, default) if hasattr(value, "get") else getattr(value, key, default)


class MessageOperations:
    def __init__(self, ctx: Any, raw: Any) -> None:
        self.ctx = ctx
        self.raw = raw

    @property
    def chat_id(self) -> Any:
        value = self._g("chat_id", "peer_id", "peer")
        return self._peer_id(value) if isinstance(value, dict) and str(_msg_field(value, "_", "")) in {"", "peerUser", "peerChat", "peerChannel"} else cast(Any, value)

    @property
    def id(self) -> int | None:
        return self._g("id", "message_id", "msg_id")


    @property
    def user_id(self) -> int | None:
        return self._peer_id(self._g("from_id", "sender_id", "user_id"))


    @property
    def out(self) -> bool:
        return self.is_me


    @property
    def is_me(self) -> bool:
        return bool(self._g("out") or self._g("is_me"))


    @property
    def media(self) -> Any:
        return self._g("media")


    @property
    def has_media(self) -> bool:
        return self._g("media") is not None or any(self._g(k) is not None for k in ("photo", "document", "video", "audio", "voice", "sticker", "animation"))


    @property
    def topic_id(self) -> int | None:
        if _msg_field(self.raw, "_ctx_topic_override", _SENTINEL) is not _SENTINEL:
            return _msg_field(self.raw, "_ctx_topic_override")
        explicit = self._g("topic_id", "message_thread_id", "top_msg_id")
        if isinstance(explicit, int) and explicit > 0:
            return explicit
        header = self._g("reply_to")
        top = _msg_field(header, "reply_to_top_id")
        if isinstance(top, int) and top > 0:
            return top
        if _msg_field(header, "forum_topic"):
            return _msg_field(header, "reply_to_msg_id")
        action = self._g("action")
        return self.id if _msg_field(action, "_") == "messageActionTopicCreate" else None


    def _g(self, *attrs: str, default: Any = None) -> Any:
        raw = self.raw
        if raw is None:
            return default
        for a in attrs:
            v = getattr(raw, a, _SENTINEL)
            if v is not _SENTINEL and v is not None:
                return v
            if hasattr(raw, "get"):
                v = raw.get(a)
                if v is not None:
                    return v
        return default


    def _target(self) -> tuple[int, int]:
        chat, mid = self.chat_id, self.id
        if not isinstance(chat, int) or not isinstance(mid, int) or mid <= 0:
            raise ValueError("message has no concrete peer/message identity")
        return chat, mid


    @staticmethod
    def _peer_id(value: Any) -> int | None:
        if isinstance(value, int):
            return value
        for key, prefix in (("channel_id", -1000000000000), ("chat_id", 0), ("user_id", None), ("id", None)):
            ident = _msg_field(value, key)
            if isinstance(ident, int):
                return ident if prefix is None else prefix - ident
        return None


    async def _on_message(self, method: str, *, id_key: str = "msg_id", **kwargs: Any) -> Any:
        if "peer" in kwargs or id_key in kwargs:
            raise ValueError("a message operation cannot override its target")
        if not isinstance(self.id, int) or self.id <= 0 or self.chat_id is None:
            raise ValueError("message has no concrete peer/message identity")
        return await self._rpc(method, peer=await self.get_input_chat(), **{id_key: self.id}, **kwargs)


    def _send_options(self, kwargs: dict[str, Any], *, reply: bool = False) -> dict[str, Any]:
        data = dict(kwargs)
        for key in ("chat_id", "peer", "message_id", "msg_id", "id", "via"):
            if key in data:
                raise ValueError(f"{key} cannot override this message's target")
        topic = data.pop("topic_id", self.topic_id)
        target = data.pop("reply_to", self.id if reply else topic)
        if reply and target != self.id:
            raise ValueError("reply must target this message")
        if target is not None:
            if isinstance(target, int):
                target = {"_": "inputReplyToMessage", "reply_to_msg_id": target}
            elif isinstance(target, dict):
                target = dict(cast('dict[str, Any]', target))
            else:
                raise TypeError("reply_to must be a message ID or InputReplyToMessage")
            if topic is not None:
                target["top_msg_id"] = topic
            data["reply_to"] = target
        if "buttons" in data:
            data["kbd"] = data.pop("buttons")
        if isinstance(data.get("kbd"), list):
            data["kbd"] = {"inline_keyboard": data["kbd"]}
        if "link_preview" in data:
            data["no_webpage"] = not data.pop("link_preview")
        return data


    async def pin(self, both_sides: bool | None = None, *, notify: bool = False, pm_oneside: bool = False) -> Any:
        return await self._on_message("messages.updatePinnedMessage", id_key="id", silent=not notify, pm_oneside=pm_oneside if both_sides is None else not both_sides)


    async def unpin(self) -> Any:
        return await self._on_message("messages.updatePinnedMessage", id_key="id", unpin=True)


    async def forward_to(self, to_chat: Any, **kwargs: Any) -> Any:
        return await self.forward(to_chat, **kwargs)


    async def copy_to(self, to_chat: Any, **kwargs: Any) -> Any:
        kwargs["drop_author"] = True
        return await self.forward(to_chat, **kwargs)


    async def react(self, emoji: Any, **kwargs: Any) -> Any:
        values = cast("list[Any]", emoji) if isinstance(emoji, list) else [emoji] if emoji else []
        reactions = [{"_": "reactionEmoji", "emoticon": v} if isinstance(v, str) else {"_": "reactionCustomEmoji", "document_id": v} if isinstance(v, int) else v for v in values]
        return await self._on_message("messages.sendReaction", reaction=reactions, **kwargs)


    async def unreact(self) -> Any:
        return await self.react([])


    async def get_sender(self) -> Any:
        uid = self.user_id
        if uid is None:
            return None
        return await self.ctx.resolve(uid)


    @staticmethod
    def _input_entity(peer: Any, kind: str) -> dict[str, Any]:
        data = dict(cast("dict[str, Any]", peer))
        ctor = str(data.get("_", ""))
        allowed = {"inputPeerChannel": "inputChannel", "inputPeerChannelFromMessage": "inputChannelFromMessage"} if kind == "channel" else {"inputPeerUser": "inputUser", "inputPeerUserFromMessage": "inputUserFromMessage", "inputPeerSelf": "inputUserSelf"}
        if ctor not in allowed:
            raise ValueError(f"message peer is not a {kind}")
        data["_"] = allowed[ctor]
        return data


    async def get_input_channel(self) -> Any:
        return self._input_entity(await self.get_input_chat(), "channel")


    async def download_media(self, destination: Any = None, **kwargs: Any) -> Any:
        return await self.download(destination, **kwargs)


    async def read(self) -> Any:
        if self.topic_id is not None:
            return await self._rpc("messages.readDiscussion", peer=await self.get_input_chat(), msg_id=self.topic_id, read_max_id=self.id)
        if self.chat_id is not None and self.chat_id < -1000000000000:
            return await self._rpc("channels.readHistory", channel=await self.get_input_channel(), max_id=self.id)
        return await self._rpc("messages.readHistory", peer=await self.get_input_chat(), max_id=self.id)


    async def mark_read(self) -> Any:
        return await self.read()


    async def read_contents(self) -> Any:
        if self.chat_id is not None and self.chat_id < -1000000000000:
            return await self._rpc("channels.readMessageContents", channel=await self.get_input_channel(), id=[self.id])
        return await self._rpc("messages.readMessageContents", id=[self.id])


    async def get_buttons(self) -> list[list[Any]]:
        return self.buttons


    @property
    def buttons(self) -> list[list[Any]]:
        markup = self._g("reply_markup")
        rows: Any = _msg_field(markup, "rows") or _msg_field(markup, "inline_keyboard") or _msg_field(markup, "keyboard") or []
        return [[MessageButton(self, button) for button in (cast("list[Any]", row) if isinstance(row, list) else _msg_field(row, "buttons", []))] for row in rows]


    @property
    def button_count(self) -> int:
        return sum(len(row) for row in self.buttons)


    async def click(self, i: Any = None, j: int | None = None, *, text: Any = None, filter: Any = None, data: Any = None, share_phone: Any = None, share_geo: Any = None, password: Any = None, open_url: bool = False) -> Any:
        if data is not None:
            return await self.click_button({"_": "keyboardButtonCallback", "data": data}, password=password)
        if sum(value is not None for value in (i, text, filter)) > 1:
            raise ValueError("select only one of i, text or filter")
        if j is not None and (text is not None or filter is not None or isinstance(i, (list, tuple))):
            raise ValueError("j requires a row index")
        poll = _msg_field(self.media, "poll") or self._g("poll")
        answers: Any = _msg_field(poll, "answers", [])
        if poll is not None:
            if j is not None:
                raise ValueError("poll options have no columns")
            if i is not None:
                indices = cast("list[Any]", i) if isinstance(i, (list, tuple)) else [i]
                selected = [answers[index] for index in indices]
            else:
                selected = cast("list[Any]", [])
                for answer in answers:
                    label = _msg_field(answer, "text")
                    label = _msg_field(label, "text", label)
                    if (filter(MessageButton(self, answer)) if filter is not None else text(label) if callable(text) else text is not None and label == text):
                        selected.append(answer)
                        break
            return await self.vote([_msg_field(a, "option") for a in selected])
        rows = self.buttons
        flat = [button for row in rows for button in row]
        if not flat:
            return None
        if text is not None:
            button = next((b for b in flat if (text(b.text) if callable(text) else b.text == text)), None)
        elif filter is not None:
            button = next((b for b in flat if filter(b)), None)
        else:
            button = rows[i or 0][j] if j is not None else flat[0 if i is None else i]
        return await button.click(share_phone=share_phone, share_geo=share_geo, password=password, open_url=open_url) if button else None


    async def vote(self, options: Any) -> Any:
        answers = _msg_field(_msg_field(self.media, "poll") or self._g("poll"), "answers", [])
        values = cast("list[Any]", options) if isinstance(options, (list, tuple)) else [options]
        selected = [_msg_field(answers[v], "option") if isinstance(v, int) else v for v in values]
        return await self._on_message("messages.sendVote", options=selected)


    async def retract_vote(self) -> Any:
        return await self.vote([])


    async def get_poll_results(self) -> Any:
        return await self._on_message("messages.getPollResults", poll_hash=0)


    async def get_poll_votes(self, *, option: bytes | None = None, offset: str | None = None, limit: int = 100) -> Any:
        return await self._on_message("messages.getPollVotes", id_key="id", option=option, offset=offset, limit=limit)


    async def get_reactions(self) -> Any:
        return await self._rpc("messages.getMessagesReactions", peer=await self.get_input_chat(), id=[self.id])


    async def get_reactors(self, *, reaction: Any = None, offset: str | None = None, limit: int = 100) -> Any:
        if isinstance(reaction, str):
            reaction = {"_": "reactionEmoji", "emoticon": reaction}
        return await self._on_message("messages.getMessageReactionsList", id_key="id", reaction=reaction, offset=offset, limit=limit)


    async def get_views(self, increment: bool = False) -> Any:
        return await self._rpc("messages.getMessagesViews", peer=await self.get_input_chat(), id=[self.id], increment=increment)


    async def get_replies(self, *, offset_id: int = 0, limit: int = 100, **kwargs: Any) -> Any:
        data: dict[str, Any] = {"offset_id": offset_id, "offset_date": 0, "add_offset": 0, "limit": limit, "max_id": 0, "min_id": 0, "hash": 0, **kwargs}
        return await self._on_message("messages.getReplies", **data)


    async def get_discussion(self) -> Any:
        return await self._on_message("messages.getDiscussionMessage")


    async def get_link(self, *, grouped: bool = False, thread: bool = False) -> str:
        if self.chat_id is not None and self.chat_id < -1000000000000:
            result = await self._rpc("channels.exportMessageLink", channel=await self.get_input_channel(), id=self.id, grouped=grouped, thread=thread)
            return str(_msg_field(_msg_field(result, "result", result), "link", ""))
        chat, mid = self._target()
        return f"tg://openmessage?{'user_id' if chat > 0 else 'chat_id'}={abs(chat)}&message_id={mid}"


    async def remove_buttons(self) -> Any:
        return await self.edit_buttons()


    async def edit_caption(self, caption: str, **kwargs: Any) -> Any:
        return await self.edit(caption, **kwargs)


    async def reply_media(self, media: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.send_media(media, caption, **self._send_options(kwargs, reply=True))


    async def send_file(self, file: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.send_media(file, caption, **kwargs)


    async def reply_file(self, file: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.reply_media(file, caption, **kwargs)


    async def send_photo(self, photo: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.send_media(photo, caption, kind="photo", **kwargs)


    async def reply_photo(self, photo: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.reply_media(photo, caption, kind="photo", **kwargs)


    async def send_video(self, video: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.send_media(video, caption, kind="video", **kwargs)


    async def reply_video(self, video: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.reply_media(video, caption, kind="video", **kwargs)


    async def send_audio(self, audio: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.send_media(audio, caption, kind="audio", **kwargs)


    async def reply_audio(self, audio: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.reply_media(audio, caption, kind="audio", **kwargs)


    async def send_voice(self, voice: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.send_media(voice, caption, kind="voice", **kwargs)


    async def reply_voice(self, voice: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.reply_media(voice, caption, kind="voice", **kwargs)


    async def send_animation(self, animation: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.send_media(animation, caption, kind="animation", **kwargs)


    async def reply_animation(self, animation: Any, caption: str | None = None, **kwargs: Any) -> Any:
        return await self.reply_media(animation, caption, kind="animation", **kwargs)


    async def send_sticker(self, sticker: Any, **kwargs: Any) -> Any:
        return await self.send_media(sticker, kind="sticker", **kwargs)


    async def reply_sticker(self, sticker: Any, **kwargs: Any) -> Any:
        return await self.reply_media(sticker, kind="sticker", **kwargs)


    async def send_contact(self, phone: str, first_name: str, *, last_name: str = "", **kwargs: Any) -> Any:
        return await self.send_media({"_": "inputMediaContact", "phone_number": phone, "first_name": first_name, "last_name": last_name, "vcard": ""}, **kwargs)


    async def reply_contact(self, phone: str, first_name: str, **kwargs: Any) -> Any:
        return await self.send_contact(phone, first_name, **self._send_options(kwargs, reply=True))


    async def send_location(self, latitude: float, longitude: float, **kwargs: Any) -> Any:
        return await self.send_media({"_": "inputMediaGeoPoint", "geo_point": {"_": "inputGeoPoint", "lat": latitude, "long": longitude}}, **kwargs)


    async def reply_location(self, latitude: float, longitude: float, **kwargs: Any) -> Any:
        return await self.send_location(latitude, longitude, **self._send_options(kwargs, reply=True))


    async def send_dice(self, emoji: str = "🎲", **kwargs: Any) -> Any:
        return await self.send_media({"_": "inputMediaDice", "emoticon": emoji}, **kwargs)


    async def reply_dice(self, emoji: str = "🎲", **kwargs: Any) -> Any:
        return await self.send_dice(emoji, **self._send_options(kwargs, reply=True))


    async def reply_rich(self, rich: Any, **kwargs: Any) -> Any:
        return await self.send_rich(rich, **self._send_options(kwargs, reply=True))


    async def save(self, **kwargs: Any) -> Any:
        return await self.forward("me", **kwargs)


    async def typing(self, action: str = "sendMessageTypingAction") -> Any:
        return await self._rpc("messages.setTyping", peer=await self.get_input_chat(), top_msg_id=self.topic_id, action={"_": action})


    async def _rpc(self, method: str, **kwargs: Any) -> Any:
        if self._g("src", "source", default="mt") not in {"mt", "mtproto"}:
            raise ValueError("message operations require an MTProto message")
        result = await self.ctx.mt(method, **kwargs)
        if _msg_field(result, "ok") is False:
            raise RuntimeError(f"{method}: {_msg_field(result, 'error', 'RPC failed')}")
        return result

    async def get_chat(self) -> Any:
        return await self.ctx.resolve(self.chat_id)

    async def get_input_chat(self) -> Any:
        peer = self.chat_id
        if isinstance(peer, bytes):
            return peer
        if peer in ("me", "self"):
            return {"_": "inputPeerSelf"}
        if isinstance(peer, int) and -1000000000000 < peer < 0:
            return {"_": "inputPeerChat", "chat_id": -peer}
        entity: Any = cast('dict[str, Any]', peer) if isinstance(peer, dict) else await self.ctx.resolve(peer)
        tag = str(_msg_field(entity, "_", ""))
        if tag.startswith("inputPeer"):
            return entity
        if tag in {"peerUser", "peerChannel", "peerChat"}:
            return await MessageOperations(self.ctx, {"chat_id": self._peer_id(entity)}).get_input_chat()
        ident = _msg_field(entity, "id")
        if not isinstance(ident, int) or isinstance(ident, bool) or ident <= 0:
            raise ValueError("peer could not be resolved")
        if tag in {"chat", "chatEmpty", "chatForbidden"}:
            return {"_": "inputPeerChat", "chat_id": ident}
        if tag == "user" and _msg_field(entity, "self", False):
            return {"_": "inputPeerSelf"}
        access_hash = _msg_field(entity, "access_hash")
        if not isinstance(access_hash, int) or isinstance(access_hash, bool) or access_hash == 0:
            raise ValueError("resolved peer has no usable access hash")
        channel = tag in {"channel", "channelForbidden"} or isinstance(peer, int) and peer <= -1000000000000
        return {"_": "inputPeerChannel" if channel else "inputPeerUser", "channel_id" if channel else "user_id": ident, "access_hash": access_hash}

    async def get_input_sender(self) -> Any:
        return await MessageOperations(self.ctx, {"chat_id": self.user_id}).get_input_chat() if self.user_id is not None else None

    async def send_text(self, text: str, **kwargs: Any) -> Any:
        data = self._send_options(kwargs)
        data.update(await self.ctx._ctx_prepare(text, data.pop("parse_mode", "html"), data.pop("kbd", None)))
        data.setdefault("random_id", secrets.randbits(63))
        return await self._rpc("messages.sendMessage", peer=await self.get_input_chat(), **data)

    async def edit(self, text: str, **kwargs: Any) -> Any:
        data = self._send_options(kwargs)
        data.pop("reply_to", None)
        data.update(await self.ctx._ctx_prepare(text, data.pop("parse_mode", "html"), data.pop("kbd", None)))
        return await self._on_message("messages.editMessage", id_key="id", **data)

    async def reply(self, text: str, **kwargs: Any) -> Any:
        return await self.send_text(text, **self._send_options(kwargs, reply=True))

    async def respond(self, text: str, **kwargs: Any) -> Any:
        mode = kwargs.pop("mode", kwargs.pop("output", "auto"))
        if kwargs.pop("force_reply", False):
            mode = "reply"
        if mode not in {"auto", "edit", "reply"}:
            raise ValueError("response mode must be auto, edit or reply")
        return await (self.edit(text, **kwargs) if mode == "edit" or mode == "auto" and self.out else self.reply(text, **kwargs))

    async def delete(self, revoke: bool = True) -> Any:
        chat, mid = self._target()
        if chat < -1000000000000:
            return await self._rpc("channels.deleteMessages", channel=await self.get_input_channel(), id=[mid])
        return await self._rpc("messages.deleteMessages", id=[mid], revoke=revoke)

    async def forward(self, to_chat: Any, **kwargs: Any) -> Any:
        topic = kwargs.pop("topic_id", self.topic_id if to_chat == self.chat_id else None)
        if topic is not None:
            kwargs["top_msg_id"] = topic
        return await self._rpc("messages.forwardMessages", from_peer=await self.get_input_chat(), id=[self.id], to_peer=await MessageOperations(self.ctx, {"chat_id": to_chat}).get_input_chat(), random_id=[secrets.randbits(63)], **kwargs)

    async def download(self, destination: Any = None, **kwargs: Any) -> Any:
        if not self.has_media:
            raise ValueError("this message has no media")
        return await self.ctx.download_file(self.raw, destination, **kwargs)

    async def get_reply_message(self) -> Any:
        direct = self._g("reply_to_message", "reply_msg")
        header = self._g("reply_to")
        mid = self._g("reply_to_msg_id") or _msg_field(header, "reply_to_msg_id")
        peer = self._peer_id(_msg_field(header, "reply_to_peer_id")) or self.chat_id
        raw = direct if direct is not None else await self.ctx.msg(peer, mid) if mid else None
        return {"chat_id": peer, **cast("dict[str, Any]", raw)} if isinstance(raw, dict) else raw

    async def refresh(self) -> Any:
        chat, mid = self._target()
        raw = await self.ctx.msg(chat, mid)
        if raw is None:
            raise LookupError("message no longer exists or is inaccessible")
        return {"chat_id": chat, **cast("dict[str, Any]", raw)} if isinstance(raw, dict) else raw

    async def edit_buttons(self, buttons: Any = None) -> Any:
        prepared = await self.ctx._ctx_prepare(None, None, buttons if buttons is not None else {"inline_keyboard": []})
        return await self._on_message("messages.editMessage", id_key="id", **prepared)

    async def _prepare_media(self, media: Any, kind: str, file_name: str | None = None, mime: str | None = None) -> Any:
        if kind not in {"document", "photo", "video", "audio", "voice", "animation", "sticker"}:
            raise ValueError("unsupported media kind")
        media = _msg_field(media, "media", media)
        ctor = str(_msg_field(media, "_", ""))
        if ctor.startswith("inputMedia"):
            return media
        for name in ("photo", "document"):
            item = _msg_field(media, name) or (media if ctor in {name, "input" + name.title()} else None)
            if item is not None:
                ref = {"_": "input" + name.title(), **{key: _msg_field(item, key) for key in ("id", "access_hash", "file_reference")}}
                return {"_": "inputMedia" + name.title(), "id": ref}
        if ctor:
            raise ValueError(f"cannot send {ctor} as InputMedia")
        if isinstance(media, str) and media.startswith(("https://", "http://")):
            return {"_": "inputMediaPhotoExternal" if kind == "photo" else "inputMediaDocumentExternal", "url": media}
        up = await self.ctx.upload_file(media, file_name=file_name)
        mime = mime or mimetypes.guess_type(str(file_name or up.get("name", "")))[0]
        if not mime or mime == "application/octet-stream":
            mime = {"voice": "audio/ogg", "audio": "audio/mpeg", "video": "video/mp4", "animation": "video/mp4", "sticker": "image/webp"}.get(kind, "application/octet-stream")
        payload = self.ctx.file_media(up, mime=mime, file_name=file_name, force_file=kind == "document")
        if kind == "photo":
            return {"_": "inputMediaUploadedPhoto", "file": payload["file"]}
        attrs: Any = payload["attributes"]
        if kind in {"audio", "voice"}:
            attrs.append({"_": "documentAttributeAudio", "voice": kind == "voice", "duration": 0})
        elif kind == "video":
            attrs.append({"_": "documentAttributeVideo", "duration": 0, "w": 0, "h": 0, "supports_streaming": True})
        elif kind == "animation":
            attrs.append({"_": "documentAttributeAnimated"})
        elif kind == "sticker":
            attrs.append({"_": "documentAttributeSticker", "alt": "", "stickerset": {"_": "inputStickerSetEmpty"}})
        return payload

    async def edit_media(self, media: Any, *, caption: str | None = None, kind: str = "document", **kwargs: Any) -> Any:
        data = self._send_options(kwargs)
        data.pop("reply_to", None)
        media = await self._prepare_media(media, kind, data.pop("file_name", None), data.pop("mime_type", data.pop("mime", None)))
        data.update(await self.ctx._ctx_prepare(caption, data.pop("parse_mode", "html"), data.pop("kbd", None)))
        return await self._on_message("messages.editMessage", id_key="id", media=media, **data)

    async def send_media(self, media: Any, caption: str | None = None, *, kind: str = "document", **kwargs: Any) -> Any:
        data = self._send_options(kwargs)
        media = await self._prepare_media(media, kind, data.pop("file_name", None), data.pop("mime_type", data.pop("mime", None)))
        data.update(await self.ctx._ctx_prepare(caption or "", data.pop("parse_mode", "html"), data.pop("kbd", None)))
        data.setdefault("random_id", secrets.randbits(63))
        return await self._rpc("messages.sendMedia", peer=await self.get_input_chat(), media=media, **data)

    async def send_rich(self, rich: Any, **kwargs: Any) -> Any:
        payload = rich.to_tl() if hasattr(rich, "to_tl") else {"_": "inputRichMessageHTML", "html": rich} if isinstance(rich, str) else rich
        return await self.send_text("", rich_message=payload, **kwargs)

    async def edit_rich(self, rich: Any, **kwargs: Any) -> Any:
        payload = rich.to_tl() if hasattr(rich, "to_tl") else {"_": "inputRichMessageHTML", "html": rich} if isinstance(rich, str) else rich
        return await self.edit("", rich_message=payload, **kwargs)

    async def schedule(self, text: str, when: int, **kwargs: Any) -> Any:
        return await self.send_text(text, schedule_date=when, **kwargs)

    async def click_button(self, button: Any, *, share_phone: Any = None, share_geo: Any = None, password: Any = None, open_url: bool = False) -> Any:
        kind = _msg_field(button, "type", button)
        ctor = str(_msg_field(kind, "_", ""))
        data = _msg_field(kind, "data", _msg_field(button, "callback_data"))
        if data is not None or ctor in {"keyboardButtonGame", "inlineButtonTypeGame"}:
            args: dict[str, Any] = {"game": True} if data is None else {"data": data.encode() if isinstance(data, str) else data}
            if password is not None:
                if isinstance(password, str):
                    raise ValueError("password requires an InputCheckPasswordSRP value")
                args["password"] = password
            return await self._on_message("messages.getBotCallbackAnswer", **args)
        url = _msg_field(kind, "url", _msg_field(button, "url"))
        if url is not None and ctor not in {"keyboardButtonUrlAuth", "inlineButtonTypeUrlAuth"}:
            if open_url:
                raise PermissionError("opening a browser is not a message operation")
            return url
        if ctor in {"keyboardButtonRequestPhone", "buttonTypeRequestPhone"} or _msg_field(button, "request_contact"):
            if not share_phone:
                raise ValueError("share_phone must be explicit")
            if isinstance(share_phone, dict):
                return await self.reply_media(share_phone)
            me = await self._rpc("users.getUsers", id=[{"_": "inputUserSelf"}])
            me = cast("list[Any]", me)[0] if isinstance(me, list) and me else cast(Any, me)
            phone = _msg_field(me, "phone") if share_phone is True else share_phone
            return await self.reply_contact(phone, _msg_field(me, "first_name", ""), last_name=_msg_field(me, "last_name", ""))
        if ctor in {"keyboardButtonRequestGeoLocation", "buttonTypeRequestGeoLocation"} or _msg_field(button, "request_location"):
            if share_geo is None:
                raise ValueError("share_geo=(longitude, latitude) must be explicit")
            if isinstance(share_geo, (tuple, list)):
                longitude, latitude = cast('Any', share_geo)
                return await self.reply_location(latitude, longitude)
            geo = share_geo if _msg_field(share_geo, "_") == "inputMediaGeoPoint" else {"_": "inputMediaGeoPoint", "geo_point": share_geo}
            return await self.reply_media(geo)
        if ctor in {"keyboardButtonSwitchInline", "inlineButtonTypeSwitchInline"} or _msg_field(button, "switch_inline_query") is not None or _msg_field(button, "switch_inline_query_current_chat") is not None:
            bot = self._g("via_bot_id") or self.user_id
            query = _msg_field(kind, "query", _msg_field(button, "switch_inline_query", _msg_field(button, "switch_inline_query_current_chat", "")))
            import secrets
            return await self._rpc("messages.startBot", bot=self._input_entity(await MessageOperations(self.ctx, {"chat_id": bot}).get_input_chat(), "user"), peer=await self.get_input_chat(), random_id=secrets.randbits(63), start_param=query)
        if ctor in {"keyboardButton", "buttonTypeDefault"} or not ctor and (isinstance(button, str) or isinstance(button, dict) and set(cast("dict[str, Any]", button)) <= {"text"}):
            return await self.reply(button if isinstance(button, str) else _msg_field(button, "text", ""))
        raise NotImplementedError(f"click is not supported for {ctor or 'this button type'}")


class MessageButton:
    def __init__(self, message: MessageOperations, raw: Any) -> None:
        self.message = message
        self.raw = raw

    @property
    def text(self) -> str:
        text = self.raw if isinstance(self.raw, str) else _msg_field(self.raw, "text", "")
        return str(_msg_field(text, "text", text))

    def get(self, key: str, default: Any = None) -> Any:
        return _msg_field(self.raw, key, default)

    def __getitem__(self, key: str) -> Any:
        value = self.get(key, _SENTINEL)
        if value is _SENTINEL:
            raise KeyError(key)
        return value

    def __getattr__(self, key: str) -> Any:
        value = _msg_field(self.__dict__.get("raw"), key, _SENTINEL)
        if value is _SENTINEL:
            raise AttributeError(key)
        return value

    @property
    def data(self) -> Any:
        kind = _msg_field(self.raw, "type", self.raw)
        return _msg_field(kind, "data", _msg_field(self.raw, "callback_data"))

    @property
    def url(self) -> Any:
        return _msg_field(_msg_field(self.raw, "type", self.raw), "url")

    async def click(self, **kwargs: Any) -> Any:
        return await self.message.click_button(self.raw, **kwargs)


CTX_OPERATIONS: dict[str, Callable[..., Awaitable[Any]]] = {
    name: cast(Callable[..., Awaitable[Any]], method) for name, method in vars(MessageOperations).items()
    if not name.startswith("_") and callable(method) and hasattr(method, "__code__") and method.__code__.co_flags & 128
}


class ContextOperations:
    async def _ctx_operation(self, name: str, *args: Any, target: Any = None, **kwargs: Any) -> Any:
        operation = CTX_OPERATIONS.get(name)
        if operation is None:
            raise AttributeError(name)
        source = target if target is not None else getattr(self, "_source", None)
        if source is None:
            source = getattr(self, "_msg", {})
        if name in CTX_CATALOG:
            return await operation(MessageOperations(self, source), *args, **kwargs)
        peer = kwargs.pop("peer", kwargs.pop("chat_id", _SENTINEL))
        mid = kwargs.pop("message_id", _SENTINEL)
        topic = kwargs.pop("topic_id", _SENTINEL)
        if peer is not _SENTINEL or mid is not _SENTINEL or topic is not _SENTINEL:
            original = MessageOperations(self, source)
            same_peer = peer is _SENTINEL or peer == original.chat_id
            data = dict(cast("dict[str, Any]", source)) if isinstance(source, dict) else {key: _msg_field(source, key) for key in ("media", "poll", "reply_to", "reply_to_message", "from_id", "via_bot_id", "reply_markup", "src")}
            data.update({"chat_id": original.chat_id if peer is _SENTINEL else peer,
                         "id": original.id if mid is _SENTINEL and same_peer else None if mid is _SENTINEL else mid,
                         "_ctx_topic_override": original.topic_id if topic is _SENTINEL and same_peer else None if topic is _SENTINEL else topic,
                         "out": original.out if same_peer else False})
            data.pop("message_id", None)
            data.pop("msg_id", None)
            data["is_me"] = data["out"]
            if not same_peer:
                data.pop("reply_to", None)
                data.pop("reply_to_message", None)
            source = data
            if topic is not _SENTINEL and name in {"forward", "forward_to", "copy_to", "save"}:
                kwargs["topic_id"] = topic
        return await operation(MessageOperations(self, source), *args, **kwargs)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(CTX_OPERATIONS))

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name not in CTX_OPERATIONS:
            raise AttributeError(name)
        async def operation(*args: Any, **kwargs: Any) -> Any:
            return await self._ctx_operation(name, *args, **kwargs)
        return operation


def _ctx_text(value: Any) -> dict[str, Any]:
    if isinstance(value, str) and value.strip():
        return {"_": "textWithEntities", "text": value, "entities": []}
    if isinstance(value, dict):
        data = cast('dict[str, Any]', value)
        if data.get("_") == "textWithEntities" and isinstance(data.get("text"), str) and data["text"].strip():
            return dict(data)
    raise ValueError("expected non-empty text or textWithEntities")


def _ctx_todo_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("todo items must be a non-empty sequence")
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    for index, value in enumerate(cast('list[Any]', items), 1):
        item = cast('dict[str, Any]', value) if isinstance(value, dict) else {"id": index, "title": value}
        item_id = item.get("id", index)
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0 or item_id in seen:
            raise ValueError("todo item ids must be unique positive integers")
        seen.add(item_id)
        result.append({"_": "todoItem", "id": item_id, "title": _ctx_text(item.get("title"))})
    return result


def _ctx_todo(title: Any, items: Any, others_can_append: bool, others_can_complete: bool) -> dict[str, Any]:
    return {"_": "inputMediaTodo", "todo": {"_": "todoList", "title": _ctx_text(title), "list": _ctx_todo_items(items), "others_can_append": others_can_append, "others_can_complete": others_can_complete}}


async def _ctx_send_todo(target: MessageOperations, title: Any, items: Any, *, others_can_append: bool = False, others_can_complete: bool = False, **kwargs: Any) -> Any:
    return await target.send_media(_ctx_todo(title, items, others_can_append, others_can_complete), **kwargs)


async def _ctx_edit_todo(target: MessageOperations, title: Any, items: Any, *, others_can_append: bool = False, others_can_complete: bool = False, **kwargs: Any) -> Any:
    return await target.edit_media(_ctx_todo(title, items, others_can_append, others_can_complete), **kwargs)


async def _ctx_send_poll(target: MessageOperations, question: Any, answers: Any, *, multiple_choice: bool = False, public_voters: bool = False, correct_answers: Any = None, solution: str | None = None, poll_options: dict[str, Any] | None = None, **kwargs: Any) -> Any:
    if not isinstance(answers, (list, tuple)) or len(cast("list[Any]", answers)) < 2:
        raise ValueError("a poll requires at least two answers")
    choices: list[dict[str, Any]] = []
    options: set[bytes] = set()
    for index, value in enumerate(cast('list[Any]', answers)):
        item: dict[str, Any] = dict(cast("dict[str, Any]", value)) if isinstance(value, dict) and "text" in value and _msg_field(value, "_") != "textWithEntities" else {"text": value}
        option = item.get("option", index.to_bytes(4, "big"))
        if not isinstance(option, bytes) or not option or option in options:
            raise ValueError("poll option identifiers must be unique non-empty bytes")
        options.add(option)
        choices.append({"_": "pollAnswer", "text": _ctx_text(item["text"]), "option": option})
    poll: dict[str, Any] = {"_": "poll", "id": 0, "hash": 0, "question": _ctx_text(question), "answers": choices, "multiple_choice": multiple_choice, "public_voters": public_voters}
    extra = dict(poll_options or {})
    allowed = {"open_answers", "revoting_disabled", "shuffle_answers", "hide_results_until_close", "subscribers_only", "countries_iso2", "close_period", "close_date"}
    if set(extra) - allowed:
        raise ValueError("unsupported poll option")
    poll.update(extra)
    media: dict[str, Any] = {"_": "inputMediaPoll", "poll": poll}
    if correct_answers is not None:
        indices: list[Any] = list(cast("list[Any]", correct_answers)) if isinstance(correct_answers, (list, tuple)) else [correct_answers]
        if not indices or any(not isinstance(i, int) or isinstance(i, bool) or i < 0 or i >= len(choices) for i in indices) or len(set(indices)) != len(indices):
            raise ValueError("correct_answers must contain valid unique answer indices")
        poll["quiz"] = True
        media["correct_answers"] = indices
    if solution is not None:
        if correct_answers is None:
            raise ValueError("a solution requires a quiz")
        media["solution"] = solution
        media["solution_entities"] = []
    return await target.send_media(media, **kwargs)


async def _ctx_append_todo(target: MessageOperations, items: Any) -> Any:
    raw = await target.refresh()
    todo = _msg_field(_msg_field(raw, "media"), "todo")
    if todo is None:
        raise ValueError("target message is not a checklist")
    existing = {_msg_field(item, "id") for item in _msg_field(todo, "list", [])}
    if not isinstance(items, (list, tuple)) or not items:
        raise ValueError("items must be a non-empty sequence")
    next_id = max(existing, default=0) + 1
    prepared: list[dict[str, Any]] = []
    for value in cast('list[Any]', items):
        if isinstance(value, dict) and "id" in value:
            item = cast('dict[str, Any]', value)
        else:
            while next_id in existing:
                next_id += 1
            item = {"id": next_id, "title": _msg_field(value, "title", value)}
            next_id += 1
        if item["id"] in existing:
            raise ValueError("todo item id already exists")
        existing.add(item["id"])
        prepared.append(item)
    if target.id is None:
        raise ValueError("message id is unavailable")
    return await target.ctx.mt("messages.appendTodoList", peer=await target.get_input_chat(), msg_id=target.id, list=_ctx_todo_items(prepared))


async def _ctx_close_poll(target: MessageOperations) -> Any:
    raw = await target.refresh()
    value = _msg_field(_msg_field(raw, "media"), "poll")
    if not isinstance(value, dict):
        raise ValueError("target message is not a poll")
    poll = dict(cast('dict[str, Any]', value))
    poll["closed"] = True
    return await target.edit_media({"_": "inputMediaPoll", "poll": poll})


CTX_OPERATIONS.update({"send_todo": _ctx_send_todo, "edit_todo": _ctx_edit_todo, "append_todo": _ctx_append_todo, "send_poll": _ctx_send_poll, "close_poll": _ctx_close_poll})


CTX_CATALOG: dict[str, tuple[Any, ...]] = {
    'set_checklist_completed': ('messages.toggleTodoCompleted', ['peer', 'msg_id', 'completed', 'incompleted'], {'peer': 'InputPeer', 'msg_id': 'int', 'completed': 'Vector<int>', 'incompleted': 'Vector<int>'}, {'peer': '$peer', 'msg_id': '$message'}, []),
    'add_poll_answer': ('messages.addPollAnswer', ['peer', 'msg_id', 'answer'], {'peer': 'InputPeer', 'msg_id': 'int', 'answer': 'PollAnswer'}, {'peer': '$peer', 'msg_id': '$message'}, []),
    'remove_poll_answer': ('messages.deletePollAnswer', ['peer', 'msg_id', 'option'], {'peer': 'InputPeer', 'msg_id': 'int', 'option': 'bytes'}, {'peer': '$peer', 'msg_id': '$message'}, []),
    'unread_poll_votes': ('messages.getUnreadPollVotes', ['peer', 'offset_id', 'add_offset', 'limit', 'max_id', 'min_id'], {'peer': 'InputPeer', 'top_msg_id': 'flags.0?int', 'offset_id': 'int', 'add_offset': 'int', 'limit': 'int', 'max_id': 'int', 'min_id': 'int'}, {'peer': '$peer', 'offset_id': 0, 'add_offset': 0, 'limit': 50, 'max_id': 0, 'min_id': 0}, []),
    'mark_poll_votes_read': ('messages.readPollVotes', ['peer'], {'peer': 'InputPeer', 'top_msg_id': 'flags.0?int'}, {'peer': '$peer'}, []),
    'poll_statistics': ('stats.getPollStats', ['peer', 'msg_id'], {'dark': 'flags.0?true', 'peer': 'InputPeer', 'msg_id': 'int'}, {'peer': '$peer', 'msg_id': '$message'}, []),
    'scheduled_messages': ('messages.getScheduledHistory', ['peer', 'hash'], {'peer': 'InputPeer', 'hash': 'long'}, {'peer': '$peer', 'hash': 0}, []),
    'scheduled_by_ids': ('messages.getScheduledMessages', ['peer', 'id'], {'peer': 'InputPeer', 'id': 'Vector<int>'}, {'peer': '$peer'}, []),
    'publish_scheduled': ('messages.sendScheduledMessages', ['peer', 'id'], {'peer': 'InputPeer', 'id': 'Vector<int>'}, {'peer': '$peer'}, []),
    'cancel_scheduled': ('messages.deleteScheduledMessages', ['peer', 'id'], {'peer': 'InputPeer', 'id': 'Vector<int>'}, {'peer': '$peer'}, []),
    'configure_chat_reactions': ('messages.setChatAvailableReactions', ['peer', 'available_reactions'], {'peer': 'InputPeer', 'available_reactions': 'ChatReactions', 'reactions_limit': 'flags.0?int', 'paid_enabled': 'flags.1?Bool'}, {'peer': '$peer'}, []),
    'available_reactions': ('messages.getAvailableReactions', ['hash'], {'hash': 'int'}, {'hash': 0}, []),
    'set_default_reaction': ('messages.setDefaultReaction', ['reaction'], {'reaction': 'Reaction'}, {}, []),
    'unread_reactions': ('messages.getUnreadReactions', ['peer', 'offset_id', 'add_offset', 'limit', 'max_id', 'min_id'], {'peer': 'InputPeer', 'top_msg_id': 'flags.0?int', 'saved_peer_id': 'flags.1?InputPeer', 'offset_id': 'int', 'add_offset': 'int', 'limit': 'int', 'max_id': 'int', 'min_id': 'int'}, {'peer': '$peer', 'offset_id': 0, 'add_offset': 0, 'limit': 50, 'max_id': 0, 'min_id': 0}, []),
    'mark_reactions_read': ('messages.readReactions', ['peer'], {'peer': 'InputPeer', 'top_msg_id': 'flags.0?int', 'saved_peer_id': 'flags.1?InputPeer'}, {'peer': '$peer'}, []),
    'popular_reactions': ('messages.getTopReactions', ['limit', 'hash'], {'limit': 'int', 'hash': 'long'}, {'limit': 50, 'hash': 0}, []),
    'recent_reactions': ('messages.getRecentReactions', ['limit', 'hash'], {'limit': 'int', 'hash': 'long'}, {'limit': 50, 'hash': 0}, []),
    'clear_recent_reactions': ('messages.clearRecentReactions', [], {}, {}, []),
    'remove_member_reactions': ('messages.deleteParticipantReactions', ['peer', 'participant'], {'peer': 'InputPeer', 'participant': 'InputPeer'}, {'peer': '$peer'}, []),
    'remove_member_message_reaction': ('messages.deleteParticipantReaction', ['peer', 'msg_id', 'participant'], {'peer': 'InputPeer', 'msg_id': 'int', 'participant': 'InputPeer'}, {'peer': '$peer', 'msg_id': '$message'}, []),
    'saved_dialogs': ('messages.getSavedDialogs', ['offset_date', 'offset_id', 'offset_peer', 'limit', 'hash'], {'exclude_pinned': 'flags.0?true', 'parent_peer': 'flags.1?InputPeer', 'offset_date': 'int', 'offset_id': 'int', 'offset_peer': 'InputPeer', 'limit': 'int', 'hash': 'long'}, {'offset_date': 0, 'offset_id': 0, 'offset_peer': {'_': 'inputPeerEmpty'}, 'limit': 50, 'hash': 0}, []),
    'saved_history': ('messages.getSavedHistory', ['peer', 'offset_id', 'offset_date', 'add_offset', 'limit', 'max_id', 'min_id', 'hash'], {'parent_peer': 'flags.0?InputPeer', 'peer': 'InputPeer', 'offset_id': 'int', 'offset_date': 'int', 'add_offset': 'int', 'limit': 'int', 'max_id': 'int', 'min_id': 'int', 'hash': 'long'}, {'offset_id': 0, 'offset_date': 0, 'add_offset': 0, 'limit': 50, 'max_id': 0, 'min_id': 0, 'hash': 0}, []),
    'clear_saved_history': ('messages.deleteSavedHistory', ['peer', 'max_id'], {'parent_peer': 'flags.0?InputPeer', 'peer': 'InputPeer', 'max_id': 'int', 'min_date': 'flags.2?int', 'max_date': 'flags.3?int'}, {}, []),
    'pinned_saved_dialogs': ('messages.getPinnedSavedDialogs', [], {}, {}, []),
    'set_saved_dialog_pinned': ('messages.toggleSavedDialogPin', ['peer'], {'pinned': 'flags.0?true', 'peer': 'InputDialogPeer'}, {}, []),
    'reorder_saved_pins': ('messages.reorderPinnedSavedDialogs', ['order'], {'force': 'flags.0?true', 'order': 'Vector<InputDialogPeer>'}, {}, []),
    'saved_reaction_tags': ('messages.getSavedReactionTags', ['hash'], {'peer': 'flags.0?InputPeer', 'hash': 'long'}, {'hash': 0}, []),
    'rename_saved_tag': ('messages.updateSavedReactionTag', ['reaction'], {'reaction': 'Reaction', 'title': 'flags.0?string'}, {}, []),
    'suggested_saved_tags': ('messages.getDefaultTagReactions', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'saved_dialogs_by_peers': ('messages.getSavedDialogsByID', ['ids'], {'parent_peer': 'flags.1?InputPeer', 'ids': 'Vector<InputPeer>'}, {}, []),
    'mark_saved_history_read': ('messages.readSavedHistory', ['parent_peer', 'peer', 'max_id'], {'parent_peer': 'InputPeer', 'peer': 'InputPeer', 'max_id': 'int'}, {}, []),
    'forum_topics': ('messages.getForumTopics', ['peer', 'offset_date', 'offset_id', 'offset_topic', 'limit'], {'peer': 'InputPeer', 'q': 'flags.0?string', 'offset_date': 'int', 'offset_id': 'int', 'offset_topic': 'int', 'limit': 'int'}, {'peer': '$peer', 'offset_date': 0, 'offset_id': 0, 'offset_topic': 0, 'limit': 50}, []),
    'forum_topics_by_ids': ('messages.getForumTopicsByID', ['peer', 'topics'], {'peer': 'InputPeer', 'topics': 'Vector<int>'}, {'peer': '$peer'}, []),
    'edit_forum_topic': ('messages.editForumTopic', ['peer', 'topic_id'], {'peer': 'InputPeer', 'topic_id': 'int', 'title': 'flags.0?string', 'icon_emoji_id': 'flags.1?long', 'closed': 'flags.2?Bool', 'hidden': 'flags.3?Bool'}, {'peer': '$peer'}, []),
    'set_forum_topic_pinned': ('messages.updatePinnedForumTopic', ['peer', 'topic_id', 'pinned'], {'peer': 'InputPeer', 'topic_id': 'int', 'pinned': 'Bool'}, {'peer': '$peer'}, []),
    'reorder_forum_pins': ('messages.reorderPinnedForumTopics', ['peer', 'order'], {'force': 'flags.0?true', 'peer': 'InputPeer', 'order': 'Vector<int>'}, {'peer': '$peer'}, []),
    'create_forum_topic': ('messages.createForumTopic', ['peer', 'title', 'random_id'], {'title_missing': 'flags.4?true', 'peer': 'InputPeer', 'title': 'string', 'icon_color': 'flags.0?int', 'icon_emoji_id': 'flags.3?long', 'random_id': 'long', 'send_as': 'flags.2?InputPeer'}, {'peer': '$peer', 'random_id': '$random'}, []),
    'remove_forum_topic': ('messages.deleteTopicHistory', ['peer', 'top_msg_id'], {'peer': 'InputPeer', 'top_msg_id': 'int'}, {'peer': '$peer'}, []),
    'set_forum_enabled': ('channels.toggleForum', ['channel', 'enabled', 'tabs'], {'channel': 'InputChannel', 'enabled': 'Bool', 'tabs': 'Bool'}, {'channel': '$peer'}, []),
    'set_forum_message_view': ('channels.toggleViewForumAsMessages', ['channel', 'enabled'], {'channel': 'InputChannel', 'enabled': 'Bool'}, {'channel': '$peer'}, []),
    'story_posting_quota': ('stories.canSendStory', ['peer'], {'peer': 'InputPeer'}, {}, []),
    'post_story': ('stories.sendStory', ['peer', 'media', 'privacy_rules', 'random_id'], {'pinned': 'flags.2?true', 'noforwards': 'flags.4?true', 'fwd_modified': 'flags.7?true', 'peer': 'InputPeer', 'media': 'InputMedia', 'media_areas': 'flags.5?Vector<MediaArea>', 'caption': 'flags.0?string', 'entities': 'flags.1?Vector<MessageEntity>', 'privacy_rules': 'Vector<InputPrivacyRule>', 'random_id': 'long', 'period': 'flags.3?int', 'fwd_from_id': 'flags.6?InputPeer', 'fwd_from_story': 'flags.6?int', 'albums': 'flags.8?Vector<int>', 'music': 'flags.9?InputDocument'}, {'random_id': '$random'}, []),
    'edit_story': ('stories.editStory', ['peer', 'id'], {'peer': 'InputPeer', 'id': 'int', 'media': 'flags.0?InputMedia', 'media_areas': 'flags.3?Vector<MediaArea>', 'caption': 'flags.1?string', 'entities': 'flags.1?Vector<MessageEntity>', 'privacy_rules': 'flags.2?Vector<InputPrivacyRule>', 'music': 'flags.4?InputDocument'}, {}, []),
    'delete_stories': ('stories.deleteStories', ['peer', 'id'], {'peer': 'InputPeer', 'id': 'Vector<int>'}, {}, []),
    'set_stories_on_profile': ('stories.togglePinned', ['peer', 'id', 'pinned'], {'peer': 'InputPeer', 'id': 'Vector<int>', 'pinned': 'Bool'}, {}, []),
    'story_feed': ('stories.getAllStories', [], {'next': 'flags.1?true', 'hidden': 'flags.2?true', 'state': 'flags.0?string'}, {}, []),
    'profile_stories': ('stories.getPinnedStories', ['peer', 'offset_id', 'limit'], {'peer': 'InputPeer', 'offset_id': 'int', 'limit': 'int'}, {'offset_id': 0, 'limit': 50}, []),
    'archived_stories': ('stories.getStoriesArchive', ['peer', 'offset_id', 'limit'], {'peer': 'InputPeer', 'offset_id': 'int', 'limit': 'int'}, {'offset_id': 0, 'limit': 50}, []),
    'stories_by_ids': ('stories.getStoriesByID', ['peer', 'id'], {'peer': 'InputPeer', 'id': 'Vector<int>'}, {}, []),
    'mark_stories_read': ('stories.readStories', ['peer', 'max_id'], {'peer': 'InputPeer', 'max_id': 'int'}, {'max_id': 0}, []),
    'story_viewers': ('stories.getStoryViewsList', ['peer', 'id', 'offset', 'limit'], {'just_contacts': 'flags.0?true', 'reactions_first': 'flags.2?true', 'forwards_first': 'flags.3?true', 'peer': 'InputPeer', 'q': 'flags.1?string', 'id': 'int', 'offset': 'string', 'limit': 'int'}, {'offset': '', 'limit': 50}, []),
    'story_view_counts': ('stories.getStoriesViews', ['peer', 'id'], {'peer': 'InputPeer', 'id': 'Vector<int>'}, {}, []),
    'story_link': ('stories.exportStoryLink', ['peer', 'id'], {'peer': 'InputPeer', 'id': 'int'}, {}, []),
    'activate_story_stealth': ('stories.activateStealthMode', [], {'past': 'flags.0?true', 'future': 'flags.1?true'}, {}, []),
    'react_to_story': ('stories.sendReaction', ['peer', 'story_id', 'reaction'], {'add_to_recent': 'flags.0?true', 'peer': 'InputPeer', 'story_id': 'int', 'reaction': 'Reaction'}, {}, []),
    'peer_stories': ('stories.getPeerStories', ['peer'], {'peer': 'InputPeer'}, {}, []),
    'story_read_markers': ('stories.getAllReadPeerStories', [], {}, {}, []),
    'peer_latest_story_ids': ('stories.getPeerMaxIDs', ['id'], {'id': 'Vector<InputPeer>'}, {}, []),
    'story_posting_chats': ('stories.getChatsToSend', [], {}, {}, []),
    'set_peer_stories_hidden': ('stories.togglePeerStoriesHidden', ['peer', 'hidden'], {'peer': 'InputPeer', 'hidden': 'Bool'}, {}, []),
    'story_reaction_senders': ('stories.getStoryReactionsList', ['peer', 'id', 'limit'], {'forwards_first': 'flags.2?true', 'peer': 'InputPeer', 'id': 'int', 'reaction': 'flags.0?Reaction', 'offset': 'flags.1?string', 'limit': 'int'}, {'limit': 50}, []),
    'reorder_profile_story_pins': ('stories.togglePinnedToTop', ['peer', 'id'], {'peer': 'InputPeer', 'id': 'Vector<int>'}, {}, []),
    'search_stories': ('stories.searchPosts', ['offset', 'limit'], {'hashtag': 'flags.0?string', 'area': 'flags.1?MediaArea', 'peer': 'flags.2?InputPeer', 'offset': 'string', 'limit': 'int'}, {'offset': '', 'limit': 50}, []),
    'create_story_album': ('stories.createAlbum', ['peer', 'title', 'stories'], {'peer': 'InputPeer', 'title': 'string', 'stories': 'Vector<int>'}, {}, []),
    'edit_story_album': ('stories.updateAlbum', ['peer', 'album_id'], {'peer': 'InputPeer', 'album_id': 'int', 'title': 'flags.0?string', 'delete_stories': 'flags.1?Vector<int>', 'add_stories': 'flags.2?Vector<int>', 'order': 'flags.3?Vector<int>'}, {}, []),
    'reorder_story_albums': ('stories.reorderAlbums', ['peer', 'order'], {'peer': 'InputPeer', 'order': 'Vector<int>'}, {}, []),
    'remove_story_album': ('stories.deleteAlbum', ['peer', 'album_id'], {'peer': 'InputPeer', 'album_id': 'int'}, {}, []),
    'story_albums': ('stories.getAlbums', ['peer', 'hash'], {'peer': 'InputPeer', 'hash': 'long'}, {'hash': 0}, []),
    'story_album_items': ('stories.getAlbumStories', ['peer', 'album_id', 'offset', 'limit'], {'peer': 'InputPeer', 'album_id': 'int', 'offset': 'int', 'limit': 'int'}, {'offset': 0, 'limit': 50}, []),
    'stickers_for_emoji': ('messages.getStickers', ['emoticon', 'hash'], {'emoticon': 'string', 'hash': 'long'}, {'hash': 0}, []),
    'installed_sticker_sets': ('messages.getAllStickers', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'sticker_set': ('messages.getStickerSet', ['stickerset', 'hash'], {'stickerset': 'InputStickerSet', 'hash': 'int'}, {'hash': 0}, []),
    'install_sticker_set': ('messages.installStickerSet', ['stickerset', 'archived'], {'stickerset': 'InputStickerSet', 'archived': 'Bool'}, {'archived': False}, []),
    'uninstall_sticker_set': ('messages.uninstallStickerSet', ['stickerset'], {'stickerset': 'InputStickerSet'}, {}, []),
    'reorder_sticker_sets': ('messages.reorderStickerSets', ['order'], {'masks': 'flags.0?true', 'emojis': 'flags.1?true', 'order': 'Vector<long>'}, {}, []),
    'saved_gifs': ('messages.getSavedGifs', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'set_gif_saved': ('messages.saveGif', ['id', 'unsave'], {'id': 'InputDocument', 'unsave': 'Bool'}, {}, []),
    'featured_sticker_sets': ('messages.getFeaturedStickers', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'mark_featured_stickers_seen': ('messages.readFeaturedStickers', ['id'], {'id': 'Vector<long>'}, {}, []),
    'recent_stickers': ('messages.getRecentStickers', ['hash'], {'attached': 'flags.0?true', 'hash': 'long'}, {'hash': 0}, []),
    'set_sticker_recent': ('messages.saveRecentSticker', ['id', 'unsave'], {'attached': 'flags.0?true', 'id': 'InputDocument', 'unsave': 'Bool'}, {}, []),
    'clear_recent_stickers': ('messages.clearRecentStickers', [], {'attached': 'flags.0?true'}, {}, []),
    'archived_sticker_sets': ('messages.getArchivedStickers', ['offset_id', 'limit'], {'masks': 'flags.0?true', 'emojis': 'flags.1?true', 'offset_id': 'long', 'limit': 'int'}, {'offset_id': 0, 'limit': 50}, []),
    'installed_masks': ('messages.getMaskStickers', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'media_sticker_sets': ('messages.getAttachedStickers', ['media'], {'media': 'InputStickeredMedia'}, {}, []),
    'favorite_stickers': ('messages.getFavedStickers', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'set_sticker_favorite': ('messages.faveSticker', ['id', 'unfave'], {'id': 'InputDocument', 'unfave': 'Bool'}, {}, []),
    'search_sticker_sets': ('messages.searchStickerSets', ['q', 'hash'], {'exclude_featured': 'flags.0?true', 'q': 'string', 'hash': 'long'}, {'hash': 0}, []),
    'bulk_sticker_set_action': ('messages.toggleStickerSets', ['stickersets'], {'uninstall': 'flags.0?true', 'archive': 'flags.1?true', 'unarchive': 'flags.2?true', 'stickersets': 'Vector<InputStickerSet>'}, {}, []),
    'older_featured_sticker_sets': ('messages.getOldFeaturedStickers', ['offset', 'limit', 'hash'], {'offset': 'int', 'limit': 'int', 'hash': 'long'}, {'offset': 0, 'limit': 50, 'hash': 0}, []),
    'custom_emoji_documents': ('messages.getCustomEmojiDocuments', ['document_id'], {'document_id': 'Vector<long>'}, {}, []),
    'installed_emoji_sets': ('messages.getEmojiStickers', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'featured_emoji_sets': ('messages.getFeaturedEmojiStickers', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'search_custom_emoji': ('messages.searchCustomEmoji', ['emoticon', 'hash'], {'emoticon': 'string', 'hash': 'long'}, {'hash': 0}, []),
    'search_emoji_sets': ('messages.searchEmojiStickerSets', ['q', 'hash'], {'exclude_featured': 'flags.0?true', 'q': 'string', 'hash': 'long'}, {'hash': 0}, []),
    'owned_sticker_sets': ('messages.getMyStickers', ['offset_id', 'limit'], {'offset_id': 'long', 'limit': 'int'}, {'offset_id': 0, 'limit': 50}, []),
    'search_stickers': ('messages.searchStickers', ['q', 'emoticon', 'lang_code', 'offset', 'limit', 'hash'], {'emojis': 'flags.0?true', 'q': 'string', 'emoticon': 'string', 'lang_code': 'Vector<string>', 'offset': 'int', 'limit': 'int', 'hash': 'long'}, {'offset': 0, 'limit': 50, 'hash': 0}, []),
    'create_sticker_set': ('stickers.createStickerSet', ['user_id', 'title', 'short_name', 'stickers'], {'masks': 'flags.0?true', 'emojis': 'flags.5?true', 'text_color': 'flags.6?true', 'user_id': 'InputUser', 'title': 'string', 'short_name': 'string', 'thumb': 'flags.2?InputDocument', 'stickers': 'Vector<InputStickerSetItem>', 'software': 'flags.3?string'}, {}, []),
    'remove_sticker': ('stickers.removeStickerFromSet', ['sticker'], {'sticker': 'InputDocument'}, {}, []),
    'move_sticker': ('stickers.changeStickerPosition', ['sticker', 'position'], {'sticker': 'InputDocument', 'position': 'int'}, {}, []),
    'add_sticker': ('stickers.addStickerToSet', ['stickerset', 'sticker'], {'stickerset': 'InputStickerSet', 'sticker': 'InputStickerSetItem'}, {}, []),
    'set_sticker_set_thumbnail': ('stickers.setStickerSetThumb', ['stickerset'], {'stickerset': 'InputStickerSet', 'thumb': 'flags.0?InputDocument', 'thumb_document_id': 'flags.1?long'}, {}, []),
    'check_sticker_set_name': ('stickers.checkShortName', ['short_name'], {'short_name': 'string'}, {}, []),
    'suggest_sticker_set_name': ('stickers.suggestShortName', ['title'], {'title': 'string'}, {}, []),
    'edit_sticker_metadata': ('stickers.changeSticker', ['sticker'], {'sticker': 'InputDocument', 'emoji': 'flags.0?string', 'mask_coords': 'flags.1?MaskCoords', 'keywords': 'flags.2?string'}, {}, []),
    'rename_sticker_set': ('stickers.renameStickerSet', ['stickerset', 'title'], {'stickerset': 'InputStickerSet', 'title': 'string'}, {}, []),
    'remove_sticker_set': ('stickers.deleteStickerSet', ['stickerset'], {'stickerset': 'InputStickerSet'}, {}, []),
    'replace_sticker': ('stickers.replaceSticker', ['sticker', 'new_sticker'], {'sticker': 'InputDocument', 'new_sticker': 'InputStickerSetItem'}, {}, []),
    'contact_ids': ('contacts.getContactIDs', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'contact_statuses': ('contacts.getStatuses', [], {}, {}, []),
    'contacts_list': ('contacts.getContacts', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'import_contacts': ('contacts.importContacts', ['contacts'], {'contacts': 'Vector<InputContact>'}, {}, []),
    'remove_contacts': ('contacts.deleteContacts', ['id'], {'id': 'Vector<InputUser>'}, {}, []),
    'remove_contacts_by_phone': ('contacts.deleteByPhones', ['phones'], {'phones': 'Vector<string>'}, {}, []),
    'block_peer': ('contacts.block', ['id'], {'my_stories_from': 'flags.0?true', 'id': 'InputPeer'}, {}, []),
    'unblock_peer': ('contacts.unblock', ['id'], {'my_stories_from': 'flags.0?true', 'id': 'InputPeer'}, {}, []),
    'blocked_peers': ('contacts.getBlocked', ['offset', 'limit'], {'my_stories_from': 'flags.0?true', 'offset': 'int', 'limit': 'int'}, {'offset': 0, 'limit': 50}, []),
    'search_public_peers': ('contacts.search', ['q', 'limit'], {'broadcasts': 'flags.0?true', 'bots': 'flags.1?true', 'q': 'string', 'limit': 'int'}, {'limit': 50}, []),
    'frequent_peers': ('contacts.getTopPeers', ['offset', 'limit', 'hash'], {'correspondents': 'flags.0?true', 'bots_pm': 'flags.1?true', 'bots_inline': 'flags.2?true', 'phone_calls': 'flags.3?true', 'forward_users': 'flags.4?true', 'forward_chats': 'flags.5?true', 'groups': 'flags.10?true', 'channels': 'flags.15?true', 'bots_app': 'flags.16?true', 'bots_guestchat': 'flags.17?true', 'offset': 'int', 'limit': 'int', 'hash': 'long'}, {'offset': 0, 'limit': 50, 'hash': 0}, []),
    'reset_peer_rating': ('contacts.resetTopPeerRating', ['category', 'peer'], {'category': 'TopPeerCategory', 'peer': 'InputPeer'}, {}, []),
    'clear_imported_contacts': ('contacts.resetSaved', [], {}, {}, []),
    'imported_contacts': ('contacts.getSaved', [], {}, {}, []),
    'set_frequent_peers_enabled': ('contacts.toggleTopPeers', ['enabled'], {'enabled': 'Bool'}, {}, []),
    'add_contact': ('contacts.addContact', ['id', 'first_name', 'last_name', 'phone'], {'add_phone_privacy_exception': 'flags.0?true', 'id': 'InputUser', 'first_name': 'string', 'last_name': 'string', 'phone': 'string', 'note': 'flags.1?TextWithEntities'}, {}, []),
    'accept_contact': ('contacts.acceptContact', ['id'], {'id': 'InputUser'}, {}, []),
    'set_close_friends': ('contacts.editCloseFriends', ['id'], {'id': 'Vector<long>'}, {}, []),
    'replace_blocklist': ('contacts.setBlocked', ['id', 'limit'], {'my_stories_from': 'flags.0?true', 'id': 'Vector<InputPeer>', 'limit': 'int'}, {'limit': 50}, []),
    'contact_birthdays': ('contacts.getBirthdays', [], {}, {}, []),
    'edit_contact_note': ('contacts.updateContactNote', ['id', 'note'], {'id': 'InputUser', 'note': 'TextWithEntities'}, {}, []),
    'dialogs': ('messages.getDialogs', ['offset_date', 'offset_id', 'offset_peer', 'limit', 'hash'], {'exclude_pinned': 'flags.0?true', 'folder_id': 'flags.1?int', 'offset_date': 'int', 'offset_id': 'int', 'offset_peer': 'InputPeer', 'limit': 'int', 'hash': 'long'}, {'offset_date': 0, 'offset_id': 0, 'offset_peer': {'_': 'inputPeerEmpty'}, 'limit': 50, 'hash': 0}, []),
    'chat_history': ('messages.getHistory', ['peer', 'offset_id', 'offset_date', 'add_offset', 'limit', 'max_id', 'min_id', 'hash'], {'peer': 'InputPeer', 'offset_id': 'int', 'offset_date': 'int', 'add_offset': 'int', 'limit': 'int', 'max_id': 'int', 'min_id': 'int', 'hash': 'long'}, {'peer': '$peer', 'offset_id': 0, 'offset_date': 0, 'add_offset': 0, 'limit': 50, 'max_id': 0, 'min_id': 0, 'hash': 0}, []),
    'search_chat_messages': ('messages.search', ['peer', 'q', 'filter', 'min_date', 'max_date', 'offset_id', 'add_offset', 'limit', 'max_id', 'min_id', 'hash'], {'peer': 'InputPeer', 'q': 'string', 'from_id': 'flags.0?InputPeer', 'saved_peer_id': 'flags.2?InputPeer', 'saved_reaction': 'flags.3?Vector<Reaction>', 'top_msg_id': 'flags.1?int', 'filter': 'MessagesFilter', 'min_date': 'int', 'max_date': 'int', 'offset_id': 'int', 'add_offset': 'int', 'limit': 'int', 'max_id': 'int', 'min_id': 'int', 'hash': 'long'}, {'peer': '$peer', 'filter': {'_': 'inputMessagesFilterEmpty'}, 'offset_id': 0, 'add_offset': 0, 'limit': 50, 'max_id': 0, 'min_id': 0, 'hash': 0, 'min_date': 0, 'max_date': 0}, []),
    'mark_chat_read': ('messages.readHistory', ['peer', 'max_id'], {'peer': 'InputPeer', 'max_id': 'int'}, {'peer': '$peer'}, []),
    'clear_chat_history': ('messages.deleteHistory', ['peer', 'max_id'], {'just_clear': 'flags.0?true', 'revoke': 'flags.1?true', 'peer': 'InputPeer', 'max_id': 'int', 'min_date': 'flags.2?int', 'max_date': 'flags.3?int'}, {'peer': '$peer'}, []),
    'peer_settings': ('messages.getPeerSettings', ['peer'], {'peer': 'InputPeer'}, {'peer': '$peer'}, []),
    'basic_groups': ('messages.getChats', ['id'], {'id': 'Vector<long>'}, {}, []),
    'basic_group_details': ('messages.getFullChat', ['chat_id'], {'chat_id': 'long'}, {}, []),
    'search_all_messages': ('messages.searchGlobal', ['q', 'filter', 'min_date', 'max_date', 'offset_rate', 'offset_peer', 'offset_id', 'limit'], {'broadcasts_only': 'flags.1?true', 'groups_only': 'flags.2?true', 'users_only': 'flags.3?true', 'folder_id': 'flags.0?int', 'community': 'flags.4?InputChannel', 'q': 'string', 'filter': 'MessagesFilter', 'min_date': 'int', 'max_date': 'int', 'offset_rate': 'int', 'offset_peer': 'InputPeer', 'offset_id': 'int', 'limit': 'int'}, {'filter': {'_': 'inputMessagesFilterEmpty'}, 'offset_rate': 0, 'offset_peer': {'_': 'inputPeerEmpty'}, 'offset_id': 0, 'limit': 50, 'min_date': 0, 'max_date': 0}, []),
    'message_editability': ('messages.getMessageEditData', ['peer', 'id'], {'peer': 'InputPeer', 'id': 'int'}, {'peer': '$peer', 'id': '$message'}, []),
    'peer_dialogs': ('messages.getPeerDialogs', ['peers'], {'peers': 'Vector<InputDialogPeer>'}, {}, []),
    'all_drafts': ('messages.getAllDrafts', [], {}, {}, []),
    'mutual_groups': ('messages.getCommonChats', ['user_id', 'max_id', 'limit'], {'user_id': 'InputUser', 'max_id': 'long', 'limit': 'int'}, {'max_id': 0, 'limit': 50}, []),
    'set_dialog_pinned': ('messages.toggleDialogPin', ['peer'], {'pinned': 'flags.0?true', 'peer': 'InputDialogPeer'}, {}, []),
    'reorder_dialog_pins': ('messages.reorderPinnedDialogs', ['folder_id', 'order'], {'force': 'flags.0?true', 'folder_id': 'int', 'order': 'Vector<InputDialogPeer>'}, {}, []),
    'pinned_dialogs': ('messages.getPinnedDialogs', ['folder_id'], {'folder_id': 'int'}, {}, []),
    'unread_mentions': ('messages.getUnreadMentions', ['peer', 'offset_id', 'add_offset', 'limit', 'max_id', 'min_id'], {'peer': 'InputPeer', 'top_msg_id': 'flags.0?int', 'offset_id': 'int', 'add_offset': 'int', 'limit': 'int', 'max_id': 'int', 'min_id': 'int'}, {'peer': '$peer', 'offset_id': 0, 'add_offset': 0, 'limit': 50, 'max_id': 0, 'min_id': 0}, []),
    'mark_mentions_read': ('messages.readMentions', ['peer'], {'peer': 'InputPeer', 'top_msg_id': 'flags.0?int'}, {'peer': '$peer'}, []),
    'recent_live_locations': ('messages.getRecentLocations', ['peer', 'limit', 'hash'], {'peer': 'InputPeer', 'limit': 'int', 'hash': 'long'}, {'peer': '$peer', 'limit': 50, 'hash': 0}, []),
    'set_dialog_unread': ('messages.markDialogUnread', ['peer'], {'unread': 'flags.0?true', 'parent_peer': 'flags.1?InputPeer', 'peer': 'InputDialogPeer'}, {}, []),
    'unread_dialog_marks': ('messages.getDialogUnreadMarks', [], {'parent_peer': 'flags.0?InputPeer'}, {}, []),
    'clear_all_drafts': ('messages.clearAllDrafts', [], {}, {}, []),
    'chat_online_count': ('messages.getOnlines', ['peer'], {'peer': 'InputPeer'}, {'peer': '$peer'}, []),
    'message_type_counts': ('messages.getSearchCounters', ['peer', 'filters'], {'peer': 'InputPeer', 'saved_peer_id': 'flags.2?InputPeer', 'top_msg_id': 'flags.0?int', 'filters': 'Vector<MessagesFilter>'}, {'peer': '$peer'}, []),
    'dismiss_peer_settings_bar': ('messages.hidePeerSettingsBar', ['peer'], {'peer': 'InputPeer'}, {'peer': '$peer'}, []),
    'mark_discussion_read': ('messages.readDiscussion', ['peer', 'msg_id', 'read_max_id'], {'peer': 'InputPeer', 'msg_id': 'int', 'read_max_id': 'int'}, {'peer': '$peer', 'msg_id': '$message'}, []),
    'unpin_all_chat_messages': ('messages.unpinAllMessages', ['peer'], {'peer': 'InputPeer', 'top_msg_id': 'flags.0?int', 'saved_peer_id': 'flags.1?InputPeer'}, {'peer': '$peer'}, []),
    'message_readers': ('messages.getMessageReadParticipants', ['peer', 'msg_id'], {'peer': 'InputPeer', 'msg_id': 'int'}, {'peer': '$peer', 'msg_id': '$message'}, []),
    'message_search_calendar': ('messages.getSearchResultsCalendar', ['peer', 'filter', 'offset_id', 'offset_date'], {'peer': 'InputPeer', 'saved_peer_id': 'flags.2?InputPeer', 'filter': 'MessagesFilter', 'offset_id': 'int', 'offset_date': 'int'}, {'peer': '$peer', 'filter': {'_': 'inputMessagesFilterEmpty'}, 'offset_id': 0, 'offset_date': 0}, []),
    'message_search_positions': ('messages.getSearchResultsPositions', ['peer', 'filter', 'offset_id', 'limit'], {'peer': 'InputPeer', 'saved_peer_id': 'flags.2?InputPeer', 'filter': 'MessagesFilter', 'offset_id': 'int', 'limit': 'int'}, {'peer': '$peer', 'filter': {'_': 'inputMessagesFilterEmpty'}, 'offset_id': 0, 'limit': 50}, []),
    'set_default_send_as': ('messages.saveDefaultSendAs', ['peer', 'send_as'], {'peer': 'InputPeer', 'send_as': 'InputPeer'}, {'peer': '$peer'}, []),
    'sent_media_search': ('messages.searchSentMedia', ['q', 'filter', 'limit'], {'q': 'string', 'filter': 'MessagesFilter', 'limit': 'int'}, {'filter': {'_': 'inputMessagesFilterEmpty'}, 'limit': 50}, []),
    'outgoing_read_date': ('messages.getOutboxReadDate', ['peer', 'msg_id'], {'peer': 'InputPeer', 'msg_id': 'int'}, {'peer': '$peer', 'msg_id': '$message'}, []),
    'channel_message_author': ('channels.getMessageAuthor', ['channel', 'id'], {'channel': 'InputChannel', 'id': 'int'}, {'channel': '$peer'}, []),
    'move_dialogs_to_archive': ('folders.editPeerFolders', ['folder_peers'], {'folder_peers': 'Vector<InputFolderPeer>'}, {}, []),
    'chat_folders': ('messages.getDialogFilters', [], {}, {}, []),
    'suggested_chat_folders': ('messages.getSuggestedDialogFilters', [], {}, {}, []),
    'save_chat_folder': ('messages.updateDialogFilter', ['id'], {'id': 'int', 'filter': 'flags.0?DialogFilter'}, {}, []),
    'reorder_chat_folders': ('messages.updateDialogFiltersOrder', ['order'], {'order': 'Vector<int>'}, {}, []),
    'set_folder_tags_visible': ('messages.toggleDialogFilterTags', ['enabled'], {'enabled': 'Bool'}, {}, []),
    'share_chat_folder': ('chatlists.exportChatlistInvite', ['chatlist', 'title', 'peers'], {'chatlist': 'InputChatlist', 'title': 'string', 'peers': 'Vector<InputPeer>'}, {}, []),
    'remove_folder_invite': ('chatlists.deleteExportedInvite', ['chatlist', 'slug'], {'chatlist': 'InputChatlist', 'slug': 'string'}, {}, []),
    'edit_folder_invite': ('chatlists.editExportedInvite', ['chatlist', 'slug'], {'chatlist': 'InputChatlist', 'slug': 'string', 'title': 'flags.1?string', 'peers': 'flags.2?Vector<InputPeer>'}, {}, []),
    'folder_invites': ('chatlists.getExportedInvites', ['chatlist'], {'chatlist': 'InputChatlist'}, {}, []),
    'inspect_folder_invite': ('chatlists.checkChatlistInvite', ['slug'], {'slug': 'string'}, {}, []),
    'join_folder_invite': ('chatlists.joinChatlistInvite', ['slug', 'peers'], {'slug': 'string', 'peers': 'Vector<InputPeer>'}, {}, []),
    'folder_updates': ('chatlists.getChatlistUpdates', ['chatlist'], {'chatlist': 'InputChatlist'}, {}, []),
    'accept_folder_updates': ('chatlists.joinChatlistUpdates', ['chatlist', 'peers'], {'chatlist': 'InputChatlist', 'peers': 'Vector<InputPeer>'}, {}, []),
    'dismiss_folder_updates': ('chatlists.hideChatlistUpdates', ['chatlist'], {'chatlist': 'InputChatlist'}, {}, []),
    'folder_leave_suggestions': ('chatlists.getLeaveChatlistSuggestions', ['chatlist'], {'chatlist': 'InputChatlist'}, {}, []),
    'leave_shared_folder': ('chatlists.leaveChatlist', ['chatlist', 'peers'], {'chatlist': 'InputChatlist', 'peers': 'Vector<InputPeer>'}, {}, []),
    'edit_profile': ('account.updateProfile', [], {'first_name': 'flags.0?string', 'last_name': 'flags.1?string', 'about': 'flags.2?string'}, {}, []),
    'set_online_status': ('account.updateStatus', ['offline'], {'offline': 'Bool'}, {}, []),
    'check_profile_username': ('account.checkUsername', ['username'], {'username': 'string'}, {}, []),
    'set_profile_username': ('account.updateUsername', ['username'], {'username': 'string'}, {}, []),
    'privacy_rules': ('account.getPrivacy', ['key'], {'key': 'InputPrivacyKey'}, {}, []),
    'account_inactivity_ttl': ('account.getAccountTTL', [], {}, {}, []),
    'set_emoji_status': ('account.updateEmojiStatus', ['emoji_status'], {'emoji_status': 'EmojiStatus'}, {}, []),
    'suggested_emoji_statuses': ('account.getDefaultEmojiStatuses', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'recent_emoji_statuses': ('account.getRecentEmojiStatuses', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'clear_recent_emoji_statuses': ('account.clearRecentEmojiStatuses', [], {}, {}, []),
    'reorder_profile_usernames': ('account.reorderUsernames', ['order'], {'order': 'Vector<string>'}, {}, []),
    'set_profile_username_active': ('account.toggleUsername', ['username', 'active'], {'username': 'string', 'active': 'Bool'}, {}, []),
    'profile_photo_emojis': ('account.getDefaultProfilePhotoEmojis', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'set_profile_color': ('account.updateColor', [], {'for_profile': 'flags.1?true', 'color': 'flags.2?PeerColor'}, {}, []),
    'set_birthday': ('account.updateBirthday', [], {'birthday': 'flags.0?Birthday'}, {}, []),
    'set_personal_channel': ('account.updatePersonalChannel', ['channel'], {'channel': 'InputChannel'}, {}, []),
    'set_profile_tab': ('account.setMainProfileTab', ['tab'], {'tab': 'ProfileTab'}, {}, []),
    'set_music_saved': ('account.saveMusic', ['id'], {'unsave': 'flags.0?true', 'id': 'InputDocument', 'after_id': 'flags.1?InputDocument'}, {}, []),
    'saved_music_ids': ('account.getSavedMusicIds', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'user_details': ('users.getFullUser', ['id'], {'id': 'InputUser'}, {}, []),
    'contact_requirements': ('users.getRequirementsToContact', ['id'], {'id': 'Vector<InputUser>'}, {}, []),
    'user_music': ('users.getSavedMusic', ['id', 'offset', 'limit', 'hash'], {'id': 'InputUser', 'offset': 'int', 'limit': 'int', 'hash': 'long'}, {'offset': 0, 'limit': 50, 'hash': 0}, []),
    'user_music_by_documents': ('users.getSavedMusicByID', ['id', 'documents'], {'id': 'InputUser', 'documents': 'Vector<InputDocument>'}, {}, []),
    'suggest_contact_birthday': ('users.suggestBirthday', ['id', 'birthday'], {'id': 'InputUser', 'birthday': 'Birthday'}, {}, []),
    'select_profile_photo': ('photos.updateProfilePhoto', ['id'], {'fallback': 'flags.0?true', 'bot': 'flags.1?InputUser', 'id': 'InputPhoto'}, {}, []),
    'upload_profile_photo': ('photos.uploadProfilePhoto', [], {'fallback': 'flags.3?true', 'bot': 'flags.5?InputUser', 'file': 'flags.0?InputFile', 'video': 'flags.1?InputFile', 'video_start_ts': 'flags.2?double', 'video_emoji_markup': 'flags.4?VideoSize'}, {}, []),
    'remove_profile_photos': ('photos.deletePhotos', ['id'], {'id': 'Vector<InputPhoto>'}, {}, []),
    'profile_photos': ('photos.getUserPhotos', ['user_id', 'offset', 'max_id', 'limit'], {'user_id': 'InputUser', 'offset': 'int', 'max_id': 'long', 'limit': 'int'}, {'offset': 0, 'max_id': 0, 'limit': 50}, []),
    'set_contact_photo': ('photos.uploadContactProfilePhoto', ['user_id'], {'suggest': 'flags.3?true', 'save': 'flags.4?true', 'user_id': 'InputUser', 'file': 'flags.0?InputFile', 'video': 'flags.1?InputFile', 'video_start_ts': 'flags.2?double', 'video_emoji_markup': 'flags.5?VideoSize'}, {}, []),
    'notification_settings': ('account.getNotifySettings', ['peer'], {'peer': 'InputNotifyPeer'}, {}, []),
    'set_notification_settings': ('account.updateNotifySettings', ['peer', 'settings'], {'peer': 'InputNotifyPeer', 'settings': 'InputPeerNotifySettings'}, {}, []),
    'reset_notification_settings': ('account.resetNotifySettings', [], {}, {}, []),
    'contact_signup_notifications': ('account.getContactSignUpNotification', [], {}, {}, []),
    'set_contact_signup_notifications': ('account.setContactSignUpNotification', ['silent'], {'silent': 'Bool'}, {}, []),
    'notification_exceptions': ('account.getNotifyExceptions', [], {'compare_sound': 'flags.1?true', 'compare_stories': 'flags.2?true', 'peer': 'flags.0?InputNotifyPeer'}, {}, []),
    'reaction_notification_settings': ('account.getReactionsNotifySettings', [], {}, {}, []),
    'set_reaction_notification_settings': ('account.setReactionsNotifySettings', ['settings'], {'settings': 'ReactionsNotifySettings'}, {}, []),
    'saved_notification_sounds': ('account.getSavedRingtones', ['hash'], {'hash': 'long'}, {'hash': 0}, []),
    'set_notification_sound_saved': ('account.saveRingtone', ['id', 'unsave'], {'id': 'InputDocument', 'unsave': 'Bool'}, {}, []),
    'upload_notification_sound': ('account.uploadRingtone', ['file', 'file_name', 'mime_type'], {'file': 'InputFile', 'file_name': 'string', 'mime_type': 'string'}, {}, []),
    'rename_basic_group': ('messages.editChatTitle', ['chat_id', 'title'], {'chat_id': 'long', 'title': 'string'}, {}, []),
    'set_basic_group_photo': ('messages.editChatPhoto', ['chat_id', 'photo'], {'chat_id': 'long', 'photo': 'InputChatPhoto'}, {}, []),
    'add_basic_group_member': ('messages.addChatUser', ['chat_id', 'user_id', 'fwd_limit'], {'chat_id': 'long', 'user_id': 'InputUser', 'fwd_limit': 'int'}, {'fwd_limit': 0}, []),
    'remove_basic_group_member': ('messages.deleteChatUser', ['chat_id', 'user_id'], {'revoke_history': 'flags.0?true', 'chat_id': 'long', 'user_id': 'InputUser'}, {}, []),
    'create_basic_group': ('messages.createChat', ['users', 'title'], {'users': 'Vector<InputUser>', 'title': 'string', 'ttl_period': 'flags.0?int'}, {}, []),
    'set_basic_group_admin': ('messages.editChatAdmin', ['chat_id', 'user_id', 'is_admin'], {'chat_id': 'long', 'user_id': 'InputUser', 'is_admin': 'Bool'}, {}, []),
    'upgrade_basic_group': ('messages.migrateChat', ['chat_id'], {'chat_id': 'long'}, {}, []),
    'edit_chat_description': ('messages.editChatAbout', ['peer', 'about'], {'peer': 'InputPeer', 'about': 'string'}, {'peer': '$peer'}, []),
    'set_default_member_restrictions': ('messages.editChatDefaultBannedRights', ['peer', 'banned_rights'], {'peer': 'InputPeer', 'banned_rights': 'ChatBannedRights'}, {'peer': '$peer'}, []),
    'remove_basic_group': ('messages.deleteChat', ['chat_id'], {'chat_id': 'long'}, {}, []),
    'set_chat_auto_delete': ('messages.setHistoryTTL', ['peer', 'period'], {'peer': 'InputPeer', 'period': 'int'}, {'peer': '$peer'}, []),
    'set_content_protection': ('messages.toggleNoForwards', ['peer', 'enabled'], {'peer': 'InputPeer', 'enabled': 'Bool', 'request_msg_id': 'flags.0?int'}, {'peer': '$peer'}, []),
    'mark_channel_read': ('channels.readHistory', ['channel', 'max_id'], {'channel': 'InputChannel', 'max_id': 'int'}, {'channel': '$peer'}, []),
    'channel_members': ('channels.getParticipants', ['channel', 'filter', 'offset', 'limit', 'hash'], {'channel': 'InputChannel', 'filter': 'ChannelParticipantsFilter', 'offset': 'int', 'limit': 'int', 'hash': 'long'}, {'channel': '$peer', 'filter': {'_': 'channelParticipantsRecent'}, 'offset': 0, 'limit': 50}, []),
    'channel_member': ('channels.getParticipant', ['channel', 'participant'], {'channel': 'InputChannel', 'participant': 'InputPeer'}, {'channel': '$peer'}, []),
    'channel_details': ('channels.getFullChannel', ['channel'], {'channel': 'InputChannel'}, {'channel': '$peer'}, []),
    'create_channel': ('channels.createChannel', ['title', 'about'], {'broadcast': 'flags.0?true', 'megagroup': 'flags.1?true', 'for_import': 'flags.3?true', 'forum': 'flags.5?true', 'title': 'string', 'about': 'string', 'geo_point': 'flags.2?InputGeoPoint', 'address': 'flags.2?string', 'ttl_period': 'flags.4?int'}, {}, []),
    'set_channel_admin': ('channels.editAdmin', ['channel', 'user_id', 'admin_rights'], {'channel': 'InputChannel', 'user_id': 'InputUser', 'admin_rights': 'ChatAdminRights', 'rank': 'flags.0?string'}, {'channel': '$peer'}, []),
    'rename_channel': ('channels.editTitle', ['channel', 'title'], {'channel': 'InputChannel', 'title': 'string'}, {'channel': '$peer'}, []),
    'set_channel_photo': ('channels.editPhoto', ['channel', 'photo'], {'channel': 'InputChannel', 'photo': 'InputChatPhoto'}, {'channel': '$peer'}, []),
    'check_channel_username': ('channels.checkUsername', ['channel', 'username'], {'channel': 'InputChannel', 'username': 'string'}, {'channel': '$peer'}, []),
    'set_channel_username': ('channels.updateUsername', ['channel', 'username'], {'channel': 'InputChannel', 'username': 'string'}, {'channel': '$peer'}, []),
    'join_channel': ('channels.joinChannel', ['channel'], {'channel': 'InputChannel'}, {'channel': '$peer'}, []),
    'leave_channel': ('channels.leaveChannel', ['channel'], {'channel': 'InputChannel'}, {'channel': '$peer'}, []),
    'invite_channel_members': ('channels.inviteToChannel', ['channel', 'users'], {'channel': 'InputChannel', 'users': 'Vector<InputUser>'}, {'channel': '$peer'}, []),
    'remove_channel': ('channels.deleteChannel', ['channel'], {'channel': 'InputChannel'}, {'channel': '$peer'}, []),
    'set_channel_signatures': ('channels.toggleSignatures', ['channel'], {'signatures_enabled': 'flags.0?true', 'profiles_enabled': 'flags.1?true', 'channel': 'InputChannel'}, {'channel': '$peer'}, []),
    'owned_public_channels': ('channels.getAdminedPublicChannels', [], {'by_location': 'flags.0?true', 'check_limit': 'flags.1?true', 'for_personal': 'flags.2?true', 'for_community_peer': 'flags.3?true'}, {}, []),
    'restrict_channel_member': ('channels.editBanned', ['channel', 'participant', 'banned_rights'], {'channel': 'InputChannel', 'participant': 'InputPeer', 'banned_rights': 'ChatBannedRights'}, {'channel': '$peer'}, []),
    'channel_admin_log': ('channels.getAdminLog', ['channel', 'q', 'max_id', 'min_id', 'limit'], {'channel': 'InputChannel', 'q': 'string', 'events_filter': 'flags.0?ChannelAdminLogEventsFilter', 'admins': 'flags.1?Vector<InputUser>', 'max_id': 'long', 'min_id': 'long', 'limit': 'int'}, {'channel': '$peer', 'q': '', 'limit': 50}, []),
    'set_group_sticker_set': ('channels.setStickers', ['channel', 'stickerset'], {'channel': 'InputChannel', 'stickerset': 'InputStickerSet'}, {'channel': '$peer'}, []),
    'mark_channel_media_read': ('channels.readMessageContents', ['channel', 'id'], {'channel': 'InputChannel', 'id': 'Vector<int>'}, {'channel': '$peer'}, []),
    'clear_channel_history': ('channels.deleteHistory', ['channel', 'max_id'], {'for_everyone': 'flags.0?true', 'channel': 'InputChannel', 'max_id': 'int'}, {'channel': '$peer'}, []),
    'set_prehistory_hidden': ('channels.togglePreHistoryHidden', ['channel', 'enabled'], {'channel': 'InputChannel', 'enabled': 'Bool'}, {'channel': '$peer'}, []),
    'left_channels': ('channels.getLeftChannels', ['offset'], {'offset': 'int'}, {'offset': 0}, []),
    'eligible_discussion_groups': ('channels.getGroupsForDiscussion', [], {}, {}, []),
    'set_discussion_group': ('channels.setDiscussionGroup', ['broadcast', 'group'], {'broadcast': 'InputChannel', 'group': 'InputChannel'}, {}, []),
    'set_group_location': ('channels.editLocation', ['channel', 'geo_point', 'address'], {'channel': 'InputChannel', 'geo_point': 'InputGeoPoint', 'address': 'string'}, {'channel': '$peer'}, []),
    'set_slow_mode': ('channels.toggleSlowMode', ['channel', 'seconds'], {'channel': 'InputChannel', 'seconds': 'int'}, {'channel': '$peer'}, []),
    'inactive_channels': ('channels.getInactiveChannels', [], {}, {}, []),
    'send_as_identities': ('channels.getSendAs', ['peer'], {'for_paid_reactions': 'flags.0?true', 'for_live_stories': 'flags.1?true', 'peer': 'InputPeer'}, {'peer': '$peer'}, []),
    'remove_member_history': ('channels.deleteParticipantHistory', ['channel', 'participant'], {'channel': 'InputChannel', 'participant': 'InputPeer'}, {'channel': '$peer'}, []),
    'set_join_to_send': ('channels.toggleJoinToSend', ['channel', 'enabled'], {'channel': 'InputChannel', 'enabled': 'Bool'}, {'channel': '$peer'}, []),
    'set_join_requests': ('channels.toggleJoinRequest', ['channel', 'enabled'], {'apply_to_invites': 'flags.1?true', 'channel': 'InputChannel', 'enabled': 'Bool', 'guard_bot': 'flags.0?InputUser'}, {'channel': '$peer'}, []),
    'reorder_channel_usernames': ('channels.reorderUsernames', ['channel', 'order'], {'channel': 'InputChannel', 'order': 'Vector<string>'}, {'channel': '$peer'}, []),
    'set_channel_username_active': ('channels.toggleUsername', ['channel', 'username', 'active'], {'channel': 'InputChannel', 'username': 'string', 'active': 'Bool'}, {'channel': '$peer'}, []),
    'deactivate_channel_usernames': ('channels.deactivateAllUsernames', ['channel'], {'channel': 'InputChannel'}, {'channel': '$peer'}, []),
    'set_antispam': ('channels.toggleAntiSpam', ['channel', 'enabled'], {'channel': 'InputChannel', 'enabled': 'Bool'}, {'channel': '$peer'}, []),
    'set_members_hidden': ('channels.toggleParticipantsHidden', ['channel', 'enabled'], {'channel': 'InputChannel', 'enabled': 'Bool'}, {'channel': '$peer'}, []),
    'set_channel_color': ('channels.updateColor', ['channel'], {'for_profile': 'flags.1?true', 'channel': 'InputChannel', 'color': 'flags.2?int', 'background_emoji_id': 'flags.0?long'}, {'channel': '$peer'}, []),
    'related_channels': ('channels.getChannelRecommendations', [], {'channel': 'flags.0?InputChannel'}, {}, []),
    'set_channel_emoji_status': ('channels.updateEmojiStatus', ['channel', 'emoji_status'], {'channel': 'InputChannel', 'emoji_status': 'EmojiStatus'}, {'channel': '$peer'}, []),
    'set_group_emoji_set': ('channels.setEmojiStickers', ['channel', 'stickerset'], {'channel': 'InputChannel', 'stickerset': 'InputStickerSet'}, {'channel': '$peer'}, []),
    'set_channel_auto_translation': ('channels.toggleAutotranslation', ['channel', 'enabled'], {'channel': 'InputChannel', 'enabled': 'Bool'}, {'channel': '$peer'}, []),
    'set_channel_profile_tab': ('channels.setMainProfileTab', ['channel', 'tab'], {'channel': 'InputChannel', 'tab': 'ProfileTab'}, {'channel': '$peer'}, []),
    'edit_member_rank': ('messages.editChatParticipantRank', ['peer', 'participant', 'rank'], {'peer': 'InputPeer', 'participant': 'InputPeer', 'rank': 'string'}, {'peer': '$peer'}, []),
    'create_chat_invite': ('messages.exportChatInvite', ['peer'], {'legacy_revoke_permanent': 'flags.2?true', 'request_needed': 'flags.3?true', 'peer': 'InputPeer', 'expire_date': 'flags.0?int', 'usage_limit': 'flags.1?int', 'title': 'flags.4?string', 'subscription_pricing': 'flags.5?StarsSubscriptionPricing'}, {'peer': '$peer'}, ['subscription_pricing']),
    'inspect_chat_invite': ('messages.checkChatInvite', ['hash'], {'hash': 'string'}, {}, []),
    'join_chat_invite': ('messages.importChatInvite', ['hash'], {'hash': 'string'}, {}, []),
    'chat_invites': ('messages.getExportedChatInvites', ['peer', 'admin_id', 'limit'], {'revoked': 'flags.3?true', 'peer': 'InputPeer', 'admin_id': 'InputUser', 'offset_date': 'flags.2?int', 'offset_link': 'flags.2?string', 'limit': 'int'}, {'peer': '$peer', 'limit': 50}, []),
    'chat_invite_details': ('messages.getExportedChatInvite', ['peer', 'link'], {'peer': 'InputPeer', 'link': 'string'}, {'peer': '$peer'}, []),
    'edit_chat_invite': ('messages.editExportedChatInvite', ['peer', 'link'], {'revoked': 'flags.2?true', 'peer': 'InputPeer', 'link': 'string', 'expire_date': 'flags.0?int', 'usage_limit': 'flags.1?int', 'request_needed': 'flags.3?Bool', 'title': 'flags.4?string'}, {'peer': '$peer'}, []),
    'purge_revoked_chat_invites': ('messages.deleteRevokedExportedChatInvites', ['peer', 'admin_id'], {'peer': 'InputPeer', 'admin_id': 'InputUser'}, {'peer': '$peer'}, []),
    'remove_chat_invite': ('messages.deleteExportedChatInvite', ['peer', 'link'], {'peer': 'InputPeer', 'link': 'string'}, {'peer': '$peer'}, []),
    'invite_admins': ('messages.getAdminsWithInvites', ['peer'], {'peer': 'InputPeer'}, {'peer': '$peer'}, []),
    'invite_joiners': ('messages.getChatInviteImporters', ['peer', 'offset_date', 'offset_user', 'limit'], {'requested': 'flags.0?true', 'subscription_expired': 'flags.3?true', 'peer': 'InputPeer', 'link': 'flags.1?string', 'q': 'flags.2?string', 'offset_date': 'int', 'offset_user': 'InputUser', 'limit': 'int'}, {'peer': '$peer', 'offset_date': 0, 'offset_user': {'_': 'inputUserEmpty'}, 'limit': 50}, []),
    'resolve_join_request': ('messages.hideChatJoinRequest', ['peer', 'user_id'], {'approved': 'flags.0?true', 'peer': 'InputPeer', 'user_id': 'InputUser'}, {'peer': '$peer'}, []),
    'resolve_all_join_requests': ('messages.hideAllChatJoinRequests', ['peer'], {'approved': 'flags.0?true', 'peer': 'InputPeer', 'link': 'flags.1?string'}, {'peer': '$peer'}, []),
    'channel_statistics': ('stats.getBroadcastStats', ['channel'], {'dark': 'flags.0?true', 'channel': 'InputChannel'}, {'channel': '$peer'}, []),
    'supergroup_statistics': ('stats.getMegagroupStats', ['channel'], {'dark': 'flags.0?true', 'channel': 'InputChannel'}, {'channel': '$peer'}, []),
    'message_statistics': ('stats.getMessageStats', ['channel', 'msg_id'], {'dark': 'flags.0?true', 'channel': 'InputChannel', 'msg_id': 'int'}, {'channel': '$peer'}, []),
    'message_public_forwards': ('stats.getMessagePublicForwards', ['channel', 'msg_id', 'offset', 'limit'], {'channel': 'InputChannel', 'msg_id': 'int', 'offset': 'string', 'limit': 'int'}, {'channel': '$peer', 'offset': '', 'limit': 50}, []),
    'story_statistics': ('stats.getStoryStats', ['peer', 'id'], {'dark': 'flags.0?true', 'peer': 'InputPeer', 'id': 'int'}, {'peer': '$peer', 'id': '$message'}, []),
    'story_public_forwards': ('stats.getStoryPublicForwards', ['peer', 'id', 'offset', 'limit'], {'peer': 'InputPeer', 'id': 'int', 'offset': 'string', 'limit': 'int'}, {'peer': '$peer', 'id': '$message', 'offset': '', 'limit': 50}, []),
}


async def _catalog_convert(target: MessageOperations, kind: str, value: Any) -> Any:
    kind = kind.split("?", 1)[-1]
    if kind.startswith("Vector<"):
        if not isinstance(value, (list, tuple)):
            raise TypeError("expected a sequence for " + kind)
        return [await _catalog_convert(target, kind[7:-1], item) for item in cast('list[Any]', value)]
    if kind in {"InputPeer", "InputUser", "InputChannel", "InputDialogPeer", "InputNotifyPeer"}:
        if isinstance(value, dict):
            data = cast('dict[str, Any]', value)
            if str(data.get("_", "")).startswith({"InputUser": "inputUser", "InputChannel": "inputChannel", "InputDialogPeer": "inputDialogPeer", "InputNotifyPeer": "inputNotify"}.get(kind, "inputPeer")):
                return data
        peer: Any = await MessageOperations(target.ctx, {"chat_id": value}).get_input_chat()
        if kind == "InputPeer":
            return peer
        if kind == "InputDialogPeer":
            return {"_": "inputDialogPeer", "peer": peer}
        if kind == "InputNotifyPeer":
            return {"_": "inputNotifyPeer", "peer": peer}
        tag = str(_msg_field(peer, "_", ""))
        if kind == "InputUser" and tag == "inputPeerSelf":
            return {"_": "inputUserSelf"}
        if kind == "InputUser" and tag == "inputPeerUser":
            return {"_": "inputUser", "user_id": peer["user_id"], "access_hash": peer["access_hash"]}
        if kind == "InputChannel" and tag == "inputPeerChannel":
            return {"_": "inputChannel", "channel_id": peer["channel_id"], "access_hash": peer["access_hash"]}
        raise ValueError("peer is not a " + kind)
    if kind == "TextWithEntities":
        return _ctx_text(value)
    if kind == "TodoItem":
        return _ctx_todo_items([value])[0]
    if kind == "InputMessage" and isinstance(value, int) and not isinstance(value, bool):
        return {"_": "inputMessageID", "id": value}
    if kind == "InputChatlist" and isinstance(value, int) and not isinstance(value, bool):
        return {"_": "inputChatlistDialogFilter", "filter_id": value}
    if kind == "InputStickerSet" and isinstance(value, str):
        return {"_": "inputStickerSetShortName", "short_name": value}
    if kind == "Reaction":
        if isinstance(value, str):
            return {"_": "reactionEmoji", "emoticon": value}
        if isinstance(value, int) and not isinstance(value, bool):
            return {"_": "reactionCustomEmoji", "document_id": value}
    if kind == "ChatReactions":
        if value in ("all", "none"):
            return {"_": "chatReactionsAll" if value == "all" else "chatReactionsNone"}
        if isinstance(value, (list, tuple)):
            return {"_": "chatReactionsSome", "reactions": await _catalog_convert(target, "Vector<Reaction>", value)}
    if kind == "PollAnswer" and isinstance(value, str):
        return {"_": "inputPollAnswer", "text": _ctx_text(value)}
    if kind == "InputFolderPeer" and isinstance(value, dict):
        data = dict(cast('dict[str, Any]', value))
        data["_"] = "inputFolderPeer"
        data["peer"] = await _catalog_convert(target, "InputPeer", data["peer"])
        return data
    if kind in {"ChatAdminRights", "ChatBannedRights", "InputPeerNotifySettings"} and isinstance(value, dict):
        data = dict(cast('dict[str, Any]', value))
        data.setdefault("_", kind[:1].lower() + kind[1:])
        if kind == "ChatBannedRights":
            data.setdefault("until_date", 0)
        return data
    if kind in {"InputDocument", "InputPhoto"} and isinstance(value, dict):
        data = cast('dict[str, Any]', value)
        if str(data.get("_", "")).startswith(kind[:1].lower() + kind[1:]):
            return dict(data)
        return {"_": kind[:1].lower() + kind[1:], "id": data["id"], "access_hash": data["access_hash"], "file_reference": data["file_reference"]}
    if kind in {"int", "long"} and (not isinstance(value, int) or isinstance(value, bool)):
        raise TypeError("expected integer")
    if kind == "string" and not isinstance(value, str):
        raise TypeError("expected string")
    if kind in {"true", "Bool"} and not isinstance(value, bool):
        raise TypeError("expected boolean")
    if kind == "bytes" and not isinstance(value, bytes):
        raise TypeError("expected bytes")
    return value


def _catalog_guard(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in cast('dict[str, Any]', value).items():
            if key in {"allow_paid_stars", "allow_paid_floodskip", "stars", "quick_reply_shortcut", "suggested_post", "subscription_pricing"} and item is not None and item is not False and item != 0:
                raise ValueError("paid or business fields require explicit raw API use")
            if key == "_" and item == "reactionPaid":
                raise ValueError("paid reactions are not supported by these helpers")
            _catalog_guard(item)
    elif isinstance(value, (list, tuple)):
        for item in cast('list[Any]', value):
            _catalog_guard(item)


def _catalog_operation(name: str, spec: tuple[Any, ...]) -> Callable[..., Awaitable[Any]]:
    method, required, field_types, defaults, forbidden = spec
    async def operation(target: MessageOperations, *args: Any, **kwargs: Any) -> Any:
        if len(args) > len(required):
            raise TypeError(name + ": too many positional arguments")
        data = dict(kwargs)
        explicit_peer = data.get("peer", data.get("channel", data.get("chat_id", _SENTINEL)))
        if "chat_id" in data and "chat_id" not in field_types:
            key = "peer" if "peer" in field_types else "channel" if "channel" in field_types else None
            if key is None or key in data:
                raise TypeError(name + ": invalid or duplicate chat_id")
            data[key] = data.pop("chat_id")
        if "message_id" in data:
            key = "msg_id" if "msg_id" in field_types else "id" if field_types.get("id") == "int" else None
            if key is None or key in data:
                raise TypeError(name + ": invalid or duplicate message_id")
            data[key] = data.pop("message_id")
        if "topic_id" in data and "topic_id" not in field_types:
            if "top_msg_id" not in field_types or "top_msg_id" in data:
                raise TypeError(name + ": this operation does not accept topic_id")
            data["top_msg_id"] = data.pop("topic_id")
        for field, value in zip(required, args):
            if field in data:
                raise TypeError(name + ": duplicate argument " + field)
            data[field] = value
        explicit_peer = data.get("peer", data.get("channel", explicit_peer))
        unknown = set(data) - set(field_types)
        if unknown:
            raise TypeError(name + ": unknown arguments " + ", ".join(sorted(unknown)))
        for field in forbidden:
            if field in data:
                raise ValueError(name + ": unsupported field " + field)
        for field, value in defaults.items():
            if field in data:
                continue
            if value == "$peer":
                value = target.chat_id
            elif value == "$message":
                value = target.id if explicit_peer is _SENTINEL or explicit_peer == target.chat_id else None
            elif value == "$random":
                value = secrets.randbits(63)
            if value is not None:
                data[field] = value
        missing = [field for field in required if field not in data or data[field] is None]
        if missing:
            raise TypeError(name + ": missing " + ", ".join(missing))
        _catalog_guard(data)
        converted = {field: await _catalog_convert(target, field_types[field], value) for field, value in data.items() if value is not None}
        return await target.ctx.mt(method, **converted)
    operation.__name__ = name
    return operation


for _name, _spec in CTX_CATALOG.items():
    if _name in CTX_OPERATIONS:
        raise RuntimeError("duplicate ctx operation: " + _name)
    CTX_OPERATIONS[_name] = _catalog_operation(_name, _spec)


async def _ctx_translate(target: MessageOperations, text: Any, to_lang: str | None = None, *, source: str = "auto", provider: str = "auto", **kwargs: Any) -> str:
    if to_lang is None:
        to_lang = text
        text = target.raw
    if isinstance(text, int) and not isinstance(text, bool):
        text = await target.ctx.msg(target.chat_id, text)
    if not isinstance(text, str):
        raw = _msg_field(text, "raw", text)
        text = _msg_field(raw, "message", _msg_field(raw, "text", _msg_field(raw, "caption")))
    if not isinstance(text, str):
        raise ValueError("translation requires text or a text message")
    async def telegram(value: str, language: str, source_language: str) -> str:
        try:
            result = await target.ctx.cap("mt", {"method": "messages.translateText", "kwargs": {"text": [_ctx_text(value)], "to_lang": language}, "timeout": min(kwargs.get("timeout", 15.0), kwargs.get("attempt_timeout", 7.0))})
        except PermissionError:
            raise
        except Exception as exc:
            raise OSError("Telegram translation failed: " + type(exc).__name__) from None
        body = _msg_field(result, "result", result)
        if isinstance(body, dict):
            body = _msg_field(body, "result")
        if not isinstance(body, list) or not body:
            raise RuntimeError("Telegram returned no translated text")
        parts = [_msg_field(item, "text") for item in cast('list[Any]', body)]
        if any(not isinstance(item, str) for item in parts):
            raise RuntimeError("Telegram returned invalid translated text")
        return "".join(cast('list[str]', parts))
    async def request(payload: dict[str, Any]) -> dict[str, Any]:
        result = await target.ctx.cap("net", payload)
        if not isinstance(result, dict):
            raise RuntimeError("network provider returned invalid response")
        return cast('dict[str, Any]', result)
    return str(await target.ctx._ctx_translate_text(text, to_lang, source=source, provider=provider, request=request, telegram=telegram, **kwargs))


CTX_OPERATIONS["translate"] = _ctx_translate
