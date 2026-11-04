from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
import time
from pathlib import Path
from typing import Any, cast

from goygram import ext

from relay.emoji import to_rich
from relay.files import document, put
from relay.firewall import trusted_scope

_assets = Path(__file__).parent / "assets"
_banners = {
    "brand": ("hotaru-brand", "photo", "banner.png"),
    "banner": ("hotaru-banner", "photo", "banner.png"),
    "logo": ("hotaru-logo", "photo", "banner.png"),
    "help": ("hotaru-help", "video", "help.mp4"),
    "info": ("hotaru-info", "video", "info.mp4"),
    "settings": ("hotaru-settings", "video", "settings.mp4"),
    "backups": ("hotaru-backups", "video", "backups.mp4"),
    "catalyst": ("hotaru-catalyst", "video", "catalyst.mp4"),
    "executor": ("hotaru-executor", "video", "executor.mp4"),
    "logger": ("hotaru-logger", "video", "logger.mp4"),
    "ping": ("hotaru-ping", "video", "ping.mp4"),
    "power": ("hotaru-power", "video", "power.mp4"),
    "singularity": ("hotaru-singularity", "video", "singularity.mp4"),
    "translations": ("hotaru-translations", "video", "translations.mp4"),
    "updater": ("hotaru-updater", "video", "updater.mp4"),
    "accounts": ("hotaru-accounts", "video", "accounts.mp4"),
    "acl": ("hotaru-acl", "video", "acl.mp4"),
}


def banner(name: str = "brand") -> str:
    entry = _banners.get(name)
    if entry is None:
        return ""
    media_id, kind, filename = entry
    tag = "video" if kind == "video" else "img"
    return f'<{tag} src="tg://{kind}?id={media_id}"/>'


class Branding:
    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self._lock = asyncio.Lock()
        self._cache: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}

    async def rich(self, text: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"_": "inputRichMessageHTML", **to_rich(text)}
        needed = [(mid, kind, name) for mid, kind, name in _banners.values() if f"tg://{kind}?id={mid}" in text]
        if not needed:
            if "html" in payload:
                payload["html"] = re.sub(r'<(?:img|video)\s+src="https?://[^"]*"\s*/?>', "", payload["html"])
            return payload
        app = self.runtime.inline.bot_app
        files: list[dict[str, Any]] = []
        async with self._lock:
            for media_id, kind, filename in needed:
                key = (id(app), media_id)
                cached = self._cache.get(key)
                if cached is not None and time.monotonic() - cached[0] < 1800:
                    files.append(cached[1])
                    continue
                try:
                    entry = await self._upload(app, media_id, kind, filename)
                except Exception as exc:
                    tag = "video" if kind == "video" else "img"
                    payload["html"] = payload["html"].replace(f'<{tag} src="tg://{kind}?id={media_id}"/>', "")
                    if self.runtime.observatory is not None:
                        self.runtime.observatory.emit("branding", "banner_failed", asset=media_id, error=type(exc).__name__)
                    continue
                self._cache[key] = (time.monotonic(), entry)
                files.append(entry)
        if files:
            payload["files"] = files
        if "html" in payload:
            payload["html"] = re.sub(r'<(?:img|video)\s+src="https?://[^"]*"\s*/?>', "", payload["html"])
        return payload

    async def _upload(self, app: Any, media_id: str, kind: str, filename: str) -> dict[str, Any]:
        up = await put(app, str(_assets / filename), file_name=filename)
        upload = document(up, mime="video/mp4" if kind == "video" else "image/png", force_file=False)
        if kind == "photo":
            media = {"_": "inputMediaUploadedPhoto", "file": upload["file"]}
        else:
            media = upload
            media["attributes"].extend([
                {"_": "documentAttributeAnimated"},
                {"_": "documentAttributeVideo", "duration": 4.0, "w": 1920, "h": 1080, "supports_streaming": True},
            ])
        with trusted_scope():
            result = await app.mt_messages_upload_media(peer={"_": "inputPeerSelf"}, media=media)
        if not isinstance(result, dict):
            result = result.to_dict()
        result = cast(dict[str, Any], result)
        result = result.get("result", result)
        source: Any = cast(dict[str, Any], result)["photo" if kind == "photo" else "document"]
        if not isinstance(source, dict):
            source = source.to_dict()
        source = cast(dict[str, Any], source)
        ref: dict[str, Any] = {"_": "inputPhoto" if kind == "photo" else "inputDocument", **{k: source[k] for k in ("id", "access_hash", "file_reference")}}
        return {"_": "inputRichFilePhoto" if kind == "photo" else "inputRichFileDocument", "id": media_id, "photo" if kind == "photo" else "document": ref}

    async def apply_avatars(self, forum: Any, chat_id: int) -> None:
        state = self.runtime.state
        app = self.runtime.app
        path = _assets / "logo.png"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        info = getattr(self.runtime.inline, "info", None)
        targets = [("forum", chat_id)]
        if info is not None:
            targets.append(("bot", info.bot_id))
        for kind, target in targets:
            key = f"brand-avatar-{kind}-{target}"
            should_update = state.get_setting(key) != digest
            if not should_update and kind == "forum":
                try:
                    peer = forum._user_channel(chat_id)
                    if peer is not None:
                        full = await app.mt_req("channels.getFullChannel", channel=peer)
                        chats = full.get("chats", []) if isinstance(full, dict) else getattr(full, "chats", [])
                        if chats:
                            chat_obj = chats[0] if isinstance(chats[0], dict) else chats[0].to_dict() if hasattr(chats[0], "to_dict") else {}
                            photo = chat_obj.get("photo")
                            if not photo or (isinstance(photo, dict) and photo.get("_") == "chatPhotoEmpty"):
                                should_update = True
                except Exception:
                    pass
            if not should_update:
                continue
            try:
                if kind == "forum":
                    peer = forum._user_channel(chat_id)
                    if peer is None:
                        continue
                else:
                    assert info is not None
                    peer = await forum._user_bot(info.bot_id, info.username)
                    if peer is None:
                        continue
                up = await put(app, str(path), file_name=path.name)
                file = document(up)["file"]
                with trusted_scope():
                    if kind == "forum":
                        await app.mt_channels_edit_photo(channel=peer, photo={"_": "inputChatUploadedPhoto", "file": file})
                    else:
                        await app.mt_photos_upload_profile_photo(bot=peer, file=file)
                state.set_setting(key, digest)
            except Exception as exc:
                if self.runtime.observatory is not None:
                    self.runtime.observatory.emit("branding", "avatar_failed", target=kind, error=type(exc).__name__)
        try:
            peer = forum._user_channel(chat_id)
            if peer is not None:
                prem = False
                if hasattr(self.runtime, "is_premium"):
                    val = self.runtime.is_premium()
                    prem = await val if inspect.isawaitable(val) else bool(val)
                if prem:
                    from relay.proxies import topic_emoji_id
                    res = await app.mt_req("messages.getForumTopics", peer=peer, offset_date=0, offset_id=0, offset_topic=0, limit=100)
                    topics = res.get("topics", []) if isinstance(res, dict) else getattr(res, "topics", [])
                    for item in topics:
                        top = item if isinstance(item, dict) else item.to_dict() if hasattr(item, "to_dict") else {}
                        if isinstance(top.get("id"), int) and not top.get("icon_emoji_id"):
                            eid = topic_emoji_id(str(top.get("title") or ""))
                            if eid:
                                await app.mt_req("messages.editForumTopic", peer=peer, topic_id=int(top["id"]), icon_emoji_id=eid)
        except Exception as exc:
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("branding", "topic_emoji_sync_failed", error=type(exc).__name__)

    async def apply_bot_avatar(self, info: Any) -> None:
        state = self.runtime.state
        app = self.runtime.app
        key = f"brand-avatar-bot-{info.bot_id}"
        try:
            path = _assets / "logo.png"
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if state.get_setting(key) == digest:
                return
            with trusted_scope():
                peer = await app.mt.resolve_peer("@" + info.username)
            if isinstance(peer, bytes):
                peer = ext.deserialize_constructor(peer)
            elif not isinstance(peer, dict):
                peer = peer.to_dict()
            peer = cast(dict[str, Any], peer)
            if peer.get("_") != "inputPeerUser" or peer.get("user_id") != info.bot_id:
                raise ValueError("resolved bot identity does not match")
            bot = {"_": "inputUser", "user_id": info.bot_id, "access_hash": peer["access_hash"]}
            up = await put(app, str(path), file_name=path.name)
            with trusted_scope():
                await app.mt_photos_upload_profile_photo(bot=bot, file=document(up)["file"])
            state.set_setting(key, digest)
        except Exception as exc:
            if self.runtime.observatory is not None:
                self.runtime.observatory.emit("branding", "avatar_failed", target="bot", error=type(exc).__name__)
